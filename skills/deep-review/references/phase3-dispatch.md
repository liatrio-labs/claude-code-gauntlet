# Phase 3 Dispatch Reference

In v3 the review stages run **inside** the workflow. Phase 3 is a single `Workflow` tool call — not a batch of subagent dispatches. This reference covers the invocation contract, the internal stage map, and the agent roles the workflow drives.

---

## The Invocation

```
Workflow(
  scriptPath: "{plugin_root}/workflows/pipeline.js",
  args: { ...the args object assembled in Phase 2... }
)
```

- **One call.** The workflow orchestrates all eight stages and persists artifacts. Do not split work across calls, and do not run any review stage inline in the main session.
- **`scriptPath` is a repo file path.** The plugin ships `workflows/pipeline.js` as a plain file; there is no native plugin-workflow component, so invocation is always by `scriptPath`. Never copy the bundle into `.claude/workflows/` (avoids version drift).
- **Args arrive as one object.** The workflow normalizes a JSON-string-or-object waist, validates it (`validateArgs`), and rejects an unknown `argsVersion` or a missing required field before any dispatch.
- **The return is compact:** `{ ok, phaseReached, stats, artifactPaths: { findings, report, checkpoints }, resolvedPolicy, gaps }`. Full findings/report live on disk at `artifactPaths.*` — Phase 8 reads them.

---

## Internal Stage Map

The workflow threads eight stages inside one top-level try/catch, checkpointing each. Every `agent()` call is wrapped for the throw/`null` failure contract, so a stage degrades (records a gap) rather than aborting the run.

| # | Stage | What it dispatches | Degradation |
|---|-------|--------------------|-------------|
| 1 | **Summarize** | `change-summarizer` (one call; fans out per-file buckets + a merge call for >500-line PRs) | empty summary + gap |
| 2 | **Discover** | one `parallel()` fan-out to every active discovery agent (see roster) | a null member marks that agent's dimensions degraded |
| 3 | **Merge** | pure JS (no agents) — regroups + dedups discovered findings | — |
| 4 | **Verify** | one `executor` per finding-slice, sequential | any untrusted slice → whole set `origin=unknown`, `verified=false`, loud gap |
| 5 | **Validate** | one `validator` per batch, `parallel()` | a null batch → its findings `validation=skipped`, kept at face value |
| 6 | **Filter** | pure JS (no agents) — thresholds, injection filter, dedup, routing | — |
| 7 | **Challenge** | one `challenger` per finding (blind), up to `limits.challengeCap` | overflow / null → `challenge=skipped`, routed to the unverified bucket |
| 8 | **Report** | `report-writer` (segmented if oversized) | throw/null → deterministic minimal report + gap |
|  | **Persist** | `artifact-writer` writes findings.json + report.md + checkpoints | throw/null → partial-artifacts gap, `artifactPaths` nulled |

Models per stage come from `resolvePolicy` (S5): discovery Sonnet with **security-reviewer Opus**; validator, challenger, executor, report-writer, artifact-writer Sonnet. The `frontier` flag upgrades the challenger to `policy.frontierModelId`. A non-null `policy.subagentModel` (from `CLAUDE_CODE_SUBAGENT_MODEL`) overrides all of these.

---

## Discovery Agent Roster

The Discover stage groups the nine dimensions by agent and fans out one task per **active** agent (a dimension is active when its `conditionalFlag` is null or the matching `agentFlags` entry is truthy — all nine are unconditional today):

1. **bug-detector** — logic errors, edge cases, null handling, race conditions, API misuse. Dimension: `bug`.
2. **security-reviewer** — OWASP top 10, injection, auth bypass, data exposure, crypto. Always Opus. Dimension: `security`.
3. **cross-file-impact** — caller/dependent tracing, cross-module impact. Dimension: `cross_file_impact`.
4. **test-analyzer** — coverage gaps, test quality. Dimension: `test_coverage`.
5. **conventions-and-intent** — CLAUDE.md/REVIEW.md adherence, intent alignment, comment accuracy. Dimensions: `convention`, `intent`, `comment_accuracy`.
6. **code-simplifier** — simplification opportunities, dead code, redundancy. Dimension: `simplification`.
7. **type-design-analyzer** — type encapsulation, invariant expression. Dimension: `type_design`.

Each agent returns structured findings against the canonical schema (`agent()` `schema`), wrapped in the per-agent envelope `{ findings, complete, total_seen }`. The orchestrator injects the `agent` field during Merge. Discovery agents receive only the **context file path** (`{output_dir}/deep-review-context-{head_sha_short}.md`) and their target dimension(s) in the prompt — all shared context (rules, risk, diff) lives in that file, which Phase 2 wrote.

> **NDJSON emission is vestigial in v3.** Discovery agent `.md` bodies still carry the `printf`-NDJSON emission prose, but the workflow collects findings through structured output, not disk files. The bodies are untouched this plan (their cleanup is the deferred S8 migration). If a discovery agent hits the schema-retry throw because the vestigial "emit NDJSON" instructions conflict with structured-output dispatch, that surfaces as a discover-stage gap — pull the S8 prose removal forward if it recurs.

---

## Executor and Writer Roles

Two mechanical agents exist because the workflow script has no disk or shell:

- **executor** (`tools: Bash, Read`) — runs one pinned `verify_findings.py` command exactly as given, reads the `--output` file, and returns its contents verbatim through the receipt schema. It never interprets, edits, or fabricates a success envelope; an honest `{status:'failed', ...}` is a legal answer. The Verify stage trusts a slice only when `status==='ok'` and the receipt echoes the dispatched nonce, head sha, and slice length.
- **artifact-writer** (`tools: Write, Read`) — persists the by-value payload (findings JSON, report markdown, checkpoint JSON) to the exact paths given. The output directory already exists (Phase 2 created it). It writes exactly what it is given — no reformatting.

---

## Agent Failure Handling

Stage failures are non-fatal by design and arrive as `gaps` in the return: a degraded discovery dimension, an unverified verify set, a skipped validation batch, capped challenges, a minimal report, or partial artifacts. Surface every gap in the methodology — never hide a degraded stage. A hard `ok:false` (an unexpected throw in the deterministic glue) is recoverable via resume-from-checkpoint (Phase 8). Never reproduce a failed stage inline in the main session.
