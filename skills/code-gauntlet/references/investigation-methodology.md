# Investigation Methodology: LSP-First

> **Canonical source of truth.** This file is the source of investigation methodology for validator and challenger.
>
> **Duplication contract.** Validator and challenger carry adapted copies of this guidance. When updating, change this file first, then propagate to both copies:
>
> 1. `agents/validator.md`
> 2. `agents/challenger.md`
>
> Both copies have a `<!-- Canonical source: references/investigation-methodology.md -->` comment pointing back here.

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

Map changed behavior, invariants, callers and failure modes to exercised test
inputs and assertions, including integration contracts and indirect coverage.
Trace test defects through helpers and fixture lifetimes; audit every supplied
removed-test item. For types, walk construction and mutation paths to states
observed by callers, checking invariant enforcement and reachable invalid states.
Report demonstrated production defects as such.
For quality findings, prove a present defect, duplication, a behavior-preserving
simpler equivalent or an applicable idiom established in existing repository code.
