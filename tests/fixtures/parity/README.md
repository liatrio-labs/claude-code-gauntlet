# JS/Python parity fixtures

Each case is a directory `<script>/<case>/` with:
- `input.json` — a single object whose keys are the twin function's parameter names.
- `expected.json` — the full return value as a named-key object (tuples become
  `{name: value}`). Generated FROM the authoritative Python twin by
  `workflows/test/tools/record_parity.py`; never hand-edit.

Both runtimes assert against `expected.json`:
- Python: `tests/test_parity_fixtures.py` (Python twin == expected; also asserts
  `record_parity.py` output is unchanged — golden freshness).
- JS: `workflows/test/parity.test.js` (JS twin == expected).

Assertion rule: decision outcomes and integer counts/stats are asserted EXACTLY.
Free-text fields (`elimination_reason`, warning bodies, `escalation_note`,
`corroborated_by` ordering of equal keys) are asserted for substring/prefix
presence only — Python f-string formatting need not match JS template strings.
