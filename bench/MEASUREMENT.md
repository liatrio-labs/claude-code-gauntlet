# Measurement policy (canonical)

This is the in-repo home for the ratcheted measurement policy (Issue #28).
Maintainer work-queue items link here rather than restating the tiers — see
[`docs/maintainer-issues.md`](../docs/maintainer-issues.md).

**Supersedes** the framing "every behavior-changing item ships behind a paired
bench measurement." Paired measurements are rare, expensive, owner-triggered
events — not the default gate for every change.

## Tier ladder

Every cost below is `auth_mode=api` spend; see
[Which tiers may run on subscription](#which-tiers-may-run-on-subscription).

| Tier | Slug | What | Trigger | Cost (ledger, `auth_mode=api`) | Purpose |
|------|------|------|---------|---------------|---------|
| Always-on suites | `suites-only` | `pytest` (pipeline + `bench/tests/`) + `node --test workflows/test/*.test.js` + `pre-commit` | Every PR / every commit path | $0 | Deterministic correctness |
| Functional smoke | `smoke` | `--tier smoke` (2–3 PRs) + mechanical checker | Per sub-release, run by the release manager | ~$21–$32 (mean ~$27; 16–22 min/PR) | "No bugs, functions correctly" — **not** performance proof |
| Paired mini-subset | `paired-mini` | `--tier mini` or `--prs mini` (6 PRs) vs baseline of record | Owner-triggered, only when a change plausibly moves recall/noise | ~$78–$85 / leg | Gate-grade paired measurement |
| Full-15 / holdout | — | `--tier subset` (~$190–$230) / `--tier holdout` (sealed) | Owner-triggered, release-grade | see ledger | Confirmation / V3.2 reservation |

The `Slug` column is the spelling work-queue issues and PR descriptions use
when they name the tier a change ships behind; the release-grade row has no
short slug because it is owner-triggered and named by the run it produced.

Owner-triggered spend stays owner-triggered: this runbook documents readiness
and protocol; it never schedules or auto-triggers spend, and no work issue
gates on the owner running a measurement.

## Which tiers may run on subscription

`python3 bench/run.py --child-auth subscription` runs the review children on the
owner's own Claude subscription capacity instead of the metered key, at no
marginal cost. Mechanics, prerequisites, and cost-honesty rules:
[`README.md` → Child auth modes](README.md#child-auth-modes).

| Tier | Subscription? | Why |
|------|---------------|-----|
| Always-on suites | n/a | $0 already; no `claude` invocation |
| Functional smoke | **Yes — recommended** | Mechanical pass/fail, not measurement; the recurring per-sub-release cost |
| Dev iteration (`--prs`, `--anchor naive`) | **Yes** | Not of record |
| Paired mini-subset | **No** | Of record: clean cost accounting, no throttle-induced timeouts mid-leg |
| Full-15 / holdout | **No** | Same, at release grade |

Caveat on the "yes" rows: what a subscription usage-limit hit looks like in bench terms
is **not yet confirmed** — expected to surface as a per-PR watchdog `timeout`, recoverable
with `--retry-failed`, but no live subscription smoke has run yet. The first one settles
it and updates [`README.md` → Usage limits](README.md#usage-limits).

Every ledger cost figure in this runbook is `auth_mode=api` spend. A
`auth_mode=subscription` row's `cost_usd` is recorded but is not billable spend
(Anthropic documents it as "not relevant for billing purposes"), so it is
excluded from the dashboard's cost aggregates and must never be quoted here as a
tier cost. A subscription leg is therefore not cost-comparable with an
API-keyed one — which is the second reason of-record legs stay on `api`.

## Functional smoke (mechanical)

Smoke recall is noise (0.0–0.75 swing across 14 smoke rows on ~4 goldens). The
verdict is **mechanical**, never the judge:

```bash
python3 bench/run.py --tier smoke        # --child-auth subscription skips API spend (see caveat above)
python3 bench/run.py --check <RUN_ID>
```

`bench/run.py --check RUN_ID` implements the mechanical smoke gates:

1. Completeness — every `run.json` `pr_urls` entry has terminal status `ok`
2. Payload parse + adapter-required fields + union-schema findings check
   (requires ≥1 `code-gauntlet-findings-*.json` per PR)
3. Zero `origin=unknown` findings; no writer no-write-proof / partial-artifacts
   degrade. **Also fails** when a PR delivers any *unclassified* finding
   (origin not `new`/`surfaced`, including a finding with no `origin` key at
   all — strictly wider than the `origin=unknown` check, matching
   `isClassified` in `workflows/src/stages.js`) whose persisted artifacts
   carry no health-degradation banner sentinel
   (`<!-- code-gauntlet:health:begin -->`) on **either** delivery surface —
   `code-gauntlet-report-*.md` **or** `code-gauntlet-post-review-*.json`'s
   `review_body` field. Both surfaces are checked, and either one is
   sufficient, deliberately not both: `pr_comments` is a legal standalone
   delivery mode (`references/headless-mode.md`) that never shows `report.md`
   to anyone, and the pipeline's own empty-report path persists no report at
   all (`bannered = !emptyReport` in `workflows/src/stages.js`), so review_body
   alone is the correct, documented shape in both cases — requiring both
   surfaces would fail runs the pipeline is behaving correctly on. This is
   reported as an additional gate-3 failure condition rather than a new gate
   number — same underlying fault (an unclassified finding shipped in the
   review), and gate 3 already owns that fault class; what's new is checking
   that a degraded run actually *disclosed* it (issue #25 req 7). Carriers that own a `gaps` array — `workflows/wf_*.json` (the
   compact Workflow return) and `code-gauntlet-checkpoint-all-*.json` — are
   judged from that parsed array alone; their raw bytes are never scanned,
   because a wf record echoes the whole `workflows/pipeline.js` bundle into its
   `script` field and the bundle's own source contains those sentinels as
   ordinary substrings (string/template literals and comments). When such a
   carrier will not parse, or carries no `gaps`
   at all, it falls back to a raw-text scan with that `script` field blanked
   first. Carriers with no `gaps` structure to parse — `raw.json` (a result
   envelope whose `.result` is prose) and `code-gauntlet-report-*.md` — are
   raw-text scanned as-is; they never embed the bundle.
4. Plugin identity — when the Headless config echo carries `pipeline_version` and
   `plugin_root`, those receipts are validated against the repo's
   `workflows/pipeline.js` version and plugin root (primary). A complete valid
   echo receipt is sufficient even when no `pr_dir/workflows/wf_*.json` records
   were collected. When records exist, top-level Workflow `scriptPath` is also
   checked (defense in depth). Without a complete echo receipt, G4 requires
   collected workflow records and falls back to scriptPath-only (not `raw.json`,
   which is only the result envelope — parsed tolerantly for preamble/stderr).
5. ≥1 delivered comment across the run set

Plus one **reported stat, not a gate** — `--check` prints an `input_proof` line
built from each PR's `workflows/wf_*.json` `result.stats.inputProof` (issue #25
PR3): the verify stage's slice-input content-proof measurement, aggregated
across the run's PRs as `slices` / `proven` / `unproven` / `recovered` /
`rewritten` / `degraded`, plus `measured_prs` / `unmeasured_prs`. It stays a
stat rather than a sixth gate deliberately — a slice whose input never got
proven degrades to `origin=unknown`, which gate 3 already fails, so a second
verdict on the identical root cause would just double-count it. Read
structurally (`result.stats.inputProof`), never by regex — see gate 3's note
on why a wf record's raw bytes are unsafe to scan. Absent on any run recorded
before PR3 landed (and printed as `not measured`, never as zeros — a run that
was never measured is not the same fact as a run that measured zero drift).

A second reported stat, also **not a gate** — `--check` prints a `health` line
built from each PR's `workflows/wf_*.json` `result.stats.health` (issue #25
reqs 7-9): the delivered review's own health as the pipeline itself computed
it (`delivered` / `notChallenged` / `unclassified` counts, `dimensionsLost`,
whether verify's corroborating signals were fresh), aggregated across the
run's PRs as `measured_prs` / `unmeasured_prs` / `degraded_prs` plus the
summed counters and the union of `dimensionsLost`. This is a *different*
signal from the gate-3 banner-pairing failure condition above: that check is
derived directly from the persisted findings artifact and report, so it still
fires correctly on a run that collected no `wf_*.json` records at all — the
case where this stat reads `not measured` rather than a gate failure.

When a PR has more than one `wf_*.json` record (e.g. it was retried),
`_select_pr_health_snapshot` picks the snapshot to report by the record's own
`timestamp` field — **never** by sorted glob order. `wf_*.json` filenames are
`wf_<random>`, so that sort is arbitrary; on a measured run one four-record PR
sorted its OLDEST record (by 90 minutes) last. This is the same currency
problem issue #85 files against `_iter_workflow_records` generally (a
superseded record has already won a different gate's verdict once — a dead
first attempt outliving a clean `--retry-failed` rerun); this only narrows
the fix to the health snapshot. When no candidate has a usable timestamp,
the fallback prefers any record reporting `degraded: true` over a quieter
one, since under-reporting is the wrong direction to fail in for a
disclosure signal.

Exit code is the smoke verdict. The checker never imports or calls the scorer.
`--check` applies to skill runs only — naive-anchor runs are refused (exit 2).

### Reading a FAIL — the tier has a non-change failure floor

A red checker does **not** by itself mean the change under test is broken. Two
failure modes fire on clean code and both have been observed on branches that
were otherwise correct, so triage the reason before drawing a conclusion.

**`config_echo_mismatch` — 6 of 140 collected children (4.3%).** Reason
distribution across all 30 retained run dirs: `ok` 130, `config_echo_mismatch`
6, `plugin_mutated_by_child` 3, `is_error(success)` 1. The child renders the
Phase 1 identity receipt as prose in its final message (e.g.
`**plugin 3.2.2 · PIPELINE_VERSION 3.2.2**`) instead of the machine-readable
`pipeline_version=… (bundle)` / `plugin_root=… (resolved)` lines
`_echo_ok` parses from stdout, the `.result` envelope, or the report `.md`.
The review itself can be complete underneath it — on
`smoke-20260728-144630-a162ecd` the affected child returned `ok:true`, all 8
phases, zero gaps, a full artifact set and a captured dry-run payload. Confirm
that before re-running: an `invalid` with a complete artifact set is a
formatting miss, not a pipeline failure. (Distinct from
`workflow_backgrounded`, which the runner labels in
`bench/runner/invoke.py`'s `_workflow_backgrounded` — a status distinct from
any of `check.py`'s G1–G5 gates.)

**`origin=unknown` from artifact-writer transcription drift.** The writer is a
sampled agent, not a function (see CLAUDE.md, "The by-value writer is not
trustworthy"), and a corrupted slice-input file makes `verify_findings.py`
refuse the slice, degrading every finding in it to `origin=unknown` and
tripping gate 3. Measured on two consecutive smokes:
`smoke-20260727-205454-f99d948` (`receipt nonce mismatch`, plus
`artifact-content-proof` divergence on all 3 PRs and one assemble refusal) and
`smoke-20260728-144630-a162ecd` (one stray `}` appended after an otherwise
complete document, on 2 of 3 PRs — 23 findings lost, all recoverable with
`raw_decode`; see issue #69). Until that is fixed in the parser, expect gate 3
to be the tier's least stable gate, and diff the run's `wf_*.json` `gaps`
against the previous smoke before attributing it to the change under test.

Issue #25 PR3 targets exactly this: `verify_findings.py` now accepts a
complete document plus trailing bytes (the shape all measured corruption so
far has taken) and reports a content proof of what it parsed, so the workflow
can tell a proven-clean slice input from one it must re-materialize and retry.
A gate-3 FAIL on a run recorded after PR3 landed should be read alongside the
`--check` output's `input_proof` line before being attributed to the change
under test: `recovered` > 0 means a corrupted input was caught and repaired
(informational, not itself a failure); a slice counted in `degraded` is one
whose input stayed unproven even after a retry, and *that* is the gate-3
`origin=unknown` finding you are looking at. `input_proof: not measured` means
the run predates PR3 or the record could not be read — treat gate 3 the same
as before on that run.

**A gate-3 FAIL naming "no persisted code-gauntlet-report-\*.md carries the
health-degradation banner" is a different class from the two above and should
NOT be waved off as tier noise.** It fires on a PR whose findings artifact
already contains an unclassified finding (so the `origin=unknown` transcription
drift above may well be the underlying cause) but whose *report* additionally
failed to disclose that fact. That second half — `reviewHealth` /
`applyHealthBanner` in `workflows/src/stages.js` computing and prepending the
banner — is deterministic pipeline logic, not a sampled agent's transcription,
so a FAIL here on a run recorded after the banner landed points at a bug in
that logic (or in a resume path bypassing it), not at ordinary artifact-writer
drift. Cross-check the run's `health` stat line: `degraded_prs` > 0 with the
gate still failing means the pipeline *knew* the run was degraded and still
shipped a report without the banner.

CI: `.github/workflows/bench-smoke.yml` (`workflow_dispatch`) runs smoke then
`--check` on the newest run dir; the job fails if either step fails. Bare
mirrors under `bench/workspace/mirrors/` are cached on GH-hosted runners via
split restore/save (`actions/cache/restore` + `actions/cache/save`), keyed on
`bench-mirrors-${{ runner.os }}-${{ hashFiles('bench/golden/shas.json') }}-v2`
so new golden pins invalidate the cache. Save runs only when the smoke step
succeeded and the exact key missed — a failed checker still persists mirrors,
but an interrupted/partial populate cannot freeze a broken set under that key.
`ensure_mirror` also tears down unusable mirror directories and re-clones.
Several GB per upstream; a cache hit avoids cold clones; a miss remains
correct but slower. GitHub evicts caches after 7 days of no access.

## Named mini subset

Registered as `"mini"` in [`golden/subsets.json`](golden/subsets.json) — the
six pre-registered highest-golden-density gate PRs:

1. discourse-graphite#4
2. cal.com#11059
3. cal.com#14740
4. sentry#93824
5. grafana#79265
6. discourse-graphite#10

Resolve without archaeology:

```bash
python3 bench/run.py --tier mini          # run_id prefix mini-…
python3 bench/run.py --prs mini           # expands the same URLs; labels custom
```

## Baseline of record

Mini-subset A `custom-20260723-102149-381e9ff` — recall 19/30 = 0.6333, noise
0.2233, on the six pre-registered PRs above. Ledger cost: $77.73.

## Ledger-sourced costs

From `bench/experiments.jsonl`:

Rows with `auth_mode=subscription` are excluded — they are not API spend:

```text
smoke:        $21.11–$32.04 across 14 runs (mean ~$27, 16–22 min/PR)
mini-subset:  $84.18 (custom-20260723-070640-c1dd46f)
              $77.73 (custom-20260723-102149-381e9ff, of record: recall 19/30=0.6333, noise 0.2233)
full-15:      $190.72–$229.88
holdout:      $169.34 (holdout-20260721-085348-eec15be, recall 0.7407, noise 0.2095 — sealed for V3.2)
scoring:      ~$2/run;  M4 trivial fixture: ~$3
```

## Pre-registered owner measurement options

Preserved verbatim (no reliance on issue links):

```text
Mini-subset B (V3.1's cut M7): same 6 pre-registered PRs, --child-model sonnet,
  fully paired vs custom-20260723-102149-381e9ff; secondary metric =
  SKILL.md-adherence deviation count (D21); ~$85; interim orchestrator
  recommendation stands: inherit.
Full-15 subset promotion: confirms the 6-PR verdict at gate grade (~$230).
M4 trivial-PR fixture check: ~$3.
Holdout holdout-20260721-085348-eec15be (recall 0.7407, noise 0.2095):
  reserved exclusively for V3.2 fable.
```

## Watch ledger

```text
code-simplifier malformed StructuredOutput (PR-310 one-off) — not reproduced:
9 dispatches across the 3 V3.1 measured runs, zero events.

subscription usage-limit stall — unclassified: a limit hit is expected to block
until the window resets, which surfaces as a watchdog `timeout` and is therefore
indistinguishable from a genuine hang. Deferred deliberately: classifying it as
its own invalid reason needs a reliable transcript signature, and no subscription
run has produced one yet. `--retry-failed` after the reset recovers either way.
```

The code-simplifier watch-item drop relies on this fact surviving issue deletion.
The subscription entry is here for the same reason: it is the one idea the
`--child-auth` work deferred rather than built, and it would otherwise live only
in a closed issue. The observed failure mode, once a run shows one, belongs in
[`README.md` → Usage limits](README.md#usage-limits).

## Contributor rule

- **External PRs:** ship behind the always-on deterministic suites only
  (`python -m pytest tests/ -q`, `python -m pytest bench/tests/ -q`,
  `node --test workflows/test/*.test.js`, `pre-commit run --all-files`).
- **Functional smoke:** run by the release manager (not a contributor gate).
- **Paired mini / full-15 / holdout:** owner-triggered only.

See also [`CONTRIBUTING.md`](../CONTRIBUTING.md).
