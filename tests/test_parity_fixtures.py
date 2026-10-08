import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "parity"


def _load(case_dir):
    return (
        json.loads((case_dir / "input.json").read_text(encoding="utf-8")),
        json.loads((case_dir / "expected.json").read_text(encoding="utf-8")),
    )


def _case_dirs(family):
    return sorted(path for path in (FIXTURES / family).iterdir() if path.is_dir())


@pytest.mark.parametrize(
    "case_dir", _case_dirs("verify_deltas"), ids=lambda path: path.name
)
def test_verify_deltas_parity(case_dir):
    from gauntlet.verify.wire import build_deltas, checksum_or_none

    verify_delta_drop = {
        "blame_metadata",
        "factual_verification",
        "diff_validation",
    }
    inp, expected = _load(case_dir)
    verified_by_id = {f["id"]: f for f in inp["result"]["verified"]}
    post_by_id = dict(verified_by_id)
    post_by_id.update({f["id"]: f for f in inp["result"]["eliminated"]})
    ordered = [post_by_id[f["id"]] for f in inp["dispatched"]]
    deltas = build_deltas(ordered, inp["result"]["verified"])
    checksum = checksum_or_none(deltas)
    joined = [
        {k: v for k, v in post_by_id[f["id"]].items() if k not in verify_delta_drop}
        for f in inp["dispatched"]
        if f["id"] in verified_by_id
    ]
    assert {"deltas": deltas, "checksum": checksum, "joined": joined} == expected


@pytest.mark.parametrize(
    "case_dir", _case_dirs("slice_input_proof"), ids=lambda path: path.name
)
def test_slice_input_proof_parity(case_dir):
    from gauntlet.jsjson import fnv1a32, js_stringify_pretty
    from gauntlet.verify.wire import checksum_or_none

    inp, expected = _load(case_dir)
    assert fnv1a32(js_stringify_pretty(inp["doc"])) == expected["checksum"]
    assert checksum_or_none(inp["doc"]) == expected["checksum"]


@pytest.mark.parametrize("mode", [[], ["--check"]], ids=["record", "check"])
def test_recorder_rejects_unknown_family(mode):
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "workflows/test/tools/record_parity.py"),
            *mode,
            "filter_findings",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.splitlines() == ["unknown recorder family: filter_findings"]


def test_check_reports_stale_and_never_writes_into_fixture_tree(tmp_path, monkeypatch):
    import importlib
    import shutil

    sys.path.insert(0, str(REPO / "workflows" / "test" / "tools"))
    mod = importlib.import_module("record_parity")
    tmp_fixtures = tmp_path / "parity"
    shutil.copytree(mod.FIXTURES / "verify_deltas", tmp_fixtures / "verify_deltas")
    monkeypatch.setattr(mod, "FIXTURES", tmp_fixtures)
    case_dir = tmp_fixtures / "verify_deltas" / "plain_verified"
    golden_path = case_dir / "expected.json"
    corrupted = golden_path.read_text(encoding="utf-8") + "\n// corrupted by test\n"
    golden_path.write_text(corrupted, encoding="utf-8")
    mismatches = mod.check("verify_deltas")
    assert any(
        "STALE" in mismatch and "plain_verified" in mismatch for mismatch in mismatches
    ), mismatches
    assert golden_path.read_text(encoding="utf-8") == corrupted
