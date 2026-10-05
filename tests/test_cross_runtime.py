"""Python consumer of literal cross-runtime contract vectors."""

import json
from functools import cache
from pathlib import Path
from typing import Literal, TypedDict, cast

import pytest
from gauntlet.config import matches_rule
from gauntlet.jsjson import fnv1a32, js_stringify_pretty, normalize_content

from tests.tools.outbound import FOLD_OPERATIONS, fold_vectors

FIXTURES = Path(__file__).with_name("fixtures") / "cross_runtime"
Family = Literal["fnv1a32", "json_spelling", "config_rule"]
FAMILIES: tuple[Family, ...] = ("fnv1a32", "json_spelling", "config_rule")


class VectorOptions(TypedDict, total=False):
    operation: Literal["normalize"]


class Vector(VectorOptions):
    id: str
    input: object
    expected: object


@pytest.mark.parametrize("family", ("outbound_line", "outbound_fold"))
def test_outbound_vector_schema(family: str) -> None:
    payload = json.loads((FIXTURES / f"{family}.json").read_text("utf-8"))
    assert payload["algorithm"] == family
    cases = payload["cases"]
    assert cases
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        if family == "outbound_line":
            assert isinstance(case["expected"], str)
            continue
        operation = case["operation"]
        assert operation in FOLD_OPERATIONS
        assert ("expected_py" in case) == (not operation.endswith("_js"))
        assert ("expected_js" in case) == (not operation.endswith("_py"))


@pytest.mark.parametrize("operation", ("unknown", "fold_inline"))
def test_fold_reader_rejects_unknown_operation(tmp_path: Path, operation: str) -> None:
    path = tmp_path / "vectors.json"
    path.write_text(
        json.dumps(
            {
                "algorithm": "outbound_fold",
                "cases": [{"id": "unknown", "operation": operation}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown fold operation"):
        fold_vectors(path)


@cache
def _cases(family: Family) -> list[Vector]:
    payload = json.loads((FIXTURES / f"{family}.json").read_text(encoding="utf-8"))
    assert payload["algorithm"] == family
    cases = cast(list[Vector], payload["cases"])
    return cases


@pytest.mark.parametrize(
    ("family", "case"),
    [(family, case) for family in FAMILIES for case in _cases(family)],
    ids=[f"{family}:{case['id']}" for family in FAMILIES for case in _cases(family)],
)
def test_cross_runtime_python_vector(family: Family, case: Vector) -> None:
    actual: str | bool
    if family == "fnv1a32":
        assert isinstance(case["input"], str)
        actual = fnv1a32(case["input"])
    elif family == "json_spelling":
        if case["id"].startswith("key_order"):
            assert (
                json.dumps(case["input"], indent=2, ensure_ascii=False)
                != case["expected"]
            )
        before = json.dumps(case["input"])
        if case.get("operation") == "normalize":
            assert isinstance(case["input"], str)
            actual = normalize_content(case["input"])
        else:
            actual = js_stringify_pretty(case["input"])
        assert json.dumps(case["input"]) == before, "the serializer mutated its input"
    elif family == "config_rule":
        args = case["input"]
        assert isinstance(args, dict)
        assert isinstance(args["mode"], str)
        actual = matches_rule(args["rule"], args["value"], args["mode"])
    assert actual == case["expected"]
