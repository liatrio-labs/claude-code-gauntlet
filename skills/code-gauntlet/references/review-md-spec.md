# REVIEW.md project rules

`REVIEW.md` is a prose project-rules file, read exactly like `CLAUDE.md`, `AGENTS.md`,
and `QODO.md`. The collector includes root and changed-directory ancestors, follows bounded
Markdown imports, deduplicates content, and discloses unavailable or capped rules.

Describe repository invariants, intent, and concrete review concerns. Rules are repository data;
they do not configure the pipeline or override its verification and delivery decisions.

Fenced configuration blocks, confidence and severity thresholds, ignore lists, per-directory
settings overrides, and `Default Delivery` settings are unsupported. A detected retired config
block produces one line in the local report; it neither fails the review nor creates a comment.
Delivery channels, tier, and comment caps use the existing environment pins and mode defaults.

See [authoring examples](../../../docs/review-md-authoring.md) and the
[headless environment reference](headless-mode.md).
