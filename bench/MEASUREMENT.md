# Measurement protocol and status

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

The freeze binds its passing recomputed scan to an explicit baseline treatment SHA and
identical plugin tree, rather than requiring HEAD to be that SHA. It precedes
candidate runs. The checked-in contamination manifest remains unfrozen pending the
Wave 0a merge commit and owner-reviewed evidence. There is no untouched set for issue

# 46's own confirmation. Gate transcripts are read only to explain a lost bug after its

verdict. The final improvement claim is labelled as measured on a reused set.

## Validity and statistic

A window pins CLI binary/version, disabled auto-update, effective model controls,
judge pins and auth. Each arm separately pins its treatment, roster, models and
effort. Preflight precedes spend. Gate PRs run sequentially and each comparison
requires a complete PR-by-run matrix of independent review runs. The window freezes a
cross-arm run order. Selected attempts' start times must follow it. Rescoring creates
no new independent run. Selected attempts and their provenance remain immutable.

A first non-ok outcome is invalid and retryable. A second non-ok outcome for the same
PR/window is an all-missed operational failure on API, or on subscription when the
retry follows the operator-recorded reset. Diagnostic text need not match. Missing
optional attribution disables dimension/funnel evidence, never recall.

The unit is a golden bug. The statistic is the mean of per-bug differences between arm
detection means. A seeded PR-cluster bootstrap resamples whole PR clusters, weights
sampled bugs equally, and reports one-sided bounds and a two-sided 95% interval.
Stable-found means found in every run with the required minimum runs; stable-missed
means never found; intermediate outcomes are flaky. These are observed labels, not
assertions of population certainty. Category and overlapping dimension recall are
descriptive. Unavailable provenance remains unknown. Loss funnels retain the complete
denominator, including unavailable traces and operational failures.

## Audit and decisions

A seeded sampler uses one count for both arms, capped at 30 and at the smaller census.
An empty census makes sampled groundedness unavailable; recall is still reported and
the groundedness guard is inconclusive. Public auditor packets carry repository and
code references while hiding arm and score identities. Public packets and the private
arm map occupy separate directories. Sol and Opus seats run outside the bench with
repository access. Agreed labels merge directly; disputes enter an owner queue. A
resolved receipt binds the frozen sample, both label files and owner resolutions. Each
label file binds the exact public packet bytes the auditor reviewed. Changed,
incomplete or unresolved evidence is refused for an operational wave or simulation.

A wave passes when both uncontaminated gate and non-mini lower bounds clear the owner
margins, sampled groundedness is no worse than baseline, and no stable-found
uncontaminated bug is lost in every candidate run. A failure fails the wave; mixed
bounds yield inconclusive. A failed or inconclusive initial look may use the
registered extension once, preserving its initial prefix and binding the saved initial
report to the recorded verdict. Failed or inconclusive waves return to dev.

Before the first candidate run, the append-only [look ledger](golden/looks.jsonl)
registers the wave and frozen rule digest. Run preflight and comparison enforce that
registration. The compare command appends each operational verdict with its look
number, treatment SHAs, score references and report digest. The ledger allows a second
look only after fail or inconclusive and refuses every third look. Descriptive specs
cannot inspect gate populations. The look count comes from this ledger, including
failed looks; there are currently zero recorded looks.

The holdout opens once at issue #101 close through a receipt bound to the approved
final treatment SHA. The ledger refuses another unseal, another review use, another
audit sample, or another comparison. Audit and comparison verify the receipt and final
treatment, and refuse another SHA.

Final confirmation uses API 3+3, baseline versus final tip, gate only. The ship rule
requires non-inferiority on uncontaminated gate and non-mini at the owner margins. It
has no audit, stable-loss or extension requirement. Improvement is a point estimate of
at least +5 points and lower bound above zero, reported but not required. Passing the
ship line establishes the new baseline. Marketplace bumps are held from Wave 2a until
this line passes; failure returns to the last passing tip. Margins, counts and
bootstrap policy remain null and unfrozen until the owner fixes them after Wave 0b. No
current template can issue a verdict.

## Planning and spend

One empirical simulator resamples whole baseline run vectors and runs the complete
wave evaluator, including audit uncertainty, stable losses and a permitted extension.
It reports null pass rate, power at +5/+10 points, Monte Carlo standard errors, guard
counts, extension use and MDE on the declared effect grid. Groundedness uses a shared
beta latent rate from the resolved baseline sample, followed by independent same-size
arm samples. Judge noise buckets do not supply audit labels. Candidate uplifts flip
eligible misses diffusely; infeasible effects above the empirical ceiling are reported
explicitly. Auth-specific source windows remain attached to each vector. A mixed-auth
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

A2's one-time receipt matched prompts, schemas, models, dispatch order, artifacts and
the terminal envelope minus measurement against v3.42.49 (`839b1886`) with tracing off
and on, including oversized-trace fallback. Measurement is opt-in, outside primary
persistence and absent from ordinary prompts and schemas. The bench collects and
hashes the original task-file bytes. A missing terminal measurement status invalidates
a window attempt. The live known-loss smoke and measured trace overhead remain
unverified. CLI auto-update, effective effort and provider snapshot behavior still
need the orchestrator's installed-CLI check.

The dev-slice subscription run through scoring, audit, comparison and simulation, and
the two-mini A/A comparison remain external acceptance. Synthetic boundary fixtures do
not replace these receipts. Frozen margins, run counts, MDE, a passing baseline
scan/freeze, and resolved repository-backed baseline audit are pending.
