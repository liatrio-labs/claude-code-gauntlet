# Validation Pipeline (internal workflow stages)

## Contents

- [Verify stage (`verifyStage`)](#verify-stage-verifystage)
- [Validate stage (`validateStage`)](#validate-stage-validatestage)
- [Filter stage (`filterStage`)](#filter-stage-filterstage)
- [Admission (`evaluateAdmission`)](#admission-evaluateadmission)
- [Challenge stage (`challengeStage`)](#challenge-stage-challengestage)
- [Report stage (`renderReport`)](#report-stage-renderreport)
- [Operational Recovery](#operational-recovery)

The validation pipeline runs inside `workflows/pipeline.js`. Its deterministic transforms live in `workflows/src/{mergeFindings,applyValidations,injection,filterFindings,applyChallenges}.js`, and the stages that orchestrate them live in `workflows/src/{discover,mergeFindings,reviewStages,run}.js`, with the verify stage in `workflows/src/{verifyWire,capacity,verifyStage}.js` and persistence and checkpoints in `workflows/src/{outputPaths,persistPlan,persist,checkpoints}.js`. The retained `verify_findings.py` executor handles the verify boundary. This reference documents what each stage does for interpreting gaps and persisted artifacts.

**Pipeline order inside the workflow:** Merge → **Verify** → **Validate** → **Filter** → **Admission** → **Challenge** → Report. Admission is also evaluated before Filter so routing uses the current policy verdict.

---

## Verify stage (`verifyStage`)

Classifies each finding as `new` (introduced by this change) or `surfaced` (pre-existing code exposed by the change), fact-checks evidence against file content, and validates line references against the diff. It is the one stage that still shells out to Python — the workflow has no shell, so it dispatches an **executor** agent per finding-slice.

**The executor pattern.** The stage plans slices under `VERIFY_INLINE_CHAR_BUDGET` and `limits.verifySliceSize`, then processes slices in order; recovery can fan out in parallel. `VERIFY_SLICE_FIELDS` generates the Python wire type: `id`, `file`, `line_start`, `line_end`, `severity`, `confidence`, optional verbatim `anchor`, `sites`, and registry-defined reference requests (`searched_paths`, `candidate_tests`, `production_element`, `duplicate_of`, `precedent`). Sites and source references carry only `file`, `line_start`, `line_end`, and `anchor`; searched paths remain strings. True-only `has_cross_file_refs` and `has_removed_test_location`, plus sorted, space-joined `symbols` when nonempty, complete the slice. JS extracts symbols once per finding. Prose, removed-test records and `origin` stay on held findings. Each executor runs:

```
{python} "{plugin_root}/scripts/verify_findings.py" --input '{inputPathBase}.slice{i}{attempt}.json' --input-inline '<payload>' \
  --output '{outputPathBase}.slice{i}{attempt}.json' --nonce '{nonce}.{i}{attempt}' --head-sha '{headShaShort}' \
  --base-branch '{baseBranch}' --target-kind '{targetKind}' [--diff-file '{diffPath}'] \
  --context-map '{contextMap}' --context-file '{contextFile}'
```

`{attempt}` is empty initially, `.r1` for the batched retry, or `.r2.{index}` for a
singleton retry, where `{index}` is the record's original index within the slice.
Each attempt keeps its own input and output artifacts.

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

**Trust.** Each attempt requires `status==='ok'`, the dispatched nonce, head sha, and effective `base_branch`. The receipt's `diff_source` must echo the dispatched diff path (null for a git diff) and confirm it was read. `record_proofs` is mandatory. Its ids must be unique and equal the delta id set. Duplicate deltas veto the attempt too. One foreign id appearing once in both lists is ignored; more than one vetoes the attempt. Each delta must have a boolean `verified` and an honest elimination stamp: `verified: false` requires `elimination_reason`, while `verified: true` must not carry one. The workflow recomputes `deltas_checksum` over the echoed deltas in `record_proofs` order, regardless of delta echo order. A changed delta rejects the whole attempt.

**Input proof.** Python snapshots each decoded record's `fnv1a32` before coercion or decisions and emits `{id, checksum}` rows in received order. A dispatched finding is trusted only when its proof matches and its delta exists. Missing or mismatched records stay unresolved, including claimed eliminations. `n_in` and `input_checksum` (decoded document) are diagnostics when record proofs are available, and `inline_checksum` (exact ASCII token) is not consulted: the decoder accepts any spelling with one meaning. An unprovable expected record makes the slice require the whole-slice token rule instead. Missing or malformed `record_proofs` rejects the whole attempt.

**Recovery is per finding.** Accepted decisions stay fixed. Unresolved findings receive one batched `r1` retry. Findings still unresolved after a multi-record `r1`, including an `r1` that failed as a whole, receive parallel singleton retries within the budget. No third dispatch follows an already-singleton `r1`. A slice of `len` findings permits at most `2 + min(3, len - 1)` dispatches.

A finding above the 5,000-character copy target gets its own slice; multi-finding slices stay within that target, reduced when the hard cap is lower. A finding whose encoded document exceeds the hard cap degrades without dispatch: the cap is 50,000 characters on every host, further limited by the available Windows command budget (including native quoting overhead).

After recovery, only unresolved findings degrade to `origin='unknown'`; the gap groups their ids by terminal reason, retains the first failure reason, and names how many out of the stage's total findings. Any degradation sets the stage's `verified` to false. Final output walks held findings in original order, applying accepted decisions and trusted eliminations. `stats.inputProof` retains slice counters: `proven` requires an intact aggregate input proof for the original slice, while a fully repaired slice increments `retried` plus its fault kind. `mismatched` and `missing` count original slices whose aggregate input proof failed and was not repaired by a retry, whether their findings were accepted on record proofs or degraded. Finding counters expose `localised` (siblings accepted before retry), `retriedFindings` (all findings sent to `r1`, including cap-reached degradations) and `degradedFindings`.

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

Independent factual checking follows the [shared investigation contract](investigation-methodology.md),
using fresh agents defaulting to Sonnet subject to the resolved override.

Validators return `claim` (`confirmed`, `refuted`, `not_established`), `consequence`
(`present_failure`, `latent`, `artifact_defect`, `no_practical_effect`) only when confirmed, and `justification`. Consequence is the only runtime-effect axis.
A latent consequence names its future change or unusual precondition in `latent_condition`;
`not_established` names one missing fact in `missing_fact`. Refutations require a closed
`refutation_kind` and a source reference (`file`, `line_start`, `line_end`, exact `anchor`).
The schema and route registry live in `workflows/src/registry.js`.

Both readers inspect confirmed findings, including a validator's non-posting consequence. When
both confirm, the first consequence in the route registry's seriousness order wins. A challenger’s uncertainty or unverified refutation preserves a validator’s confirmation. Raw reader
answers remain separate from the resolved verdict. Latent findings post inline at low severity
when their locations allow it; consequence never forces body placement.

The executor runs `check_refutations.py` for refutations. Code checks the exact quote at the
captured head (the working tree for local reviews), the line range, and the saved diff.
Comment-only quotes and any span containing added lines fail closed. Requiring every cited line
to predate the PR prevents a new guard being padded with old context. Source resolution cannot
prove a model's semantic inference, but an intent comment alone cannot authorize elimination.
Unavailable or unverifiable evidence sends the finding onward.
The checker compiles changed-line ownership once, follows the saved patch's path-prefix policy,
and requires strictly decoded text outside the shared non-source directories and
artifact paths (documentation, data and repository rules). No external classifier is required. Unavailable or
undecodable source is reported as a gap. Incremental reviews use the full local base-to-captured-head range; an
unavailable base prevents verification and produces a gap. Security findings go to blind
challenge after a validator refutation. Only two independently verified refutations eliminate;
a single verified refutation stays report-only at main tier, without asserting low impact.

One smaller retry round repairs missing, duplicate or malformed typed returns. A persistent
failure records `validation=skipped` and proceeds as not established. Unknown Verify origins
remain held back because the source investigation was not proven.

---

## Filter stage (`filterStage`)

The registry routes truth and consequence independently:

| Answer | Route |
| --- | --- |
| Refuted with a code-verified citation | `eliminated`; security requires verified refutations from both readers |
| Refuted without verified counter-evidence | `not-established` |
| Confirmed present failure | `posted` at its severity |
| Confirmed latent consequence | `posted-latent`, low severity, suggestion tier |
| Confirmed artifact defect | `posted-artifact`, low severity |
| Confirmed no practical effect | `report-only` |
| Not established or absent verdict | `not-established` |

These are eligibility routes before challenge and delivery. Every posting route retains factual and deterministic kind gates. Injection checks still apply.
REVIEW.md is prose project rules, with no settings parser, thresholds, ignore lists, exclusions,
or per-directory overrides. A retired config block produces one local notice.

## Admission (`evaluateAdmission`)

For typed findings, admission projects the route table into existing delivery rights and retains
source-verification safeguards. It does not make a second posting-value judgment. Missing or invalid typed verdicts use the unresolved route, including older checkpoints.

## Challenge stage (`challengeStage`)

The existing blind challenger checks admitted findings and unresolved validation findings within
`limits.challengeCap`. Its allowlist supplies only title, description and location (and the saved
diff for deleted tests), withholding upstream verdicts and reasoning. It returns the same typed
schema and uses the same code citation check and routing table. A successful independent check
can establish an unresolved finding, including one skipped after validator dispatch failure.
The validator skip remains recorded. Unresolved results and failed or capped calls stay local
as held back. Consequence-based report-only findings also receive blind challenge. Finding-kind
admission failures remain local.

One ranking orders severity, consequence seriousness, security, proven origin, main versus suggestion tier, inline
eligibility, file, line and ID. It governs challenge capacity, delivery selection, group leads
and report order. Confidence and prose length do not participate. Group severity is the maximum
of the routed member severities; its tier is main when any admitted member is main. The grouper
returns only member IDs; code chooses the lead. Folded security claims remain visible.

The grouper still identifies claims fixed by one correction. Disposal assigns `deliver`, `body`,
`report_only`, `held_back` or `eliminate` from the existing source and admission rights. Delivery
caps and `main_only` still apply to whole groups. There is no additional cap on low-severity or
latent findings; the replay analysis retains its bounded policy only as a diagnostic.

---

## Report stage (`renderReport`)

`report_only` findings appear under **Findings held back by admission** with their predicate reasons. `held_back` findings appear under **Unverified / pipeline-degraded findings**; neither status grants posting or fix rights.

Renders `report.md` in code — `renderReport()` in `workflows/src/renderReport.js`, with text primitives in `workflows/src/outboundText.js` and report vocabularies in `workflows/src/registry.js`, a pure, total function of the findings and unverified buckets, the change summary (kept in Change Context after the outcome-first Summary), the dimension stats and the run identity. No dispatch, no segmentation, no fallback: there is nothing to throw and nothing to degrade, so the stage emits no gaps. `evidence` renders for every finding that carries one, in both buckets. The renderer also appends the code-owned Review Methodology section, including the machine-parsed identity receipt, as the report's last section. Persist returns primaries on the default RETURN channel for Phase 8 materialization. Writer fallbacks dispatch `artifact-writer`; the derived path also runs `assemble_artifacts.py`. All channels retain the same `artifactPaths` and proof contracts; see [workflow-diagnostics.md](workflow-diagnostics.md#executor-and-writer-roles).

---

## Operational Recovery

The workflow reports gaps and returns signed checkpoints on failure or partial persistence.
Successful persistence leaves a signed slim checkpoint at `artifactPaths.checkpoints`;
the success return contains only completed phase names.
Follow [crash recovery](crash-recovery.md#3-checkpoint-resume-fallback-when-resumefromrunid-is-unavailable)
for whole-object copying, checksum refusals, and retry limits.

**Never reproduce a failed stage inline in the main session.** Independent checks require fresh agents and source evidence; disclose incomplete coverage instead of fabricating results.
