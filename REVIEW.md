# Review rules

Project rules for reviews of this repository. This file is collected as prose alongside
CLAUDE.md, AGENTS.md, and QODO.md; it does not configure filtering or delivery.

## Rules

Review-specific rules for this repo. These are checks a reviewer should apply that are not
obvious from the diff alone; the full engineering rules live in `AGENTS.md`.

- **CRITICAL: A regression test must fail against the bug it names.** A test whose name and
  docstring describe a defect, but which passes against the reverted implementation, is worse
  than no test. Flag any new test where the assertion could not distinguish the fix from the bug
  — the repo has shipped several.
- **CRITICAL: Never accept a phrase count as a guard.** A check that greps for wording is
  defeated by rewording. Flag any guard that asserts on the presence of a string where a
  structural property was available.
- **A rule stated in prose that could be enforced by code is a finding.** Prompt text, agent
  contracts and instruction files are the fallback for what cannot be made structural, not the
  first fix.
- **Cross-runtime lists derive from JS `DELTA_KEYS`.** The generator writes Python
  `DELTA_VALUE_FIELDS`; a `verifyWire.js` filter builds JS `DELTA_VALUE_KEYS`. Both
  drop `id` and `verified`.
- **One implementation per mechanism.** A second copy of a mechanism that already has a home is a
  finding.
- **A test that re-proves what another layer proves is a finding**, as is a near-copy a
  `parametrize` table would replace, a builder that computes an expected value, or a fixed-shape
  record passed as a loose dict or tuple.
- **Shipped code carries no history.** An issue number, date or incident narrative in a comment
  or docstring is a finding; it belongs in the PR.
- **`workflows/src/` must not reference host globals.** `structuredClone`, `Buffer`,
  `TextEncoder`, `URL`, `setTimeout`, `process` and `console` exist under `node --test` but not in
  the workflow sandbox, so a reference passes every test and throws on first live dispatch.
- **A new finding field must appear in `workflows/src/registry.js`.** A field declared only in an
  agent contract is rejected at dispatch by the closed item schema (`additionalProperties: false`)
  — schema retries, then a terminal agent failure that degrades the dimension.
- **Instruction files are loaded on every turn.** Flag additions to an `AGENTS.md`
  that are derivable from the code, already stated in a comment at the site, or that record
  history, version numbers, or past incidents.
- **Claims must name their measurement.** Flag a comment or doc that asserts a behaviour,
  frequency, or limit without saying how it was established.
