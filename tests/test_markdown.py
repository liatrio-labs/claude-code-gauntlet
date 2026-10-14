"""Markdown fence, span, and delimiter rules shared across the Python pipeline."""

import pytest
from gauntlet.markdown import (
    code_span,
    code_spans,
    fence_run,
    open_fence,
    paired_code_spans,
    tick_run_index,
)


@pytest.mark.parametrize(
    ("text", "strict", "expected"),
    [
        pytest.param("   ````x", False, ("`", 4, 3), id="opener-shape-indented-run"),
        pytest.param("````\n````\nprose", False, None, id="closed-fence"),
        pytest.param(
            "```\n``` info\n@hidden.md\n",
            False,
            ("`", 3, 0),
            id="closer-info-string-does-not-close",
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
    ],
)
def test_open_fence_intervals(text, expected_state, expected_intervals):
    intervals = []
    assert open_fence(text, intervals=intervals) == expected_state
    assert intervals == expected_intervals


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(
            "left `x ``` <ins> @user`", [(5, 23, 24)], id="unequal-run-payload"
        ),
        pytest.param(r"left \``<ins> @inside`", [(7, 21, 22)], id="escaped-first-tick"),
        pytest.param("`a``b`", [(0, 5, 6)], id="exact-width-closer"),
        pytest.param(r"a \\`x`", [(4, 6, 7)], id="even-slash-opener"),
    ],
)
def test_code_span_index_contract(value, expected):
    index = tick_run_index(value)
    assert paired_code_spans(index) == expected
    assert code_spans(value)[0] == [(start, end) for start, _close, end in expected]


def test_code_span_index_retries_unmatched_run_suffix():
    index = tick_run_index("``a`b")
    assert paired_code_spans(index) == []
    assert paired_code_spans(index, suffix_retry=True) == [(1, 3, 4)]
    assert index.starts_by_width == {2: (0,), 1: (3,)}


def _large_analysis(source, operation):
    import json
    import subprocess
    import sys

    script = """
import json, sys
sys.path.insert(0, 'scripts')
source = sys.stdin.read()
if sys.argv[1] == 'prepare':
    from gauntlet.text import prepare_prose
    sys.stdout.write(prepare_prose(source))
else:
    from gauntlet.markdown import code_spans
    sys.stdout.write(json.dumps(code_spans(source), separators=(',', ':')))
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, operation],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
        check=True,
    )
    return json.loads(result.stdout) if operation == "spans" else result.stdout


def test_large_markdown_analysis():
    count = 40000
    fences_text = "~~~\nx\n~~~\ny\n" * count
    assert _large_analysis(fences_text, "prepare") == fences_text

    source = "`outside`\n" + fences_text + "`after`"
    spans, fences = _large_analysis(source, "spans")
    assert spans == [[0, 9], [len(fences_text) + 10, len(fences_text) + 17]]
    assert fences == [[10 + 12 * index, 20 + 12 * index] for index in range(count)]


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
    ],
)
def test_fence_run(payload, expected):
    assert fence_run(payload) == expected


@pytest.mark.parametrize(
    ("value", "pad_space_edges", "expected"),
    [
        pytest.param("`path", False, "`` `path ``", id="default-pads-backtick-start"),
        pytest.param("a`b", False, "``a`b``", id="lengthens-over-inner-run"),
        pytest.param("no ticks here", False, "`no ticks here`", id="plain-value"),
        pytest.param(
            " path",
            True,
            "`  path `",
            id="outbound-pads-space-edge",
        ),
        pytest.param(" path", False, "` path`", id="default-leaves-space-edge"),
    ],
)
def test_code_span_padding_policy(value, pad_space_edges, expected):
    assert code_span(value, pad_space_edges=pad_space_edges) == expected
