# T04 Proof Summary

**Task**: Fix merge_findings.py truncation detection + clean up dead code
**Status**: COMPLETED
**Timestamp**: 2026-04-05T00:00:00Z

## Requirements

| ID     | Description                                                        | Status |
|--------|--------------------------------------------------------------------|--------|
| R04.1  | Truncation detection uses pre-validation NDJSON counts             | PASS   |
| R04.2  | No truncation warning when NDJSON empty but text has valid JSON    | PASS   |
| R04.3  | No unused imports, constants, or variables                         | PASS   |
| R04.4  | Test temp directories cleaned up in tearDown                       | PASS   |

## Proof Artifacts

| File                  | Type | Status | Notes                              |
|-----------------------|------|--------|------------------------------------|
| T04-01-test.txt       | test | PASS   | 67/67 tests pass (63 baseline + 4) |

## Changes Made

### scripts/merge_findings.py

- **L1 (dead code removed)**:
  - Removed `import warnings` (unused)
  - Removed `_JSON_BLOCK_RE` regex constant (unused)
  - Removed `text_count_raw` variable (unused)

- **M4 (pre-validation NDJSON counts)**:
  - `detect_truncation()` signature changed: replaced `ndjson_findings` dict
    parameter with `ndjson_raw_counts: dict[str, int]` (pre-validation counts)
  - In `merge()`, compute `ndjson_raw_counts` before validation filtering
  - Pass `ndjson_raw_counts` instead of post-filtered `ndjson_findings` to
    `detect_truncation()`

- **M5 (text fallback check)**:
  - `detect_truncation()` gains new `text_findings` parameter
  - Truncation condition now requires `text_empty` (no valid JSON in text) in
    addition to `ndjson_empty`, `has_prose`, and `not has_skip`
  - In `merge()`, preserve `text_findings_pre_validation` before filtering and
    pass it to `detect_truncation()`

### tests/test_merge_findings.py

- **L2 (new false-positive tests)**:
  - `TestDetectTruncation.test_m4_no_false_positive_when_ndjson_has_invalid_findings`
  - `TestDetectTruncation.test_m5_no_false_positive_when_text_has_valid_json_blocks`
  - `TestMerge.test_m4_no_false_positive_when_ndjson_findings_fail_validation`
  - `TestMerge.test_m5_no_false_positive_when_text_has_valid_json_blocks`

- **L5 (tearDown cleanup)**:
  - Added `tearDown` to `TestMerge` — calls `shutil.rmtree(self.tmpdir, ignore_errors=True)`
  - Added `tearDown` to `TestMain` — calls `shutil.rmtree(self.tmpdir, ignore_errors=True)`
  - Added `import shutil` at top of test file

- Updated `TestDetectTruncation._run()` helper and all direct `detect_truncation()`
  call sites to use the new function signature.
