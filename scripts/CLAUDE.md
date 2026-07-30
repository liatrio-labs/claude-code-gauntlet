<!-- GENERATED from AGENTS.md by scripts/sync_agent_rules.py — do not edit.
     Claude Code's on-demand loader injects this file verbatim and does NOT expand
     @imports, so the rules must be physically present here. Edit AGENTS.md. -->

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
- **Always emit exactly one receipt line.** `assemble_artifacts.py`'s `main()` falls back to a
  hand-built minimal receipt if the real one will not serialize: an empty stdout is
  indistinguishable from a dead executor.
