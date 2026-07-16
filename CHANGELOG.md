# CHANGELOG

<!-- version list -->

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
