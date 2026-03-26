# T06 Proof Summary

Task: Pipeline correctness fixes — code-simplifier path, REVIEW.md thresholds, trust boundary tags

## Fixes Implemented

### C1: code-simplifier bypasses Phases 5-6
SKILL.md line ~422: changed "must go through Phase 4 (blame classification + factual verification)"
to "must go through Phases 4-6 (blame classification, factual verification, validation, and filtering)"

### C2: REVIEW.md confidence_threshold and severity_threshold not consumed
Phase 6a in SKILL.md: updated 6a threshold filter to reference REVIEW.md `confidence_threshold`
(default 80, security minimum 70 regardless) and `severity_threshold` (default low).
Phase 6a in validation-pipeline.md: replaced hardcoded "security: 70, all others: 80" list
with REVIEW.md override language matching SKILL.md.

### I1: Trust boundary tags missing in Phases 5 and 7 (and 2i)
Added `<untrusted-code-content>...</untrusted-code-content>` wrapping to:
- Phase 2i file summarizer agent prompt template (diff section)
- Phase 5 validation agent prompt template (relevant code sections)
- Phase 7 challenge agent prompt template (raw code section)
Updated validation-pipeline.md Phase 5 description to mention the wrapping.

## Proof Results

| Proof | File | Status | Description |
|-------|------|--------|-------------|
| C1 | T06-01-file.txt | PASS | code-simplifier now routes through Phases 4-6 |
| C2 | T06-02-file.txt | PASS | REVIEW.md threshold overrides wired in both files |
| I1 | T06-03-file.txt | PASS | untrusted-code-content tags added to 3 agent templates |

## Files Modified

- `skills/deep-review/SKILL.md` — C1, C2, I1 fixes
- `skills/deep-review/references/validation-pipeline.md` — C2, I1 fixes

## Verification

All three correctness fixes applied and verified against the task specification.
No unintended side effects — changes are targeted to the specific lines described in the task.
