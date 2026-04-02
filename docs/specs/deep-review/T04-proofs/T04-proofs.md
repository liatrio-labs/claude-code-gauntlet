# T04 Proof Summary: Incremental Finding Emission (V5-04)

## Task
Restructure all 7 discovery agent output formats from single JSON array to incremental per-finding emission. Update orchestrator merge logic and dispatch template.

## Proof Artifacts

| # | Type | File | Status |
|---|------|------|--------|
| 1 | file | T04-01-file.txt | PASS |
| 2 | file | T04-02-file.txt | PASS |
| 3 | test | T04-03-test.txt | PASS |

## Proof Details

### T04-01-file.txt (Agent Files)
- All 7 discovery agents contain "Emit each finding immediately" instruction
- No agents retain the old "Return a JSON array of findings" pattern
- All 7 agents contain the SKIP format for explicit skips
- No agents retain "return an empty array" fallback

### T04-02-file.txt (Orchestrator Files)
- SKILL.md "Merge Phase 3 Outputs" section updated with incremental parsing instructions
- phase3-dispatch.md has new "Parsing Agent Output" section with truncation handling

### T04-03-test.txt (Test Suite)
- All 208 tests pass after changes (no regressions)

## Files Modified
- `agents/bug-detector.md` - Output format section
- `agents/security-reviewer.md` - Output format section
- `agents/cross-file-impact.md` - Output format section
- `agents/test-analyzer.md` - Output format section
- `agents/conventions-and-intent.md` - Output format section
- `agents/type-design-analyzer.md` - Output format section
- `agents/code-simplifier.md` - Output format section
- `skills/deep-review/SKILL.md` - Merge Phase 3 Outputs section
- `skills/deep-review/references/phase3-dispatch.md` - Added Parsing Agent Output section
