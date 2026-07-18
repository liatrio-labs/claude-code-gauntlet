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


class TestGoldenFreshness(unittest.TestCase):
    def test_recorder_output_matches_committed(self):
        before = {p: p.read_bytes() for p in FIXTURES.rglob("expected.json")}
        subprocess.run([sys.executable, str(REPO / "workflows/test/tools/record_parity.py")], check=True, cwd=REPO)
        for p, b in before.items():
            self.assertEqual(p.read_bytes(), b, f"stale golden: {p} — rerun record_parity.py")


if __name__ == "__main__":
    unittest.main()
