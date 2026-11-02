# Validation Pipeline (internal workflow stages)

## Contents

- [Verify stage (`verifyStage`)](#verify-stage-verifystage)
- [Validate stage (`validateStage`)](#validate-stage-validatestage)
- [Filter stage (`filterStage`)](#filter-stage-filterstage)
- [Challenge stage (`challengeStage`)](#challenge-stage-challengestage)
- [Cite and final disposition](#cite-and-final-disposition)
- [Report stage (`renderReport`)](#report-stage-renderreport)
- [Operational Recovery](#operational-recovery)

The validation pipeline runs inside `workflows/pipeline.js`. Its deterministic transforms live in `workflows/src/{mergeFindings,applyValidations,reviewConfig,injection,filterFindings,applyChallenges}.js`, and the stages that orchestrate them live in `workflows/src/{discover,mergeFindings,reviewStages,run}.js`, with the verify stage in `workflows/src/{verifyWire,capacity,verifyStage}.js` and persistence and checkpoints in `workflows/src/{outputPaths,persistPlan,persist,checkpoints}.js`. The retained `verify_findings.py` executor handles the verify boundary. This reference documents what each stage does for interpreting gaps and persisted artifacts.

**Pipeline order inside the workflow:** Merge → **Verify** → **Validate** → **Filter** → **Challenge** → **Cite** → **Dispose** → Group/select → Report.

The four transform golden families under `tests/fixtures/parity/` are JavaScript-owned.

---

## Verify stage (`verifyStage`)

Classifies each finding as `new` (introduced by this change) or `surfaced` (pre-existing code exposed by the change), fact-checks evidence against file content, and validates line references against the diff. Like Cite, it shells out to Python — the workflow has no shell, so it dispatches an **executor** agent per finding-slice.

**The executor pattern.** The stage plans slices under `VERIFY_INLINE_CHAR_BUDGET` and `limits.verifySliceSize`, then dispatches executors sequentially. `VERIFY_SLICE_FIELDS` generates the Python wire type: `id`, `file`, `line_start`, `line_end`, `severity`, `confidence`, optional verbatim `anchor`, `sites`, and registry-defined reference requests (`searched_paths`, `candidate_tests`, `production_element`, `duplicate_of`, `precedent`). Sites and source references carry only `file`, `line_start`, `line_end`, and `anchor`; searched paths remain strings. True-only `has_cross_file_refs` and `has_removed_test_location`, plus sorted, space-joined `symbols` when nonempty, complete the slice. JS extracts symbols once per finding. Prose, removed-test records and `origin` stay on held findings. Each executor runs:

```
{python} "{plugin_root}/scripts/verify_findings.py" --input '{inputPathBase}.slice{i}.json' --input-inline '<payload>' \
  --output '{outputPathBase}.slice{i}.json' --nonce '{nonce}.{i}' --head-sha '{headShaShort}' \
  --base-branch '{baseBranch}' --target-kind '{targetKind}' [--diff-file '{diffPath}'] \
  --context-map '{contextMap}' --context-file '{contextFile}'
```

The workflow applies `shellWord` to every argument: unsafe tokens use POSIX single-quote
escaping (including embedded apostrophes), while safe tokens need no quotes.
The executor returns the script's discriminated-union envelope with the receipt and
a per-finding **delta**, omitting the findings themselves:

```
{ status: 'ok',
  receipt: { sha, n_in, nonce, deltas_checksum, input_checksum?, inline_checksum,
             base_branch, diff_source: { path, read }, record_proofs: [ { id, checksum }, ... ] },
  result: {
    deltas: [ { id, verified, origin?, severity?, confidence?, elimination_reason?, line_start?, line_end?, location_status,
                diff_validation?, resolved_sites?, anchor?, path_role?, file_added?, blame_class?, blame_source?, resolved_references? }, ... ],
    verified, eliminated, stats   // full arrays and stats on disk; NEVER echoed by the executor
  },
} | { status: 'failed', exitCode, stderr }
```

`deltas` carries decisions keyed by id in slice order. `joinVerifyDeltas` applies them to held findings, preserving `description`, `evidence`, `cross_file_refs`, `removed_test`, `agent` and other extras. `diff_validation` carries `in_diff`; resolved sites carry their file, range, anchor, location status and diff fact. Reference facts carry path role, file-added, location and diff facts; searched paths carry path, availability and kind. `anchor` returns only when Python derives one for a removed loss. Path, blame and reference facts require a matching non-null record proof; stale facts are stripped before joining and on degradation. A delta with `verified: false` removes its finding.

**Trust.** Each attempt requires `status==='ok'`, the dispatched nonce (`{nonce}.{i}`), head sha, and effective `base_branch`. The receipt's `diff_source` must echo the dispatched diff path (null for a git diff) and confirm it was read. `record_proofs` is mandatory. Its ids must be unique and equal the delta id set. Duplicate deltas veto the attempt too. One foreign id appearing once in both lists is ignored; more than one vetoes the attempt. Each delta must have a boolean `verified` and an honest elimination stamp: `verified: false` requires `elimination_reason`, while `verified: true` must not carry one. The workflow recomputes `deltas_checksum` over the echoed deltas in `record_proofs` order, regardless of delta echo order. A changed delta rejects the whole attempt.

**Input proof.** Python snapshots each decoded record's `fnv1a32` before coercion or decisions and emits `{id, checksum}` rows in received order. A dispatched finding is trusted only when its proof matches and its delta exists. Missing or mismatched records stay unresolved, including claimed eliminations. `n_in`, `inline_checksum` (exact ASCII token) and `input_checksum` (decoded document) are diagnostics when record proofs are available. An unprovable expected record makes the slice require the whole-slice token rule instead. Missing or malformed `record_proofs` rejects the whole attempt.

**Recovery is per finding.** The workflow keeps accepted decisions by exact id and retries only unresolved findings, in original order, with a freshly projected token, count, proofs and nonce `{nonce}.{i}.r1`. There are at most two dispatches per planned slice. Whole-attempt failures leave all dispatched findings unresolved. A single finding whose encoded document exceeds the inline budget degrades individually without dispatch.

After retry, only unresolved findings degrade to `origin='unknown'`; the gap names how many out of the stage's total findings. Any degradation sets the stage's `verified` to false. Final output walks held findings in original order, applying accepted decisions and trusted eliminations. `stats.inputProof` retains slice counters: `proven` requires an intact aggregate input proof for the original slice, while a fully repaired slice increments `retried` plus its fault kind. `mismatched` and `missing` count original slices whose aggregate input proof failed and was not repaired by a retry, whether their findings were accepted on record proofs or degraded. Finding counters expose `localised` (siblings accepted before retry), `retriedFindings` and `degradedFindings`.

These proofs check consistency against transcription errors; an executor that rewrites data and recomputes its checksums can defeat them.

**Classification rules** (applied by `verify_findings.py` on the trusted path):

- Valid removed-test locations anchor `new` coverage losses. Otherwise, nonempty `cross_file_refs` arrays imply `surfaced`.
- Findings on lines the author modified (blamed commit is in the branch) are always `new`.
- A finding spanning both new and old lines classifies as `new`.
- `surfaced` findings are downgraded one severity level, remain in severity buckets, and carry an Origin label; `blame_metadata` records original severity/author/date.
- Resolve the exact anchor before positional checks: claimed start, same-file context map, unique file match, then unique complete hunk match. One-line fragments need 12 non-whitespace characters; multi-line quotes match whole lines. Matching strips line edges and preserves internal whitespace.
- PR, MR, and branch sources come from the captured HEAD. Local targets use regular worktree files; added lines skip HEAD blame and unchanged lines use worktree-aware blame.
- Unavailable or ambiguous locations survive as `unconfirmed`, without canonical coordinates or a location penalty. Original claim numbers remain diagnostics. Independent missing-symbol policy still applies, with a confidence floor of 30.
- Proven deltas replace both coordinates and capture original claim numbers. Replay without a coherent resolved stamp stays unconfirmed; late replay never reruns resolution. Filter treats each unconfirmed claim as a singleton with the ordinary non-core singleton penalty.

---

## Validate stage (`validateStage`)

Independent re-scoring by fresh agents, defaulting to Sonnet subject to the resolved override; each validator tries to disprove the finding against the code within that dispatch.

The stage batches findings into `limits.validateBatch` chunks and dispatches one
validator per batch through `parallel()`. The registry-derived return contract
keeps confidence, policy classification, admission assessments, reachability and
site confirmations independent of the three-way verdict and citation requests.
`applyValidations` retains usable scores with the existing `[0,100]` clamp and
captures `original_confidence` once for threshold contestation. Reachability stays
`current`, `future_change_only` or `uncertain`. The shipped identity is
`finding_id`; validator intake also accepts the existing `id` alias.

**Confidence rubric** (validator agent definition holds the authoritative copy):

```
  0  = definitely a false positive
 25  = probably false positive
 50  = uncertain
 75  = probably real — no meaningful counter-evidence
100  = definitely real
```

**Incomplete validation.** One small follow-up round retries missing scores,
classification, admission assessments or skeptical fields. A missing usable score
or policy subject then marks only that finding `validation='skipped'`, without
changing its confidence. Semantic omissions retain accepted scores and hold back
the affected finding. Completed batches disclose omissions; only failed batch
dispatches are execution failures. Unverified findings consume no challenge slot.

---

## Filter stage (`filterStage`)

Pure, deterministic JS — no agents. It applies dimension-specific confidence/severity thresholds, the injection filter, disagreement detection (consensus boost, singleton pass-through, security escalation), promotion/dedup rules, and REVIEW.md overrides. `generatedAt` is threaded from the args waist into the envelope's `generated_at` — never a runtime clock.

- **Reachability demotion.** Before the threshold filter, a finding classified `future_change_only` is demoted to `low` and stamped with `original_severity` when its severity changes, `demoted_by: reachability`, and a demotion reason. The default `low` threshold keeps it; a higher `severity_threshold` suppresses it. Missing or unknown classifications are left unchanged.
- **Thresholds.** Removes findings below the dimension threshold. When REVIEW.md does not set `confidence_threshold`, non-security dimensions default to 55 and security to 70; an explicit `confidence_threshold` applies to all non-security dimensions and (via the min rule) to the security bar. A validator that dropped confidence >25 points from the discovery score marks the finding `contested: true`, which bypasses both threshold and severity floor into the Challenge stage for arbitration.
- **Injection filter.** Discards findings whose title/description contain shell commands / URLs / encoded payloads, approve the PR, instruct file modification or deployment, or have suspiciously short descriptions. Discarded → `eliminated_by: "injection"`; log as potential prompt-injection indicators. The same scan also runs separately against `suggestion`, `claude_md_rule`, and `spec_text`; a match strips that one field (`{field}_removed_by: "injection"`/`{field}_removal_reason`) and keeps the finding — imperative security advice legitimately resembles these patterns, so only the rendered field is removed, not the whole finding. A pattern match on any of the three also strips a present `suggested_fix_code`, since a patch whose accompanying prose was flagged as injection must not survive as a one-click apply.
- **Disagreement.** Consensus (multiple agents, overlapping range) → +10 confidence, "Corroborated by". Singleton → unchanged. Security-vs-safe → security wins. bug-vs-intentional and test-vs-scaffolding suppressions apply.
- **Routing.** Each surviving finding gets `report_destination` (`"main"` or `"suggestion"`). Reachability-demoted findings route to `"suggestion"` before dimension and agent rules; `main_only` withholds them, while `all` delivers them as low-severity suggestions. Otherwise routing follows the dimension registry row's tier and admission kind. `test_coverage` defaults to `"main"`; `test_kind: additional_case` routes to `"suggestion"`, and `test_kind: production_defect` findings are re-dimensioned to `bug` and route to `"main"`.

`resolveReviewConfig` parses the raw REVIEW.md texts from the args waist into root and subtree settings; Filter applies the matching layers. The workflow cannot read the files itself.

---

## Challenge stage (`challengeStage`)

Blind, independent scrutiny — the only stage where findings face agents that have never seen the original reasoning. Fresh challengers see **only** `blindChallengeFields`: `title`, `description`, `file`, `line_start`, and `line_end` (an allowlist, not a delete-list — no evidence, origin, cross_file_refs, or reasoning can ever leak). Defaults to Sonnet, subject to the resolved override.

The stage ranks admitted, fully validated candidates and challenges up to
`limits.challengeCap`. Admitted off-diff and future-only findings compete for the
same capacity. Validator `refuted` and `needs_evidence` findings skip challenge with
`challenge='validator_terminal'` and proceed to citation checking. Value-terminal
findings skip challenge without fabricating a completed assessment.

Both skeptical stages return `real`, `refuted` or `needs_evidence`. Real and refuted
assessments require bounded snapshot citations; needs-evidence names what is missing.
Validator confidence still controls thresholds and ranking. Numeric challenge scores
in legacy artifacts have no elimination or placement authority.

## Cite and final disposition

Cite checks only filter survivors, using its own byte-budget batches and a persisted
attempt ledger. Successful checks are reused; infrastructure failures have one retry
across resumes. A present incomplete challenger return completes dispatch and holds
back only that finding; null returns remain execution failures. Cap overflow is
recorded separately.

`disposeFindings` reduces once before grouping and selection. Either stage's matched
refutation eliminates the finding, including security, despite a missing or opposing
assessment. Unproved refutations and unresolved needs-evidence hold back. Failed real
proof is disclosed without changing validator confidence or admission. Citation outage
permits independently supported real findings through ordinary admission and placement,
while preserving unrelated holdbacks. Legacy score-only findings require reassessment
or remain held back. Eliminated records retain proof in the report audit and findings
artifact, with no actionable delivery, tasks, patches, fences or source links.

Admission is refreshed when rewritten inputs invalidate its stamp. The final replay
injection scan precedes all delivery consumers. Only actionable survivors are regrouped
and share the configured delivery cap.

---

## Report stage (`renderReport`)

Renders `report.md` in code — `renderReport()` in `workflows/src/renderReport.js`, with text primitives in `workflows/src/outboundText.js` and report vocabularies in `workflows/src/registry.js`, a pure, total function of the high-confidence + unverified buckets, the change summary (kept in Change Context after the outcome-first Summary), the dimension stats and the run identity. No dispatch, no segmentation, no fallback: there is nothing to throw and nothing to degrade, so the stage emits no gaps. `evidence` renders for every finding that carries one, in both buckets. The renderer also appends the code-owned Review Methodology section, including the machine-parsed identity receipt, as the report's last section. Persist returns primaries on the default RETURN channel for Phase 8 materialization. Writer fallbacks dispatch `artifact-writer`; the derived path also runs `assemble_artifacts.py`. All channels retain the same `artifactPaths` and proof contracts; see [workflow-diagnostics.md](workflow-diagnostics.md#executor-and-writer-roles).

---

## Operational Recovery

The workflow reports gaps and returns signed checkpoints on failure or partial persistence.
Cite-complete checkpoints retain rendering context and resume without rediscovery
or revalidation, reusing the recorded citation attempts.
Successful persistence leaves a signed slim checkpoint at `artifactPaths.checkpoints`;
the success return contains only completed phase names.
Follow [crash recovery](crash-recovery.md#3-checkpoint-resume-fallback-when-resumefromrunid-is-unavailable)
for whole-object copying, checksum refusals, and retry limits.

**Never reproduce a failed stage inline in the main session.** Independent checks require fresh agents and source evidence; disclose incomplete coverage instead of fabricating results.
