# T08-BF-08 Proof Summary

## Task
T08-BF-08: Embed scoping rules in Phase 3 dispatch template (BF-08)

Add scoping reminders directly in the Agent dispatch template in phase3-dispatch.md. Instead of relying on a separate reference table, put parenthetical reminders in the template to prevent orchestrator from applying risk-based filtering to agents that should get all files.

## Implementation

### Changes Made
Modified `/skills/deep-review/references/phase3-dispatch.md`:

1. **Restructured Agent Tool Call Template section** (lines 51-158)
   - Changed from a single generic template to 7 agent-specific templates
   - Each template now includes explicit scoping rules as part of the prompt text

2. **Embedded scoping reminders for each agent:**
   - **bug-detector**: `Scoped diff (HIGH and MEDIUM risk files only, plus test files and history context):`
   - **security-reviewer**: `Scoped diff (ALL changed files — do not filter by risk level):`
   - **cross-file-impact**: `Scoped diff (ALL changed files + entire codebase for symbol search):`
   - **test-analyzer**: `Scoped diff (changed production files plus all test files):`
   - **conventions-and-intent**: `Scoped diff (ALL changed files for full convention and intent checking):`
   - **type-design-analyzer**: `Scoped diff (files with new type definitions only):`
   - **code-simplifier**: `Scoped diff (all changed files for simplification opportunities):`

3. **Updated Frontier mode example** (lines 162-169)
   - Frontier mode example now includes full scoping reminder for bug-detector agent

### Purpose
The scoping reminders are parenthetical comments that:
- Prevent the orchestrator from applying risk-based filtering to agents that need all files (security-reviewer, cross-file-impact, conventions-and-intent, code-simplifier)
- Make scoping rules explicit in the template rather than relying on separate documentation
- Reduce friction from orchestrator misunderstanding which files each agent should receive

## Proof Artifacts

| File | Type | Status | Description |
|------|------|--------|-------------|
| T08-BF-08-01-file-changes.txt | file | PASS | All 8 scoped diff comments verified in place |
| T08-BF-08-02-security-reviewer-scope.txt | file | PASS | Security-reviewer template includes "ALL changed files" |
| T08-BF-08-03-cross-file-impact-scope.txt | file | PASS | Cross-file-impact includes codebase search instruction |

## Files Modified
- `skills/deep-review/references/phase3-dispatch.md`

## Verification
- [x] All 7 agents now have explicit scoping rules in template
- [x] Frontier mode example includes scoping reminder
- [x] Risk-filtering agents (bug-detector, test-analyzer) have narrowed scopes
- [x] All-file agents (security-reviewer, cross-file-impact, conventions-and-intent) have explicit "ALL changed files" labels
- [x] Parenthetical reminders clarify why each scoping is required

## Notes
The implementation satisfies the task requirement to embed scoping rules directly in the dispatch template as parenthetical reminders. This approach is more robust than separate reference documentation because the scoping rules travel with the template and orchestrators cannot accidentally apply risk-based filtering when explicit scoping is embedded in the prompt itself.
