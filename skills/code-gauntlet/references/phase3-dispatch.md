# Phase 3 Dispatch Reference

## Invocation

```
Workflow(
  name: "code-gauntlet:code-gauntlet-pipeline",
  args: { ...the args object assembled in Phase 2... }
)
```

Invoke by registered name, with the assembled args object. Never pass a cache `scriptPath`,
copy the bundle, split the stages, or reproduce them inline. Validation rejects malformed args
before dispatch. Stage defaults and resolved overrides are in [model-policy.md](model-policy.md).

The compact return is
`{ ok, phaseReached, stats, artifactPaths: { findings, report, postReview, checkpoints }, checkpoints, resolvedPolicy, gaps, persistReturn }`.
On RETURN, paths are intended destinations until Phase 8 materializes the primaries;
`persistReturn` carries `channel`, `nonce`, `planPath`, and `entries`. The awaiter elides entries
into `paths` and `resolvedPath`; never transcribe them. Writer paths persist before returning.

## Wait protocol

Hold the turn and observe the result with the awaiter. Completion notifications arrive between
turns; a `claude -p` run that yields can terminate background tasks at
`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (default 600000 ms). Use the same protocol interactively.
An inline compact return needs no awaiter. Otherwise run with Bash `timeout: 600000`:

```
{python} "{plugin_root}/scripts/await_workflow.py" --python "{python}" --attempt 1
        --artifacts-dir "{output_dir}" --head-sha {head_sha_short} -- "<task-id>"
```

| Exit | Action |
| --- | --- |
| 0 | Terminal compact return on stdout; carry it to Phase 8. |
| 3 | Run stdout's `next_command` verbatim with `timeout: 600000`; do not end the turn. |
| 5 | Artifacts landed without an observed return; declare `workflow-timeout` using marker `detail` and collect marker `artifactPaths`. |
| 4 | Attempts exhausted or awaiter failed; declare `workflow-timeout` and enter Phase 8 salvage. On RETURN try materialization with `--nonce {nonce}`. |
| 2 | Malformed command, empty stdout; fix from stderr and retry. |

Only a compact return or terminal failure receipt permits Phase 8. Missing output never means
an empty successful review. RETURN cannot produce exit 5 because its files await materialization.
Do not claim an `ok`, count, or gap you have not read.

## Diagnostics and recovery

Partial coverage records gaps; total discovery loss with no findings or challenge replay returns
`ok:false` with `all-degraded:` and means no review occurred. Surface per-agent gaps and
`resolvedPolicy`. Use [crash-recovery.md](crash-recovery.md) for resume, checksum refusals,
and retry limits. [workflow-diagnostics.md](workflow-diagnostics.md) describes internal stages,
read-plan safeguards, executor proofs, and writer fallbacks; read it when diagnosing those paths.

## Permission troubleshooting

Live permission-mode behavior is unverified. Under `acceptEdits`, a dynamic-workflow review gate
or the executor's `verify_findings.py` Bash command may wait for approval the workflow cannot
surface. Check the gate and command permissions before retrying a stalled run. Do not infer
completion from a permission stall or bypass the invocation and recovery contracts.
