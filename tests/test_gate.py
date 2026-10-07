"""Pure gate verdicts, platform sites, and first-wins intervals."""

import copy

import pytest
from gauntlet.delivery import gate
from gauntlet.diff import parse_diff

from tests.support.diff import diff_facts

DIFF = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,3 @@\n def f():\n+    return 1\n+    # tail\n"


@pytest.mark.parametrize(
    "overrides, absent, apply_range, expected",
    [
        pytest.param(
            {"file": [], "line": []},
            ("suggested_fix_code",),
            (2, 3),
            gate.FixVerdict(True, None, (2, 3)),
            id="absent-fix",
        ),
        pytest.param(
            {}, (), (2, 3), gate.FixVerdict(True, None, (2, 3)), id="multiline"
        ),
        pytest.param(
            {"end_line": 2, "suggested_fix_code": "    return 2"},
            (),
            (2, 2),
            gate.FixVerdict(True, None, (2, 2)),
            id="single-line",
        ),
        pytest.param(
            {"suggested_fix_code": "        return 2\n        # done"},
            (),
            (2, 3),
            gate.FixVerdict(True, None, (2, 3)),
            id="reindentation",
        ),
        pytest.param(
            {"line": type("Line", (int,), {})(2)},
            (),
            (2, 3),
            gate.FixVerdict(True, None, (2, 3)),
            id="integer-subclass",
        ),
        pytest.param(
            {"suggested_fix_code": 42},
            (),
            (2, 3),
            gate.FixVerdict(False, "non_string", (2, 3)),
            id="non-string",
        ),
        pytest.param(
            {"suggested_fix_code": None},
            (),
            (2, 3),
            gate.FixVerdict(False, "non_string", (2, 3)),
            id="null",
        ),
        pytest.param(
            {"suggested_fix_code": "  \n  "},
            (),
            (2, 3),
            gate.FixVerdict(False, "empty", (2, 3)),
            id="empty",
        ),
        pytest.param(
            {"suggested_fix_code": "foo\rbar\rbaz"},
            (),
            (2, 3),
            gate.FixVerdict(False, "carriage_return", (2, 3)),
            id="interior-cr",
        ),
        pytest.param(
            {"suggested_fix_code": "    return 2\r\n    # done\r\n"},
            (),
            (2, 3),
            gate.FixVerdict(False, "carriage_return", (2, 3)),
            id="crlf-fix",
        ),
        pytest.param(
            {"suggested_fix_code": "    token = 'ghp_" + "A" * 24 + "'"},
            (),
            (2, 3),
            gate.FixVerdict(False, "redacted", (2, 3)),
            id="redacted",
        ),
        pytest.param(
            {},
            ("end_line",),
            (2, 2),
            gate.FixVerdict(False, "missing_end_line", (2, 2)),
            id="missing-end",
        ),
        pytest.param(
            {"end_line": None},
            (),
            (2, 2),
            gate.FixVerdict(False, "missing_end_line", (2, 2)),
            id="null-end",
        ),
        pytest.param(
            {"line": 3, "end_line": 2},
            (),
            (3, 2),
            gate.FixVerdict(False, "invalid_range", (3, 2)),
            id="reversed-range",
        ),
        pytest.param(
            {"line": 2.0},
            (),
            (2.0, 3),
            gate.FixVerdict(False, "invalid_range", None),
            id="float-line",
        ),
        pytest.param(
            {"line": True, "end_line": True},
            (),
            (True, True),
            gate.FixVerdict(False, "invalid_range", None),
            id="boolean-line",
        ),
        pytest.param(
            {"line": 0, "end_line": 1},
            (),
            (0, 1),
            gate.FixVerdict(False, "invalid_range", (0, 1)),
            id="nonpositive-line",
        ),
        pytest.param(
            {"end_line": 940},
            (),
            (2, 940),
            gate.FixVerdict(False, "range_not_in_diff", (2, 940)),
            id="range-not-in-diff",
        ),
        pytest.param(
            {"file": "other.py"},
            (),
            (2, 3),
            gate.FixVerdict(False, "range_not_in_diff", (2, 3)),
            id="unknown-path",
        ),
        pytest.param(
            {},
            (),
            (2, 2),
            gate.FixVerdict(False, "anchor_mismatch", (2, 2)),
            id="narrow-anchor",
        ),
        pytest.param(
            {},
            (),
            None,
            gate.FixVerdict(False, "anchor_mismatch", None),
            id="no-anchor",
        ),
        pytest.param(
            {"suggested_fix_code": "    return 1\n    # tail"},
            (),
            (2, 3),
            gate.FixVerdict(False, "no_op_replacement", (2, 3)),
            id="no-op",
        ),
        pytest.param(
            {"suggested_fix_code": "\treturn 2\n\t# done"},
            (),
            (2, 3),
            gate.FixVerdict(False, "indentation_mismatch", (2, 3)),
            id="space-to-tab",
        ),
        pytest.param(
            {"suggested_fix_code": "\n    return 1\n    # tail"},
            (),
            (2, 3),
            gate.FixVerdict(True, None, (2, 3)),
            id="leading-blank",
        ),
        pytest.param(
            {"suggested_fix_code": "    return 1\n    # tail\n\n"},
            (),
            (2, 3),
            gate.FixVerdict(True, None, (2, 3)),
            id="trailing-blank",
        ),
        pytest.param(
            {"suggested_fix_code": "    return 1\n    # tail\n"},
            (),
            (2, 3),
            gate.FixVerdict(False, "no_op_replacement", (2, 3)),
            id="one-lf-no-op",
        ),
        pytest.param(
            {"suggested_fix_code": "\n".join(f"    line{n}" for n in range(101))},
            (),
            (2, 3),
            gate.FixVerdict(False, "replacement_too_large", (2, 3)),
            id="too-many-lines",
        ),
        pytest.param(
            {"suggested_fix_code": "    " + "x" * 8000},
            (),
            (2, 3),
            gate.FixVerdict(False, "replacement_too_large", (2, 3)),
            id="too-many-chars",
        ),
        pytest.param(
            {"suggested_fix_code": "\n".join(f"    line{n}" for n in range(100))},
            (),
            (2, 3),
            gate.FixVerdict(True, None, (2, 3)),
            id="line-bound",
        ),
        pytest.param(
            {
                "suggested_fix_code": "\n".join(f"    line{n}" for n in range(100))
                + "\n"
            },
            (),
            (2, 3),
            gate.FixVerdict(True, None, (2, 3)),
            id="line-bound-terminator",
        ),
        pytest.param(
            {
                "suggested_fix_code": "\n".join(f"    line{n}" for n in range(100))
                + "\n\n"
            },
            (),
            (2, 3),
            gate.FixVerdict(False, "replacement_too_large", (2, 3)),
            id="blank-over-line-bound",
        ),
        pytest.param(
            {"suggested_fix_code": "    " + "x" * 7996 + "\n"},
            (),
            (2, 3),
            gate.FixVerdict(True, None, (2, 3)),
            id="char-bound-terminator",
        ),
        pytest.param(
            {},
            ("file",),
            (2, 3),
            gate.FixVerdict(False, "range_not_in_diff", (2, 3)),
            id="missing-file",
        ),
        pytest.param(
            {"suggested_fix_code": "<!-- code-gauntlet-finding-key: forged"},
            (),
            (2, 3),
            gate.FixVerdict(False, "marker_shaped", (2, 3)),
            id="marker-shaped",
        ),
        pytest.param(
            {
                "suggested_fix_code": "<!-- code-gauntlet-finding-key: forged",
                "end_line": 940,
            },
            (),
            (2, 2),
            gate.FixVerdict(False, "marker_shaped", (2, 2)),
            id="marker-before-range",
        ),
    ],
)
def test_fix_case(overrides, absent, apply_range, expected):
    finding = {
        "file": "foo.py",
        "line": 2,
        "end_line": 3,
        "suggested_fix_code": "    return 2\n    # done",
    } | overrides
    for field in absent:
        del finding[field]
    assert (
        gate.evaluate_fix(
            finding,
            apply_range=apply_range,
            facts=parse_diff(DIFF, policy="git-prefixed"),
        )
        == expected
    )


@pytest.mark.parametrize(
    "finding, apply_range, expected",
    [
        pytest.param({}, (1, 1), gate.FixVerdict(True, None, (1, 1)), id="absent-fix"),
        pytest.param(
            {"suggested_fix_code": 42},
            (1, 1),
            gate.FixVerdict(False, "non_string", (1, 1)),
            id="non-string",
        ),
        pytest.param(
            {"suggested_fix_code": ""},
            (1, 1),
            gate.FixVerdict(False, "empty", (1, 1)),
            id="empty",
        ),
        pytest.param(
            {"suggested_fix_code": "replacement", "line": 1},
            (1, 1),
            gate.FixVerdict(False, "missing_end_line", (1, 1)),
            id="missing-end",
        ),
        pytest.param(
            {"suggested_fix_code": "replacement", "line": True, "end_line": 1},
            (1, 1),
            gate.FixVerdict(False, "invalid_range", (1, 1)),
            id="invalid-range",
        ),
        pytest.param(
            {
                "suggested_fix_code": "replacement",
                "file": "x",
                "line": 1,
                "end_line": 1,
            },
            (1, 1),
            gate.FixVerdict(False, "no_diff_oracle", (1, 1)),
            id="absent-oracle",
        ),
    ],
)
def test_failure_order(finding, apply_range, expected):
    assert gate.evaluate_fix(finding, apply_range=apply_range, facts=None) == expected


@pytest.mark.parametrize(
    "finding, facts, apply_range, expected",
    [
        pytest.param(
            {
                "file": "foo.py",
                "line": 2,
                "end_line": 3,
                "suggested_fix_code": "    return 1\n    # tail",
            },
            diff_facts(
                {("foo.py", 2): None, ("foo.py", 3): None},
                line_texts={("foo.py", 2): "    return 1"},
            ),
            (2, 3),
            gate.FixVerdict(False, "no_diff_oracle", (2, 3)),
            id="partial",
        ),
        pytest.param(
            {"file": "foo.py", "line": 2, "end_line": 3, "suggested_fix_code": "fixed"},
            None,
            (2, 3),
            gate.FixVerdict(False, "no_diff_oracle", (2, 3)),
            id="absent",
        ),
        pytest.param(
            {
                "file": "b/x",
                "line": 1,
                "end_line": 1,
                "suggested_fix_code": "replacement",
            },
            None,
            None,
            gate.FixVerdict(False, "no_diff_oracle", None),
            id="absent-no-anchor",
        ),
        pytest.param(
            {
                "file": "b/x",
                "line": 1,
                "end_line": 1,
                "suggested_fix_code": "replacement",
            },
            None,
            (1, 2),
            gate.FixVerdict(False, "no_diff_oracle", (1, 2)),
            id="absent-mismatched-anchor",
        ),
        pytest.param(
            {
                "file": "b/x",
                "line": 1,
                "end_line": 1,
                "suggested_fix_code": "replacement",
            },
            diff_facts({("b/x", 2): 2, ("x", 2): 2}),
            (1, 1),
            gate.FixVerdict(False, "no_diff_oracle", (1, 1)),
            id="ambiguous-off-diff",
        ),
        pytest.param(
            {
                "file": "b/x.py",
                "line": 10,
                "end_line": 10,
                "suggested_fix_code": "changed",
            },
            diff_facts(
                {("b/x.py", 10): 10, ("x.py", 10): 10},
                line_texts={("b/x.py", 10): "subline", ("x.py", 10): "topline"},
            ),
            (10, 10),
            gate.FixVerdict(False, "no_diff_oracle", (10, 10)),
            id="collision-hit",
        ),
        pytest.param(
            {
                "file": "b/x.py",
                "line": 10,
                "end_line": 10,
                "suggested_fix_code": "changed",
            },
            diff_facts(
                {("b/x.py", 9): 9, ("x.py", 10): 10},
                line_texts={("b/x.py", 9): "subline", ("x.py", 10): "topline"},
            ),
            (10, 10),
            gate.FixVerdict(False, "no_diff_oracle", (10, 10)),
            id="collision-miss",
        ),
        pytest.param(
            {
                "file": "b/x.py",
                "line": 10,
                "end_line": 10,
                "suggested_fix_code": "changed",
            },
            diff_facts({("x.py", 10): 10}, line_texts={("x.py", 10): "topline"}),
            (10, 10),
            gate.FixVerdict(True, None, (10, 10)),
            id="residual-recall",
        ),
        pytest.param(
            {
                "file": "foo.py",
                "line": 2,
                "end_line": 3,
                "suggested_fix_code": "    return 1\n    # tail",
            },
            parse_diff(
                DIFF.replace("+    return 1\n", "+    return 1\r\n").replace(
                    "+    # tail\n", "+    # tail\r\n"
                ),
                policy="git-prefixed",
            ),
            (2, 3),
            gate.FixVerdict(False, "no_op_replacement", (2, 3)),
            id="crlf-oracle-no-op",
        ),
        pytest.param(
            {
                "file": "t.py",
                "line": 2,
                "end_line": 2,
                "suggested_fix_code": "    return 2",
            },
            parse_diff(
                "diff --git a/t.py b/t.py\n--- a/t.py\n+++ b/t.py\n@@ -1,1 +1,2 @@\n def f():\n+\treturn 1\n",
                policy="git-prefixed",
            ),
            (2, 2),
            gate.FixVerdict(False, "indentation_mismatch", (2, 2)),
            id="tab-to-space",
        ),
        pytest.param(
            {
                "file": "foo.py",
                "line": 2,
                "end_line": 3,
                "suggested_fix_code": "\tfixed2\n\tfixed3",
            },
            parse_diff(
                "--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,3 @@\n original\n+line2\n+line3\n",
                policy="git-prefixed",
            ),
            (2, 3),
            gate.FixVerdict(True, None, (2, 3)),
            id="unindented",
        ),
    ],
)
def test_oracle_case(finding, facts, apply_range, expected):
    assert gate.evaluate_fix(finding, apply_range=apply_range, facts=facts) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param(None, None, id="none"),
        pytest.param(42, "42", id="non-string-render"),
        pytest.param(" \n ", None, id="whitespace"),
        pytest.param("patch\n", "patch", id="one-lf"),
        pytest.param("\npatch", "\npatch", id="leading-blank"),
        pytest.param("patch\n\n", "patch\n", id="trailing-blank"),
    ],
)
def test_normalization(value, expected):
    assert gate.fix_code_text(value) == expected


@pytest.mark.parametrize(
    "end_line, anchor, mismatch, demote, expected",
    [
        pytest.param(
            3,
            (2, 3),
            "anchor_mismatch",
            None,
            gate.FixVerdict(True, None, (2, 3)),
            id="no-demotion",
        ),
        pytest.param(
            3,
            (2, 3),
            "span_exceeds_platform_cap",
            "overlaps_kept_fence",
            gate.FixVerdict(False, "overlaps_kept_fence", (2, 3)),
            id="passed-then-demoted",
        ),
        pytest.param(
            None,
            (2, 3),
            "anchor_mismatch",
            "overlaps_kept_fence",
            gate.FixVerdict(False, "missing_end_line", (2, 3)),
            id="per-fix-wins",
        ),
        pytest.param(
            3,
            (2, 2),
            "span_exceeds_platform_cap",
            None,
            gate.FixVerdict(False, "span_exceeds_platform_cap", (2, 2)),
            id="cap-rename",
        ),
    ],
)
def test_demotion_precedence(end_line, anchor, mismatch, demote, expected):
    finding = {
        "file": "foo.py",
        "line": 2,
        "end_line": end_line,
        "suggested_fix_code": "fixed",
    }
    assert (
        gate.evaluate_fix(
            finding,
            apply_range=anchor,
            facts=parse_diff(DIFF, policy="git-prefixed"),
            mismatch_reason=mismatch,
            demote_reason=demote,
        )
        == expected
    )


@pytest.mark.parametrize(
    "line, end_line, anchor, expected",
    [
        pytest.param(2, 2, 2, gate.ApplySite((2, 2), (0, 0)), id="single-line"),
        pytest.param(2, 4, 2, gate.ApplySite((2, 4), (0, 2)), id="below-anchor"),
        pytest.param(2, 4, 4, gate.ApplySite((2, 4), (2, 0)), id="above-anchor"),
        pytest.param(2, 4, 1, gate.ApplySite((1, 1)), id="anchor-before"),
        pytest.param(2, 4, 5, gate.ApplySite((5, 5)), id="anchor-after"),
        pytest.param(2, 102, 2, gate.ApplySite((2, 102), (0, 100)), id="cap-inclusive"),
        pytest.param(
            2,
            103,
            2,
            gate.ApplySite((2, 2), cap_exceeded=True),
            id="below-cap-exceeded",
        ),
        pytest.param(
            2,
            103,
            103,
            gate.ApplySite((103, 103), cap_exceeded=True),
            id="above-cap-exceeded",
        ),
        pytest.param(2, None, 2, gate.ApplySite((2, 2)), id="missing-bound"),
        pytest.param(2, "3", 2, gate.ApplySite((2, 2)), id="string-bound"),
        pytest.param(2, 3.0, 2, gate.ApplySite((2, 2)), id="float-bound"),
        pytest.param(2, True, 2, gate.ApplySite((2, 2)), id="boolean-bound"),
    ],
)
def test_gitlab_site(line, end_line, anchor, expected):
    assert (
        gate.gitlab_apply_range({"line": line, "end_line": end_line}, anchor)
        == expected
    )


@pytest.mark.parametrize(
    "line, end_line, expected",
    [
        pytest.param(2, 3, gate.ApplySite((2, 3), multiline=True), id="multiline"),
        pytest.param(2, None, gate.ApplySite((2, 2)), id="missing-end"),
        pytest.param(2, 2, gate.ApplySite((2, 2)), id="same-line"),
        pytest.param(2, 940, gate.ApplySite((2, 2)), id="off-diff-end"),
        pytest.param(1, True, gate.ApplySite((1, 1)), id="boolean-end"),
        pytest.param(2, 3.0, gate.ApplySite((2, 2)), id="float-end-geometry"),
    ],
)
def test_github_site(line, end_line, expected):
    assert (
        gate.github_apply_range(
            parse_diff(DIFF, policy="git-prefixed"),
            "foo.py",
            line,
            end_line,
        )
        == expected
    )


@pytest.mark.parametrize(
    "records, expected",
    [
        pytest.param([(0, "foo.py", (2, 4)), (1, "foo.py", (3, 5))], {1}, id="overlap"),
        pytest.param(
            [(0, "foo.py", (5, 5)), (1, "foo.py", (5, 5))],
            {1},
            id="identical-single-line",
        ),
        pytest.param(
            [(0, "foo.py", (1, 3)), (1, "foo.py", (4, 6))], set(), id="adjacent"
        ),
        pytest.param(
            [(0, "foo.py", (1, 5)), (1, "foo.py", (4, 8)), (2, "foo.py", (7, 10))],
            {1},
            id="loser-claims-nothing",
        ),
        pytest.param(
            [(0, "foo.py", (2, 4)), (1, "bar.py", (2, 4))], set(), id="different-paths"
        ),
        pytest.param([], set(), id="empty"),
        pytest.param(
            [(0, "foo.py", (1, 10)), (1, "foo.py", (4, 6))], {1}, id="containment"
        ),
    ],
)
def test_overlap_case(records, expected):
    assert (
        gate.overlap_losers(gate.OverlapCandidate(*row) for row in records) == expected
    )


@pytest.mark.parametrize(
    "label, expected",
    [
        pytest.param(
            "suggested-fix", "suggested-fix downgraded: f.py:3 (empty)", id="delivery"
        ),
        pytest.param(
            "report-patch", "report-patch downgraded: f.py:3 (empty)", id="report"
        ),
    ],
)
def test_warning_label(label, expected):
    assert (
        gate.format_fix_warning({"file": "f.py", "line": 3}, "empty", label=label)
        == expected
    )


def test_gate_purity(capsys):
    finding = {
        "file": "foo.py",
        "line": 2,
        "end_line": 3,
        "suggested_fix_code": "fixed",
        "future": {"retained": [1]},
    }
    facts = parse_diff(DIFF, policy="git-prefixed")
    before = copy.deepcopy((finding, facts))
    assert gate.evaluate_fix(
        finding, apply_range=(2, 3), facts=facts
    ) == gate.FixVerdict(True, None, (2, 3))
    assert gate.evaluate_fix(
        {"suggested_fix_code": 42}, apply_range=None, facts=None
    ) == gate.FixVerdict(False, "non_string", None)
    assert gate.evaluate_fix(
        finding, apply_range=(2, 3), facts=facts
    ) == gate.FixVerdict(True, None, (2, 3))
    assert (finding, facts) == before
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
