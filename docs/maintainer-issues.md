# Maintainer work-queue issue standard

This document is the landing page for maintainer work-queue conventions
(expanded by Issue #30). Sibling work-queue items should link here for process
norms and link out to specialty runbooks for domain policy.

## Measurement policy

Do **not** restate the measurement tier ladder in work-queue issues. The
canonical text lives in [`bench/MEASUREMENT.md`](../bench/MEASUREMENT.md):

- Always-on deterministic suites (every PR)
- Per-sub-release functional smoke (release manager; mechanical checker)
- Owner-triggered paired mini-subset (~$85) for changes that plausibly move
  recall/noise
- Owner-triggered full-15 / holdout (holdout sealed for V3.2)

Work-queue issue drafts and PR descriptions that need measurement framing
should reference that runbook rather than the superseded
"every behavior-changing item ships behind a paired bench measurement" rule.

## Related

- Issue #28 — codified the measurement policy, named mini subset, and smoke checker
- Issue #30 — contribution-surface refresh (forms, labels, SECURITY.md, this doc)
