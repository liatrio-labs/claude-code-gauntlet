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
accepted validations, then its admission and filter functions run. Retired source settings have no interpreter in this treatment.
Known-bug matches join the score's verify-version attribution by unchanged finding ID.
Unmatched passing findings are counted without judging their validity.

Outputs include:

- `replay.json`: source, treatment hashes, replay count, auth, child model and effort.
- `replay-NNN/PR/`: relocated context, input, source hashes and bug IDs, scripts, child commands,
  raw child output, Workflow records, full dispatch returns, and `outcomes.json` with every
  finding's validator fields, final filter record, pass/fail and reason, counts and bug hits.
- `summary.json`: projected inline counts (`passing`, `known_bugs_passing`, `unmatched_passing`) by PR and replay; `filter_survivors` and `filtered_out` count filter survival and elimination separately.
- `spread.json`: per-finding typed verdicts, routes, pass counts, and adjacent replay flips.

`passes_filter` records survival of the filter. `posts_inline` records projected inline eligibility. `spread.json`
contains verdicts, routes, filter pass counts and adjacent-pass route changes.
Completed outcomes remain on disk if a later PR or round fails. Rerun into a new directory;
this command does not resume or overwrite an earlier replay.

The small Practice fixture under `bench/tests/fixtures/validate_replay.json` contains four
findings, recorded validator returns and filter outcomes from one PR, with no golden prose.
`python -m pytest bench/tests/test_validate_replay.py -q` checks prompt parity against direct
product-stage calls, recorded filter parity, retries, treatment ownership and the CLI boundary.

## Typed-verdict treatment

This treatment records `verdict` (the validator's closed answers), `route`, `refutation_check`,
`rank_index`, and `changed_lines` in each outcome. Refutations add a deterministic citation-check
round through the existing executor; the child therefore permits Bash for that pinned command.
Validators retain their read-only tools. Replay does not run blind challenge: `not-established`
means sent to the existing next check, not eliminated or posted. `posted`, `posted-latent`, and `posted-artifact`
measure eligibility after validation and filtering, not actual delivery. Each outcome also has
`disposition` and `posts_inline`, projected through the live disposition function assuming the
blind challenger agrees. `passes_filter` records filter survival, including findings routed to the body or local report.
`posts_inline` records projected inline placement, excluding body and local-only findings. The projection does not predict a future challenger answer, grouping or delivery caps. Report-only and unresolved findings remain in the full results.

The analysis writes JSON tables per pass, per PR, in total, and averaged across passes. A known
bug can occur on several routes, so route counts are not a partition of the 61 bugs. Places are
fixed clusters of consecutive starts within five lines in one file, using canonical lines or
claimed lines when unconfirmed. The cluster is marked matching if any member matches a known
bug. Unmatched places are not presumed false. The same place may occur on more than one route.

Both volume policies are reported: `all` keeps every posted route; `bounded` allows
`2 + ceil(sqrt(changed_lines / 100))` low-severity or latent findings per PR, sharing one budget
and using product `rank_index`. Excess findings become report-only for that policy. This counts
candidate comments before delivery grouping, not a prediction of the grouper's eventual comments.
The `all` policy is selected; `bounded` remains a diagnostic and is not applied by the pipeline.

Confusion-table rows are validator answers and columns are independent-reader answers. Worth
maps present failure, latent, and artifact defect to yes, no practical effect to no, independently of claim truth.
Reader coverage is partial; `reader_matches` states the joined denominator. Refuted and not-established claims have no worth answer (`not_applicable`). Baseline numeric
scores are never imputed as typed answers (`no_typed_verdict`). Baseline routes use the stored
`passes_floor` observations; they do not reconstruct historical full delivery. Adjacent-pass
route changes and findings that vary at all are reported separately for each arm and policy.

With `SOURCE_RUN`, `SCORE_ID`, and `MIRRORS_DIR` set to the driving session's scored input and
local mirrors, and `PACK` containing the baseline passes and independent reader JSON, run these commands from the treatment tree. Use fresh output directories:

```bash
python bench/runner/validate_replay.py \
  --source-run "$SOURCE_RUN" --score-id "$SCORE_ID" \
  --treatment-tree "$PWD" --mirrors-dir "$MIRRORS_DIR" \
  --replays 3 --out /tmp/verdict-followup-dry-run \
  --child-auth subscription --child-model sonnet --child-effort low --dry-run

python bench/runner/validate_replay.py \
  --source-run "$SOURCE_RUN" --score-id "$SCORE_ID" \
  --treatment-tree "$PWD" --mirrors-dir "$MIRRORS_DIR" \
  --replays 3 --out /tmp/verdict-followup-live \
  --child-auth subscription --child-model sonnet --child-effort low

PACK=/path/to/baseline-evidence-pack
python bench/analyze_verdicts.py \
  --replay-dir /tmp/verdict-followup-live --second-reader "$PACK/second_reader.json" \
  --baseline-pack "$PACK" --out /tmp/verdict-followup-analysis.json
```

`--replays 3` makes the three independent live passes in one command. Use `--child-auth api`
if that is the driving session's intended authentication mode. Analysis rejects dry-run outputs,
incomplete pass populations, changed finding IDs, duplicate IDs, and a denominator other than
61 unless `--expected-known-bugs` explicitly supplies another fixture population.

`artifact_defect` distinguishes verifiable defects in tests, standalone documentation and
unreachable code from preferences. Its `posted-artifact` route is low severity and preserves
finding-kind admission gates. `comment_defect` covers inaccurate or unclear code comments and
docstrings where code and tests behave correctly; it is report-only and still challenged. Outcomes whose kind admission does not allow posting count as
`report-only`. Per-route reader match counts, claim agreements, and worth yes/no counts accompany
the known-bug and place counts in every pass and average.

The analysis reports `inline_posting` bugs and places separately from route counts for each PR,
pass, average and volume policy. Older observations without `posts_inline` are explicitly
unmeasured; a posting route alone never supplies an inline count. The bounded policy remains a
diagnostic, rather than the live delivery policy.

Workflow measurement transport may use `encoding: "json-pool-lzw16-base64-v1"` to preserve full history
within the shared return budget. Collection and delivery replay expand it to ordinary trace
JSON before hashing and binding. Uncompressed captures remain supported. If selection evidence
is unavailable, `stage_trace_bindings.json` records `delivery_unavailable_reason` alongside the
empty delivery binding.
