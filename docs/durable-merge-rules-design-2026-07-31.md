# Durable merge rules (#58 + #108) — design

Date: 2026-07-31
Issues: [#58](https://github.com/liatrio-labs/claude-code-gauntlet/issues/58) (Wave 1), [#108](https://github.com/liatrio-labs/claude-code-gauntlet/issues/108) (Wave 2, #55 audit)
Roadmap: [#101](https://github.com/liatrio-labs/claude-code-gauntlet/issues/101)
Tier: suites-only

## Goal

Make merge-to-`main` durable: required CI status checks, explicit bypass, documented zero-approval bar, tag integrity for `v*`, squash-only history coherent with PR-title lint + semantic-release, and code-side guards so new always-on gates update the ruleset and a frozen name list together.

**#108 is the vehicle; #58 closes as a byproduct** when the branch ruleset carries the required checks and the review/bypass decisions are recorded.

## Approach

**Settings first, then one code PR.**

1. Owner GitHub settings (ruleset, tag ruleset, PR merge options).
2. One PR: citation prose fixes + frozen check-name test + residue scrub + rules cross-ref check + octo-sts `job_workflow_ref` pin + light docs.
3. Evidence on the issues; close #58 and #108; note the ratchet on #55 follow-ups.

## Scope

### In scope

| Track | Deliverable |
| --- | --- |
| Branch ruleset `protect-default-branch` (id `16049246`) | `required_status_checks` with nine contexts; approvals stay `0` (documented); Octo STS sole bypass; deletion + non-fast-forward kept; strict “up to date” **off** |
| Tag ruleset (new) | `refs/tags/v*`; Restrict deletions + Restrict updates; Octo STS bypass only; no Restrict creations |
| Repo PR settings | Squash-only; auto-delete head branches |
| Code | Frozen required-check tuple + parser assertion; repo-wide rules-file residue scrub; quoted rules cross-ref check; octo-sts claim pin; citation retargets |
| Process | Dated `gh api` evidence; smoke that a failed required check blocks merge; close #58/#108; ratchet notes on #102/#105 etc. |

### Out of scope

- Inventing new CI jobs, coverage gates, linters, scanners (#55 follow-ups / #102–#107).
- Requiring CodeQL, Analyze (\*), Cursor Bugbot, or path-filtered `labels-verify`.
- `require_code_owner_review` / raising approving review count above 0.
- Asserting the live ruleset from CI via `gh api` (would need a new admin credential).
- Action SHA-pinning, Scorecard, signed commits/tags, GOVERNANCE/SUPPORT docs.

## Required status check contexts

Exact GitHub check-run names (re-derived 2026-07-31 from PR #111 head):

- `Run Linting`
- `Run Tests (3.10)`, `Run Tests (3.11)`, `Run Tests (3.12)`
- `Run Workflow JS Tests`
- `Run Bench Self-Tests (3.11)`, `Run Bench Self-Tests (3.12)`
- `Validate plugin.json`
- `lint-pr-title`

Source workflows: `.github/workflows/ci.yml`, `validate.yml`, `pr-title-lint.yml`.

## Settings sequence

1. **Update ruleset 16049246**
   - Add required status checks (nine contexts above).
   - Do **not** enable “Require branches to be up to date before merging”.
   - Keep `pull_request` with `required_approving_review_count: 0`.
   - Set ruleset description: zero approvals is deliberate (solo owner; GitHub blocks self-approval).
   - Keep `bypass_actors`: Integration `801323` (Octo STS), mode `always` only.
   - Keep `deletion` and `non_fast_forward`.

2. **Create tag ruleset**
   - Enforcement Active; include `refs/tags/v*`.
   - Rules: Restrict deletions, Restrict updates (not creations).
   - Bypass: Octo STS only.

3. **Repo → General → Pull Requests**
   - Disable merge commits and rebase merging (squash only).
   - Enable automatically delete head branches.

4. **Evidence**
   - `gh api repos/liatrio-labs/claude-code-gauntlet/rulesets/16049246` (and list endpoint for the new tag ruleset).
   - Smoke: PR with failed required check (e.g. `Run Tests (3.12)`) cannot merge without bypass — UI merge box and/or API mergeability. If a throwaway failing PR is impractical, document merge-box / status-rollup proof instead.

## Code PR layout

### Citation prose (same PR, before gate is green)

- `skills/code-gauntlet/SKILL.md` — retarget the false-positive / duplication doctrine citation to real text in `agents/AGENTS.md` (stop quoting a non-existent CLAUDE.md sentence).
- `skills/code-gauntlet/references/ndjson-emission-contract.md` — drop the invented CLAUDE.md heading; point at the surviving doctrine.

Mutation proof for the cross-ref check is taken against pre-fix text locally, then fixes ship with the gate.

### Frozen required-check names — `tests/test_contribution_surface.py`

- Literal tuple of the nine contexts.
- Docstring names ruleset **16049246** and states that any new always-on PR gate updates **both** the tuple and the ruleset in the same change.
- Derive names from each contributing job: use the job-level `name:` when present, otherwise the job id (this is why `pr-title-lint.yml` yields `lint-pr-title` rather than the workflow title). Expand matrix `python-version:` axes as `"<job name> (<value>)"`.
- Assert derived set equals the frozen tuple.
- Mutation: change a contributing job name or matrix axis → test red.

### Residue scrub — `tests/test_agent_contracts.py`

- Enumerate tracked rules files with `git ls-files` matching root and one-level `AGENTS.md` / `CLAUDE.md` (same discovery shape as `scripts/sync_agent_rules.py` / `agents_dirs()`).
- Split pattern: `printf|ndjson|validate_ndjson` on all those files; `Bash` only on the `agents/` pair (so `scripts/AGENTS.md` shell discussion does not false-positive).

### Rules cross-reference check — `tests/test_agent_instruction_layout.py`

- Scan `skills/**/*.md` and `agents/*.md` for quoted spans attributed to a rules file; each quote must appear in some tracked `AGENTS.md` / `CLAUDE.md`.
- Keep parsing narrow (prior art: existing referenced-path / symbol checks). Do not build a full markdown AST.

### Octo-sts pin — `.github/chainguard/main-semantic-release.sts.yaml`

- Constrain so only `.github/workflows/release.yml` on `refs/heads/main` can mint `contents: write` under the `main-semantic-release` identity.
- Add an octo-sts `claim_pattern` (or the policy field that maps OIDC claims) pinning `job_workflow_ref` to the exact string observed on a real `release.yml` run before editing the file. Prefer fail-closed if the claim is mis-copied.
- Wrong pattern fails releases closed (safe direction).

### Docs

- Short note in `CONTRIBUTING.md` (contributor-facing): merges to `main` are CI-gated by the nine required checks; approvals stay 0 by design; bypass is Octo STS for release only.
- Comments on #55 follow-ups that add always-on gates (#102, #105, …): update ruleset + frozen tuple together.

## Failure modes

| Failure | Response |
| --- | --- |
| Job renamed without updating freeze tuple | Contribution-surface test fails; merge blocked once checks are required |
| Tuple updated but live ruleset not | Process backstop (docstring + issue ratchet); no CI admin-token assertion |
| Wrong octo-sts `job_workflow_ref` | Releases fail closed; re-read OIDC claim from a release run and fix |
| Tag ruleset blocks tag **creation** | Unexpected — creations are not restricted; verify bypass actor 801323 |
| Smoke failing PR impractical | Document UI/API proof that merge is disabled while a required check is failed/pending |

## Verification

- Settings: dated ruleset API dumps; smoke or documented merge-block proof.
- Code: mutate each guarded surface (three workflows, eight rules files, planted bad quote) → red; restore → green.
- Suites: `pre-commit run --all-files`; `python -m pytest tests/ -q`; `python -m pytest bench/tests/ -q`; `node --test workflows/test/*.test.js`; `node workflows/build.js && git diff --exit-code workflows/pipeline.js`.
- Post-merge: first release still cuts a `v*` tag under the new tag ruleset + tightened octo-sts policy.
- Close #58 (subsumed) and #108; update #101 wave notes as appropriate.

## Decisions already locked

- Approvals remain **0** (solo-owner deadlock if raised).
- Strict up-to-date policy stays **off**.
- Octo STS remains the only ruleset bypass; harden via trust-policy claim, do not remove bypass.
- Do not require CodeQL / Bugbot / labels-verify.
- #108 owns the fuller hardening; #58 does not get a separate overlapping ruleset edit.
