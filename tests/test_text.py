"""Public outbound preparation contracts, independent of delivery composition."""

import json
import subprocess
from collections.abc import Callable
from typing import Literal

import gauntlet.text as text
import pytest
from gauntlet.markdown import open_fence

from tests.test_outbound_contract import assert_outbound_string_invariant
from tests.tools.outbound import (
    TextOperation,
    TextVector,
    attack_corpus,
    comment_cases,
    line_vectors,
    markup_corpus,
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
        "id": "corpus_" + case["field_class"] + "_" + case["id"],
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


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "tok " + "ghp_" + "A" * 36 + " and " + "glpat-" + "B" * 20 + " end",
            "tok [REDACTED] and [REDACTED] end",
            id="redact_two_providers",
        ),
        *[
            pytest.param(
                "x " + prefix + "A" * (20 if prefix.endswith("-") else 36) + " y",
                "x [REDACTED] y",
                id="redact_" + prefix.rstrip("_-"),
            )
            for prefix in (
                "ghp_",
                "gho_",
                "ghs_",
                "ghr_",
                "ghu_",
                "github_pat_",
                "glpat-",
                "glrt-",
            )
        ],
    ],
)
def test_redaction_tokens(source: str, expected: str) -> None:
    assert text.redact_secrets(source) == expected


@pytest.mark.parametrize("case", VECTORS, ids=[case["id"] for case in VECTORS])
def test_text_vector(case: TextVector) -> None:
    actual = PREPARERS[case["operation"]](case["input"])
    assert actual == case["expected"]
    if case["operation"] != "redact":
        assert_outbound_string_invariant(
            actual or "", check_prose_rules=case["operation"] in ("prose", "rule")
        )
    if case.get("also") == "untrusted_fence":
        assert isinstance(case["input"], str)
        assert open_fence(case["input"], strict=True) is None


@pytest.mark.parametrize(
    ("source", "redactor_output", "expected"),
    [
        ("&#62;&#62;&#62;", None, "\\>>>"),
        ("redactor output", "/close\n>>>", "\\/close\n\\>>>"),
        ("redactor output", "[[alt|u]]", "\\[[alt|u]]"),
    ],
    ids=[
        "quote_after_normalization",
        "quote_after_redaction",
        "wikilink_after_redaction",
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
            id="prose_and_line_idempotence",
        ),
        pytest.param(
            917,
            ("prose",),
            id="prose_idempotence",
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
    ids=["seeded_containment"],
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


def test_markup_idempotence_and_line_parity() -> None:
    sources = markup_corpus()
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            """
import { prepareLine } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(JSON.stringify(JSON.parse(source).map(prepareLine)));
""",
        ],
        input=json.dumps(sources),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    lines = [text.prepare_line(source) for source in sources]
    assert json.loads(result.stdout) == lines
    for source, line in zip(sources, lines, strict=True):
        assert text.prepare_line(line) == line, repr(source)
        prose = text.prepare_prose(source)
        assert text.prepare_prose(prose) == prose, repr(source)


@pytest.mark.parametrize("operation", ("prose", "line"))
def test_seeded_markup_invariant(operation: TextOperation) -> None:
    for source in markup_corpus():
        assert_outbound_string_invariant(
            PREPARERS[operation](source) or "", check_prose_rules=operation == "prose"
        )


@pytest.mark.parametrize("operation", ("prose", "line", "optional", "rule"))
def test_image_redaction_bridge(operation: TextOperation) -> None:
    source = "!" + "ghp_" + "A" * 36 + "(url)"
    assert PREPARERS[operation](source) == "\\![REDACTED](url)"


@pytest.mark.parametrize("operation", ("prose", "line", "optional", "rule"))
def test_reference_redaction_bridge(operation: TextOperation) -> None:
    source = "ghp_" + "A" * 36 + ": url"
    assert PREPARERS[operation](source) == "[REDACTED]\\: url"
