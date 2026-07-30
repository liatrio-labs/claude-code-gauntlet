# CLAUDE.md

@AGENTS.md

## Claude Code specifics

Each directory's `CLAUDE.md` is a generated twin of its `AGENTS.md` and loads only when you read a
file in that directory. Edit `AGENTS.md`, then run `python3 scripts/sync_agent_rules.py`. The twin
exists because the on-demand loader injects a memory file verbatim and does not expand `@imports`.

- The review pipeline runs as a single `Workflow` tool call from `skills/code-gauntlet/SKILL.md`,
  which owns Phases 1–2 (prepare) and Phase 8 (deliver). The eight review stages run inside
  `workflows/pipeline.js`.
- Durable project knowledge belongs in auto-memory, not here. This file is loaded on every turn, so
  an addition must fail the test *"would removing this cause Claude to make mistakes?"* before it
  earns a place — and must not already be stated by a comment at the code it describes.
