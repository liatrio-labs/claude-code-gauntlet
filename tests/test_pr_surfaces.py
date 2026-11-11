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
    assert "### 1 issue described here" in posted["body"]


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


@pytest.mark.parametrize("pathway", ["workflow", "report", "bare"])
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
            "const out=disposeFindings({findings,grouping_reply:{groups:rows.length===3?[{finding_ids:['ACTIVE','BUG']}]:[]}});"
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
        if row["lead_id"] == ("BUG" if vector == "group-values" else "ACTIVE")
    )
    arguments = ()
    if pathway == "report":
        payload.pop("review_summary")
        payload["review_body"] = ""
        report = tmp_path / "report.md"
        report.write_text(rendered["report"], encoding="utf-8")
        arguments = ("--report", str(report))
    if pathway == "bare":
        payload = payload["findings"]
        arguments = (
            "--owner",
            "o",
            "--repo",
            "r",
            "--pr-number",
            "5",
            "--platform",
            "github",
            "--sha",
            "a" * 40,
        )
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
    assert (
        "Other member detail." if vector == "group-values" else "Inline detail."
    ) in posted["comments"][0]["body"]
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


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("field", ["counts", "bullet", "reasons"])
@pytest.mark.parametrize(
    "hostile",
    ["<img src=x>", "@reviewer", "ghp_" + "a" * 30, "/close", "<!-- hidden -->"],
)
def test_uncontained_summary_degrades_without_losing_inline(
    platform, field, hostile, tmp_path, monkeypatch, capsys, forge_factory
):
    summary = {
        "counts": "Found 1 issue in this change: 1 high.",
        "index": [{"lead_id": "A", "bullet": "- safe issue"}],
        "omitted": 0,
        "reasons": [],
    }
    if field == "bullet":
        summary["index"][0]["bullet"] = hostile
    elif field == "reasons":
        summary["reasons"] = [hostile]
    else:
        summary[field] = hostile
    rows = [finding(id="A", end_line=2, anchor="valid_value()", body="Inline detail.")]
    run = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        review(platform, rows, review_summary=summary),
        diff="diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -2 +2 @@\n-old_value()\n+valid_value()\n",
    )
    assert run.code == 0, run.err
    if platform == "github":
        body = run.payload["payload"]["body"]
        comments = run.payload["payload"]["comments"]
    else:
        body = run.payload["summary"]["body"]
        comments = run.payload["discussions"]
    assert len(comments) == 1
    assert "Inline detail." in comments[0]["body"]
    assert hostile not in body and "- safe issue" not in body
    assert "Found 1 issue in this change: 1 high." in body
    assert run.payload["skipped"] == [
        "summary: Uncontained summary text.; issue index omitted"
    ]


@pytest.mark.parametrize(
    "broken",
    [
        None,
        [],
        {},
        {
            "counts": "Counts",
            "index": [{"lead_id": "foreign", "bullet": "- safe"}],
            "omitted": 0,
            "reasons": [],
        },
        {"counts": "Counts", "index": [], "omitted": 0, "reasons": []},
    ],
)
def test_summary_contract_failure_keeps_inline(
    broken, tmp_path, monkeypatch, capsys, forge_factory
):
    rows = [finding(id="A", end_line=2, anchor="valid_value()")]
    run = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        review(findings=rows, review_summary=broken),
        diff="diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -2 +2 @@\n-old_value()\n+valid_value()\n",
    )
    assert run.code == 0, run.err
    assert len(run.payload["payload"]["comments"]) == 1
    assert run.payload["skipped"][0].startswith("summary: ")
    assert "issue index omitted" in run.payload["skipped"][0]


def test_summary_index_budget_and_remainder_at_posting_boundary(
    tmp_path, monkeypatch, capsys, forge_factory
):
    rows = [finding(id=key, end_line=2, anchor="valid_value()") for key in "ABC"]
    first = "- " + "x" * 11998
    summary = {
        "counts": "Found 5 issues in this change: 5 high.",
        "index": [
            {"lead_id": key, "bullet": first if key == "A" else "- overflow " + key}
            for key in "ABC"
        ],
        "omitted": 2,
        "reasons": ["this review posts at most 3 issues"],
    }
    run = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        review(findings=rows, review_summary=summary),
        diff="diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -2 +2 @@\n-old_value()\n+valid_value()\n",
    )
    assert run.code == 0, run.err
    assert len(run.payload["payload"]["comments"]) == 3
    body = run.payload["payload"]["body"]
    assert first in body and "- overflow" not in body
    assert (
        "4 more issues not listed here (this review posts at most 3 issues; did not fit in this comment)."
        in body
    )
    assert run.payload["skipped"] == []


@pytest.mark.parametrize(
    "title", ["<img src=x>", "@reviewer", "[[a|https://example.test/p.png]]"]
)
def test_folded_security_claim_is_prepared_at_posting_boundary(
    title, tmp_path, monkeypatch, capsys, forge_factory
):
    stamp = {"issue_key": "group:A", "lead_id": "A", "finding_ids": ["A", "S"]}
    rows = [
        finding(
            id="A",
            end_line=2,
            anchor="valid_value()",
            severity="critical",
            dimension="bug",
            body="Most severe text.",
            delivery_group=stamp,
        ),
        finding(
            id="S",
            end_line=2,
            anchor="valid_value()",
            severity="medium",
            dimension="security",
            title=title,
            body="Hidden security prose.",
            delivery_group=stamp,
        ),
    ]
    run = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        review(findings=rows),
        diff="diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -2 +2 @@\n-old_value()\n+valid_value()\n",
    )
    assert run.code == 0, run.err
    comments = run.payload["payload"]["comments"]
    assert len(comments) == 1
    from gauntlet.text import prepare_line

    assert "**Security claim:** " + prepare_line(title) in comments[0]["body"]
    assert title not in comments[0]["body"]
    assert comments[0]["body"].startswith("**🔴 [CRITICAL]")
    assert "Most severe text." in comments[0]["body"]
