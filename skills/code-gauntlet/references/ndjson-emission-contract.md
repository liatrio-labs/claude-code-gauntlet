# NDJSON Emission Contract — Canonical Source

This is the canonical NDJSON emission contract from v2, retained as v2-compat
surface. It is no longer duplicated into any agent: v3 discovery agents return
findings by value through their dispatch schema, and the emission prose was
stripped from all 7 contracts. The duplication doctrine it once followed still
governs the false-positive exclusion list and the complete-read contract — see
the duplication-doctrine bullet in `agents/AGENTS.md`. Should emission return,
update this file first, then propagate to every file carrying a
`<!-- Canonical source: references/ndjson-emission-contract.md -->` marker.

## The contract (drop-in block for agent definitions)

> **Strict NDJSON contract — every finding must satisfy these rules.**
>
> `printf '%s\n'` writes one physical line. The merge pipeline reads the
> findings file line-by-line, so a literal newline, tab, or carriage
> return inside any JSON string value (`description`, `evidence`,
> `suggestion`, `title`, …) splits one finding into two corrupt records.
> Replace each control character with its two-character JSON escape
> sequence:
>
> | Inside a string value | Escape as |
> |-----------------------|-----------|
> | newline (`\n`, byte 0x0A) | `\n` |
> | tab (`\t`, byte 0x09)     | `\t` |
> | carriage return (`\r`, byte 0x0D) | `\r` |
> | apostrophe `'` | `\u0027` |
> | double-quote `"`          | `\"`     |
> | backslash `\`             | `\\`     |
>
> **`description` field — prose only.** Single paragraph, ≤500 chars.
> No fenced code blocks, no multi-line snippets, no bullet lists. Code
> pointers go in `evidence` (single short line, ≤200 chars) and
> `cross_file_refs` (array of file paths). If you need to show a fix,
> keep `suggestion` to one or two sentences of prose — not in `description`.
>
> **BAD — literal newline splits one finding into two corrupt lines:**
>
> ```bash
> printf '%s\n' '{"id":"bug-1","description":"Issue at line 42.
> The value is null.","line_start":42}' >> "<findings_file>"
> ```
>
> **GOOD — same content with the newline escaped to two characters `\n`:**
>
> ```bash
> printf '%s\n' '{"id":"bug-1","description":"Issue at line 42.\nThe value is null.","line_start":42}' >> "<findings_file>"
> ```
>
> **Final step — validate before returning.** After your last finding,
> run the validator. The plugin_root path is in the "Validator" section
> of the context file:
>
> ```bash
> python3 "<plugin_root>/scripts/validate_ndjson.py" "<findings_file>"
> ```
>
> Exit code 0 means every line parses. A non-zero exit means at least
> one finding has unescaped control characters or an unterminated
> string — the script prints the failing line numbers and a snippet of
> each. Re-emit the affected finding(s) with proper escaping and re-run
> until the validator returns 0. Do not return until validation passes.

## Why each rule exists

- **No literal `\n`/`\t`/`\r` in strings** — `printf '%s\n'` puts one
  physical newline at the end of its output. Any newline byte that the
  agent embeds inside a JSON string value is *also* written verbatim,
  splitting the line. The merge pipeline keys on physical lines, so
  the finding becomes two malformed records.
- **`description` prose-only** — when descriptions are unconstrained,
  agents reach for fenced code blocks and bullet lists, which require
  embedded newlines. Constraining the field structurally removes the
  pull toward control-character escapes that get forgotten.
- **Final validator step** — `merge_findings.py` has a text-channel
  fallback that scrapes findings from agent text returns when NDJSON
  parsing fails, but reconstruction is lossy (no `cross_file_refs`,
  truncated descriptions). Catching the bug while the agent is still
  in scope, with the original strings still in memory, is strictly
  cheaper than reconstructing later.
- **`python3 path/script.py args` is AST-safe** — unlike `python3 -c
  "..."`, a plain script invocation is just three word tokens to the
  tree-sitter-bash parser. No unrecognized AST nodes, so the subagent
  sandbox auto-approves the call.

## How agents discover the validator path

Phase 2 writes `<plugin_root>/scripts/validate_ndjson.py` into the
shared context file under a "Validator" section. Every agent reads the
context file at startup and learns the absolute validator path along
with the rest of the shared context — the dispatch prompt itself stays
at ~100 tokens.
