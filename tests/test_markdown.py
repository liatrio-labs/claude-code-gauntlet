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


@pytest.mark.parametrize(
    "source, expected",
    [
        (
            "para\n    code\n\n    code\n\nnext",
            [
                (0, False, "paragraph", False),
                (0, False, "paragraph", True),
                (0, False, "blank", False),
                (0, False, "indented_code", False),
                (0, False, "blank", False),
                (0, False, "paragraph", False),
            ],
        ),
        (
            "> - para\n>   lazy\nlazy\n\n    code",
            [
                (4, True, "paragraph", False),
                (4, True, "paragraph", True),
                (0, True, "paragraph", True),
                (0, False, "blank", False),
                (0, False, "indented_code", False),
            ],
        ),
        (
            "- item\n\n    para\n\n      code\nout",
            [
                (2, True, "paragraph", False),
                (0, True, "blank", False),
                (2, True, "paragraph", False),
                (0, True, "blank", False),
                (2, True, "indented_code", False),
                (0, False, "paragraph", False),
            ],
        ),
        (
            "> ```\n> body\nplain",
            [
                (2, True, "fence", False),
                (2, True, "fence", False),
                (0, False, "paragraph", False),
            ],
        ),
        (
            "- ~~~\n  body\n  ~~~~ trailing\n  ~~~~\n  next",
            [
                (2, True, "fence", False),
                (2, True, "fence", False),
                (2, True, "fence", False),
                (2, True, "fence", False),
                (2, True, "paragraph", False),
            ],
        ),
        (
            "# heading\n    code\npara\n===\n    code\n---",
            [
                (0, False, "heading", False),
                (0, False, "indented_code", False),
                (0, False, "paragraph", False),
                (0, False, "heading", False),
                (0, False, "indented_code", False),
                (0, False, "rule", False),
            ],
        ),
        (
            "\tcode\n\n-\titem\n\t\tcode",
            [
                (0, False, "indented_code", False),
                (0, False, "blank", False),
                (2, True, "paragraph", False),
                (1, True, "paragraph", True),
            ],
        ),
        (
            "para\n2. continuation\n\n2. item\n\n    code",
            [
                (0, False, "paragraph", False),
                (0, False, "paragraph", True),
                (0, False, "blank", False),
                (3, True, "paragraph", False),
                (0, True, "blank", False),
                (3, True, "paragraph", False),
            ],
        ),
        pytest.param(
            "> The handler swallows the error.\n2. Then the retry loop spins.\n\n"
            "    [critical]: https://evil.example",
            [
                (2, True, "paragraph", False),
                (0, True, "paragraph", True),
                (0, False, "blank", False),
                (0, False, "indented_code", False),
            ],
            id="ordered-two-continues-unmatched-quote",
        ),
        pytest.param(
            "Details:\n\n-    \n    [critical]: https://evil.example",
            [
                (0, False, "paragraph", False),
                (0, False, "blank", False),
                (2, True, "blank", False),
                (2, True, "paragraph", False),
            ],
            id="blank-list-item-ignores-trailing-padding",
        ),
        (
            "-     code\n\n- - -",
            [
                (2, True, "indented_code", False),
                (0, True, "blank", False),
                (0, False, "rule", False),
            ],
        ),
        pytest.param(
            "~~~\n~~~\u00a0\nx",
            [
                (0, False, "fence", False),
                (0, False, "fence", False),
                (0, False, "fence", False),
            ],
            id="non-ascii-space-does-not-close-fence",
        ),
        pytest.param(
            "<!a>",
            [(0, False, "html", False)],
            id="lowercase-html-declaration",
        ),
        pytest.param(
            "<pre\u00a0>\n\n<div\u00a0>\n\n<x\u00a0>",
            [
                (0, False, "paragraph", False),
                (0, False, "blank", False),
                (0, False, "paragraph", False),
                (0, False, "blank", False),
                (0, False, "paragraph", False),
            ],
            id="non-ascii-space-is-not-tag-boundary",
        ),
        pytest.param(
            "<PrE/>",
            [(0, False, "paragraph", False)],
            id="raw-text-tag-excluded-from-type-seven",
        ),
    ],
)
def test_blocks_container_and_paragraph_facts(source, expected):
    from dataclasses import astuple

    from gauntlet.markdown import blocks

    assert [astuple(line) for line in blocks(source)] == expected


@pytest.mark.parametrize(
    "opening, closing, blank_ends",
    [
        ("<script>", "</script>", False),
        ("<!--", "-->", False),
        ("<?x", "?>", False),
        ("<!DOCTYPE x", ">", False),
        ("<![CDATA[", "]]>", False),
        ("<div>", "", True),
        ('<custom-tag attr="x">', "", True),
    ],
)
def test_blocks_html_end_conditions_and_disable(opening, closing, blank_ends):
    from gauntlet.markdown import blocks

    source = f"{opening}\n\nopaque\n{closing}\n\nlive"
    kinds = [line.kind for line in blocks(source)]
    assert kinds == (
        ["html", "blank", "paragraph", "blank", "blank", "paragraph"]
        if blank_ends
        else ["html", "html", "html", "html", "blank", "paragraph"]
    )
    assert all(line.kind != "html" for line in blocks(source, html=False))


@pytest.mark.parametrize(
    "source, expected",
    [
        (
            "- " * 16000 + "x\n" + "\n" * 30000 + "plain",
            ["paragraph", "blank", "paragraph"],
        ),
        (("> - x\n\nplain\n" * 16667), ["paragraph", "paragraph", "blank"]),
        ("<!--" * 25000, ["html", "html", "html"]),
        (
            ">" * 64000 + "x\n\n    ambiguous\nplain",
            ["paragraph", "paragraph", "paragraph"],
        ),
    ],
    ids=["deep-list-blanks", "alternating-containers", "unclosed-html", "deep-quotes"],
)
def test_blocks_hostile_inputs(source, expected):
    import json
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            "import json, sys; sys.path.insert(0, 'scripts'); from gauntlet.markdown import blocks; facts = blocks(sys.stdin.read()); print(json.dumps([facts[0].kind, facts[len(facts)//2].kind, facts[-1].kind])); assert not any(f.kind == 'indented_code' and not f.contained for f in facts)",
        ],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
        check=True,
    )
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("comment, masked", [("<!--", False), ("<!-->", True)])
def test_import_inline_scan_does_not_rescan_comment_suffixes(comment, masked):
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); from gauntlet.project_rules import _strip_code; text = sys.stdin.read(); result = _strip_code(text); assert result == ('text ' + ' ' * 25000 if sys.argv[1] == 'masked' else text)",
            "masked" if masked else "literal",
        ],
        input="text " + comment * 25000,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
        check=True,
    )
    assert result.stdout == ""
