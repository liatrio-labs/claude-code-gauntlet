# Measurement policy (canonical)

This is the in-repo home for the ratcheted measurement policy (Issue #28).
Maintainer work-queue items link here rather than restating the tiers — see
[`docs/maintainer-issues.md`](../docs/maintainer-issues.md).

**Supersedes** the framing "every behavior-changing item ships behind a paired
bench measurement." Paired measurements are rare, expensive, owner-triggered
events — not the default gate for every change.

## Tier ladder

| Tier | Slug | What | Trigger | Cost (ledger) | Purpose |
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

## Functional smoke (mechanical)

Smoke recall is noise (0.0–0.75 swing across 14 smoke rows on ~4 goldens). The
verdict is **mechanical**, never the judge:

```bash
python3 bench/run.py --tier smoke
python3 bench/run.py --check <RUN_ID>
```

`bench/run.py --check RUN_ID` implements the mechanical smoke gates:

1. Completeness — every `run.json` `pr_urls` entry has terminal status `ok`
2. Payload parse + adapter-required fields + union-schema findings check
   (requires ≥1 `code-gauntlet-findings-*.json` per PR)
3. Zero `origin=unknown` findings; no writer no-write-proof / partial-artifacts
   degrade (scans compact-return carriers `workflows/wf_*.json` + `raw.json`,
   plus report / `code-gauntlet-checkpoint-all-*.json`)
4. Child `scriptPath` under the repo's `workflows/pipeline.js`, read from
   collected `pr_dir/workflows/wf_*.json` records (not `raw.json`, which is
   only the result envelope). Upgrades to `pipeline_version`/`plugin_root`
   echo receipts when environment-purity receipts land — Issue #23
5. ≥1 delivered comment across the run set

Exit code is the smoke verdict. The checker never imports or calls the scorer.
`--check` applies to skill runs only — naive-anchor runs are refused (exit 2).

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
```

The code-simplifier watch-item drop relies on this fact surviving issue deletion.

## Contributor rule

- **External PRs:** ship behind the always-on deterministic suites only
  (`python -m pytest tests/ -q`, `python -m pytest bench/tests/ -q`,
  `node --test workflows/test/*.test.js`, `pre-commit run --all-files`).
- **Functional smoke:** run by the release manager (not a contributor gate).
- **Paired mini / full-15 / holdout:** owner-triggered only.

See also [`CONTRIBUTING.md`](../CONTRIBUTING.md).
