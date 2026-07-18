# CHANGELOG

<!-- version list -->

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
