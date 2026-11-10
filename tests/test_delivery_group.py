"""One defect produces one short comment while member identity stays stable."""

import json
import subprocess
from pathlib import Path

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


def test_security_member_keeps_its_own_pr_comment_after_model_grouping():
    output = run_node(
        "import { makeFinding } from './workflows/test/helpers/pipelineMock.js';"
        "import { disposeFindings } from './workflows/src/dispose.js';"
        "import { writerPayload } from './workflows/src/persistPlan.js';"
        "const findings=[makeFinding('LEAD',{description:'Ordinary defect text.'}),"
        "makeFinding('SECURITY',{dimension:'security',description:'Security defect text must stay on the PR.'})];"
        "process.stdout.write(JSON.stringify(writerPayload(disposeFindings({findings,grouping_reply:"
        "{groups:[{finding_ids:['LEAD','SECURITY'],lead_id:'LEAD'}]}})).findings));",
        {},
    )
    groups = compose.consolidate_delivery(json.loads(output.stdout))
    comments = [
        compose.render_group_body(group.primary, group.corroborators)
        for group in groups
    ]
    assert len(comments) == 2
    assert "Ordinary defect text." in comments[0]
    assert "Security defect text must stay on the PR." in comments[1]
