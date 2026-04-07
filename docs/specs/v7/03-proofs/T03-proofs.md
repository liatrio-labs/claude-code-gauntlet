# T03 Proofs: Fix cross-agent dedup — same-agent sibling bug + missing intent dimension

## Summary

Task: Fix issues in filter_findings.py cross-agent dedup.
Native ID: 154
Execution: 2026-04-05

## Requirements Addressed

| Req | Description | Status |
|-----|-------------|--------|
| R03.1 | Only drop different-agent findings; same-agent siblings of winner kept | PASS |
| R03.2 | `_CORE_DIMENSIONS` includes `"intent"` | PASS |
| R03.3 | Test: bug-detector has 2 findings + test-analyzer has 1 — both bug-detector survive | PASS |
| R03.4 | Test: bucket-boundary straddling (lines 12 and 13 with proximity=5) | PASS |

## Changes Made

### scripts/filter_findings.py
- `_CORE_DIMENSIONS` (line 568) — added `"intent"` to the set (R03.2)
- `_dedup_cross_agent` — after selecting winner, same-agent siblings of winner are now kept in `kept_ids` instead of dropped (R03.1). Added `winner_agent` extraction and a `loser_agent == winner_agent` guard that calls `kept_ids.add(id(loser))` and skips the drop logic.

### tests/test_filter_findings.py
- `TestGroupByProximity::test_bucket_boundary_straddling` — verifies lines 12 and 13 land in separate buckets (round(12/5)*5=10 vs round(13/5)*5=15) confirming they are NOT grouped together (R03.4 / L6)
- `TestDedupCrossAgent::test_mixed_agent_group_same_agent_siblings_preserved` — bug-detector(2 findings) + test-analyzer(1 finding) at same location: both bug-detector findings kept, only test-analyzer dropped (R03.3)

## Proof Artifacts

### T03-01-test.txt
- **Type**: test (pytest)
- **Command**: `python -m pytest tests/test_filter_findings.py -q`
- **Status**: PASS
- **Result**: 142 tests pass (140 pre-existing + 2 new)
- **Key Evidence**: Both new tests pass; no regressions

## Test Count

- Before: 140 passing
- After: 142 passing
- Delta: +2 new tests

## Technical Details

### Bug: Same-agent sibling dropped (H3)

**Before (buggy):**
```python
winner = ranked[0]
kept_ids.add(id(winner))
for loser in ranked[1:]:
    # ALL non-winners dropped, even same-agent siblings
    dup = dict(loser)
    dup["eliminated_by"] = "dedup:cross-agent"
    ...
    dropped.append(dup)
```

**After (fixed):**
```python
winner = ranked[0]
winner_agent = winner.get("agent", "").lower()
kept_ids.add(id(winner))
for loser in ranked[1:]:
    loser_agent = loser.get("agent", "").lower()
    # Keep same-agent siblings — only drop different-agent findings
    if loser_agent == winner_agent:
        kept_ids.add(id(loser))
        continue
    ...
    dropped.append(dup)
```

### Fix: _CORE_DIMENSIONS missing "intent" (M7)

**Before:**
```python
_CORE_DIMENSIONS = {"bug", "security", "cross_file_impact"}
```

**After:**
```python
_CORE_DIMENSIONS = {"bug", "security", "cross_file_impact", "intent"}
```

---
Status: COMPLETE
