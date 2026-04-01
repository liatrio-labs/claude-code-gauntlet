# T03 Proof Summary

**Task:** T03 -- Fix agent LSP guidance and structure (RF-07, RF-08)
**Status:** COMPLETED
**Date:** 2026-04-01

## Requirements Verified

| ID | Requirement | Status |
|----|-------------|--------|
| RF-07a | bug-detector.md step 2 updated from Grep-only to LSP-first framing | PASS |
| RF-07b | cross-file-impact.md step 1 updated from Grep-only to LSP-first framing | PASS |
| RF-07-audit | All other agents audited for Grep-only steps that should be LSP-first | PASS (no additional changes needed) |
| RF-08 | type-design-analyzer.md "How to investigate" moved to after opening paragraph | PASS |

## Agent Audit Summary (RF-07)

All 10 agents were audited for Grep-only investigation steps that should be LSP-first:

| Agent | Finding | Action |
|-------|---------|--------|
| bug-detector.md | Step 2 was Grep-only ("Use Grep to find all callers") | Fixed: LSP-first |
| cross-file-impact.md | Step 1 was Grep-only ("use Grep to identify all callers") | Fixed: LSP-first |
| security-reviewer.md | Step 1 already had LSP-first framing | No change needed |
| test-analyzer.md | Steps are behavioral (read, identify, apply); context-pulling already mentions LSP | No change needed |
| type-design-analyzer.md | "How to investigate" section had correct LSP guidance but was misplaced | Fixed by RF-08 |
| code-simplifier.md | Context-pulling mentions LSP; no explicit Grep-only investigation steps | No change needed |
| conventions-and-intent.md | Context-pulling mentions LSP for comment accuracy | No change needed |
| validator.md | No investigation steps referencing Grep specifically | No change needed |
| challenger.md | No investigation steps referencing Grep specifically | No change needed |
| change-summarizer.md | No LSP tools (no tools listed in frontmatter) | No change needed |

## Proof Artifacts

| File | Type | Result |
|------|------|--------|
| T03-01-file.txt | file: bug-detector.md step 2 LSP-first | PASS |
| T03-02-file.txt | file: cross-file-impact.md step 1 LSP-first | PASS |
| T03-03-file.txt | file: type-design-analyzer.md section order | PASS |

## Changes Made

### agents/bug-detector.md
- Step 2 "How to investigate": Changed "Use Grep to find all callers of changed functions" to "Prefer LSP `findReferences` to find all callers of changed functions, or Grep if LSP is unavailable"

### agents/cross-file-impact.md
- Step 1 "How to investigate": Changed "use Grep to identify all callers across the codebase" to "prefer LSP `findReferences` to identify all callers across the codebase, or Grep if LSP is unavailable"

### agents/type-design-analyzer.md
- Moved "## How to investigate" section from after "## False-positive exclusions" (~line 147) to immediately after the opening paragraph (now line 12), consistent with bug-detector, security-reviewer, and cross-file-impact agent structure
