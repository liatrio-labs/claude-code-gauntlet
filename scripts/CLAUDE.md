<!-- GENERATED from AGENTS.md by scripts/sync_agent_rules.py — do not edit.
     Claude Code's on-demand loader injects this file verbatim and does NOT expand
     @imports, so the rules must be physically present here. Edit AGENTS.md. -->

# scripts/

Retained Python is **stdlib only**; CI tooling in `pyproject.toml` `[dependency-groups]` is
exempt. Scripts are **language-agnostic**: never assume the reviewed codebase's language.

Run entry points as files (`python3 scripts/<name>.py`). Each entry uses the 11-line template
and imports one `gauntlet.<module>.CLI`; implementation and tests import `gauntlet.*`, never an
entry file. `gauntlet.cli.Command` owns the CLI boundary and adapts unconverted mains.

- **Repo root for searches.** `gauntlet.verify.decide` resolves the root at startup via
  `git rev-parse --show-toplevel`; symbol searches use `git grep -l` with `cwd=REPO_ROOT` and a
  3-second per-symbol timeout.
- **`gauntlet.project_rules` resolves `@path` import pointers, not just filenames.** `Read` does
  not expand the `@import` directive, and real repos ship `CLAUDE.md` as a single `@AGENTS.md`
  pointer, so a filename allowlist misses arbitrary targets. Resolved paths are confined via
  `realpath`, must be `.md`, are byte-bounded with `os.stat` before any `open`, and are depth-capped.
- **`gauntlet.marker` owns the prior-review marker:** it builds what `gauntlet.delivery.post` writes and
  parses what `gauntlet.prior_review` reads. Readers never branch on `version`: both token
  generations carry `"version":"3.0"` with different wire shapes. `TestRoundTrip` guards parity.
- **`gauntlet.jsjson` owns JS parity.** Both runtimes refuse non-integer or unsafe numbers
  to avoid divergent artifacts.
- **Always emit exactly one receipt line.** `gauntlet.artifacts`'s, `gauntlet.materialize`'s
  and `gauntlet.patches`'s `main()` fall back to a hand-built minimal receipt if the real one
  will not serialize: an empty stdout is indistinguishable from a dead executor.
- **`gauntlet.materialize` writes primaries** from `tasks/<task-id>.output`. Reuse task
  resolution, derivation, `gauntlet.jsjson` checksums and `gauntlet.fs` atomic writes.
- **Never print a returned payload to stdout.** `gauntlet.awaiting` reduces
  `persistReturn.entries` to `paths` + `resolvedPath` so the model never handles those bytes.
- **Stdout carries the payload or nothing.** Human-facing status lines go to stderr;
  `gauntlet.fix_tasks` calls `gauntlet.jsjson.write_result(obj)`.
- **Commands use `gauntlet.proc`; file reads and path checks use `gauntlet.fs`.**
