"""Current PR surfaces use issue counts and reveal only the lead's prose."""

import json
import re
from typing import cast

import pytest
from gauntlet.delivery import compose
from gauntlet.delivery.input import Finding

from tests.support.delivery import finding
from tests.support.pipeline import run_node
from tests.support.posting import invoke_posting, review
from tests.support.pr_surfaces import render_pr_surfaces


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
    legacy = compose.legacy_summary(cast(list[Finding], actual["findings"]))
    assert legacy["counts"] == actual["summary_body"].split("\n")[0]
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


@pytest.mark.parametrize(
    "title,filepath",
    [
        (title, "foo.py")
        for title in [
            "An invalid range",
            "[x]: u",
            "`x [a]: SENT](a b)`",
            "x" * 520,
            "a\nb",
        ]
    ]
    + [("Placeholder path", "{title}/{suggestion}.py")],
)
def test_late_anchor_failure_removes_only_its_index_bullet(
    title, filepath, tmp_path, monkeypatch, capsys, forge_factory
):
    rows = [
        finding(
            id="A",
            file=filepath,
            line=2,
            end_line=2,
            title=title,
            body="Unanchored lead detail",
            anchor="wrong_value()",
        ),
        finding(
            id="B",
            file=filepath,
            line=2,
            end_line=2,
            title="Visible issue",
            body="Inline lead detail",
            anchor="valid_value()",
        ),
    ]
    summary = json.loads(
        run_node(
            "import { renderPublicSummary } from './workflows/src/renderReport.js';"
            "let s='';for await(const c of process.stdin)s+=c;"
            "process.stdout.write(JSON.stringify(renderPublicSummary(JSON.parse(s))));",
            {"findings": rows},
        ).stdout
    )
    body = summary.pop("body")
    run = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        review(findings=rows, review_body=body, review_summary=summary),
        diff=f"diff --git a/{filepath} b/{filepath}\n--- a/{filepath}\n+++ b/{filepath}\n@@ -2 +2 @@\n-old_value()\n+valid_value()\n",
    )
    assert run.code == 0, run.err
    posted = run.payload["payload"]
    assert len(posted["comments"]) == 1
    assert "Visible issue" in posted["comments"][0]["body"]
    bullets = [
        line for line in posted["body"].splitlines() if line.startswith("- 🟠 [HIGH]")
    ]
    assert bullets == [f"- 🟠 [HIGH] `{filepath}:2`: Visible issue"]
    assert posted["body"].count("Unanchored lead detail") == 1
    assert (
        "### 1 issue could not be attached to a changed line, so it is described here"
        in posted["body"]
    )


def test_legacy_summary_owns_its_index():
    rows = [
        finding(id=str(i), report_tag="suggestion", severity="low") for i in range(2)
    ]
    summary = compose.legacy_summary(cast(list[Finding], rows))
    assert (
        summary["counts"]
        == "Found 2 issues in this change: 2 low. 2 are suggested improvements rather than defects."
    )
    assert len(summary["index"]) == 2


@pytest.mark.parametrize(
    "index,error",
    [
        ([{"lead_id": "foreign", "bullet": "opaque text"}], "Unknown summary lead id"),
        ([], "Missing summary bullet"),
    ],
)
def test_summary_contract_rejects_reference_breaks(index, error):
    rows = [finding(id="A")]
    with pytest.raises(compose.ReportShapeError, match=error):
        compose.compose_review_body(
            "",
            [],
            platform="github",
            findings_count=1,
            sha="a" * 40,
            index_findings=cast(list[Finding], rows),
            review_summary={
                "counts": "Counts",
                "index": index,
                "omitted": 0,
                "reasons": [],
            },
        )


def test_summary_contract_preserves_producer_bytes():
    rows = [finding(id="A")]
    bullet = "- workflow bytes with  two spaces and \\~\\~\\~"
    body = compose.compose_review_body(
        "untrusted stale body",
        [],
        platform="github",
        findings_count=1,
        sha="a" * 40,
        index_findings=cast(list[Finding], rows),
        review_summary={
            "counts": "Exact counts",
            "index": [{"lead_id": "A", "bullet": bullet}],
            "omitted": 0,
            "reasons": [],
        },
    ).body
    assert "Exact counts\n\n" + bullet in body
    assert "untrusted stale body" not in body


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
        "import { renderPublicSummary } from './workflows/src/renderReport.js';"
        "let s='';for await(const c of process.stdin)s+=c;process.stdout.write(renderPublicSummary(JSON.parse(s)).body);",
        {"findings": rows},
    ).stdout
    assert (
        compose.legacy_summary(cast(list[Finding], rows))["counts"]
        == body.split("\n")[0]
    )


@pytest.mark.parametrize("pathway", ["workflow", "report"])
@pytest.mark.parametrize(
    "vector", ["group-values", "tilde-title", "path-spaces", "title-spaces"]
)
def test_summary_data_crosses_delivery_boundary(
    vector, pathway, tmp_path, monkeypatch, capsys, forge_factory
):
    filepath = "  src/a  b.py  " if vector == "path-spaces" else "src/a.py"
    title = {"tilde-title": "A ~~~ title", "title-spaces": "  A  spaced  title  "}.get(
        vector, "Visible issue"
    )
    rows = [
        finding(
            id="ACTIVE",
            file=filepath,
            line=2,
            end_line=2,
            title=title,
            body="Inline detail.",
            anchor="valid_value()",
            severity="medium" if vector == "group-values" else "high",
            dimension="security" if vector == "group-values" else "bug",
            report_tag="suggestion" if vector == "group-values" else "main",
        ),
        finding(
            id="HIDDEN",
            file=filepath,
            line=2,
            end_line=2,
            title="Unanchored issue",
            body="Unanchored detail.",
            anchor="wrong_value()",
        ),
    ]
    if vector == "group-values":
        rows.append(
            finding(
                id="BUG",
                file=filepath,
                line=2,
                end_line=2,
                title="Other member",
                body="Other member detail.",
                severity="critical",
                report_tag="main",
                anchor="valid_value()",
            )
        )
    rendered = json.loads(
        run_node(
            "import { makeFinding } from './workflows/test/helpers/pipelineMock.js';"
            "import { disposeFindings } from './workflows/src/dispose.js';"
            "import { renderPublicSummary, renderReport } from './workflows/src/renderReport.js';"
            "import { writerPayload } from './workflows/src/persistPlan.js';"
            "let s='';for await(const c of process.stdin)s+=c;const rows=JSON.parse(s);"
            "const findings=rows.map(f=>makeFinding(f.id,{...f,description:f.body}));"
            "const out=disposeFindings({findings,grouping_reply:{groups:rows.length===3?[{finding_ids:['ACTIVE','BUG'],lead_id:'BUG'}]:[]}});"
            "const {body:reviewBody,...reviewSummary}=renderPublicSummary({...out,delivered:out.findings});"
            "process.stdout.write(JSON.stringify(writerPayload({findings:out.findings,postReview:out.findings,reviewBody,reviewSummary,report:renderReport(out),"
            "prIdentity:{owner:'o',repo:'r',pr_number:5,sha_full:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',platform:'github',web_origin:'https://github.com'}})));",
            rows,
        ).stdout
    )
    payload = rendered["postReview"]
    expected = next(
        row["bullet"]
        for row in payload["review_summary"]["index"]
        if row["lead_id"] == "ACTIVE"
    )
    arguments = ()
    if pathway == "report":
        payload.pop("review_summary")
        payload["review_body"] = ""
        report = tmp_path / "report.md"
        report.write_text(rendered["report"], encoding="utf-8")
        arguments = ("--report", str(report))
    old, new = json.dumps("a/" + filepath), json.dumps("b/" + filepath)
    patch = f"diff --git {old} {new}\n--- {old}\n+++ {new}\n@@ -2 +2 @@\n-old_value()\n+valid_value()\n"
    run = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        payload,
        arguments=arguments,
        diff=patch,
    )
    assert run.code == 0, run.err
    posted = run.payload["payload"]
    assert len(posted["comments"]) == 1
    assert posted["comments"][0]["path"] == filepath
    assert "Inline detail." in posted["comments"][0]["body"]
    bullets = [line for line in posted["body"].splitlines() if line.startswith("- ")]
    assert len(bullets) == 1
    assert posted["body"].count("Unanchored detail.") == 1
    if pathway == "workflow":
        assert bullets == [expected]
    if vector == "group-values":
        assert bullets[0].startswith("- 🔴 [CRITICAL]")
        assert "improvement suggestion" not in posted["body"]
    if vector == "tilde-title":
        assert r"\~\~\~" in bullets[0]
