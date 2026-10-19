# Parity fixtures

`finding_dedup`, `merge_findings`, `apply_validations`, `filter_findings`, and
`apply_challenges` are JavaScript-owned. Change their `expected.json` files only
with `UPDATE_GOLDENS=1 node --test workflows/test/goldens.test.js` and review the
resulting diff.

`verify_deltas`, `slice_input_proof`, and `slice_inline` are verify-wire fixtures
recorded by `workflows/test/tools/record_parity.py`. The Python recorder uses
the JavaScript encoder for `slice_inline`.

`review_text_checksum` pins REVIEW.md normalization and UTF-16 hashing in both runtimes, recorded by the Python recorder.

`review_md_set_checksum` pins ordered REVIEW.md list hashing, including null checksums and the empty list, in both runtimes, recorded by the Python recorder.

`decisions` freezes raw Merge findings and per-finding receipt deltas plus
`factual_verification` and `diff_validation`, recorded by
`workflows/test/tools/record_parity.py decisions`. Replay uses the real JS
projection and encoder, then `verify_findings.py` in a temporary two-commit repo.

`symbol_extractor` is a frozen, JS-owned port oracle recorded from Python 3.12.13
by `workflows/test/tools/record_parity.py symbol_extractor`. It includes the six
existing parametrized cases, Unicode and fence hazards, and 320 fuzz cases with
seed 529. Fuzz characters were restricted to Unicode 3.2 assignments, a subset
of Unicode 13. Inputs persist the generated text; outputs are sorted symbol lists.
Keep both families frozen while changing the wire and extractor.
