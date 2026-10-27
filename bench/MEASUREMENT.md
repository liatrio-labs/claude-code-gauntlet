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
| Paired mini-subset | `paired-mini` | `--tier mini` or `--prs mini` (6 PRs) vs baseline of record | Owner-triggered, only when a change plausibly moves recall/noise | $19–$85 / leg in the ledger ($19.47 on v3.42.49) | Gate-grade paired measurement |
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

Caveat on the "yes" rows: the first live subscription smoke has now run
(`smoke-20260812-030333-0f3f550`, 2026-08-12, main @ 0f3f550 / v3.8.0, `--tier
smoke`, 3 PRs sequential, `--child-auth subscription`). All 3 review children
ran to completion under the subscription token — no watchdog timeout, no
mid-run auth failure. N=1: what a usage-limit hit looks like in bench terms
remains unobserved. See [`README.md` → Usage limits](README.md#usage-limits)
for the full record.

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
   degrade. The report is not a carrier: it is rendered before persistence from data
   that carries only a gap count, so code-owned report text cannot hold the signal,
   although model prose can. G3 scans only the direct `workflows/wf_*.json`, `raw.json`,
   and `code-gauntlet-checkpoint-all-*.json` carriers. `raw.json` is scanned as text.
   The other carriers are judged from parsed `gaps` arrays and their string items alone;
   an empty array is clean. If a structured carrier cannot parse or has no `gaps` array,
   G3 falls back to a raw scan after blanking its `script` field.
4. Plugin identity — when the Headless config echo carries `pipeline_version` and
   `plugin_root`, those receipts are validated against the repo's
   `workflows/pipeline.js` version and plugin root (primary). A complete valid
   echo receipt is sufficient even when no `pr_dir/workflows/wf_*.json` records
   were collected. When records exist, the record's `script` bytes and
   `workflowName` identify the repo bundle (primary when present);
   `scriptPath` resolving to the repo bundle remains accepted for older records.
   Without a complete echo receipt, G4 requires collected workflow records and
   falls back to the record identity check (or scriptPath-only for corrupt
   records).
5. ≥1 delivered comment across the run set
6. Artifact completeness — findings, report, post-review, and checkpoint-all are
   each present exactly once, non-empty, and JSON members are parseable

Exit code is the smoke verdict. The checker never imports or calls the scorer.
`--check` applies to skill runs only — naive-anchor runs are refused (exit 2).

New outcomes also retain hashed immutable copies under
`attempts/<pr-key>/<attempt-id>/`; checkpoints select the latest attempt.
`--retry-failed` uses explicit attempt retryability when present, falling back to
`timeout`/`failed` for legacy checkpoints. Subscription retries require an
operator-recorded `--reset-confirmed-at` timestamp after failure completion.

`--resume` and `--retry-failed` both move each todo PR's prior `workflows/wf_*.json`
and prior-attempt deliverables into their respective `superseded/` directories
before re-invoking, so `--check` gates the current attempt only; forensic records
stay on disk.
Pre-fix run dirs that still hold
a failed attempt beside a success keep FAILing `--check` until retried under
this behavior.

### Reading a FAIL — the tier has a non-change failure floor

A red checker does **not** by itself mean the change under test is broken. Two
failure modes fire on clean code and both have been observed on branches that
were otherwise correct, so triage the reason before drawing a conclusion.

**`config_echo_mismatch` — the registry-knob echo floor.** This label means
the runner did not find every expected configuration knob in any accepted source;
it is separate from the G4 identity check for `pipeline_version` and `plugin_root`.
The report now carries the code-rendered knob receipt, so once the report is
materialized this floor is expected to die; Smoke B observes whether any
non-materialized path still needs the stdout or envelope fallback. Historically,
the child could complete the review while omitting one knob from its prose, so an
`invalid` with a complete artifact set was a formatting miss, not a pipeline
failure. (Distinct from
`workflow_backgrounded`, which the runner labels in
`bench/runner/invoke.py`'s `_workflow_backgrounded` — a status distinct from
any of `check.py`'s G1–G6 gates.)

**`failed` with `all_degraded` or `pipeline_failed` — the pipeline return envelope.**
`bench/runner/invoke.py` reads the child Workflow record's `result` after a
clean CLI exit. `all_degraded` comes from the `all-degraded:` prefix in
`result.error` or `result.gaps`; any other `ok: false` result is
`pipeline_failed`. These are failed, not invalid, because the pipeline itself
reported the outcome. They remain unscored but are retryable with
`--retry-failed`; an all-degraded result is self-describing and commonly
transient.

The prose identity receipt symptom belongs to **G4**, not this floor: G4 checks
that the code-owned `pipeline_version=… (bundle)` and `plugin_root=… (resolved)`
lines identify the active plugin, with Workflow `scriptPath` as defense in depth.

**Current verify input transport.** Verify slices now travel to the executor as a
percent-encoded `--input-inline` token planned under `VERIFY_INLINE_CHAR_BUDGET`;
the executor reproduces the token exactly in one Bash call; `verify_findings.py`
decodes it and writes the destination before running verification. The content
proof remains as a belt. The artifact-writer
still handles Persist's artifact paths; its historical slice-input transcription
failure is retained below as benchmark evidence:
`smoke-20260727-205454-f99d948` (`receipt nonce mismatch`, plus
`artifact-content-proof` divergence on all 3 PRs and one assemble refusal) and
`smoke-20260728-144630-a162ecd` (one stray `}` appended after an otherwise
complete document, on 2 of 3 PRs — 23 findings lost, all recoverable with
`raw_decode`; see issue #69). New runs should be diagnosed from the inline-token
and content-proof gaps rather than by expecting a verify-input writer label.

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

Claude Code CLI is installed in that workflow via a pinned
`npm install -g @anthropic-ai/claude-code@<version>` (same pin as
`validate.yml`). Revisit the pin when preparing each smoke checkpoint
(Smoke A / Smoke B / later) and record the CLI version alongside the
smoke result in the ledger — Dependabot does not see `run:` package
installs.

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

`mini-20261020-030509-839b1886` (v3.42.49, Claude 5.5 children, `api` auth) — recall
19/30 = 0.6333, noise 0.0649, valid-extra 0.7273, on the six pre-registered PRs
above. Ledger cost: $19.47. It replaces `custom-20260723-102149-381e9ff` (recall
0.6333, noise 0.2233, $77.73).

## Ledger-sourced costs

From `bench/experiments.jsonl`:

Rows with `auth_mode=subscription` are excluded — they are not API spend:

```text
smoke:        $21.11–$32.04 across 14 runs (mean ~$27, 16–22 min/PR)
mini-subset:  $84.18 (custom-20260723-070640-c1dd46f)
              $77.73 (custom-20260723-102149-381e9ff, former baseline: recall 19/30=0.6333, noise 0.2233)
              $54.92 (mini-20260901-170531-2cb9104, v3.24 post-#273 check: recall 19/30=0.6333, noise 0.1786)
              $51.02 (mini-20260903-204424-e8b3af7, post-#283 check: recall 20/30=0.6667, noise 0.1379)
              $19.47 (mini-20261020-030509-839b1886, of record: recall 19/30=0.6333, noise 0.0649)
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
citation absence preamble (#265) — instrument = `bench/runner/citations.py` (regex
heuristic, lower bound, sensitivity depends on agent wording; fixture-pinned on 40
hand-labeled corpus rows); measurement of record 2026-09-05 over local runs:
pre-#218 era 12/26 (46.2%); post-#218 era (mini-20260901-170531-2cb9104,
mini-20260903-204424-e8b3af7; every child echoed `conditionalSchema: true`) 7/14
(50.0%). The two eras draw on different PR mixes and per-PR rates range 0/4 to
6/6, so the aggregate cannot separate the schema change from composition; on the
PRs common to both eras the paired rate is flat or worse. Read-out instruction: at
Smoke B and the wave-end paired mini run `python3 -m bench.runner.citations <run_dir>`
and record the convention-dimension `rule_source` histogram for `documented_rule`,
`code_comment`, `repo_precedent`, and `self_inconsistency`.

code-simplifier malformed StructuredOutput (PR-310 one-off) — not reproduced:
9 dispatches across the 3 V3.1 measured runs, zero events.

subscription usage-limit stall — unclassified: a limit hit is expected to block
until the window resets, which surfaces as a watchdog `timeout` and is therefore
indistinguishable from a genuine hang. Deferred deliberately: classifying it as
its own invalid reason needs a reliable transcript signature, and no subscription
run has produced one yet. `--retry-failed` with a confirmed reset time permits a second attempt.
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

## Measurement protocol and status

The active epoch is `e616e84-v1`: 173 golden bugs over 50 PRs. Core is primary (158
full, 66 gate); all (173, 73) and strict (139, 58) are descriptive reported lines.
Scoring identity is independent of original review provenance. A golden correction
requires rescoring every run of both arms on the same corrected epoch. Historical
epoch scores remain recorded but do not enter active comparisons. Operating commands
and file shapes live in [README.md](README.md).

## Populations and contamination

| Population | Membership and use |
| --- | --- |
| gate | 15 PR decision set; no iterative tuning |
| gate-uncontaminated | Gate minus excluded PRs and matched sweep comments; primary verdict |
| gate-uncontaminated-non-mini | Uncontaminated gate outside the six mini PRs; separate guard |
| gate-contaminated | Reported without verdict weight |
| dev-slice | Six highest-core-golden uncontaminated dev PRs, canonical URL tie break |
| dev-pool | 23 uncontaminated PRs outside gate and holdout; iteration |
| mini | Six original mini PRs; descriptive A/A only |
| holdout | 10 PRs; one final-treatment unseal receipt at #101 close |

Named populations derive from the epoch's subsets and reviewed contamination manifest.
A named identifier or concrete scenario match excludes its entire PR. A narrow
registry sweep excludes only matched golden comments. A textbook bug class excludes
nothing and remains subject to the leakage lens's review.

Re-derived dispositions retain four excluded PRs: discourse-graphite#4 and
sentry-greptile#1 in gate; sentry-greptile#2 and keycloak#32918 in dev. Thirteen
sweep-comment references join by project, repository, PR number and epoch-local
ordinal to stable bug ids. The uncontaminated core gate has 50 bugs on 13 PRs; 21 bugs
are outside the mini. Dev exclusions precede ranking: 25 PRs become 23, with 57 core,
61 all and 48 strict bugs. The six highest-core uncontaminated PRs hold 23 core, 25
all and 23 strict bugs: discourse-graphite#1, #5 and #7, sentry-greptile#5,
cal.com#22532 and cal.com#7232. Textbook classes create no exclusions. Dev and mini
comparisons accept this reviewed unfrozen import; gate comparisons and simulation
require its frozen baseline/scan binding. Every comparison reports and binds each
resolved population's content hash.

The lexical guard scans agent and skill text, shared context, workflow literals, and
emitted source/bundle prompts and schemas. A finite exact vocabulary predicate
excludes ordinary terms. Allowances bind surface, source, exact passage/hash and
expected count. Unlisted and stale hits fail. From Wave 2a, identifier allowances must
be empty. Passage hashes stay in the policy with expected_count zero, so retained
passage text fails the scan. Holdout diagnostics use salted hashes and redact matching
lines. A holdout match must be deleted; its wording and suggested rewrites never enter
build briefs. Semantic dispositions are reviewed separately from a passing lexical
scan.

The freeze binds its passing recomputed scan to the baseline plugin input tree digest.
Evidence ledgers are outside that identity and HEAD does not identify a treatment.
Candidate dispatch captures the frozen contamination digest. Comparison requires
that digest and a freeze timestamp strictly before every candidate run. The checked-in
contamination manifest is frozen against the baseline tree at `465011c1`. There is no
untouched set for issue #46's own confirmation.
Gate transcripts are read only to explain a lost bug after its verdict. The final
improvement claim is labelled as measured on a reused set.

## Validity and statistics

A window pins CLI binary/version, disabled auto-update, effective model controls,
judge pins and auth. Each arm separately pins its treatment, roster, models and
effort. Preflight precedes spend. Gate PRs run sequentially and each comparison
requires a complete PR-by-run matrix of independent review runs. Each window is a block and freezes a
cross-arm run order. Selected attempts' start times must follow it. Rescoring creates
no new independent run. Selected attempts and their provenance remain immutable.

A first non-ok outcome is invalid and retryable. A second non-ok outcome for the same
PR/window and observed fingerprint is an all-missed operational failure on API, or on
subscription when the retry follows the operator-recorded reset. Diagnostic text need not match. Missing
optional attribution leaves discovery recall unknown. Delivered dimension recall
uses delivered candidate provenance. Funnels retain unavailable cells for every
selected run. When no eliminated discovery finding needs evaluation, attribution
makes no extra judge calls; missing evaluation costs remain null.

The unit is a golden bug. Core recall is primary; all and strict profiles are
reported descriptively. Category and overlapping dimension recall are descriptive.
Unavailable provenance remains unknown. Loss funnels retain the complete selected
denominator, including unavailable traces.

Create declarations with `python3 -m bench.runner.windows --population NAME`, two
`--arm NAME=sha:REV|NAME=worktree:PATH` arguments, `--run-indices`, `--auth-mode`,
`--duration-hours`, `--child-model`, and `--out`. Schema 2 supports local block plans;
schema-1 windows retain full-rule preflight capacity checks. Both schemas keep
`max_parallel_prs: 1`, serial PRs and immutable resume evidence.

A comparison's `window_paths` list declares balanced blocks, including reserved empty
blocks; `window_path` is the singleton alias. Each block uses its own local planned
prefix. Top-level arm score lists retain their order. Reports list lanes, local and
pooled indices, and zero-run blocks. Lanes may differ in auth and CLI identity.
Treatments, scoring context and the common wave/rule remain fixed. Extensions bind
initial score prefixes and every original block's bytes and local run assignments, then fill balanced reserved capacity or append blocks.
Aggregate capacity covers the initial and extension counts. One verdict records one look.

Comparison first averages each bug's Boolean detections over all independent runs in
its arm across blocks (never block means), then averages those rates over the population. A seeded percentile bootstrap
resamples whole PR clusters of paired bug-rate differences. It reports lower and
upper one-sided bounds at the rule's alpha and a descriptive two-sided 95% interval.
Fewer than two PR clusters cannot establish primary passage. Stability labels require
the rule's minimum run count; bugs found in every baseline run and no candidate run
are stable losses.

Each score execution records dedup, judge and adjudicator charges from response usage
at pinned prices. Attribution charges are separate. Unknown costs remain null.
Review costs come from captured invocation receipts. Historical rescoring remains
descriptive and does not establish attempt validity. Subscription review consumption
is recorded but is not API spend; scoring still runs on API. A paid projection above
$500 requires an owner ruling. No paid run is scheduled by this document.

## Audit and decisions

A seeded sampler binds the uncontaminated decision population. Gate comparisons use
two equal-size samples capped at 30 and the smaller uncontaminated census. Wave 0b
planning uses one baseline sample capped at 30, with the same receipt format.
An empty census makes sampled groundedness unavailable; recall is still reported and
the groundedness guard is inconclusive. Public auditor packets carry repository and
code references while hiding arm and score identities. Public packets and the private
arm map occupy separate directories. Sol and Opus seats run outside the bench with
repository access. Agreed labels merge directly; disputes enter an owner queue. A
resolved receipt binds the frozen sample, both label files and owner resolutions. Each
label file binds the exact public packet bytes the auditor reviewed. Changed,
incomplete or unresolved evidence is refused for an operational gate or simulation.

A wave starts with three runs per arm. Its PR-cluster bootstrap lower bound for
candidate minus baseline core recall must exceed -7 points, using one-sided
stage alpha 0.0294 at both looks. It passes early when that bound clears and every harm
check passes. It fails early when the recall point estimate is below -7 points or a
harm check fails. Otherwise it adds three runs per arm, preserving the initial
prefix and saved report, and passes only if the primary bound clears without harm.
An inconclusive harm check is recorded at the final look and does not fail it.
The final look always passes or fails. A fail is final.

Secondary checks fail only on evidence of harm: the non-mini recall upper bound is
below zero, at least two bugs found in every baseline run are found in no candidate
run, the pooled core strict-precision PR-cluster bootstrap upper bound is below zero,
or the blinded audit's Newcombe hybrid Wilson upper bound for correct share is below
zero. Upper bounds use one-sided alpha 0.05. This avoids requiring small secondary
samples to prove equality. Simulation passed about 1.5 percent when truly 7 points
worse, 76 percent when equal, and 91 percent at +5, averaging about 3.8 runs per arm
across equal and +5 scenarios. These rates depend on the empirical packet library
and proxy audit labels.

Before the first candidate run, the append-only [look ledger](golden/looks.jsonl)
registers the wave, frozen rule digest and baseline plugin input tree digest with
`python3 -m bench.runner.looks --wave WAVE --rule RULE.json --baseline-treatment-sha DIGEST --contamination contamination.json`.
For each wave, pin the accepted main baseline and create fresh paired windows. Retain
reviewed exposure exclusions; if the baseline or dispositions changed, use the existing
`bench.runner.populations --spec freeze-spec.json --out contamination.json` freezer on
that clean baseline before any candidate run, and pass those frozen bytes throughout.
Preflight refuses a missing registration or mismatched baseline freeze. Only a declared
source substitution adds `--baseline-source-sha SOURCE`; each baseline run then needs
an equivalence receipt binding `release_sha=SOURCE`, `merge_sha=DIGEST` and evidence refs.
The compare command appends each operational verdict with its look
number, treatment SHAs, score references and report digest. A second look requires an
extendable initial report and its unchanged prefix; every third look is refused. Descriptive specs
cannot inspect gate populations. The look count comes from this ledger, including
failed looks; there are currently zero recorded looks.

The holdout opens once at issue #101 close through a receipt bound to the approved
final plugin input tree digest. After preflight, the ledger reserves review use for
one run identity and permits its resume and retry. It refuses another unseal, another
independent review run, another audit sample, or another comparison. Audit and
comparison verify the receipt and final treatment, and refuse another SHA.

Final confirmation uses 3+3 on API or subscription auth, baseline versus final tip,
gate only. The ship rule requires primary and non-mini recall lower bounds to exceed
their negative owner margins. It also uses the harm checks and requires a resolved
audit. It has no extension. Improvement is a point estimate of
at least +5 points and lower bound above zero, reported but not required. Passing the
ship line establishes the new baseline. Marketplace bumps are held from Wave 2a until
this line passes; failure returns to the last passing tip.

## Planning and spend

One empirical simulator resamples whole baseline run vectors and runs the complete
wave evaluator. Source lanes retain their own window evidence and counts. Pooling
assumes source lanes are comparable; this is planning evidence, not a blocked verdict.
It reports null pass rate, power at +5/+10 points, Monte Carlo standard errors, guard
counts, extension use and MDE on the declared effect grid. Groundedness uses a shared
beta latent rate from the resolved baseline sample, followed by independent same-size
arm samples. Judge noise buckets do not supply audit labels. Candidate effects flip
eligible misses to hits or hits to misses diffusely; effects outside the empirical
recall floor and ceiling are reported explicitly. Precision uses sampled packets
unchanged, and both looks reuse the same simulated audit. Auth-specific source windows
remain attached to each vector. A mixed-auth
library assumes comparable review distributions and reports its auth counts.

With K=3, empirical per-bug rates have 1/3 granularity: 0, 1/3, 2/3, or 1. Resampling
cannot reveal failures absent from that finite library and understates unbiased run
variance by the factor (K-1)/K. MDE is grid-based, not a continuous estimate. Diffuse
flips cannot model a particular prompt's correlated effects. Simulation is planning
evidence and never authorizes promotion. Actual auditor behavior and positive-effect
power remain unverified until measured.

Review spend counts each immutable attempt once, including retries. Dedup, judge and
adjudicator scoring charges count each score execution once. Attribution is reported
separately. Unknown costs remain null. Subscription review consumption is recorded but
is not API spend; its scoring still runs on API. Re-project spend after Wave 0, Wave
2b and Wave 3. A paid projection above $500 requires an owner ruling. No paid run is
scheduled by this document.

## Recorded status

The historical baseline mini has 19/30 recall, 56/77 valid-extra and 5/77 noise on its
historical epoch. Free replay reproduced the buckets; missing pair judgments prevent
exact recall replay. Both new-epoch mini rejudgments, rebuilt anchors and judge
variability/noise calibration remain external acceptance. No new paid result is
asserted here.

## Windowed run validity

The runner accepts a JSON measurement window with explicit arm and planned run
index. It checks shared CLI, scoring, epoch and auth controls, per-arm plugin and
model treatment, time bounds and the requested PR set before mirror work. PRs run
sequentially. Resume and retry require the same window path, bytes and provenance.
The plugin identity is the digest of tracked index entries under `.claude-plugin`,
`agents`, `skills`, `workflows` and `scripts`; it is independent of HEAD and bench
ledger changes. See [README.md](README.md#measurement-windows-and-immutable-attempts)
for the flags and artifact layout.

A first non-ok attempt is invalid and retryable. A second non-ok attempt for the
same PR, window and observed fingerprint is an all-missed operational failure on
API, or on subscription after the operator-recorded reset. Diagnostic text need
not match. Attempt snapshots preserve artifact hashes, model provenance and
consumption. Selected attempts and review provenance remain immutable; rescoring
creates no new independent review run.

The runner collects stage traces and records their availability. Windowed skill
attempts require a terminal measurement availability status; stage execution
failures invalidate an attempt even without optional traces. Unavailable traces
leave attribution unknown. A baseline-equivalence receipt is copied to the run
manifest as supplied JSON, without content validation by this runner.

The dev-slice subscription run through scoring, audit, comparison and simulation, and
the two-mini A/A comparison remain external acceptance. Synthetic boundary fixtures do
not replace these receipts. MDE and a resolved repository-backed baseline audit remain
pending. The rule and contamination manifest are frozen.
