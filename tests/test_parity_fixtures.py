import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "parity"
sys.path.insert(0, str(REPO / "scripts"))


def _load(case_dir):
    return (json.loads((case_dir / "input.json").read_text()),
            json.loads((case_dir / "expected.json").read_text()))


class TestFindingDedupParity(unittest.TestCase):
    def test_all_cases(self):
        from finding_dedup import dedup_by_id
        for case_dir in sorted((FIXTURES / "finding_dedup").iterdir()):
            if not case_dir.is_dir():
                continue
            with self.subTest(case=case_dir.name):
                inp, expected = _load(case_dir)
                merged, dupes, dropped = dedup_by_id(inp["ndjson_findings"], inp["text_findings"])
                got = {"merged": merged, "duplicates_resolved": dupes, "dropped_no_id": dropped}
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
                with tempfile.TemporaryDirectory() as fd, tempfile.TemporaryDirectory() as td:
                    for n, t in inp.get("findings_dir_files", {}).items():
                        (Path(fd) / n).write_text(t)
                    for n, t in inp.get("text_dir_files", {}).items():
                        (Path(td) / n).write_text(t)
                    got = merge(findings_dir=fd, session_sha=a["session_sha"], agents=a["agents"],
                                text_dir=td, base_branch=a["base_branch"], head_sha=a["head_sha"],
                                pr_number=a["pr_number"], owner=a["owner"], repo=a["repo"])
                self.assertEqual(got["methodology"]["duplicates_resolved"], expected["methodology"]["duplicates_resolved"])
                self.assertEqual(len(got["findings"]), len(expected["findings"]))
                self.assertEqual(got["methodology"]["truncation_warnings"], expected["methodology"]["truncation_warnings"])


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
                    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as t:
                        t.write(inp["markdown"])
                        path = t.name
                    self.assertEqual({"config": ff.parse_review_md(path)}, expected)
                elif fn == "load_exclusions":
                    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as t:
                        t.write(inp["markdown"])
                        path = t.name
                    self.assertEqual({"patterns": ff.load_exclusions(path)}, expected)
                elif fn == "apply_threshold_filter":
                    passed, eliminated, contested = ff.apply_threshold_filter(inp["findings"], inp["config"])
                    self.assertEqual(
                        {"kept": passed, "eliminated": eliminated, "contested_count": contested}, expected
                    )
                elif fn == "apply_injection_filter":
                    kept, eliminated = ff.apply_injection_filter(inp["findings"])
                    self.assertEqual({"kept": kept, "eliminated": eliminated}, expected)
                elif fn == "apply_exclusions":
                    kept, eliminated = ff.apply_exclusions(inp["findings"], inp["exclusion_patterns"])
                    self.assertEqual({"kept": kept, "eliminated": eliminated}, expected)
                elif fn == "detect_disagreement":
                    active, suppressed, boosted_count = ff.detect_disagreement(inp["findings"])
                    self.assertEqual(
                        {"active": active, "suppressed": suppressed, "boosted_count": boosted_count}, expected
                    )
                elif fn == "_route_by_dimension":
                    self.assertEqual({"route": ff._route_by_dimension(inp["finding"])}, expected)
                elif fn == "dedup_cross_agent":
                    kept, dropped = ff.dedup_cross_agent(inp["findings"])
                    self.assertEqual({"kept": kept, "dropped": dropped}, expected)
                elif fn == "tag_findings":
                    tagged, dedup_dropped, main_count, suggestion_count = ff.tag_findings(inp["findings"])
                    self.assertEqual(
                        {
                            "tagged": tagged,
                            "dedup_dropped": dedup_dropped,
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
                adjusted_count, unmatched_ids = apply_validations(findings, inp["validations"])
                got = {"findings": findings, "adjusted_count": adjusted_count, "unmatched_ids": unmatched_ids}
                self.assertEqual(got, expected)


class TestGoldenFreshness(unittest.TestCase):
    def test_recorder_output_matches_committed(self):
        before = {p: p.read_bytes() for p in FIXTURES.rglob("expected.json")}
        subprocess.run([sys.executable, str(REPO / "workflows/test/tools/record_parity.py")], check=True, cwd=REPO)
        for p, b in before.items():
            self.assertEqual(p.read_bytes(), b, f"stale golden: {p} — rerun record_parity.py")


if __name__ == "__main__":
    unittest.main()
