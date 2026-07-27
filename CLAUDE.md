# CLAUDE.md — claude-code-gauntlet

## Scripts

- **stdlib-only Python.** No pip dependencies. All scripts must use only the Python standard library.
- **Language-agnostic.** Scripts must not assume any particular programming language in the reviewed codebase. No `--include=*.py` or similar language filters — use `--exclude-dir` for non-source directories instead.
- **Repo root for searches.** `verify_findings.py` resolves the repo root at startup via `git rev-parse --show-toplevel`. Symbol searches use `git grep -l` with `cwd=REPO_ROOT` and a 3-second per-symbol timeout.

## Prior-review signal

`scripts/review_marker.py` is the single source of truth for the prior-review marker/footer: it builds what `post_review.py` writes to a PR/MR review body and parses what `detect_prior_review.py` reads back on a rerun. Readers never branch on the payload's `version` field — it is informational/forensic only, since both the current `code-gauntlet-findings` token and the legacy `deep-review-findings` token carry `"version":"3.0"` despite being different wire shapes. Both token generations are recognized by every reader. `tests/test_review_marker.py::TestRoundTrip` is the guard that the write and read paths agree.

## Workflow runtime (JS)

The v3 review pipeline runs inside `workflows/pipeline.js`, invoked from SKILL.md via the `Workflow` tool (`scriptPath` + args). Rules:

- **node is the runtime (pinned v24.18.0).** All JS tests run with `node --test`. Use only Node built-ins and language globals available in the workflow runtime — no npm, no `package.json`, no `node_modules`. Stable `Array.prototype.sort` is relied upon.
- **Only JSON-safe language globals are guaranteed in the workflow runtime sandbox.** node/browser host globals that `node --test` provides but the sandbox does NOT — `structuredClone`, `Buffer`, `TextEncoder`/`TextDecoder`, `URL`, `setTimeout`/`queueMicrotask`, `process`, `console` — must not be used in `workflows/src`; a reference throws `X is not defined` on the first live dispatch while every test stays green (the `structuredClone` crash the live smoke run hit). Deep-clone with the JSON round-trip helper `deepClone` (findings are JSON-safe by construction), not `structuredClone`. Tests pin this: `pipeline_run.test.js` runs the pipeline with these globals deleted from `globalThis`.
- **`workflows/pipeline.js` is a generated, dependency-free bundle.** It contains no `import`/`require` — only workflow runtime globals (`agent`, `parallel`, `pipeline`, `phase`) and language builtins. Never edit it by hand.
- **Source lives in `workflows/src/*.js`.** These modules may use ESM `import`/`export` for test-time only. `workflows/build.js` (dependency-free node concatenator) strips those lines and concatenates `src/*.js` → `workflows/pipeline.js`. After changing any source module, rebuild (`node workflows/build.js`); `tests/test_bundle_fresh.py` enforces that the committed bundle is byte-identical to a fresh build and import-free.
- **No wall-clock, no `process.env` in workflow JS.** Timestamps arrive via the args waist (`generatedAt`, ISO8601, stamped by the skill); environment values (notably `CLAUDE_CODE_SUBAGENT_MODEL`) are read by the skill and passed in `policy.subagentModel`.
- **Five deterministic transforms have JS twins** (`mergeFindings`, `findingDedup`, `filterFindings`, `applyValidations`, `applyChallenges`), proven at parity against the authoritative Python via frozen golden fixtures. The Python scripts are still shipped (kept green) and are NOT deleted in this plan.

## Artifact persistence

The sandbox has no disk, so every persisted byte must be emitted as some agent's tool-call argument at least once. The floor is **one generation pass per unique byte** — not zero. `writeArtifacts` therefore has the artifact-writer emit only *unique* content (findings JSON, report markdown, a persist-plan JSON) and a pinned executor derive the rest:

- **Derived, never re-typed.** `post-review.json` is a ranked/capped id projection of `findings.json`; the checkpoint's `phases.challenge.findings` is its alias-stripped twin. `scripts/assemble_artifacts.py --plan <path>` builds both. Re-adding a derivable array to the writer payload re-introduces the cost this design exists to remove (measured: ~66% of the writer's 88 KB was re-transcription of one 21.4 KB array).
- **`fnv1a32` is defined over UTF-16 code units and MUST agree between runtimes.** JS uses `charCodeAt` + `Math.imul` (language builtins — `TextEncoder`/`Buffer` are unavailable); Python reads `s.encode('utf-16-le')` as 2-byte LE units. `chars` is the code-unit count on both sides. `tests/test_assemble_artifacts.py` pins the parity over surrogates, astral pairs, U+2028/U+2029 and control characters — that test is the only thing standing between a serializer tweak and a silently divergent artifact.
- **Structural failures hard-fail; PRIMARY content-proof mismatches do not.** A missing file, unparseable JSON, a missing/duplicate id, or a bad **plan** checksum writes nothing and returns `ok:false` (the plan is the instruction set, not data). A mismatch on a *primary's* checksum still derives from on-disk truth and emits a loud gap — refusing there would invent a new way to lose a run, against the never-fabricate contract. A mismatch on a **derived** document is the opposite call: the derivation itself is wrong and there is no second copy, so `trustAssembleReceipt` degrades to partial-artifacts — *unless* `findings.json` (the sole source of both projections) itself came back `mismatch`, in which case the derived difference is the expected consequence of the tolerated one and is reported as a gap instead.
- **One retry on a structural assemble refusal, then degrade — and NEVER a legacy fallback.** The writer is a sampled agent, not a function, so a second dispatch is a fresh sample: `writeArtifactsDerived` re-runs the whole derived persist (writer + assembler) **exactly once** when the script refuses (unparseable JSON, missing/duplicate id, bad plan checksum, derived-document mismatch), then degrades. A *tolerated* primary content-proof mismatch is not retried — that is a successful persist with a disclosed divergence. A writer throw/null/failed write-proof is not retried either — nothing reached the script to refuse. Falling back to the legacy by-value writer here was considered and **rejected**: it carries no content proof, so it would convert a visible failure into a silent one. Both attempts are named in the gaps, whichever way the retry lands.
- **The by-value writer is not trustworthy — that is what the proof is for.** First measured with a content proof on the 2026-07-27 smoke (3 PRs): the artifact-writer's transcription of `findings.json` diverged from the payload it was handed on **3 of 3** runs — 16 chars, 8 chars, and one document broken outright by `\"` over-escaped to `\\"`. Re-serializing the on-disk JSON through the same pretty printer reproduced the on-disk bytes exactly, so the drift is in the DATA, not the serializer. This is long-standing; only the detection is new.
- **Both derived documents carry a content proof, not just a path.** The plan's `derive` block holds `{path, chars, checksum}` for post-review.json and checkpoint-all.json, computed in JS from `writerPayload(inp).postReview`/`.checkpoints` through the same `normalizeForChecksum` + `fnv1a32` as the primaries; the script reports its own numbers in `written[]` and `trustAssembleReceipt` compares. Like `trustSlice`, this is a consistency/liveness check against a stale or confused executor and against serializer divergence — **not authentication**: the plan is on disk, so any executor can read the values it names.
- **The two runtimes' checkpoint-skeleton guards must stay in lockstep.** `persistPlan` empties `phases.challenge.findings` only when it holds an ARRAY; `assemble_artifacts.py` refills it under the identical predicate. A looser Python predicate fabricates an array the pipeline never had.
- **`writeArtifacts` never throws.** Its try/catch covers the WHOLE body — the plan/primary computation runs before any dispatch and a throw there degrades to partial-artifacts like any writer failure (SKILL.md Error Recovery: writer failure is non-fatal). `assemble_artifacts.py`'s `main()` likewise always emits one receipt line, falling back to a hand-built minimal one if the receipt will not serialize: an empty stdout is indistinguishable from a dead executor.
- **Numbers must be JS-reproducible.** Both runtimes refuse non-integer or out-of-safe-range numeric values rather than write an artifact whose float spelling differs between languages; the JS side falls back to the legacy by-value writer instead of degrading.
- **The legacy by-value path stays live** and is taken when `args.persist` is absent or when finding ids are missing/duplicated. Do not delete it — it is the safety net for pathological input.
- **The report is unwrapped where the string is first received.** The report-writer intermittently returns its markdown already wrapped as `{"report": "# Code Gauntlet Report..."}` (~15 of 25 dated runs since 2026-07-22 — flaky, not a regression), and the writer persists that wrapper verbatim, as its contract requires. `dispatchReportSegment` unwraps it, so the single-dispatch and segmented paths are covered by one rule and the *persisted* artifact is markdown. Conservative by construction: a successful `JSON.parse` **and** a plain object **and** a string `report` member with no other meaningful content, else the string is returned untouched. Phase 8 also unwraps at delivery — belt-and-braces, not one of them dead.

## Findings schema

All pipeline stages use the **canonical agent schema**. These field names are non-negotiable:

- `description` (not `body`)
- `line_start` / `line_end` (not `line`)
- `origin` (not `blame_tag`)
- `dimension` — short name from agent output: `"bug"`, `"security"`, `"cross_file_impact"`, `"test_coverage"`, `"convention"`, `"intent"`, `"comment_accuracy"`, `"type_design"`, `"simplification"`. Never the agent name.
- `agent` — injected by the orchestrator during merge: `"bug-detector"`, `"security-reviewer"`, etc. Agents do not emit this field themselves.
- `cross_file_refs` — preserve from agent output. Used by `verify_findings.py` for automatic "surfaced" classification.

The canonical schema is defined once in `workflows/src/registry.js` (`DIMENSIONS` — the nine dimension names above must stay in lockstep with the bullet list; `tests/test_dimensions_registry.py` asserts it) and consumed by the JS stages. **Persist boundary:** the artifact-writer persists findings under a **union schema** — the v2 aliases `line`/`end_line`/`body` are added alongside the canonical `line_start`/`line_end`/`description` so the retained `post_review.py` (which indexes the v2 names) and `verify_findings.py` (canonical names) both consume the same file unchanged. See `workflows/src/stages.js` `toV2Aliased`/`writerPayload` and `tests/test_boundary_parity.py`.

## Agents

- **Frontmatter is system-enforced.** `tools`, `effort`, `model`, `color` in agent YAML frontmatter are not advisory — Claude Code enforces them.
- **LSP-first investigation.** Agents prefer LSP (`goToDefinition`, `findReferences`, `hover`) with Grep fallback. This is documented in each agent's "How to investigate" section.
- **False-positive exclusion list is intentionally duplicated** across all 7 discovery agents. Do not refactor into a shared read — we want the guarantee that every agent has the list even if a file read fails. Each copy has a `<!-- Canonical source: references/false-positive-exclusions.md -->` comment pointing to the source of truth.

## Plugin structure

Scripts and agents live at the plugin root, not under `skills/code-gauntlet/`:

```
claude-code-gauntlet/          <- plugin root ({plugin_root})
├── agents/
├── scripts/                  <- retained Python (verify_findings.py, post_review.py, ...)
├── workflows/                <- JS pipeline: src/*.js, build.js, pipeline.js (bundle), test/
├── bench/                    <- benchmark harness (stdlib-exempt; not touched by the v3 build)
├── tests/                    <- pytest suite (Python side, incl. parity + bundle-freshness)
└── skills/
    └── code-gauntlet/          <- skill base directory
```

SKILL.md derives `{plugin_root}` as two levels above the skill base directory. Script invocations use `{plugin_root}/scripts/`; the workflow entry is `{plugin_root}/workflows/pipeline.js`.

## Tests

- **Python:** pytest with `unittest.TestCase` style. Run: `python -m pytest tests/ -q`. The suite covers every retained pipeline script (`verify_findings.py`, `filter_findings.py`, `post_review.py`, `merge_findings.py`, `finding_dedup.py`, `apply_validations.py`, `apply_challenges.py`, `validate_ndjson.py`) plus the JS/Python boundary: `tests/test_parity_fixtures.py` (Python twin == golden), `tests/test_bundle_fresh.py` (committed bundle == fresh build, import-free), `tests/test_boundary_parity.py`, `tests/test_dimensions_registry.py` (CLAUDE.md dimensions ⇄ registry), `tests/test_assemble_artifacts.py` (derived-persistence round trip + the cross-runtime `JSON.stringify`/`fnv1a32` parity that shells out to `node`).
- **Latency:** `bench/profile_run.py` reconstructs a per-stage/per-agent profile from a recorded workflow run (`bench/PROFILING.md`). It is the measurement method behind any wall-clock claim — re-run it rather than asserting a speedup.
- **JS:** `node --test workflows/test/*.test.js` (unit + orchestration-contract + parity; the bare directory form is not a valid `node --test` target on node 24). Dual-runtime golden fixtures live in `tests/fixtures/parity/<script>/<case>/{input,expected}.json`; the recorder is `workflows/test/tools/record_parity.py`.

## Output directory convention

- `{output_dir}` in SKILL.md and references defaults to `.code-gauntlet/` (repo-local, gitignored). Override with `$CODE_GAUNTLET_OUTPUT_DIR` for CI or custom environments.
- **File-based context handoff.** Shared context (diff, rules, summary, risk) is written to `{output_dir}/code-gauntlet-context-{head_sha_short}.md` during Phase 2. Agent dispatch prompts contain only the context file path and findings file path (~100 tokens each), ensuring all 7 fit in a single response. Agents Read the context file at startup.
- **AST-safe emission.** Agents use `printf '%s\n' '...' >> "literal_path"` (not `echo` — zsh's builtin `echo` interprets `\n` as newlines even in single quotes, breaking NDJSON). For apostrophes in JSON values, use `\u0027` (valid JSON Unicode escape). Avoid `$'...'` ANSI-C quoting, `$VAR`, heredocs, `python3 -c`, and command substitution — the tree-sitter-bash AST parser treats these as unrecognized nodes and they get silently denied in subagent sessions running with sandbox auto-approval.
- **NDJSON one-line contract.** Every JSON object an agent emits must be a single physical line. Literal newlines, tabs, and carriage returns inside JSON string values must be written as the two-character escapes `\n`, `\t`, `\r` — a raw byte 0x0A inside a string splits one finding into two corrupt physical lines. The `description` field is constrained to single-paragraph prose (≤500 chars, no fenced code blocks, no multi-line snippets, no bullet lists); code references go in `evidence` and `cross_file_refs`. Canonical contract: `references/ndjson-emission-contract.md`. The contract is duplicated verbatim into each of the 7 discovery agent files (same rationale as the false-positive exclusion list).
- **Final-step NDJSON validation.** Phase 3 agents run `python3 "{plugin_root}/scripts/validate_ndjson.py" "<findings_file>"` as their last action. The validator path is written into the context file's `## Validator` section by Phase 2. A standalone script invocation is AST-safe (three plain word tokens) where `python3 -c "..."` is not. Non-zero exit means the agent must re-emit any flagged findings before returning.
- The head SHA (`head_sha_short`) is resolved in Phase 2 after PR checkout — not in Phase 1 — so filenames reflect the actual PR HEAD.

## Writing pipeline JSON

Use the `python3 -c "import json; ..."` pattern to write JSON to disk for scripts. Never use the Write tool (requires prior Read on target) or Bash heredocs (zsh corrupts `!` as `\!`).
