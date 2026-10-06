# agents/

Each file here is a subagent system prompt.

- Frontmatter `tools`, `effort`, `model`, and `color` are enforced by Claude Code, not advisory.
- Finding fields are declared in `workflows/src/registry.js`. The dispatch schema is closed, so a
  field named only in a contract is rejected. Omit a not-applicable field; never send `null`.
- Findings return by value as `{ findings, complete, total_seen }`. Nothing is written to disk,
  and no discovery contract has a shell tool.
- The false-positive exclusion list and the complete-read contract are duplicated across
  contracts on purpose, so each survives a failed file read. Tests pin the copies byte-identical.
  Do not refactor them into a shared read.
