# CLAUDE.md — claude-code-gauntlet

## Scripts

- **stdlib-only Python.** No pip dependencies. All scripts must use only the Python standard library.
- **Language-agnostic.** Scripts must not assume any particular programming language in the reviewed codebase. No `--include=*.py` or similar language filters — use `--exclude-dir` for non-source directories instead.
- **Repo root for searches.** `verify_findings.py` resolves the repo root at startup via `git rev-parse --show-toplevel`. Symbol searches use `git grep -l` with `cwd=REPO_ROOT` and a 3-second per-symbol timeout.

## Workflow runtime (JS)

The v3 review pipeline runs inside `workflows/pipeline.js`, invoked from SKILL.md via the `Workflow` tool (`scriptPath` + args). Rules:

- **node is the runtime (pinned v24.18.0).** All JS tests run with `node --test`. Use only Node built-ins and language globals available in the workflow runtime — no npm, no `package.json`, no `node_modules`. Stable `Array.prototype.sort` is relied upon.
- **Only JSON-safe language globals are guaranteed in the workflow runtime sandbox.** node/browser host globals that `node --test` provides but the sandbox does NOT — `structuredClone`, `Buffer`, `TextEncoder`/`TextDecoder`, `URL`, `setTimeout`/`queueMicrotask`, `process`, `console` — must not be used in `workflows/src`; a reference throws `X is not defined` on the first live dispatch while every test stays green (the `structuredClone` crash the live smoke run hit). Deep-clone with the JSON round-trip helper `deepClone` (findings are JSON-safe by construction), not `structuredClone`. Tests pin this: `pipeline_run.test.js` runs the pipeline with these globals deleted from `globalThis`.
- **`workflows/pipeline.js` is a generated, dependency-free bundle.** It contains no `import`/`require` — only workflow runtime globals (`agent`, `parallel`, `pipeline`, `phase`) and language builtins. Never edit it by hand.
- **Source lives in `workflows/src/*.js`.** These modules may use ESM `import`/`export` for test-time only. `workflows/build.js` (dependency-free node concatenator) strips those lines and concatenates `src/*.js` → `workflows/pipeline.js`. After changing any source module, rebuild (`node workflows/build.js`); `tests/test_bundle_fresh.py` enforces that the committed bundle is byte-identical to a fresh build and import-free.
- **No wall-clock, no `process.env` in workflow JS.** Timestamps arrive via the args waist (`generatedAt`, ISO8601, stamped by the skill); environment values (notably `CLAUDE_CODE_SUBAGENT_MODEL`) are read by the skill and passed in `policy.subagentModel`.
- **Five deterministic transforms have JS twins** (`mergeFindings`, `findingDedup`, `filterFindings`, `applyValidations`, `applyChallenges`), proven at parity against the authoritative Python via frozen golden fixtures. The Python scripts are still shipped (kept green) and are NOT deleted in this plan.

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

- **Python:** pytest with `unittest.TestCase` style. Run: `python -m pytest tests/ -q`. The suite covers every retained pipeline script (`verify_findings.py`, `filter_findings.py`, `post_review.py`, `merge_findings.py`, `finding_dedup.py`, `apply_validations.py`, `apply_challenges.py`, `validate_ndjson.py`) plus the JS/Python boundary: `tests/test_parity_fixtures.py` (Python twin == golden), `tests/test_bundle_fresh.py` (committed bundle == fresh build, import-free), `tests/test_boundary_parity.py`, `tests/test_dimensions_registry.py` (CLAUDE.md dimensions ⇄ registry).
- **JS:** `node --test workflows/test/` (unit + orchestration-contract + parity). Dual-runtime golden fixtures live in `tests/fixtures/parity/<script>/<case>/{input,expected}.json`; the recorder is `workflows/test/tools/record_parity.py`.

## Output directory convention

- `{output_dir}` in SKILL.md and references defaults to `.code-gauntlet/` (repo-local, gitignored). Override with `$CODE_GAUNTLET_OUTPUT_DIR` for CI or custom environments.
- **File-based context handoff.** Shared context (diff, rules, summary, risk) is written to `{output_dir}/code-gauntlet-context-{head_sha_short}.md` during Phase 2. Agent dispatch prompts contain only the context file path and findings file path (~100 tokens each), ensuring all 7 fit in a single response. Agents Read the context file at startup.
- **AST-safe emission.** Agents use `printf '%s\n' '...' >> "literal_path"` (not `echo` — zsh's builtin `echo` interprets `\n` as newlines even in single quotes, breaking NDJSON). For apostrophes in JSON values, use `\u0027` (valid JSON Unicode escape). Avoid `$'...'` ANSI-C quoting, `$VAR`, heredocs, `python3 -c`, and command substitution — the tree-sitter-bash AST parser treats these as unrecognized nodes and they get silently denied in subagent sessions running with sandbox auto-approval.
- **NDJSON one-line contract.** Every JSON object an agent emits must be a single physical line. Literal newlines, tabs, and carriage returns inside JSON string values must be written as the two-character escapes `\n`, `\t`, `\r` — a raw byte 0x0A inside a string splits one finding into two corrupt physical lines. The `description` field is constrained to single-paragraph prose (≤500 chars, no fenced code blocks, no multi-line snippets, no bullet lists); code references go in `evidence` and `cross_file_refs`. Canonical contract: `references/ndjson-emission-contract.md`. The contract is duplicated verbatim into each of the 7 discovery agent files (same rationale as the false-positive exclusion list).
- **Final-step NDJSON validation.** Phase 3 agents run `python3 "{plugin_root}/scripts/validate_ndjson.py" "<findings_file>"` as their last action. The validator path is written into the context file's `## Validator` section by Phase 2. A standalone script invocation is AST-safe (three plain word tokens) where `python3 -c "..."` is not. Non-zero exit means the agent must re-emit any flagged findings before returning.
- The head SHA (`head_sha_short`) is resolved in Phase 2 after PR checkout — not in Phase 1 — so filenames reflect the actual PR HEAD.

## Writing pipeline JSON

Use the `python3 -c "import json; ..."` pattern to write JSON to disk for scripts. Never use the Write tool (requires prior Read on target) or Bash heredocs (zsh corrupts `!` as `\!`).
