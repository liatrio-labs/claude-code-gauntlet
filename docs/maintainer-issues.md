# Maintainer work-queue issue standard

This is the standard for **maintainer-filed work-queue issues** — the items
maintainers open to carry planned work on this repo. External contributors do
not use it: bug reports, feature requests, and questions go through the issue
forms under `.github/ISSUE_TEMPLATE/`, which collect the diagnostics those
reports need and apply triage labels on submission.

A work-queue issue exists so that someone other than its author can pick it
up, check its claims independently, and prove it done. Every rule below serves
that.

## Issue structure

Every maintainer work-queue issue carries these six sections, in this order,
spelled as written.

### Problem / Goal

Why this is worth doing now, and what the end state is. State the current
behavior, the cost of leaving it alone, and what "fixed" looks like. Two short
paragraphs are usually enough; if it takes many more, the issue is probably
two issues.

### Requirements

Numbered, and each one individually verifiable — a reader must be able to
point at a command, a file, or an API read that settles whether it holds.
Requirements say what must be true, not how to build it. Implementation
prescriptions belong in Candidate directions, where the implementer is free to
ignore them.

### Evidence

Verbatim command output, API reads, or file excerpts, each with the date it
was taken. The point is that a reader can re-derive the claim rather than
trust it: a summarized assertion ("the label is missing") is not evidence; the
dated `gh api` output that shows it is.

Evidence goes stale. When a later read contradicts a block, correct the block
and record the correction in a comment rather than quietly rewriting history —
Issue #30's own label Evidence was corrected exactly this way, and the comment
is what makes the discrepancy traceable.

### Candidate directions

Prior analysis handed to the implementer: approaches already considered, dead
ends already walked, a decomposition that looked workable. **Non-binding and
optional by construction** — say so in the heading itself
(`Candidate directions (non-binding, optional)`) so no reader mistakes it for
a mandate. If a direction is in fact binding, it is a requirement and belongs
in Requirements.

### Out of scope

What this issue must not touch: adjacent files, decisions reserved for the
owner, work deferred to a sibling issue. This is what keeps a work-queue item
from growing into a rewrite, and it is what a reviewer reads the diff against.

### Verification

How done is proven — the commands that must pass, the artifacts that must
exist, and the measurement tier the change ships behind.

## Measurement policy

A Verification section names exactly one measurement tier, drawn from this
vocabulary:

- `suites-only` — the always-on deterministic suites.
- `smoke` — a mechanical functional pass/fail check per sub-release.
- `paired-mini` — an owner-triggered paired benchmark against the baseline of
  record.

One clause of orientation per tier, as above, is the ceiling. Do **not**
restate the tier definitions, their costs, their triggers, or the owner
measurement options in a work-queue issue or here — that duplication is how
the policy drifts. The canonical home is
[`bench/MEASUREMENT.md`](../bench/MEASUREMENT.md); drafts, issues, and PR
descriptions link there.

That policy supersedes the retired framing "every behavior-changing item ships
behind a paired bench measurement": paired measurements are rare, expensive,
and owner-triggered, not the default gate for every change.

## Labels

A work-queue issue carries `work-queue` plus the topic labels and `area:*`
labels that apply to it. `needs-triage` is for inbound issues filed through
the forms and is not used on maintainer-filed items.

The taxonomy is checked in at [`.github/labels.json`](../.github/labels.json):
one entry per label, with `name`, `color`, `description`, and a `category` of
`type`, `triage`, `topic`, or `area`. That file — not the live repo state — is
the reviewable source of truth, and `tests/test_contribution_surface.py`
asserts its shape and its coverage of the labels the issue forms apply.

Topic and area labels answer different questions, which is why pairs like
`bench` / `area:bench` and `tooling` / `area:ci` both exist and are not
duplicates. An `area:*` label says **where the change lands** — the directory a
reviewer will read the diff in. A topic label says **what concern the item
addresses**, wherever the code for it happens to live: a pipeline change made to
unblock the harness is `bench` + `area:workflows`, not `area:bench`.

Two rules that are easy to get wrong:

- **Plural `area:*` forms are canonical.** The seven area labels are
  `area:workflows`, `area:agents`, `area:scripts`, `area:skills`,
  `area:bench`, `area:docs`, and `area:ci`. Singular drift in a draft is a bug
  and the test suite fails on it.
- **A label must already exist in the repo before an issue form or an issue
  can apply it.** GitHub drops a form label that names a nonexistent label,
  silently: the issue still files, just unlabeled, and nothing reports the
  failure. Add the label to the manifest and sync it first, then reference it.

### Syncing the taxonomy to GitHub

Syncing is a maintainer step and needs a token with write access on the repo.
Derive the commands from the manifest and read them before running anything:

```bash
python3 -c "import json,shlex;[print('gh label create',shlex.quote(l['name']),'--color',l['color'],'--description',shlex.quote(l['description']),'--force') for l in json.load(open('.github/labels.json'))['labels']]"
```

Review the printed commands, then re-run the same line with `| sh` appended to
apply them.

`--force` updates an existing label in place — color and description — instead
of failing on conflict, so the sync is safe to re-run and safe to run against
a repo where some labels already exist. It never deletes: a label that exists
in the repo but is absent from the manifest is left untouched. Removing a
label is therefore always a deliberate, separate `gh label delete`.

## Related

- [`bench/MEASUREMENT.md`](../bench/MEASUREMENT.md) — canonical measurement
  policy: tier ladder, costs, triggers
- [`.github/labels.json`](../.github/labels.json) — the checked-in label
  taxonomy
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — contributor workflow and the
  CI-enforced test commands
- Issue #28 — codified the measurement policy, named mini subset, and smoke
  checker
- Issue #30 — contribution-surface refresh (forms, labels, `SECURITY.md`,
  this doc)
