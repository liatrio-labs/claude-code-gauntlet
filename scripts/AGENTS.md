# scripts/

Retained Python is **stdlib only**; CI tooling in `pyproject.toml` `[dependency-groups]` is
exempt. Scripts are **language-agnostic**: never assume the reviewed codebase's language.

Run entry points as files (`python3 scripts/<name>.py`). Each entry uses the ten-line template
and imports one `gauntlet.<module>.CLI`; implementation and tests import `gauntlet.*`, never an
entry file. `gauntlet.cli.Command` owns the CLI boundary and adapts unconverted mains.

- **Repo root for searches.** `verify_findings.py` resolves the root at startup via
  `git rev-parse --show-toplevel`; symbol searches use `git grep -l` with `cwd=REPO_ROOT` and a
  3-second per-symbol timeout.
- **`collect_project_rules.py` resolves `@path` import pointers, not just filenames.** `Read` does
  not expand the `@import` directive, and real repos ship `CLAUDE.md` as a single `@AGENTS.md`
  pointer, so a filename allowlist misses arbitrary targets. Resolved paths are confined via
  `realpath`, must be `.md`, are byte-bounded with `os.stat` before any `open`, and are depth-capped.
- **`gauntlet.marker` owns the prior-review marker:** it builds what `post_review.py` writes and
  parses what `detect_prior_review.py` reads. Readers never branch on `version`: both token
  generations carry `"version":"3.0"` with different wire shapes. `TestRoundTrip` guards parity.
- **Numbers crossing to JS must be JS-reproducible.** Both runtimes refuse non-integer or
  out-of-safe-range values rather than write an artifact whose float spelling differs by language.
- **Always emit exactly one receipt line.** `assemble_artifacts.py`'s, `materialize_artifacts.py`'s
  and `report_patches.py`'s `main()` fall back to a hand-built minimal receipt if the real one
  will not serialize: an empty stdout is indistinguishable from a dead executor.
- **`materialize_artifacts.py` writes the primaries** from `tasks/<task-id>.output`. Reuse the
  awaiter's task resolution and the assembler's checksum, atomic write and derivation.
- **Never print a returned payload to stdout.** `await_workflow.py` reduces
  `persistReturn.entries` to `paths` + `resolvedPath` so the model never handles those bytes.
- **Stdout carries the payload or nothing.** Human-facing status lines go to stderr;
  `render_fix_tasks.py` calls `gauntlet.jsjson.write_result(obj)`.
