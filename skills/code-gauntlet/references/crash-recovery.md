# Crash recovery — resume-first playbook

When a code-gauntlet Workflow run dies mid-pipeline (a stage throws, the background task
is killed, the session loses the compact return), recover in this order. Do **not** start
by hand-marshalling checkpoint JSON into a fresh invocation — the live PR-310 recovery
spent a 73KB checkpoint-assembly detour before pivoting to the path below, which then
replayed all 11 completed agents from cache with **zero re-billed tokens**.

## 1. `resumeFromRunId` first

Every Workflow invocation returns a `runId` (also visible in `/workflows`). Re-launch the
SAME script with the SAME args plus `resumeFromRunId`:

```
Workflow({ scriptPath: "<plugin_root>/workflows/pipeline.js", args: <the identical args waist>, resumeFromRunId: "wf_..." })
```

- Completed `agent()` calls with unchanged (prompt, opts) return their cached results
  instantly — **no re-billing** (verified live: journal `wf_cb410bd2-208` replayed 11/11
  agents from cache).
- Only the failed call and everything after it runs live.
- The args waist must be **byte-identical** (same `generatedAt`, same `nonce`, same
  everything) or the changed-prefix rule re-runs stages you already paid for. Reuse the
  exact args object from the failed invocation — never re-stamp the clock.
- Stop the prior run first (`TaskStop`) if it is still listed as running.

## 2. Diagnose from the journal + envelope, not the narrative

- The error envelope names the crash site: **`failingPhase`** is the stage that actually
  threw; `phaseReached` is the last stage that *completed* (narrating from `phaseReached`
  misattributes the crash — the live Filter crash read as "failed during Validate").
- The workflow journal (`<transcriptDir>/journal.jsonl`) records each agent's actual
  return value — read it before assuming what any stage produced.
- `.code-gauntlet/` artifacts (context, diff, checkpoint files) show what was persisted
  before the crash.

## 3. Checkpoint resume (fallback when `resumeFromRunId` is unavailable)

Only when the run cannot be resumed by id (e.g. a new session without the prior runId,
or the runtime rejected the resume): use the compact return's `checkpoints` per SKILL.md
Phase 3 — a `.phases` map re-enters via `args.checkpoints` (the workflow skips completed
phases); a `{ completed, truncated: true }` return means nothing resumable came back —
re-run from scratch and note it in the methodology.

## Rules

- Never re-dispatch discovery/verify stages by hand to "reconstruct" lost output — the
  cache replay or checkpoint path is always cheaper and provably consistent.
- Never edit the args between crash and resume (new nonce/timestamp = cache miss).
- Record the recovery in the report methodology: the failing phase, the recovery path
  used, and whether any stage re-ran live.
