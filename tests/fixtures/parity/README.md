# Parity fixtures

`finding_dedup`, `merge_findings`, `apply_validations`, `filter_findings`, and
`apply_challenges` are JavaScript-owned. Change their `expected.json` files only
with `UPDATE_GOLDENS=1 node --test workflows/test/goldens.test.js` and review the
resulting diff.

`verify_deltas`, `slice_input_proof`, and `slice_inline` are verify-wire fixtures
recorded by `workflows/test/tools/record_parity.py` from the live Python verifier.
