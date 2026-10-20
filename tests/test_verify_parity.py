"""Verify decisions through the JS projection must match the frozen Python oracle."""

import json
from pathlib import Path

import pytest

from tests.support.verify_parity import replay_decisions

FIXTURES = Path(__file__).parent / "fixtures/parity"


@pytest.mark.parametrize(
    "case_dir",
    sorted((FIXTURES / "decisions").iterdir()),
    ids=lambda path: path.name,
)
def test_decisions_parity(case_dir, tmp_path):
    inp = json.loads((case_dir / "input.json").read_text(encoding="utf-8"))
    expected = (case_dir / "expected.json").read_bytes()
    actual = json.dumps(replay_decisions(inp, tmp_path), indent=2, sort_keys=True)
    assert (actual + "\n").encode("utf-8") == expected
