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
> comment pointing back here. `tests/test_registry_contracts.py::test_agent_mirrors[complete-read]`
> asserts every copy is byte-identical to the block below — the guard that stops the
> copies drifting apart. Every file-reading agent carries this backstop so it survives
> a failed shared-file read. Scope rows in `references/false-positive-exclusions.md`
> are generated separately.

## Why this exists

A Read can stop without a truncation notice, leaving a plausible but incomplete context.
A completeness claim without an end check cannot distinguish that from an empty review.

The primary fix is arithmetic, not instruction: the skill measures the context file and
`contextReadPlan` (`workflows/src/contextPlan.js`) turns the measurement into the exact
`Read` calls that cover it, which the dispatch prompt enumerates. **This block is the
backstop** — it covers the agent's reads of every *other* file, and the case where a
dispatch prompt carries no measurement.

## The block (duplicated verbatim into all 10 agents)

<!-- BEGIN CANONICAL BLOCK -->
## Reading a file completely

A `Read` can return only part of a file without a truncation notice. Completion
requires checking that the result reaches the measured end or exhausting the file.
One successful `Read` suffices when its end is checked.

- When your dispatch prompt names a shared context file, it is mandatory reading in full.
  Make every listed `Read` call in its coverage plan. If a call stops early, continue
  with `offset` past the last line received until that chunk is covered. When only a
  line count is given, reach that line. When neither is given, continue issuing
  `Read` with `offset` past the last line received until a call returns no further content.
- For any other file your conclusion depends on, check where the result stopped
  against where the content should end. A result ending mid-hunk, mid-function or
  inside an unclosed tag, bracket or quote requires continuation from that offset.
- Stopping early leaves unseen content outside your analysis. A plausible result
  alone does not establish that the file was read completely.
<!-- END CANONICAL BLOCK -->
