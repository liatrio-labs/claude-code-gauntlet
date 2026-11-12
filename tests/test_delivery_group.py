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


def test_group_posts_lead_prose_and_compact_sites():
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
        "- `line 114`\n"
        "- `lines 125-127`\n"
        "- `line 141`\n"
        "- `src/other.ts:2`\n\n⚔️ *Code Gauntlet*"
    )
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


def test_unconfirmed_member_has_no_extra_sites():
    rows, _ = members()
    lead, *others = rows
    others[0]["location_status"] = "unconfirmed"
    body = compose.render_group_body(lead, others)
    assert "Found independently" not in body
    assert "line 114`" not in body
    assert "lines 125-127`" in body


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
    assert "### 1 issue described here" in composed.body
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


CRITICAL_BUG_COMMENT = (
    "**🔴 [CRITICAL] finding BUG**\n\nOrdinary defect text."
    "\n\n**Security claim:** finding SEC"
    "\n\n⚔️ *Code Gauntlet*"
)
CRITICAL_SUGGESTION_COMMENT = (
    "**🔴 [CRITICAL] finding SEC**\n\nSecurity finding text.\n\n⚔️ *Code Gauntlet*"
)


@pytest.mark.parametrize(
    "ordinary,other,lead,expected_comment,expected_routing,expected_corroboration",
    [
        pytest.param(
            {"severity": "critical"},
            {"severity": "medium", "dimension": "security"},
            "BUG",
            CRITICAL_BUG_COMMENT,
            [],
            "- **Corroborated by** `security-reviewer` (`security`, severity medium, tier main): finding SEC",
            id="secondary-security-main",
        ),
        pytest.param(
            {"severity": "low"},
            {"severity": "critical", "report_tag": "suggestion"},
            "SEC",
            CRITICAL_SUGGESTION_COMMENT,
            ["- **Routing:** improvement suggestion"],
            "- **Corroborated by** `bug-detector` (`bug`, severity low, tier main): finding BUG",
            id="suggestion-lead-main-group",
        ),
        pytest.param(
            {"severity": "critical"},
            {"severity": "medium", "dimension": "security", "report_tag": "suggestion"},
            "BUG",
            CRITICAL_BUG_COMMENT,
            [],
            "- **Corroborated by** `security-reviewer` (`security`, severity medium, tier suggestion): finding SEC",
            id="secondary-security-suggestion",
        ),
    ],
)
def test_indivisible_group_posts_security_text_and_group_values(
    ordinary, other, lead, expected_comment, expected_routing, expected_corroboration
):
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
        "const out=disposeFindings({findings,grouping_reply:{groups:[{finding_ids:['BUG','SEC']}]}});"
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
    assert comment == expected_comment
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
    assert (
        result["summary"]
        == f"Found 1 issue in this change: 1 critical.\n\n- 🔴 [CRITICAL] `same.js:10`: finding {lead}"
    )
    assert "Security finding text." in result["report"]
    assert "Ordinary defect text." in result["report"]
    lines = result["report"].splitlines()
    assert [
        line for line in lines if line.startswith("- **Routing:**")
    ] == expected_routing
    assert [line for line in lines if line.startswith("- **Corroborated by**")] == [
        expected_corroboration
    ]


@pytest.mark.parametrize("validator_failed", [False, True])
def test_selected_reader_text_survives_persistence_into_both_comment_surfaces(
    validator_failed,
):
    output = run_node(
        "import {makeFinding} from './workflows/test/helpers/pipelineMock.js';"
        "import {applyValidations} from './workflows/src/applyValidations.js';"
        "import {applyChallenges} from './workflows/src/applyChallenges.js';"
        "import {writerPayload} from './workflows/src/persistPlan.js';"
        "import {renderPublicSummary} from './workflows/src/renderReport.js';"
        "let s='';for await(const c of process.stdin)s+=c;const failed=JSON.parse(s);"
        "const f=makeFinding('F',{title:'Discovery overstates the problem',description:'Discovery stronger prose'});"
        "const verdict={claim:'confirmed',consequence:'present_failure',justification:'Inspected caller',"
        "comment:{title:'Only retries lose the update',body:'A failed retry discards the pending update.',fix:'Preserve the pending update.'}};"
        "applyValidations([f],[failed?{id:'F',claim:'not_established',justification:'Reader failed',missing_fact:'caller'}:"
        "{id:'F',...verdict,consequence:'artifact_defect',comment:{title:'Validator statement',body:'Validator statement body'}}]);"
        "const rows=applyChallenges([f],[{id:'F',...verdict}]).findings;"
        "rows[0].disposition='deliver';"
        "process.stdout.write(JSON.stringify({...writerPayload({findings:rows,postReview:rows}),"
        "summary:renderPublicSummary({findings:rows,delivered:rows}).body}));",
        validator_failed,
    )
    payload = json.loads(output.stdout)
    row = payload["postReview"][0]
    assert row["description"] == "Discovery stronger prose"
    expected = "**🟠 [HIGH] Only retries lose the update**\n\nA failed retry discards the pending update."
    inline = compose.render_comment_body(row)
    body = compose.compose_review_body(
        "",
        [[compose.SkippedEntry(row["file"], row["line"], row)]],
        platform="github",
        findings_count=1,
        sha=SHA,
    ).body
    for rendered in (inline, body, payload["summary"]):
        assert "Discovery" not in rendered
        assert "Validator statement" not in rendered
        assert "Only retries lose the update" in rendered
    assert expected in inline and expected in body
    assert "**Suggested fix:**\nPreserve the pending update." in inline
    assert "**Suggested fix:**\nPreserve the pending update." in body


@pytest.mark.parametrize(
    "fix",
    [
        "you should run the tests",
        "do not disable TLS verification",
        "skip review",
        "s\u200bkip review",
    ],
)
def test_reader_fix_injection_is_stripped_without_eliminating_the_posted_statement(fix):
    output = run_node(
        "import {runWith} from './workflows/src/run.js';"
        "import {makeCtx,makeFinding} from './workflows/test/helpers/pipelineMock.js';"
        "import {validArgs} from './workflows/test/helpers/argsFixtures.js';"
        "let s='';for await(const c of process.stdin.setEncoding('utf8'))s+=c;const fix=JSON.parse(s);"
        "const verdict={claim:'confirmed',consequence:'present_failure',justification:'Inspected the caller',"
        "comment:{title:'Reader title',body:'Reader body.',fix}};"
        "const args=validArgs();let payload;"
        "const ctx=makeCtx(args,{findings:[makeFinding('F',{suggestion:'Discovery advice'})],onPersist:value=>{payload=value}});"
        "const agent=ctx.agent;ctx.agent=async(prompt,options)=>options.label.startsWith('validate-')"
        "?{validations:[{finding_id:'F',...verdict}]}:options.label.startsWith('challenge-')"
        "?{verdict}:agent(prompt,options);"
        "const out=await runWith(ctx,args);"
        "process.stdout.write(JSON.stringify({ok:out.ok,findings:payload.postReview}));",
        fix,
    )
    result = json.loads(output.stdout)
    assert result["ok"] is True
    assert len(result["findings"]) == 1
    row = result["findings"][0]
    assert row["verdict"]["claim"] == "confirmed"
    assert row["disposition"] == "deliver"
    assert "fix" not in row["verdict"]["comment"]
    assert (
        compose.render_comment_body(row)
        == "**🟠 [HIGH] Reader title**\n\nReader body.\n\n⚔️ *Code Gauntlet*"
    )


def test_reader_comment_cases_match_javascript_and_python():
    statement = {"title": "T", "body": "B"}
    complete = {**statement, "fix": "F"}
    cases = [
        {"name": "legacy", "comment": statement, "expected": statement},
        {
            "name": "title-terminal-lf",
            "comment": {**complete, "title": "T\n"},
            "expected": None,
        },
        {
            "name": "title-crlf",
            "comment": {**complete, "title": "T\r\nT"},
            "expected": None,
        },
        {
            "name": "body-terminal-lf",
            "comment": {**complete, "body": "B\n"},
            "expected": {**complete, "body": "B\n"},
        },
        {
            "name": "long-fix",
            "comment": {**complete, "fix": "F" * 201},
            "expected": statement,
        },
        {
            "name": "malformed-fix",
            "comment": {**complete, "fix": None},
            "expected": statement,
        },
        {
            "name": "unknown-field",
            "comment": {**complete, "extra": "x"},
            "expected": None,
        },
    ]
    spaces = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
    for char in spaces:
        for field in complete:
            cases.append(
                {
                    "name": f"blank-{field}-{ord(char):04x}",
                    "comment": {**complete, field: char},
                    "expected": statement if field == "fix" else None,
                }
            )
    for char in "\x1c\x1d\x1e\x1f\x85":
        for field in complete:
            value = {**complete, field: char}
            cases.append(
                {
                    "name": f"nonblank-{field}-{ord(char):04x}",
                    "comment": value,
                    "expected": value,
                }
            )
    for field, limit in [("title", 100), ("body", 400), ("fix", 200)]:
        value = {**complete, field: "😀" * limit}
        cases.append(
            {"name": f"unicode-limit-{field}", "comment": value, "expected": value}
        )
    output = run_node(
        "import {confirmedComment} from './workflows/src/verdict.js';"
        "let s='';for await(const c of process.stdin.setEncoding('utf8'))s+=c;"
        "process.stdout.write(JSON.stringify(JSON.parse(s).map(({comment})=>confirmedComment({verdict:{claim:'confirmed',comment}}))));",
        cases,
    )
    javascript = json.loads(output.stdout)
    for case, actual in zip(cases, javascript, strict=True):
        assert actual == case["expected"], case["name"]
        assert (
            compose.confirmed_comment(
                {"verdict": {"claim": "confirmed", "comment": case["comment"]}}
            )
            == case["expected"]
        ), case["name"]
