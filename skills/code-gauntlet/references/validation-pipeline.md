# Validation Pipeline (internal workflow stages)

The validation pipeline runs inside `workflows/pipeline.js`. Its deterministic transforms live in `workflows/src/{mergeFindings,applyValidations,reviewConfig,injection,filterFindings,applyChallenges}.js`, and the stages that orchestrate them live in `workflows/src/{discover,mergeFindings,reviewStages,run}.js`, with the verify stage in `workflows/src/{verifyWire,capacity,verifyStage}.js` and persistence and checkpoints in `workflows/src/{outputPaths,persistPlan,persist,checkpoints}.js`. The retained `verify_findings.py` executor handles the verify boundary. This reference documents what each stage does for interpreting gaps and persisted artifacts.

**Pipeline order inside the workflow:** Merge → **Verify** → **Validate** → **Filter** → **Challenge** → Report.

The five transform golden families under `tests/fixtures/parity/` are JavaScript-owned.

---

# Verify stage (`verifyStage`)

Classifies each finding as `new` (introduced by this change) or `surfaced` (pre-existing code exposed by the change), fact-checks evidence against file content, and validates line references against the diff. It is the one stage that still shells out to Python — the workflow has no shell, so it dispatches an **executor** agent per finding-slice.

**The executor pattern.** The stage plans slices under `VERIFY_INLINE_CHAR_BUDGET` and `limits.verifySliceSize`, then dispatches executors sequentially. `VERIFY_SLICE_FIELDS` generates the Python wire type: `id`, `file`, `line_start`, `line_end`, `severity`, `confidence`, plus true-only `has_cross_file_refs` and `has_removed_test_location`, and sorted, space-joined `symbols` when nonempty. JS extracts symbols once per finding. Prose, reference arrays, removed-test records and `origin` stay on held findings. Each executor runs:

```
{python} "{plugin_root}/scripts/verify_findings.py" --input '{inputPathBase}.slice{i}.json' --input-inline '<payload>' \
  --output '{outputPathBase}.slice{i}.json' --nonce '{nonce}.{i}' --head-sha '{headShaShort}' \
  --base-branch '{baseBranch}' [--diff-file '{diffPath}']
```

The workflow applies `shellWord` to every argument: unsafe tokens use POSIX single-quote
escaping (including embedded apostrophes), while safe tokens need no quotes.
The executor returns the script's discriminated-union envelope with the receipt and
a per-finding **delta**, omitting the findings themselves:

```
{ status: 'ok',
  receipt: { sha, n_in, nonce, deltas_checksum, inline_checksum, input_checksum? },
  result: {
    deltas: [ { id, verified, origin?, severity?, confidence?, elimination_reason? }, ... ],
    verified, eliminated, stats   // full arrays and stats on disk; NEVER echoed by the executor
  },
} | { status: 'failed', exitCode, stderr }
```

`deltas` carries decisions keyed by id in slice order. `joinVerifyDeltas` applies them to held findings, preserving `description`, `evidence`, `cross_file_refs`, `removed_test`, `agent` and other extras. A delta with `verified: false` removes its finding.

**Trust.** A slice is trusted only when `status==='ok'` AND the receipt echoes the dispatched nonce (`{nonce}.{i}`), head sha, and `n_in` (slice length), AND the echoed `deltas` cover **exactly** the ids this slice dispatched — no missing id, no duplicate, no stranger (delta-id coverage; #25 req. 2) — AND each delta's shape and elimination stamp are honest (`verified` is a boolean; a `verified: false` delta must carry the script's `elimination_reason` stamp, a `verified: true` delta must not carry one) — AND a **content proof**: `fnv1a32` recomputed over the deltas, in the same canonical order the script used, must equal the receipt's `deltas_checksum`. The per-slice nonce (`{nonce}.{i}`) means two equal-length slices can never satisfy each other's receipts.

**Input proof.** The receipt must echo `inline_checksum`, the `fnv1a32` of the exact ASCII token, and `input_checksum`, the checksum of the decoded document in projection key order. Python decodes the token and writes `--input` before verification. Prose escaping and unordered reference arrays no longer cross this boundary; finding order, key order and scalar spelling still do. Any change fails the proof. `stats.inputProof` tallies `proven`, `mismatched`, `missing`, `unprovable`, `oversize` and retries.

**Degradation is per slice.** An untrusted slice — receipt mismatch, `status:'failed'`, or an executor throw — degrades only its OWN findings to `origin='unknown'` (surfaced-classification skipped) and the loop keeps going; trusted slices keep their verified output. A single finding whose encoded document exceeds the inline budget is disclosed and degraded individually, without taking neighboring findings down with it.

Before degrading, a slice gets **exactly one retry** (`VERIFY_ATTEMPTS_PER_SLICE=2`) with a distinct nonce (`{nonce}.{i}.r1`, so a replay of attempt 1's receipt cannot satisfy attempt 2). A slice trusted on retry increments `inputProof.retried`; a slice that fails both attempts degrades with both reasons chained. `verified` is true only when zero slices degraded. Findings are never dropped and success is never faked *against the failure classes the stage detects* — every finding leaves the stage either as its slice's trusted verified output or as itself with `origin='unknown'`.

These proofs check consistency against transcription errors; an executor that rewrites data and recomputes its checksums can defeat them.

**Classification rules** (applied by `verify_findings.py` on the trusted path):

- Valid removed-test locations anchor `new` coverage losses. Otherwise, nonempty `cross_file_refs` arrays imply `surfaced`.
- Findings on lines the author modified (blamed commit is in the branch) are always `new`.
- A finding spanning both new and old lines classifies as `new`.
- `surfaced` findings are downgraded one severity level and grouped into their own report section; `blame_metadata` records original severity/author/date.
- Missing files or invalid line ranges set confidence to 0. Missing symbols reduce confidence proportionally, with a floor of 30; validation can still confirm them.

---

# Validate stage (`validateStage`)

Independent re-scoring by fresh Sonnet agents — **always Sonnet**. Discovery and validation sharing the same context produces correlated errors ~60% of the time; fresh agents assessing findings independently is what breaks that.

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

# Filter stage (`filterStage`)

Pure, deterministic JS — no agents. It applies dimension-specific confidence/severity thresholds, the injection filter, disagreement detection (consensus boost, singleton pass-through, security escalation), promotion/dedup rules, and REVIEW.md overrides. `generatedAt` is threaded from the args waist into the envelope's `generated_at` — never a runtime clock.

- **Reachability demotion.** Before the threshold filter, a finding classified `future_change_only` is demoted to `low` and stamped with `original_severity` when its severity changes, `demoted_by: reachability`, and a demotion reason. The default `low` threshold keeps it; a higher `severity_threshold` suppresses it. Missing or unknown classifications are left unchanged.
- **Thresholds.** Removes findings below the dimension threshold. When REVIEW.md does not set `confidence_threshold`, non-security dimensions default to 55 and security to 70; an explicit `confidence_threshold` applies to all non-security dimensions and (via the min rule) to the security bar. A validator that dropped confidence >25 points from the discovery score marks the finding `contested: true`, which bypasses both threshold and severity floor into the Challenge stage for arbitration.
- **Injection filter.** Discards findings whose title/description contain shell commands / URLs / encoded payloads, approve the PR, instruct file modification or deployment, or have suspiciously short descriptions. Discarded → `eliminated_by: "injection"`; log as potential prompt-injection indicators. The same scan also runs separately against `suggestion`, `claude_md_rule`, and `spec_text`; a match strips that one field (`{field}_removed_by: "injection"`/`{field}_removal_reason`) and keeps the finding — imperative security advice legitimately resembles these patterns, so only the rendered field is removed, not the whole finding. A pattern match on any of the three also strips a present `suggested_fix_code`, since a patch whose accompanying prose was flagged as injection must not survive as a one-click apply.
- **Disagreement.** Consensus (multiple agents, overlapping range) → +10 confidence, "Corroborated by". Singleton → unchanged. Security-vs-safe → security wins. bug-vs-intentional and test-vs-scaffolding suppressions apply.
- **Routing.** Each surviving finding gets `report_destination` (`"main"` or `"suggestion"`). Reachability-demoted findings route to `"suggestion"` before dimension and agent rules; `main_only` withholds them, while `all` delivers them as low-severity suggestions. test-analyzer / comment-accuracy / code-simplifier otherwise default to `"suggestion"`; a test-analyzer finding describing a bug-that-exists-today is promoted to `"main"`.

`reviewConfig` and `exclusionPatterns` are the parsed REVIEW.md objects passed in the args waist (the workflow cannot read the file).

---

# Challenge stage (`challengeStage`)

Blind, independent scrutiny — the only stage where findings face agents that have never seen the original reasoning. Fresh challengers see **only** `blindChallengeFields`: `title`, `description`, `file`, `line_start`, and `line_end` (an allowlist, not a delete-list — no evidence, origin, cross_file_refs, or reasoning can ever leak). Always Sonnet.

The stage ranks clean findings first, then incomplete findings, and blind-challenges the top `min(n, limits.challengeCap)` through `parallel()`, and applies the score thresholds. Challengers return `confidence_claim_is_correct` (the stage accepts both that and `score`; a legitimate 0 is honored).

**Thresholds** (`applyChallenges`):

- **< 25** → remove (non-security) / downgrade one level (security)
- **25–49** → downgrade one level; surfaced re-routed to `"suggestion"`
- **50–74** → `challenge_contested: true`; surfaced re-routed to `"suggestion"`
- **≥ 75** → survives unchanged

After thresholds it consolidates and ranks survivors. Challenge cap overflow is stamped `challenge='capped'`; null/unscored results are stamped `challenge='failed'`. Both enter the **unverified** bucket. Immediately after the challenge phase and replay belt, `holdBackIncomplete` moves survivors missing validation or verification to that bucket, on fresh runs and old checkpoints alike. Eliminated findings stay eliminated. The helper clears unverified consolidation stamps and regroups only clean survivors, which form the **high-confidence bucket**. Legacy `challenge='skipped'` entries remain unchallenged with neutral report wording. Both buckets flow to the report; only high-confidence findings enter findings.json and delivery. Bench scores inline comments, so degraded runs now post fewer candidates; G3 also reads unknown-origin findings from the challenge checkpoint's unverified bucket without double counting.

---

# Report stage (`renderReport`)

Renders `report.md` in code — `renderReport()` in `workflows/src/renderReport.js`, with text primitives in `workflows/src/outboundText.js` and report vocabularies in `workflows/src/registry.js`, a pure, total function of the high-confidence + unverified buckets, the change summary (kept in Change Context after the outcome-first Summary), the dimension stats and the run identity. No dispatch, no segmentation, no fallback: there is nothing to throw and nothing to degrade, so the stage emits no gaps. `evidence` renders for every finding that carries one, in both buckets. The renderer also appends the code-owned Review Methodology section, including the machine-parsed identity receipt, as the report's last section. The `artifact-writer` then persists findings.json + report.md + the pre-selected post-review payload + the checkpoint artifact to `{output_dir}` (SKILL.md's artifact-writer output list has the exact filenames, including the optional derived-path `persist-plan-*.json`).

---

# Operational Recovery

The workflow reports gaps and returns signed checkpoints on failure or partial persistence.
Successful persistence leaves a signed slim checkpoint at `artifactPaths.checkpoints`;
the success return contains only completed phase names.
Follow [crash recovery](crash-recovery.md#3-checkpoint-resume-fallback-when-resumefromrunid-is-unavailable)
for whole-object copying, checksum refusals, and retry limits.

**Never reproduce a failed stage inline in the main session.** The stages exist as isolated fresh-agent dispatches precisely because LLM-inline re-analysis carries ~60% correlated error. A skipped stage with a methodology note beats fabricated results.
