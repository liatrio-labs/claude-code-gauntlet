<!--
PR Title Format: <type>(<optional scope>): <description>

Valid types: feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert

Examples:
  feat(security): add SSRF detection patterns
  fix: resolve race condition in agent dispatch
  docs: update installation instructions

The PR title will be validated automatically.
-->

## Why?

<!-- Summarize the motivation for this change. Reference issues as needed. -->

## What Changed?

<!-- Call out the key updates in this PR. -->

## Additional Notes

<!-- Optional: document follow-ups, rollout concerns, or reviewer guidance. -->

- [ ] Pipeline tests pass: `python -m pytest tests/ -q`
- [ ] Bench self-tests pass: `python -m pytest bench/tests/ -q`
- [ ] Workflow tests pass: `node --test workflows/test/*.test.js` (Node 24.18.0; the glob is required)
- [ ] Bundle rebuilt and byte-exact: `node workflows/build.js` then `git diff --exit-code workflows/pipeline.js`
- [ ] Parity fixtures regenerated with `workflows/test/tools/record_parity.py` if a deterministic transform changed
- [ ] Linters/hooks pass: `pre-commit run --all-files`
- [ ] Docs, skill references, and agent contracts updated if behavior changed
