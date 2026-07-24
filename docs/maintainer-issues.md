# Maintainer work-queue issue standard

This document is the landing page for maintainer work-queue conventions
(expanded by Issue #30). Sibling work-queue items should link here for process
norms and link out to specialty runbooks for domain policy.

## Measurement policy

Do **not** restate the measurement tier ladder, costs, or owner options in
work-queue issues. Link to the canonical runbook instead:

→ [`bench/MEASUREMENT.md`](../bench/MEASUREMENT.md)

Work-queue issue drafts and PR descriptions that need measurement framing
should reference that runbook rather than the superseded
"every behavior-changing item ships behind a paired bench measurement" rule.

## Related

- Issue #28 — codified the measurement policy, named mini subset, and smoke checker
- Issue #30 — contribution-surface refresh (forms, labels, SECURITY.md, this doc)
