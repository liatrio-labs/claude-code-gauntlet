# T03-BF03 Proof Summary — Add explicit Phase 3→4 merge instructions with example JSON

## Task

BF-03: Add an explicit "Merge Phase 3 outputs" step between Phase 3 and Phase 4 with a concrete JSON example showing all required fields: `dimension` (short name, not agent name), `agent` (injected by orchestrator, not emitted by agents), and `cross_file_refs` (preserved from agent output).

Root cause addressed: orchestrator confuses agent names with dimension names, omits `agent` field, omits `cross_file_refs` — all because there was no concrete example of the merged JSON.

## Requirements Verified

| Requirement | Status | Evidence |
|---|---|---|
| "Merge Phase 3 Outputs" section added to SKILL.md between Phase 3 and Phase 4 | PASS | T03-BF03-01-file.txt |
| dimension mapping table present (agent name → dimension short name) | PASS | T03-BF03-01-file.txt |
| agent field injection instruction present ("Agents do NOT emit this field") | PASS | T03-BF03-01-file.txt |
| cross_file_refs preservation instruction present | PASS | T03-BF03-01-file.txt |
| Concrete example JSON with all three required fields per finding | PASS | T03-BF03-01-file.txt |
| Section positioned correctly (after Phase 3 `---`, before `## Phase 4`) | PASS | T03-BF03-01-file.txt |
| validation-pipeline.md Phase 4 input schema adds `agent` field | PASS | T03-BF03-02-file.txt |
| Field notes clarify dimension vs agent and cross_file_refs semantics | PASS | T03-BF03-02-file.txt |
| All 149 tests pass — no regressions | PASS | T03-BF03-03-test.txt |

## Files Modified

- `skills/deep-review/SKILL.md` — Added "Merge Phase 3 Outputs" section (lines 84–166) between Phase 3 and Phase 4, with dimension mapping table, agent injection note, cross_file_refs note, and a 3-finding example JSON
- `skills/deep-review/references/validation-pipeline.md` — Added `agent` field to Phase 4 input JSON schema and added "Field notes for the merged input" section explaining dimension vs agent vs cross_file_refs semantics

## Summary

The orchestrator now has unambiguous step-by-step guidance for merging Phase 3 agent outputs before calling `verify_findings.py`. The concrete JSON example shows exactly what each merged finding must contain, distinguishing between `dimension` (short name from agent schema) and `agent` (exact name string injected by orchestrator). The `cross_file_refs` preservation requirement is called out explicitly since its omission breaks Phase 4a blame classification.
