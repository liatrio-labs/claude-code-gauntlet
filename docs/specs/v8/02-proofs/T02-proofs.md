# T02 Proof Summary: LSP-first investigation methodology across all agents

## Status: PASS

## Changes Made

1. **Created** `skills/deep-review/references/investigation-methodology.md` -- canonical LSP-first investigation reference with duplication contract header.

2. **Updated 9 agents** with canonical source comment and LSP investigation guidance:
   - `bug-detector.md` -- added canonical comment (already had LSP in investigation steps)
   - `security-reviewer.md` -- added canonical comment (already had LSP in investigation steps)
   - `cross-file-impact.md` -- added canonical comment (already had LSP in investigation steps)
   - `type-design-analyzer.md` -- added canonical comment (already had LSP in investigation steps)
   - `test-analyzer.md` -- added canonical comment + new step 8 with LSP guidance for test coverage verification
   - `code-simplifier.md` -- added new "How to investigate" section with 4 steps including LSP-first guidance
   - `conventions-and-intent.md` -- added canonical comment + LSP steps to all 3 investigation passes
   - `validator.md` -- added canonical comment + expanded step 4 to include LSP tools
   - `challenger.md` -- added canonical comment + expanded tool usage paragraph to include LSP

3. **Excluded** `change-summarizer.md` -- has `tools: none`, LSP guidance is not applicable.

## Proof Artifacts

| File | Type | Status |
|------|------|--------|
| T02-01-file.txt | Canonical comment verification (9/9 agents) | PASS |
| T02-02-file.txt | LSP mention counts per agent | PASS |
