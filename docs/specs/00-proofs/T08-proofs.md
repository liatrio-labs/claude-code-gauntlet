# T08 Proof Summary — Subagent hardening: tool allowlists, effort field, model defaults, security boundary update

## Requirements Verified

| Requirement | Status | Evidence |
|---|---|---|
| R08.1 — Tool allowlists and effort fields added to all Agent() call templates in SKILL.md | PASS | T08-01-file.txt |
| R08.2 — Model defaults added to frontmatter of all 7 agent definition files | PASS | T08-02-file.txt |
| R08.3 — SECURITY BOUNDARY note updated to reference structural controls | PASS | T08-03-file.txt |
| R08.4 — agent-prompt-template.md updated with required fields table | PASS | T08-04-file.txt |

## Files Modified

- `skills/deep-review/SKILL.md` — Added `tools` and `effort` to all 5 Agent() call templates; updated SECURITY BOUNDARY note; updated code-simplifier timing text
- `skills/deep-review/references/agent-prompt-template.md` — Added "Required Agent() call fields" section with per-phase tools/effort table
- `skills/deep-review/agents/security-reviewer.md` — Added `model: opus` to frontmatter
- `skills/deep-review/agents/bug-detector.md` — Added `model: sonnet` to frontmatter
- `skills/deep-review/agents/cross-file-impact-analyzer.md` — Added `model: sonnet` to frontmatter
- `skills/deep-review/agents/test-analyzer.md` — Added `model: sonnet` to frontmatter
- `skills/deep-review/agents/conventions-and-intent.md` — Added `model: sonnet` to frontmatter
- `skills/deep-review/agents/type-design-analyzer.md` — Added `model: sonnet` to frontmatter
- `skills/deep-review/agents/code-simplifier.md` — Added `model: sonnet` to frontmatter

## Summary

All agent dispatch points now specify explicit tool allowlists and effort levels. No review agent (Phases 3-7) can call Write, Edit, Bash, or MCP tools — only Read, Grep, Glob, and LSP. Summarizer agents get empty tool lists. The security boundary note now references these structural controls rather than relying on behavioral instructions alone. Agent frontmatter defaults enable the orchestrator to read model tier from the file rather than hardcoding it.
