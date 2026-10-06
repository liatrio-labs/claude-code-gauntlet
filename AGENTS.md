# AGENTS.md

`claude-code-gauntlet` is a Claude Code marketplace plugin: a multi-agent code review pipeline.
There is no server, database, or Docker. Running it means running the suites below.

`workflows/` and `agents/` each carry a short `AGENTS.md` of their own. `CONTRIBUTING.md` holds the
CI coverage gates and everything else a human contributor needs.

## Design

- **Build the mechanism, not the instruction.** Whatever code, a schema, a data structure, or a
  removed capability can enforce, it must. Prose in a prompt or agent file is the fallback.
- **"Add more text" is a design smell.** If that is the fix under consideration, change the shape.
- **Extending should cost one edit.** If a new dimension, field, or agent takes coordinated edits
  across several files, fix the shape instead of documenting the ritual.

## Commands

```bash
python -m pytest tests/ -q               # pipeline scripts
python -m pytest bench/tests/ -q         # benchmark harness
node --test workflows/test/*.test.js     # Node 24; a bare directory is not a valid target
node workflows/build.js                  # after editing workflows/src; commit the bundle
python3 workflows/test/tools/biome_check.py
pre-commit run --all-files               # the lint gate
```

pre-commit sees tracked files only, so `git add` a new file before trusting a green run.
`markdownlint-fix` fixes in place and then reports failure: re-stage and re-run.

## Constraints

- **No package manifest in the tree.** `scripts/` is stdlib-only Python 3.10, and the shipped
  bundle has zero dependencies. Tests enforce both, so do not reach for a dependency.
- **Language-agnostic.** Never assume the reviewed codebase's language: exclude non-source
  directories, never include by extension.
- **Python shape.** A JSON wire shape is a `TypedDict`, an internal record is a frozen slotted
  dataclass, a closed vocabulary is a `Literal`. `scripts/*.py` are thin entry files; logic lives
  in `scripts/gauntlet/`.
- **Tests are pytest functions** that prove a behaviour once, at the boundary callers use. A
  regression test must fail against the bug it names: mutate the whole mechanism and watch it go
  red, because a partial mutation falls through to a neighbouring fallback and passes.
- **Comments say why.** No history, dates, or issue numbers in code; those belong in the PR.
- **Never write the literal skip-ci token in a commit message**, even when writing about it.
  GitHub Actions scans the whole message and silently skips every workflow.
- **Session scratch stays out of the tree.** Plans, memos, and handoff notes go in the PR or the
  issue. Coverage data files stay out too: set `COVERAGE_FILE` to a temp path.

## Adding to this file

A line belongs here only if removing it would cause a mistake that no test, lint rule, or comment
at the code would catch. A test pins the size of these files, so an addition displaces something.
