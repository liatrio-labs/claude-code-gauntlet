"""Prepared delivery folds preserve code boundaries and UTF-8 budgets."""

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
from gauntlet.markdown import fence_closer, open_fence
from gauntlet.text import prepare_prose

from tests.support.outbound import (
    FIXTURES,
    FoldVector,
    fold_vectors,
)
from tests.support.outbound_assertions import assert_outbound_string_invariant

FOLD_IDS = {
    "F4",
    "FT",
    "FM",
    "PARTIAL",
    "review_long_closer",
    "fold_crlf_retreat",
    "review_multibyte_boundary",
    "fold_notice_only",
    "inline_partial_suggestion",
    "inline_long_closer",
    "inline_multibyte_boundary",
    "inline_cut_span_fold_review_py",
    "inline_complete_span_fold_review_py",
    "escaped_cut_slash",
    "escaped_cut_quote",
    "short_next_line",
    "fold_impossible_allowance",
    "review_unterminated_lf_retreat",
    "review_first_line_retreat",
    "review_repeated_whole_line_retreat",
    "inline_suggestion_at_zero",
    "inline_suggestion_cr_span_retreat",
}
OWNED_FOLD_VECTORS = [
    row
    for row in [*fold_vectors(), *fold_vectors(FIXTURES / "outbound_fold.json")]
    if row["id"] in FOLD_IDS
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


def test_utf8_len() -> None:
    assert utf8_len("\U0001f600\u6f22") == 7
