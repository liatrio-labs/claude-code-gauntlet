"""Public outbound preparation contracts, independent of delivery composition."""

import json
import subprocess
import sys
from collections.abc import Callable
from typing import Literal

import gauntlet.text as text
import pytest
from gauntlet.markdown import open_fence

from tests.support.outbound import (
    TextOperation,
    TextVector,
    attack_corpus,
    line_vectors,
    markup_corpus,
    text_vectors,
)
from tests.support.outbound_assertions import (
    _render_summary_bodies,
    assert_outbound_string_invariant,
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
VECTOR_IDS = {
    "reference_fence_payload",
    "tilde_fixed",
    "closed_comment_removed",
    "open_comment_contained",
    "hex_comment_removed",
    "newlines_preserved_invisibles_removed",
    "tab_and_lf_preserved",
    "bare_credential_prefix_preserved",
    "rule_cap_marker_outside_limit",
    "rule_exact_cap",
    "rule_collapse_before_cap",
    "invalid_fence_info",
    "fake_fence_quote",
    "table_span_pairing",
    "location_fullwidth",
    "location_space_edges",
    "fence_multiline_marker",
    "quote_after_dash",
    "prose_none",
    "optional_comment_only",
    "optional_lf_edges",
    "image_fence_boundary",
    "reference_multiline_escaped_label",
    "reference_blank_line_stop",
    "reference_trusted_fence",
    "wikilink_multiline",
    "F-suggestion",
    "F-open",
    "F-offsets",
    "F-container-term",
    "A-mid",
    "A-aliases",
    "A-redaction",
    "A-label-control",
    "A-link-control",
    "D-overlap1",
    "N1",
    "N3",
    "D-join",
    "D-unknown",
    "T-suffix",
    "tilde_code",
    "tilde_even",
    "tilde_odd",
    "tilde_short_control",
    "footnote_opener_code_span",
    "footnote_opener_spaced_control",
}
VECTORS: list[TextVector] = [
    case for case in text_vectors() if case["id"] in VECTOR_IDS
] + [
    case
    for case in line_vectors()
    if case["operation"] == "line"
    and case["id"]
    in {
        "outbound_line_reference_indented_code",
        "outbound_line_reference_tab",
        "outbound_line_reference_issue",
        "outbound_line_reference_merge_request",
        "outbound_line_reference_gh",
        "outbound_line_reference_label",
        "outbound_line_reference_milestone",
        "outbound_line_reference_url_protection",
        "outbound_line_reference_urls",
        "outbound_line_reference_named_entities",
        "outbound_line_reference_epic",
        "outbound_line_reference_percent_parenthesized",
        "outbound_line_reference_backslash",
        "outbound_line_email",
        "outbound_line_inline_code",
        "outbound_line_unclosed_code",
        "outbound_line_literal_comparisons",
        "outbound_line_numeric_ascii_digits",
        "outbound_line_image_inline",
        "outbound_line_image_span_boundary",
        "outbound_line_wikilink_inline_code",
        "outbound_line_wikilink_prose_definition_fixpoint",
        "outbound_line_A-split-label-flattened",
        "outbound_line_blank_nbsp",
        "outbound_line_footnote_opener_basic",
    }
]


@pytest.mark.parametrize("prefix", ("ghp_", "glpat-"))
def test_redaction_tokens(prefix: str) -> None:
    assert text.redact_secrets("x " + prefix + "A" * 20) == "x [REDACTED]"
    assert text.redact_secrets("x " + prefix + "A" * 19) == "x " + prefix + "A" * 19


@pytest.mark.parametrize("case", VECTORS, ids=[case["id"] for case in VECTORS])
def test_text_vector(case: TextVector) -> None:
    actual = PREPARERS[case["operation"]](case["input"])
    assert actual == case["expected"]
    if case["operation"] == "line":
        assert text.prepare_line(actual) == actual
    if case["operation"] != "redact":
        assert_outbound_string_invariant(
            actual or "", check_prose_rules=case["operation"] in ("prose", "rule")
        )
    if case.get("also") == "untrusted_fence":
        assert isinstance(case["input"], str)
        assert open_fence(case["input"], strict=True) is None


def test_text_idempotence() -> None:
    for source in attack_corpus(1729):
        once = text.prepare_prose(source)
        assert text.prepare_prose(once) == once, repr(source)
        assert_outbound_string_invariant(once)


def test_markup_idempotence_and_line_parity() -> None:
    sources = markup_corpus()
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            (
                "\nimport { prepareLine } from './workflows/src/outboundText.js';"
                "\nlet source = '';\nfor await (const chunk of process.stdin) sou"
                "rce += chunk;\nprocess.stdout.write(JSON.stringify(JSON.parse(so"
                "urce).map((value) => prepareLine(value))));\n"
            ),
        ],
        input=json.dumps(sources),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    lines = [text.prepare_line(source) for source in sources]
    js_lines: list[str] = json.loads(result.stdout)
    for source, line, js_line in zip(sources, lines, js_lines, strict=True):
        assert js_line == line, repr(source)
        assert text.prepare_line(line) == line, repr(source)
        prose = text.prepare_prose(source)
        assert text.prepare_prose(prose) == prose, repr(source)
        assert_outbound_string_invariant(prose)
    summary_inputs = [
        {
            "findings": [
                {
                    "id": f"markup-{index}",
                    "title": source,
                    "file": "src/markup.py",
                    "line_start": 1,
                    "severity": "low",
                }
            ]
        }
        for index, source in enumerate(sources)
    ]
    summaries = _render_summary_bodies(summary_inputs)
    for source, summary in zip(sources, summaries, strict=True):
        assert text.prepare_prose(summary) == summary, repr(source)


@pytest.mark.parametrize(
    ("source", "expected", "prose"),
    [
        (
            "app/[[...slug]]/page.tsx",
            "`app/[\uff3b...slug]]/page.tsx`",
            "app/[\uff3b...slug]]/page.tsx",
        ),
        ("src/![a](u).py", "`src/!\uff3ba](u).py`", "src/!\uff3ba](u).py"),
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


def test_image_redaction_bridge() -> None:
    source = "!" + "ghp_" + "A" * 36 + "(url)"
    assert text.prepare_prose(source) == "!\uff3bREDACTED](url)"


def test_reference_redaction_bridge() -> None:
    source = "ghp_" + "A" * 36 + ": url"
    assert text.prepare_prose(source) == "[REDACTED]\\: url"


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
        pytest.param("&" + "#38;" * 20000 + "#64;", "\uff20", id="nested-entities"),
        pytest.param("&#" + "9" * 225000 + ";", "", id="numeric_saturation"),
        pytest.param(
            "x " + " ".join("`" * width for width in range(1, 601)),
            "x " + " ".join("\\`" * width for width in range(1, 601)),
            id="unequal-width-runs",
        ),
        pytest.param("[`a`](" * 20000, "[`a`](" * 20000, id="unclosed-destinations"),
    ],
)
def test_large_outbound_preparation(source: str, expected: str) -> None:
    actual = _timed_preparation(source, "line")
    assert actual == expected
    assert text.prepare_line(actual) == actual


@pytest.mark.parametrize("operation", ("prose",))
@pytest.mark.parametrize(
    ("source", "expected"),
    [
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
    ],
)
def test_large_definition_preparation(source: str, expected: str) -> None:
    assert _timed_preparation(source, "prose") == expected


@pytest.mark.parametrize(
    "source,expected",
    [
        pytest.param(
            "<!-- open\n<!-- another -->",
            "&lt;!-- open\n&lt;!-- another -->",
            id="all-literal-openers",
        ),
        pytest.param("&lt;!-- < !--", "&lt;!-- < !--", id="already-neutralized"),
    ],
)
def test_neutralize_comment_openers(source: str, expected: str) -> None:
    assert text.neutralize_comment_openers(source) == expected


@pytest.mark.parametrize(
    "source, expected",
    [
        ("para\n\n    >>> parse(x)", "para\n\n    >>> parse(x)"),
        (
            "para\n\n    ```\n    ~~~\n    [a]: b\n    `",
            "para\n\n    ```\n    ~~~\n    [a]: b\n    `",
        ),
        ("    ```", "    \\`\\`\\`"),
        ("# heading\n    ~~~", "# heading\n    \\~\\~\\~"),
        ("---\n    ~~~", "---\n    \\~\\~\\~"),
        ("para\n\n<custom-tag>\n    ~~~", "para\n\n&lt;custom-tag>\n    \\~\\~\\~"),
        ("para\n    >>> x", "para\n    \\>>> x"),
        ("para\n\n>>>\n    ~~~", "para\n\n\\>>>\n    \\~\\~\\~"),
        ("para\n\n> x\n>\n>     ~~~", "para\n\n> x\n>\n>     \\~\\~\\~"),
        ("- item\n\n    ~~~", "- item\n\n    \\~\\~\\~"),
        pytest.param(
            "2. a\n\n    [x]: y",
            "2. a\n\n    [x]\\: y",
            id="ordered-list-definition-stays-escaped",
        ),
        pytest.param(
            "3) a\n\n    ~~~",
            "3) a\n\n    \\~\\~\\~",
            id="ordered-list-fence-stays-escaped",
        ),
        ("> x\n>\n>     ~~~", "> x\n>\n>     \\~\\~\\~"),
        ("para\n    `lazy\n` paired", "para\n    \\`lazy\n\\` paired"),
        ("para\n<div>\n\n    ~~~", "para\n&lt;div>\n\n    ~~~"),
        ("para\n```invalid`\n    ~~~", "para\n\\`\\``invalid`\n    \\~\\~\\~"),
        ("para\n\n\t~~~", "para\n\n\t~~~"),
        (
            "para\n\n    `@user` [x](https://x.test/@user) <!-- open",
            "para\n\n    `\uff20user` [x](https://x.test/\uff20user) &lt;!-- open",
        ),
        ("para\n\n    ghp_" + "A" * 20 + " ~~~", "para\n\n    [REDACTED] ~~~"),
    ],
)
def test_indented_prose_preserves_only_inert_delimiters(source, expected):
    assert text.prepare_prose(source) == expected
    assert text.prepare_prose(expected) == expected
    assert_outbound_string_invariant(expected)


def test_cited_rules_keep_indented_delimiters_contained():
    assert (
        text.prepared_prose("para\n\n    ```\n    ~~~\n    [a]: b", cap=True)
        == "para\n\n    \\`\\`\n    \\~\\~\\~\n    [a]\\: b"
    )
