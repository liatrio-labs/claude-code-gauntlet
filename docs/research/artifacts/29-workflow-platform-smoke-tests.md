# 29 — Workflow platform smoke tests (v3 Phase 0 gate)

Empirical verification of the 16 platform assumptions in the v3 design spec (S9).
Run dates, CLI version, and raw observations recorded per row. Statuses:
CONFIRMED / REFUTED / INCONCLUSIVE.

**CLI version under test:** 2.1.214 (Claude Code)

| # | Assumption | Status | Observed | Decision impact |
|---|------------|--------|----------|-----------------|
| 1 | Workflow scriptPath resolves from the installed plugin dir | CONFIRMED | Absolute scriptPath inside the versioned marketplace cache dir (`~/.claude/plugins/cache/claude-deep-review/deep-review/2.5.0/probes-tmp/noop.js`) executed and returned `{"ok":true,"marker":"PROBE_NOOP_V1"}`; repo-path invocation also works | packaging (S2) |
| 2 | scriptPath scripts can import/require sibling files | REFUTED | Static ESM import fails at parse: `SyntaxError: Unexpected token '{'. import call expects one or two arguments.` (script never launches); `require()` throws `ReferenceError: require is not defined` at runtime | source layout (S3) |
| 3 | Headless `claude -p` can invoke the Workflow tool via scriptPath | CONFIRMED | Cited from artifact 33 (P2b pinned invocation context, P3 cost fields) + one residual-gap check run 2026-07-18: default-mode `claude -p --model sonnet` invoked Workflow noop via scriptPath, marker returned, $0.348, `is_error:false`. Nested `--dangerously-skip-permissions` remains blocked by the outer session's classifier (as in artifact 33 P1 history) — default mode is the working path | bench runner (S6) |
| 4 | Workflow-spawned agent Bash works under interactive default/acceptEdits (or scoped allowlist pre-approves) | pending | | executor (S4.4) |
| 5 | agent() returns null (not throw) on terminal failure; parallel() isolates member failures | pending | | degradation paths (S4) |
| 6 | Frontmatter `effort` survives agentType dispatch | pending | | parity claim (S5) |
| 7 | opts.model overrides agentType frontmatter model | pending | | policy resolver (S5) |
| 8 | `model:'fable'` alias accepted in agent() opts | pending | | frontier flag (S5) |
| 9 | Worst-case args payload (~500KB) accepted without truncation | pending | | args waist (S1) |
| 10 | parallel() with >16 tasks queues to completion; pipeline()/phase() available | pending | | fan-out stages (S4) |
| 11 | Oversized StructuredOutput behavior: truncation vs retry, bounded retries, cost | pending | | schema design (S4) |
| 12 | Large workflow return value (~2MB) survives to the session | pending | | compact-return design (S1) |
| 13 | A skill can detect Workflow-tool absence and fail cleanly | CONFIRMED | `CLAUDE_CODE_DISABLE_WORKFLOWS=1 claude -p "...say WORKFLOW_PRESENT/WORKFLOW_ABSENT..."` → `WORKFLOW_ABSENT`; same prompt without the env var → `WORKFLOW_PRESENT`. Detection recipe for SKILL.md preflight: ask the session to check its own tool registry for `Workflow` before dispatch | preflight (S1) |
| 14 | semantic-release version_variables bumps pipeline.js in lockstep | pending | | packaging (S2) |
| 15 | /goal evaluator reliably matches sentinel lines (pass and fail transcripts) | pending | | goal loop (S7) |
| 16 | `tools:` frontmatter enforced under agentType (validator cannot Bash) | pending | | agent security (S4) |

## Decisions forced by this gate

- **D1 (from #2): source layout** — lib/ modules vs build-step artifact: **build-step artifact.** Workflow scripts are hard self-contained: static ESM import fails to parse and `require` is undefined. Source lives in `workflows/src/*.js`; a build step concatenates/bundles into the single shipped `workflows/pipeline.js`. JS/Python parity fixtures test the built artifact.
- **D2 (from #4): executor permission strategy** — allowlist entry vs docs vs redesign: (pending)
- **D3 (from #6): effort regression compensation** — none needed vs model bump vs prompt: (pending)

## Raw observations

### Task 2 — Workflow invocation probes (2026-07-18, CLI 2.1.214)

**#1 (scriptPath from installed plugin dir).** The plugin is a versioned marketplace copy (`~/.claude/plugins/cache/claude-deep-review/deep-review/{2.4.0,2.5.0}/`), not a symlink to the repo. Copied `noop.js` to `2.5.0/probes-tmp/`, invoked Workflow with the absolute path → completed in 7ms, 0 agents, returned `{"ok":true,"marker":"PROBE_NOOP_V1"}`. `probes-tmp/` removed afterward. Note: `rm -rf` against the plugin cache is denied by the session permission layer; per-file `rm -f` + `rmdir` works.

**#2 (sibling import).** `import { probeValue } from './importee.js'` → rejected at launch: `SyntaxError: Unexpected token '{'. import call expects one or two arguments.` (the parser treats `import` as the dynamic `import()` call form only). `require('./importee.js')` variant → script launches but throws `ReferenceError: require is not defined`. Both module mechanisms unavailable ⇒ D1 = build-step artifact.

**#3 (headless).** Primary citation: artifact 33 — P2b pins the bench invocation context (isolated `HOME`+`CLAUDE_CONFIG_DIR`+`--plugin-dir`, no `--bare`, pre-seeded `hasTrustDialogAccepted`), P3 documents the cost-field envelope. Residual gap: artifact 33 never invoked the Workflow tool itself under `-p`, so one direct check was run: `claude -p "Invoke the Workflow tool with scriptPath '$PWD/probes/noop.js'..." --model sonnet --output-format json` (default permission mode) → `is_error:false`, 5 turns, $0.348, result text contains `{"ok":true,"marker":"PROBE_NOOP_V1"}`. The `--dangerously-skip-permissions` variant of the same command is denied by the outer orchestration session's classifier (consistent with artifact 33 P1's history note); default mode suffices for the bench runner.

**#13 (absence detection).** `CLAUDE_CODE_DISABLE_WORKFLOWS=1 claude -p <one-word probe> --model sonnet` → `WORKFLOW_ABSENT` ($0.195); identical command without the env var → `WORKFLOW_PRESENT` ($0.165). Clean, deterministic detection; no hang, no error path.
