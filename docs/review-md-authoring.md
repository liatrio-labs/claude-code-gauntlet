# Writing REVIEW.md rules

Use `REVIEW.md` for short prose rules and repository invariants, as you would use `CLAUDE.md`,
`AGENTS.md`, or `QODO.md`. Explain a concrete constraint and why it matters.

```markdown
# Review rules

- Report changes that break the public response shape; downstream clients persist it.
- Treat authorization as an invariant at every entry point, including background tasks.
- Follow the caller before classifying apparently unreachable branches.
```

The plugin collects rules from the changed files' directories and their parents, and records where each rule came from.
Markdown imports in these files are followed within the same limits as the other rule files.

`REVIEW.md` holds rules only.
A settings block in it does not change filtering or posting.
When the plugin recognizes an old settings block, the local report notes that it was ignored.
Delivery settings are environment variables, listed in
[headless configuration](../skills/code-gauntlet/references/headless-mode.md).
The [format reference](../skills/code-gauntlet/references/review-md-spec.md) has the details.
