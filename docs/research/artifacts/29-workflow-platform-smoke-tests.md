# 29 — Workflow platform smoke tests (v3 Phase 0 gate)

Empirical verification of the 16 platform assumptions in the v3 design spec (S9).
Run dates, CLI version, and raw observations recorded per row. Statuses:
CONFIRMED / REFUTED / INCONCLUSIVE.

**CLI version under test:** 2.1.214 (Claude Code)

| # | Assumption | Status | Observed | Decision impact |
|---|------------|--------|----------|-----------------|
| 1 | Workflow scriptPath resolves from the installed plugin dir | pending | | packaging (S2) |
| 2 | scriptPath scripts can import/require sibling files | pending | | source layout (S3) |
| 3 | Headless `claude -p` can invoke the Workflow tool via scriptPath | pending | | bench runner (S6) |
| 4 | Workflow-spawned agent Bash works under interactive default/acceptEdits (or scoped allowlist pre-approves) | pending | | executor (S4.4) |
| 5 | agent() returns null (not throw) on terminal failure; parallel() isolates member failures | pending | | degradation paths (S4) |
| 6 | Frontmatter `effort` survives agentType dispatch | pending | | parity claim (S5) |
| 7 | opts.model overrides agentType frontmatter model | pending | | policy resolver (S5) |
| 8 | `model:'fable'` alias accepted in agent() opts | pending | | frontier flag (S5) |
| 9 | Worst-case args payload (~500KB) accepted without truncation | pending | | args waist (S1) |
| 10 | parallel() with >16 tasks queues to completion; pipeline()/phase() available | pending | | fan-out stages (S4) |
| 11 | Oversized StructuredOutput behavior: truncation vs retry, bounded retries, cost | pending | | schema design (S4) |
| 12 | Large workflow return value (~2MB) survives to the session | pending | | compact-return design (S1) |
| 13 | A skill can detect Workflow-tool absence and fail cleanly | pending | | preflight (S1) |
| 14 | semantic-release version_variables bumps pipeline.js in lockstep | pending | | packaging (S2) |
| 15 | /goal evaluator reliably matches sentinel lines (pass and fail transcripts) | pending | | goal loop (S7) |
| 16 | `tools:` frontmatter enforced under agentType (validator cannot Bash) | pending | | agent security (S4) |

## Decisions forced by this gate

- **D1 (from #2): source layout** — lib/ modules vs build-step artifact: (pending)
- **D2 (from #4): executor permission strategy** — allowlist entry vs docs vs redesign: (pending)
- **D3 (from #6): effort regression compensation** — none needed vs model bump vs prompt: (pending)

## Raw observations

(append per-probe sections below)
