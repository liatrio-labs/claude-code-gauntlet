"""Frozen decision and symbol oracles recorded before changing the verify wire."""

import json
from pathlib import Path

import pytest
from gauntlet.verify.decide import _extract_symbols

from tests.support.verify_parity import replay_decisions

FIXTURES = Path(__file__).parent / "fixtures/parity"


@pytest.mark.parametrize(
    "case_dir",
    sorted((FIXTURES / "decisions").iterdir()),
    ids=lambda path: path.name,
)
def test_decisions_parity(case_dir, tmp_path):
    inp = json.loads((case_dir / "input.json").read_text(encoding="utf-8"))
    expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
    assert replay_decisions(inp, tmp_path) == expected


@pytest.mark.parametrize(
    "case_dir",
    sorted((FIXTURES / "symbol_extractor").iterdir()),
    ids=lambda path: path.name,
)
def test_symbol_extractor_parity(case_dir):
    inp = json.loads((case_dir / "input.json").read_text(encoding="utf-8"))
    expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
    assert len(inp["cases"]) == len(expected)
    for case, symbols in zip(inp["cases"], expected, strict=True):
        assert (
            sorted(_extract_symbols(case["description"], case["evidence"])) == symbols
        ), case
