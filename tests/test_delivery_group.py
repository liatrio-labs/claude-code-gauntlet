"""One defect produces one short comment while member identity stays stable."""

import json
import subprocess
from pathlib import Path

import pytest
from gauntlet.delivery import compose
from gauntlet.delivery.input import Finding
from gauntlet.marker import build_finding_marker, find_finding_markers
from gauntlet.prior_review import prior_delivery_from_entries

from tests.support.delivery import date_override_members
from tests.support.pipeline import run_node
from tests.support.posting import invoke_posting, review

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/delivery_group.json"
SHA = "a" * 40


def members() -> tuple[list[Finding], dict[str, str]]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return date_override_members(), fixture


def test_group_posts_lead_prose_sites_and_distinct_reviewer_count():
    rows, keys = members()
    lead, *others = rows
    lead["sites"] = [
        {
            "file": lead["file"],
            "line_start": 107,
            "line_end": 109,
            "location_status": "confirmed",
        },
        {
            "file": lead["file"],
            "line_start": 125,
            "line_end": 127,
            "location_status": "confirmed",
        },
        {
            "file": "hidden.ts",
            "line_start": 1,
            "line_end": 1,
            "location_status": "unconfirmed",
        },
    ]
    others[0]["sites"] = [
        {
            "file": "src/other.ts",
            "line_start": 2,
            "line_end": 2,
            "location_status": "relocated",
        },
        {
            "file": lead["file"],
            "line_start": 125,
            "line_end": 127,
            "location_status": "confirmed",
        },
    ]
    body = compose.render_group_body(lead, others)
    assert body == (
        "**🟠 [HIGH] Multiple date overrides on one day make valid slots unavailable**\n\n"
        + str(lead["body"])
        + "\n\n**Suggested fix:**\n"
        + str(lead["suggestion"])
        + "\n\n**Additional sites:**\n"
        "- `src/availability/slots.ts:125-127`\n"
        "- `src/availability/slots.ts:114`\n"
        "- `src/other.ts:2`\n"
        "- `src/availability/slots.ts:141`\n\n"
        "_Found independently by 3 reviewers._\n\n⚔️ *Code Gauntlet*"
    )
    assert "Corroborating finding" not in body
    assert all(
        str(other["description"]) not in body and str(other["title"]) not in body
        for other in others
    )
    for row in rows:
        assert (
            compose.finding_key(
                row["file"], row["line"], row["title"], compose.key_material_body(row)
            )
            == keys[row["id"]]
        )
    marked = (
        body
        + "\n\n"
        + "\n".join(build_finding_marker(SHA, key) for key in keys.values())
    )
    prior = prior_delivery_from_entries([{"body": marked}], SHA)
    assert prior.finding_keys == frozenset(keys.values())
    assert prior.legacy_group_keys == frozenset()


def test_single_reviewer_and_unconfirmed_member_have_no_extra_claims():
    rows, _ = members()
    lead, *others = rows
    for row in rows:
        row["agent"] = lead["agent"]
    others[0]["location_status"] = "unconfirmed"
    body = compose.render_group_body(lead, others)
    assert "Found independently" not in body
    assert ":114`" not in body
    assert ":125-127`" in body


def test_unanchored_group_is_one_issue_with_all_member_marker_coverage(
    tmp_path, monkeypatch, capsys, forge_factory
):
    rows, keys = members()
    for row in rows:
        row["disposition"] = "body"
    skipped = tuple(
        compose.SkippedEntry(row["file"], row["line"], row) for row in rows[:2]
    )
    composed = compose.compose_review_body(
        "", [skipped], platform="github", findings_count=2, sha=SHA
    )
    assert (
        "### 1 issue could not be attached to a changed line, so it is described here"
        in composed.body
    )
    assert composed.body.count("#### ") == 1
    assert str(rows[1]["description"]) not in composed.body
    assert "### bug" not in composed.body
    assert "### cross_file_impact" not in composed.body
    assert composed.shown == 1
    assert composed.shown_entries == skipped
    run = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        review("gitlab", rows[:2]),
        dry_run=False,
    )
    assert run.code == 0, run.err
    assert len(run.requests) == 1
    markers = find_finding_markers(run.requests[0].payload["body"])
    assert {marker["key"] for marker in markers} == {keys["F107"], keys["F114"]}
    assert len(markers) == 2


def test_local_report_keeps_every_member_description():
    rows, _ = members()
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            "import { renderReport } from './workflows/src/renderReport.js';"
            "let s='';for await(const c of process.stdin)s+=c;"
            "process.stdout.write(renderReport(JSON.parse(s)));",
        ],
        cwd=ROOT,
        input=json.dumps({"findings": rows}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert all(str(row["description"]) in result.stdout for row in rows)


@pytest.mark.parametrize(
    "ordinary,other,lead",
    [
        (
            {"severity": "critical"},
            {"severity": "medium", "dimension": "security"},
            "SEC",
        ),
        (
            {"severity": "low"},
            {"severity": "critical", "report_tag": "suggestion"},
            "BUG",
        ),
        (
            {"severity": "critical"},
            {"severity": "medium", "dimension": "security", "report_tag": "suggestion"},
            "SEC",
        ),
    ],
)
def test_indivisible_group_posts_security_text_and_group_values(ordinary, other, lead):
    output = run_node(
        "import { makeFinding } from './workflows/test/helpers/pipelineMock.js';"
        "import { disposeFindings } from './workflows/src/dispose.js';"
        "import { selectDelivery } from './workflows/src/reviewStages.js';"
        "import { writerPayload } from './workflows/src/persistPlan.js';"
        "import { renderPublicSummary, renderReport } from './workflows/src/renderReport.js';"
        "let s='';for await(const c of process.stdin)s+=c;const rows=JSON.parse(s);"
        "const findings=rows.map((row,i)=>makeFinding(i?'SEC':'BUG',"
        "{file:'same.js',description:i?'Security finding text.':'Ordinary defect text.',agent:i?'security-reviewer':'bug-detector',...row}));"
        "const assessed=findings.map(f=>[f.id,f.severity,f.report_tag]);"
        "const out=disposeFindings({findings,grouping_reply:{groups:[{finding_ids:['BUG','SEC'],lead_id:'BUG'}]}});"
        "const selected=selectDelivery([...out.findings,makeFinding('OTHER',{severity:'high'})],1,'main_only');"
        "process.stdout.write(JSON.stringify({...writerPayload({findings:out.findings,postReview:selected}),"
        "assessed,summary:renderPublicSummary({...out,delivered:selected}).body,report:renderReport(out)}));",
        [ordinary, other],
    )
    result = json.loads(output.stdout)
    groups = compose.consolidate_delivery(result["postReview"])
    assert len(groups) == 1
    group = groups[0]
    assert group.primary["id"] == lead
    assert {f["id"] for f in (group.primary, *group.corroborators)} == {"BUG", "SEC"}
    assert (
        compose.legacy_summary(result["postReview"])["counts"]
        == "Found 1 issue in this change: 1 critical."
    )
    assessed_group = [
        (f["id"], f["severity"], f["report_tag"])
        for f in (group.primary, *group.corroborators)
    ]
    comment = compose.render_group_body(group.primary, group.corroborators)
    assert [
        (f["id"], f["severity"], f["report_tag"])
        for f in (group.primary, *group.corroborators)
    ] == assessed_group
    assert comment.startswith("**🔴 [CRITICAL]")
    assert ("Security finding text." in comment) == (lead == "SEC")
    assert ("Ordinary defect text." in comment) == (lead == "BUG")
    assert [
        [f["id"], f["severity"], f["report_tag"]] for f in result["findings"]
    ] == result["assessed"]
    summary = compose.compose_review_body(
        result["summary"],
        [],
        platform="github",
        findings_count=2,
        sha=SHA,
        index_findings=result["postReview"],
    ).body
    assert "Found 1 issue in this change: 1 critical." in summary
    assert "improvement suggestion" not in summary
    assert summary.count("- 🔴 [CRITICAL]") == 1
    assert "Security finding text." in result["report"]
    assert "Ordinary defect text." in result["report"]
    assert "severity critical, tier " in result["report"]
    if (
        ordinary.get("report_tag") == "suggestion"
        or other.get("report_tag") == "suggestion"
    ):
        assert (
            "tier suggestion" if lead == "BUG" else "improvement suggestion"
        ) in result["report"]
