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
        json.loads((case_dir / "input.json").read_text(encoding="utf-8")),
        json.loads((case_dir / "expected.json").read_text(encoding="utf-8")),
    )


class TestVerifyDeltasParity(unittest.TestCase):
    def test_all_cases(self):
        from verify_findings import build_deltas, deltas_checksum

        verify_delta_drop = {
            "blame_metadata",
            "factual_verification",
            "diff_validation",
        }
        for case_dir in sorted((FIXTURES / "verify_deltas").iterdir()):
            if not case_dir.is_dir():
                continue
            with self.subTest(case=case_dir.name):
                inp, expected = _load(case_dir)
                verified_by_id = {f["id"]: f for f in inp["result"]["verified"]}
                post_by_id = dict(verified_by_id)
                post_by_id.update({f["id"]: f for f in inp["result"]["eliminated"]})
                ordered = [post_by_id[f["id"]] for f in inp["dispatched"]]
                deltas = build_deltas(ordered, inp["result"]["verified"])
                checksum = deltas_checksum(deltas)
                joined = [
                    {
                        k: v
                        for k, v in post_by_id[f["id"]].items()
                        if k not in verify_delta_drop
                    }
                    for f in inp["dispatched"]
                    if f["id"] in verified_by_id
                ]
                self.assertEqual(
                    {"deltas": deltas, "checksum": checksum, "joined": joined},
                    expected,
                )


class TestSliceInputProofParity(unittest.TestCase):
    def test_all_cases(self):
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
    def test_recorder_output_matches_verify_wire_committed_goldens(self):
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "workflows/test/tools/record_parity.py"),
                "--check",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stale/missing verify-wire golden(s) -- rerun record_parity.py:\n{result.stderr}",
        )

    def test_check_reports_stale_and_never_writes_into_fixture_tree(self):
        import importlib
        import shutil
        import tempfile

        sys.path.insert(0, str(REPO / "workflows" / "test" / "tools"))
        mod = importlib.import_module("record_parity")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_fixtures = Path(tmp) / "parity"
            shutil.copytree(
                mod.FIXTURES / "verify_deltas", tmp_fixtures / "verify_deltas"
            )
            original_fixtures = mod.FIXTURES
            mod.FIXTURES = tmp_fixtures
            try:
                case_dir = tmp_fixtures / "verify_deltas" / "plain_verified"
                golden_path = case_dir / "expected.json"
                corrupted = (
                    golden_path.read_text(encoding="utf-8") + "\n// corrupted by test\n"
                )
                golden_path.write_text(corrupted, encoding="utf-8")
                mismatches = mod.check("verify_deltas")
            finally:
                mod.FIXTURES = original_fixtures

            self.assertTrue(
                any(
                    "STALE" in mismatch and "plain_verified" in mismatch
                    for mismatch in mismatches
                ),
                mismatches,
            )
            self.assertEqual(
                golden_path.read_text(encoding="utf-8"),
                corrupted,
                "check() must never write into the fixture tree it is checking",
            )


if __name__ == "__main__":
    unittest.main()
