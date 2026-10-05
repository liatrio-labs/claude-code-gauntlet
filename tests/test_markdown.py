"""Markdown fence, span, and delimiter rules shared across the Python pipeline."""

import pytest
from gauntlet.markdown import code_span, code_spans, fence_run, open_fence


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("   ````x", ("`", 4, 3), id="opener-shape-indented-run"),
        pytest.param("prose\r````\rx", ("`", 4, 6), id="cr-line-endings"),
        pytest.param("````\n````\nprose", None, id="closed-fence"),
        pytest.param(
            "```\n``` info\n@hidden.md\n",
            ("`", 3, 0),
            id="closer-info-string-does-not-close",
        ),
        pytest.param("```\n``` \t\n", None, id="closer-trailing-spaces-and-tabs"),
        pytest.param("````\n```\n", ("`", 4, 0), id="shorter-closer-does-not-close"),
        pytest.param("```\n````\n", None, id="longer-closer-closes"),
        pytest.param("```\n~~~\n", ("`", 3, 0), id="other-character-does-not-close"),
        pytest.param("```a`b\n@imp.md\n", None, id="backtick-info-string-rejected"),
        pytest.param("~~~a`b\n", ("~", 3, 0), id="tilde-info-allows-backtick"),
        pytest.param("    ```\n", None, id="four-space-indent-is-not-opener"),
        pytest.param("``\n", None, id="opener-needs-three-ticks"),
    ],
)
def test_open_fence(text, expected):
    assert open_fence(text) == expected


@pytest.mark.parametrize(
    "indent",
    [
        pytest.param(" ", id="strict-indent-one"),
        pytest.param("  ", id="strict-indent-two"),
        pytest.param("   ", id="strict-indent-three"),
    ],
)
def test_open_fence_strict_rejects_indented_openers(indent):
    assert open_fence(f"{indent}```\n", strict=True) is None


def test_open_fence_strict_applies_only_to_openers():
    assert open_fence("```\nx\n ```\n", strict=True) is None


def test_open_fence_intervals_include_closed_and_trailing_open_fences():
    closed = "```\nx\n```\n"
    text = closed + "~~~\ny"
    intervals = []
    assert open_fence(text, intervals=intervals) == ("~", 3, len(closed))
    assert intervals == [(0, len(closed)), (len(closed), len(text))]


def test_open_fence_intervals_use_python_offsets_after_non_ascii_text():
    text = "\u00e9\n```\nx\n```\n"
    intervals = []
    assert open_fence(text, intervals=intervals) is None
    assert intervals == [(2, len(text))]


def test_byte_fold_intervals_use_exact_runs_and_literal_span_backslashes():
    unequal = "left `x ``` <ins> @user`"
    assert code_spans(unequal)[0] == [(5, len(unequal))]

    ended_by_backslash = r"left `protected\` <table> @inside`"
    first_close = ended_by_backslash.index("`", 6) + 1
    assert code_spans(ended_by_backslash)[0] == [(5, first_close)]

    escaped_first = r"left \``<ins> @inside`"
    assert code_spans(escaped_first)[0] == [
        (escaped_first.index("``") + 1, len(escaped_first))
    ]


def test_code_spans_skip_trusted_fence_contents():
    text = "`outside`\n```\n`inside`\n```\n`after`"
    spans, fences = code_spans(text)
    assert [text[start:end] for start, end in spans] == ["`outside`", "`after`"]
    assert [text[start:end] for start, end in fences] == ["```\n`inside`\n```\n"]


@pytest.mark.parametrize(
    ("payload", "minimum", "expected"),
    [
        pytest.param("plain text", 3, "```", id="minimum-three"),
        pytest.param("a ``` run", 3, "````", id="longest-run-plus-one"),
        pytest.param("a ```` run", 3, "`````", id="four-run-needs-five"),
        pytest.param("plain text", 5, "`````", id="caller-minimum"),
    ],
)
def test_fence_run(payload, minimum, expected):
    assert fence_run(payload, minimum) == expected


def test_code_span_lengthens_the_run_over_a_backtick_in_the_path():
    assert code_span("a`b") == "``a`b``"
    assert code_span("no ticks here") == "`no ticks here`"


@pytest.mark.parametrize(
    ("value", "options", "expected"),
    [
        pytest.param("`path", {}, "`` `path ``", id="default-pads-backtick-start"),
        pytest.param("path`", {}, "`` path` ``", id="default-pads-backtick-end"),
        pytest.param(
            " path",
            {"pad_space_edges": True},
            "`  path `",
            id="outbound-pads-space-edge",
        ),
        pytest.param(" path", {}, "` path`", id="default-leaves-space-edge"),
        pytest.param(
            "path ",
            {"pad_space_edges": True},
            "` path  `",
            id="outbound-pads-trailing-space",
        ),
        pytest.param("path ", {}, "`path `", id="default-leaves-trailing-space"),
    ],
)
def test_code_span_padding_policy(value, options, expected):
    assert code_span(value, **options) == expected
