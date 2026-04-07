# T02 Proof Summary

**Task:** Fix apply_challenges.py — security handling, dead code, schema, ranking
**Timestamp:** 2026-04-05T00:00:00Z
**Model:** sonnet

## Requirements Covered

| Req | Description | Status |
|-----|-------------|--------|
| R02.1 | Score < 25 + security dimension downgrades severity instead of removing | PASS |
| R02.2 | Cap-dropped findings have elimination_reason in eliminated list | PASS |
| R02.3 | Output uses 'findings' key (not 'filtered') to match post_review.py input | PASS |
| R02.4 | Ranking uses risk_level as tertiary key when available, description-length as fallback | PASS |

## Proof Artifacts

| File | Type | Status |
|------|------|--------|
| T02-01-test.txt | test | PASS |

## Test Results

- 62 tests pass (53 pre-existing + 9 new)
- Full suite: 455 tests pass (zero regressions)

## Changes Made

### scripts/apply_challenges.py
- R02.1: Added `is_security` branch in score < 25 path — security findings (dimension="security") are downgraded instead of removed; already-at-low security findings still removed
- R02.2: Fixed dead code in cap loop — created `cap_dropped_elim` list, appended annotated `elim` dicts inside loop, used this list in `all_eliminated`; removed the redundant list comprehension
- R02.3: Changed output key from `"filtered"` to `"findings"` to match post_review.py input schema
- R02.4: Changed `_rank_key()` tertiary sort key to use `risk_level` field when present (negated for descending order), with description length as fallback

### tests/test_apply_challenges.py
- Added `TestApplyChallenges` tests for R02.1: security downgrade, security-at-low-removes, non-security still removes, score-0 edge case
- Added `TestRankFindings` tests for R02.4: risk_level tiebreak, fallback to description, risk_level beats description
- Added `TestCapAnnotation` class for R02.2: elimination_reason present, cap size in reason
- Updated `TestMainCLI`: changed all `result["filtered"]` references to `result["findings"]`; added `assertNotIn("filtered")` assertions
