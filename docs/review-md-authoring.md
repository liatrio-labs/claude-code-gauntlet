# Writing REVIEW.md rules

Use `REVIEW.md` for short prose rules and repository invariants, as you would use `CLAUDE.md`,
`AGENTS.md`, or `QODO.md`. Explain a concrete constraint and why it matters.

```markdown
# Review rules

- Report changes that break the public response shape; downstream clients persist it.
- Treat authorization as an invariant at every entry point, including background tasks.
- Follow the caller before classifying apparently unreachable branches.
```

Rules in changed-file directories and their ancestors are collected with source provenance.
Markdown imports follow the same bounded collector as other project-rules files.

Configuration blocks and headings no longer set confidence, severity, ignores, or delivery.
A retired settings form produces one local report notice naming `CODE_GAUNTLET_DELIVERY` as the delivery replacement and has no posting effect.
Use the documented environment pins for delivery controls. See the
[format reference](../skills/code-gauntlet/references/review-md-spec.md).
