"""Python consumer of literal cross-runtime contract vectors."""

import json
from functools import cache
from pathlib import Path

import pytest
from gauntlet.artifacts import fnv1a32, js_stringify_pretty, normalize_content
from gauntlet.config import matches_rule
from gauntlet.delivery.post import _fence_closer, _fold_review_body, prepare_line

FIXTURES = Path(__file__).with_name("fixtures") / "cross_runtime"
FAMILIES = ("fnv1a32", "json_spelling", "outbound_line", "outbound_fold", "config_rule")


@cache
def _cases(family):
    payload = json.loads((FIXTURES / f"{family}.json").read_text(encoding="utf-8"))
    assert payload["algorithm"] == family
    return payload["cases"]


@pytest.mark.parametrize(
    ("family", "case"),
    [(family, case) for family in FAMILIES for case in _cases(family)],
    ids=[f"{family}:{case['id']}" for family in FAMILIES for case in _cases(family)],
)
def test_cross_runtime_python_vector(family, case):
    if family == "fnv1a32":
        actual = fnv1a32(case["input"])
    elif family == "json_spelling":
        actual = (
            normalize_content(case["input"])
            if case.get("operation") == "normalize"
            else js_stringify_pretty(case["input"])
        )
    elif family == "outbound_line":
        actual = prepare_line(case["input"])
    elif family == "config_rule":
        args = case["input"]
        actual = matches_rule(args["rule"], args["value"], args["mode"])
    else:
        if case["operation"] == "closer":
            actual = _fence_closer(case["input"])
        else:
            source = case["input"]
            text = source["prefix"] + source["repeat"] * source["count"]
            actual, dropped = _fold_review_body(
                text, case["py_allowance"], case["platform"]
            )
            assert dropped == case["expected_dropped_bytes"]
            assert len(actual.encode("utf-8")) <= case["py_allowance"]
        assert actual == case["expected_py"]
        return
    assert actual == case["expected"]
