"""Public outbound preparation contracts, independent of delivery composition."""

from collections.abc import Callable
from typing import Literal

import gauntlet.text as text
import pytest
from gauntlet.markdown import open_fence

from tests.tools.outbound import (
    TextOperation,
    TextVector,
    assert_outbound_string_invariant,
    attack_corpus,
    comment_cases,
    line_vectors,
    text_vectors,
)


def _prepare_rule(value: object) -> str | None:
    return text.prepared_prose(value, cap=True)


PREPARERS: dict[TextOperation, Callable[[object], str | None]] = {
    "prose": text.prepare_prose,
    "line": text.prepare_line,
    "optional": text.prepared_prose,
    "rule": _prepare_rule,
    "redact": lambda value: text.redact_secrets(str(value)),
    "location": text.prepare_location,
}
FIELD_OPERATIONS: dict[str, TextOperation] = {
    "prose": "prose",
    "rule": "rule",
    "single_line": "line",
    "location": "line",
}
CORPUS_VECTORS: list[TextVector] = [
    {
        "id": (
            "test_prepare_prose_fixture_cases:"
            if case["field_class"] in ("prose", "rule")
            else "test_prepare_line_fixture_cases:"
        )
        + case["id"],
        "operation": FIELD_OPERATIONS[case["field_class"]],
        "input": case["input"],
        "expected": case["expected"] or None
        if case["field_class"] == "rule"
        else case["expected"],
    }
    for case in comment_cases()
]
VECTORS: list[TextVector] = [
    *text_vectors(),
    *CORPUS_VECTORS,
    *line_vectors(),
]


@pytest.mark.parametrize("case", VECTORS, ids=[case["id"] for case in VECTORS])
def test_text_vector(case: TextVector) -> None:
    actual = PREPARERS[case["operation"]](case["input"])
    assert actual == case["expected"]
    if case["operation"] != "redact":
        assert_outbound_string_invariant(
            actual or "", check_prose_rules=case["operation"] in ("prose", "rule")
        )
    if ":fake-" in case["id"] or case["id"].endswith(":bad-info"):
        assert isinstance(case["input"], str)
        assert open_fence(case["input"], strict=True) is None


@pytest.mark.parametrize(
    ("source", "redactor_output", "expected"),
    [
        ("&#62;&#62;&#62;", None, "\\>>>"),
        ("redactor output", "/close\n>>>", "\\/close\n\\>>>"),
    ],
    ids=[
        "test_prose_escape_rules_run_after_normalization_and_redaction:normalization",
        "test_prose_escape_rules_run_after_normalization_and_redaction:redaction",
    ],
)
def test_text_pass_order(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    redactor_output: str | None,
    expected: str,
) -> None:
    if redactor_output is not None:
        monkeypatch.setattr(text, "redact_secrets", lambda _value: redactor_output)
    assert text.prepare_prose(source) == expected


@pytest.mark.parametrize(
    ("seed", "operations"),
    [
        pytest.param(
            1729,
            ("prose", "line"),
            id="test_preparation_is_idempotent_over_fixture_and_seeded_corpus",
        ),
        pytest.param(
            917,
            ("prose",),
            id="TestOutboundComposerContracts.test_prose_preparation_is_idempotent_for_fixtures_and_seeded_inputs",
        ),
    ],
)
def test_text_idempotence(
    seed: Literal[1729, 917], operations: tuple[TextOperation, ...]
) -> None:
    sources = [case["input"] for case in comment_cases()] + attack_corpus(seed)
    assert len(sources) >= 5000
    for source in sources:
        for operation in operations:
            prepare = PREPARERS[operation]
            once = prepare(source)
            assert prepare(once) == once, (operation, repr(source))


@pytest.mark.parametrize(
    "seed",
    [1729],
    ids=["test_string_invariant_for_fixtures_seeded_corpus_and_poisoned_sinks"],
)
def test_text_invariant(seed: Literal[1729, 917]) -> None:
    for case in comment_cases():
        operation = FIELD_OPERATIONS[case["field_class"]]
        assert_outbound_string_invariant(
            PREPARERS[operation](case["input"]) or "",
            check_prose_rules=operation != "line",
        )
    for source in attack_corpus(seed):
        assert_outbound_string_invariant(text.prepare_prose(source))
        assert_outbound_string_invariant(
            text.prepare_line(source), check_prose_rules=False
        )
