# T03.4 Proof Summary

Task: T03.4 (#80) — Implement finding tagging and end-to-end output
File: scripts/filter_findings.py
Completed: 2026-03-31

## Requirements Verified

| Req | Description | Status |
|-----|-------------|--------|
| R03.4.1 | Tags findings as main-report or improvement-suggestion | PASS |
| R03.4.2 | Promotes test-analyzer functional correctness findings to main | PASS |
| R03.4.3 | Deduplicates overlapping test-analyzer findings | PASS |
| R03.4.4 | Complete pipeline produces valid output JSON | PASS |

## Proof Artifacts

1. **T80-01-cli.txt** — `python3 scripts/filter_findings.py --help` — Full usage with all features
2. **T80-02-test.txt** — Unit tests for tag_findings(), _is_test_correctness_finding(), _dedup_test_analyzer()

## Implementation Summary

### tag_findings() — agent-based routing

Replaced the dimension-based routing with agent-based routing per spec section 6d:

- **Main report agents**: bug-detector, security-reviewer, cross-file-impact[-analyzer], type-design-analyzer, conventions-and-intent (passes 1-2)
- **Improvement suggestion agents**: test-analyzer (unless promoted), code-simplifier, conventions-and-intent pass 3 (comment-accuracy dimension)

### Promotion rule

`_is_test_correctness_finding()` matches functional correctness patterns: race conditions, deadlocks, always-pass assertions, flaky tests, vacuous assertions, logic errors. Promoted findings get `promoted_from: "test-analyzer"` and `report_destination: "main"`.

### Dedup rule

`_dedup_test_analyzer()` compares test-analyzer findings against other agents at the same file within 5 lines. When overlap detected, test-analyzer finding is dropped with `eliminated_by: "dedup:test-analyzer"`.

### Pipeline changes

- `tag_findings()` now returns 4-tuple: `(findings, dedup_dropped, main_count, suggestion_count)`
- Stats now include `test_analyzer_deduped` and `test_analyzer_promoted`
- Each finding gets `report_destination: "main" | "suggestion"` plus `report_tag` (alias)

## Output JSON Schema (new fields)

```json
{
  "filtered": [
    {
      "report_destination": "main",
      "report_tag": "main",
      "promoted_from": "test-analyzer",  // only on promoted findings
      "promotion_reason": "..."           // only on promoted findings
    }
  ],
  "stats": {
    "test_analyzer_deduped": 0,
    "test_analyzer_promoted": 1
  }
}
```
