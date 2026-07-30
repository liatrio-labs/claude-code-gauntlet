# Validation Pipeline (internal workflow stages)

In v3 the validation pipeline runs **inside** `workflows/pipeline.js` — the main session does not invoke `verify_findings.py`, `apply_validations.py`, `filter_findings.py`, or `apply_challenges.py` between agent rounds. Those five deterministic transforms are now JS twins compiled into the bundle (`workflows/src/{mergeFindings,applyValidations,filterFindings,applyChallenges,findingDedup}.js`), and the stages that orchestrate them live in `workflows/src/stages.js`. This reference documents what each stage does — for understanding a `gaps` entry or debugging a persisted artifact, not for running anything by hand.

**Pipeline order inside the workflow:** Merge → **Verify** → **Validate** → **Filter** → **Challenge** → Report.

The Python scripts are still shipped and still pass their suites (parity is proven against frozen golden fixtures), but they are exercised only by the retained `verify_findings.py` executor path and by tests — not by the skill.

---

# Verify stage (`verifyStage`)

Classifies each finding as `new` (introduced by this change) or `surfaced` (pre-existing code exposed by the change), fact-checks evidence against file content, and validates line references against the diff. It is the one stage that still shells out to Python — the workflow has no shell, so it dispatches an **executor** agent per finding-slice.

**The executor pattern.** The stage slices the merged findings into `limits.verifySliceSize` chunks and first dispatches the **artifact-writer** to persist each `${verify.inputPathBase}.slice{i}.json` (the workflow has no disk, so the writer materializes the slice inputs the script will read; the writer fans out in groups, and a group that throws / returns null / cannot prove its writes degrades only the slices THAT GROUP carried — never a fabricated verification). That write proof is a **liveness** check only: it confirms a path was echoed back, not that the bytes which landed there are the bytes dispatched. The **content** of what actually landed is no longer taken on faith either — see "The slice-input content proof" below. It then dispatches one `executor` agent per slice **sequentially** so each receipt pairs to its slice by order. Each executor runs exactly:

```
python3 {verify.scriptPath} --input {inputPathBase}.slice{i}.json --output {outputPathBase}.slice{i}.json \
  --nonce {nonce}.{i} --head-sha {headShaShort} --base-branch {baseBranch} [--diff-file {diffPath}]
```

and returns the script's discriminated-union envelope — but no longer by value (issue #25 req. 1). The executor echoes the receipt plus a per-finding **delta**, never the findings themselves:

```
{ status: 'ok',
  receipt: { sha, n_in, nonce, deltas_checksum },
  result: {
    deltas: [ { id, verified, origin?, severity?, confidence?, elimination_reason? }, ... ],
    verified, eliminated, batches, stats   // unchanged on disk; NEVER echoed by the executor
  }
} | { status: 'failed', exitCode, stderr }
```

`deltas` carries only what `verify_findings.py` changed on a finding, keyed by id and ordered by the dispatched slice — the workflow already holds every finding it dispatched by value, so the delta is the only new information the script can offer. `joinVerifyDeltas` rebuilds the slice's verified findings by walking the DISPATCHED slice (not the echo) and applying each finding's delta onto the copy this stage already holds; a finding whose delta says `verified: false` was eliminated by the script and is omitted, exactly as it was omitted from the old echo's `verified[]`. `joinVerifyDeltas` also strips `agent` from every finding it emits — see below.

**Trust.** A slice is trusted only when `status==='ok'` AND the receipt echoes the dispatched nonce (`{nonce}.{i}`), head sha, and `n_in` (slice length), AND the echoed `deltas` cover **exactly** the ids this slice dispatched — no missing id, no duplicate, no stranger (delta-id coverage; #25 req. 2) — AND each delta's shape and elimination stamp are honest (`verified` is a boolean; a `verified: false` delta must carry the script's `elimination_reason` stamp, a `verified: true` delta must not carry one) — AND a **content proof**: `fnv1a32` recomputed over the deltas, in the same canonical order the script used, must equal the receipt's `deltas_checksum`. The per-slice nonce (`{nonce}.{i}`) means two equal-length slices can never satisfy each other's receipts. Only once a receipt clears every one of those gates is it graded on a second axis — not what the script SAID, but what it READ. See "The slice-input content proof" below.

**Degradation is per slice (issues #54, #25 req. 3).** An untrusted slice — receipt mismatch, `status:'failed'`, an executor throw, or a slice whose `--input` file was never provably written by the artifact-writer — degrades only its OWN findings to `origin='unknown'` (surfaced-classification skipped) and the loop keeps going; trusted slices keep their verified output. This replaced an all-or-nothing `break`: measured live on 2026-07-27, a single dropped nonce echo on slice 0 marked all 16 findings of a one-slice PR `origin=unknown`. The blast radius now tracks the size of the fault.

Before degrading, a slice gets **exactly one retry** (`VERIFY_ATTEMPTS_PER_SLICE=2`) with a distinct nonce (`{nonce}.{i}.r1`, so a replay of attempt 1's receipt cannot satisfy attempt 2). A slice recovered on retry emits a disclosure gap but is not counted as degraded; a slice that fails both attempts degrades with both reasons chained. When the first attempt's failure is INPUT-implicated — the script's own reason code, or a receipt whose input content proof came back `mismatch` — the retry first re-materializes that one slice's `--input` file with a fresh artifact-writer dispatch (`writeSliceInputs`) before the second executor call, because re-dispatching the executor alone would read the identical bad bytes and fail the identical way. This costs no extra executor attempt (`VERIFY_ATTEMPTS_PER_SLICE` stays 2); it adds at most one writer dispatch, and only for a slice whose input was actually implicated — a non-input failure (a dropped nonce echo, a garbled delta) still takes the plain retry over the unchanged file. A writer-group failure while materializing slice inputs degrades only the slices that group carried — other groups' slices still reach the executor. `verified` is true only when zero slices degraded. Findings are never dropped and success is never faked *against the failure classes the stage detects* — every finding leaves the stage either as its slice's trusted verified output or as itself with `origin='unknown'`.

**The shape-vs-content gap is now closed (#25 req. 2).** The old design's trust guards bound only a slice's **shape** — nonce, sha, `n_in`, and a count sum (`verified.length + eliminated.length === n_in`) — never its **content**: an envelope carrying a same-length sibling slice's findings satisfied every guard, and the real slice's findings were dropped outright while the run reported `verified: true` (reproduced end to end during the #54 review; a count is trivially satisfied by any slice of the same size, since `verifySliceSize` is a constant and most slices in a run share a length). That gap predates per-slice degradation. It closes here for two reasons at once: first, the findings on the trusted path are now the ones THIS stage dispatched — `joinVerifyDeltas` walks the dispatched slice, so no echo, however wrong, can substitute or delete a finding, only fail to enrich it; second, the count guard became delta-id coverage (an id set cannot be satisfied by a same-length stranger) plus the content proof above (which catches coherent VALUE drift — one flipped `origin`, one shifted `confidence` — that id coverage alone would miss). As with every trust check in this pipeline, this is a **consistency check against a stale, drifting, or confused executor, not authentication**: the checksum travels in the same envelope as the data it covers, so nothing here defends against a Byzantine executor that recomputes both — it defends against the LLM-transcription failure this boundary keeps observing in practice (the by-value writer's transcription of `findings.json` diverged on 3 of 3 measured runs; this boundary's own live incident turned a 10-verified/0-eliminated disk result into a 7/3 echo under a receipt that passed every shape guard).

**The slice-input content proof closes a second gap (issue #25 PR3): between what the writer echoes and what the script actually reads.** The shape-vs-content proof above covers the delta ECHO — what the script said. It says nothing about the `--input` file the script READ, and that file has a measured failure mode of its own: of every verify slice-input file this repo has retained, 4 of 31 (12.9%) are unparseable, and every one is a complete document with bytes appended after its final byte (`}` × 3, `</content></invoke>` × 1 — the artifact-writer transcription defect of issue #69; `bench/MEASUREMENT.md` records "2 of 3 PRs — 23 findings lost"). The retry that existed before this landed could not recover any of them, because it re-dispatches only the executor, never the writer — `wf_3f640577-31c` failed both of its attempts at the identical byte offset, reading the identical bad bytes twice.

`verify_findings.py` now parses leniently in receipt mode: it accepts the leading complete JSON value and reports how many bytes followed it (`receipt.input_trailing_bytes`), then computes `receipt.input_checksum` — the same `fnv1a32`/canonical-pretty-print pair used everywhere else in the pipeline — over the document AS PARSED, before numeric coercion runs. The workflow computes its own expectation for what that checksum should be (`sliceInputProofFor`, over the identical construction site that wrote the file), and only once a receipt has already cleared `trustSlice` is that expectation compared against what the script reported (`gradeInputProof`). This is a **canonical-VALUE** proof, not a raw-bytes one — deliberately: slice inputs are handed to the artifact-writer as an object to serialize, so the bytes on disk are the writer's choice and only the parsed value is the contract (findings.json, by contrast, is handed over as a pre-serialized string and is proven byte-for-byte). Measured over all 27 parseable slice-input files this repo has retained, the two runtimes' canonical forms agree on every one.

Three outcomes follow. **Detection**: a file that parses cleanly but drifted in VALUE — right ids, right count, wrong text — used to pass every existing guard silently; it now mismatches and is caught. **Recovery**: the measured trailing-bytes corruption is accepted, but only once its content proof confirms the recovered leading document really is the one the pipeline dispatched — recovery is never silent, and the run discloses it either way. **Degradation**: an input-implicated failure spends one re-materialize dispatch before the existing retry (see "Degradation is per slice" above); if the content proof still does not match afterward, the slice degrades to `origin='unknown'` rather than being delivered with verdicts computed from a file that was provably not the one dispatched. A receipt with no `input_checksum` at all — an older `verify_findings.py`, or an executor that dropped the field — leaves the slice merely UNPROVEN, disclosed in a single aggregated gap and never fatal, which is the same state every run was in before this proof existed.

**`agent` is stripped at the join, on purpose (issue #22 sequencing).** `joinVerifyDeltas` deletes `agent` from every finding it emits. It used to be withheld by omission — the by-value echo schema simply did not declare `agent`, so `StructuredOutput` dropped it, but only most of the time (observed surviving on 2 of 6 PRs). Joining a delta onto findings this stage already holds by value would have made that survival deterministic instead of flaky, and deterministic `agent` past Verify is the measured dedup recall-collapse mechanism (mini-subset A: dedup eliminations 7→33, same-6 recall 20/30→13/30) — so the withholding had to become an explicit `delete` at the join rather than an accidental schema gap. It re-lands only with the cross-dimension consolidation redesign, issue #22.

**Classification rules** (applied by `verify_findings.py` on the trusted path):

- Findings with `cross_file_refs` are always `surfaced`.
- Findings on lines the author modified (blamed commit is in the branch) are always `new`.
- A finding spanning both new and old lines classifies as `new`.
- `surfaced` findings are downgraded one severity level and grouped into their own report section; `blame_metadata` records original severity/author/date.
- Factual verification: if any claim is wrong (line number, symbol existence, code mismatch), confidence is set to 0.

---

# Validate stage (`validateStage`)

Independent re-scoring by fresh Sonnet agents — **always Sonnet**. Discovery and validation sharing the same context produces correlated errors ~60% of the time; fresh agents assessing findings independently is what breaks that.

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

Blind, independent scrutiny — the only stage where findings face agents that have never seen the original reasoning. Fresh challengers see **only** `blindChallengeFields`: `title`, `description`, and raw `code` (an allowlist, not a delete-list — no evidence, origin, cross_file_refs, or reasoning can ever leak). Always Sonnet.

The stage ranks findings, blind-challenges the top `min(n, limits.challengeCap)` through `parallel()`, and applies the score thresholds. Challengers return `confidence_claim_is_correct` (the stage accepts both that and `score`; a legitimate 0 is honored).

**Thresholds** (`applyChallenges`):

- **< 25** → remove (non-security) / downgrade one level (security)
- **25–49** → downgrade one level; surfaced re-routed to `"suggestion"`
- **50–74** → `challenge_contested: true`; surfaced re-routed to `"suggestion"`
- **≥ 75** → survives unchanged

After thresholds it re-runs cross-agent dedup and ranks; that ranked set is the **high-confidence bucket**. Every UNCHALLENGED finding — challengeCap overflow OR a null/unscored member — is marked `challenge='skipped'` and routed to the **unverified** bucket (pipeline-degraded); it never enters the high-confidence set. Both buckets flow to the Report stage.

---

# Report stage (`reportStage`)

Dispatches the `report-writer` agent to render markdown from the high-confidence + unverified buckets (carried by value — the workflow has no disk). Oversized finding payloads are segmented into per-chunk dispatches joined under titled headings. On a throw OR null result, a deterministic **minimal report** is assembled from the pipeline stats and a gap is recorded — report failure is non-fatal. The `artifact-writer` then persists findings.json + report.md + the pre-selected post-review payload + the checkpoint artifact to `{output_dir}` (SKILL.md's artifact-writer output list has the exact filenames, including the optional derived-path `persist-plan-*.json`).

**The degradation banner (issue #25 reqs 7-9).** The report-writer never characterises the review's own health — it is handed findings and challenge outcomes, never the discover/verify outcomes, so any health sentence it composed would be invented. That is exactly what happened twice on the record: a report stated "0 unverified/pipeline-degraded findings" while every delivered finding carried `origin='unknown'`. Instead `reviewHealth` computes the review's health deterministically in code, from the DELIVERED findings' own `origin` values — `'new'`/`'surfaced'` means verify classified it; anything else (`'unknown'`, or the field simply absent — `origin` is not in `VERIFY_SCHEMA`'s delta `required` list) means it did not — plus, only when this run's stage-level signals actually describe the delivered set, the verify slice-degradation counters and which review dimensions produced zero results. `healthBanner` renders that into a GitHub-alert-syntax block that `applyHealthBanner` prepends to the report (nothing is prepended when the review is healthy).

The banner is applied in `runWith`, immediately before `writeArtifacts` — not inside `reportStage`. That is the one point every report body passes through: `reportStage` has three producers of its own (single dispatch, segmented join, minimal fallback), but a **resume** replays a previous run's report object straight from the checkpoint and never calls `reportStage` at all. Banding inside the stage would leave exactly the runs most likely to be degraded unbannered.

`applyHealthBanner` is **idempotent**: it strips any existing sentinel-delimited block (`<!-- code-gauntlet:health:begin/end -->`) before prepending a freshly computed one. A resumed run's replayed report may already carry a previous run's banner, and a stale banner is worse than none — it would describe a health state this run never measured — so it is always replaced, never stacked.

**Resume behaviour.** `PERSISTED_RESUME_PHASES` is `['challenge']`, so a resume re-runs discover/verify/filter over a freshly rediscovered population that it then discards, delivering the replayed challenge output instead. Quoting the fresh recomputation's stage-level health over the replayed findings would print one population's health above another's results, so on that path `reviewHealth` is called with `evidenceIsFresh: false`: it uses only the delivered findings' own (replayed) `origin` values, and withholds the verify-stage counters and dimension-loss list rather than borrowing them from the orphaned recomputation.

Unproven slice inputs (see "The slice-input content proof" above) deliberately do **not**, by themselves, raise the banner — PR3 established unproven as the pre-existing baseline, not a degradation, and a banner that fires on ordinary runs trains readers to ignore it. They are still named inside the banner once it is already firing for another reason.

---

# Operational Recovery

The workflow degrades internally and returns `gaps` rather than throwing, so most recovery is reading gaps and disclosing them. On the failure paths, nothing is persisted, so the resume state rides back **in the return's `checkpoints` field** (not on disk). Two cases need the skill:

1. **Hard failure (`ok:false`).** An unexpected throw in the deterministic glue returns `{ ok:false, error, phaseReached, checkpoints, ... }` with `artifactPaths` empty. Offer **resume-from-checkpoint**: if `return.checkpoints` has a `.phases` map, re-invoke the same `Workflow` call with `args.checkpoints` set to `return.checkpoints` (the workflow unwraps `.phases` and resumes at the first missing phase). If it is `{ completed, truncated: true }` (phase-outputs map over the ~1M-char return budget — the findings bulk is deliberately withheld from the return), re-run from scratch without `args.checkpoints`.
2. **Partial artifacts (`ok:true`, writer failed).** A failed artifact-writer empties `artifactPaths` and records a partial-artifacts gap; the return's `checkpoints` carries the same `{ phases, completed }` (or truncated) resume state. Retry via resume-from-checkpoint as above; if it fails again, deliver whatever report exists via chat and disclose the gap.

On `ok:true` **success**, resume state is NOT in the return (`checkpoints` is just `{ completed }` names) — the full `{ phases, completed, phaseReached }` map is on disk at `artifactPaths.checkpoints` for a later re-run.

**Never reproduce a failed stage inline in the main session.** The stages exist as isolated fresh-agent dispatches precisely because LLM-inline re-analysis carries ~60% correlated error. A skipped stage with a methodology note beats fabricated results.
