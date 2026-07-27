# Profiling a recorded workflow run

`bench/profile_run.py` turns a single recorded `code-gauntlet` workflow run into a
timing/cost profile: per-stage wall-clock spans, a per-agent table, parallel-capacity
accounting for the fan-out stages, the critical path through the stage graph,
model-generation vs tool-execution time, output-byte accounting for the Write-shaped
agents, and orchestrator phase spans (including Phase 2's sequential Bash-call
latency). It is read-only and does not invoke the pipeline — it only inspects
transcripts that already exist on disk from a prior run.

This is the tool that produced the N=1 profile issue #38 cites. Its output (the
`--out-json`/`--out-md` files) is not committed to the repo — profiles are run
outputs, generated on demand from whatever recorded runs exist locally under
`~/.claude/projects/`. It is meant to be re-run against any future recorded run,
not just the one issue #38 cites.

## What it reads

Three sources, all under `~/.claude/projects/<project-dir>/<sessionId>/`:

1. **The workflow record** — `workflows/wf_<runId>.json`. Its `workflowProgress[]`
   array is the per-agent summary: `label`, `agentType`, `model`, `state`, `attempt`,
   `queuedAt`, `startedAt`, `durationMs`, `tokens`, `toolCalls`. The record's own
   `agentCount` / `totalTokens` / `totalToolCalls` / `durationMs` are the ground truth
   the profiler reconciles its own sums against.
2. **Per-agent subagent transcripts** — `subagents/workflows/<runId>/agent-<agentId>.jsonl`.
   One JSON object per line; `tool_use` blocks (with an `id`) are matched to their
   `tool_result` counterpart (by `tool_use_id`, found on a `user`-typed row) to split
   each agent's wall-clock span into tool-execution time vs everything else
   (model-generation time).
3. **The orchestrator session transcript** — `<projectDir>/<sessionId>.jsonl`, the
   sibling file of the `<sessionId>/` folder itself. Same `tool_use`/`tool_result`
   shape, at the top-level (non-sidechain) session. Used to derive Phase 1 / Phase 2 /
   Phase 3-wait / Phase 8 spans and Phase 2's Bash-call latency breakdown.

## Running it

```bash
# Profile a specific run:
python3 bench/profile_run.py wf_cef39739-577 \
  --out-json /tmp/profile.json --out-md /tmp/profile.md

# Profile the most recently completed code-gauntlet run found under ~/.claude/projects:
python3 bench/profile_run.py --out-json /tmp/profile.json --out-md /tmp/profile.md

# Point at a non-default projects root (e.g. a copied-out fixture):
python3 bench/profile_run.py wf_x --projects-dir /path/to/projects --out-md /tmp/profile.md
```

With neither `--out-json` nor `--out-md`, both are printed to stdout. `RUN_ID` is the
`wf_...` filename stem (no `.json`).

## How to read the output

- **Reconciliation** — checked first, on purpose. If `agentCount` / `totalTokens` /
  `totalToolCalls` don't match the record's own headline numbers, something about the
  run's shape has drifted and the rest of the report should be treated with
  suspicion. `durationMs` is not independently re-derived (it defines the workflow
  wall clock everything else is measured against).
- **Stage profile** — stages are derived by grouping `workflowProgress[]` labels:
  `summarize`, `discover` (the 7 discovery agentTypes, grouped together), the pure-JS
  `merge`/`filter` transform phases (no agent — see `workflows/src/stages.js`
  `runPhase('merge', ...)` / `runPhase('filter', ...)`; only observable as the gap
  between the stages either side), `verify-input-writer-*`, `verify-slice-*`,
  `validate-batch-*`, `challenge-*`, `report-writer`, `artifact-writer`. `avg
  concurrency` = agent-seconds used / stage span (how "full" the stage was on
  average); `max concurrency` is the actual peak overlap from a sweep-line over each
  agent's `[startedAt, startedAt+durationMs)` interval.
- **Parallel-capacity accounting** — for each stage, `capacity = slowest-agent duration
  × slots` (slots = however many agents were actually dispatched in that stage), `used
  = Σ durationMs`, `idle = capacity − used`. High idle % on a fan-out stage means the
  stage's wall-clock is being set by one slow outlier while the rest of its agents
  finish early and sit idle.
- **Critical path** — walks the (mostly linear) stage graph in pipeline order; within
  a fan-out stage the "critical" member is whichever agent finishes last, since that's
  what gates the next stage's dispatch. Each hop reports `gap_before` (time between the
  previous hop's completion and this one's `queuedAt` — dispatch overhead plus, for the
  merge/filter hops, the transform phase itself), `dispatch_latency`
  (`startedAt − queuedAt`), and `compute` (`durationMs`). The totals let you see the
  split of total wall clock into compute vs dispatch-overhead vs orchestration-gap.
- **Model-generation vs tool-execution time** — per the transcript matching described
  above: `tool_time = Σ(tool_result.ts − tool_use.ts)` over matched pairs;
  `generation_time = transcript_span − tool_time`. Unmatched `tool_use` (no
  `tool_result` found — e.g. a final `StructuredOutput` call with no reply captured
  before the agent terminates) and unmatched `tool_result` counts are reported per
  agent so you can see when the split is on shakier ground.
- **Output-byte accounting** — for `artifact-writer` and `verify-input-writer` labels
  specifically (identified by `agentType == code-gauntlet:artifact-writer`), every
  `Write` tool call's `content` byte length (UTF-8) and how long that specific call
  took.
- **Orchestrator phase spans** — Phase 1 ends at the first `AskUserQuestion` tool call
  (the REVIEW.md/delivery-target gate); the time the human takes to answer it is
  reported separately from Phase 1 itself. Phase 2 ends at the `Workflow` tool call
  whose result reports `Task ID: <the record's own taskId>` — a session can contain
  more than one `Workflow` tool_use (e.g. a relaunch), so the profiler picks the one
  that actually produced the run being profiled and calls out when there was more than
  one. Phase 3's wait is measured from the workflow's own recorded `startTime` to the
  first orchestrator-session event at or after the workflow's recorded completion
  (`startTime + durationMs`); the gap between those two is reported as
  "dispatch-to-resume latency" (harness/notification overhead, not pipeline time).
  Phase 8 runs from that resume point to the last event in the session transcript —
  it is not decomposed into human-wait-on-AskUserQuestion vs compute, so a
  question-heavy delivery phase will show as one large number.
- **Phase 2 Bash-call breakdown** — every `Bash` tool call between the end of the
  Phase 1 human-wait and the matched `Workflow` launch, with `model_latency` (time
  since the previous call's result, i.e. how long the orchestrator took to decide on
  this command) and `shell_time` (the command's own execution time).

## What it honestly can't derive

The profiler prints an explicit `UNAVAILABLE: <reason>` string (never a guessed
number) wherever the source data doesn't support a metric — e.g. a missing subagent
transcript directory, a stage with zero agents dispatched in this particular run, an
unmatched `AskUserQuestion` reply, or a session transcript that doesn't contain an
event at/after the workflow's recorded completion (so Phase 3/8 can't be split).
`merge`/`filter` transform-phase time is *only* ever the gap between the stages either
side of it — the profiler cannot see inside the sandbox to separate transform-phase
compute from orchestration overhead within that gap, and says so in the stage's note.

## Tests and fixtures

`bench/tests/test_profile_run.py` builds a small synthetic run (record JSON +
per-agent transcripts + orchestrator session) under a `TemporaryDirectory` — it does
not depend on any real transcript data, which lives outside this repo under
`~/.claude/projects/`. Run it with the rest of the bench suite:

```bash
python3 -m pytest bench/tests/ -q
```
