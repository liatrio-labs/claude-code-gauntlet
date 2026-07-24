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

Every maintainer work-queue issue carries these six sections, in this order.
Headings are spelled as written, with one exception: Candidate directions
takes a parenthetical, whose wording is free (see that section).

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
ends already walked, a decomposition that looked workable. **Non-binding by
construction** — say so in the heading itself, so no reader mistakes it for a
mandate. The heading begins with `Candidate directions` and carries a
parenthetical stating that the section is non-binding; the exact wording of
that parenthetical is free. `Candidate directions (non-binding, optional)` and
`Candidate directions (non-binding, from prior analysis)` both satisfy the
rule. If a direction is in fact binding, it is a requirement and belongs in
Requirements.

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
- `full-holdout` — the owner-triggered release-grade confirmation runs.

Those four slugs are the `Slug` column of the tier ladder in
[`bench/MEASUREMENT.md`](../bench/MEASUREMENT.md), which is where a reader who
does not recognize one resolves it. Nothing outside that ladder is a tier.

One clause of orientation per tier, as above, is the ceiling. Do **not**
restate the tier definitions, their costs, their triggers, or the owner
measurement options in a work-queue issue or here — that duplication is how
the policy drifts. The canonical home is the runbook linked above; drafts,
issues, and PR descriptions link there.

That policy supersedes the retired framing "every behavior-changing item ships
behind a paired bench measurement": paired measurements are rare, expensive,
and owner-triggered, not the default gate for every change.

## Labels

A work-queue issue carries `work-queue` plus the topic labels and `area:*`
labels that apply to it. `needs-triage` is for inbound issues filed through
the forms and is not used on maintainer-filed items.

The taxonomy is checked in at [`.github/labels.json`](../.github/labels.json):
one entry per label, with `name`, `color`, `description`, and a `category`.
The manifest is where a label change is proposed and reviewed; the repository's
own labels are what GitHub actually applies. The two are meant to be identical,
the manifest leads, and the drift check below is how that is settled — a
manifest entry that was never synced is not a label.

`tests/test_contribution_surface.py` asserts the manifest's shape — entries
name-sorted and unique, exactly those four keys, six-digit lowercase hex
colors, a non-empty description inside GitHub's 100-character limit, a
category drawn from the six below — and that every label the issue forms
auto-apply resolves to an entry in it. It cannot see GitHub: a label that is
in the manifest and not in the repo passes the suite and still drops silently
on submission. Only the sync and the drift check below settle that.

### Categories

`category` is manifest metadata, not something GitHub stores. It records what
kind of statement a label makes, so a triager can see the six axes at once:

| Category | Question it answers | Labels |
|----------|---------------------|--------|
| `type` | What kind of item is this? | `bug`, `documentation`, `enhancement`, `question` |
| `state` | Where is it in the queue? | `needs-triage`, `work-queue` |
| `resolution` | Why was it closed without a fix? | `duplicate`, `invalid`, `wontfix` |
| `outreach` | Who is being invited to pick it up? | `good first issue`, `help wanted` |
| `topic` | What concern does it address? | `bench`, `benchmarking`, `determinism`, `latency`, `policy`, `process`, `reliability`, `tooling`, `verify-boundary` |
| `area` | Where does the change land? | the seven `area:*` labels |

An item normally carries at most one `type` and one `state`, a `resolution`
only when it closes unfixed, and as many `topic` and `area` labels as apply.

### Telling near-duplicate labels apart

Topic and area labels answer different questions, which is why pairs like
`bench` / `area:bench` and `tooling` / `area:ci` both exist and are not
duplicates. An `area:*` label says **where the change lands** — the directory a
reviewer will read the diff in. A topic label says **what concern the item
addresses**, wherever the code for it happens to live: a pipeline change made to
unblock the harness is `bench` + `area:workflows`, not `area:bench`.

Within the topic labels, two pairs need a decision rule:

- **`bench` is the machinery; `benchmarking` is the measurement.** Use `bench`
  for the harness itself — the runner, the golden fixtures, the mirrors, the
  ledger plumbing. Use `benchmarking` for taking a measurement — running a tier,
  scoring, the judge, the metrics, the cost. A broken mirror cache is `bench`;
  recording a new baseline of record is `benchmarking`. An item that changes
  the harness in order to take a measurement carries both.
- **`policy` beats `benchmarking` on measurement policy.** Deciding which tier
  gates what, when a paired run is required, or how the ladder ratchets is
  `policy`, not `benchmarking` — `benchmarking` is for measuring under the
  policy, never for setting it. Where the two overlap, `policy` wins.

Two more rules that are easy to get wrong:

- **Plural `area:*` forms are canonical.** The seven area labels are
  `area:workflows`, `area:agents`, `area:scripts`, `area:skills`,
  `area:bench`, `area:docs`, and `area:ci`. Five of them have a singular form
  to drift to — the agents, docs, scripts, skills, and workflows names — and
  `tests/test_contribution_surface.py` fails on any singular spelling of those
  five inside the file set it scans: every `.md`, `.yml`, `.yaml`, and `.json`
  file under `.github/` and `docs/`, plus `CONTRIBUTING.md`, `README.md`,
  `SECURITY.md`, `CLAUDE.md`, and `AGENTS.md`. That is the whole guarantee.
  Drift in an issue body, a PR description, a commit message, or a source file
  is outside the scan and nothing catches it.
- **A label must already exist in the repo before an issue form or an issue
  can apply it.** GitHub drops a form label that names a nonexistent label,
  silently: the issue still files, just unlabeled, and nothing reports the
  failure. Add the label to the manifest and sync it first, then reference it.

### Syncing the taxonomy to GitHub

Syncing is a maintainer step and needs a token with write access on the repo.
Run every command in this section from the repo root, so the relative paths
resolve. Derive the commands from the manifest and read them before running
anything:

```bash
python3 .github/labels_diff.py --commands
```

That prints one `gh label create ... --force` command per manifest label —
every label, not only the drifted ones, which is safe because `--force` makes
each command idempotent. Add `--live` to narrow the output to the labels that
actually differ. Review them, then apply them under a shell that aborts on the
first failure, so a single rejected label is loud rather than a silent partial
sync:

```bash
set -euo pipefail
python3 .github/labels_diff.py --commands | sh -eu
```

`--force` updates an existing label in place — color and description — instead
of failing on conflict, so the sync is safe to re-run and safe to run against
a repo where some labels already exist. It never deletes: a label that exists
in the repo but is absent from the manifest is left untouched. Removing a
label is therefore always a deliberate, separate `gh label delete`.

The cost of `--force` is that it overwrites: a color or description someone
curated by hand in the GitHub UI is replaced by the manifest's value, with no
prompt and no diff. If a live label carries wording worth keeping, put it in
`.github/labels.json` first and sync second.

Confirm the sync landed rather than assuming it did. The same helper in
`--live` mode compares the manifest against the repo's labels and exits 1 on
any drift:

```bash
gh api "repos/liatrio-labs/claude-code-gauntlet/labels" --paginate | python3 .github/labels_diff.py --live -
```

It reports three things: labels in the manifest that the repo is missing,
labels whose live color or description has drifted from the manifest, and
labels that exist in the repo but not in the manifest — those last are listed
and left alone, never deleted. `--commands --live <file>` then emits commands
for exactly the first two groups.

CI carries the same check as the "Verify Label Taxonomy" workflow
(`.github/workflows/labels-verify.yml`), triggered manually with
`workflow_dispatch` after a sync, because a sync needs write access a pull
request does not get. The drift check is what makes the manifest verifiable
instead of aspirational: without it, a repo that was never synced looks exactly
like one that was.

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
