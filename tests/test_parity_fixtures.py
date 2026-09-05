import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "parity"
sys.path.insert(0, str(REPO / "scripts"))


def _load(case_dir):
    return (
        json.loads((case_dir / "input.json").read_text()),
        json.loads((case_dir / "expected.json").read_text()),
    )


class TestFindingDedupParity(unittest.TestCase):
    def test_all_cases(self):
        from finding_dedup import dedup_by_id

        for case_dir in sorted((FIXTURES / "finding_dedup").iterdir()):
            if not case_dir.is_dir():
                continue
            with self.subTest(case=case_dir.name):
                inp, expected = _load(case_dir)
                merged, dupes, dropped = dedup_by_id(
                    inp["ndjson_findings"], inp["text_findings"]
                )
                got = {
                    "merged": merged,
                    "duplicates_resolved": dupes,
                    "dropped_no_id": dropped,
                }
                self.assertEqual(got, expected)


class TestMergeFindingsParity(unittest.TestCase):
    def test_all_cases(self):
        import tempfile

        from merge_findings import merge

        for case_dir in sorted((FIXTURES / "merge_findings").iterdir()):
            if not case_dir.is_dir():
                continue
            with self.subTest(case=case_dir.name):
                inp, expected = _load(case_dir)
                a = inp["args"]
                with (
                    tempfile.TemporaryDirectory() as fd,
                    tempfile.TemporaryDirectory() as td,
                ):
                    for n, t in inp.get("findings_dir_files", {}).items():
                        (Path(fd) / n).write_text(t)
                    for n, t in inp.get("text_dir_files", {}).items():
                        (Path(td) / n).write_text(t)
                    got = merge(
                        findings_dir=fd,
                        session_sha=a["session_sha"],
                        agents=a["agents"],
                        text_dir=td,
                        base_branch=a["base_branch"],
                        head_sha=a["head_sha"],
                        pr_number=a["pr_number"],
                        owner=a["owner"],
                        repo=a["repo"],
                    )
                self.assertEqual(
                    got["methodology"]["duplicates_resolved"],
                    expected["methodology"]["duplicates_resolved"],
                )
                self.assertEqual(len(got["findings"]), len(expected["findings"]))
                self.assertEqual(
                    got["methodology"]["truncation_warnings"],
                    expected["methodology"]["truncation_warnings"],
                )


class TestFilterFindingsParity(unittest.TestCase):
    def test_all_cases(self):
        import tempfile

        import filter_findings as ff

        # rglob, not iterdir: filter_findings fixtures nest one level deeper
        # (filter_findings/<group>/<case>/) than finding_dedup/merge_findings'
        # flat (<script>/<case>/) layout.
        for input_path in sorted((FIXTURES / "filter_findings").rglob("input.json")):
            case_dir = input_path.parent
            case_label = str(case_dir.relative_to(FIXTURES / "filter_findings"))
            with self.subTest(case=case_label):
                inp, expected = _load(case_dir)
                fn = inp["fn"]
                if fn == "normalize_field_names":
                    findings = inp["findings"]
                    ff.normalize_field_names(findings)
                    self.assertEqual({"findings": findings}, expected)
                elif fn == "parse_review_md":
                    with tempfile.NamedTemporaryFile(
                        "w", suffix=".md", delete=False
                    ) as t:
                        t.write(inp["markdown"])
                        path = t.name
                    self.assertEqual({"config": ff.parse_review_md(path)}, expected)
                elif fn == "build_review_config":
                    self.assertEqual(
                        {"config": ff.build_review_config(inp["entries"])}, expected
                    )
                elif fn == "config_for_file":
                    self.assertEqual(
                        {"config": ff.config_for_file(inp["config"], inp["file"])},
                        expected,
                    )
                elif fn == "load_exclusions":
                    with tempfile.NamedTemporaryFile(
                        "w", suffix=".md", delete=False
                    ) as t:
                        t.write(inp["markdown"])
                        path = t.name
                    self.assertEqual({"patterns": ff.load_exclusions(path)}, expected)
                elif fn == "apply_threshold_filter":
                    passed, eliminated, contested = ff.apply_threshold_filter(
                        inp["findings"], inp["config"]
                    )
                    self.assertEqual(
                        {
                            "kept": passed,
                            "eliminated": eliminated,
                            "contested_count": contested,
                        },
                        expected,
                    )
                elif fn == "apply_reachability_demotion":
                    findings, demoted_count = ff.apply_reachability_demotion(
                        inp["findings"]
                    )
                    self.assertEqual(
                        {"findings": findings, "demoted_count": demoted_count},
                        expected,
                    )
                elif fn == "apply_injection_filter":
                    kept, eliminated = ff.apply_injection_filter(inp["findings"])
                    self.assertEqual({"kept": kept, "eliminated": eliminated}, expected)
                elif fn == "apply_replay_injection_scan":
                    kept, eliminated = ff.apply_replay_injection_scan(inp["findings"])
                    self.assertEqual({"kept": kept, "eliminated": eliminated}, expected)
                elif fn == "apply_exclusions":
                    kept, eliminated = ff.apply_exclusions(
                        inp["findings"], inp["exclusion_patterns"]
                    )
                    self.assertEqual({"kept": kept, "eliminated": eliminated}, expected)
                elif fn == "apply_filter_pipeline":
                    got = ff.apply_filter_pipeline(
                        inp["findings"],
                        inp["config"],
                        inp["exclusion_patterns"],
                        inp["generated_at"],
                    )
                    self.assertEqual(got, expected)
                elif fn == "detect_disagreement":
                    active, suppressed, boosted_count = ff.detect_disagreement(
                        inp["findings"]
                    )
                    self.assertEqual(
                        {
                            "active": active,
                            "suppressed": suppressed,
                            "boosted_count": boosted_count,
                        },
                        expected,
                    )
                elif fn == "_route_by_dimension":
                    self.assertEqual(
                        {"route": ff._route_by_dimension(inp["finding"])}, expected
                    )
                elif fn == "consolidate_cross_agent":
                    findings, consolidated_count = ff.consolidate_cross_agent(
                        inp["findings"]
                    )
                    self.assertEqual(
                        {
                            "findings": findings,
                            "consolidated_count": consolidated_count,
                        },
                        expected,
                    )
                elif fn == "tag_findings":
                    tagged, consolidated_count, main_count, suggestion_count = (
                        ff.tag_findings(inp["findings"])
                    )
                    self.assertEqual(
                        {
                            "tagged": tagged,
                            "consolidated_count": consolidated_count,
                            "main_count": main_count,
                            "suggestion_count": suggestion_count,
                        },
                        expected,
                    )
                else:
                    self.fail(f"unhandled fn: {fn!r}")


class TestApplyValidationsParity(unittest.TestCase):
    def test_all_cases(self):
        import copy

        from apply_validations import apply_validations

        for case_dir in sorted((FIXTURES / "apply_validations").iterdir()):
            if not case_dir.is_dir():
                continue
            with self.subTest(case=case_dir.name):
                inp, expected = _load(case_dir)
                findings = copy.deepcopy(inp["findings"])
                adjusted_count, unmatched_ids = apply_validations(
                    findings, inp["validations"]
                )
                got = {
                    "findings": findings,
                    "adjusted_count": adjusted_count,
                    "unmatched_ids": unmatched_ids,
                }
                self.assertEqual(got, expected)


class TestApplyChallengesParity(unittest.TestCase):
    def test_all_cases(self):
        import copy

        from apply_challenges import apply_challenges, rank_findings
        from filter_findings import consolidate_cross_agent

        for case_dir in sorted((FIXTURES / "apply_challenges").iterdir()):
            if not case_dir.is_dir():
                continue
            with self.subTest(case=case_dir.name):
                inp, expected = _load(case_dir)
                findings = copy.deepcopy(inp["findings"])
                # deep_copy_no_mutation_of_input additionally asserts the
                # caller's input findings list is untouched by apply_challenges
                # (mirrors the JS twin's structuredClone-before-mutation check).
                snapshot = (
                    copy.deepcopy(findings)
                    if case_dir.name == "deep_copy_no_mutation_of_input"
                    else None
                )

                total_input = len(findings)
                active, challenge_eliminated, challenge_stats = apply_challenges(
                    findings, inp["challenges"]
                )

                if snapshot is not None:
                    self.assertEqual(findings, snapshot)

                active, cross_agent_consolidated = consolidate_cross_agent(active)
                active = rank_findings(active)
                stats = {
                    "total_input": total_input,
                    "challenge_removed": challenge_stats["challenge_removed"],
                    "challenge_downgraded": challenge_stats["challenge_downgraded"],
                    "challenge_contested": challenge_stats["challenge_contested"],
                    "challenge_survived": challenge_stats["challenge_survived"],
                    "unchallenged": challenge_stats["unchallenged"],
                    "cross_agent_consolidated": cross_agent_consolidated,
                    "final_count": len(active),
                }
                got = {
                    "findings": active,
                    "eliminated": challenge_eliminated,
                    "stats": stats,
                }
                self.assertEqual(got, expected)


# The script's own audit trail (blame_metadata / factual_verification / diff_validation --
# see verify_findings.py's "DELIBERATELY EXCLUDED" comment above its _DELTA_FIELDS
# constant). No workflow schema declares any of these, and joinVerifyDeltas (stages.js)
# only ever writes DELTA_VALUE_KEYS onto the finding it already holds -- it never carries
# any of these across the join either -- so a golden "joined" finding must not either.
# `agent` is DELIBERATELY NOT in this drop list as of #22: the join now keeps it
# deterministically on both the trusted and degraded paths.
# Duplicated from record_parity.py's own _VERIFY_DELTA_DROP/_project_verify_delta rather
# than imported, matching this file's existing convention of re-deriving each recorder's
# computation independently (e.g. TestApplyChallengesParity re-composes apply_challenges'
# bridge rather than calling record_parity.py's _apply_challenges) -- a shared import
# would let a recorder bug and its "parity" test agree with each other by construction.
_VERIFY_DELTA_DROP = (
    "blame_metadata",
    "factual_verification",
    "diff_validation",
)


def _project_verify_delta(finding):
    return {k: v for k, v in finding.items() if k not in _VERIFY_DELTA_DROP}


class TestVerifyDeltasParity(unittest.TestCase):
    def test_all_cases(self):
        from verify_findings import build_deltas, deltas_checksum

        for case_dir in sorted((FIXTURES / "verify_deltas").iterdir()):
            if not case_dir.is_dir():
                continue
            with self.subTest(case=case_dir.name):
                inp, expected = _load(case_dir)
                verified_by_id = {f["id"]: f for f in inp["result"]["verified"]}
                post_by_id = dict(verified_by_id)
                post_by_id.update({f["id"]: f for f in inp["result"]["eliminated"]})
                # Reorder to dispatch order using the SAME dict objects as
                # result["verified"]/result["eliminated"] (looked up from post_by_id) --
                # build_deltas decides membership by object identity, mirroring how
                # verify_findings.py's own findings/verified pair are literally the same
                # mutated-in-place objects.
                ordered = [post_by_id[f["id"]] for f in inp["dispatched"]]
                deltas = build_deltas(ordered, inp["result"]["verified"])
                checksum = deltas_checksum(deltas)
                joined = [
                    _project_verify_delta(post_by_id[f["id"]])
                    for f in inp["dispatched"]
                    if f["id"] in verified_by_id
                ]
                got = {"deltas": deltas, "checksum": checksum, "joined": joined}
                self.assertEqual(got, expected)


class TestSliceInputProofParity(unittest.TestCase):
    def test_all_cases(self):
        # Re-derived from the shared pair rather than imported from record_parity.py,
        # matching this file's convention: a shared import would let a recorder bug and
        # its "parity" test agree with each other by construction. The second assertion
        # pins verify_findings' own wrapper to that pair, which is the value the receipt
        # actually carries.
        from assemble_artifacts import fnv1a32, js_stringify_pretty
        from verify_findings import _input_checksum

        for case_dir in sorted((FIXTURES / "slice_input_proof").iterdir()):
            if not case_dir.is_dir():
                continue
            with self.subTest(case=case_dir.name):
                inp, expected = _load(case_dir)
                self.assertEqual(
                    fnv1a32(js_stringify_pretty(inp["doc"])), expected["checksum"]
                )
                self.assertEqual(_input_checksum(inp["doc"]), expected["checksum"])


class TestGoldenFreshness(unittest.TestCase):
    def test_recorder_output_matches_committed(self):
        # --check records into a TEMP tree and diffs against the committed
        # goldens -- it never writes into tests/fixtures/parity, so a run of
        # this test (mutated implementation or not) cannot corrupt the working
        # tree the way in-place recording used to (issue #211 review F7: the
        # old form also silently minted-but-never-compared the golden for any
        # BRAND NEW case, since its snapshot loop only knew about
        # expected.json paths that existed before the subprocess ran).
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "workflows/test/tools/record_parity.py"),
                "--check",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stale/missing golden(s) -- rerun record_parity.py:\n{result.stderr}",
        )


class TestRecordParityCheckDirect(unittest.TestCase):
    """Direct, isolated test of record_parity.py's check() (#211 round-1
    adjudication item 4 / r2-F4): TestGoldenFreshness above only ever
    exercises check() through a subprocess against the REAL (correct) twins,
    so a mutation of check() itself that still compares fresh-vs-committed
    bytes correctly (e.g. writing the fresh recording in place instead of
    into a temp tree) is invisible there -- the bytes it writes and the bytes
    already committed are identical, so the corruption is a silent no-op.
    This test corrupts a COMMITTED golden first, so check()'s comparison has
    a real mismatch to report, and asserts both of its guarantees directly:
    it reports the mismatch, and it never touches the corrupted file."""

    def test_check_reports_stale_and_never_writes_into_the_fixture_tree(self):
        import importlib
        import shutil
        import tempfile

        sys.path.insert(0, str(REPO / "workflows" / "test" / "tools"))
        mod = importlib.import_module("record_parity")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_fixtures = Path(tmp) / "parity"
            shutil.copytree(mod.FIXTURES, tmp_fixtures)
            original_fixtures = mod.FIXTURES
            mod.FIXTURES = tmp_fixtures
            try:
                case_dir = (
                    tmp_fixtures
                    / "filter_findings"
                    / "injection"
                    / "word_count_nel_joined_high_confidence"
                )
                golden_path = case_dir / "expected.json"
                corrupted = golden_path.read_text() + "\n// corrupted by test\n"
                golden_path.write_text(corrupted)

                mismatches = mod.check()
            finally:
                mod.FIXTURES = original_fixtures

            self.assertTrue(
                any(
                    "STALE" in m and "word_count_nel_joined_high_confidence" in m
                    for m in mismatches
                ),
                mismatches,
            )
            self.assertEqual(
                golden_path.read_text(),
                corrupted,
                "check() must never write into the fixture tree it is checking",
            )


if __name__ == "__main__":
    unittest.main()
