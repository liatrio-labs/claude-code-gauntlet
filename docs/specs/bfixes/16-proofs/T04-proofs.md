# T04 Proof Summary

**Task:** T04 (BF-16) -- Pass Phase 2 diff to verify_findings.py via --diff-file
**Status:** COMPLETED
**Date:** 2026-04-01

## Requirements Verified

| ID | Requirement | Status |
|----|-------------|--------|
| BF-16-phase2 | SKILL.md Phase 2 includes diff persistence section | PASS (committed by BF-14 worker) |
| BF-16-phase4 | SKILL.md Phase 4 invocation includes --diff-file | PASS (committed by BF-14 worker) |
| BF-16-triage | phase2-triage.md 2c has diff-save instruction with validation rules | PASS |
| BF-16-pipeline | validation-pipeline.md Step 4.0 example includes --diff-file | PASS |
| BF-16-fallback | All three files document omitting --diff-file for branch/local targets | PASS |
| BF-16-validation | Diff validation rules documented (non-empty, starts with diff --git) | PASS |
| BF-16-api-limit | 20K-line API limit failure handling documented | PASS |
| BF-16-no-regression | Full test suite passes (208 tests) | PASS |

## Changes Made

### skills/deep-review/references/phase2-triage.md
- Added diff-save instruction block in section 2c after diff collection
- Includes `gh pr diff` command example saving to `$TMPDIR/deep-review-diff.patch`
- Includes validation rules (non-empty, starts with `diff --git`)
- Includes fallback behavior when `gh pr diff` fails (API limit)
- Explicit note that branch comparison and local changes skip this step

### skills/deep-review/references/validation-pipeline.md
- Updated Step 4.0 example invocation to include `--diff-file "$TMPDIR/deep-review-diff.patch"`
- Added explanation paragraph about when to pass vs omit `--diff-file`

### skills/deep-review/SKILL.md (committed by BF-14 worker in 492876d)
- Phase 2: "Diff persistence for Phase 4 (PR/MR mode)" section
- Phase 4: `--diff-file` in invocation template with explanation

## Proof Artifacts

| File | Type | Result |
|------|------|--------|
| T04-01-file.txt | file: phase2-triage.md diff-save instruction | PASS |
| T04-02-file.txt | file: validation-pipeline.md --diff-file invocation | PASS |
| T04-03-file.txt | file: SKILL.md diff persistence and --diff-file | PASS |
| T04-04-test.txt | test: full test suite (208 tests, 0 regressions) | PASS |

## Notes

The `--diff-file` flag already existed in `verify_findings.py` (implemented by BF-11). BF-16 is
purely an instruction change to tell the orchestrator to save the Phase 2 diff and pass it to
the script. No script changes were needed.

The SKILL.md portion of BF-16 was committed alongside BF-14 by another worker (commit 492876d).
This task adds the reference file changes (phase2-triage.md and validation-pipeline.md) that
complete the full BF-16 instruction set.
