# AGENTS.md

Repository-specific guidance for AI agents. See `CLAUDE.md` for the authoritative
architecture, schema, and pipeline rules; `README.md` (Development section) and
`CONTRIBUTING.md` for the canonical dev commands.

## Cursor Cloud specific instructions

This repo is a Claude Code plugin with **no runtime dependencies** — the dev
environment is just the test/lint toolchain. Two languages are in play: Python 3
(stdlib-only pipeline scripts + pytest) and Node (the workflow bundle + `node --test`).

### Environment gotchas

- **Use `python3`, not `python`.** This VM has no `python` symlink; CI uses
  `python` because it runs on `actions/setup-python`. Run commands as
  `python3 -m pytest ...`.
- **Node is pinned to 24.18.0 via nvm.** The VM also ships a bundled node v22 at
  `/exec-daemon/node` that appears earlier on the default `PATH`. Setup appended a
  `PATH` prepend to `~/.bashrc` so the pinned Node 24 and user pip bins
  (`~/.local/bin`, where `pytest`/`pre-commit` live) win. New login shells (the
  default) pick this up automatically; if a tool resolves to the wrong version,
  start a login shell (`bash -lc '...'`) or check `which node`.
- **`node --test` needs the glob form.** Run `node --test workflows/test/*.test.js`
  — the bare directory form is not a valid target on node 24 (also noted in CLAUDE.md).

### Commands (all from repo root)

- Python tests: `python3 -m pytest tests/ -q`
- Bench self-tests: `python3 -m pytest bench/tests/ -q`
- Workflow JS tests: `node --test workflows/test/*.test.js`
- Lint (all hooks): `pre-commit run --all-files`
- Rebuild the JS bundle after editing `workflows/src/*.js`: `node workflows/build.js`
  (CI byte-verifies the committed `workflows/pipeline.js` against a fresh build).

### Notes

- `CHANGELOG.md` is release-automation generated. `markdownlint-fix` may want to
  reformat it — do not commit that incidental change.
- There is no application server / GUI. The "app" is the deterministic review
  gauntlet; exercise it by piping a findings JSON through the pipeline scripts,
  e.g. `python3 scripts/filter_findings.py <findings.json>`.
