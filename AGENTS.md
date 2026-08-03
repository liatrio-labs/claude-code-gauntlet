# AGENTS.md

`claude-code-gauntlet` is a Claude Code marketplace plugin — no server, database, or Docker.
"Running it" means running the test suites and the stdlib-only Python pipeline scripts.

Directory-scoped rules live in nested `AGENTS.md` files: `workflows/`, `scripts/`, `agents/`.
Read the one for the directory you are editing.

## Design

**Build the mechanism, not the instruction.** Whatever code, a schema, a data structure, or a
removed capability can enforce, it must. Prose in an instruction, prompt, or agent file is the
fallback for what cannot be made structural.

**"Add more text" is a design smell.** If that is the fix under consideration, the shape is
wrong — change the shape.

**Extending should cost one edit.** A new dimension, field, or agent belongs in one place. If it
takes coordinated edits across N files, fix the shape rather than documenting the ritual.

## Scripts

- **stdlib-only Python.** No pip dependencies in shipped `scripts/` runtime —
  nothing under `scripts/` may import a non-stdlib module. Pinned CI tooling in
  `pyproject.toml` `[dependency-groups]` (pytest, pytest-cov, coverage) is exempt
  and never runs inside the plugin.
- **Language-agnostic.** Scripts must not assume a language in the reviewed codebase. Use
  `--exclude-dir` for non-source directories, never `--include=*.py`-style filters.

## Tests

```bash
python -m pytest tests/ -q            # pipeline + boundary parity
python -m pytest bench/tests/ -q      # benchmark harness self-tests
node --test workflows/test/*.test.js  # needs Node 24; the bare directory is not a valid target
```

Coverage gates (CI 3.12 only). Locally, COVERAGE_FILE must be outside the repo
tree (an in-tree data file trips the bench plugin-mutation guard):

```bash
COVDIR="$(mktemp -d)"
COVERAGE_FILE="$COVDIR/.coverage" python -m pytest tests/ -q \
  --cov=scripts --cov=.github --cov-fail-under=91.3
COVERAGE_FILE="$COVDIR/.coverage" python -m pytest bench/tests/ -q \
  --cov=bench --cov-fail-under=87
```

Floors 91.3 / 87, pinned 2026-08-03 from the first green 3.12 CI run (92.27 /
87.96); policy: a floor sits no more than 1.0 pp below the CI 3.12 measurement.
`workflows/test/tools/record_parity.py` is test infrastructure, outside both
scopes. Lower a floor only in the PR that causes the drop, reason in the body.
A sudden multi-point drop means broken subprocess capture — fix capture, do not
lower.

After editing `workflows/src/*.js`, rebuild and confirm the bundle is unchanged:

```bash
node workflows/build.js && git diff --exit-code workflows/pipeline.js
```

**A regression test must fail against the bug it names.** Verify that by mutating the
implementation and watching it go red — not by reading the test. Mutate the whole mechanism; a
partial mutation falls through to a neighbouring fallback and passes misleadingly.

## Lint

`pre-commit run --all-files` is the gate.

- `markdownlint-fix` rewrites files in place, then reports failure if it changed anything. A
  "Failed / files were modified by this hook" result means the fixes are already applied — re-stage
  and re-run.
- `CHANGELOG.md` is excluded from that hook deliberately; it is regenerated on every release. Do
  not re-add it.
- pre-commit sees **git-tracked files only**. A new file is invisible to every hook until
  `git add`, so it can pass locally and fail in CI. Stage new files before trusting a green run.
- **Never put the literal skip-ci token in a commit message, even when writing about it.** GitHub
  Actions scans the whole message, so the workflows silently never run — and a squash merge carries
  it onto `main`. Call it "the skip-ci token".

## Writing JSON for pipeline scripts

Use `python3 -c "import json; ..."`. Not the Write tool (it requires a prior Read), and not a
heredoc (zsh corrupts `!`).

## Output directory

`{output_dir}` defaults to `.code-gauntlet/` (repo-local, gitignored). Override with
`$CODE_GAUNTLET_OUTPUT_DIR`.

## Layout

```text
claude-code-gauntlet/         <- plugin root
├── agents/                   <- subagent contracts
├── scripts/                  <- retained Python (verify_findings.py, post_review.py, ...)
├── workflows/                <- JS pipeline: src/*.js, build.js, pipeline.js (generated), test/
├── bench/                    <- benchmark harness (stdlib-exempt)
├── tests/                    <- pytest suite
└── skills/code-gauntlet/     <- skill base directory
```

`{plugin_root}` is two levels above the skill base directory. Never locate it by searching the
filesystem — a `find` hit picks arbitrarily among every cached plugin version.

Session scratch — design memos, implementation plans, handoff notes — never lands in the tree; it
belongs in the PR description or issue thread. Tracked docs are allowlisted by
`tests/test_docs_registry.py`; a durable doc is added there in the same commit, with a reason.

## Contribution surface

`tests/test_contribution_surface.py` covers the issue-form schema, `.github/labels.json`, and the
CI commands quoted in `CONTRIBUTING.md`. Two things it cannot reach, both needing write access
after a merge:

- GitHub serves issue forms from the default branch only, and refuses to render an invalid form
  without erroring. A plan that tests form submission before the merge is mis-ordered.
- Labels must exist before a form can apply one; GitHub drops unknown labels silently. Sync with
  `python3 .github/labels_diff.py --commands --repo <owner>/<repo>`, confirm with `--live -`. See
  `docs/maintainer-issues.md`.
