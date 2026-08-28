# agents/

Subagent contracts. Each `.md` is a system prompt with enforced YAML frontmatter.

- **Frontmatter is system-enforced.** `tools`, `effort`, `model` and `color` are not advisory —
  Claude Code enforces them.
- **LSP-first investigation.** Agents prefer `goToDefinition` / `findReferences` / `hover`, with
  Grep as fallback.
- **A finding field is one entry in `workflows/src/registry.js` plus the owning agent's output
  block.** A field no schema declares is **rejected at dispatch** — the item schema is closed
  (`additionalProperties: false`, issue #53) — so emitting it burns schema retries and, persisted,
  fails the agent. `tests/test_dimensions_registry.py` fails the build when the two drift.
- **Declared-but-optional fields are not nullable.** A not-applicable value is *omitted*, never
  `null`; the platform types each property to a single type and a null burns retries.
- **`required` is `FINDING_REQUIRED` plus per-row `requiredExtra`.** Required only when the
  contract emits a field unconditionally (no OMIT branch); a multi-dimension agent requires it
  only when every sibling dimension does too. A field required only in its OWN dimension
  (siblings OMIT it — `claude_md_rule`, `spec_text`) goes in `requiredWhenDimension` instead,
  schema-enforced there on a first-party-direct dispatch only; contract prose is the floor
  elsewhere.
- `dimension` — short name from agent output: `"bug"`, `"security"`, `"cross_file_impact"`, `"test_coverage"`, `"convention"`, `"intent"`, `"comment_accuracy"`, `"type_design"`, `"simplification"`. Never the agent name. `agent` is injected by the orchestrator at merge; agents do not emit it.
- **Canonical fields** — every dispatch schema declares exactly these: `id`, `file`, `line_start`, `line_end`, `title`, `description`, `severity`, `confidence`, `dimension`, `origin`, `evidence`, `suggestion`, `claude_md_rule`, `cross_file_refs`, `suggested_fix_code`.
- **Per-dimension extras** — one entry on the owning registry row: `hidden_errors` (bug), `attack_vector` (security), `affected_consumers` (cross_file_impact), `criticality` + `failure_scenario` (test_coverage), `spec_text` (intent), `invalid_state_example` (type_design), `behavior_preserved` (simplification).
- **The false-positive exclusion list and the complete-read contract are intentionally duplicated**
  into every agent that needs them, so the guarantee survives a failed file read. Each copy carries
  a `<!-- Canonical source: ... -->` pointer. Do not refactor them into a shared read.
  `tests/test_agent_contracts.py::TestCompleteReadContract` asserts the complete-read copies are
  byte-identical; `TestPromptInjectionArtifactsMirror` asserts the 7 exclusion-list copies are
  byte-identical to each other (not to the canonical skills file, which deliberately uses a
  different person/voice).
- **The challenger is structurally blind by design** — title, description, location, and the code it
  opens itself. It is never given the shared context path.

## Returning findings

Findings are returned **by value** as the task's structured result — `{ findings, complete,
total_seen }` per the dispatch schema. There is no findings file, nothing is written to disk, and
the discovery contracts grant no shell tool. `description` is single-paragraph prose (≤500 chars,
no fenced blocks, no bullet lists); code references belong in `evidence` and `cross_file_refs`.
An apostrophe never needs escaping inside a JSON string value.
