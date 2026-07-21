# Validation Pipeline (internal workflow stages)

In v3 the validation pipeline runs **inside** `workflows/pipeline.js` — the main session does not invoke `verify_findings.py`, `apply_validations.py`, `filter_findings.py`, or `apply_challenges.py` between agent rounds. Those five deterministic transforms are now JS twins compiled into the bundle (`workflows/src/{mergeFindings,applyValidations,filterFindings,applyChallenges,findingDedup}.js`), and the stages that orchestrate them live in `workflows/src/stages.js`. This reference documents what each stage does — for understanding a `gaps` entry or debugging a persisted artifact, not for running anything by hand.

**Pipeline order inside the workflow:** Merge → **Verify** → **Validate** → **Filter** → **Challenge** → Report.

The Python scripts are still shipped and still pass their suites (parity is proven against frozen golden fixtures), but they are exercised only by the retained `verify_findings.py` executor path and by tests — not by the skill.

---

# Verify stage (`verifyStage`)

Classifies each finding as `new` (introduced by this change) or `surfaced` (pre-existing code exposed by the change), fact-checks evidence against file content, and validates line references against the diff. It is the one stage that still shells out to Python — the workflow has no shell, so it dispatches an **executor** agent per finding-slice.

**The executor pattern.** The stage slices the merged findings into `limits.verifySliceSize` chunks and first dispatches the **artifact-writer** to persist each `${verify.inputPathBase}.slice{i}.json` (the workflow has no disk, so the writer materializes the slice inputs the script will read; a writer throw/null degrades the whole set to UNVERIFIED — never a fabricated verification). It then dispatches one `executor` agent per slice **sequentially** so each receipt pairs to its slice by order. Each executor runs exactly:

```
python3 {verify.scriptPath} --input {inputPathBase}.slice{i}.json --output {outputPathBase}.slice{i}.json \
  --nonce {nonce}.{i} --head-sha {headShaShort} --base-branch {baseBranch} [--diff-file {diffPath}]
```

and returns the script's discriminated-union envelope verbatim:

```
{ status: 'ok', receipt: { sha, n_in, nonce }, result: { verified, eliminated, batches, stats } }
   | { status: 'failed', exitCode, stderr }
```

**Trust.** A slice is trusted only when `status==='ok'` AND the receipt echoes the dispatched nonce (`{nonce}.{i}`), head sha, and `n_in` (slice length), AND `verified.length + eliminated.length === n_in` (the truncation guard — proof the result body was not silently cut). The per-slice nonce (`{nonce}.{i}`) means two equal-length slices can never satisfy each other's receipts.

**Degradation.** ANY untrusted slice — receipt mismatch, `status:'failed'`, or an executor throw — degrades the WHOLE set to the UNVERIFIED path: every original finding is re-emitted with `origin='unknown'`, surfaced-classification is skipped, a loud gap is recorded, and `verified=false`. Findings are never dropped and success is never faked.

**Classification rules** (applied by `verify_findings.py` on the trusted path):

- Findings with `cross_file_refs` are always `surfaced`.
- Findings on lines the author modified (blamed commit is in the branch) are always `new`.
- A finding spanning both new and old lines classifies as `new`.
- `surfaced` findings are downgraded one severity level and grouped into their own report section; `blame_metadata` records original severity/author/date.
- Factual verification: if any claim is wrong (line number, symbol existence, code mismatch), confidence is set to 0.

---

# Validate stage (`validateStage`)

Independent re-scoring by fresh Sonnet agents — **always Sonnet, even under `frontier`**. Discovery and validation sharing the same context produces correlated errors ~60% of the time; fresh agents assessing findings independently is what breaks that.

The stage batches findings into `limits.validateBatch` chunks and dispatches one `validator` per batch through `parallel()`. Each validator attempts to **disprove** each finding and returns `[{ id, confidence, justification }]` (confidence 0–100). `applyValidations` merges the adjustments in place (id match, `[0,100]` clamp, `original_confidence` captured once for the Phase-6 contestation mechanism). The validator's shipped output uses `finding_id`; the stage accepts both `finding_id` and `id`.

**Confidence rubric** (validator agent definition holds the authoritative copy):

```
  0  = definitely a false positive
 25  = probably false positive
 50  = uncertain
 75  = probably real — no meaningful counter-evidence
100  = definitely real
```

If the only path to an issue requires a hypothetical future change (new caller, changed config, new code path), cap confidence at **65** — below the non-security threshold of 70.

**Degradation.** A null/malformed batch means its findings went UNVALIDATED: they are kept at face value (never dropped, confidence untouched) and marked `validation='skipped'` with a loud gap. Attribution is by batch index so a degraded batch traces to its exact findings.

---

# Filter stage (`filterStage`)

Pure, deterministic JS — no agents. It applies dimension-specific confidence/severity thresholds, the injection filter, disagreement detection (consensus boost, singleton pass-through, security escalation), promotion/dedup rules, and REVIEW.md overrides. `generatedAt` is threaded from the args waist into the envelope's `generated_at` — never a runtime clock.

- **Thresholds.** Removes findings below the dimension threshold. When REVIEW.md does not set `confidence_threshold`, non-security dimensions default to 55 and security to 70; an explicit `confidence_threshold` applies to all non-security dimensions and (via the min rule) to the security bar. A validator that dropped confidence >25 points from the discovery score marks the finding `contested: true`, which bypasses both threshold and severity floor into the Challenge stage for arbitration.
- **Injection filter.** Discards findings that contain shell commands / URLs / encoded payloads, approve the PR, instruct file modification or deployment, or have suspiciously short descriptions. Discarded → `eliminated_by: "injection_filter"`; log as potential prompt-injection indicators.
- **Disagreement.** Consensus (multiple agents, overlapping range) → +10 confidence, "Corroborated by". Singleton → unchanged. Security-vs-safe → security wins. bug-vs-intentional and test-vs-scaffolding suppressions apply.
- **Routing.** Each surviving finding gets `report_destination` (`"main"` or `"suggestion"`). test-analyzer / comment-accuracy / code-simplifier default to `"suggestion"`; a test-analyzer finding describing a bug-that-exists-today is promoted to `"main"`.

`reviewConfig` and `exclusionPatterns` are the parsed REVIEW.md objects passed in the args waist (the workflow cannot read the file).

---

# Challenge stage (`challengeStage`)

Blind, independent scrutiny — the only stage where findings face agents that have never seen the original reasoning. Fresh challengers see **only** `blindChallengeFields`: `title`, `description`, and raw `code` (an allowlist, not a delete-list — no evidence, origin, cross_file_refs, or reasoning can ever leak). Sonnet in Optimized mode, `policy.frontierModelId` under `frontier`.

The stage ranks findings, blind-challenges the top `min(n, limits.challengeCap)` through `parallel()`, and applies the score thresholds. Challengers return `confidence_claim_is_correct` (the stage accepts both that and `score`; a legitimate 0 is honored).

**Thresholds** (`applyChallenges`):

- **< 25** → remove (non-security) / downgrade one level (security)
- **25–49** → downgrade one level; surfaced re-routed to `"suggestion"`
- **50–74** → `challenge_contested: true`; surfaced re-routed to `"suggestion"`
- **≥ 75** → survives unchanged

After thresholds it re-runs cross-agent dedup and ranks; that ranked set is the **high-confidence bucket**. Every UNCHALLENGED finding — challengeCap overflow OR a null/unscored member — is marked `challenge='skipped'` and routed to the **unverified** bucket (pipeline-degraded); it never enters the high-confidence set. Both buckets flow to the Report stage.

---

# Report stage (`reportStage`)

Dispatches the `report-writer` agent to render markdown from the high-confidence + unverified buckets (carried by value — the workflow has no disk). Oversized finding payloads are segmented into per-chunk dispatches joined under titled headings. On a throw OR null result, a deterministic **minimal report** is assembled from the pipeline stats and a gap is recorded — report failure is non-fatal. The `artifact-writer` then persists findings.json + report.md + the checkpoint artifact to `{output_dir}`.

---

# Operational Recovery

The workflow degrades internally and returns `gaps` rather than throwing, so most recovery is reading gaps and disclosing them. On the failure paths, nothing is persisted, so the resume state rides back **in the return's `checkpoints` field** (not on disk). Two cases need the skill:

1. **Hard failure (`ok:false`).** An unexpected throw in the deterministic glue returns `{ ok:false, error, phaseReached, checkpoints, ... }` with `artifactPaths` empty. Offer **resume-from-checkpoint**: if `return.checkpoints` has a `.phases` map, re-invoke the same `Workflow` call with `args.checkpoints` set to `return.checkpoints` (the workflow unwraps `.phases` and resumes at the first missing phase). If it is `{ completed, truncated: true }` (phase-outputs map over the ~100k-char budget — the findings bulk is deliberately withheld from the return), re-run from scratch without `args.checkpoints`.
2. **Partial artifacts (`ok:true`, writer failed).** A failed artifact-writer empties `artifactPaths` and records a partial-artifacts gap; the return's `checkpoints` carries the same `{ phases, completed }` (or truncated) resume state. Retry via resume-from-checkpoint as above; if it fails again, deliver whatever report exists via chat and disclose the gap.

On `ok:true` **success**, resume state is NOT in the return (`checkpoints` is just `{ completed }` names) — the full `{ phases, completed, phaseReached }` map is on disk at `artifactPaths.checkpoints` for a later re-run.

**Never reproduce a failed stage inline in the main session.** The stages exist as isolated fresh-agent dispatches precisely because LLM-inline re-analysis carries ~60% correlated error. A skipped stage with a methodology note beats fabricated results.
