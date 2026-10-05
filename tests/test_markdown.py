"""Markdown fence, span, and delimiter rules shared across the Python pipeline."""

import pytest
from gauntlet.markdown import code_span, code_spans, fence_run, open_fence


@pytest.mark.parametrize(
    ("text", "strict", "expected"),
    [
        pytest.param("   ````x", False, ("`", 4, 3), id="opener-shape-indented-run"),
        pytest.param("prose\r````\rx", False, ("`", 4, 6), id="cr-line-endings"),
        pytest.param("````\n````\nprose", False, None, id="closed-fence"),
        pytest.param(
            "```\n``` info\n@hidden.md\n",
            False,
            ("`", 3, 0),
            id="closer-info-string-does-not-close",
        ),
        pytest.param(
            "```\n``` \t\n", False, None, id="closer-trailing-spaces-and-tabs"
        ),
        pytest.param(
            "````\n```\n", False, ("`", 4, 0), id="shorter-closer-does-not-close"
        ),
        pytest.param("```\n````\n", False, None, id="longer-closer-closes"),
        pytest.param(
            "```\n~~~\n", False, ("`", 3, 0), id="other-character-does-not-close"
        ),
        pytest.param(
            "```a`b\n@imp.md\n", False, None, id="backtick-info-string-rejected"
        ),
        pytest.param("~~~a`b\n", False, ("~", 3, 0), id="tilde-info-allows-backtick"),
        pytest.param("    ```\n", False, None, id="four-space-indent-is-not-opener"),
        pytest.param("``\n", False, None, id="opener-needs-three-ticks"),
        pytest.param(" ```\n", True, None, id="strict-indent-one"),
        pytest.param("  ```\n", True, None, id="strict-indent-two"),
        pytest.param("   ```\n", True, None, id="strict-indent-three"),
        pytest.param(
            "```\nx\n ```\n",
            True,
            None,
            id="strict-indented-closer-still-closes",
        ),
    ],
)
def test_open_fence(text, strict, expected):
    assert open_fence(text, strict=strict) == expected


@pytest.mark.parametrize(
    ("text", "expected_state", "expected_intervals"),
    [
        pytest.param(
            "```\nx\n```\n~~~\ny",
            ("~", 3, 10),
            [(0, 10), (10, 15)],
            id="closed-and-trailing-open-fences",
        ),
        pytest.param(
            "é\n```\nx\n```\n",
            None,
            [(2, 12)],
            id="python-offsets-after-non-ascii-text",
        ),
    ],
)
def test_open_fence_intervals(text, expected_state, expected_intervals):
    intervals = []
    assert open_fence(text, intervals=intervals) == expected_state
    assert intervals == expected_intervals


def test_code_spans_pair_exact_runs_with_literal_backslashes():
    unequal = "left `x ``` <ins> @user`"
    assert code_spans(unequal)[0] == [(5, len(unequal))]

    ended_by_backslash = r"left `protected\` <table> @inside`"
    first_close = ended_by_backslash.index("`", 6) + 1
    assert code_spans(ended_by_backslash)[0] == [(5, first_close)]

    escaped_first = r"left \``<ins> @inside`"
    assert code_spans(escaped_first)[0] == [
        (escaped_first.index("``") + 1, len(escaped_first))
    ]


def test_code_spans_even_backslashes_leave_the_opener_live():
    text = r"a \\`x`"
    assert code_spans(text)[0] == [(4, 7)]


def test_code_spans_skip_trusted_fence_contents():
    text = "`outside`\n```\n`inside`\n```\n`after`"
    spans, fences = code_spans(text)
    assert [text[start:end] for start, end in spans] == ["`outside`", "`after`"]
    assert [text[start:end] for start, end in fences] == ["```\n`inside`\n```\n"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param("plain text", "```", id="minimum-three"),
        pytest.param("a ``` run", "````", id="longest-run-plus-one"),
        pytest.param("a ```` run", "`````", id="four-run-needs-five"),
    ],
)
def test_fence_run(payload, expected):
    assert fence_run(payload) == expected


@pytest.mark.parametrize(
    ("value", "pad_space_edges", "expected"),
    [
        pytest.param("`path", False, "`` `path ``", id="default-pads-backtick-start"),
        pytest.param("path`", False, "`` path` ``", id="default-pads-backtick-end"),
        pytest.param("a`b", False, "``a`b``", id="lengthens-over-inner-run"),
        pytest.param("no ticks here", False, "`no ticks here`", id="plain-value"),
        pytest.param(
            " path",
            True,
            "`  path `",
            id="outbound-pads-space-edge",
        ),
        pytest.param(" path", False, "` path`", id="default-leaves-space-edge"),
        pytest.param(
            "path ",
            True,
            "` path  `",
            id="outbound-pads-trailing-space",
        ),
        pytest.param("path ", False, "`path `", id="default-leaves-trailing-space"),
    ],
)
def test_code_span_padding_policy(value, pad_space_edges, expected):
    assert code_span(value, pad_space_edges=pad_space_edges) == expected
