# Complete-Read Contract

> **Canonical source of truth.** This file is the single source of truth for the
> read-completeness rule every file-reading review agent carries.
>
> **Duplication contract.** The block below is duplicated **verbatim** into each of the
> 10 agents that open files. When updating, change this file first, then propagate to
> all 10 copies:
>
> 1. `agents/bug-detector.md`
> 2. `agents/security-reviewer.md`
> 3. `agents/cross-file-impact.md`
> 4. `agents/test-analyzer.md`
> 5. `agents/conventions-and-intent.md`
> 6. `agents/type-design-analyzer.md`
> 7. `agents/code-simplifier.md`
> 8. `agents/validator.md`
> 9. `agents/challenger.md`
> 10. `agents/change-summarizer.md`
>
> Each agent copy is preceded by a `<!-- Canonical source: references/complete-read-contract.md -->`
> comment pointing back here. `tests/test_agent_contracts.py::TestCompleteReadContract`
> asserts every copy is byte-identical to the block below — the guard that stops the
> copies drifting apart. Same duplication rationale as
> `references/false-positive-exclusions.md`: every agent must carry the rule even if a
> file read fails, so it is not refactored into a shared read.

## Why this exists

Issue #48, measured on run `wf_cef39739-577`. All 7 discovery agents' **first** `Read`
of the 95,057-byte / 2,028-line shared context file returned 58,145 characters,
ending at line 1083. **No tool result contained a truncation notice** — checked
programmatically across all 7 transcripts. Six agents inferred the cutoff from the line
numbering and paginated on to the file's true end. `security-reviewer` did not: it made
one `Read` of the context file and returned `{"findings":[],"complete":true}` after
seeing roughly the first half of the diff and project rules.

Nothing in the run distinguished that from a clean empty result. The report, the
artifacts, and the transcript all looked normal.

The primary fix is arithmetic, not instruction: the skill measures the context file and
`contextReadPlan` (`workflows/src/stages.js`) turns the measurement into the exact
`Read` calls that cover it, which the dispatch prompt enumerates. **This block is the
backstop** — it covers the agent's reads of every *other* file, and the case where a
dispatch prompt carries no measurement.

## The block (duplicated verbatim into all 10 agents)

<!-- BEGIN CANONICAL BLOCK -->
## Reading a file completely

A `Read` can return only PART of a file and tell you nothing about it. There is no
truncation notice, and a partial result looks exactly like a complete file. One `Read`
is never proof you have the whole file.

- **The shared context file is mandatory reading in full.** When your dispatch prompt
  lists the `Read` calls that cover it, make every one of them. When it instead states
  a line count, read until you reach that line. When it gives neither, keep issuing
  `Read` with `offset` set past the last line you received until a call returns no
  further content.
- **For any other file your conclusion depends on**, check where the result stopped
  against where the content should end. Output that breaks off mid-hunk, mid-function,
  or inside an unclosed tag, bracket, or quote means the file continues — read on from
  that offset before you judge it.
- **Stopping early is a silent failure.** You will analyze only the portion you saw and
  report as though you had seen all of it. Never treat "I read the file" as settled
  because one call returned something plausible.
<!-- END CANONICAL BLOCK -->
