# Contributing to claude-code-gauntlet

Thanks for your interest in contributing! This guide explains how to set up your environment, follow our style and commit conventions, run tests and linters, and submit pull requests.

## Overview

This repository provides a Claude Code plugin that orchestrates multi-agent code review through a deterministic
pipeline: a generated JavaScript workflow bundle (`workflows/pipeline.js`) plus retained standard-library Python
scripts (`scripts/`) for verification, posting, and parity. Contributions generally fall into one of these areas:

- Review pipeline stages in the workflow bundle source (`workflows/src/`) — see
  [The v3 workflow pipeline](#the-v3-workflow-pipeline-js)
- Bug fixes in the retained Python scripts (`scripts/`), including the parity twins of the JS stages
- New or improved review agents (`agents/`)
- Skill orchestration improvements (`skills/`)
- Benchmark harness, golden set, and measurement tooling (`bench/`)
- Documentation, examples, and research updates
- New tests or coverage gaps in the test suite (`tests/`, `bench/tests/`, `workflows/test/`)

Please open an issue first for significant changes to discuss the approach.

## Measurement policy (contributors)

External PRs ship behind the **always-on deterministic suites only** — not a
paired bench measurement. The functional smoke (`bench/run.py --tier smoke` +
`--check`) is run by the release manager. Paired mini-subset / full-15 / holdout
runs are owner-triggered and reserved for changes that plausibly move recall or
noise. See [`bench/MEASUREMENT.md`](bench/MEASUREMENT.md) for the full tier
ladder, costs, and pre-registered owner options.

## Getting Started

1. Fork and clone the repository.
2. Ensure you have Python 3.12+ installed (for the test suite and pre-commit hooks).
3. Set up the development environment:

```bash
pip install pre-commit
pre-commit install
```

Pipeline scripts use only the Python standard library — no runtime dependencies are required to run the plugin.

## Development Setup

- Install pre-commit hooks once with `pre-commit install`.
- Keep changes small and focused; prefer incremental PRs.
- Pipeline scripts must remain stdlib-only (no `pip install` runtime dependencies).
- Pipeline scripts must remain language-agnostic — they make no assumptions about the language of the codebase being reviewed.

### Recommended: Secret Scanning Pre-commit Hooks

Secret scanning (`gitleaks`) is included in `.pre-commit-config.yaml` and runs on every commit. To prevent accidental commits of API keys or tokens during development, ensure pre-commit is installed locally with `pre-commit install`.

> ⚠️ **Note:** To keep your hooks current with the latest versions, periodically run `pre-commit autoupdate`.

### Common Commands

```bash
# Pipeline pytest suite
python -m pytest tests/ -q

# Bench harness unit tests (stdlib; no API spend)
python -m pytest bench/tests/ -q

# JS workflow tests (requires Node 24.18.0)
node --test workflows/test/*.test.js

# Rebuild the generated workflow bundle after editing workflows/src/
node workflows/build.js

# Run full pre-commit checks across the repo
pre-commit run --all-files

# Run markdown linting only
pre-commit run markdownlint-fix --all-files

# Run docs spell checking only
pre-commit run cspell --all-files
```

Pass the test glob explicitly: the bare directory form is not a valid `node --test` target on node 24.

## The v3 workflow pipeline (JS)

The review pipeline runs inside Claude Code's workflow runtime, so it carries constraints the Python side does not.

- **Source lives in `workflows/src/*.js`.** Those modules use ESM `import`/`export` for test time only; the shipped
  artifact has no module system at all.
- **`workflows/pipeline.js` is generated — never hand-edit it.** `node workflows/build.js` strips the
  import/export lines and concatenates `workflows/src/*.js` into that single dependency-free bundle. Rebuild after
  every source change and commit the result.
- **Only JSON-safe language globals are guaranteed.** The workflow runtime sandbox does not provide
  `structuredClone`, `Buffer`, `TextEncoder`/`TextDecoder`, `URL`, `setTimeout`/`queueMicrotask`, `process`, or
  `console`. Referencing one keeps every local test green and then throws on the first live dispatch, so treat the
  absence as a hard constraint — deep-clone with the bundle's JSON round-trip helper, not `structuredClone`.
- **Node 24.18.0 is the pinned runtime** for the tests and the build. There is no `package.json` and no
  `node_modules`; use Node built-ins only.
- **Bundle freshness is enforced.** `tests/test_bundle_fresh.py` and CI both rebuild and compare, so a stale or
  hand-edited bundle fails the build:

```bash
node workflows/build.js
git diff --exit-code workflows/pipeline.js
```

### Parity fixtures

Five deterministic transforms (`mergeFindings`, `findingDedup`, `filterFindings`, `applyValidations`,
`applyChallenges`) exist twice: as JS stages in the bundle and as the authoritative Python twins under `scripts/`.
They are held at parity by frozen golden fixtures at
`tests/fixtures/parity/<script>/<case>/{input,expected}.json`, which both runtimes replay.

Never hand-edit a fixture to make a test pass — a fixture that no longer matches recorded behavior is exactly the
drift the fixtures exist to catch. When a transform's intended behavior genuinely changes, change the Python twin
and the JS stage together, then regenerate with the recorder and review the resulting diff:

```bash
python3 workflows/test/tools/record_parity.py                  # every case
python3 workflows/test/tools/record_parity.py filter_findings  # one script
```

## Style and Quality

- Markdown is linted using markdownlint (via pre-commit). Keep lines reasonably short and headings well structured.
- Public-facing docs are spell-checked with cspell. Add broadly reusable project terms to `cspell.config.yaml` and prefer file-specific overrides for one-off names.
- YAML and TOML files are validated for syntax errors.
- Commit messages must follow the Conventional Commits specification (enforced via commitlint).
- Keep documentation consistent with `README.md`.

## Testing

Before submitting a PR, run:

```bash
python -m pytest tests/ -q
python -m pytest bench/tests/ -q
node --test workflows/test/*.test.js
node workflows/build.js && git diff --exit-code workflows/pipeline.js
pre-commit run --all-files
```

This will:

- Execute the pytest suite for pipeline scripts and the bench harness
- Execute the Node workflow test suite (bundle freshness / stage contracts)
- Confirm the committed bundle is byte-identical to a fresh build
- Check YAML and TOML syntax
- Fix Markdown formatting issues
- Spell-check public-facing documentation
- Scan for committed secrets
- Validate the commit message format (on commit)

Live bench smoke and paired measurements are **not** contributor gates — see
[`bench/MEASUREMENT.md`](bench/MEASUREMENT.md).

## Branching and Commit Conventions

### Branch Naming

Use short, descriptive branch names with a category prefix:

- `feat/<short-topic>`
- `fix/<short-topic>`
- `docs/<short-topic>`
- `chore/<short-topic>`
- `refactor/<short-topic>`

Examples:

- `feat/new-agent`
- `docs/usage-examples`
- `fix/verify-findings-blame`

### Conventional Commits

We follow the Conventional Commits specification. Examples:

- `feat: add new severity dimension to bug-detector`
- `fix: correct blame classification for renamed files`
- `docs: add usage examples`
- `chore: update markdownlint config`

If a change is breaking, include `!` (e.g., `feat!: change findings JSON schema`).

## Pull Requests

- Keep PRs focused and well scoped.
- **PR titles must follow Conventional Commits format** (e.g., `feat: add new feature`). This is enforced by an automated check.
- PR description template:

```markdown
## Why?

## What Changed?

## Additional Notes
```

- Ensure all checks pass (pre-commit and the test suite) before requesting review. The checklist in
  `.github/pull_request_template.md` enumerates every CI-enforced gate; tick the ones that apply and say why for
  any you skipped.
- Reference related issues where applicable. For work-queue issues, keep the PR aligned with the issue's stated
  requirements and verification steps ([`docs/maintainer-issues.md`](docs/maintainer-issues.md)).

### PR Title Format

PR titles are validated automatically and must follow this format:

```text
<type>(<optional scope>): <description>
```

**Valid types:** `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`

**Examples:**

- `feat(security): add SSRF detection patterns`
- `fix: resolve race condition in agent dispatch`
- `docs: update installation instructions`
- `chore: bump pre-commit hook versions`

The description should:

- Start with a lowercase letter
- Be concise and descriptive
- Use imperative mood (e.g., "add" not "added" or "adds")

**Breaking changes:** Add `!` after the type (e.g., `feat!: change findings JSON schema`)

If the automated check fails, update your PR title and it will re-run automatically.

## Issue Templates

Use the GitHub issue templates under `.github/ISSUE_TEMPLATE/` for bug reports, feature requests, and questions. These templates prompt for summary, context, reproduction steps, and the relevant phase, agent, or pipeline script.

The label taxonomy those forms draw from is checked in at `.github/labels.json`. GitHub applies an issue-form label
only when the label already exists in the repository, and silently drops it otherwise — so a new form label has to
land in `labels.json` and in the repository before the form can apply it.

Maintainer-authored work-queue issues follow a stricter standard than the public forms:
[`docs/maintainer-issues.md`](docs/maintainer-issues.md) defines the required sections and the evidence an issue
needs before it is queued. Read it before filing or picking up work-queue issues.

Suspected vulnerabilities do not belong in any issue form — follow [`SECURITY.md`](SECURITY.md) and use the private
advisory form instead.

## Code of Conduct

We strive to maintain a welcoming and respectful community. Please review our [Code of Conduct](CODE_OF_CONDUCT.md) to understand our community standards and expectations.

If you have any concerns, please contact the Liatrio Maintainers team (`@liatrio-labs/liatrio-labs-maintainers`) or use GitHub's private reporting form for this repository.

## References

- `README.md` — overview and quick start
- `CLAUDE.md` — repo conventions the pipeline itself is held to (schema, runtime, plugin layout)
- `SECURITY.md` — supported versions, private reporting channel, and scope
- `bench/MEASUREMENT.md` — ratcheted measurement policy (canonical)
- `docs/maintainer-issues.md` — maintainer work-queue issue standard
- `.pre-commit-config.yaml` — linting and formatting hooks
- `.github/ISSUE_TEMPLATE/` — issue forms
- `.github/labels.json` — checked-in label taxonomy the forms resolve against
- `docs/research/` — research artifacts informing the design
