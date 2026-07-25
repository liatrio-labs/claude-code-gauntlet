# Final review fix report

## Branch

`cursor/env-purity-receipts-1412`

## Findings fixed

- I1: `_script_path_matches_repo` now resolves relative `scriptPath` values against
  `repo_root`, so `workflows/pipeline.js` and `./workflows/pipeline.js` match the repo
  bundle independent of process CWD. Absolute paths still resolve against the expected
  `{repo_root}/workflows/pipeline.js`.
- I2: identity echo parsing now captures `pipeline_version` and `plugin_root` values up
  to the `(bundle)` / `(resolved)` source tag, preserving plugin roots with spaces.
- M1: `fake_claude` has a `no_identity_echo` mode that emits all eight expected knob
  lines without identity lines, plus payload and a success envelope. The invoker marks
  that run invalid with `plugin_identity_mismatch`.

## TDD evidence

Initial focused run failed only the three new regression tests:

```text
FAILED bench/tests/test_invoke.py::PluginIdentityGuardTest::test_missing_identity_lines_marks_invalid
FAILED bench/tests/test_invoke.py::IdentityReceiptHelpersTest::test_parse_identity_echo_preserves_plugin_root_spaces
FAILED bench/tests/test_invoke.py::ScriptPathMatchesRepoTest::test_relative_paths_are_repo_relative_not_cwd_relative
```

## Final verification

```text
python3 -m pytest bench/tests/test_invoke.py bench/tests/test_check.py bench/tests/test_headless_echo_contract.py -q
140 passed in 3.91s

python3 -m pytest bench/tests/ -q
497 passed, 22 subtests passed in 7.97s
```
