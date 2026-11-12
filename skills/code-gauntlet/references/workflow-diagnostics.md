# Workflow Diagnostics

Read after a gap or failure, or when maintaining stage interfaces.
For live invocation and waiting use [phase3-dispatch.md](phase3-dispatch.md).

## Internal Stage Map

The workflow checkpoints stage outputs and records partial coverage as gaps. Total discovery loss with no findings or challenge replay fails the review; a gap must never be interpreted as a clean result.

| # | Stage | What it dispatches | Degradation |
|---|-------|--------------------|-------------|
| 1 | **Summarize** | `change-summarizer` (one call; fans out per-file buckets + a merge call for >500-line changes spanning multiple buckets) | empty summary + gap |
| 2 | **Discover** | primary specialist fan-out, then a bounded gap assignment on full reviews | a null primary marks its dimensions degraded; hunter failures are separate ledger diagnostics |
| 3 | **Merge** | pure JS (no agents) — regroups + dedups discovered findings | — |
| 4 | **Verify** | slices in order; batched `r1`, then bounded parallel `r2` singletons ([recovery contract](validation-pipeline.md#verify-stage-verifystage)) | `record_proofs` map trusted deltas to findings; `base_branch` and `diff_source` path/read gate each attempt; `n_in`, `inline_checksum` and `input_checksum` are diagnostics; only findings unresolved after recovery get `origin=unknown`; other slices unaffected; `verified=false` iff any finding degraded; loud gap per degraded slice; affected findings held back to the report |
| 5 | **Validate** | one `validator` per batch, `parallel()`, plus one follow-up round for missing typed verdicts | a null/malformed batch or missing verdict after retry → affected findings `validation=skipped`; a successful blind challenge supplies the missing validation |
| 6 | **Filter** | pure JS (no agents) — typed routes, injection filter, dedup | — |
|  | **Admission** | pure `evaluateAdmission`, repeated after Filter changes inputs | deterministic kind failures bypass challenge; unresolved claims and confirmed non-posting consequences go to challenge. Other incomplete findings bypass challenge as `admission_terminal`; disposal keeps `report_only` or `held_back` |
| 7 | **Challenge** | one blind `challenger` per admitted finding, unresolved claim or confirmed non-posting consequence, up to `limits.challengeCap` | overflow → `challenge=capped`, null/malformed verdict → `challenge=failed`; unchallenged and incomplete findings are held back after replay checks |
|  | **Group** | `grouper` after blind challenge, checkpointed with Challenge; only when two or more findings can be delivered | invalid or missing reply uses positional grouping with a gap; groups stay indivisible, use maximum severity and main tier when any member is main; lead ranking starts with severity, consequence, then security; folded security titles stay visible |
|  | **Select delivery** | pure `selectDelivery` applies `args.delivery.tier` (`all` ⇒ every surviving group, `main_only` ⇒ groups with any main-tier member), ranks, and caps at `limits.deliveryCap` | — (deterministic glue, no dispatch) |
| 8 | **Report** | **no dispatch** — `renderReport()` builds `report.md` as a pure function of the pipeline's own output | — (deterministic: there is no dispatch to fail) |
|  | **Persist** | **no dispatch at all** on the default RETURN channel — the primaries ride home in the return and Phase 8 writes them; on the writer paths, `artifact-writer` (+ a second pinned executor command on the derived path — see below) writes/derives findings.json + report.md + post-review payload + checkpoints | RETURN: nothing to fail here, and a failure to materialize is loud in Phase 8. Writer paths: throw/null, or an untrusted assemble receipt → partial-artifacts gap, `artifactPaths` nulled |

Models per stage come from `resolvePolicy`; [model-policy.md](model-policy.md) owns the stage defaults and the `subagentModel` override.

---

## Discovery Agent Roster

The Discover stage groups the nine dimensions by agent and fans out one task per **active** agent (a dimension is active when its `conditionalFlag` is null — the ungateable core dims `bug`, `security` — or its entry in the pipeline's own DERIVED scope-gating map (`deriveAgentFlags`, from `riskTable`/`changedLines`/`scopeAnswer` — see `references/phase2-triage.md`) is not the literal `false`; the seven extended dims share the `deep` flag, so a light-scope derivation of `{ deep: false }` leaves only bug-detector + security-reviewer):

1. **bug-detector** — correctness, error handling, and defects in changed tests. Dimension: `bug`.
2. **security-reviewer** — security across trust boundaries and attacker-controlled operations. Defaults to Opus, subject to the resolved override. Dimension: `security`.
3. **cross-file-impact** — compatibility between changed code and its consumers. Dimension: `cross_file_impact`.
4. **test-analyzer** — test behavior, coverage gaps, and exposed production defects. Dimension: `test_coverage`.
5. **conventions-and-intent** — project conventions, documented intent, and comment accuracy. Dimensions: `convention`, `intent`, `comment_accuracy`.
6. **code-simplifier** — behavior-preserving simplifications and present correctness defects. Dimension: `simplification`.
7. **type-design-analyzer** — invariants, invalid states, and type design quality. Dimension: `type_design`.

The supplemental **gap-hunter** runs only in full scope when citation-only coverage leaves eligible regions. It uses the bug finding schema, its own producer identity and the `gap_hunt` trace category. Findings and cited evidence establish coverage; self-reported read claims do not. A skipped or failed hunter does not degrade primary dimensions or change the primary discover snapshot's coverage.

Each reviewer returns `{ findings, complete, total_seen }`. The primary specialists receive the shared context read plan and repository root; test-analyzer also receives the removed-test inventory. The hunter receives only framed assignment metadata, selected hunk reads and related fact reads. Its total assignment fits 24,000 bytes and 750 lines, favoring neglected files, then risk and smaller packets. The ledger records omitted regions and facts. Deletion-only packets with no surviving head anchor are not dispatched.

The pinned `plan_discovery.py` reads the SHA-256-bound plan from disk. Its full ledger records citations, assignments, omissions, hunter status, outside-assignment findings and remaining uncited regions. The trace carries a bounded `coverage_ledger` summary plus `coverage_ledger_artifact` (filename and SHA-256); the trace collector copies and verifies that companion artifact before loading full coverage. Missing or invalid plans, transport receipts or ledger artifacts disclose coverage unavailable while preserving specialist findings. Selection costs one Sonnet executor call per request part and a dispatched hunter adds finalization calls under the same per-part bound. Requests are plain JSON: snapshot file indexes, ascending source-line ranges per reviewer, and indexed risk ranges. Each part is at most 1,000 characters; more than four parts disables coverage. Reviewer indexes follow the registry; file indexes follow the sorted snapshot manifest. Both maps are checksum-bound. A citation such as `"0":"2:12-14;5:8,20-33"` means reviewer 0 cited those source-line ranges in files 2 and 5. The planner validates indexes and ordering against the disk manifest, and receipts bind the applied request. Executor effort is low and policy model overrides still apply. Each stdout envelope is capped at 2,048 characters.

## Shared-context read plan

Phase 2 stamps the existing context measurements `contextLines`, `contextChars`, `contextMaxLineChars` (including LF),
`contextReadPlanVersion: 1`, and `contextReadChunks`: per-chunk line counts packed from actual lengths.
The writer packs at most 750 lines and 24,000 UTF-8 bytes including LF and a 12-byte reserve per line
for numbering. This assumes a host Read cap of 25,000 tokens and at least one input byte per text token,
so the charged bytes bound the tokens and leave a 1,000-token margin. Oversized single lines remain isolated.
The 30,000-character heuristic remains only for aggregate fallback; per-chunk size counts stay internal.
The workflow validates positive integer counts, their 750-line limit, their sum against `contextLines`,
and the shared registry's 2,000-chunk ceiling. The writer uses that same ceiling; it never emits more counts.
Prompts enumerate at most ten calls in full, then list compact offset:limit pairs for every variable chunk;
uniform plans use one shared predicate to select the stepping rule. Large measured plans retain their
boundaries instead of reverting to the widest-line heuristic. At most 2,000 counts mean at most 10,000
characters in the writer's JSON array, 25,998 characters of pairs, and a context-read instruction below
26,400 characters plus the path. This bounded prompt cost preserves packed reading on large contexts.
All entry points apply the same version gate. Missing or invalid receipts produce one aggregate-fallback
gap, including aggregate-only legacy receipts: losing both new fields is indistinguishable from an old receipt,
so fallback is always disclosed. Missing line counts take precedence and produce only `context_unmeasured`;
if no aggregate plan fits the allocation ceiling, one `context_unplannable` gap names that cause.
Agents make every planned call and continue past the last received line if a call stops early.
`runWith` builds `sharedContextLine` once and threads `contextLine` to stages so they cannot
construct a competing instruction from a path. Missing measurement yields `context_unmeasured`;
an unplannable measurement yields `context_unplannable`. Both use fixed 750-line stepping until
no content remains. The complete-read mirrors in every reader provide the dependent-file backstop.

---

## Executor and Writer Roles

Two mechanical agents exist because the workflow script has no disk or shell:

- **executor** (`tools: Bash, Read`) — runs one pinned command exactly as given and returns what its agent file specifies for that script through the receipt schema. For `assemble_artifacts.py` that is still the whole stdout line, verbatim; for `verify_findings.py` it is only a slice of the `--output` file — `status`, every `receipt` field (`sha`, `n_in`, `nonce`, `deltas_checksum`, `input_checksum`, `inline_checksum`, `base_branch`, `diff_source`, `record_proofs`), and every `result.deltas` entry — never the full `result.verified`/`result.eliminated` arrays the same file also holds. It never interprets, edits, or fabricates a success envelope; an honest `{status:'failed', ...}`/`{ok:false, ...}` is a legal answer. Pinned commands include:
  - **Discover stage:** `plan_discovery.py` consumes bounded plain citation requests and the disk plan, writes the ledger and assignment, and returns a checked receipt. A second operation records hunter results; neither operation returns the full plan or ledger through the model.
  - **Verify stage:** `verify_findings.py --input <destination> --input-inline '<payload>' ...`. The executor reproduces the quoted token exactly in one Bash call. The workflow requires `status==='ok'`, the dispatched nonce/head sha, matching `base_branch` and `diff_source` path with `read=true`, and valid deltas with a matching content checksum (`deltas_checksum`). `record_proofs` determine which dispatched findings those deltas verify. `n_in`, `inline_checksum` and `input_checksum` are diagnostics. The [recovery contract](validation-pipeline.md#verify-stage-verifystage) defines retry eligibility, nonces and separate attempt artifacts.
  - **Persist stage, derived path only:** `{python} "{plugin_root}/scripts/assemble_artifacts.py" --plan <persist-plan path>`, dispatched *after* the artifact-writer has put the plan on disk. It never re-derives findings itself — it reads the plan, derives `post-review.json` (a ranked/capped id projection of `findings.json`) and the checkpoint's `phases.challenge.findings` (that same array's alias-stripped twin), and returns an assemble receipt (`{ ok, planChecksum, verified: [{path, expected_chars, expected_checksum}...], written: [...] }`). The Persist stage (`trustAssembleReceipt`) grades every claim in that receipt against values the pipeline computed independently (the plan's own `planChecksum`, and each `expect`/`derive` entry's `chars`/`checksum`) — it never trusts a receipt to grade itself. A structural failure (no receipt, `ok:false`, an unverified path, a `planChecksum` mismatch) degrades to a partial-artifacts gap exactly like a writer failure; a *content*-proof mismatch on a derived document is also structural (there is no other on-disk copy to fall back to), while a mismatch on one of the two *primaries* (findings.json/report.md) is not — the run keeps its artifacts and raises a loud gap instead, since on-disk truth still exists to derive from.
- **artifact-writer** (`tools: Write, Read`) — persists a by-value payload to the exact paths given, writing exactly what it is given (no reformatting). Writer fallbacks have two shapes:
  - **Legacy (full by-value):** taken whenever `args.persist` is absent, or the derivability guard refuses (a missing/duplicate finding id, a postReview/checkpoint entry that isn't a byte-identical twin of its findings.json row, or a non-JS-safe number) — one dispatch carrying all four terminal artifacts by value: findings JSON, report markdown, the pre-selected post-review payload JSON, and checkpoint JSON. This is the safety net for pathological input and is never deleted.
  - **Derived:** taken when the derivability guard passes — the writer persists only the three genuinely unique primaries (findings.json, report.md, and a persist-plan JSON at `code-gauntlet-persist-plan-{head_sha_short}.json`, matched by the same stale-file truncation glob so no separate cleanup is needed), and the executor's second pinned command (above) derives `post-review.json` and the checkpoint from what actually landed on disk. The plan is the instruction set, not data — it carries its own `planChecksum` (computed over the plan minus that field) plus per-document `{path, chars, checksum}` expectations (`expect` for the two primaries, `derive` for the two derived documents) so the assemble receipt can be checked against the pipeline's own numbers rather than trusted at face value.

- **No agent at all — the RETURN channel (`args.persist.returnPrimaries: true`, the default).** The Persist stage dispatches nothing. It returns the same three primaries inside the workflow's compact return, at `persistReturn`, and Phase 8 runs `{python} "{plugin_root}/scripts/materialize_artifacts.py"` to write them and derive the projections with the same `assemble_artifacts.py`. The plan retains the same `expect` / `planChecksum` / `derive` proof on all channels. Above `RETURN_CHAR_BUDGET` (1,000,000 characters), RETURN falls back with a disclosed gap to the derived writer when derivable, otherwise the legacy writer. The workflow derives the assembler path from `pluginRoot`; RETURN is selected by `returnPrimaries` alone.

  The public contract is identical on all three: the same `artifactPaths` shape (`findings`, `report`, `postReview`, `checkpoints`), the same partial-artifacts degradation. The output directory already exists (Phase 1 created it).

---

## Agent Failure Handling

Partial-stage failures arrive as `gaps`: degraded discovery dimensions, unverified verify slices, skipped validation, capped/failed challenges, or partial artifacts. Code renders their count in methodology. All active discovery dimensions degraded with no findings and no challenge replay returns `ok:false` and `all-degraded:`; the returned checkpoint omits discovery so recovery dispatches it again. Unexpected deterministic failures also return `ok:false`. Follow [crash recovery](crash-recovery.md), never reproduce a failed stage inline.
