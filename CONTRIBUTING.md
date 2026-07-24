# Contributing to claude-code-gauntlet

Thanks for your interest in contributing! This guide explains how to set up your environment, follow our style and commit conventions, run tests and linters, and submit pull requests.

## Overview

This repository provides a Claude Code plugin that orchestrates multi-agent code review with a deterministic Python verification pipeline. Contributions generally fall into one of these areas:

- Bug fixes in the pipeline scripts (`scripts/`)
- New or improved review agents (`agents/`)
- Skill orchestration improvements (`skills/`)
- Workflow pipeline (`workflows/`) and benchmark harness (`bench/`)
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

# Run full pre-commit checks across the repo
pre-commit run --all-files

# Run markdown linting only
pre-commit run markdownlint-fix --all-files

# Run docs spell checking only
pre-commit run cspell --all-files
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
pre-commit run --all-files
```

This will:

- Execute the pytest suite for pipeline scripts and the bench harness
- Execute the Node workflow test suite (bundle freshness / stage contracts)
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

- Ensure all checks pass (pre-commit and the test suite) before requesting review.
- Reference related issues where applicable.

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

## Code of Conduct

We strive to maintain a welcoming and respectful community. Please review our [Code of Conduct](CODE_OF_CONDUCT.md) to understand our community standards and expectations.

If you have any concerns, please contact the Liatrio Maintainers team (`@liatrio-labs/liatrio-labs-maintainers`) or use GitHub's private reporting form for this repository.

## References

- `README.md` — overview and quick start
- `bench/MEASUREMENT.md` — ratcheted measurement policy (canonical)
- `docs/maintainer-issues.md` — maintainer work-queue issue standard
- `.pre-commit-config.yaml` — linting and formatting hooks
- `.github/ISSUE_TEMPLATE/` — issue forms
- `docs/research/` — research artifacts informing the design
