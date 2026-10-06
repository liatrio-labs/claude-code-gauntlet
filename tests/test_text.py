"""Public outbound preparation contracts, independent of delivery composition."""

import json
import re
import subprocess
import sys
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


@pytest.mark.parametrize("delimiter", ("```", "~~~"), ids=["backtick", "tilde"])
@pytest.mark.parametrize(
    "info",
    (
        "suggestion",
        " suggestion",
        "suggestion foo",
        "suggestion:-0+0",
        "sug&#103;estion",
        "Suggestion",
        "SUGGESTION",
        "{suggestion}",
        "suggestion\\",
        "python",
        "future:params",
    ),
)
@pytest.mark.parametrize("closed", (True, False), ids=["closed", "open"])
def test_prose_fence_info_is_empty(delimiter: str, info: str, closed: bool) -> None:
    source = f"{delimiter}{info}\nx = SENT"
    if closed:
        source += f"\n{delimiter}"
    assert text.prepare_prose(source).splitlines() == [delimiter, "x = SENT", delimiter]


@pytest.mark.parametrize(
    "prefix",
    (
        " ",
        "  ",
        "   ",
        "    ",
        "\t",
        "> ",
        "> - ",
        "- ",
        "+ ",
        "* ",
        "1. ",
        "1) ",
        ": ",
        "~ ",
        "text ",
    ),
)
def test_prose_tilde_runs_escape_all_container_prefixes(prefix: str) -> None:
    source = f"term\n{prefix}~~~suggestion\n  x = SENT\n  ~~~\n\ntail"
    assert text.prepare_prose(source) == (
        f"term\n{prefix}\\~~~suggestion\n  x = SENT\n  \\~~~\n\ntail"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "é\n~~~python\nx\n~~~\ny\n```mermaid\nz\n```\n@out <b>",
            "é\n~~~\nx\n~~~\ny\n```\nz\n```\n＠out &lt;b>",
            id="original-fence-offsets",
        ),
        pytest.param(
            "x " + "ghp_" + "A" * 36 + ": //e/SENT](a b) after",
            "x [REDACTED]： //e/SENT](a b) after",
            id="redaction-before-definition-guard",
        ),
        pytest.param(r"already \~~~", r"already \~~~", id="odd-slash-run"),
        pytest.param(r"even \\~~~", r"even \\\~~~", id="even-slash-run"),
        pytest.param("`~~~`", "`\\~~~`", id="inline-code-tilde-run"),
    ],
)
def test_prose_structure_pass_order(source: str, expected: str) -> None:
    assert text.prepare_prose(source) == expected
    assert text.prepare_prose(expected) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "before x [a]: //example.test/SENT](a b) after",
            "before x [a]： //example.test/SENT](a b) after",
            id="midline",
        ),
        pytest.param(
            "x [a]: //e/SENT]\\(a b) after",
            "x [a]： //e/SENT]\\(a b) after",
            id="escaped-trigger",
        ),
        pytest.param("x [a]: u][a b] q", "x [a]: u][a b] q", id="no-pair"),
        pytest.param("x [a]: u] (a b) q", "x [a]: u] (a b) q", id="spaced-pair"),
        pytest.param("x [a]: u]\\\\(a b) q", "x [a]: u]\\\\(a b) q", id="two-slashes"),
        pytest.param(
            "`x [a]: SENT](a b)`",
            "`x [a]: SENT](a b)`",
            id="code-span-control",
        ),
        pytest.param("[[x]: u", "[\uff3bx]\\: u", id="after-containment-pass"),
        pytest.param(
            "**x [a]: u](a b)**",
            "**x [a]： u](a b)**",
            id="bold-prefix",
        ),
        pytest.param("> x [a]: u](a b)", "> x [a]： u](a b)", id="quote-prefix"),
        pytest.param("- x [a]: u](a b)", "- x [a]： u](a b)", id="list-prefix"),
    ],
)
def test_definition_colon_contract(source: str, expected: str) -> None:
    assert text.prepare_prose(source) == expected
    assert text.prepare_line(source) == expected


def test_entity_vocabulary_complete() -> None:
    from html.entities import html5

    expected = {
        "commat;": "@",
        "excl;": "!",
        "lbrack;": "[",
        "lsqb;": "[",
        "rsqb;": "]",
        "rbrack;": "]",
        "colon;": ":",
        "lpar;": "(",
        "rpar;": ")",
        "bsol;": "\\",
    }
    targets = set(expected.values())
    assert {name: value for name, value in html5.items() if value in targets} == expected
    for name, value in expected.items():
        assert text._normalize_outbound("&" + name) == value


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("<!-->tail", "<!-->tail", id="overlapping-one"),
        pytest.param("<!--->tail", "<!--->tail", id="overlapping-two"),
        pytest.param("<!---->tail", "tail", id="empty-comment"),
        pytest.param("<!-->x-->y", "y", id="later-closer-after-one"),
        pytest.param("<!--->x-->y", "y", id="later-closer-after-two"),
        pytest.param("<!-<!--x-->->tail", "<!-->tail", id="restored-overlap"),
        pytest.param(
            "<!--&#38;#45;&#38;#45;&#38;#62;z-->tail",
            "z-->tail",
            id="entity-decoded-closer",
        ),
        pytest.param("<!--x--\u200b>y-->z", "y-->z", id="invisible-completes-closer"),
        pytest.param(
            "<!-<!--x-->- y --<!--z-->> w", "> w", id="sibling-comment-join"
        ),
        pytest.param(
            "<!-<!--x-->- a --&#6<!--y-->2; b -->",
            "2; b -->",
            id="sibling-join-with-decoded-tail",
        ),
    ],
)
def test_normalizer_overlap_and_semantics(source: str, expected: str) -> None:
    assert text._normalize_outbound(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("&#" + "0" * 5000 + "64;", "@", id="saturating-decimal"),
        pytest.param("&#" + "9" * 5000 + ";", "", id="decimal-overflow"),
        pytest.param("&#x" + "0" * 5000 + "40;", "@", id="saturating-hex"),
    ],
)
def test_numeric_reference_outputs(source: str, expected: str) -> None:
    assert text._normalize_outbound(source) == expected


def _has_complete_nonoverlapping_comment(value: str) -> bool:
    opener_end: int | None = None
    index = 0
    while index < len(value):
        if opener_end is None and value.startswith("<!--", index):
            opener_end = index + 4
            index += 4
            continue
        if opener_end is not None and value.startswith("-->", index):
            if index >= opener_end:
                return True
            index += 3
            continue
        index += 1
    return False


@pytest.mark.parametrize("seed", [1729], ids=["seeded_normal_form"])
def test_seeded_normal_form(seed: Literal[1729, 917]) -> None:
    invisible_ranges = (
        range(0x00, 0x09),
        (0x0B, 0x0C, 0x0D),
        range(0x0E, 0x20),
        range(0x7F, 0xA0),
        (0x00AD, 0x200B, 0x200C, 0x200D, 0xFEFF, 0x2060),
        range(0x202A, 0x202F),
        range(0x2066, 0x206A),
    )
    recognized_names = (
        "commat",
        "excl",
        "lbrack",
        "lsqb",
        "rsqb",
        "rbrack",
        "colon",
        "lpar",
        "rpar",
        "bsol",
    )
    sources = [row["input"] for row in VECTORS if isinstance(row["input"], str)]
    sources.extend(attack_corpus(seed))
    for source in sources:
        normalized = text._normalize_outbound(source)
        assert not _has_complete_nonoverlapping_comment(normalized), repr(source)
        assert re.search(r"&#(?:[0-9]+|[xX][0-9a-fA-F]+);", normalized) is None
        assert all(f"&{name};" not in normalized for name in recognized_names)
        assert not any(
            ord(character) in ord_range
            for character in normalized
            for ord_range in invisible_ranges
        )
        assert text._normalize_outbound(normalized) == normalized, repr(source)


@pytest.mark.parametrize(
    ("source", "redactor_output", "expected"),
    [
        ("&#62;&#62;&#62;", None, "\\>>>"),
        ("redactor output", "/close\n>>>", "\\/close\n\\>>>"),
        ("redactor output", "[[alt|u]]", "[\uff3balt|u]]"),
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
            ("prose", "line", "optional"),
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


@pytest.mark.parametrize(
    ("source", "expected", "prose"),
    [
        (
            "app/[[...slug]]/page.tsx",
            "`app/[\uff3b...slug]]/page.tsx`",
            "app/[\uff3b...slug]]/page.tsx",
        ),
        ("src/![a](u).py", "`src/!\uff3ba](u).py`", "src/!\uff3ba](u).py"),
        ("src/[[[a.py", "`src/[\uff3b\uff3ba.py`", "src/[\uff3b\uff3ba.py"),
        (
            "src/\\[[a]]/\\![a](u).py",
            "`src/\\[\uff3ba]]/\\!\uff3ba](u).py`",
            "src/\\[\uff3ba]]/\\!\uff3ba](u).py",
        ),
        (
            "src/\\\\[[[a.py",
            "`src/\\\\[\uff3b\uff3ba.py`",
            "src/\\\\[\uff3b\uff3ba.py",
        ),
        ("src/!\\[a](u).py", "`src/!\\\uff3ba](u).py`", "src/!\\\uff3ba](u).py"),
        ("src/[\\[a]].py", "`src/[\\\uff3ba]].py`", "src/[\\\uff3ba]].py"),
    ],
)
def test_location_openers_are_prose_fixed_points(
    source: str, expected: str, prose: str
) -> None:
    location = text.prepare_location(source)
    assert location == expected
    assert text.prepare_prose(location) == expected
    assert text.prepare_prose(text.prepare_prose(location)) == expected
    assert text.prepare_prose(source) == prose
    assert text.prepare_prose(prose) == prose


@pytest.mark.parametrize("operation", ("prose", "line"))
def test_seeded_markup_invariant(operation: TextOperation) -> None:
    for source in markup_corpus():
        assert_outbound_string_invariant(
            PREPARERS[operation](source) or "", check_prose_rules=operation == "prose"
        )


@pytest.mark.parametrize("operation", ("prose", "line", "optional", "rule"))
def test_image_redaction_bridge(operation: TextOperation) -> None:
    source = "!" + "ghp_" + "A" * 36 + "(url)"
    assert PREPARERS[operation](source) == "!\uff3bREDACTED](url)"


@pytest.mark.parametrize("operation", ("prose", "line", "optional", "rule"))
def test_reference_redaction_bridge(operation: TextOperation) -> None:
    source = "ghp_" + "A" * 36 + ": url"
    assert PREPARERS[operation](source) == "[REDACTED]\\: url"


def _timed_preparation(source: str, operation: Literal["prose", "line"]) -> str:
    # A child deadline bounds failures in preparation.
    result = subprocess.run(
        [
            sys.executable,
            # The child's stdio defaults to the Windows code page, which cannot
            # encode the fullwidth bracket.
            "-X",
            "utf8",
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            f"from gauntlet.text import prepare_{operation}; "
            f"sys.stdout.write(prepare_{operation}(sys.stdin.read()))",
        ],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
        check=True,
    )
    return result.stdout


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("<!--" * 25000, "&lt;!--" * 25000, id="comment-openers"),
        pytest.param(
            "<&excl;--" * 20000,
            "&lt;!--" * 20000,
            id="entity-built-comment-openers",
        ),
        pytest.param("&" + "#38;" * 20000 + "#64;", "＠", id="nested-entities"),
        pytest.param(
            "<!" * 55000 + "<!-- x -->" + "-- y -->" * 55000,
            "",
            id="comment-deletion-waves",
        ),
        pytest.param("x" + "`" * 250000, "x" + "\\`" * 250000, id="tick-run"),
        pytest.param(
            "x " + " ".join("`" * width for width in range(1, 601)),
            "x " + " ".join("\\`" * width for width in range(1, 601)),
            id="unequal-width-runs",
        ),
    ],
)
def test_large_outbound_preparation(source: str, expected: str) -> None:
    actual = _timed_preparation(source, "line")
    assert actual == expected
    assert text.prepare_line(actual) == actual


@pytest.mark.parametrize("operation", ("prose", "line"))
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("\\" * 500000, "\\" * 500000, id="bare"),
        pytest.param(
            "!" + "\\" * 500000 + "[",
            "!" + "\\" * 500000 + "\uff3b",
            id="image_split",
        ),
    ],
)
def test_large_backslash_preparation(
    operation: Literal["prose", "line"], source: str, expected: str
) -> None:
    assert _timed_preparation(source, operation) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "[a\n" + " " * 500000 + "\\a",
            "[a\n" + " " * 500000 + "\\a",
            id="continuation_prefix",
        ),
        pytest.param(
            ("[" + "\\a" * 2500 + "\n") * 100,
            ("[" + "\\a" * 2500 + "\n") * 100,
            id="unclosed_labels",
        ),
    ],
)
def test_large_definition_preparation(source: str, expected: str) -> None:
    assert _timed_preparation(source, "prose") == expected
