"""Pure gate verdicts, platform sites, and first-wins intervals."""

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
    "integer-subclass": (
        fix_finding(line=type("Line", (int,), {})(2)),
        FACTS,
        (2, 3),
        {},
        (True, None, (2, 3)),
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
    "missing-end": (
        fix_finding(omit=("end_line",)),
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
    "no-anchor": (fix_finding(), FACTS, None, {}, (False, "anchor_mismatch", None)),
    "oversized-no-op-order": (
        finding(
            line=1,
            end_line=101,
            suggested_fix_code="x\n" * 100 + "x",
        ),
        diff_facts(
            {("foo.py", line): line for line in range(1, 102)},
            line_texts={("foo.py", line): "x" for line in range(1, 102)},
        ),
        (1, 101),
        {},
        (False, "no_op_replacement", (1, 101)),
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
    "line-bound-terminator": (
        finding(
            suggested_fix_code="\n".join(f"    line{n}" for n in range(100)) + "\n"
        ),
        FACTS,
        (2, 3),
        {},
        (True, None, (2, 3)),
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
    "marker-before-range": (
        finding(
            suggested_fix_code="<!-- code-gauntlet-finding-key: forged", end_line=940
        ),
        FACTS,
        (2, 2),
        {},
        (False, "marker_shaped", (2, 2)),
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
    "order-invalid-range": (
        fix_finding(
            suggested_fix_code="replacement", line=True, end_line=1, omit=("file",)
        ),
        None,
        (1, 1),
        {},
        (False, "invalid_range", (1, 1)),
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
    "oracle-absent-no-anchor": (
        fix_finding(file="b/x", line=1, end_line=1, suggested_fix_code="replacement"),
        None,
        None,
        {},
        (False, "no_diff_oracle", None),
    ),
    "oracle-ambiguous-off-diff": (
        fix_finding(file="b/x", line=1, end_line=1, suggested_fix_code="replacement"),
        diff_facts({("b/x", 2): 2, ("x", 2): 2}),
        (1, 1),
        {},
        (False, "no_diff_oracle", (1, 1)),
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
    "anchor-mismatch-renamed-to-platform-cap-reason": (
        finding(end_line=3, suggested_fix_code="fixed"),
        FACTS,
        (2, 2),
        {"mismatch_reason": "span_exceeds_platform_cap"},
        (False, "span_exceeds_platform_cap", (2, 2)),
    ),
    "oracle-x-suffix-is-content": (
        finding(suggested_fix_code="    return 1\n    # tail"),
        parse_diff(DIFF.replace("# tail", "# tailX"), policy="git-prefixed"),
        (2, 3),
        {},
        (True, None, (2, 3)),
    ),
    "oracle-letter-led-space-to-tab": (
        fix_finding(file="t.py", end_line=2, suggested_fix_code="\tX = 2"),
        parse_diff(
            "diff --git a/t.py b/t.py\n--- a/t.py\n+++ b/t.py\n@@ -1,1 +1,2 @@\n def f():\n+    X = 1\n",
            policy="git-prefixed",
        ),
        (2, 2),
        {},
        (False, "indentation_mismatch", (2, 2)),
    ),
    "missing-file-placeholder-path": (
        fix_finding(omit=("file",)),
        diff_facts(
            {("?", 2): 2, ("?", 3): 3},
            line_texts={("?", 2): "    return 1", ("?", 3): "    # tail"},
        ),
        (2, 3),
        {},
        (True, None, (2, 3)),
    ),
}


@pytest.mark.parametrize(
    "finding,facts,site,options,expected", FIX_CASE_CASES.values(), ids=FIX_CASE_CASES
)
def test_fix_case(finding, facts, site, options, expected):
    verdict = gate.evaluate_fix(finding, apply_range=site, facts=facts, **options)
    assert (verdict.keep, verdict.reason, verdict.apply_range) == expected


GITLAB_SITE_CASES = {
    "anchor-before": (2, 4, 1, gate.GitLabApplySite((1, 1))),
    "anchor-after": (2, 4, 5, gate.GitLabApplySite((5, 5))),
    "cap-inclusive": (2, 102, 2, gate.GitLabApplySite((2, 102), (0, 100))),
    "above-cap-inclusive": (2, 102, 102, gate.GitLabApplySite((2, 102), (100, 0))),
    "below-cap-exceeded": (2, 103, 2, gate.GitLabApplySite((2, 2), cap_exceeded=True)),
    "above-cap-exceeded": (
        2,
        103,
        103,
        gate.GitLabApplySite((103, 103), cap_exceeded=True),
    ),
    "missing-bound": (2, None, 2, gate.GitLabApplySite((2, 2))),
}


@pytest.mark.parametrize(
    "verdict,expected",
    [
        pytest.param(
            gate.FixVerdict(True, None, (2, 3)),
            (False, "overlaps_kept_fence", (2, 3)),
            id="demote-passed-then-demoted",
        ),
        pytest.param(
            gate.FixVerdict(False, "missing_end_line", (2, 3)),
            (False, "missing_end_line", (2, 3)),
            id="demote-per-fix-wins",
        ),
    ],
)
def test_overlap_demotion(verdict, expected):
    demoted = gate.demote(verdict)
    assert (demoted.keep, demoted.downgrade_reason, demoted.apply_range) == expected


@pytest.mark.parametrize(
    "line, end_line, anchor, expected",
    GITLAB_SITE_CASES.values(),
    ids=GITLAB_SITE_CASES,
)
def test_gitlab_site(line, end_line, anchor, expected):
    assert gate.gitlab_apply_range(line, end_line, anchor) == expected


GITHUB_SITE_CASES = {
    "multiline": (2, 3, gate.GitHubApplySite((2, 3), multiline=True)),
    "off-diff-end": (2, 940, gate.GitHubApplySite((2, 2))),
    "reversed-end": (2, 1, gate.GitHubApplySite((2, 2))),
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
    "identical-single-line": ([(0, "foo.py", (5, 5)), (1, "foo.py", (5, 5))], {1}),
    "adjacent": ([(0, "foo.py", (1, 3)), (1, "foo.py", (4, 6))], set()),
    "loser-claims-nothing": (
        [(0, "foo.py", (1, 5)), (1, "foo.py", (4, 8)), (2, "foo.py", (7, 10))],
        {1},
    ),
    "different-paths": ([(0, "foo.py", (2, 4)), (1, "bar.py", (2, 4))], set()),
    "containment": ([(0, "foo.py", (1, 10)), (1, "foo.py", (4, 6))], {1}),
    "later-starts-earlier": ([(0, "foo.py", (3, 8)), (1, "foo.py", (1, 5))], {1}),
}


@pytest.mark.parametrize(
    "records, expected", OVERLAP_CASE_CASES.values(), ids=OVERLAP_CASE_CASES
)
def test_overlap_case(records, expected):
    assert (
        gate.overlap_losers(gate.OverlapCandidate(*row) for row in records) == expected
    )


WARNING_LABEL_CASES = {
    "delivery": (
        {"file": "f.py", "line": 3},
        "suggested-fix",
        "suggested-fix downgraded: f.py:3 (empty)",
    ),
    "missing-file": (
        {"line": 3},
        "report-patch",
        "report-patch downgraded: ?:3 (empty)",
    ),
}


@pytest.mark.parametrize(
    "finding,label,expected", WARNING_LABEL_CASES.values(), ids=WARNING_LABEL_CASES
)
def test_warning_label(finding, label, expected):
    assert gate.format_fix_warning(finding, "empty", label=label) == expected
