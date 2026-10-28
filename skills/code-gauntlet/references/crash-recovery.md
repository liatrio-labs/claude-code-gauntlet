# Crash recovery — resume-first playbook

When a workflow dies mid-pipeline, recover in this order. Keep the original args and
checkpoint source so recovery can reuse completed work without reconstructing findings.

## 1. `resumeFromRunId` first

Every Workflow invocation returns a `runId` (also visible in `/workflows`). Re-launch the
same registered workflow with the same args plus `resumeFromRunId`:

```
Workflow({ name: "code-gauntlet:code-gauntlet-pipeline", args: <the identical args waist>, resumeFromRunId: "wf_..." })
```

- Completed `agent()` calls with unchanged (prompt, opts) return their cached results
  instantly without re-billing.
- Only the failed call and everything after it runs live.
- The args waist must be **byte-identical** (same `generatedAt`, same `nonce`, same
  everything) or the changed-prefix rule re-runs stages you already paid for. Reuse the
  exact args object from the failed invocation — never re-stamp the clock.
- Stop the prior run first (`TaskStop`) if it is still listed as running.

## 2. Diagnose from the journal + envelope, not the narrative

- The error envelope names the crash site: **`failingPhase`** is the stage that actually
  threw; `phaseReached` is the last stage that completed.
  `failingPhase` also carries two pre-dispatch pseudo-phases that never named a real
  stage: `'args'` (the waist itself failed validation) and `'checkpoints'` (a
  `checkpoint-shape:` or `checkpoint-checksum:` refusal before dispatch). Neither has
  cached work to replay; follow the checkpoint refusal rules below.
- The workflow journal (`<transcriptDir>/journal.jsonl`) records each agent's actual
  return value — read it before assuming what any stage produced.
- `.code-gauntlet/` artifacts (context, diff, checkpoint files) show what was persisted
  before the crash.

## 3. Checkpoint resume (fallback when `resumeFromRunId` is unavailable)

When resume by id is unavailable, copy the checkpoint object **whole, `checksum` included**
from `return.checkpoints` on failure or partial persistence, or from the file at
`artifactPaths.checkpoints` after successful persistence, into `args.checkpoints`.
The slim disk checkpoint retains challenge output and per-phase counts; upstream phases run again.
Never edit, summarize, unwrap, rebuild, or re-sign the checkpoint. The producer's checksum covers
`JSON.stringify` of the whole object except `checksum`, with two-space indentation,
native key order and UTF-16 hashing. This detects value drift, not authentication or
changes to the disk JSON's presentation whitespace. Fractional numbers remain valid.

- A signed `.phases` map replays completed outputs and dispatches missing phases.
- After checksum and shape checks, missing or incoherent location status becomes `unconfirmed`.
  Verify and later replay clears canonical addresses and preserves original claims as diagnostics.
  Late replay does not resolve locations again; discover/merge numbers remain requests for Verify.
- `{ completed }`, `{ completed, truncated: true }`, and empty maps contain no replay
  state. Interactive mode can offer a fresh run without `args.checkpoints`; headless
  delivers the partial report plus gaps and stops.
- On `checkpoint-checksum:`, re-copy once from the **original source**, unchanged.
  A second refusal offers a fresh run interactively. Headless delivers the partial
  report plus gaps and stops; never start a fresh paid review.
- On `checkpoint-shape:`, read the cited phase/field. Do not edit signed checkpoints;
  offer a fresh run interactively, or deliver partial results and gaps headless.
- On other failures or partial artifacts, offer resume interactively. Headless has a
  **single retry budget**: auto-resume once when a signed phase map is available, then
  deliver the partial report plus gaps and stop. A checksum re-copy dispatches nothing,
  so it does not spend that budget.
- If resume is declined or fails again, deliver any existing report markdown-only
  (path plus a short chat summary) and disclose all gaps. Partial persistence can
  leave proved findings/report primaries even when derived artifact paths are null; deliver
  them markdown-only and keep the gap. PR comments come only from `artifactPaths.postReview`,
  never from a salvaged `findings.json`.

## Rules

- Never re-dispatch discovery/verify stages by hand to "reconstruct" lost output — the
  cache replay or checkpoint path is always cheaper and provably consistent.
- Record the recovery in the report methodology: the failing phase, the recovery path
  used, and whether any stage re-ran live.
