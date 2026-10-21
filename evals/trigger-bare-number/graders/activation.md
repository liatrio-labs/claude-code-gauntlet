---
type: tool_used
tool: Skill
input_match: '"skill"\s*:\s*"(?:[\w-]+:)?code-gauntlet"'
min: 1
---

Passes when the session invokes the code-gauntlet skill through the Skill tool, namespaced or not. A pre-flight stop after activation still passes.
