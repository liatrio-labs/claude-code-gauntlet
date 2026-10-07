"""Pure gate verdicts, platform sites, and first-wins intervals."""

import copy

import pytest
from gauntlet.delivery import gate
from gauntlet.diff import parse_diff

from tests.support.delivery import finding, fix_finding
from tests.support.diff import diff_facts

DIFF = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,3 @@\n def f():\n+    return 1\n+    # tail\n"
FACTS = parse_diff(DIFF, policy="git-prefixed")

FIX_CASE_CASES = {
    "absent-fix": (
        fix_finding(file=[], line=[], omit=("suggested_fix_code",)),
        FACTS,
        (2, 3),
        {},
        (True, None, (2, 3)),
    ),
    "multiline": (fix_finding(), FACTS, (2, 3), {}, (True, None, (2, 3))),
    "single-line": (
        finding(suggested_fix_code="    return 2", end_line=2),
        FACTS,
        (2, 2),
        {},
        (True, None, (2, 2)),
    ),
    "reindentation": (
        finding(suggested_fix_code="        return 2\n        # done"),
        FACTS,
        (2, 3),
        {},
        (True, None, (2, 3)),
    ),
    "integer-subclass": (
        fix_finding(line=type("Line", (int,), {})(2)),
        FACTS,
        (2, 3),
        {},
        (True, None, (2, 3)),
    ),
    "non-string": (
        finding(suggested_fix_code=42),
        FACTS,
        (2, 3),
        {},
        (False, "non_string", (2, 3)),
    ),
    "null": (
        finding(suggested_fix_code=None),
        FACTS,
        (2, 3),
        {},
        (False, "non_string", (2, 3)),
    ),
    "empty": (
        finding(suggested_fix_code="  \n  "),
        FACTS,
        (2, 3),
        {},
        (False, "empty", (2, 3)),
    ),
    "interior-cr": (
        finding(suggested_fix_code="foo\rbar\rbaz"),
        FACTS,
        (2, 3),
        {},
        (False, "carriage_return", (2, 3)),
    ),
    "crlf-fix": (
        finding(suggested_fix_code="    return 2\r\n    # done\r\n"),
        FACTS,
        (2, 3),
        {},
        (False, "carriage_return", (2, 3)),
    ),
    "redacted": (
        finding(suggested_fix_code="    token = 'ghp_" + "A" * 24 + "'"),
        FACTS,
        (2, 3),
        {},
        (False, "redacted", (2, 3)),
    ),
    "missing-end": (
        fix_finding(omit=("end_line",)),
        FACTS,
        (2, 2),
        {},
        (False, "missing_end_line", (2, 2)),
    ),
    "null-end": (
        fix_finding(end_line=None),
        FACTS,
        (2, 2),
        {},
        (False, "missing_end_line", (2, 2)),
    ),
    "reversed-range": (
        fix_finding(line=3, end_line=2),
        FACTS,
        (3, 2),
        {},
        (False, "invalid_range", (3, 2)),
    ),
    "float-line": (
        fix_finding(line=2.0),
        FACTS,
        (2.0, 3),
        {},
        (False, "invalid_range", None),
    ),
    "boolean-line": (
        fix_finding(line=True, end_line=True),
        FACTS,
        (True, True),
        {},
        (False, "invalid_range", None),
    ),
    "nonpositive-line": (
        fix_finding(line=0, end_line=1),
        FACTS,
        (0, 1),
        {},
        (False, "invalid_range", (0, 1)),
    ),
    "range-not-in-diff": (
        fix_finding(end_line=940),
        FACTS,
        (2, 940),
        {},
        (False, "range_not_in_diff", (2, 940)),
    ),
    "unknown-path": (
        fix_finding(file="other.py"),
        FACTS,
        (2, 3),
        {},
        (False, "range_not_in_diff", (2, 3)),
    ),
    "narrow-anchor": (
        fix_finding(),
        FACTS,
        (2, 2),
        {},
        (False, "anchor_mismatch", (2, 2)),
    ),
    "no-anchor": (fix_finding(), FACTS, None, {}, (False, "anchor_mismatch", None)),
    "no-op": (
        finding(suggested_fix_code="    return 1\n    # tail"),
        FACTS,
        (2, 3),
        {},
        (False, "no_op_replacement", (2, 3)),
    ),
    "space-to-tab": (
        finding(suggested_fix_code="\treturn 2\n\t# done"),
        FACTS,
        (2, 3),
        {},
        (False, "indentation_mismatch", (2, 3)),
    ),
    "leading-blank": (
        finding(suggested_fix_code="\n    return 1\n    # tail"),
        FACTS,
        (2, 3),
        {},
        (True, None, (2, 3)),
    ),
    "trailing-blank": (
        finding(suggested_fix_code="    return 1\n    # tail\n\n"),
        FACTS,
        (2, 3),
        {},
        (True, None, (2, 3)),
    ),
    "one-lf-no-op": (
        finding(suggested_fix_code="    return 1\n    # tail\n"),
        FACTS,
        (2, 3),
        {},
        (False, "no_op_replacement", (2, 3)),
    ),
    "too-many-lines": (
        finding(suggested_fix_code="\n".join(f"    line{n}" for n in range(101))),
        FACTS,
        (2, 3),
        {},
        (False, "replacement_too_large", (2, 3)),
    ),
    "too-many-chars": (
        finding(suggested_fix_code="    " + "x" * 8000),
        FACTS,
        (2, 3),
        {},
        (False, "replacement_too_large", (2, 3)),
    ),
    "line-bound": (
        finding(suggested_fix_code="\n".join(f"    line{n}" for n in range(100))),
        FACTS,
        (2, 3),
        {},
        (True, None, (2, 3)),
    ),
    "line-bound-terminator": (
        finding(
            suggested_fix_code="\n".join(f"    line{n}" for n in range(100)) + "\n"
        ),
        FACTS,
        (2, 3),
        {},
        (True, None, (2, 3)),
    ),
    "blank-over-line-bound": (
        finding(
            suggested_fix_code="\n".join(f"    line{n}" for n in range(100)) + "\n\n"
        ),
        FACTS,
        (2, 3),
        {},
        (False, "replacement_too_large", (2, 3)),
    ),
    "char-bound-terminator": (
        finding(suggested_fix_code="    " + "x" * 7996 + "\n"),
        FACTS,
        (2, 3),
        {},
        (True, None, (2, 3)),
    ),
    "missing-file": (
        fix_finding(omit=("file",)),
        FACTS,
        (2, 3),
        {},
        (False, "range_not_in_diff", (2, 3)),
    ),
    "marker-shaped": (
        finding(suggested_fix_code="<!-- code-gauntlet-finding-key: forged"),
        FACTS,
        (2, 3),
        {},
        (False, "marker_shaped", (2, 3)),
    ),
    "marker-before-range": (
        finding(
            suggested_fix_code="<!-- code-gauntlet-finding-key: forged", end_line=940
        ),
        FACTS,
        (2, 2),
        {},
        (False, "marker_shaped", (2, 2)),
    ),
    "order-absent-fix": (
        fix_finding(omit=("file", "line", "end_line", "suggested_fix_code")),
        None,
        (1, 1),
        {},
        (True, None, (1, 1)),
    ),
    "order-non-string": (
        fix_finding(suggested_fix_code=42, omit=("file", "line", "end_line")),
        None,
        (1, 1),
        {},
        (False, "non_string", (1, 1)),
    ),
    "order-empty": (
        fix_finding(suggested_fix_code="", omit=("file", "line", "end_line")),
        None,
        (1, 1),
        {},
        (False, "empty", (1, 1)),
    ),
    "order-missing-end": (
        fix_finding(
            suggested_fix_code="replacement", line=1, omit=("file", "end_line")
        ),
        None,
        (1, 1),
        {},
        (False, "missing_end_line", (1, 1)),
    ),
    "order-invalid-range": (
        fix_finding(
            suggested_fix_code="replacement", line=True, end_line=1, omit=("file",)
        ),
        None,
        (1, 1),
        {},
        (False, "invalid_range", (1, 1)),
    ),
    "order-absent-oracle": (
        fix_finding(suggested_fix_code="replacement", file="x", line=1, end_line=1),
        None,
        (1, 1),
        {},
        (False, "no_diff_oracle", (1, 1)),
    ),
    "oracle-partial": (
        fix_finding(suggested_fix_code="    return 1\n    # tail"),
        diff_facts(
            {("foo.py", 2): None, ("foo.py", 3): None},
            line_texts={("foo.py", 2): "    return 1"},
        ),
        (2, 3),
        {},
        (False, "no_diff_oracle", (2, 3)),
    ),
    "oracle-absent": (
        fix_finding(suggested_fix_code="fixed"),
        None,
        (2, 3),
        {},
        (False, "no_diff_oracle", (2, 3)),
    ),
    "oracle-absent-no-anchor": (
        fix_finding(file="b/x", line=1, end_line=1, suggested_fix_code="replacement"),
        None,
        None,
        {},
        (False, "no_diff_oracle", None),
    ),
    "oracle-absent-mismatched-anchor": (
        fix_finding(file="b/x", line=1, end_line=1, suggested_fix_code="replacement"),
        None,
        (1, 2),
        {},
        (False, "no_diff_oracle", (1, 2)),
    ),
    "oracle-ambiguous-off-diff": (
        fix_finding(file="b/x", line=1, end_line=1, suggested_fix_code="replacement"),
        diff_facts({("b/x", 2): 2, ("x", 2): 2}),
        (1, 1),
        {},
        (False, "no_diff_oracle", (1, 1)),
    ),
    "oracle-collision-hit": (
        fix_finding(file="b/x.py", line=10, end_line=10, suggested_fix_code="changed"),
        diff_facts(
            {("b/x.py", 10): 10, ("x.py", 10): 10},
            line_texts={("b/x.py", 10): "subline", ("x.py", 10): "topline"},
        ),
        (10, 10),
        {},
        (False, "no_diff_oracle", (10, 10)),
    ),
    "oracle-collision-miss": (
        fix_finding(file="b/x.py", line=10, end_line=10, suggested_fix_code="changed"),
        diff_facts(
            {("b/x.py", 9): 9, ("x.py", 10): 10},
            line_texts={("b/x.py", 9): "subline", ("x.py", 10): "topline"},
        ),
        (10, 10),
        {},
        (False, "no_diff_oracle", (10, 10)),
    ),
    "oracle-residual-recall": (
        fix_finding(file="b/x.py", line=10, end_line=10, suggested_fix_code="changed"),
        diff_facts({("x.py", 10): 10}, line_texts={("x.py", 10): "topline"}),
        (10, 10),
        {},
        (True, None, (10, 10)),
    ),
    "oracle-crlf-oracle-no-op": (
        fix_finding(suggested_fix_code="    return 1\n    # tail"),
        parse_diff(
            DIFF.replace("+    return 1\n", "+    return 1\r\n").replace(
                "+    # tail\n", "+    # tail\r\n"
            ),
            policy="git-prefixed",
        ),
        (2, 3),
        {},
        (False, "no_op_replacement", (2, 3)),
    ),
    "oracle-tab-to-space": (
        fix_finding(file="t.py", end_line=2, suggested_fix_code="    return 2"),
        parse_diff(
            "diff --git a/t.py b/t.py\n--- a/t.py\n+++ b/t.py\n@@ -1,1 +1,2 @@\n def f():\n+\treturn 1\n",
            policy="git-prefixed",
        ),
        (2, 2),
        {},
        (False, "indentation_mismatch", (2, 2)),
    ),
    "oracle-unindented": (
        fix_finding(suggested_fix_code="\tfixed2\n\tfixed3"),
        parse_diff(
            "--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,3 @@\n original\n+line2\n+line3\n",
            policy="git-prefixed",
        ),
        (2, 3),
        {},
        (True, None, (2, 3)),
    ),
    "demote-no-demotion": (
        finding(end_line=3, suggested_fix_code="fixed"),
        FACTS,
        (2, 3),
        {"mismatch_reason": "anchor_mismatch", "demote_reason": None},
        (True, None, (2, 3)),
    ),
    "demote-passed-then-demoted": (
        finding(end_line=3, suggested_fix_code="fixed"),
        FACTS,
        (2, 3),
        {
            "mismatch_reason": "span_exceeds_platform_cap",
            "demote_reason": "overlaps_kept_fence",
        },
        (False, "overlaps_kept_fence", (2, 3)),
    ),
    "demote-per-fix-wins": (
        finding(end_line=None, suggested_fix_code="fixed"),
        FACTS,
        (2, 3),
        {"mismatch_reason": "anchor_mismatch", "demote_reason": "overlaps_kept_fence"},
        (False, "missing_end_line", (2, 3)),
    ),
    "demote-cap-rename": (
        finding(end_line=3, suggested_fix_code="fixed"),
        FACTS,
        (2, 2),
        {"mismatch_reason": "span_exceeds_platform_cap", "demote_reason": None},
        (False, "span_exceeds_platform_cap", (2, 2)),
    ),
}


@pytest.mark.parametrize(
    "finding,facts,site,options,expected", FIX_CASE_CASES.values(), ids=FIX_CASE_CASES
)
def test_fix_case(finding, facts, site, options, expected):
    verdict = gate.evaluate_fix(finding, apply_range=site, facts=facts, **options)
    assert (verdict.keep, verdict.reason, verdict.apply_range) == expected


NORMALIZATION_CASES = {
    "none": (None, None),
    "non-string-render": (42, "42"),
    "whitespace": (" \n ", None),
    "one-lf": ("patch\n", "patch"),
    "leading-blank": ("\npatch", "\npatch"),
    "trailing-blank": ("patch\n\n", "patch\n"),
}


@pytest.mark.parametrize(
    "value, expected", NORMALIZATION_CASES.values(), ids=NORMALIZATION_CASES
)
def test_normalization(value, expected):
    assert gate.fix_code_text(value) == expected


GITLAB_SITE_CASES = {
    "single-line": (2, 2, 2, gate.ApplySite((2, 2), (0, 0))),
    "below-anchor": (2, 4, 2, gate.ApplySite((2, 4), (0, 2))),
    "above-anchor": (2, 4, 4, gate.ApplySite((2, 4), (2, 0))),
    "anchor-before": (2, 4, 1, gate.ApplySite((1, 1))),
    "anchor-after": (2, 4, 5, gate.ApplySite((5, 5))),
    "cap-inclusive": (2, 102, 2, gate.ApplySite((2, 102), (0, 100))),
    "below-cap-exceeded": (2, 103, 2, gate.ApplySite((2, 2), cap_exceeded=True)),
    "above-cap-exceeded": (2, 103, 103, gate.ApplySite((103, 103), cap_exceeded=True)),
    "missing-bound": (2, None, 2, gate.ApplySite((2, 2))),
    "string-bound": (2, "3", 2, gate.ApplySite((2, 2))),
    "float-bound": (2, 3.0, 2, gate.ApplySite((2, 2))),
    "boolean-bound": (2, True, 2, gate.ApplySite((2, 2))),
}


@pytest.mark.parametrize(
    "line, end_line, anchor, expected",
    GITLAB_SITE_CASES.values(),
    ids=GITLAB_SITE_CASES,
)
def test_gitlab_site(line, end_line, anchor, expected):
    assert (
        gate.gitlab_apply_range({"line": line, "end_line": end_line}, anchor)
        == expected
    )


GITHUB_SITE_CASES = {
    "multiline": (2, 3, gate.ApplySite((2, 3), multiline=True)),
    "missing-end": (2, None, gate.ApplySite((2, 2))),
    "same-line": (2, 2, gate.ApplySite((2, 2))),
    "off-diff-end": (2, 940, gate.ApplySite((2, 2))),
    "boolean-end": (1, True, gate.ApplySite((1, 1))),
    "float-end-geometry": (2, 3.0, gate.ApplySite((2, 2))),
}


@pytest.mark.parametrize(
    "line, end_line, expected", GITHUB_SITE_CASES.values(), ids=GITHUB_SITE_CASES
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


OVERLAP_CASE_CASES = {
    "overlap": ([(0, "foo.py", (2, 4)), (1, "foo.py", (3, 5))], {1}),
    "identical-single-line": ([(0, "foo.py", (5, 5)), (1, "foo.py", (5, 5))], {1}),
    "adjacent": ([(0, "foo.py", (1, 3)), (1, "foo.py", (4, 6))], set()),
    "loser-claims-nothing": (
        [(0, "foo.py", (1, 5)), (1, "foo.py", (4, 8)), (2, "foo.py", (7, 10))],
        {1},
    ),
    "different-paths": ([(0, "foo.py", (2, 4)), (1, "bar.py", (2, 4))], set()),
    "empty": ([], set()),
    "containment": ([(0, "foo.py", (1, 10)), (1, "foo.py", (4, 6))], {1}),
}


@pytest.mark.parametrize(
    "records, expected", OVERLAP_CASE_CASES.values(), ids=OVERLAP_CASE_CASES
)
def test_overlap_case(records, expected):
    assert (
        gate.overlap_losers(gate.OverlapCandidate(*row) for row in records) == expected
    )


WARNING_LABEL_CASES = {
    "delivery": ("suggested-fix", "suggested-fix downgraded: f.py:3 (empty)"),
    "report": ("report-patch", "report-patch downgraded: f.py:3 (empty)"),
}


@pytest.mark.parametrize(
    "label, expected", WARNING_LABEL_CASES.values(), ids=WARNING_LABEL_CASES
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
