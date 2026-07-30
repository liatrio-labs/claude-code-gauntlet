<!-- GENERATED from AGENTS.md by scripts/sync_agent_rules.py — do not edit.
     Claude Code's on-demand loader injects this file verbatim and does NOT expand
     @imports, so the rules must be physically present here. Edit AGENTS.md. -->

# agents/

Subagent contracts. Each `.md` is a system prompt with enforced YAML frontmatter.

- **Frontmatter is system-enforced.** `tools`, `effort`, `model` and `color` are not advisory —
  Claude Code enforces them.
- **LSP-first investigation.** Agents prefer `goToDefinition` / `findReferences` / `hover`, with
  Grep as fallback.
- **A finding field is one entry in `workflows/src/registry.js` plus the owning agent's output
  block.** A field no schema declares is **dropped silently** — StructuredOutput returns only
  declared properties — so a field a contract instructs but the registry does not declare never
  reaches merge, on every run. `tests/test_dimensions_registry.py` fails the build when the two drift.
- **Declared-but-optional fields are not nullable.** A not-applicable value is *omitted*, never
  `null`; the platform types each property to a single type and a null burns retries.
- **`required` is one flat list shared by every dimension.** A field a contract calls required for
  its own dimension is contract-enforced, not schema-enforced. Do not fake it by appending a
  single-dimension field to `FINDING_REQUIRED`.
- `dimension` — short name from agent output: `"bug"`, `"security"`, `"cross_file_impact"`, `"test_coverage"`, `"convention"`, `"intent"`, `"comment_accuracy"`, `"type_design"`, `"simplification"`. Never the agent name. `agent` is injected by the orchestrator at merge; agents do not emit it.
- **Canonical fields** — every dispatch schema declares exactly these: `id`, `file`, `line_start`, `line_end`, `title`, `description`, `severity`, `confidence`, `dimension`, `origin`, `evidence`, `suggestion`, `claude_md_rule`, `cross_file_refs`.
- **Per-dimension extras** — one entry on the owning registry row: `hidden_errors` (bug), `attack_vector` (security), `affected_consumers` (cross_file_impact), `criticality` + `failure_scenario` (test_coverage), `spec_text` (intent), `invalid_state_example` (type_design), `behavior_preserved` (simplification).
- **The false-positive exclusion list and the complete-read contract are intentionally duplicated**
  into every agent that needs them, so the guarantee survives a failed file read. Each copy carries
  a `<!-- Canonical source: ... -->` pointer. Do not refactor them into a shared read.
  `tests/test_agent_contracts.py::TestCompleteReadContract` asserts the copies are byte-identical.
- **The challenger is structurally blind by design** — title, description, location, and the code it
  opens itself. It is never given the shared context path.

## Emitting findings

NDJSON, one physical line per object. Literal newlines, tabs and carriage returns inside string
values must be written as the two-character escapes — a raw `0x0A` splits one finding into two
corrupt lines. `description` is single-paragraph prose (≤500 chars, no fenced blocks, no bullet
lists); code references belong in `evidence` and `cross_file_refs`.

Emit with `printf '%s\n' '...' >> "literal_path"` — **not** `echo`, whose zsh builtin interprets
`\n` even inside single quotes. For apostrophes in JSON values use `'`. Avoid `$'...'`,
`$VAR`, heredocs, `python3 -c` and command substitution: the tree-sitter-bash parser treats these
as unrecognized nodes and they are silently denied under sandbox auto-approval.

Canonical contract: `skills/code-gauntlet/references/ndjson-emission-contract.md`.
