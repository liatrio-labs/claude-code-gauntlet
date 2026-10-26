"""Pure gate verdicts, platform sites, and first-wins intervals."""

from collections.abc import Mapping
from dataclasses import dataclass

import pytest
from gauntlet.delivery import gate
from gauntlet.diff import DiffFacts, parse_diff

from tests.support.delivery import finding, fix_finding
from tests.support.diff import diff_facts

DIFF = (
    "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,"
    "1 +1,3 @@\n def f():\n+    return 1\n+    # tail\n"
)
FACTS = parse_diff(DIFF, policy="git-prefixed")


@dataclass(frozen=True, slots=True)
class FixCase:
    finding: Mapping[str, object]
    reason: gate.FixReason | None = None
    facts: DiffFacts | None = FACTS
    site: gate.ApplyRange | None = (2, 3)
    mismatch_reason: gate.FixReason = "anchor_mismatch"


FIX_CASES = {
    "absent-fix": FixCase(fix_finding(omit=("suggested_fix_code",))),
    "empty": FixCase(finding(suggested_fix_code="  \n  "), "empty"),
    "interior-cr": FixCase(
        finding(suggested_fix_code="foo\rbar\rbaz"), "carriage_return"
    ),
    "missing-end": FixCase(
        fix_finding(omit=("end_line",)), "missing_end_line", site=(2, 2)
    ),
    "range-not-in-diff": FixCase(
        fix_finding(claimed_line_end=940), "anchor_mismatch", site=(2, 3)
    ),
    "no-anchor": FixCase(fix_finding(), "anchor_mismatch", site=None),
    "space-to-tab": FixCase(
        finding(suggested_fix_code="\treturn 2\n\t# done"), "indentation_mismatch"
    ),
    "leading-blank": FixCase(finding(suggested_fix_code="\n    return 1\n    # tail")),
    "too-many-lines": FixCase(
        finding(suggested_fix_code="\n".join(f"    line{n}" for n in range(101))),
        "replacement_too_large",
    ),
    "too-many-chars": FixCase(
        finding(suggested_fix_code="    " + "x" * 8000), "replacement_too_large"
    ),
    "line-bound-terminator": FixCase(
        finding(suggested_fix_code="\n".join(f"    line{n}" for n in range(100)) + "\n")
    ),
    "char-bound-terminator": FixCase(
        finding(suggested_fix_code="    " + "x" * 7996 + "\n")
    ),
    "marker-before-range": FixCase(
        finding(
            suggested_fix_code="<!-- code-gauntlet-finding-key: forged",
            claimed_line_end=940,
        ),
        "marker_shaped",
        site=(2, 2),
    ),
    "oracle-partial": FixCase(
        fix_finding(suggested_fix_code="    return 1\n    # tail"),
        "no_diff_oracle",
        facts=diff_facts(
            {("foo.py", 2): None, ("foo.py", 3): None},
            line_texts={("foo.py", 2): "    return 1"},
        ),
    ),
    "oracle-absent-no-anchor": FixCase(
        fix_finding(file="b/x", line=1, end_line=1, suggested_fix_code="replacement"),
        "no_diff_oracle",
        facts=None,
        site=None,
    ),
    "oracle-collision-miss": FixCase(
        fix_finding(file="b/x.py", line=10, end_line=10, suggested_fix_code="changed"),
        "no_diff_oracle",
        facts=diff_facts(
            {("b/x.py", 9): 9, ("x.py", 10): 10},
            line_texts={("b/x.py", 9): "subline", ("x.py", 10): "topline"},
        ),
        site=(10, 10),
    ),
    "oracle-residual-recall": FixCase(
        fix_finding(
            file="b/x.py",
            line=10,
            end_line=10,
            anchor="residual_source_value",
            suggested_fix_code="changed",
        ),
        facts=diff_facts(
            {("x.py", 10): 10}, line_texts={("x.py", 10): "residual_source_value"}
        ),
        site=(10, 10),
    ),
    "oracle-crlf-oracle-no-op": FixCase(
        fix_finding(suggested_fix_code="    return 1\n    # tail"),
        "no_op_replacement",
        facts=parse_diff(
            DIFF.replace("+    return 1\n", "+    return 1\r\n").replace(
                "+    # tail\n", "+    # tail\r\n"
            ),
            policy="git-prefixed",
        ),
    ),
    "oracle-unindented": FixCase(
        fix_finding(anchor="line2\nline3", suggested_fix_code="\tfixed2\n\tfixed3"),
        facts=parse_diff(
            "--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,3 @@\n original\n+line2\n+line3\n",
            policy="git-prefixed",
        ),
    ),
    "anchor-mismatch-renamed-to-platform-cap-reason": FixCase(
        finding(end_line=3, suggested_fix_code="fixed"),
        "span_exceeds_platform_cap",
        site=(2, 2),
        mismatch_reason="span_exceeds_platform_cap",
    ),
    "held-span-before-platform-cap": FixCase(
        finding(claimed_line_end=5, suggested_fix_code="fixed"),
        "anchor_mismatch",
        site=(2, 2),
        mismatch_reason="span_exceeds_platform_cap",
    ),
    "redacted": FixCase(finding(suggested_fix_code="ghp_" + "A" * 36), "redacted"),
}


@pytest.mark.parametrize("case", FIX_CASES.values(), ids=FIX_CASES)
def test_fix_case(case: FixCase) -> None:
    verdict = gate.evaluate_fix(
        case.finding,
        apply_range=case.site,
        facts=case.facts,
        mismatch_reason=case.mismatch_reason,
    )
    assert (verdict.keep, verdict.reason, verdict.apply_range) == (
        case.reason is None,
        case.reason,
        case.site,
    )


GITLAB_SITE_CASES = {
    "anchor-before": (2, 4, 1, gate.GitLabApplySite((1, 1))),
    "cap-inclusive": (2, 102, 2, gate.GitLabApplySite((2, 102), (0, 100))),
    "below-cap-exceeded": (2, 103, 2, gate.GitLabApplySite((2, 2), cap_exceeded=True)),
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
    "first-wins": (
        [
            (0, "foo.py", (1, 5)),
            (1, "foo.py", (5, 8)),
            (2, "foo.py", (7, 10)),
            (3, "bar.py", (1, 5)),
        ],
        {1},
    ),
}


@pytest.mark.parametrize(
    "records, expected", OVERLAP_CASE_CASES.values(), ids=OVERLAP_CASE_CASES
)
def test_overlap_priority(records, expected):
    assert (
        gate.overlap_losers(gate.OverlapCandidate(*row) for row in records) == expected
    )


WARNING_LABEL_CASES = {
    "delivery": (
        {"file": "f.py", "line": 3},
        "suggested-fix",
        "suggested-fix downgraded: f.py:3 (empty)",
    ),
}


@pytest.mark.parametrize(
    "finding,label,expected", WARNING_LABEL_CASES.values(), ids=WARNING_LABEL_CASES
)
def test_warning_label(finding, label, expected):
    assert gate.format_fix_warning(finding, "empty", label=label) == expected


@pytest.mark.parametrize(
    "changes,site,reason",
    [
        ({}, (2, 3), None),
        ({"anchor": "return 1\n# tail"}, (2, 3), None),
        ({"anchor": "1\n# tail"}, (2, 3), "anchor_text_mismatch"),
        ({"anchor": "return  1\n# tail"}, (2, 3), "anchor_text_mismatch"),
        ({"anchor": "return 1\nwrong tail"}, (2, 3), "anchor_text_mismatch"),
        (
            {"anchor": "return 1\n# tail", "line_end": 4, "claimed_line_end": 4},
            (2, 4),
            "range_not_in_diff",
        ),
        ({"claimed_line_end": 5}, (2, 3), "anchor_mismatch"),
        ({"claimed_line_end": None}, (2, 3), "missing_end_line"),
        ({"claimed_line_end": True}, (2, 3), "invalid_range"),
        ({"claimed_line_start": 2**53}, (2, 3), "invalid_range"),
        ({"anchor": None}, (2, 3), "anchor_text_mismatch"),
        ({"location_status": None}, (2, 3), "location_unconfirmed"),
        ({}, (2, 2), "anchor_mismatch"),
        ({"anchor": "return 1\n# tail\n"}, (2, 3), None),
        ({"anchor": "\nreturn 1\n# tail"}, (2, 3), "anchor_text_mismatch"),
    ],
)
def test_full_anchor_preimage_controls_fence(changes, site, reason):
    member = fix_finding(**changes)
    verdict = gate.evaluate_fix(member, apply_range=site, facts=FACTS)
    assert (verdict.keep, verdict.reason) == (reason is None, reason)


@pytest.mark.parametrize("claimed_end,reason", [(40, None), (43, "anchor_mismatch")])
def test_whole_line_anchor_cannot_shrink_held_fix(claimed_end, reason):
    member = finding(
        line_start=2,
        line_end=2,
        claimed_line_start=40,
        claimed_line_end=claimed_end,
        anchor="original_source_value()",
        suggested_fix_code="changed()",
    )
    facts = parse_diff(
        "--- foo.py\n+++ foo.py\n@@ -1 +2 @@\n-old\n+original_source_value()\n",
        policy="verbatim",
    )
    verdict = gate.evaluate_fix(member, apply_range=(2, 2), facts=facts)
    assert (verdict.keep, verdict.reason) == (reason is None, reason)
