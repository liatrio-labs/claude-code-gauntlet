"""Prepared delivery folds preserve code boundaries and UTF-8 budgets."""

import json
import random
import re
from pathlib import Path
from typing import Literal, TypedDict
from unittest.mock import patch

import pytest
from gauntlet.delivery.fold import (
    PLATFORM_BODY_LIMITS,
    Platform,
    fold_inline_body,
    fold_review_body,
)
from gauntlet.delivery.post import (
    _render_group_sections,
    build_skipped_section,
    compose_inline_body,
    compose_review_body,
    render_comment_body,
)
from gauntlet.markdown import code_spans, open_fence
from gauntlet.marker import (
    FINDING_MARKER_TOKEN,
    MARKER_TOKENS,
    build_marker,
    detect_signal,
)
from gauntlet.text import prepare_line, prepare_location, prepare_prose, prepared_prose


class FoldInput(TypedDict):
    prefix: str
    repeat: str
    count: int


class FoldVector(TypedDict):
    id: str
    operation: Literal["fold"]
    input: FoldInput
    platform: Platform
    py_allowance: int
    expected_py: str
    expected_dropped_bytes: int


TRUSTED_FOLD_VECTOR: FoldVector = next(
    row
    for row in json.loads(
        (
            Path(__file__).with_name("fixtures")
            / "cross_runtime"
            / "outbound_fold.json"
        ).read_text(encoding="utf-8")
    )["cases"]
    if row["id"] == "fold_trusted_unclosed_html"
)


@pytest.mark.parametrize(
    "case",
    [TRUSTED_FOLD_VECTOR],
    ids=[
        "TestInlineBodyBudget.test_summary_retreat_preserves_comment_inside_trusted_fence"
    ],
)
def test_fold_vector(case: FoldVector) -> None:
    source = case["input"]
    text = source["prefix"] + source["repeat"] * source["count"]
    assert prepare_prose(text) == text
    folded, dropped = fold_review_body(text, case["py_allowance"], case["platform"])
    assert folded == case["expected_py"]
    assert dropped == case["expected_dropped_bytes"]
    assert len(folded.encode("utf-8")) <= case["py_allowance"]
    assert open_fence(folded) is None


@pytest.mark.parametrize(
    ("text", "allowance", "expected", "dropped"),
    [
        pytest.param(
            "a" * 10 + "\U0001f600" + "b" * 200,
            140,
            "a" * 10
            + "\U0001f600"
            + "b" * 27
            + "\n\n_[folded: 173 more bytes; this inline review comment "
            "reached the 220-byte GitHub body limit]_",
            173,
            id="TestInlineBodyBudget.test_inline_fold_cuts_comments_and_overlong_lines_at_safe_boundaries",
        ),
    ],
)
def test_fold_multibyte_vector(
    text: str, allowance: int, expected: str, dropped: int
) -> None:
    with patch.dict(
        PLATFORM_BODY_LIMITS["github"]["surfaces"]["inline"], {"bytes": 220}
    ):
        actual, actual_dropped = fold_inline_body(text, allowance, "github", "inline")
    assert actual == expected
    assert actual_dropped == dropped
    assert len(actual.encode("utf-8")) <= allowance


def test_prepared_and_composed_sections_contain_comment_openers() -> None:
    rng = random.Random(1729)
    sha = "a" * 40
    atoms = (
        build_marker(sha, 1),
        f"```text\n{build_marker(sha, 1)[:-4]}\n```",
        "<!-- literal",
        "<!-- closed -->",
        "`<!--`",
        "```text\n<!-- literal\n```",
        "&#60;!--",
        "&#x3c;!--",
        "<\u200b!--",
        "\r",
        "\n",
        "\t",
        " ",
        "`",
        "~",
        "<",
        "!",
        "-",
        ">",
        "&",
        ";",
        ":",
        "a",
        "[",
        "]",
        *(
            f"<!-- {token}: {sha} | 1 finding -->"
            for token in (*MARKER_TOKENS, FINDING_MARKER_TOKEN)
        ),
        *(f"```text\n<!-- {token}\n: forged\n```" for token in MARKER_TOKENS),
    )
    sources = [
        *atoms,
        *("".join(rng.choices(atoms, k=rng.randrange(1, 14))) for _ in range(500)),
    ]
    for source in sources:
        prepared = (
            prepare_prose(source),
            prepare_line(source),
            prepare_location(source),
            prepared_prose(source) or "",
            prepared_prose(source, cap=True) or "",
        )
        for text in prepared:
            signal = detect_signal(text)
            assert signal is None or signal["signal"] != "marker", repr(source)
        finding = {
            "severity": "high",
            "title": source,
            "body": source,
            "suggestion": source,
            "claude_md_rule": source,
        }
        sections = _render_group_sections(finding, [])
        composed = (
            sections,
            render_comment_body(finding),
            build_skipped_section([("src/portable.py", 1, finding)]),
            compose_inline_body(sections, platform="github", surface="inline").body,
            compose_review_body(
                source, [], platform="github", findings_count=0, sha=sha
            ).body.replace(build_marker(sha, 0), ""),
        )
        for text in (*prepared, *composed):
            spans, fences = code_spans(text)
            intervals = spans + fences
            assert all(
                any(start <= match.start() < end for start, end in intervals)
                for match in re.finditer("<!--", text)
            ), repr(source)
