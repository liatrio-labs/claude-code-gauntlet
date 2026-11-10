"""Current PR surfaces use issue counts and reveal only the lead's prose."""

import json
import re
from pathlib import Path
from typing import cast

import pytest
from gauntlet.delivery import compose
from gauntlet.delivery.input import Finding
from gauntlet.text import prepare_summary_title

from tests.support.delivery import finding
from tests.support.pipeline import run_node
from tests.support.posting import invoke_posting, review
from tests.support.pr_surfaces import render_pr_surfaces

ROOT = Path(__file__).resolve().parents[1]


def test_rendered_pr_surfaces_have_no_internal_review_vocabulary():
    actual = render_pr_surfaces()
    vocabulary = json.loads(
        run_node(
            "import { AGENTS, DIMENSIONS, STAGE_DEFAULTS } from './workflows/src/registry.js';"
            "process.stdout.write(JSON.stringify([...AGENTS.map(a=>a.split(':').pop()),"
            "...Object.keys(STAGE_DEFAULTS),...DIMENSIONS.map(d=>d.dimension)]));",
            {},
        ).stdout
    )
    for surface in [actual["summary"], *actual["inline"]]:
        prose = "\n".join(
            line for line in surface.splitlines() if "Code Gauntlet" not in line
        )
        assert not re.search(
            r"(?<![-\w])gauntlet\s|admission|pipeline|corroborat|consolidat|confidence",
            prose,
            re.I,
        )
        assert not any(
            re.search(r"\b" + re.escape(term) + r"\b", prose, re.I)
            for term in vocabulary
        )
        assert "—" not in prose
    legacy = compose.summary_for_delivery(
        compose.summary_body_from_report(actual["report"]),
        cast(list[Finding], actual["findings"]),
    )
    assert legacy == actual["summary_body"]
    assert actual["summary"].startswith(
        "### ⚔️ Code Gauntlet\n\nFound 4 issues in this change: 1 high, 2 medium, 1 low. 1 is a suggested improvement rather than a defect."
    )
    assert actual["summary"].count("- 🟠 [HIGH]") == 1
    assert (
        actual["summary"].count(
            "The default time zone is read before the user's setting loads"
        )
        == 1
    )
    assert "An unconfirmed observation" not in actual["summary"]
    assert "An unconfirmed observation" in actual["report"]
    assert all(row["description"] in actual["report"] for row in actual["findings"])


def test_summary_title_reconciliation_matches_workflow_vectors():
    payload = json.loads(
        (ROOT / "tests/fixtures/cross_runtime/outbound_line.json").read_text(
            encoding="utf-8"
        )
    )
    titles = [row["input"]["findings"][0]["title"] for row in payload["summary_cases"]]
    titles += ["x" * 520, "a\nb", "before &amp;lt; @someone", "[x]: u"]
    expected = json.loads(
        run_node(
            "import { prepareSummaryTitle } from './workflows/src/outboundText.js';"
            "let s='';for await(const c of process.stdin)s+=c;"
            "process.stdout.write(JSON.stringify(JSON.parse(s).map(title=>prepareSummaryTitle(title))));",
            titles,
        ).stdout
    )
    assert [prepare_summary_title(title) for title in titles] == expected


@pytest.mark.parametrize(
    "title", ["An invalid range", "[x]: u", "`x [a]: SENT](a b)`", "x" * 520, "a\nb"]
)
def test_late_anchor_failure_removes_only_its_index_bullet(
    title, tmp_path, monkeypatch, capsys, forge_factory
):
    rows = [
        finding(
            id="A",
            file="foo.py",
            line=2,
            end_line=2,
            title=title,
            body="Unanchored lead detail",
            anchor="wrong_value()",
        ),
        finding(
            id="B",
            file="foo.py",
            line=2,
            end_line=2,
            title="Visible issue",
            body="Inline lead detail",
            anchor="valid_value()",
        ),
    ]
    summary = run_node(
        "import { renderSummaryBody } from './workflows/src/renderReport.js';"
        "let s='';for await(const c of process.stdin)s+=c;"
        "process.stdout.write(renderSummaryBody(JSON.parse(s)));",
        {"findings": rows},
    ).stdout
    run = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        review(findings=rows, review_body=summary),
        diff="diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -2 +2 @@\n-old_value()\n+valid_value()\n",
    )
    assert run.code == 0, run.err
    posted = run.payload["payload"]
    assert len(posted["comments"]) == 1
    assert "Visible issue" in posted["comments"][0]["body"]
    bullets = [
        line for line in posted["body"].splitlines() if line.startswith("- 🟠 [HIGH]")
    ]
    assert bullets == ["- 🟠 [HIGH] `foo.py:2`: Visible issue"]
    assert posted["body"].count("Unanchored lead detail") == 1
    assert (
        "### 1 issue could not be attached to a changed line, so it is described here"
        in posted["body"]
    )


def test_legacy_summary_plural_improvements_and_missing_index_are_plain():
    rows = [
        finding(id=str(i), report_tag="suggestion", severity="low") for i in range(2)
    ]
    body = compose.summary_for_delivery("", cast(list[Finding], rows))
    assert body == (
        "Found 2 issues in this change: 2 low. 2 are suggested improvements rather than defects."
        "\n\n2 more issues not listed here (did not fit in this comment)."
    )


@pytest.mark.parametrize(
    "drift",
    [
        lambda text: text.replace("- 🟠 [HIGH]", "- HIGH"),
        lambda text: text.replace(": Visible issue", " :: Visible issue"),
    ],
)
def test_summary_index_contract_drift_fails_before_posting(drift):
    rows = [finding(id="A", title="Visible issue", anchor="wrong_value()")]
    summary = run_node(
        "import { renderSummaryBody } from './workflows/src/renderReport.js';"
        "let s='';for await(const c of process.stdin)s+=c;"
        "process.stdout.write(renderSummaryBody(JSON.parse(s)));",
        {"findings": rows},
    ).stdout
    with pytest.raises(compose.ReportShapeError, match="Summary index does not match"):
        compose.compose_review_body(
            drift(summary),
            [],
            platform="github",
            findings_count=1,
            sha="a" * 40,
            index_findings=cast(list[Finding], rows),
        )


@pytest.mark.parametrize(
    "severities,suggestions",
    [([], 0), (["critical"], 0), (["high", "low"], 1), (["medium", "low", "low"], 2)],
)
def test_summary_count_templates_match_across_runtimes(severities, suggestions):
    rows = [
        finding(
            id=str(i),
            severity=severity,
            report_tag="suggestion" if i < suggestions else "main",
        )
        for i, severity in enumerate(severities)
    ]
    body = run_node(
        "import { renderSummaryBody } from './workflows/src/renderReport.js';"
        "let s='';for await(const c of process.stdin)s+=c;process.stdout.write(renderSummaryBody(JSON.parse(s)));",
        {"findings": rows},
    ).stdout
    assert (
        compose.summary_for_delivery("", cast(list[Finding], rows)).split("\n")[0]
        == body.split("\n")[0]
    )
