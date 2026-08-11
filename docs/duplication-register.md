# Duplication register

**This is a living document.** It records every known duplication in this repository together with
the verdict on why it exists. The point-in-time findings that produced it live in
[engineering-audit-2026-07.md](engineering-audit-2026-07.md); that document is not maintained, this
one is.

**How to use it when you add a duplicate.** Before copying a block of code, a fixture, or a contract
paragraph, find the closest row below. If a row already covers your case, follow its verdict. If
nothing covers it, add a row: the pair, the classification, a one-sentence reason, and — for an
intentional one — the doc reference that justifies it. A duplication with no row is an unclassified
duplication, which is the state this register exists to prevent.

Classifications are `intentional-and-documented` (a rules file, in-code comment, or completed
consolidation already justifies it — cite it), `intentional-but-undocumented` (a real reason exists;
from now on this register is the documentation), and `accidental` (no reason; queue it for removal).

**Scan basis.** Measured 2026-07-30 at HEAD `ebf399d` (tag v3.3.4) with:

```text
npx --yes jscpd --min-tokens 60 --reporters json,console \
  --ignore "**/bench/vendor/**,**/pipeline.js,**/.venv/**,**/node_modules/**" \
  scripts workflows/src workflows/build.js workflows/test tests bench
```

It reported 116 clone pairs, 1,483 duplicated lines, 2.16% of lines and 2.49% of tokens over 232
first-party files. Thirty-two of the 116 pairs were opened at both locations and given their own
row; 84 were grouped into the seven patterns in the second table, each with one exemplar read at
both locations. Duplications that jscpd cannot detect were added as rows as well. Rows plus groups
reconcile to 116 pairs plus the ten tool-invisible relationships.

**Two limits on the 2.16% figure, to be quoted alongside it.**

- jscpd cannot see cross-language duplication, so the five JS/Python twin pairs and the
  `fnv1a32`/delta-key parity contribute nothing to it. Those relationships are held by the frozen
  parity fixtures under `tests/fixtures/parity/`, and are recorded below regardless.
- `agents/` was outside the scanned tree, and it holds the single largest deliberate duplication in
  the repository. It is recorded below so this table does not imply otherwise. Any recurring
  duplication metric must either include `agents/` or state in writing that it excludes it.

## Individually classified rows

Thirty-nine rows: 29 `intentional-and-documented`, 9 `intentional-but-undocumented`, 1
`accidental`. The `accidental` row postdates the scan basis above and is outside its 116-pair
reconciliation.

| Pair | Classification | Reason | Doc ref |
| --- | --- | --- | --- |
| `scripts/AGENTS.md:1-33` ↔ `scripts/CLAUDE.md:5-37` | intentional-and-documented | Verbatim output of `scripts/sync_agent_rules.py`; the 4-line generated header is the whole offset. Enforced by `--check`, a pre-commit hook, and `tests/test_agent_instruction_layout.py:156-163`. | `CLAUDE.md:7-9` |
| `AGENTS.md` ↔ `CLAUDE.md`, `workflows/` pair, `agents/` pair | intentional-and-documented | Same generator, below the 60-token threshold so jscpd missed them. Listed so all four twin pairs are covered, not just the one the tool surfaced. | `CLAUDE.md:7-9`; `.pre-commit-config.yaml:79` |
| `agents/*.md` — false-positive exclusion list + complete-read contract, ~10 files × 3 canonical-source blocks | intentional-and-documented | Duplicated so the guarantee survives a failed file read; `tests/test_agent_contracts.py:133-163` asserts the complete-read copies byte-identical. Outside the jscpd scan. | `agents/AGENTS.md:21-24` |
| `workflows/src/mergeFindings.js` (427 ln) ↔ `scripts/merge_findings.py` (632 ln) | intentional-and-documented | Cross-language twin. The sandbox has no disk or shell, so the transform must exist in JS; Python stays the authoritative CLI. Parity held by frozen fixtures. | `scripts/AGENTS.md:17-19` |
| `workflows/src/filterFindings.js` (971 ln) ↔ `scripts/filter_findings.py` (1402 ln) | intentional-and-documented | Largest twin pair, so the frozen-golden enforcement carries the most weight here. `scripts/apply_challenges.py:73-74` imports from the Python side, which is also a shared library. | `scripts/AGENTS.md:17-19` |
| `workflows/src/applyChallenges.js` (229 ln) ↔ `scripts/apply_challenges.py` (518 ln) | intentional-and-documented | Cross-language twin; largest fixture family, source of six of the top-20 pairs. The line gap is the CLI the JS twin does not port. | `scripts/AGENTS.md:17-19` |
| `workflows/src/applyValidations.js` (74 ln) ↔ `scripts/apply_validations.py` (329 ln) | intentional-and-documented | Cross-language twin; the 4x line gap is the CLI/main the JS side omits. | `scripts/AGENTS.md:17-19` |
| `workflows/src/findingDedup.js` (23 ln) ↔ `scripts/finding_dedup.py` (57 ln) | intentional-and-documented | Smallest of the five twins; fixtures at `tests/fixtures/parity/finding_dedup/`. | `scripts/AGENTS.md:17-19` |
| `stages.js:2343` fnv1a32 + `:493` DELTA_KEYS ↔ `assemble_artifacts.py:178` fnv1a32 + `verify_findings.py:1061` `_DELTA_FIELDS` | intentional-and-documented | Only the unavoidable JS↔Python crossing is duplicated: the Python side has exactly one `fnv1a32` and both other modules import it (`verify_findings.py:91` carries an in-code note against a third copy). | `workflows/AGENTS.md:43-48,59-60` |
| `stages.js` `VERIFY_SLICE_FIELDS` ↔ `verify_findings.py` `_SLICE_INPUT_FIELDS` | intentional-and-documented | Cross-language crossing: the verify slice input is projected to the fields the script consults, in the same list order in both runtimes. `tests/test_verify_findings.py` pins the pair in lockstep. | `workflows/AGENTS.md` (The verify boundary) |
| `workflows/test/args.test.js:8-16` ↔ `workflows/test/entry_guard.test.js:23-31` | intentional-and-documented | The copy carries its own written justification at the copy site: a shared fixture module lets one bad edit silently rebaseline both suites. | `workflows/test/entry_guard.test.js:19-21` |
| `merge_findings` fixtures: `dropped_no_id_both_channels` ↔ `unterminated_brace` | intentional-and-documented | Byte-diffed: differ only in what each case name exercises. Machine-generated goldens; consolidating breaks the byte-compare freshness assertion. | `tests/fixtures/parity/README.md` |
| `merge_findings` fixtures: `dropped_no_id_both_channels` ↔ `truncation_m5_no_false_positive` | intentional-and-documented | Byte-diffed: identical three-field delta to the row above. Same generated-golden mechanism. | `tests/fixtures/parity/README.md` |
| `merge_findings` fixtures: `invalid_ndjson_line_warns_not_fatal` ↔ `ndjson_only_single_agent` | intentional-and-documented | Byte-diffed: differs only in `line_start` and the one validation warning the invalid-NDJSON case produces. | `tests/fixtures/parity/README.md` |
| `apply_challenges` fixtures: `score_25_boundary_downgrades` ↔ `score_49_boundary_downgrades` | intentional-and-documented | One-line difference, `challenge_score` 25 vs 49. Both ends of one equivalence class must be pinned; identical-except-the-boundary is the evidence. | `tests/fixtures/parity/README.md` |
| `apply_challenges` fixtures: `score_50_boundary_contests` ↔ `score_74_boundary_contests` | intentional-and-documented | One line, `challenge_score` 50 vs 74. Second boundary band, same rationale. | `tests/fixtures/parity/README.md` |
| `apply_challenges` fixtures: `score_24_boundary_removed` ↔ `score_below_25_non_security_removed` | intentional-and-documented | Score 24 vs 15 plus the `elimination_reason` that quotes it. Below-threshold pair, same rationale. | `tests/fixtures/parity/README.md` |
| `apply_challenges` fixtures: `deep_copy_no_mutation_of_input` ↔ `score_25_boundary_downgrades` | intentional-and-documented | Genuinely different cases sharing the generated finding-record skeleton. Structural overlap of an envelope, nothing to merge. | `tests/fixtures/parity/README.md` |
| `apply_challenges/issue47_extra_fields_pass_through/expected.json:22-47` ↔ `filter_findings/tag_findings/.../expected.json:25-50` | intentional-and-documented | Envelopes differ completely; the overlap is the issue #47 record surviving the stage, which is exactly what #47 asserts. | `tests/fixtures/parity/README.md` |
| `apply_challenges/issue47_extra_fields_pass_through/expected.json:2-23` ↔ `merge_findings/.../expected.json:2-24` | intentional-and-documented | `merge_findings`' envelope carries different keys; the shared span is the same pass-through record. Generated golden. | `tests/fixtures/parity/README.md` |
| `apply_challenges/issue47_.../input.json` ↔ `filter_findings/tag_findings/issue47_.../input.json` | intentional-but-undocumented | Largest pair in the scan (40 lines). `input.json` files are hand-authored and self-contained per case directory by the README's contract, and #47 asserts the *same* record passes unchanged through every stage — a shared fixture would weaken that. | — |
| `apply_challenges/issue47_.../input.json` ↔ `apply_validations/issue47_.../input.json` | intentional-but-undocumented | Already divergent where the stage differs (`challenges` replaced by `validations`); same self-contained-case-dir rationale. | — |
| `apply_challenges/issue47_.../input.json` ↔ `finding_dedup/issue47_.../input.json` | intentional-but-undocumented | `finding_dedup`'s copy restructures the top level entirely while reusing the two records. Same rationale. | — |
| `bench/golden/benchmark_data.min.json:682-707` ↔ `bench/golden/golden_comments/discourse.json:55-80` | intentional-but-undocumented | The per-repo golden files are upstream provenance for the aggregate; only the aggregate is read by first-party code (`bench/run.py:871,906`, `bench/runner/score.py:60`). | — |
| `bench/golden/anchors/candidates.json:2366-2388` ↔ `:2642-2664` | intentional-but-undocumented | Text content is entirely different; jscpd is matching the JSON record shape of a data file, not duplicated content. Nothing actionable. | — |
| `bench/tests/test_check.py:341-365` ↔ `:451-475` | intentional-but-undocumented | Two upstream triggers for one G3 failure; the second copy's extra `script` key is the variable under test and is legible only because the surrounding literal is present. | — |
| `tests/test_parity_fixtures.py:151-165` ↔ `workflows/test/tools/record_parity.py:158-171` | intentional-but-undocumented | Load-bearing and must not be consolidated: `record_parity.py` generates `expected.json`, so importing it into the test would compare the recorder to its own output. | — |
| `scripts/post_review.py` — the GitLab position assembly ↔ `validate_position`'s expected position | intentional-and-documented | Load-bearing mirror, same shape as the row above: the gate must recompute every field independently, because one that derives its answer through the assembly moves with the assembly's bug and passes it. Below the 60-token threshold, so jscpd cannot see it. | `scripts/post_review.py:514-516` |
| `tests/test_boundary_parity.py:104-114,197-207` ↔ `tests/test_post_review.py:1018-1028,1038-1048` | intentional-but-undocumented | Trimmed local `_fake_run` plus setUp/tearDown. The alternative is test modules importing each other, which lets an unrelated suite's refactor break this one. | — |
| `workflows/src/mergeFindings.js:97-127` ↔ `:141-171` (`tryParseJsonAt` vs `findEndOfJson`) | intentional-and-documented | Consolidated behind `scanJsonObject` in #110; the thin wrappers retain their distinct parse and end-index contracts. | [#110](https://github.com/liatrio-labs/claude-code-gauntlet/issues/110) |
| `scripts/merge_findings.py:154-180` ↔ `:191-217` | intentional-and-documented | Consolidated behind `_scan_json_object` in #110; the thin wrappers retain their distinct parse and end-index contracts. | [#110](https://github.com/liatrio-labs/claude-code-gauntlet/issues/110) |
| `scripts/apply_challenges.py:485-496` ↔ `scripts/filter_findings.py:1379-1390` ↔ `scripts/apply_validations.py:302-311` | intentional-and-documented | Consolidated behind `script_io.write_result` in #110. | [#110](https://github.com/liatrio-labs/claude-code-gauntlet/issues/110) |
| `bench/runner/invoke.py:325-333` ↔ `:866-874` | intentional-and-documented | Consolidated behind `_iter_new_wf_paths` in #110. | [#110](https://github.com/liatrio-labs/claude-code-gauntlet/issues/110) |
| `bench/profile_run.py:213-223` ↔ `:494-504` | intentional-and-documented | Consolidated behind `_iter_content_blocks` in #110. | [#110](https://github.com/liatrio-labs/claude-code-gauntlet/issues/110) |
| `bench/report.py:1501-1521` ↔ `:1523-1543` | intentional-and-documented | Consolidated behind `_DARK_VIZ_VARS` in #110; both dark-mode entry points render from the shared map. | [#110](https://github.com/liatrio-labs/claude-code-gauntlet/issues/110) |
| `bench/report.html:32-52` ↔ `:54-74` | intentional-and-documented | Regenerated from `bench/report.py` in #110; its two dark-mode blocks now derive from `_DARK_VIZ_VARS`. | [#110](https://github.com/liatrio-labs/claude-code-gauntlet/issues/110) |
| `workflows/src/stages.js:608-614` ↔ `:1275-1281` ↔ `:1439-1445` | intentional-but-undocumented | Declined in #110: only 2 of the ~6 lines are identical across the 8 sites; the remainder is per-stage extraction and defaults, and each parse is independently tested. Revisit only if a ninth stage or a change to the shared 2-line parse itself lands. | [#110](https://github.com/liatrio-labs/claude-code-gauntlet/issues/110) |
| `tests/test_assemble_artifacts.py:503-513` ↔ `:1158-1168` | intentional-and-documented | Consolidated behind `assert_assemble_hard_failure` in #110. | [#110](https://github.com/liatrio-labs/claude-code-gauntlet/issues/110) |
| `scripts/assemble_artifacts.py:658-676` (`assemble`) ↔ `scripts/materialize_artifacts.py:409-429` (`materialize`) | intentional-and-documented | The public wrapper whose only body is the last-resort guard over a private worker. Below the 60-token threshold, so jscpd missed it. Not shared: each side builds its own `_receipt` shape and names its own script in the error, and the guard belongs to whichever function promises the receipt. | `scripts/AGENTS.md:23-25` |
| `scripts/post_review.py:234-421` ↔ `scripts/verify_findings.py:257-315` (the `parse_diff_lines` pair) | accidental | Two walks of the same unified-diff grammar, which carried the same two defects and were fixed twice. Half-consolidated behind `scripts/diff_lines.py`: the verify side now delegates the walk and keeps only its own header semantics, while `post_review.py` still holds its copy. Interim by sequencing — the poster's GitLab position fields (`old_line`, `new_files`, `old_paths`) migrate under their own tests rather than alongside this change, and until they do the poster's copy keeps reading git's encoded header paths (a space's trailing TAB, a C-quoted non-ASCII name) as the path itself. | [#163](https://github.com/liatrio-labs/claude-code-gauntlet/issues/163) |

## Grouped patterns

Eighty-four of the 116 pairs, grouped after reading one exemplar per group at both locations.

| Pattern | Count | Classification | Reason |
| --- | --- | --- | --- |
| Parity fixture `expected.json` ↔ `expected.json` (generated goldens) | 9 | intentional-and-documented | Same family as the individual fixture rows; every file is machine-generated by `record_parity.py` and byte-compared for freshness. Doc ref `tests/fixtures/parity/README.md`. |
| Parity fixture `input.json` ↔ `input.json` (hand-authored, self-contained per case dir) | 11 | intentional-but-undocumented | Mostly one boundary hub file matching its siblings, where identical-except-the-boundary is the evidence. One is a self-clone inside a single `verify_deltas` input. |
| `workflows/test/*.test.js` intra- and cross-file test-body repetition | 28 | intentional-but-undocumented | The same contract asserted once per degrade trigger (e.g. the four-line UNVERIFIED contract against wrong nonce, failed status, agent throw, bad slice). Factoring it out hides which trigger failed. |
| `tests/*.py` intra- and cross-file test-body repetition | 23 | intentional-but-undocumented | The paired-boundary and paired-trigger idiom: identical setup, one value changed, different assertion. Test-clarity norms apply. |
| `bench/tests/*.py` intra-file test-body repetition | 7 | intentional-but-undocumented | Same idiom as the `tests/` group; several spans are only 6-7 lines. |
| `bench/tests/*.py` module bootstrap (docstring, stdlib imports, `REPO_ROOT` sys.path insert) | 2 | intentional-but-undocumented | Standard per-module bootstrap that keeps each file independently runnable. A `conftest.py` would trade three visible lines for an invisible one. |
| `bench/MEASUREMENT.md` self-matches | 4 | not-duplication (tool artifact) | jscpd's markdown tokenizer is matching document structure — a fenced block bracketed by prose — not text. All four are overlapping ranges of one file whose content shares nothing. |

## Standing dispositions

- Rows classified `intentional-but-undocumented` are documented **by this register** from now on. A
  future reader who finds one of them needs no further justification; a future *editor* who wants to
  consolidate one should update the row rather than delete it silently.
- The accidental rows queued in #110 have been resolved: eight were consolidated behind shared
  symbols and the `stages.js` preamble was explicitly declined with a reopen condition. New
  accidental rows need a new tracked removal issue rather than being added to the closed #110 queue.
- `tests/test_parity_fixtures.py` ↔ `record_parity.py` is the one duplication that must **not** be
  consolidated. It reads like the worst violation in the register and is the highest-risk row for a
  future cleanup pass.
