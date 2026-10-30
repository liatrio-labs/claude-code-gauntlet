# Investigation Methodology: LSP-First

> **Canonical source of truth.** This file is the single source of truth for investigation methodology used by all review agents.
>
> **Duplication contract.** Each agent carries an adapted copy of this guidance tailored to its domain. When updating, change this file first, then propagate to all 9 agent copies:
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
>
> Each agent copy has a `<!-- Canonical source: references/investigation-methodology.md -->` comment pointing back here. `change-summarizer` is excluded (reads supplied shared context with `Read`, without semantic code navigation).

## Investigation goals

Establish the intended contract from supplied change context and applicable project rules, then test it against the changed code and its consumers. The summary describes intent, not the full set of affected paths. Investigate data flow, boundaries, failure paths and observable effects beyond the diff, using inspected code as evidence.

## LSP-first investigation

Use LSP as the primary tool for semantic code navigation. LSP provides accurate symbol resolution (~50ms) that catches renamed imports, aliased references, and interface implementations that text-based search misses.

Primary semantic operations:

- `goToDefinition` -- trace what a symbol actually resolves to across files and inheritance chains
- `findReferences` -- locate every caller, consumer, or implementor of a symbol across the codebase
- `hover` -- inspect inferred types and signatures without reading entire files

Fallback when LSP is unavailable:

- `Grep` -- text-based search for symbol names, patterns, and string literals
- `Glob` -- find files by name or path pattern
- `Read` -- load file contents for surrounding context and full function bodies

Use each tool for its evidence:

- LSP for symbol resolution, type checking, and reference finding (semantic accuracy)
- Grep for text patterns, string literals, config values, and comments (LSP does not index these)
- Read for understanding surrounding context, control flow, and full function bodies

## Test and quality investigation

Trace a suspected test defect through its actual input, assertion, helpers and
fixture lifetimes. Search indirect coverage before claiming complete behavioral
absence; inspect candidate assertions and audit every supplied removed-test item.
For quality findings, prove a present defect, duplication, a behavior-preserving
simpler equivalent or an applicable idiom established in existing repository code.
