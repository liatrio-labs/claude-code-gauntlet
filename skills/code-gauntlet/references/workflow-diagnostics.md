# Workflow Diagnostics

Read after a gap or failure, or when maintaining stage interfaces.
For live invocation and waiting use [phase3-dispatch.md](phase3-dispatch.md).

## Internal Stage Map

The workflow checkpoints stage outputs and records partial coverage as gaps. Total discovery loss with no findings or challenge replay fails the review; a gap must never be interpreted as a clean result.

| # | Stage | What it dispatches | Degradation |
|---|-------|--------------------|-------------|
| 1 | **Summarize** | `change-summarizer` (one call; fans out per-file buckets + a merge call for >500-line changes spanning multiple buckets) | empty summary + gap |
| 2 | **Discover** | one `parallel()` fan-out to every active discovery agent (see roster) | a null member marks that agent's dimensions degraded |
| 3 | **Merge** | pure JS (no agents) — regroups + dedups discovered findings | — |
| 4 | **Verify** | one `executor` per finding-slice, sequential; retry sends only unresolved findings | `record_proofs` map trusted deltas to findings; `base_branch` and `diff_source` path/read gate each attempt; `n_in`, `inline_checksum` and `input_checksum` are diagnostics; only findings unresolved after retry get `origin=unknown`; other slices unaffected; `verified=false` iff any finding degraded; loud gap per degraded slice; affected survivors held back after challenge |
| 5 | **Validate** | one `validator` per batch, `parallel()` | a null/malformed batch or missing score → affected findings `validation=skipped`, challenged then held back to the report |
| 6 | **Filter** | pure JS (no agents) — thresholds, injection filter, dedup, routing | — |
| 7 | **Challenge** | one `challenger` per finding (blind), up to `limits.challengeCap` | overflow → `challenge=capped`; null → `challenge=failed`; present but incomplete → `challenge=incomplete`, holding back only that finding |
| 8 | **Cite** | `executor` checks bounded snapshot citations in batches | unmatched proof and outages are recorded; infrastructure failures have one retry across resumes |
| 9 | **Dispose** | pure `disposeFindings` reduces verdicts, proof and admission before grouping | proven refutations eliminate; unresolved evidence holds back; failed real proof is disclosed |
|  | **Select delivery** | pure `selectDelivery` applies `args.delivery.tier` (`all` ⇒ every survivor, `main_only` ⇒ main-tagged), ranks, and caps at `limits.deliveryCap` | — (deterministic glue, no dispatch) |
| 10 | **Report** | **no dispatch** — `renderReport()` builds `report.md` as a pure function of the pipeline's own output | — (deterministic: there is no dispatch to fail) |
|  | **Persist** | **no dispatch at all** on the default RETURN channel — the primaries ride home in the return and Phase 8 writes them; on the writer paths, `artifact-writer` (+ a second pinned executor command on the derived path — see below) writes/derives findings.json + report.md + post-review payload + checkpoints | RETURN: nothing to fail here, and a failure to materialize is loud in Phase 8. Writer paths: throw/null, or an untrusted assemble receipt → partial-artifacts gap, `artifactPaths` nulled |

Models per stage come from `resolvePolicy`; [model-policy.md](model-policy.md) owns the stage defaults and the `subagentModel` override.

---

## Discovery Agent Roster

The Discover stage groups the nine dimensions by agent and fans out one task per **active** agent (a dimension is active when its `conditionalFlag` is null — the ungateable core dims `bug`, `security` — or its entry in the pipeline's own DERIVED scope-gating map (`deriveAgentFlags`, from `riskTable`/`changedLines`/`scopeAnswer` — see `references/phase2-triage.md`) is not the literal `false`; the seven extended dims share the `deep` flag, so a light-scope derivation of `{ deep: false }` leaves only bug-detector + security-reviewer):

1. **bug-detector** — logic errors, edge cases, null handling, race conditions, API misuse. Dimension: `bug`.
2. **security-reviewer** — OWASP top 10, injection, auth bypass, data exposure, crypto. Defaults to Opus, subject to the resolved override. Dimension: `security`.
3. **cross-file-impact** — caller/dependent tracing, cross-module impact. Dimension: `cross_file_impact`.
4. **test-analyzer** — coverage gaps, test quality. Dimension: `test_coverage`.
5. **conventions-and-intent** — CLAUDE.md/REVIEW.md adherence, intent alignment, comment accuracy. Dimensions: `convention`, `intent`, `comment_accuracy`.
6. **code-simplifier** — simplification opportunities, dead code, redundancy. Dimension: `simplification`.
7. **type-design-analyzer** — type encapsulation, invariant expression. Dimension: `type_design`.

Each agent returns structured findings against the canonical schema (`agent()` `schema`), wrapped in the per-agent envelope `{ findings, complete, total_seen }`. The orchestrator injects the `agent` field during Merge. Discovery agents receive the **context file path** (`{output_dir}/code-gauntlet-context-{head_sha_short}.md`), its **read plan**, and their target dimension(s) in the prompt; test-analyzer also receives the `removedTests` inventory. All shared context (rules, risk, diff) lives in that file, which Phase 2 wrote.

## Shared-context read plan

Phase 2 stamps `contextLines`, `contextChars` and `contextMaxLineChars` (including LF).
`contextReadPlan` bounds each chunk by 750 lines and 30,000 characters using the maximum
line length, falling back to the average for older receipts. It enumerates at most ten
calls, then expresses longer plans as an offset rule, with a ceiling of 2,000 chunks.
Agents make every planned call and continue past the last received line if a call stops early.
`runWith` builds `sharedContextLine` once and threads `contextLine` to stages so they cannot
construct a competing instruction from a path. Missing measurement yields `context_unmeasured`;
an unplannable measurement yields `context_unplannable`. Both use fixed 750-line stepping until
no content remains. The complete-read mirrors in ten readers provide the dependent-file backstop.

---

## Executor and Writer Roles

Two mechanical agents exist because the workflow script has no disk or shell:

- **executor** (`tools: Bash, Read`) — runs one pinned command exactly as given and returns what the prompt asks for through the receipt schema. For `assemble_artifacts.py` that is still the whole stdout line, verbatim; for `verify_findings.py` it is only a slice of the `--output` file — `status`, every `receipt` field (`sha`, `n_in`, `nonce`, `deltas_checksum`, `input_checksum`, `inline_checksum`, `base_branch`, `diff_source`, `record_proofs`), and every `result.deltas` entry — never the full `result.verified`/`result.eliminated` arrays the same file also holds. It never interprets, edits, or fabricates a success envelope; an honest `{status:'failed', ...}`/`{ok:false, ...}` is a legal answer. It has two distinct pinned commands, at two different stages:
  - **Verify stage:** `verify_findings.py --input <destination> --input-inline '<payload>' ...`. The executor reproduces the quoted token exactly in one Bash call. The workflow requires `status==='ok'`, the dispatched nonce/head sha, matching `base_branch` and `diff_source` path with `read=true`, and valid deltas with a matching content checksum (`deltas_checksum`). `record_proofs` determine which dispatched findings those deltas verify. `n_in`, `inline_checksum` and `input_checksum` are diagnostics. A retry sends only unresolved findings; only findings still unresolved after it degrade.
  - **Persist stage, derived path only:** `{python} "{plugin_root}/scripts/assemble_artifacts.py" --plan <persist-plan path>`, dispatched *after* the artifact-writer has put the plan on disk. It never re-derives findings itself — it reads the plan, derives `post-review.json` (a ranked/capped id projection of `findings.json`) and the checkpoint's `phases.challenge.findings` (that same array's alias-stripped twin), and returns an assemble receipt (`{ ok, planChecksum, verified: [{path, expected_chars, expected_checksum}...], written: [...] }`). The Persist stage (`trustAssembleReceipt`) grades every claim in that receipt against values the pipeline computed independently (the plan's own `planChecksum`, and each `expect`/`derive` entry's `chars`/`checksum`) — it never trusts a receipt to grade itself. A structural failure (no receipt, `ok:false`, an unverified path, a `planChecksum` mismatch) degrades to a partial-artifacts gap exactly like a writer failure; a *content*-proof mismatch on a derived document is also structural (there is no other on-disk copy to fall back to), while a mismatch on one of the two *primaries* (findings.json/report.md) is not — the run keeps its artifacts and raises a loud gap instead, since on-disk truth still exists to derive from.
- **artifact-writer** (`tools: Write, Read`) — persists a by-value payload to the exact paths given, writing exactly what it is given (no reformatting). Writer fallbacks have two shapes:
  - **Legacy (full by-value):** taken whenever `args.persist` is absent, or the derivability guard refuses (a missing/duplicate finding id, a postReview/checkpoint entry that isn't a byte-identical twin of its findings.json row, or a non-JS-safe number) — one dispatch carrying all four terminal artifacts by value: findings JSON, report markdown, the pre-selected post-review payload JSON, and checkpoint JSON. This is the safety net for pathological input and is never deleted.
  - **Derived:** taken when the derivability guard passes — the writer persists only the three genuinely unique primaries (findings.json, report.md, and a persist-plan JSON at `code-gauntlet-persist-plan-{head_sha_short}.json`, matched by the same stale-file truncation glob so no separate cleanup is needed), and the executor's second pinned command (above) derives `post-review.json` and the checkpoint from what actually landed on disk. The plan is the instruction set, not data — it carries its own `planChecksum` (computed over the plan minus that field) plus per-document `{path, chars, checksum}` expectations (`expect` for the two primaries, `derive` for the two derived documents) so the assemble receipt can be checked against the pipeline's own numbers rather than trusted at face value.

- **No agent at all — the RETURN channel (`args.persist.returnPrimaries: true`, the default).** The Persist stage dispatches nothing. It returns the same three primaries inside the workflow's compact return, at `persistReturn`, and Phase 8 runs `{python} "{plugin_root}/scripts/materialize_artifacts.py"` to write them and derive the projections with the same `assemble_artifacts.py`. The plan retains the same `expect` / `planChecksum` / `derive` proof on all channels. Above `RETURN_CHAR_BUDGET` (1,000,000 characters), RETURN falls back with a disclosed gap to the derived writer when derivable, otherwise the legacy writer. The workflow derives the assembler path from `pluginRoot`; RETURN is selected by `returnPrimaries` alone.

  The public contract is identical on all three: the same `artifactPaths` shape (`findings`, `report`, `postReview`, `checkpoints`), the same partial-artifacts degradation. The output directory already exists (Phase 1 created it).

---

## Agent Failure Handling

Partial-stage failures arrive as `gaps`: degraded discovery dimensions, unverified verify slices, skipped validation, capped/failed challenges, or partial artifacts. Code renders their count in methodology. All active discovery dimensions degraded with no findings and no challenge replay returns `ok:false` and `all-degraded:`; the returned checkpoint omits discovery so recovery dispatches it again. Unexpected deterministic failures also return `ok:false`. Follow [crash recovery](crash-recovery.md), never reproduce a failed stage inline.
