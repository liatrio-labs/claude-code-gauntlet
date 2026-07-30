# CLAUDE.md

@AGENTS.md

## Claude Code specifics

Path-scoped rules live in `.claude/rules/` and load only when you read a file they match. The
directory rules they point at (`workflows/AGENTS.md`, `scripts/AGENTS.md`, `agents/AGENTS.md`) are
the same files Cursor and Codex read — one source, four readers.

- The review pipeline runs as a single `Workflow` tool call from `skills/code-gauntlet/SKILL.md`,
  which owns Phases 1–2 (prepare) and Phase 8 (deliver). The eight review stages run inside
  `workflows/pipeline.js`.
- Durable project knowledge belongs in auto-memory, not here. This file is loaded on every turn, so
  an addition must fail the test *"would removing this cause Claude to make mistakes?"* before it
  earns a place — and must not already be stated by a comment at the code it describes.
