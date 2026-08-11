<!-- GENERATED from AGENTS.md by scripts/sync_agent_rules.py — do not edit.
     Claude Code's on-demand loader injects this file verbatim and does NOT expand
     @imports, so the rules must be physically present here. Edit AGENTS.md. -->

# workflows/

The v3 review pipeline. `pipeline.js` is invoked by the skill through the `Workflow` tool
(`scriptPath` + args); it has no disk, shell, or `process.env`.

## Runtime

- **Node 24 is the runtime.** JS tests run under `node --test`. No npm, no `package.json`, no
  `node_modules` — Node builtins and language globals only. Stable `Array.prototype.sort` is relied on.
- **Only JSON-safe language globals exist in the sandbox.** `structuredClone`, `Buffer`,
  `TextEncoder`/`TextDecoder`, `URL`, `setTimeout`/`queueMicrotask`, `process` and `console` are
  provided by `node --test` but **not** by the sandbox: a reference throws on the first live
  dispatch while every test stays green. Deep-clone with `deepClone`, never `structuredClone`.
  `pipeline_run.test.js` and `stages_persist.test.js` run with all of these except `URL`,
  `process` and `console` deleted from `globalThis`; a reference to one of those three is caught
  only on a live dispatch.
- **No wall-clock.** Timestamps arrive through the args waist (`generatedAt`); environment values
  are read by the skill and passed in `policy`.

## JS lint (CI)

CI job `js-lint` runs Biome 2.5.6 against this directory via `workflows/biome.json`
(lint-only). Formatter is **off**: default `lineWidth: 80` would wrap long
`import … from` lines and break `build.js`'s single-line import `strip()` regex,
failing `tests/test_bundle_fresh.py`. `noRestrictedGlobals` applies to `src/`
only; `test/**` and `build.js` override it (they legitimately use `process` /
`console`).

Deferred rules (measured 2026-07-30, HEAD `ebf399d`) — hit counts are why they
stay off so a later re-evaluation need not re-derive them:

| Rule | Hits | Why deferred |
| --- | --- | --- |
| `useOptionalChain` | 51 | Style churn; no reliability delta |
| `noUnusedFunctionParameters` | 55 | Dominated by positional mock callback params in tests |
| `noAssignInExpressions` | 7 | Six are intentional `args.js` pattern |
| `noControlCharactersInRegex` | 2 | Intentional U+0000/U+001F sanitization in `args.js` |
| `organizeImports` | 24 | Assist/format-adjacent; out of scope with formatter off |

## The bundle

- `pipeline.js` is **generated** — never hand-edit it. Source is `src/*.js`, which may use ESM
  `import`/`export` for tests only; `build.js` strips those and concatenates.
- Only a single-line `import … from './sibling.js'` is strippable; `build.js` (`unsafeImports`)
  fails on anything else. A `node:`/bare specifier inlines nothing, so stripping it ships an
  undefined reference; a side-effect or multi-line import `strip()` never matches ships verbatim.
  Neither lint nor the bundle-fresh check sees either — inline the value into `src/`.
- Rebuild after any source change: `node workflows/build.js`. `tests/test_bundle_fresh.py` requires
  the committed bundle to be byte-identical to a fresh build, and import-free.
- **`meta.name` must never equal the skill's name.** Both are registered by this plugin; an
  identical name makes `/code-gauntlet:code-gauntlet` resolve to the workflow, which then receives a
  raw user string instead of the args waist.

## Persistence

**Artifacts reach disk through the workflow's own return value, not through an agent.** A language
model asked to reproduce large escape-dense JSON verbatim does not: measured across every recorded
run, 26 of 73 writes failed their own content proof, ranging from escape mangling to silently
rewriting long prose shorter, and nothing about a document predicts which fails. So the Persist
stage returns the three primaries at `persistReturn` and the *harness* serializes them —
byte-exact at every size probed to 4 MB, against ~66 KB of unique content in the largest recorded
run. **The content proof survives the move and now grades the harness-written copy; do not remove
it, and do not try to fix transcription with a different encoding — re-encoding was measured and
it does not work.**

- The two budgets are unrelated and must not re-merge. `PROMPT_SEGMENT_CHAR_BUDGET` (100k) sizes
  what a model reads; `RETURN_CHAR_BUDGET` (1M) sizes what the harness serializes. Grading resume
  state or the returned primaries against the prompt budget throws away recoverable runs.
- `fnv1a32` is defined over UTF-16 code units and **must agree between runtimes** — JS uses
  `charCodeAt` + `Math.imul`; Python reads `utf-16-le` pairs. `tests/test_assemble_artifacts.py`
  pins the parity over surrogates and control characters.
- The checkpoint-skeleton guards must stay in lockstep: `persistPlan` empties
  `phases.challenge.findings` only when it holds an array, and `assemble_artifacts.py` refills it
  under the identical predicate. A looser Python predicate fabricates an array.
- Structural failures (missing file, unparseable JSON, duplicate id, bad plan checksum) hard-fail.
  A *primary* content mismatch does not — it still derives from on-disk truth and raises a gap.
- Both writer paths stay live and are not dead code: the derived one is the automatic fallback when
  the primaries exceed `RETURN_CHAR_BUDGET`, the legacy by-value one is the safety net for
  pathological input. Do not delete either.

## The verify boundary

The executor echoes a per-id delta, never findings.

- `_DELTA_FIELDS` (Python) and `DELTA_VALUE_KEYS` (JS — `DELTA_KEYS` minus the structural
  `id`/`verified`) are one list in two runtimes, walked in the same order.
- `result.deltas` must stay the **first** key of `result` — the reading executor's `Read` is
  length-capped with no truncation notice, so what it echoes must be a prefix.
- The slice input is a projection, not a full finding copy: `VERIFY_SLICE_FIELDS` (JS) and
  `_SLICE_INPUT_FIELDS` (Python) are one list in two runtimes, walked in the same order. Every
  field the script consults on dispatched slices must be listed there — a lockstep test pins the
  JS/Python pair, and a read-site scan over `verify_findings.py`'s own source (both in
  `tests/test_verify_findings.py`) enforces the list against the script, exempting only its own
  writes (`_SCRIPT_WRITTEN_FIELDS`) and legacy-CLI-only reads (`_LEGACY_CLI_FIELDS`).

## Reading the shared context file

A `Read` returns only part of a large file and gives **no truncation notice**. `contextReadPlan`
computes the exact offsets from `contextLines`/`contextChars` stamped by the skill — this is
arithmetic, not an instruction to paginate. Stages receive a prebuilt sentence, never the context
path, so they cannot construct a read instruction of their own. Do not re-thread `contextPath` into
a stage input, and never replace that structural property with a phrase check.

`contextLines` counts as `cat -n` does, not `wc -l` (which reports one fewer without a trailing
newline). It is `1` for an empty file.
