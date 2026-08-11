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
- **The return is compact:** `{ ok, phaseReached, stats, artifactPaths: { findings, report, postReview, checkpoints }, checkpoints, resolvedPolicy, gaps }`. Full findings/report and the pre-selected delivery payload live on disk at `artifactPaths.*` — Phase 8 reads them.

---

## Wait Protocol (MANDATORY — do not end the turn early)

`Workflow` typically runs the pipeline as a **background task**. What comes back is a Task ID, a transcript dir, and a Run ID — **not** the compact return, and not the task output file's path either. The return and that path both appear only in the completion `<task-notification>`, and notifications are delivered only **between** turns. That is a circularity, and it is the whole problem: a session that yields its turn to receive the notification is the session that gets killed, and a session that holds its turn can never be handed the notification. Neither can wait correctly by itself, so the waiting is done by a script that watches the file directly. (If the tool result ever carries the compact return inline with no Task ID, step 2 below still applies.)

The kill is real but narrower than it used to be written here. It is a `claude -p` mechanism, not a general turn-end one: a `-p` run blocks on background tasks still running when the main turn ends, but only up to `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (default 600000 ms), then prints `Background tasks still running after Ns; terminating.` and exits. A Phase 3 review routinely exceeds that — `smoke-20260719-190902-a14b4cc` lost all three PRs exactly this way, which is why `bench/runner/invoke.py` exports the variable as `"0"` (wait unbounded) for its own children. Interactive sessions are not subject to that ceiling; the #38-profiled run yielded its turn for 994.1s and resumed cleanly. **The protocol does not branch on which mode it is in** — holding the turn and observing the file is correct in both, and a protocol that had to know which one it was running under would be one more thing to get wrong.

1. **Never end the turn and never begin Phase 8 without a terminal workflow result.**
2. **If the tool result carries the compact return inline** (no Task ID) → carry it into Phase 8.
3. **Otherwise** → run the awaiter with the Task ID, in-turn, under an explicit Bash `timeout: 600000` (the tool's 120s default would kill it mid-wait):

   ```
   python3 "{plugin_root}/scripts/await_workflow.py" --attempt 1
           --artifacts-dir "{output_dir}" --head-sha {head_sha_short} -- <task-id>
   ```

4. **Branch on the exit code:** `0` → stdout is the terminal `{ ok, phaseReached, ... }` return, carry it into Phase 8. `3` → run stdout's `next_command` verbatim, unedited. `5` → the persisted artifacts are present but the return was never observed; declare a `workflow-timeout` gap and deliver from the on-disk artifacts. `4` → exhausted; declare a `workflow-timeout` gap and deliver partial artifacts per the Phase 8 degradation rules. Never fabricate a result.

The awaiter watches `<tmp-root>/<project-slug>/<session-uuid>/tasks/<task-id>.output`, which it resolves from the Task ID (`$CODE_GAUNTLET_TASKS_DIR` overrides the search). Detection is structural, not a keyword match: the file is an envelope of `{summary, agentCount, logs, result, workflowProgress, totalTokens, totalToolCalls}` with the pipeline's return nested at **`.result`**, so a bare `ok` is not enough to qualify — the assemble receipt carries one too, and mistaking a receipt for the return would hand Phase 8 the wrong object. A terminal result requires a boolean `ok` plus at least one field only the compact return carries. Unparseable input is never an error, only "not yet terminal" — the target reads as absent or zero bytes for most of a run, so a detector that raised or exited on unparseable input would turn the ordinary case into a lost review. Partial-write tolerance is the awaiter's job and not an assumption it gets to skip: the file has not been caught mid-write in sampling to date, but "not yet observed" is not "cannot happen", and a torn read that ends just after some nested object closes is exactly how a scan gets fooled into accepting an agent receipt as the pipeline's return.

Because the task file lives at a path the harness owns and this script has to derive, `--artifacts-dir`/`--head-sha` arm a second, independent observable: the four persisted artifacts, which are *ours*, at a path Phase 2 constructs. A layout change on the harness side alone cannot lose a review, and a writer failure alone cannot either. Only artifacts newer than the wait's start count, so a stale set left at the same head SHA can never be delivered as if it were this run.

A missing/empty compact return is a failure to surface, not an empty-but-successful review — and a result is "in hand" only when it was read from the awaiter's stdout.

---

## Internal Stage Map

The workflow threads eight stages inside one top-level try/catch, checkpointing each. Every `agent()` call is wrapped for the throw/`null` failure contract, so a stage degrades (records a gap) rather than aborting the run.

| # | Stage | What it dispatches | Degradation |
|---|-------|--------------------|-------------|
| 1 | **Summarize** | `change-summarizer` (one call; fans out per-file buckets + a merge call for >500-line PRs) | empty summary + gap |
| 2 | **Discover** | one `parallel()` fan-out to every active discovery agent (see roster) | a null member marks that agent's dimensions degraded |
| 3 | **Merge** | pure JS (no agents) — regroups + dedups discovered findings | — |
| 4 | **Verify** | one `executor` per finding-slice, sequential, one retry per slice before degrading | an untrusted slice (after its retry) → that slice's findings `origin=unknown`; other slices unaffected; `verified=false` iff any slice degraded; loud gap per degraded slice |
| 5 | **Validate** | one `validator` per batch, `parallel()` | a null batch → its findings `validation=skipped`, kept at face value |
| 6 | **Filter** | pure JS (no agents) — thresholds, injection filter, dedup, routing | — |
| 7 | **Challenge** | one `challenger` per finding (blind), up to `limits.challengeCap` | overflow / null → `challenge=skipped`, routed to the unverified bucket |
| 8 | **Report** | `report-writer` (segmented if oversized) | throw/null → deterministic minimal report + gap |
|  | **Select delivery** | pure `selectDelivery` applies `args.delivery.tier` (`all` ⇒ every survivor, `main_only` ⇒ main-tagged), ranks, and caps at `limits.deliveryCap` | — (deterministic glue, no dispatch) |
|  | **Persist** | **no dispatch at all** on the default RETURN channel — the primaries ride home in the return and Phase 8 writes them; on the writer paths, `artifact-writer` (+ a second pinned executor command on the derived path — see below) writes/derives findings.json + report.md + post-review payload + checkpoints | RETURN: nothing to fail here, and a failure to materialize is loud in Phase 8. Writer paths: throw/null, or an untrusted assemble receipt → partial-artifacts gap, `artifactPaths` nulled |

Models per stage come from `resolvePolicy` (S5): discovery Sonnet with **security-reviewer Opus**; validator, challenger, executor, report-writer, artifact-writer Sonnet. A non-null `policy.subagentModel` (from `CLAUDE_CODE_SUBAGENT_MODEL`) overrides all of these.

---

## Discovery Agent Roster

The Discover stage groups the nine dimensions by agent and fans out one task per **active** agent (a dimension is active when its `conditionalFlag` is null — the ungateable core dims `bug`, `security` — or its `agentFlags` entry is not the literal `false`; the seven extended dims share the `deep` flag, so a light-scope `{ deep: false }` leaves only bug-detector + security-reviewer):

1. **bug-detector** — logic errors, edge cases, null handling, race conditions, API misuse. Dimension: `bug`.
2. **security-reviewer** — OWASP top 10, injection, auth bypass, data exposure, crypto. Always Opus. Dimension: `security`.
3. **cross-file-impact** — caller/dependent tracing, cross-module impact. Dimension: `cross_file_impact`.
4. **test-analyzer** — coverage gaps, test quality. Dimension: `test_coverage`.
5. **conventions-and-intent** — CLAUDE.md/REVIEW.md adherence, intent alignment, comment accuracy. Dimensions: `convention`, `intent`, `comment_accuracy`.
6. **code-simplifier** — simplification opportunities, dead code, redundancy. Dimension: `simplification`.
7. **type-design-analyzer** — type encapsulation, invariant expression. Dimension: `type_design`.

Each agent returns structured findings against the canonical schema (`agent()` `schema`), wrapped in the per-agent envelope `{ findings, complete, total_seen }`. The orchestrator injects the `agent` field during Merge. Discovery agents receive only the **context file path** (`{output_dir}/code-gauntlet-context-{head_sha_short}.md`), its **read plan**, and their target dimension(s) in the prompt — all shared context (rules, risk, diff) lives in that file, which Phase 2 wrote.

> **The read plan is arithmetic, not an instruction to paginate (issue #48).** A single `Read` of the context file returns only part of it and carries **no truncation notice**: on run `wf_cef39739-577` every one of the 7 discovery agents' first `Read` came back as 58,145 characters of a 95,057-byte file, ending at line 1083 of 2,028. Six agents inferred the cutoff and read on; `security-reviewer` did not, and reviewed roughly half the diff while reporting `complete: true`. So the stop condition is no longer the agent's to work out. Phase 2 measures the file (`contextLines` / `contextChars` on the args waist); `contextReadPlan` divides it into chunks bounded under both observed platform caps (2,000 lines, ~58,000 chars) and `sharedContextLine` enumerates the resulting calls verbatim in the prompt — e.g. `Read(offset=1, limit=641), Read(offset=642, limit=641), Read(offset=1283, limit=641), Read(offset=1924, limit=105)`. The agent's job is to make the listed calls. Read-to-end-by-offset survives only as the fallback wording when no measurement was stamped. `runWith` calls `sharedContextLine` **once** and threads the resulting string as `contextLine`; the stages never receive the context path, so none of them can build a context-read instruction that skips the plan. If no measurement was stamped, the run still completes but emits a `context_unmeasured` gap and the prompts fall back to fixed 750-line stepping. The agent-side backstop lives in `references/complete-read-contract.md`, duplicated verbatim into all 10 file-reading agents.

> **NDJSON emission has been removed in v3.** The `printf`-NDJSON emission prose was stripped from all 7 discovery agent `.md` bodies, and Bash is no longer granted to discovery agents — the by-value structured-output return (`{ findings, complete, total_seen }`) is the sole output path. If a discovery agent hits the schema-retry throw, that surfaces as a discover-stage gap unrelated to emission prose.

---

## Executor and Writer Roles

Two mechanical agents exist because the workflow script has no disk or shell:

- **executor** (`tools: Bash, Read`) — runs one pinned command exactly as given and returns what the prompt asks for through the receipt schema. For `assemble_artifacts.py` that is still the whole stdout line, verbatim; for `verify_findings.py` it is only a slice of the `--output` file — `status`, the `receipt` fields (`sha`, `n_in`, `nonce`, `deltas_checksum`, and `input_checksum` when the script reports one), the optional top-level `input_recovery` object, and every `result.deltas` entry — never the full `result.verified`/`result.eliminated` arrays the same file also holds. It never interprets, edits, or fabricates a success envelope; an honest `{status:'failed', ...}`/`{ok:false, ...}` is a legal answer. It has two distinct pinned commands, at two different stages:
  - **Verify stage:** `verify_findings.py`. The Verify stage trusts a slice only when `status==='ok'`, the receipt echoes the dispatched nonce/head sha/slice length, the echoed deltas cover exactly the dispatched finding ids, their content-proof checksum (`deltas_checksum`) matches what the workflow recomputes, and — guard (4) — the receipt's `input_checksum` (when present) matches the checksum the workflow computed over the dispatched slice input at materialization time. A slice input truncated by stray trailing bytes but still parseable is recoverable rather than untrusted: the script recovers the intact document, reports `input_recovery`, and the stage emits a `RECOVERED` disclosure gap instead of degrading the slice (see `validation-pipeline.md` for the full guard/ledger detail).
  - **Persist stage, derived path only:** `python3 "{plugin_root}/scripts/assemble_artifacts.py" --plan <persist-plan path>`, dispatched *after* the artifact-writer has put the plan on disk. It never re-derives findings itself — it reads the plan, derives `post-review.json` (a ranked/capped id projection of `findings.json`) and the checkpoint's `phases.challenge.findings` (that same array's alias-stripped twin), and returns an assemble receipt (`{ ok, planChecksum, verified: [{path, expected_chars, expected_checksum}...], written: [...] }`). The Persist stage (`trustAssembleReceipt`) grades every claim in that receipt against values the pipeline computed independently (the plan's own `planChecksum`, and each `expect`/`derive` entry's `chars`/`checksum`) — it never trusts a receipt to grade itself. A structural failure (no receipt, `ok:false`, an unverified path, a `planChecksum` mismatch) degrades to a partial-artifacts gap exactly like a writer failure; a *content*-proof mismatch on a derived document is also structural (there is no other on-disk copy to fall back to), while a mismatch on one of the two *primaries* (findings.json/report.md) is not — the run keeps its artifacts and raises a loud gap instead, since on-disk truth still exists to derive from.
- **artifact-writer** (`tools: Write, Read`) — persists a by-value payload to the exact paths given, writing exactly what it is given (no reformatting). It still materializes the per-slice `verify_findings.py` input files during the Verify stage (the workflow has no disk). At the **Persist** stage it is no longer the primary path — see the RETURN channel below — but stays live in two shapes:
  - **Legacy (full by-value):** taken whenever `args.persist` is absent, or the derivability guard refuses (a missing/duplicate finding id, a postReview/checkpoint entry that isn't a byte-identical twin of its findings.json row, or a non-JS-safe number) — one dispatch carrying all four terminal artifacts by value: findings JSON, report markdown, the pre-selected post-review payload JSON, and checkpoint JSON. This is the safety net for pathological input and is never deleted.
  - **Derived (issue #38, D3):** taken when `args.persist.assembleScriptPath` is present and the derivability guard passes — the writer persists only the three genuinely unique primaries (findings.json, report.md, and a persist-plan JSON at `code-gauntlet-persist-plan-{head_sha_short}.json`, matched by the same stale-file truncation glob so no separate cleanup is needed), and the executor's second pinned command (above) derives `post-review.json` and the checkpoint from what actually landed on disk. The plan is the instruction set, not data — it carries its own `planChecksum` (computed over the plan minus that field) plus per-document `{path, chars, checksum}` expectations (`expect` for the two primaries, `derive` for the two derived documents) so the assemble receipt can be checked against the pipeline's own numbers rather than trusted at face value.

- **No agent at all — the RETURN channel (`args.persist.returnPrimaries: true`, the default).** The Persist stage dispatches nothing. It returns the same three primaries inside the workflow's compact return, at `persistReturn`, and Phase 8 runs `python3 "{plugin_root}/scripts/materialize_artifacts.py"` to write them and derive the projections with the same `assemble_artifacts.py`. The reason is measured, not stylistic: across every recorded run the artifact-writer failed its own content proof on 26 of 73 writes and never wrote 12 artifacts, with no property of a document predicting which — while the harness's own serialization of a return value was byte-exact at every size probed up to 4 MB, against ~66 KB of unique content in the largest run on record. Re-encoding and re-dispatching were both tried and both measured to fail. The proof is unchanged and lives in the same plan (`expect` / `planChecksum` / `derive`); it now grades a harness-written copy instead of a model-written one. Above a 1,000,000-char return budget it falls back to a writer path with a disclosed gap — the derived one when `assembleScriptPath` was stamped, the legacy one otherwise. That fallback is the script path's only role here; the channel is taken on `returnPrimaries` alone.

  The public contract is identical on all three: the same `artifactPaths` shape (`findings`, `report`, `postReview`, `checkpoints`), the same partial-artifacts degradation. The output directory already exists (Phase 2 created it).

---

## Agent Failure Handling

Stage failures are non-fatal by design and arrive as `gaps` in the return: a degraded discovery dimension, an unverified verify set, a skipped validation batch, capped challenges, a minimal report, or partial artifacts. Surface every gap in the methodology — never hide a degraded stage. A hard `ok:false` (an unexpected throw in the deterministic glue) is recoverable via resume-from-checkpoint (Phase 8). Never reproduce a failed stage inline in the main session.
