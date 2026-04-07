# T05 Proof Summary

**Task:** T05 — Fix SKILL.md + reference docs: security boundary, Frontier template, recovery, vestigial instructions
**Task ID:** #156
**Status:** COMPLETED
**Model:** sonnet

## Requirements Verified

| Req | Description | Status |
|-----|-------------|--------|
| R05.1 | SKILL.md security boundary mentions Bash for Phase 3 + PreToolUse hook restriction | PASS |
| R05.2 | Frontier mode template includes Findings file: line | PASS |
| R05.3 | validation-pipeline.md notes apply_validations.py handles original_confidence | PASS |
| R05.4 | merge_findings.py has failure recovery procedure in validation-pipeline.md | PASS |
| R05.5 | Design spec CLI examples match actual positional argument interface | PASS |

## Proof Artifacts

| File | Type | Result |
|------|------|--------|
| T05-01-cli.txt | cli | PASS — SKILL.md security boundary updated at lines 111 and 294 |
| T05-02-cli.txt | cli | PASS — 8/8 Findings file: lines present (7 standard + 1 Frontier) |
| T05-03-cli.txt | cli | PASS — Vestigial original_confidence instruction removed |
| T05-04-cli.txt | cli | PASS — merge_findings.py failure recovery section added |
| T05-05-cli.txt | cli | PASS — CLI examples use positional args matching actual interface |

## Changes Made

### skills/deep-review/SKILL.md
- Line 111: Security boundary updated to distinguish Phase 3 (Bash + hook restriction) from Phase 5/7 (no Bash)
- Line 294: Critical Rules #3 updated with same distinction

### skills/deep-review/references/phase3-dispatch.md
- Added `Findings file: $TMPDIR/deep-review-bug-detector-{head_sha_short}.ndjson` to Frontier mode dispatch template

### skills/deep-review/references/validation-pipeline.md
- Line ~117: Replaced vestigial manual original_confidence instruction with note that apply_validations.py handles this automatically
- Added merge_findings.py failure recovery section before existing Script Failure Recovery section

### docs/design/v7-master-improvement-plan.md
- apply_validations.py CLI example: `--findings`/`--validations` flags → positional args; output file name corrected (phase6-input → phase5-output)
- apply_challenges.py CLI example: `--findings`/`--challenges` flags → positional args; removed `--max-findings 0` (redundant default)
