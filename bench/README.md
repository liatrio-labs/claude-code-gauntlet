# bench — code-gauntlet benchmark harness

`bench/` drives the code-gauntlet skill (formerly deep-review) headlessly against a curated set of
golden PRs from [withmartian/code-review-benchmark](https://github.com/withmartian/code-review-benchmark)
(MIT — see [`vendor/VENDORED.md`](vendor/VENDORED.md)), then scores the
results in three buckets against a pinned LLM judge. It exists to give every
change to the skill a repeatable, quantified answer to "did this help or
hurt," instead of vibes.

**Measurement policy:** paired bench runs are *not* the default gate for every
change. See the canonical runbook [`MEASUREMENT.md`](MEASUREMENT.md) for the
ratcheted four-tier ladder (always-on suites → functional smoke → owner-triggered
mini-subset → owner-triggered full-15/holdout), ledger costs, and pre-registered
owner options.

## Quickstart

```bash
git clone https://github.com/liatrio-labs/claude-code-gauntlet.git
cd claude-code-gauntlet
cp bench/.env.example bench/.env
# edit bench/.env and set ANTHROPIC_API_KEY=sk-ant-...
python3 bench/run.py --tier smoke
```

That runs the skill against 3 small golden PRs end-to-end (zero prompts —
the skill runs in headless mode) and checkpoints the outcome under
`bench/workspace/runs/`. See [How scoring works](#how-scoring-works) below
for turning a run into metrics.

## Before your first run

The first `run.py` invocation for any repo it hasn't seen creates a **bare
mirror** (`git clone --mirror`) of that repo under
`bench/workspace/mirrors/` — this is a full clone of the golden PR's
upstream repo (keycloak, grafana, sentry, cal.com, discourse fork, etc.),
**several GB per repo**. Mirrors are cached and reused across runs and
tiers, so this cost is paid once per repo, not once per run. `--tier full`
touches every repo in the golden set; `--tier smoke`/`--tier subset` touch a
subset. `bench/workspace/` is gitignored — nothing here is committed.

It also spends real API credit. Measured on the smoke tier (3 PRs): a
full-skill code-gauntlet pass costs roughly **$8/PR**, a `--anchor naive`
bare single-pass review costs roughly **$0.50/PR**, and scoring one run
(dedup + judge + adjudicator) costs roughly **$2**. `--tier subset` (15
PRs) and `--tier full` (50 PRs) scale up from there — budget accordingly
before a multi-run baseline pass.

## Prerequisites

`run.py` checks these first, before touching any PR; each failure prints one
actionable line and the whole list is shown at once (exit code 2):

- `claude` CLI on `PATH`, with a parseable `--version`
- `gh` CLI on `PATH`, authenticated (`gh auth status` passing)
- `bench/.env` present with a non-empty `ANTHROPIC_API_KEY`
- `uv` on `PATH` (runs the vendored scorer in its own isolated environment)
- at least 10 GB free on the partition holding `bench/workspace/` (bare
  mirrors are large — see above)

`--score-only` skips these entirely — it re-scores an already-completed
run's captured candidates and needs no `claude`/`gh`/disk headroom.

## CLI reference

```
python3 bench/run.py --tier smoke|mini|subset|holdout|full [--runs N] [--fidelity dry-run|live]
                      [--resume RUN_ID] [--retry-failed RUN_ID]
                      [--timeout-mins 45] [--anchor naive] [--score-only RUN_ID]
                      [--check RUN_ID] [--prs URL[,URL...]|mini]
```

| Flag | Values | Default | What it does |
|---|---|---|---|
| `--tier` | `smoke` \| `mini` \| `subset` \| `holdout` \| `full` | — (required for a new run unless `--prs`) | Which PR set to run. `smoke` = 3 PRs (functional smoke). `mini` = 6-PR paired cut. `subset` = 15-PR gate. `holdout` = 10-PR confirmation set (sealed for V3.2). `full` = all 50 golden PRs. |
| `--prs` | URL list or `mini` | — | Explicit golden PR list; labels the run `custom`. The alias `mini` expands to the pre-registered 6-PR mini subset. |
| `--runs` | int | `1` | Number of sequential runs, each getting its own run directory under `bench/workspace/runs/`. |
| `--fidelity` | `dry-run` \| `live` | `dry-run` | Recorded in the run manifest. |
| `--resume` | `RUN_ID` | — | Re-run only the PRs of `RUN_ID` still `pending` (skips `ok`/`invalid`/`drifted`). |
| `--retry-failed` | `RUN_ID` | — | Re-run only the `timeout`/`failed` PRs of `RUN_ID`. |
| `--timeout-mins` | int | `45` | Per-PR watchdog: a PR whose invocation exceeds this is killed (whole process group) and marked `timeout`. No auto-retry — use `--retry-failed`. Default calibrated from the smoke shakedown, where full-skill per-PR reviews ran 16–22 minutes. |
| `--anchor` | `naive` | — | Instead of the code-gauntlet skill, run a bare single-pass same-model review (no plugin, pinned turn budget) through the same adapter, for comparison. The naive prompt requires the model to end its reply with a fenced ```` ```json ```` block containing a `comments` list; a reply where that block can't be parsed is marked `failed` with reason `naive_output_unparseable` (retryable via `--retry-failed`). |
| `--score-only` | `RUN_ID` | — | Re-score an already-captured run's candidates without re-invoking `claude`/`gh` (used for repeated judge re-scoring of the same candidate set). |
| `--check` | `RUN_ID` | — | Mechanical functional-smoke checker (payload/schema, no silent degrade, plugin `scriptPath`, ≥1 comment). Exit 0 = pass. Never invokes the judge. See [`MEASUREMENT.md`](MEASUREMENT.md). |

At most one of `--resume` / `--retry-failed` / `--score-only` may be given
per invocation. A killed pass loses at most the one PR that was mid-flight;
resuming picks up where it left off. For a new run or `--resume`/
`--retry-failed`, `run.py` exits `0` only if every targeted PR in the run
ended status `ok`, and `1` if any PR ended `timeout`/`failed`/`drifted`/
`invalid` — useful for gating a CI step on a clean pass.

> **Note:** `--fidelity` is currently accepted and recorded in the run
> manifest, but the harness always captures the skill's **dry-run** payload
> internally (`DEEP_REVIEW_POST_MODE` is pinned to `dry-run` for every bench
> invocation) — `--fidelity live` does not currently change runner
> behavior.

## How scoring works

Each PR's dry-run payload — the comment set the skill would have posted —
is adapted into one scorer "candidate" per comment. Candidates are run
through the vendored MIT scorer's dedup and LLM-judge stages against that
PR's golden comments, then every candidate the judge did **not** match to a
golden comment is classified by a frozen adjudicator prompt. Every candidate
lands in exactly one of three buckets:

1. **golden-matched** — the judge matched this candidate to a real golden
   comment (a true positive).
2. **valid_extra** — not golden-matched, but the adjudicator judged it
   grounded, concrete, and checkable against the diff it cites.
3. **noise** — not golden-matched and failed the adjudicator's checks
   (ungrounded, vague, or incoherent).

`precision_strict = tp / (tp + fp)` and `golden_recall = tp / (tp + fn)`,
both from the judge's aggregate counts (not a per-review average). The
judge and adjudicator are the same pinned, dated snapshot at temperature 0
(see [Keys and the judge](#keys-and-the-judge)), applied identically to
bench runs and to the anchor tools — scoring is blind to which tool
produced a candidate.

Each scored run's ledger row is labeled by `tool`: `deep-review-v2` for a
normal skill run, `naive-anchor` for a `--anchor naive` run — both flow
through the identical candidate/adapter/scoring pipeline, differing only in
how the candidates were produced and how the row is labeled.

**Where results land:**

- `bench/workspace/runs/{run_id}/scores.json` — full breakdown for one run
  (metrics, bucket membership, every adjudication verdict).
- `bench/experiments.jsonl` — the append-only ledger; one row per scored run
  (`golden_recall`, `valid_extra_rate`, `noise_rate`, `precision_strict`,
  `f1_strict`, `cost_usd`, `tokens_total`, `judge_pin`, `scorer_sha`, …).

## Keys and the judge

- `bench/.env` needs `ANTHROPIC_API_KEY` at minimum — the runner refuses to
  start without it.
- `BENCH_JUDGE_API_KEY` is optional: scoring (judge + adjudicator) uses it
  if set, otherwise falls back to `ANTHROPIC_API_KEY`. One key is enough to
  run everything end to end.
- The judge/adjudicator model is pinned in `bench/baselines.json` as
  `judge_pin`/`adjudicator_pin`: currently **`claude-opus-4-5-20251101`**
  (a dated snapshot, temperature 0). The spec originally targeted a dated
  Opus 4.8 snapshot, but that combination turned out to be unobtainable: no
  dated Opus 4.8 model id exists, and the Opus 4.7/4.8/Sonnet-5/Fable-5
  family rejects the `temperature` parameter outright, while the vendored
  scorer and the frozen adjudicator both hardcode `temperature=0`.
  `claude-opus-4-5-20251101` is dated, accepts `temperature=0`, is the same
  model the upstream benchmark's published results were judged with, and
  was validated end to end via a spot-check reproduction. Scoring refuses
  to run if `judge_pin` is null, or if a `MARTIAN_MODEL` env var disagrees
  with the pinned value — it never silently drifts to a different model.
- The vendored scorer talks to Anthropic's OpenAI-compatible endpoint and
  runs under `uv run` from `bench/vendor/code-review-benchmark/`; it is
  never imported by the stdlib runner.

## What gets written where

Gitignored (`bench/workspace/`, `bench/.env`):

- `bench/workspace/mirrors/` — cached bare git clones, reused across runs
- `bench/workspace/runs/{run_id}/` — per-run state: per-PR worktrees
  (removed after use), `diff.patch`, raw invocation output (`raw.json` for
  skill runs, `raw-naive.json` for `--anchor naive` runs),
  `post-review-payload.json`, the delivered report, `run.json` manifest,
  `state/` checkpoint files, `scores.json`
- `bench/workspace/claude-home/` — isolated `HOME`/`CLAUDE_CONFIG_DIR` so
  operator config never leaks into a run
- `bench/.env` — your API key(s)

Committed:

- `bench/golden/` — vendored golden PR data (50 PRs, 137 golden comments),
  `subsets.json` (tier membership), `shas.json` (pinned head/base SHAs),
  `review_md_fixture.md`
- `bench/vendor/` — pinned MIT-licensed scorer subset plus
  [`VENDORED.md`](vendor/VENDORED.md) (upstream commit, citation, file list)
- `bench/baselines.json` — judge/adjudicator pins, scorer SHA, anchor table,
  judge noise SD, `delta_noise` (read-only to any optimizing loop)
- `bench/experiments.jsonl` — the append-only scoring ledger
