# T15 Proof Summary

Task: Clarify code-simplifier re-pipeline loop, Critical Rule #2, and MANDATORY GATE language
File modified: `skills/deep-review/SKILL.md`

## Changes Made

### I-4: MANDATORY GATE language (Phase 1, line 28)
- **Old:** "Always ask — never assume a default, even with remembered preferences."
- **New:** "If REVIEW.md sets `model_tier`, use it — that is explicit user configuration, not a remembered preference. Otherwise, always ask — never assume a default from remembered preferences."
- **Proof:** T15-01-cli.txt — PASS

### I-3: Code-simplifier re-pipeline loop (Phase 6, line 136)
- **Old:** "Its findings go through Phases 4-6 before joining Phase 7."
- **New:** Enumerates all sub-steps (4a blame, 4b factual verify, 4c batch, Phase 5 Sonnet agents, 6a-6c filter, 6d tag), explicitly states this is not a separate pipeline but the same orchestrator running a mini-batch, and clarifies findings merge into the Phase 7 challenge pool.
- **Proof:** T15-02-cli.txt — PASS

### I-2: Critical Rule #2 adds Phase 2i (Critical Rules, line 213)
- **Old:** "Phases 2e, 3, 5, and 7 MUST use Agent tool calls."
- **New:** "Phases 2e, 2i (for PRs >500 lines), 3, 5, and 7 MUST use Agent tool calls."
- **Proof:** T15-03-cli.txt — PASS

## Overall Status: PASS (3/3 proof artifacts passing)
