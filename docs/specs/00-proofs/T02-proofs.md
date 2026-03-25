# T02 Proof Summary — Add model tier user preference prompt

## Requirements Verified

| Requirement | Status | Evidence |
|---|---|---|
| R02.1 — Phase 0 prompts user for Optimized vs Frontier | PASS | T02-01-file.txt |
| R02.2 — REVIEW.md model_tier checked first, skips prompt if set | PASS | T02-01-file.txt, T02-02-file.txt |
| R02.3 — Phase 2i dimension table shows both modes | PASS | T02-01-file.txt |
| R02.4 — Security-reviewer always uses Opus in both modes | PASS | T02-01-file.txt |
| R02.5 — Notes section updated with research-backed rationale | PASS | SKILL.md line 931 |

## Files Modified

- `skills/deep-review/SKILL.md` — Phase 0 mode selection, Phase 2i table, REVIEW.md support section, Notes section
- `skills/deep-review/references/review-md-spec.md` — model_tier field, section detail, hierarchy table
