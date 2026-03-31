# T07 Proof Summary

Task: T07: I-09 Validation failure protocol -- re-dispatch or degrade
Status: PASS

## Artifacts

| # | Type | File | Status |
|---|------|------|--------|
| 1 | file | T07-01-file.txt | PASS |

## Summary

Added a "Validation Failure Protocol" subsection to Phase 5 in validation-pipeline.md
with three rules: (1) detect systemic working tree mismatch and re-dispatch after
checkout fix, (2) handle individual agent failures gracefully with degradation warning,
(3) never substitute orchestrator self-validation for the pipeline. Core principle:
re-dispatch or degrade transparently.
