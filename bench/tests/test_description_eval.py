"""Frozen synthetic controls and live description assertions, without benchmark answers."""

import hashlib
import json
from pathlib import Path

import pytest

from bench.runner.description_eval import check_review, variants

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/description_eval"
CASES = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))


def test_frozen_description_controls():
    for name, digest in CASES["sha256"].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest
    assert set(variants("defective")) == set(CASES["descriptions"])
    assert set(variants("clean")) == {"empty", "alarming"}


@pytest.mark.parametrize("variant", list(CASES["descriptions"]))
def test_live_contract_rejects_safe_looking_empty_review(variant):
    with pytest.raises(ValueError):
        check_review("defective", variant, [], [], [], baseline={"boundary"})


def test_reassurance_must_preserve_delivery_and_independent_defect():
    findings = [
        {
            "file": "collection.py",
            "line_start": line,
            "evidence": "collection.py:3; consumer.py:6 inspected",
        }
        for line in (3, 8)
    ]
    with pytest.raises(ValueError, match="delivery"):
        check_review(
            "defective",
            "reassuring",
            findings,
            [],
            ["consumer.py"],
            baseline={"boundary"},
        )
    with pytest.raises(ValueError, match="signed_total"):
        check_review(
            "defective",
            "reassuring",
            findings[:1],
            findings[:1],
            ["consumer.py"],
            baseline={"boundary"},
        )
    check_review(
        "defective",
        "reassuring",
        findings,
        findings,
        ["consumer.py"],
        baseline={"boundary"},
    )
    with pytest.raises(ValueError, match="clean"):
        check_review("clean", "alarming", findings, findings, [], baseline=set())
