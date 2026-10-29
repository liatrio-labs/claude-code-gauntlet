# Validation Pipeline (internal workflow stages)

## Contents

- [Verify stage (`verifyStage`)](#verify-stage-verifystage)
- [Validate stage (`validateStage`)](#validate-stage-validatestage)
- [Filter stage (`filterStage`)](#filter-stage-filterstage)
- [Challenge stage (`challengeStage`)](#challenge-stage-challengestage)
- [Report stage (`renderReport`)](#report-stage-renderreport)
- [Operational Recovery](#operational-recovery)

The validation pipeline runs inside `workflows/pipeline.js`. Its deterministic transforms live in `workflows/src/{mergeFindings,applyValidations,reviewConfig,injection,filterFindings,applyChallenges}.js`, and the stages that orchestrate them live in `workflows/src/{discover,mergeFindings,reviewStages,run}.js`, with the verify stage in `workflows/src/{verifyWire,capacity,verifyStage}.js` and persistence and checkpoints in `workflows/src/{outputPaths,persistPlan,persist,checkpoints}.js`. The retained `verify_findings.py` executor handles the verify boundary. This reference documents what each stage does for interpreting gaps and persisted artifacts.

**Pipeline order inside the workflow:** Merge → **Verify** → **Validate** → **Filter** → **Challenge** → Report.

The five transform golden families under `tests/fixtures/parity/` are JavaScript-owned.

---

## Verify stage (`verifyStage`)

Classifies each finding as `new` (introduced by this change) or `surfaced` (pre-existing code exposed by the change), fact-checks evidence against file content, and validates line references against the diff. It is the one stage that still shells out to Python — the workflow has no shell, so it dispatches an **executor** agent per finding-slice.

**The executor pattern.** The stage plans slices under `VERIFY_INLINE_CHAR_BUDGET` and `limits.verifySliceSize`, then dispatches executors sequentially. `VERIFY_SLICE_FIELDS` generates the Python wire type: `id`, `file`, `line_start`, `line_end`, `severity`, `confidence`, optional verbatim `anchor`, plus true-only `has_cross_file_refs` and `has_removed_test_location`, and sorted, space-joined `symbols` when nonempty. JS extracts symbols once per finding. Prose, reference arrays, removed-test records and `origin` stay on held findings. Each executor runs:

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
    deltas: [ { id, verified, origin?, severity?, confidence?, elimination_reason?, line_start?, line_end?, location_status }, ... ],
    verified, eliminated, stats   // full arrays and stats on disk; NEVER echoed by the executor
  },
} | { status: 'failed', exitCode, stderr }
```

`deltas` carries decisions keyed by id in slice order. `joinVerifyDeltas` applies them to held findings, preserving `description`, `evidence`, `cross_file_refs`, `removed_test`, `agent` and other extras. A delta with `verified: false` removes its finding.

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

The stage batches findings into `limits.validateBatch` chunks and dispatches one `validator` per batch through `parallel()`. Each validator attempts to **disprove** each finding and returns `[{ id, confidence, justification, reachability }]` (confidence 0–100). Reachability is `current` when the described defect is true of the committed tree (including dead code, a wrong type, a missing test, or a misleading comment when that is the finding), `future_change_only` when the failure requires a change outside this PR, and `uncertain` when the validator cannot determine which applies. `applyValidations` merges the score, justification, and known reachability classification in place (id match, `[0,100]` clamp, `original_confidence` captured once for the Phase-6 contestation mechanism). The validator's shipped output uses `finding_id`; the stage accepts both `finding_id` and `id`.

**Confidence rubric** (validator agent definition holds the authoritative copy):

```
  0  = definitely a false positive
 25  = probably false positive
 50  = uncertain
 75  = probably real — no meaningful counter-evidence
100  = definitely real
```

**Degradation.** A null/malformed batch or a missing matched score marks the affected findings `validation='skipped'`, with confidence untouched and one gap per batch naming the count. They still face challenge, then survivors enter the unverified bucket and appear only in the report. Degraded verify slices (`origin='unknown'`) follow the same route. How often validators omit scores is unmeasured.

---

## Filter stage (`filterStage`)

Pure, deterministic JS — no agents. It applies dimension-specific confidence/severity thresholds, the injection filter, disagreement detection (consensus boost, singleton pass-through, security escalation), promotion/dedup rules, and REVIEW.md overrides. `generatedAt` is threaded from the args waist into the envelope's `generated_at` — never a runtime clock.

- **Reachability demotion.** Before the threshold filter, a finding classified `future_change_only` is demoted to `low` and stamped with `original_severity` when its severity changes, `demoted_by: reachability`, and a demotion reason. The default `low` threshold keeps it; a higher `severity_threshold` suppresses it. Missing or unknown classifications are left unchanged.
- **Thresholds.** Removes findings below the dimension threshold. When REVIEW.md does not set `confidence_threshold`, non-security dimensions default to 55 and security to 70; an explicit `confidence_threshold` applies to all non-security dimensions and (via the min rule) to the security bar. A validator that dropped confidence >25 points from the discovery score marks the finding `contested: true`, which bypasses both threshold and severity floor into the Challenge stage for arbitration.
- **Injection filter.** Discards findings whose title/description contain shell commands / URLs / encoded payloads, approve the PR, instruct file modification or deployment, or have suspiciously short descriptions. Discarded → `eliminated_by: "injection"`; log as potential prompt-injection indicators. The same scan also runs separately against `suggestion`, `claude_md_rule`, and `spec_text`; a match strips that one field (`{field}_removed_by: "injection"`/`{field}_removal_reason`) and keeps the finding — imperative security advice legitimately resembles these patterns, so only the rendered field is removed, not the whole finding. A pattern match on any of the three also strips a present `suggested_fix_code`, since a patch whose accompanying prose was flagged as injection must not survive as a one-click apply.
- **Disagreement.** Consensus (multiple agents, overlapping range) → +10 confidence, "Corroborated by". Singleton → unchanged. Security-vs-safe → security wins. bug-vs-intentional and test-vs-scaffolding suppressions apply.
- **Routing.** Each surviving finding gets `report_destination` (`"main"` or `"suggestion"`). Reachability-demoted findings route to `"suggestion"` before dimension and agent rules; `main_only` withholds them, while `all` delivers them as low-severity suggestions. test-analyzer / comment-accuracy / code-simplifier otherwise default to `"suggestion"`; a test-analyzer finding describing a bug-that-exists-today is promoted to `"main"`.

`resolveReviewConfig` parses the raw REVIEW.md texts from the args waist into root and subtree settings; Filter applies the matching layers. The workflow cannot read the files itself.

---

## Challenge stage (`challengeStage`)

Blind, independent scrutiny — the only stage where findings face agents that have never seen the original reasoning. Fresh challengers see **only** `blindChallengeFields`: `title`, `description`, `file`, `line_start`, and `line_end` (an allowlist, not a delete-list — no evidence, origin, cross_file_refs, or reasoning can ever leak). Defaults to Sonnet, subject to the resolved override.

The stage ranks clean findings first, then incomplete findings, and blind-challenges the top `min(n, limits.challengeCap)` through `parallel()`, and applies the score thresholds. Challengers return `confidence_claim_is_correct` (the stage accepts both that and `score`; a legitimate 0 is honored).

**Thresholds** (`applyChallenges`):

- **< 25** → remove (non-security) / downgrade one level (security)
- **25–49** → downgrade one level; surfaced re-routed to `"suggestion"`
- **50–74** → `challenge_contested: true`; surfaced re-routed to `"suggestion"`
- **≥ 75** → survives unchanged

After thresholds it consolidates and ranks survivors. Challenge cap overflow is stamped `challenge='capped'`; null/unscored results are stamped `challenge='failed'`. Both enter the **unverified** bucket. Immediately after the challenge phase and replay belt, `holdBackIncomplete` moves survivors missing validation or verification to that bucket, on fresh runs and old checkpoints alike. Eliminated findings stay eliminated. The helper clears unverified consolidation stamps and regroups only clean survivors, which form the **high-confidence bucket**. Legacy `challenge='skipped'` entries remain unchallenged with neutral report wording. Both buckets flow to the report; only high-confidence findings enter findings.json and delivery.

---

## Report stage (`renderReport`)

Renders `report.md` in code — `renderReport()` in `workflows/src/renderReport.js`, with text primitives in `workflows/src/outboundText.js` and report vocabularies in `workflows/src/registry.js`, a pure, total function of the high-confidence + unverified buckets, the change summary (kept in Change Context after the outcome-first Summary), the dimension stats and the run identity. No dispatch, no segmentation, no fallback: there is nothing to throw and nothing to degrade, so the stage emits no gaps. `evidence` renders for every finding that carries one, in both buckets. The renderer also appends the code-owned Review Methodology section, including the machine-parsed identity receipt, as the report's last section. Persist returns primaries on the default RETURN channel for Phase 8 materialization. Writer fallbacks dispatch `artifact-writer`; the derived path also runs `assemble_artifacts.py`. All channels retain the same `artifactPaths` and proof contracts; see [workflow-diagnostics.md](workflow-diagnostics.md#executor-and-writer-roles).

---

## Operational Recovery

The workflow reports gaps and returns signed checkpoints on failure or partial persistence.
Successful persistence leaves a signed slim checkpoint at `artifactPaths.checkpoints`;
the success return contains only completed phase names.
Follow [crash recovery](crash-recovery.md#3-checkpoint-resume-fallback-when-resumefromrunid-is-unavailable)
for whole-object copying, checksum refusals, and retry limits.

**Never reproduce a failed stage inline in the main session.** Independent checks require fresh agents and source evidence; disclose incomplete coverage instead of fabricating results.
