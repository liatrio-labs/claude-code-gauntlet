# bench — code-gauntlet benchmark harness

`bench/` drives the code-gauntlet skill headlessly against a curated set of
golden PRs from [withmartian/code-review-benchmark](https://github.com/withmartian/code-review-benchmark)
(MIT — see [`vendor/VENDORED.md`](vendor/VENDORED.md)), then scores the
results in three buckets against a pinned LLM judge. Owner-triggered runs measure
recall and noise; deterministic suites gate development.

**Measurement policy:** paired bench runs are *not* the default gate for every
change. See the canonical runbook [`MEASUREMENT.md`](MEASUREMENT.md) for the
measurement tiers (always-on suites → functional smoke → owner-triggered
gate and sealed holdout), populations and registered rule requirements.

**Profiling a recorded workflow run:** see [`PROFILING.md`](PROFILING.md) for
`profile_run.py`, which turns a recorded `code-gauntlet` workflow run (record JSON +
subagent transcripts + orchestrator session) into a stage/critical-path/cost profile.

## Golden epochs and immutable scores

The active epoch is `e616e84-v1`: 173 bugs over the fixed 50 PRs, with core
primary (158 full / 66 gate). All (173 / 73) and strict (139 / 58) are also
reported. `dfc6cb4-v1` survives in git history; its recorded scores retain
their original epoch and are never compared with active scores.
New baseline, anchor, variability and diagnostic noise measurements are pending.
Recorded measurement status is in [MEASUREMENT.md](MEASUREMENT.md).

```bash
python3 bench/run.py --score-only RUN_ID --golden-epoch e616e84-v1
```

Scoring writes a fresh UUID packet at `scores/<epoch-id>/<score-id>/`, retaining
raw candidates, dedup groups, evaluations, exact adjudication contexts, per-bug
observations and provenance. Profile summaries and ledger projections are derived
when read. A complete marker protects the evaluated artifact bytes. Failed transactions remain under `.pending-*` and cannot be read as scores.
Existing packets and legacy `scores.json` are preserved. New scores use packet paths. `latest-score.json` is a convenience pointer, never an official
comparison selector. Explicit `(run_id, epoch_id, score_id)` identities survive
rescoring. `bench.runner.ledger.replay_packet` recovers a completed packet after
a failed ledger append without judging again.

Historical captured reviews are descriptive rescoring inputs with unresolved
provenance. Scoring cannot make those runs valid measurement records. New reviews
preserve immutable attempts, validate window identity when supplied, and collect
stage traces on the run path.
See [vendoring](vendor/VENDORED.md) for active inputs and historical scores.

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
before a multi-run baseline pass. `--child-auth subscription` runs the review
children on an existing Pro/Max plan instead, at no marginal cost — see
[Child auth modes](#child-auth-modes).

## Prerequisites

`run.py` checks these first, before touching any PR; each failure prints one
actionable line and the whole list is shown at once (exit code 2):

- `claude` CLI on `PATH`, with a parseable `--version`
- `gh` CLI on `PATH`, with a credential that can read the first target PR
- the review children's credential, which depends on `--child-auth`:
  - `api` (default) — `bench/.env` present with a non-empty `ANTHROPIC_API_KEY`
  - `subscription` — a non-empty `CLAUDE_CODE_OAUTH_TOKEN` (in `bench/.env` or
    the environment) and no `apiKeyHelper` in the isolated config; see
    [Child auth modes](#child-auth-modes)
- `uv` on `PATH` (runs the vendored scorer in its own isolated environment)
- at least 10 GB free on the partition holding `bench/workspace/` (bare
  mirrors are large — see above)

For new runs, resumes, and retries, GitHub authentication is resolved once per
`run.py` invocation, before any review child starts: nonempty `GH_TOKEN`, then
`GITHUB_TOKEN`, then the operator's `gh auth token --hostname github.com`. The
selected credential is held in memory and forwarded as `GH_TOKEN` across the
isolated HOME boundary. A bounded
`gh api repos/OWNER/REPO/pulls/N` read of the first target PR probes that exact
credential under the child environment.
An explicit credential that is rejected fails preflight (exit 2); it never falls
back to another account. Probe diagnostics and token values are never logged or
written to run manifests or checkpoints.

`--score-only` and `--check` skip GitHub authentication. `--score-only` re-scores
an already-completed run's captured candidates and needs no `claude`/`gh`/disk
headroom.

## CLI reference

```
python3 bench/run.py --tier smoke|mini|subset|holdout|full [--runs N] [--fidelity dry-run|live]
                      [--resume RUN_ID] [--retry-failed RUN_ID]
                      [--timeout-mins 45] [--anchor naive] [--score-only RUN_ID]
                      [--check RUN_ID] [--prs URL[,URL...]|mini|dev-slice|dev-pool]
                      [--contamination PATH] [--rule PATH] [--unseal-holdout RECEIPT]
                      [--child-auth api|subscription|host]
                      [--golden-epoch EPOCH_ID] [--attribution]
                      [--tool deep-review-v2|deep-review-v3] [--child-model MODEL_ID]
                      [--measurement-window PATH --arm ARM --run-index N]
                      [--reset-confirmed-at ISO_TIMESTAMP]
                      [--baseline-equivalence-receipt PATH]
```

| Flag | Values | Default | What it does |
|---|---|---|---|
| `--tier` | `smoke` \| `mini` \| `subset` \| `holdout` \| `full` | — (required for a new run unless `--prs`) | Which PR set to run. `smoke` = 3 PRs (functional smoke). `mini` = 6-PR paired cut. `subset` = 15-PR gate. `holdout` = 10 sealed PRs. `full` includes sealed PRs. Both require `--unseal-holdout RECEIPT.json` at that close. |
| `--prs` | URL list, `mini`, `dev-slice`, `dev-pool` | - | Explicit or named PR list; labels the run `custom`. Dev aliases require `--contamination PATH` and resolve through the named populations. |
| `--contamination` | JSON path | off | Resolve dev aliases and bind a candidate to frozen contamination dispositions. Required for a registered candidate run. |
| `--rule` | JSON path | off | Frozen promotion rule whose digest and initial run count must match a rule-bound window. |
| `--unseal-holdout` | receipt path | off | Authorize holdout PRs for the approved final treatment and reserve one run identity after preflight. |
| `--runs` | int | `1` | Number of sequential runs, each getting its own run directory under `bench/workspace/runs/`. |
| `--fidelity` | `dry-run` \| `live` | `dry-run` | Recorded in the run manifest. |
| `--tool` | `deep-review-v2` \| `deep-review-v3` | `deep-review-v3` | Pipeline to invoke and record. |
| `--child-model` | model ID | `inherit` | Child orchestrator model. A measurement window requires resolved service IDs matching its declared treatment. |
| `--measurement-window` | JSON path | off | Validate shared controls, arm treatment, time bounds and requested PRs before mirror work. Resume and retry require the original path and unchanged bytes. |
| `--arm` | declared arm name | - | Arm in the window. Required for a new windowed run. |
| `--run-index` | nonnegative integer | - | Planned run index in the window. Required for a new windowed run; `--runs` advances it for each sequential run. |
| `--reset-confirmed-at` | timezone-aware timestamp | - | Operator-recorded subscription reset after the failed attempt completed and no later than retry start. |
| `--baseline-equivalence-receipt` | JSON path | - | Copy the supplied JSON into the new run manifest. This runner does not validate its contents. |
| `--resume` | `RUN_ID` | — | Re-run only the PRs of `RUN_ID` still `pending` (skips `ok`/`invalid`/`drifted` and also `timeout`/`failed` — use `--retry-failed` for those). |
| `--retry-failed` | `RUN_ID` | — | Re-run attempts whose validity marks them retryable, including `invalid` and `drifted` outcomes. Legacy checkpoints without validity use `timeout`/`failed`. Confirmed operational failures are terminal. |
| `--timeout-mins` | int | `45` | Per-PR watchdog: a PR whose invocation exceeds this is killed (whole process group) and marked `timeout`. Attempt validity separately records whether it is retryable or a confirmed operational failure. No auto-retry — use `--retry-failed`. Default calibrated from the smoke shakedown, where full-skill per-PR reviews ran 16–22 minutes. |
| `--anchor` | `naive` | — | Instead of the code-gauntlet skill, run a bare single-pass same-model review (no plugin, pinned turn budget) through the same adapter, for comparison. The naive prompt requires the model to end its reply with a fenced ```` ```json ```` block containing a `comments` list; a reply where that block can't be parsed is marked `failed` with reason `naive_output_unparseable` (retryable via `--retry-failed`). |
| `--child-auth` | `api` \| `subscription` \| `host` | `api` | Which credential the **review children** spend: the metered `bench/.env` key, subscription capacity via `CLAUDE_CODE_OAUTH_TOKEN`, or host-managed cloud capacity (requires `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST`). Recorded in `run.json` and as the ledger row's `auth_mode`. A `--resume`/`--retry-failed` run keeps the mode it started with; passing a flag that contradicts it is refused (exit 2) rather than mixing credentials within one run. Scoring is unaffected and stays API-keyed. See [Child auth modes](#child-auth-modes). |
| `--score-only` | `RUN_ID` | - | Score captured payloads on the active epoch without invoking review children. Each execution writes a fresh immutable packet and appends its ledger projection. |
| `--golden-epoch` | `EPOCH_ID` | active epoch | Select the epoch for `--score-only`. Only the active epoch is runnable. |
| `--attribution` | flag | off | With `--score-only`, also judge eliminated discovery claims and record stage losses. Missing traces leave attribution unavailable. |
| `--check` | `RUN_ID` | — | Mechanical functional-smoke checker for a completed **skill** run (not `--anchor naive`). Checks payload/schema, findings persistence, no silent degrade, plugin identity (Headless config `pipeline_version`/`plugin_root` echo receipts primary; the record's `script` bytes and `workflowName` identify the repo bundle when present; `scriptPath` resolving to the repo bundle remains accepted for older records), artifact completeness (findings, report, post-review, checkpoint present exactly once), and ≥1 comment. Prints the non-gating citation absence-preamble watch line. Exit 0 = pass; never invokes the judge. See [`MEASUREMENT.md`](MEASUREMENT.md). |

At most one of `--resume` / `--retry-failed` / `--score-only` / `--check` may
be given per invocation. `--check` also rejects `--tier` / `--prs` / `--anchor`
/ `--runs` (those flags would otherwise be silently ignored). A killed pass
loses at most the one PR that was mid-flight; resuming picks up where it left
off. For a new run or `--resume` / `--retry-failed`, `run.py` exits `0` only if
every targeted PR in the run ended status `ok`, and `1` if any PR ended
`timeout`/`failed`/`drifted`/`invalid` — useful for gating a CI step on a clean
pass.

> **Note:** `--fidelity` is currently accepted and recorded in the run
> manifest, but the harness always captures the skill's **dry-run** payload
> internally (`CODE_GAUNTLET_POST_MODE` is pinned to `dry-run` for every bench
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

The plugin was formerly named deep-review; ledger labels retain that name for measurement continuity.
Each scored run's ledger row is labeled by `tool`: `deep-review-v3` for a
normal skill run, `naive-anchor` for a `--anchor naive` run — both flow
through the identical candidate/adapter/scoring pipeline, differing only in
how the candidates were produced and how the row is labeled.

**Where results land:**

- `bench/workspace/runs/{run_id}/scores/{epoch-id}/{score-id}/` - immutable
  evidence packet with `scores.json`, raw scorer outputs, adjudications and contexts.
- `bench/workspace/runs/{run_id}/latest-score.json` - pointer to the latest completed score.
- `bench/experiments.jsonl` - the append-only ledger; one row per score execution
  (`golden_recall`, `valid_extra_rate`, `noise_rate`, `precision_strict`,
  `f1_strict`, `cost_usd`, `tokens_total`, `judge_pin`, `scorer_sha`, …).

## Keys and the judge

- `bench/.env` needs `ANTHROPIC_API_KEY` at minimum — the runner refuses to
  start without it (unless `--child-auth subscription` is in play; see
  [Child auth modes](#child-auth-modes)).
- `BENCH_JUDGE_API_KEY` is optional: scoring (judge + adjudicator) uses it
  if set, otherwise falls back to `ANTHROPIC_API_KEY`. One key is enough to
  run everything end to end.
- The judge/adjudicator model is pinned in `bench/golden/epoch.json` as
  `judge_pin`/`adjudicator_pin`: currently **`claude-opus-4-5-20251101`**
  (a dated snapshot, temperature 0). The spec originally targeted a dated
  Opus 4.8 snapshot, but that combination turned out to be unobtainable: no
  dated Opus 4.8 model id exists, and the Opus 4.7/4.8/Sonnet-5/Fable-5
  family rejects the `temperature` parameter outright, while the vendored
  scorer and the frozen adjudicator both hardcode `temperature=0`.
  `claude-opus-4-5-20251101` is dated, accepts `temperature=0`, is the same
  model the upstream benchmark's published results were judged with, and
  was validated end to end via a spot-check reproduction. Scoring requires
  valid epoch pins and refuses a `MARTIAN_MODEL` env var that disagrees
  with the judge pin — it never silently drifts to a different model.
- The vendored scorer talks to Anthropic's OpenAI-compatible endpoint and
  runs under `uv run` from `bench/vendor/code-review-benchmark/`; it is
  never imported by the stdlib runner.

## Child auth modes

Use `--child-auth host` inside an operator-launched Claude Code cloud session.
It requires `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST`, injects no credential, and strips
over-ranking API/provider credentials and OAuth. `ANTHROPIC_BASE_URL` and
`CLAUDE_SESSION_INGRESS_TOKEN_FILE` pass through unchanged. Select `--auth-mode host`
when creating its window; that auth mode identifies a separate lane. Final ship
comparisons still require API auth.

`--child-auth` selects the credential the **review children** spend. Nothing
else changes: the isolation envelope, the pinned knobs, the watchdog, and the
whole scoring path are identical in all three modes.

```bash
claude setup-token                 # one-time; prints a long-lived OAuth token
# put it in bench/.env as CLAUDE_CODE_OAUTH_TOKEN=..., or export it
python3 bench/run.py --tier smoke --child-auth subscription
python3 bench/run.py --check <RUN_ID>
```

| | `api` (default) | `subscription` | `host` |
|---|---|---|---|
| Credential | `ANTHROPIC_API_KEY` from `bench/.env`, force-injected | `CLAUDE_CODE_OAUTH_TOKEN` from `bench/.env`, else the ambient env | Host-managed session; requires `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST` |
| Marginal cost | metered API spend | absorbed by an existing Pro/Max plan | cloud credit first, then plan capacity |
| Ledger `auth_mode` | `api` | `subscription` | `host` |
| `cost_usd` | billable spend | recorded consumption, not API spend | recorded consumption, not API spend |

### Why subscription and host modes strip env vars

Claude Code resolves credentials in a fixed order — cloud providers
(`CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` / `CLAUDE_CODE_USE_FOUNDRY`) → `ANTHROPIC_AUTH_TOKEN`
→ `ANTHROPIC_API_KEY` → `apiKeyHelper` → `CLAUDE_CODE_OAUTH_TOKEN` → the
interactive `/login` session. The child inherits your full environment, so any
one of those higher-ranked sources surviving would silently defeat the mode and
bill the API anyway. Subscription and host modes therefore **remove** these five env vars. Host mode
also removes `CLAUDE_CODE_OAUTH_TOKEN`.

`apiKeyHelper` lives in a settings file, not the environment, so it cannot be
stripped. For subscription and host modes, the prereq check refuses the run and names the offending file
(`settings.json` / `settings.local.json` under the isolated claude home, both in
its `config/` dir — the `CLAUDE_CONFIG_DIR` the child gets — and in a
HOME-relative `.claude/`; `BENCH_CLAUDE_HOME` can relocate that home).

**Limitations of that guard.** It covers the isolated home and nothing else, so
subscription honesty also assumes no higher-precedence credential in a settings
scope the harness does not own: a golden worktree's own committed
`.claude/settings.json` (project scope, and the worktree only exists mid-run,
after preflight), or enterprise **managed settings**. Neither is plausible for
this golden set, but if the mode ever appears to run free while your API key is
still being billed, those are the scopes to check. Any such credential would
have to be present before the run to matter — the check is a one-shot preflight,
not re-verified per PR.

The mode is **token-only by design**. It never relies on the macOS Keychain or
on `~/.claude/.credentials.json` surviving the `HOME`/`CLAUDE_CONFIG_DIR`
isolation: Keychain credentials happen to survive on macOS while
`.credentials.json` is stranded on Linux, so implicit auth would be a
portability trap. No token, no run — the prereq check fails loud before any PR
is touched.

### Scoring stays API-keyed

Subscription capacity covers only the child review sessions. The judge and
adjudicator run through the vendored scorer against Anthropic's API and still
need `BENCH_JUDGE_API_KEY` or `ANTHROPIC_API_KEY`. Subscription mode drops the
`ANTHROPIC_API_KEY` **prereq** (the run genuinely does not need it), so a
key-less machine can run a review pass and the mechanical
[`--check`](#cli-reference) gate, which never invokes the judge; `--score-only`
on such a run fails loud at score time with a missing-judge-key error. Because
that failure lands hours after the spend, the runner prints a one-line `NOTE:` at
startup when a subscription run can see no judge key at all — a warning, not a
prerequisite.

### Cost honesty

Anthropic documents a subscription-served run's `total_cost_usd` as not relevant
for billing purposes, and its representation is unspecified. So the harness
records the envelope's figure unchanged — token counts are real either way — and
qualifies it with `auth_mode` on the ledger row:

- `bench/report.py` keeps the figure in the run-ledger table behind a `‡`
  marker, and **excludes** it from the run-cost tile and from every derived
  per-golden cost figure.
- Any cost figure quoted in this README or in [`MEASUREMENT.md`](MEASUREMENT.md)
  is `auth_mode=api` spend only.

### Usage limits

The first live subscription smoke (`smoke-20260812-030333-0f3f550`,
2026-08-12, main @ 0f3f550 / v3.8.0) observed **no usage-limit hit** across 3
sequential PRs (~38 min of child time). Official docs describe a 5-hour
rolling window plus weekly and Opus-specific caps, and say that on a limit hit
further `-p` requests are blocked until the window resets. No machine-readable
error shape is documented, so the predicted symptom in bench terms — the
per-PR watchdog firing (status `timeout`, reason `watchdog_timeout`) rather
than a clean error — remains unexercised; this run didn't trip the window.
A subscription retry requires `--retry-failed RUN_ID` with
`--reset-confirmed-at ISO_TIMESTAMP` after the recorded failure completed.

### Pipeline failure envelopes

The pipeline's Workflow return envelope can classify a child as `failed` with
reason `all_degraded` when every active discovery dimension degrades, or
`pipeline_failed` for another `ok: false` return. Attempt validity separately
controls retry eligibility.
A first non-ok attempt is invalid and retryable, including a checkpoint status of
`invalid`; a confirmed second failure is terminal. An `all_degraded` child is
self-describing and often transient, so retrying it after the underlying service
recovers is appropriate.

A child that stops before any Workflow record exists is `failed` with reason
`pre_workflow_stop` when its own session transcript contains a Bash tool result
with the full pinned config and matching plugin root and pipeline version.
This outcome is unscored and eligible for `--retry-failed`. Prompt text, model
prose, and receipts from other sessions cannot establish it. Missing or mismatched
receipts remain `invalid`.

### Recommended usage, and scope

Functional smokes and dev iteration are the intended home for subscription mode
— they are mechanical pass/fail, and under the ratcheted measurement policy the
smoke is the recurring per-sub-release cost. **Of-record paired legs stay on
`--child-auth api`**: clean cost accounting, and no throttle-induced timeouts
mid-measurement. [`MEASUREMENT.md`](MEASUREMENT.md) states which tiers may run
on subscription.

This mode exists to spend **your own** subscription capacity — ordinary,
individual usage, via the `setup-token` mechanism Anthropic explicitly provides
for CI pipelines and scripts. It is not a credential-routing surface for third
parties, and routing other people's review traffic through Free/Pro/Max
credentials is prohibited by the usage policy.

## Measurement history

The [release table](../README.md#benchmark-results) records the v3.0 gate subset and holdout, plus the v2 baseline and upstream anchors.
Later paired minis use the same six PRs as the v3.1 baseline.
The [committed ledger](experiments.jsonl) records these runs:

| Release | Date | Run identifiers | Recall | Noise |
| --- | --- | --- | --- | --- |
| v3.1 baseline | 2026-07-23 | `custom-20260723-102149-381e9ff` | 0.633 | 0.223 |
| v3.12 | 2026-08-18 | `mini-20260818-120540-b423885` + `custom-20260818-142206-b423885` | 0.667 | 0.106 |
| v3.24 | 2026-09-01 | `mini-20260901-170531-2cb9104` | 0.633 | 0.179 |
| v3.26 | 2026-09-03 | `mini-20260903-204424-e8b3af7` | 0.667 | 0.138 |
| v3.42.49 baseline | 2026-10-20 | `mini-20261020-030509-839b1886` | 0.633 | 0.065 |

The v3.12 row combines a five-PR mini and its one-PR completion leg.
The v3.26 mini met the pre-registered 0.24 noise ceiling.
At six PRs, one reference finding moves recall by 3.3 percentage points.
Treat the gain over v3.1 as a consistency check.
Noise in the v3.24 and v3.26 minis is below the v3.1 baseline.
That matches what we would expect from v3.24's wider injection filtering.
That change only adds eliminations.
These runs do not establish causation.

`report.py` renders release summaries, run-level recall and noise, tokens, cost, and anchor comparisons from the ledger and baselines.
Active scores and the read-only historical release dashboard appear in separate epoch sections.
The header identifies the latest ledger entry, so committed inputs reproduce the committed page.

## What gets written where

Gitignored (`bench/workspace/`, `bench/.env`):

- `bench/workspace/mirrors/` — cached bare git clones, reused across runs
- `bench/workspace/runs/{run_id}/` — per-run state: per-PR worktrees
  (removed after use), `diff.patch`, raw invocation output (`raw.json` for
  skill runs, `raw-naive.json` for `--anchor naive` runs),
  `post-review-payload.json`, the delivered report, `run.json` manifest,
  `state/` checkpoint files, `scores/{epoch-id}/{score-id}/` packets and
  `latest-score.json`
- `bench/workspace/claude-home/` — isolated `HOME`/`CLAUDE_CONFIG_DIR` so
  operator config never leaks into a run
- `bench/.env` — your API key(s), and the optional
  `CLAUDE_CODE_OAUTH_TOKEN` for `--child-auth subscription`

Committed:

- `bench/golden/` — raw upstream golden PR data (50 PRs, 173 golden comments),
  `subsets.json` (tier membership), `shas.json` (pinned head/base SHAs),
  `review_md_fixture.md`
- `bench/vendor/` — pinned MIT-licensed scorer subset plus
  [`VENDORED.md`](vendor/VENDORED.md) (upstream commit, citation, file list)
- `bench/baselines.json` - pending active-epoch measurement and calibration status.
  Both scoring pins live in `golden/epoch.json`.
- `bench/golden/historical_baselines.json` - historical anchor table, judge noise SD
  and `delta_noise` (read-only to any optimizing loop, historical epoch only).
- `bench/experiments.jsonl` — the append-only scoring ledger

Stage attribution is opt-in and evaluates only discovery versions eliminated before delivery:

```bash
python3 bench/run.py --score-only RUN_ID --golden-epoch e616e84-v1 --attribution
```

Workflow traces are separate artifacts with attempt-bound byte hashes.
Delivered candidates retain their actual posted text and credit discovery dimensions
through trace provenance without additional judging. Snapshots identify losses between
discovery and delivery. Missing or unreadable traces leave attribution unavailable
and preserve delivered recall. Collection status and its reason remain in the score packet.
Scoring records dedup, judge and adjudicator costs from response usage with pinned prices.
Attribution cost is separate; unknown costs remain null. See [MEASUREMENT.md](MEASUREMENT.md).

## Measurement windows and immutable attempts

Ordinary smoke and development runs can omit a window; their provenance is
recorded as `non_record`. Windowed runs use a reviewed JSON declaration decoded
by `bench.runner.validity.load_window`. A window declares its time bounds,
shared controls, PR URLs, planned run indices, sequential execution
(`max_parallel_prs: 1`) and two arm treatments. Shared controls pin the CLI real
path, binary hash and version, judge and adjudicator, golden epoch and scorer,
auth mode and disabled auto-update. Each arm pins its plugin tree, resolved
model roster, allowed auxiliary model IDs, agent efforts and bench policy hash.

`planned_order` declares alternating `[arm, run_index]` pairs. `wave_id` and
`rule_sha256` bind a window to its preregistered promotion rule. `candidate_arm`
identifies the candidate entry (default `candidate`); its preflight requires rule
registration and a frozen contamination manifest. Create a window with both local
sources (SHA sources are materialized temporarily without switching the checkout):

```sh
python3 -m bench.runner.windows --population mini \
  --arm baseline=sha:HEAD --arm candidate=worktree:/path/to/plugin \
  --run-indices 0,1,2 --auth-mode api --duration-hours 24 \
  --child-model claude-opus-4-8 --out window.json
```

For gate windows add `--wave WAVE --rule RULE.json`; registration remains separate.
`--cli`, `--timeout-mins`, `--opens-at`, `--contamination`, and `--unseal` are optional.
Creation captures both fingerprints with the epoch's judge pins and disabled updater.
New windows use schema 2, with sequential PRs; schema-1 declarations remain readable.

Start a declared run with the arm's resolved orchestrator model ID:

```bash
python3 bench/run.py --tier subset --measurement-window window.json \
  --child-model MODEL_ID --arm baseline --run-index 0
```

The requested tier or explicit PR list must match the window's PR set. Ambient
model, effort and provider overrides are refused before mirror work. Each PR
rechecks window identity and treatment before mirror work; the pinned CLI is
rechecked immediately before dispatch and after collection. The child gets
`DISABLE_AUTOUPDATER=1`. Installed CLI update and effective effort behavior still
require live verification. Resume and retry require the original window path,
unchanged window bytes and matching provenance.

Tracked plugin inputs must be clean at entry and before each windowed PR.
Treatment identity hashes index entries (names, modes and blob IDs) under
`.claude-plugin`, `agents`, `skills`, `workflows` and `scripts`, including the
shipped bundle and gauntlet helpers. The manifest's `treatment_git_sha` and
`agent_read_sha256` carry this digest, independent of HEAD. Bench evidence and
ledgers lie outside these plugin inputs.

Each outcome is preserved under `attempts/<pr-key>/<attempt-id>/` with an
`attempt.json` and hashed artifact copies: raw output, payloads, workflow records,
traces, model provenance and consumption receipts when available. Checkpoints
select the latest scheduling outcome; retries preserve prior attempt bytes.
The run path collects optional traces and records collection status and reason.
Windowed skill attempts require a terminal measurement status of `available` or
`unavailable`; a missing status is invalid. A declared unavailable trace remains
unknown for attribution. Stage execution failures invalidate the attempt even
when no optional trace was captured.

A first non-ok attempt is invalid and retryable. A second non-ok attempt for the
same PR, window and observed fingerprint becomes a terminal operational failure
with all golden bugs missed. The diagnostic text need not match. API retries
require no reset declaration. Subscription retries require a recorded reset time
after the first attempt completed and no later than retry start:

```bash
python3 bench/run.py --retry-failed RUN_ID --measurement-window window.json \
  --reset-confirmed-at ISO_TIMESTAMP
```

Selected attempts and review provenance remain immutable. Scoring uses the
checkpoint-selected attempt artifacts and retains prior-attempt consumption;
rescoring does not create an independent review run.

## Dispositions, audit, comparisons and planning

The operating inputs use explicit immutable score references. Commands compute reports
and never select a latest score. A descriptive comparison names `dev-slice`,
`dev-pool`, or `mini` (the historical A/A), uses `profile: "core"`, and omits rule
paths. It may declare windows or retain pathless historical evidence. Holdout comparisons additionally require `unseal`, the final-SHA
receipt path, and the approved candidate treatment. Operational comparisons name a
gate population and require a full gate matrix.

Comparisons can name `"window_paths": ["windows/api.json", "windows/host.json"]`;
`window_path` remains the singleton alias. See the
[blocked-comparison protocol](MEASUREMENT.md#validity-and-statistics) for balancing,
pooling, lanes, extensions and look accounting.

Import the leakage lens's reviewed v2 dispositions:

```sh
python3 -m bench.runner.populations --spec leak-manifest-v2.json --out proposal.json
```

The population freeze spec has `manifest` (the imported proposal object),
`baseline_treatment_sha` (the Wave 0a plugin input tree digest), and `frozen_at`
(an ISO timestamp). The baseline tree digest must match the scanned clean checkout.
The freeze runs the leak scan itself, refuses failing evidence, and binds its digest.
Every candidate captures the frozen contamination digest before dispatch. Comparison
requires that binding and a freeze time strictly before every candidate run.

```sh
python3 -m bench.runner.populations --spec freeze-spec.json --out contamination.json
```

A sample spec has `seed`, `contamination_path`, `population` (default
`gate-uncontaminated`) and `arms`, a mapping from arm names to score references.
Each reference has `run_id`, `epoch_id`, and `score_id`. Wave 0b planning uses one
baseline arm, capped at 30 findings, accepted by simulation. Wave comparisons require
two equal-size samples capped at 30 and the smaller uncontaminated census. Excluded
PRs never enter the sampler or groundedness guard. Receipts bind that population.

```sh
python3 -m bench.runner.audit --spec sample-spec.json --out audit/
```

Keep `audit/private/sample.json` private. Give only `audit/public/packets.json` to the
Sol and Opus auditor seats, run by the orchestrator with repository access. Each label
file has `auditor` (`Sol` or `Opus`), `packet_sha256` (the exact packet file hash),
and `labels`: rows with `sample_id`, Boolean `correct`, and nonempty `evidence_refs`.
A merge spec has `sample_path`, `packet_path`, and `label_paths` in Sol, Opus order.
An optional third file uses the `owner` seat and labels only disputes. The resulting
`owner_queue` must be resolved before an operational wave or ship verdict or simulation.

```sh
python3 -m bench.runner.audit --spec merge-spec.json --out audit/receipt.json
```

A ComparisonSpec has `comparison_id`, `purpose` (`descriptive`, `wave`, or `final`),
`population`, `profile`, `epoch_id`, `contamination_path`, and `baseline`/`candidate`.
Each arm has `name`, `scores`, `treatment_git_sha`, and `agent_read_sha256`.
Operational specs add `window_path`, `rule_path`, and `audit_path`. All
relative paths resolve beside the comparison spec. A permitted extension adds
`initial_spec_path` and `initial_report_path` and preserves the initial score prefix.
The ledger's first-look report hash binds the saved rule, audit and verdict.

```sh
python3 -m bench.runner.compare --spec comparison.json --out comparison-report.json
```

Rules use `kind: "wave"` or `"ship"`, `profile: "core"`, `margin_pp`,
`nonmini_margin_pp`, `initial_runs`, `extension_runs`, `stability_min_runs`,
`stable_loss_min`, `stage_alpha`, `bootstrap` (`draws`, `seed`, `alpha`), and `frozen`.
The checked-in wave rule is frozen. `stable_loss_min` sets the number of stable losses
that fails a comparison. `stage_alpha` controls the primary recall bound at both wave
looks. A wave passes at the first look only when the primary and every harm check
pass. Inconclusive checks permit the planned extension. At the final look the primary
must pass; harm checks fail only on evidence of harm and record inconclusive results.
Ship rules use 3 runs per arm, zero extension runs, and an API window. Both primary
and non-mini recall lower bounds must exceed their negative margins. The harm checks
apply separately. Ship also requires a resolved blinded audit. Register the frozen
rule before the first candidate run:

```sh
python3 -m bench.runner.looks --wave WAVE --rule frozen-rule.json
```

The append-only `golden/looks.jsonl` is the authoritative look count. The compare
command appends operational verdicts. A fail is final. Only a permitted extension
allows a second look with the saved initial report and unchanged score prefix. A third
look is refused. Before the one holdout review at issue #101 close, mint its receipt
with the approved final SHA:

```sh
python3 -m bench.runner.looks --approved-final-sha FINAL_SHA --out unseal.json
```

Pass it to run's `--unseal-holdout`, comparison's `unseal`, and the audit sample's
`unseal` with `final_sha`. Review authorization is reserved after preflight for one
immutable run id. That run may resume or retry with its recorded receipt; another run
is refused. Audit and comparison each use the receipt once. Receipt loading and
label merge revalidate its authorization. Another treatment or unseal is refused.

A simulation spec has `baseline_scores`, `epoch_id`, `contamination_path`,
`rule_paths`, `audit_path`, `baseline_arm`, `trials`, `seed`, `power_target`, and
`mde_grid_pp`. Every source retains its own auth-specific measurement window.

```sh
python3 -m bench.runner.simulation --spec simulation.json --out simulation-report.json
python3 bench/report.py
```

Comparison reports include core, all and strict automatically. Audited dimension
groundedness is reported when its sampled provenance is available. Comparison reports
and the active-epoch dashboard keep review, scoring, and attribution costs separate.
The dashboard reads the ledger and labels scores descriptive; it does not render
comparison verdicts. Unknown costs remain null. Protocol, caveats, measured status and
the gate-look ledger live in [MEASUREMENT.md](MEASUREMENT.md).
