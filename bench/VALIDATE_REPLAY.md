# Validate replay

Replay frozen post-verify findings against a treatment checkout without discovery or judge
calls. The source must be a completed score with bound stage traces and original attempt
artifacts. Local mirrors must contain the recorded head commits. Replay never fetches or
uses the old absolute scratch paths.

```bash
python bench/runner/validate_replay.py \
  --source-run /path/to/scored/run \
  --score-id SCORE_ID \
  --treatment-tree /path/to/treatment/checkout \
  --mirrors-dir /path/to/bench/workspace/mirrors \
  --replays 3 --out /tmp/validator-treatment \
  --child-auth api --child-model sonnet --child-effort low \
  --dry-run
```

Remove `--dry-run` and use a fresh output directory to launch validators. Auth mode is
required (`api` or `subscription`); the existing runner's isolated home and auth selection
apply. `--pr URL` selects one scored PR and can be repeated. Child session model and effort
default to `sonnet` and `low`; agent models come from the treatment's policy. The child
needs a Claude Code version supporting Workflow and `--effort`.

The bench reconstructs source verification using the source Workflow's saved bundle and
phase-4 deltas, checks its projection against the scored verify snapshot, and preserves
recorded removed-test audit overlap decisions. Full held findings supplement fields omitted
from the trace. Missing or changed evidence fails the replay. Context, diff, changed-file
lists and collected input artifacts are relocated together. A local shared clone owns the
detached review worktree, leaving cached mirrors untouched.

Node calls the treatment's `validateStage` to build each dispatch round. Each small Workflow
script embeds literal prompts and options and calls `agent()` through `parallel()`. Node
resumes the same stage with the recorded returns to discover any required retry round.
This keeps adaptive retries authoritative without embedding the product's dependency graph
and duplicate finding data past the Workflow script-size cap. Each round uses a fresh
headless child with the treatment loaded through `--plugin-dir`. Dry runs write the first
round's scripts and commands; retry scripts depend on live returns.

Results come only from completed `wf_*.json` records bound to the generated script path and
bytes. No session model transcribes findings or validator JSON. The treatment stage applies
accepted validations, then its admission and filter functions run with the source settings.
Known-bug matches join the score's verify-version attribution by unchanged finding ID.
Unmatched passing findings are counted without judging their validity.

Outputs include:

- `replay.json`: source, treatment hashes, replay count, auth, child model and effort.
- `replay-NNN/PR/`: relocated context, input, source hashes and bug IDs, scripts, child commands,
  raw child output, Workflow records, full dispatch returns, and `outcomes.json` with every
  finding's validator fields, final filter record, pass/fail and reason, counts and bug hits.
- `summary.json`: counts by PR and replay, plus totals for each replay.
- `spread.json`: per-finding confidence samples, range, pass counts, and adjacent replay flips.

`passes_floor` probes the treatment's confidence threshold with the highest severity and
without the contestation bypass. `passes_filter` measures the complete filter, including
severity, exclusions, injection, suppression and contestation. A low confidence can fail
the floor while the product keeps a contested finding. Flip counts compare consecutive
replays in replay-number order; `crosses_floor` records whether both sides were observed.
Completed outcomes remain on disk if a later PR or round fails. Rerun into a new directory;
this command does not resume or overwrite an earlier replay.

The small Practice fixture under `bench/tests/fixtures/validate_replay.json` contains four
findings, recorded validator returns and filter outcomes from one PR, with no golden prose.
`python -m pytest bench/tests/test_validate_replay.py -q` checks prompt parity against direct
product-stage calls, recorded filter parity, retries, treatment ownership and the CLI boundary.
