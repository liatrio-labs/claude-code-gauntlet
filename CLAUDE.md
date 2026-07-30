# CLAUDE.md — claude-code-gauntlet

## Scripts

- **stdlib-only Python.** No pip dependencies. All scripts must use only the Python standard library.
- **Language-agnostic.** Scripts must not assume any particular programming language in the reviewed codebase. No `--include=*.py` or similar language filters — use `--exclude-dir` for non-source directories instead.
- **Repo root for searches.** `verify_findings.py` resolves the repo root at startup via `git rev-parse --show-toplevel`. Symbol searches use `git grep -l` with `cwd=REPO_ROOT` and a 3-second per-symbol timeout.
- **`scripts/collect_project_rules.py` resolves `@path` import pointers, not just filenames (issue #49).** `Read` does not expand Claude Code's `@import` directive, and real repos increasingly ship CLAUDE.md as a single pointer (`@AGENTS.md`) — a fixed allowlist still misses arbitrary targets (e.g. `@AI-AGENTS.md`). The script confines resolved paths via `realpath`, requires `.md`, bounds byte totals via `os.stat` before any `open`, and caps import depth with `MAX_IMPORT_DEPTH`.

## Prior-review signal

`scripts/review_marker.py` is the single source of truth for the prior-review marker/footer: it builds what `post_review.py` writes to a PR/MR review body and parses what `detect_prior_review.py` reads back on a rerun. Readers never branch on the payload's `version` field — it is informational/forensic only, since both the current `code-gauntlet-findings` token and the legacy `deep-review-findings` token carry `"version":"3.0"` despite being different wire shapes. Both token generations are recognized by every reader. `tests/test_review_marker.py::TestRoundTrip` is the guard that the write and read paths agree.

## Workflow runtime (JS)

The v3 review pipeline runs inside `workflows/pipeline.js`, invoked from SKILL.md via the `Workflow` tool (`scriptPath` + args). Rules:

- **node is the runtime (pinned v24.18.0).** All JS tests run with `node --test`. Use only Node built-ins and language globals available in the workflow runtime — no npm, no `package.json`, no `node_modules`. Stable `Array.prototype.sort` is relied upon.
- **Only JSON-safe language globals are guaranteed in the workflow runtime sandbox.** node/browser host globals that `node --test` provides but the sandbox does NOT — `structuredClone`, `Buffer`, `TextEncoder`/`TextDecoder`, `URL`, `setTimeout`/`queueMicrotask`, `process`, `console` — must not be used in `workflows/src`; a reference throws `X is not defined` on the first live dispatch while every test stays green (the `structuredClone` crash the live smoke run hit). Deep-clone with the JSON round-trip helper `deepClone` (findings are JSON-safe by construction), not `structuredClone`. Tests pin this: `pipeline_run.test.js` runs the pipeline with these globals deleted from `globalThis`.
- **`meta.name` (`code-gauntlet-pipeline`) must never equal the skill's name.** Both are registered by this plugin, so an identical name made `/code-gauntlet:code-gauntlet` resolve to the workflow, which then got a raw user string instead of the args waist and failed by construction. The skill invokes the bundle by `scriptPath`, so the name is free to differ.
- **`workflows/pipeline.js` is a generated, dependency-free bundle.** It contains no `import`/`require` — only workflow runtime globals (`agent`, `parallel`, `pipeline`, `phase`) and language builtins. Never edit it by hand.
- **Source lives in `workflows/src/*.js`.** These modules may use ESM `import`/`export` for test-time only. `workflows/build.js` (dependency-free node concatenator) strips those lines and concatenates `src/*.js` → `workflows/pipeline.js`. After changing any source module, rebuild (`node workflows/build.js`); `tests/test_bundle_fresh.py` enforces that the committed bundle is byte-identical to a fresh build and import-free.
- **No wall-clock, no `process.env` in workflow JS.** Timestamps arrive via the args waist (`generatedAt`, ISO8601, stamped by the skill); environment values (notably `CLAUDE_CODE_SUBAGENT_MODEL`) are read by the skill and passed in `policy.subagentModel`.
- **Five deterministic transforms have JS twins** (`mergeFindings`, `findingDedup`, `filterFindings`, `applyValidations`, `applyChallenges`), proven at parity against the authoritative Python via frozen golden fixtures. The Python scripts are still shipped (kept green) and are NOT deleted in this plan.

## Artifact persistence

> **The premise this section was built on is FALSE, and it is what sent the design off the rails.**
> It read: *"The sandbox has no disk, so every persisted byte must be emitted as some agent's
> tool-call argument at least once. The floor is one generation pass per unique byte — not zero."*
> True of the current design; not of the platform. **Measured 2026-07-30, zero-subagent probe:** a
> workflow's return value is serialized **by the harness** to `tasks/<taskid>.output` — byte-exact
> at 200 KB, 500 KB and **4,000,000 chars**, fnv1a32-verified against the value computed inside the
> sandbox, preserving even a lone surrogate left by slicing an emoji in half. No model touches it.
> `await_workflow.py` already resolves and parses that file and Phase 8 has Bash, so a **zero**-
> transcription path exists and always did. Findings + report top out at ~66 KB, 1.6% of the
> proven-safe size. One run whose writer failed and persisted nothing was found with its whole
> output sitting in that file, recoverable all along.
>
> What follows describes the CURRENT writer-based design, which is being replaced. Read it as a
> description of the code, not a justification for it. Do not cite the deleted premise.

- **The writer is a language model, and it does not transcribe. Census over every recorded run
  (2026-07-30, 38 writer journals / 84 artifacts): 26 of 73 attempted writes — 36% — failed their
  own contract, plus 12 artifacts never written at all.** Corruption is pervasive, not a slip:
  median 8 divergence sites per bad document, max 46, only 2 of 10 single-site. **Nothing predicts
  it.** Not size (a 47 KB findings.json with 104 backslashes came through byte-perfect; a 6.4 KB
  zero-backslash `.md` was truncated), not escape density, not the two combined — so there is no
  safe-size rule to design to. And **no encoding tweak is worth trying**: 18 of 18 backslash-run
  sites collapsed, then after `hardenEscapeRuns` cleared every run from the wire, **0 of 28**
  `\u005c` escapes survived, rewritten back to `\\`. Both spellings fail, in opposite directions.
- **The worst failures are SILENT SUMMARIZATION, which no encoding or format can prevent.** Same
  census: one checkpoint lost 29,132 chars because the writer dropped 11 fields from *every*
  finding; another lost 13,008 chars with its schema fully intact, the writer having simply
  rewritten long prose shorter (`challenge_justification` 2,109 chars -> 75; every
  `validation_justification` ~200-350 -> 10). A third run expanded an ellipsis inside a finding's
  quoted evidence by opening the file the quote referenced. All of these parse cleanly and are
  invisible without a content proof — the one mechanism that has actually been catching this.
  `hardenEscapeRuns` ships because it is harmless and its cross-runtime claim is guarded, but
  **it is not the fix**.
- **Re-dispatching does not repair corruption.** The retry's stated rationale — *"the writer is a
  sampled agent, not a function, so a second dispatch is a fresh sample"* — holds only for a
  stochastic fault. Twice, on two different documents, the retry corrupted the same sites and failed
  at the **identical byte offset**. It is kept for genuinely flaky writer failures; it is not a
  mitigation for mangling.
- **`fnv1a32` is defined over UTF-16 code units and MUST agree between runtimes.** JS uses
  `charCodeAt` + `Math.imul` (language builtins — `TextEncoder`/`Buffer` are unavailable); Python
  reads `s.encode('utf-16-le')` as 2-byte LE units. `chars` is the code-unit count on both sides.
  `tests/test_assemble_artifacts.py` pins the parity over surrogates, astral pairs, U+2028/U+2029
  and control characters — that test is the only thing between a serializer tweak and a silently
  divergent artifact.
- **The two runtimes' checkpoint-skeleton guards must stay in lockstep.** `persistPlan` empties
  `phases.challenge.findings` only when it holds an ARRAY; `assemble_artifacts.py` refills it under
  the identical predicate. A looser Python predicate fabricates an array the pipeline never had.
- **Structural failures hard-fail; PRIMARY content-proof mismatches do not.** A missing file,
  unparseable JSON, a missing/duplicate id or a bad **plan** checksum writes nothing and returns
  `ok:false`. A *primary* mismatch still derives from on-disk truth and emits a loud gap — refusing
  there would invent a new way to lose a run. A **derived** mismatch is structural (the derivation
  itself is wrong and there is no second copy) *unless* `findings.json` also came back `mismatch`,
  which makes the derived difference its expected consequence. Full rationale at
  `trustAssembleReceipt`; it is here because the policy spans both runtimes.
- **A refused receipt keeps the primaries it PROVED** (`provenPrimaryPaths`), which is what makes
  SKILL.md's Phase 8 "deliver whatever `artifactPaths.report` exists" branch reachable — it was dead
  from the day it was written, and a run whose report was byte-perfect still reached the user as
  nothing. Grading is `trustAssembleReceipt`'s, unrelaxed; derived documents are never salvaged.
- **The legacy by-value path stays live** and is taken when `args.persist` is absent or finding ids
  are missing/duplicated. It is the safety net for pathological input; do not delete it.
- **The report is unwrapped at BOTH sites, deliberately.** The report-writer intermittently returns
  its markdown wrapped as `{"report": "..."}` (~15 of 25 dated runs since 2026-07-22 — flaky, not a
  regression) and the writer persists the wrapper verbatim, as its contract requires.
  `dispatchReportSegment` unwraps it and Phase 8 unwraps again at delivery: belt-and-braces, neither
  one dead.

## Verify boundary (delta echo)

The Verify executor echoes a per-id **delta** of what `verify_findings.py` decided — never findings — and `joinVerifyDeltas` rebuilds each slice from the findings the stage itself dispatched. The rationale and threat model live at the sites (`VERIFY_SCHEMA`/`trustSlice` in `stages.js`, the `_DELTA_FIELDS` audit block in `verify_findings.py`, `references/validation-pipeline.md`). What no single site can hold, because each rule spans two files:

- **`_DELTA_FIELDS` (Python) and `DELTA_KEYS` (JS) are one list in two runtimes.** Both sides walk it in the same order to build the checksummed canonical form, so a field added to one and not the other either silently drops out of the content proof or makes every honest receipt fail it.
- **`result.deltas` must stay the FIRST key of `result`.** The executor's `Read` of that file is length-capped and gives no truncation notice (see "Reading the shared context file"), so what it echoes has to be a PREFIX of the document — ahead of the full `verified`/`eliminated` arrays the same file still carries, unchanged, for bench/v2 consumers.
- **`agent` is deleted at the join, and re-lands only with #22.** It used to be withheld by a schema omission that worked only stochastically (2 of 6 PRs measured surviving); joining a delta onto findings the stage already holds would have made survival deterministic, which is the measured dedup recall-collapse mechanism (eliminations 7 -> 33, same-6 recall 20/30 -> 13/30).
- **The checksum reuses `assemble_artifacts.py`'s `fnv1a32`/`js_stringify_pretty`** instead of growing a third copy, so `tests/test_assemble_artifacts.py` is the cross-runtime parity guard for this boundary as well as the persist one.

## Findings schema

All pipeline stages use the **canonical agent schema**. These field names are non-negotiable:

- `description` (not `body`)
- `line_start` / `line_end` (not `line`)
- `origin` (not `blame_tag`)
- `dimension` — short name from agent output: `"bug"`, `"security"`, `"cross_file_impact"`, `"test_coverage"`, `"convention"`, `"intent"`, `"comment_accuracy"`, `"type_design"`, `"simplification"`. Never the agent name.
- `agent` — injected by the orchestrator during merge: `"bug-detector"`, `"security-reviewer"`, etc. Agents do not emit this field themselves.
- `cross_file_refs` — preserve from agent output. Used by `verify_findings.py` for automatic "surfaced" classification.
- **Canonical fields** — every dispatch schema declares exactly these: `id`, `file`, `line_start`, `line_end`, `title`, `description`, `severity`, `confidence`, `dimension`, `origin`, `evidence`, `suggestion`, `claude_md_rule`, `cross_file_refs`.
- **Per-dimension extras** — one entry on the owning registry row: `hidden_errors` (bug), `attack_vector` (security), `affected_consumers` (cross_file_impact), `criticality` + `failure_scenario` (test_coverage), `spec_text` (intent), `invalid_state_example` (type_design), `behavior_preserved` (simplification).

A field no schema declares is DROPPED, not passed through: StructuredOutput returns only declared properties, so a field an agent contract instructs but registry.js does not declare never reaches merge — silently, on every run. That is issue #47: `suggestion` and `claude_md_rule` (all 7 contracts), `spec_text` (intent), `criticality` and `failure_scenario` (test_coverage) were instructed for years and declared by nothing. Adding a finding field is therefore ONE entry in registry.js plus the owning agent's `.md` output block; `tests/test_dimensions_registry.py` fails the build when those two drift. Declared-but-not-required fields are NOT nullable — a not-applicable value is OMITTED, never null, because the platform types each property to a single type and a null burns StructuredOutput retries. `required` is one flat list shared by every dimension's dispatch schema AND by the verify echo, so a field a contract calls required for its own dimension (`claude_md_rule` for convention, `spec_text` for intent, `criticality`/`failure_scenario` for test_coverage) is contract-enforced, not schema-enforced. Do not fake it by appending a single-dimension field to `FINDING_REQUIRED`.

The canonical schema is defined once in `workflows/src/registry.js` — `FINDING_PROP_TYPES` (every declared property), `FINDING_REQUIRED` (the flat required subset), and each `DIMENSIONS[].schemaExtra` (per-dimension additions; the nine dimension names above must stay in lockstep with the bullet list; `tests/test_dimensions_registry.py` asserts it) — and consumed by the JS stages. `FINDING_PROP_TYPES` and `FINDING_REQUIRED` were moved into registry.js from `stages.js` by this change, precisely because the split is what let the drift happen. **Persist boundary:** the artifact-writer persists findings under a **union schema** — the v2 aliases `line`/`end_line`/`body` are added alongside the canonical `line_start`/`line_end`/`description` so the retained `post_review.py` (which indexes the v2 names) and `verify_findings.py` (canonical names) both consume the same file unchanged. See `workflows/src/stages.js` `toV2Aliased`/`writerPayload` and `tests/test_boundary_parity.py`.

## Agents

- **Frontmatter is system-enforced.** `tools`, `effort`, `model`, `color` in agent YAML frontmatter are not advisory — Claude Code enforces them.
- **LSP-first investigation.** Agents prefer LSP (`goToDefinition`, `findReferences`, `hover`) with Grep fallback. This is documented in each agent's "How to investigate" section.
- **False-positive exclusion list is intentionally duplicated** across all 7 discovery agents. Do not refactor into a shared read — we want the guarantee that every agent has the list even if a file read fails. Each copy has a `<!-- Canonical source: references/false-positive-exclusions.md -->` comment pointing to the source of truth.
- **Complete-read contract, duplicated across all 10 file-reading agents** (7 discovery + validator + challenger + change-summarizer). Canonical source `references/complete-read-contract.md`; same duplication rationale as the exclusion list. `tests/test_agent_contracts.py::TestCompleteReadContract` asserts every copy is byte-identical.

## Reading the shared context file

A `Read` returns only PART of a large file and emits **no truncation notice** — a partial result is indistinguishable from a complete one. Measured on run `wf_cef39739-577` (issue #48): all 7 discovery agents' first `Read` of the 95,057-byte / 2,028-line context file returned 58,145 chars ending at line 1083. Six inferred the cutoff and paginated on; `security-reviewer` did not, and reviewed roughly half the diff while returning `complete: true`.

**The fix is arithmetic, not a prompt instruction.** Do not "tell the agent to paginate until done" — that puts a judgment call where a computation belongs, and it is the judgment that failed.

- **Phase 2 measures, the workflow computes.** The skill stamps `contextLines` / `contextChars` (measured in the same `python3 -c` that writes the file) onto the args waist; the workflow has no disk and cannot measure it. `contextReadPlan(lines, chars)` in `workflows/src/stages.js` is pure arithmetic returning the exact `[{offset, limit}]` covering the file, chunked under **both** observed platform caps — the documented 2,000-line Read cap and the ~58,000-char return the profiled run hit. Neither cap is a contract, so the planner stays well under each (`READ_PLAN_MAX_LINES` 750, `READ_PLAN_MAX_CHARS` 30000).
- **Stages never receive the context path — only the prebuilt sentence.** `runWith` calls `sharedContextLine` exactly once and threads the resulting STRING as `contextLine`. Summarize/Discover/Validate therefore *cannot* build a context-read instruction of their own: the capability is removed, not policed. This replaced a guard that counted the literal `Read the shared context at` in `stages.js` — an adversarial review defeated it in one edit by rewording to `Open the shared context at …`, whole suite green. **Do not replace a structural property with a phrase count**, and do not re-thread `contextPath` into a stage input.
- **The guards that remain are behavioral and keyed on the path, not on wording.** They drive `runWith` and assert that every dispatched prompt *containing the context path* also carries the read plan (or the count-free fallback). `runWith` itself still holds the path — it must, to build the line — so those behavioral guards, not the source-level ones, are what cover it.
- **An unmeasured run DISCLOSES the degradation.** Absent `contextLines` is legal (hard-failing would trade a partial read for a dead run), but it drops back to fallback wording, so `runWith` emits a `context_unmeasured` gap. Degraded-but-disclosed, the same contract `args.js` states for a tolerated null: silence here would let a Phase 2 regression revert the whole fix with nothing anywhere saying so.
- **Even the fallback steps deterministically.** With no measurement the terminus is unknowable, so end-detection is unavoidable — but the stepping is spelled out (`Read(offset=1, limit=750)`, `Read(offset=751, limit=750)`, …), leaving only "did that call return anything" rather than "have I read enough yet".
- **Bound the plan, not just the input.** `contextReadPlan` refuses (returns `[]`, degrading to read-to-end) above `READ_PLAN_MAX_CHUNKS` 2000, checked *before* the first allocation; the waist independently caps `contextLines` at 5,000,000 and `contextChars` at 500,000,000. Unbounded, `contextLines: Number.MAX_SAFE_INTEGER` OOM-killed the node process — a V8 fatal error, so `runWith`'s top-level catch never ran, nothing dispatched, and no gap was recorded.
- **`contextLines` is `1` for an empty file, not `0`** (`"".count("\n") + 1`). Phase 2 must test `not content`, never `lines == 0`, or it stamps `{contextLines: 1, contextChars: 0}` and tells every agent the shared context is one line long.
- **Both waist fields are optional — for the live risk, not for old callers.** `SKILL.md` is the only producer of the args waist (bench invokes the skill; it does not assemble args), and it ships in the same plugin as the bundle, so there is no real version-skew window. Optionality exists because Phase 2 is **model-executed** and can skip the measurement step in a live run — and hard-failing there would trade a partial read for a dead review. That is why it degrades-and-discloses rather than rejects. `contextChars` without `contextLines` is rejected — chars alone cannot size a line-offset plan — as is any non-positive or fractional value, since a corrupt measurement yields a plan that misses the file's tail. `contextLines` is deliberately **not** on `NULLABLE_TOP_LEVEL`: a null measurement is a producer bug, not an omission.
- **The challenger is never given the context path.** It is structurally blind by design (title, description, location, and the code it opens itself). The Challenge stage input carried a dead `contextPath` until #48's pairing guard surfaced it; do not re-add it.
- **`contextLines` counts as `cat -n` numbers, not as `wc -l` counts.** `wc -l` counts newline terminators, so it reports one fewer for a file with no trailing newline — exactly the profiled file's shape. An undercount by one silently drops the file's last line from every agent's read plan.

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

Use the `python3 -c "import json; ..."` pattern to write JSON to disk for scripts. Never use the Write tool (requires prior Read on target) or Bash heredocs (zsh corrupts `!` as `\!`)..
