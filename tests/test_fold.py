"""Prepared delivery folds preserve code boundaries and UTF-8 budgets."""

import random
import re
from contextlib import nullcontext
from typing import Literal
from unittest.mock import patch

import pytest
from gauntlet.delivery.fold import (
    PLATFORM_BODY_LIMITS,
    Platform,
    body_limit,
    fold_inline_body,
    fold_review_body,
    utf8_len,
)
from gauntlet.delivery.post import (
    BRAND_TRAILER,
    build_skipped_section,
    compose_inline_body,
    compose_review_body,
    render_comment_body,
)
from gauntlet.markdown import code_spans, fence_closer, open_fence
from gauntlet.marker import (
    FINDING_MARKER_TOKEN,
    MARKER_TOKENS,
    build_marker,
    detect_signal,
)
from gauntlet.text import prepare_line, prepare_location, prepare_prose, prepared_prose

from tests.test_outbound_contract import assert_outbound_string_invariant
from tests.tools.outbound import (
    FIXTURES,
    FoldVector,
    fold_vectors,
)

OWNED_FOLD_VECTORS = [
    row
    for row in [*fold_vectors(), *fold_vectors(FIXTURES / "outbound_fold.json")]
    if not row["operation"].endswith("_js")
]


@pytest.mark.parametrize(
    "case",
    OWNED_FOLD_VECTORS,
    ids=[row["id"] for row in OWNED_FOLD_VECTORS],
)
def test_fold_fixture(case: FoldVector) -> None:
    operation = case["operation"]
    source = case["input"]
    if operation in ("closer", "fence_state"):
        assert isinstance(source, str)
        if operation == "closer":
            assert fence_closer(source) == case["expected_py"]
        else:
            state = open_fence(source)
            assert (list(state) if state is not None else None) == case["expected_py"]
        return
    assert operation in ("fold", "fold_review_py", "fold_inline_py")
    assert not isinstance(source, str)
    sections = source["prefix"] + source["repeat"] * source["count"]
    surface = case.get("surface", "summary")
    context = (
        patch.dict(
            PLATFORM_BODY_LIMITS[case["platform"]]["surfaces"][surface],
            {"bytes": case["limit_bytes"]},
        )
        if "limit_bytes" in case
        else nullcontext()
    )
    with context:
        folded, dropped = (
            fold_inline_body(sections, case["py_allowance"], case["platform"], surface)
            if operation == "fold_inline_py"
            else fold_review_body(sections, case["py_allowance"], case["platform"])
        )
    assert folded == case["expected_py"]
    assert dropped == case["expected_dropped_bytes"]
    if case.get("fits_allowance", True):
        assert len(folded.encode("utf-8")) <= case["py_allowance"]
    else:
        # The poster refuses an envelope that cannot even fit the fold notice.
        assert len(folded.encode("utf-8")) > case["py_allowance"]
    assert open_fence(folded) is None
    if case.get("also") == "prepared_fixpoint":
        assert prepare_prose(sections) == sections
    if case.get("also") == "string_invariant":
        assert_outbound_string_invariant(folded)


@pytest.mark.parametrize(
    ("platform", "label", "surface", "name", "byte_limit"),
    [
        ("github", "GitHub", "summary", "review body", 65536),
        ("github", "GitHub", "inline", "inline review comment", 65536),
        ("gitlab", "GitLab", "summary", "summary note", 1000000),
        ("gitlab", "GitLab", "discussion", "inline discussion", 1000000),
        ("gitlab", "GitLab", "note", "corroborator note", 1000000),
    ],
    ids=[
        row
        for row in (
            "github_summary",
            "github_inline",
            "gitlab_summary",
            "gitlab_discussion",
            "gitlab_note",
        )
    ],
)
def test_body_limit(
    platform: Platform,
    label: str,
    surface: Literal["summary", "inline", "discussion", "note"],
    name: str,
    byte_limit: int,
) -> None:
    limit = body_limit(platform, surface)
    assert (limit.label, limit.surface, limit.bytes) == (label, name, byte_limit)
    assert PLATFORM_BODY_LIMITS[platform]["surfaces"][surface] == {
        "surface": name,
        "bytes": byte_limit,
    }
    assert PLATFORM_BODY_LIMITS[platform]["label"] == label


def test_body_limit_has_only_supported_platforms_and_surfaces() -> None:
    assert set(PLATFORM_BODY_LIMITS) == {"github", "gitlab"}
    assert set(PLATFORM_BODY_LIMITS["github"]["surfaces"]) == {"summary", "inline"}
    assert set(PLATFORM_BODY_LIMITS["gitlab"]["surfaces"]) == {
        "summary",
        "discussion",
        "note",
    }


@pytest.mark.parametrize(
    ("platform", "surface"),
    [("github", "discussion"), ("github", "note"), ("gitlab", "inline")],
)
def test_body_limit_rejects_unsupported_surface(
    platform: Platform, surface: Literal["discussion", "note", "inline"]
) -> None:
    with pytest.raises(KeyError):
        body_limit(platform, surface)


@pytest.mark.parametrize(
    ("text", "expected"), [("", 0), ("ascii", 5), ("\U0001f600\u6f22", 7)]
)
def test_utf8_len(text: str, expected: int) -> None:
    assert utf8_len(text) == expected


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
        sections = render_comment_body(finding).removesuffix(f"\n\n{BRAND_TRAILER}")
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
