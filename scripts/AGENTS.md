# scripts/

Retained Python. **stdlib only** — no pip dependencies — and **language-agnostic**: never assume a
language in the reviewed codebase.

- **Repo root for searches.** `verify_findings.py` resolves the root at startup via
  `git rev-parse --show-toplevel`; symbol searches use `git grep -l` with `cwd=REPO_ROOT` and a
  3-second per-symbol timeout.
- **`collect_project_rules.py` resolves `@path` import pointers, not just filenames.** `Read` does
  not expand the `@import` directive, and real repos ship `CLAUDE.md` as a single `@AGENTS.md`
  pointer, so a filename allowlist misses arbitrary targets. Resolved paths are confined via
  `realpath`, must be `.md`, are byte-bounded with `os.stat` before any `open`, and are depth-capped.
- **`review_marker.py` is the single source of truth** for the prior-review marker: it builds what
  `post_review.py` writes and parses what `detect_prior_review.py` reads back. Readers never branch
  on the payload's `version` field — both token generations carry `"version":"3.0"` despite being
  different wire shapes. `tests/test_review_marker.py::TestRoundTrip` guards the agreement.
- **Twins must stay at parity.** `merge_findings.py`, `finding_dedup.py`, `filter_findings.py`,
  `apply_validations.py` and `apply_challenges.py` each have a JS twin proven against frozen golden
  fixtures in `tests/fixtures/parity/`. Change one, change both, re-record the fixture.
- **Numbers crossing to JS must be JS-reproducible.** Both runtimes refuse non-integer or
  out-of-safe-range values rather than write an artifact whose float spelling differs by language.
- **Always emit exactly one receipt line.** `assemble_artifacts.py`'s and
  `materialize_artifacts.py`'s `main()` fall back to a hand-built minimal receipt if the real one
  will not serialize: an empty stdout is indistinguishable from a dead executor.
- **`materialize_artifacts.py` is what keeps a model out of the persist path.** It reads the
  workflow's own return out of `tasks/<task-id>.output` and writes the primaries itself. It must
  reuse `await_workflow.py`'s task resolution and `assemble_artifacts.py`'s checksum, atomic write
  and derivation — a second copy of either would be a second thing to keep at parity.
- **A payload from the harness is still confined.** Every entry path is `realpath`-checked inside
  `--output-dir` before anything is written, so neither a wrong `--output-dir` nor a symlink can
  scatter a review's artifacts outside it.
- **Never print a returned payload to stdout.** `await_workflow.py` elides `persistReturn.entries`
  down to `paths` + `resolvedPath` on purpose: its documented caller is a Bash call straight into
  the orchestrator's context, and the whole point is that those bytes reach disk without passing
  through a model. The replacement key is deliberately named differently so a consumer wanting the
  bytes fails loudly instead of writing empty files.
