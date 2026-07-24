# CHANGELOG

<!-- version list -->

## v3.1.2 (2026-07-24)

### Bug Fixes

- **bench**: Catch writer degrade on compact-return carriers
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

- **bench**: Codify measurement policy with mini subset and smoke checker
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

- **bench**: Cover GitLab/union-schema paths and align mini dashboard bits
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

- **bench**: Cover wf collect wiring and grade --tier mini in report
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

- **bench**: Ignore nested verify scriptPath in smoke G4
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

- **bench**: Point smoke checker at real workflow records
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

- **bench**: Stop incomplete mirror caches from poisoning CI
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

### Chores

- Apply markdownlint-cli2 autofix to CHANGELOG.md
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))


## v3.1.1 (2026-07-23)

### Bug Fixes

- Apply markdownlint fix to CHANGELOG blank lines
  ([`bcd543b`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/bcd543b4b131b0b4ada93260b2c43aa995b767f9))

### Documentation

- Add AGENTS.md with Cursor Cloud dev environment setup
  ([`bcd543b`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/bcd543b4b131b0b4ada93260b2c43aa995b767f9))

- Add AGENTS.md with Cursor Cloud setup instructions
  ([`bcd543b`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/bcd543b4b131b0b4ada93260b2c43aa995b767f9))

- Clarify markdownlint-fix CHANGELOG gotcha in AGENTS.md
  ([`bcd543b`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/bcd543b4b131b0b4ada93260b2c43aa995b767f9))

## v3.1.0 (2026-07-23)

### Bug Fixes

- **agents**: Schema-declared extras are omit-not-null; changelog markdownlint fix (Bugbot PR-20
  wave 1)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **args,docs**: Shape-guard exclusionPatterns like reviewConfig.ignore; correct stale post-c8 doc
  claims (adversarial review)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **guard**: Mirror stage defaults for absent size limits — agent-count guard can never go
  NaN-silent (#17 item 8)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **skill**: AgentFlags is a derivation rule at assembly time + pre-dispatch light-scope check (M4
  live miss)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **skill**: Ground the light-scope answer in a fresh env re-read at assembly (M4 second miss)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **summarize**: Pin changedLines in the bucketed-path merge prompt too (Bugbot PR-20 wave 2)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

### Chores

- **bench**: Ledger + report for M3 union smoke (smoke-20260723-033811-6ea1737)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **bench**: Ledger + report for M3-of-record smoke-20260723-051739-1c6a310
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **bench**: Ledger + report for mini-subset A (custom-20260723-070640-c1dd46f, FAILED paired bar ->
  item-4 surgical revert)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **bench**: Ledger + report for mini-subset A re-run (custom-20260723-102149-381e9ff) — V3.1
  comparison row
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

### Documentation

- Qualify anchor noise-rate comparability in benchmark results
  ([#18](https://github.com/liatrio-labs/claude-code-gauntlet/pull/18),
  [`9090838`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9090838301f974bbeadb8fdd4b62b0fdb6db29ab))

- **bench**: Reframe the report as a release-progression artifact; README results as v2 -> v3.0 ->
  v3.1
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **delivery**: Let-me-pick deselections apply to the prIdentity wrapper path too (Bugbot PR-20)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **readme**: Add the v3.1 paired mini-subset row to the benchmark table
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

### Features

- Code-gauntlet v3.1 benchmark-gated hardening + orchestrator-model pinning
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **agents**: Remove v2 NDJSON emission contract from discovery agents — v3 by-value output is the
  sole path (live-run L10)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **args**: Validation mirrors consumption — require changedFiles/changedLines, demote
  changedFilesPath to optional provenance (#17 item 6)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **challenge**: Structured blind location (file/line) replaces dead code field; prompt return-shape
  matches schema (#17 item 1 + wave-3 rider)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **hardening**: Waist validation for reviewConfig, entry-args guard, PR-identity delivery wrapper,
  legacy REVIEW.md self-heal, info/exclude, failingPhase (live-run L1-L9)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **policy**: Pin explicit full model IDs through resolvePolicy — agent pins immune to orchestrator
  session variant (#17 V3.1)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **scope**: Wire light scope — deep flag gates the seven non-core dimensions, full scope
  byte-identical (#17 item 7)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **verify**: Echo content-fidelity gate, numeric confidence end-to-end, deterministic agent/extras
  echo, writer write-proof (#17 items 2,3,4,5)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

## v3.0.0 (2026-07-23)

### Bug Fixes

- **adjudicator**: Recover verdict replies with unescaped inner quotes [owner-approved]
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Child-model default back to inherit — both sonnet variants measured worse
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Classify killed-background workflow distinctly (not config_echo_mismatch)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Invoke namespace-qualified /deep-review:deep-review
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Uncap CLI background-wait ceiling so long Workflows aren't killed
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **build**: Strip duplicate trailing newline in generated bundle
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: CRLF bug in loadExclusions bullet-list fallback + review nits
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: PyIntStrict accepts booleans, matching Python int(bool)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **pipeline**: Apply the agent-count guard — wire coarsenLimits into runWith (Bugbot)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **pipeline**: Guard counts absent challengeCap as challenge-all, matching the stage (Bugbot)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **pipeline**: Land Bugbot #1 (validate content) and #3 (agent short-name)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **pipeline**: Land final-review findings — pin numerics on UNVERIFIED path, extract modelFor
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **release**: Bump PIPELINE_VERSION at its source, not just the bundle
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **release**: Remove unused meta.version instead of patching its drift
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Preserve finding description through verify; exempt dashboard from mutation guard
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Repair five smoke-revealed runtime defects + runner integrity guard
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Align every dispatch to the platform agent()/parallel() contract
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Args-waist rename contract comments + 3 tests
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Correct entry contract per live Workflow-tool probe
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Make discover degradation reachable (null agent -> dims degraded)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Materialize verify slice inputs; resume state on failure paths
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Normalize agent field names in validate/challenge stages
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Persist v2 aliases, checkpoint round-trip, report segmentation
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

### Chores

- Fix pre-commit formatting (markdownlint, end-of-file-fixer)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: BUILD_GATE — fresh bundle, all suites green
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

### Continuous Integration

- Fix semantic-release OIDC subject after repo rename
  ([#19](https://github.com/liatrio-labs/claude-code-gauntlet/pull/19),
  [`8893dcd`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8893dcd8b65839f1a0dd11062a51c31b2a1e4d7c))

### Documentation

- Add Phase 3 workflow wait protocol to SKILL (poll to terminal before Phase 8)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- Fix markdownlint failures blocking merge
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- Full README pass for the v3 launch + CI job for the JS suite
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- Reconcile SKILL v3 prose to workflow behavior (triage descriptive, resume state, phase refs)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- SKILL.md v3 workflow invocation + CLAUDE.md JS rules
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **artifact-writer**: Document postReview in the final-artifacts payload shape
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Addendum — workflow entry contract pinned during build
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Draft provisional D2 pending owner confirmation
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Finalize Phase 0 gate — decisions D1-D3 recorded
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Online validation addendum for build/bench discoveries
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record agent() failure-contract and queueing probes (tests 5, 10)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record headless executor permission matrix (test 4, five legs)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record model/effort routing probes and D3 (tests 6, 7, 16)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record semantic-release lockstep probe (test 14)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record size-limit and owner-gated probe results (tests 8, 9, 11, 12, 15)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record unattended executor-permission data point (test 4 partial)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record workflow invocation probe results (tests 1-3, 13)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Scaffold v3 Phase 0 smoke-test findings artifact
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **skill**: Include top-level checkpoints in the compact-return shape
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **skill**: Mark light/trivial scope as unwired in v3.0 (Bugbot)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

### Features

- Code-gauntlet v3 (formerly deep-review) — workflow-native pipeline
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- Rename plugin to code-gauntlet (formerly deep-review)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Add --prs explicit golden-PR list override
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Add v2-vs-v3 verdict panel + normalized efficiency
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Allow 1m-context child-model variants
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Bring every dashboard section current with the full ledger
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Expose holdout tier in run.py CLI
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Make the report dashboard legible to share
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Pin child-session model per tool (Gate-2 config)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Wire deep-review-v3 into the frozen bench harness
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Calibrate discoverPrompt against agent self-censoring (hill-climb iter 2)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Deliver all challenge-survivors, deterministic Phase 8 selection (hill-climb iter 4)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Delivery tier as a user choice, default all (hill-climb iter 4, refined)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Enrich discovery dispatch prompt to v2-grade elicitation
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Iter-5 quality — non-security threshold 55, challenge teeth, discovery sweeps
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Scope calibration paragraph to bug-detector only (hill-climb iter 3)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: ValidateArgs accepts optional delivery selector (iter 4 delta)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **verify**: Receipt + executor agent + UNVERIFIED degradation path
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Args waist + argsVersion validation
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Bundle build system, entry contract, and version_variables
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: DIMENSIONS registry + S5 policy resolver
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Report/writer/checkpoints, top-level orchestration, boundary parity
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Stages 1-3 (summarize/discover/merge) with degradation + agent-count coarsening
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Validate/filter/challenge stages with degradation semantics
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

### Performance Improvements

- **pipeline**: Gate-2 overhead-only token cuts (validate batch, slim checkpoint, writer dedup)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

### Refactoring

- **policy**: Remove the frontier mode — ship the single benchmarked policy
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

### Testing

- **bench**: Resume honors manifest tool over CLI default
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: ApplyChallenges twin (comparator/deep-clone/dedup reuse)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: ApplyValidations twin (int-strictness trap)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: Dual-runtime parity harness + findingDedup twin
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: FilterFindings twin part 1 (thresholds/injection/exclusions)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: FilterFindings twin part 2 (disagreement/dedup_cross_agent, pyRound)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: MergeFindings twin
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: Scanner edge fixtures + divergence notes
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

## v2.6.0 (2026-07-18)

### Bug Fixes

- **bench**: Actionable score_run failure surface, naive-anchor costs, nested payload probe
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Adjudicator retries non-object JSON; string line normalized before hunk slicing
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Collision-proof per-PR dirs, judge-pin fallback, naive reason and shape validation
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Contain OSError and cleanup failures per-PR; smoke workflow gets actions:write for
  artifact upload ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Exclude capacity fields from token usage sums
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Failed-run reason names is_error, not the envelope subtype
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Fetcher pins merge-base as base_sha; anchor adjudicator iterates candidate records
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Forward gh auth into the isolated context; single canonical envelope parser
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Harden run loop against missing-SHA sentinels and clone failures; nonzero exit on
  failed runs ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Load metered key from bench/.env into the claude invocation env
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Naive-anchor structured output contract wired through the adapter
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Per-PR containment of unexpected errors; per-candidate adjudication identity
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Pre-clean stale scorer stage outputs; calibrate watchdog to 45m
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Re-pin 4 base SHAs to mirror merge-base (API base.sha was branch tip, not branch point)
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Self-heal stale worktrees on resume; naive invocation labeling; single dotenv parser
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Single canonical dotenv reader for prereqs and build_env
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Wire --score-only to score_run
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **headless**: Config-echo receipt must reach the final message and report; invoke accepts all
  three sources ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **headless**: Dry-run stdout reports capture, not posting
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **headless**: Env-enforced dry-run in post_review; robust naive fence parse; shared scorer stage
  runner ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **headless**: Review closed/merged PRs, never checkout live heads; fix stale payload_path
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

### Code Style

- Pre-commit fixes; exclude bench/golden fixtures from markdownlint; JSON writers emit trailing
  newline ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

### Continuous Integration

- Bench self-tests on PR; live smoke via workflow_dispatch
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- Pin publish-to-marketplace reusable workflow to @main
  ([#13](https://github.com/liatrio-labs/claude-deep-review/pull/13),
  [`53cd5be`](https://github.com/liatrio-labs/claude-deep-review/commit/53cd5be1ff4a0465c13029fd92f6800ad6efb591))

- Restore last-known-good publish-to-marketplace pin for diagnosis
  ([#14](https://github.com/liatrio-labs/claude-deep-review/pull/14),
  [`8ecd975`](https://github.com/liatrio-labs/claude-deep-review/commit/8ecd975dfa964082d6c3e2a3a20f9de6ef0ea24c))

- Trigger marketplace publish on release and workflow_dispatch
  ([#12](https://github.com/liatrio-labs/claude-deep-review/pull/12),
  [`c2ee576`](https://github.com/liatrio-labs/claude-deep-review/commit/c2ee576cf4de887747ccb78b939f2d771fa7bd17))

### Documentation

- **bench**: Stranger quickstart README
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **research**: P1 bypass probe completed — all three modes confirmed
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **research**: Record harness headless probes (artifact 33)
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

### Features

- Bench harness + headless review mode (v3 pre-work)
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Anchors re-judged under pinned judge
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Committed performance dashboard (report.py generates report.html from the ledger)
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Fetch-and-pin per-PR head/base SHAs
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Freeze baselines and protected-path SHAs
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Headless invoker with watchdog, invalid-run detection, cost capture
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Mirror+worktree lifecycle with SHA drift guard
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: One-command runner ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Payload-to-candidates adapter
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Pin judge to claude-opus-4-8 alias (no dated 4.8 exists; discrepancy logged)
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: PR-granular checkpointing and append-only ledger
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Re-pin judge to claude-opus-4-5-20251101 (4.8 rejects temperature; spec H5 jointly
  unsatisfiable) ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Record k=5 judge determinism (judge_sd = 0)
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Record smoke, naive-anchor, and v2 baseline ledger rows
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Scaffold bench/ and env template
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Scoring orchestration, frozen adjudicator, judge pin
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Sign delta_noise=0.24; final protected-path freeze
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Vendor anchor candidates + judge plumbing spot-check
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Vendor golden data and pinned subsets
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Vendor pinned scorer (dedup+judge) with attribution
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **headless**: Add --dry-run payload capture to post_review.py
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **headless**: DEEP_REVIEW_HEADLESS env contract + gate carve-outs
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

### Testing

- **bench**: Fix module reference in capacity-field test
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Local headless E2E fixture
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

## v2.5.0 (2026-07-16)

### Bug Fixes

- Address CodeRabbit review nitpicks
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

- Address PR #5 review loose ends (dropped_no_id, dedup test, docs)
  ([#8](https://github.com/liatrio-labs/claude-deep-review/pull/8),
  [`8efe604`](https://github.com/liatrio-labs/claude-deep-review/commit/8efe60432bea276c26f2a0f11e00c274667ca897))

- Address review feedback on finding dedup module
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

- Give change-summarizer a non-empty tools list for Claude Code 2.1.211+
  ([#10](https://github.com/liatrio-labs/claude-deep-review/pull/10),
  [`4976fa0`](https://github.com/liatrio-labs/claude-deep-review/commit/4976fa04aa1cdd67459e4129268ea4b2c8f7f607))

- Resolve 3 blocking issues from leehopper review
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

- **merge_findings**: Count dropped_no_id from pre-validation findings
  ([#8](https://github.com/liatrio-labs/claude-deep-review/pull/8),
  [`8efe604`](https://github.com/liatrio-labs/claude-deep-review/commit/8efe60432bea276c26f2a0f11e00c274667ca897))

- **merge_findings**: Ensure scripts/ on sys.path for direct invocation
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

### Chores

- Strip trailing whitespace in finding_dedup.py
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

### Continuous Integration

- Bump publish-to-marketplace workflow SHA to 933e23c
  ([`bbeb92b`](https://github.com/liatrio-labs/claude-deep-review/commit/bbeb92bf54bdd1e237c56b61059cfb88afa5a1fc))

- Publish to marketplace after semantic release
  ([`4761e94`](https://github.com/liatrio-labs/claude-deep-review/commit/4761e94e50c425a03f28b4e9cf226be5eb1d3440))

- Unblock semantic-release by decoupling marketplace publish
  ([#11](https://github.com/liatrio-labs/claude-deep-review/pull/11),
  [`5f8aa87`](https://github.com/liatrio-labs/claude-deep-review/commit/5f8aa873a4ec6758dd61f1c2407792e149b1a185))

### Documentation

- Add privacy policy
  ([`3b821f5`](https://github.com/liatrio-labs/claude-deep-review/commit/3b821f5f1c8cb714c4864ed5d72a1376094597c4))

- **claude-md**: Add finding_dedup.py to pipeline-script list
  ([#8](https://github.com/liatrio-labs/claude-deep-review/pull/8),
  [`8efe604`](https://github.com/liatrio-labs/claude-deep-review/commit/8efe60432bea276c26f2a0f11e00c274667ca897))

- **finding_dedup**: Show standalone and pytest import forms
  ([#8](https://github.com/liatrio-labs/claude-deep-review/pull/8),
  [`8efe604`](https://github.com/liatrio-labs/claude-deep-review/commit/8efe60432bea276c26f2a0f11e00c274667ca897))

### Features

- Add standalone finding deduplication module with cross-session persistence
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

### Refactoring

- Extract dedup_by_id from merge_findings into standalone module
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

- Scope PR to dedup_by_id extraction only (route A)
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

### Testing

- Avoid hardcoded path in merge_findings import regression
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

- **finding_dedup**: Pin first-wins equal-priority collision invariant
  ([#8](https://github.com/liatrio-labs/claude-deep-review/pull/8),
  [`8efe604`](https://github.com/liatrio-labs/claude-deep-review/commit/8efe60432bea276c26f2a0f11e00c274667ca897))
