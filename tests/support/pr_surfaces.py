"""Small delivery cases rendered through the workflow and production composer."""

import json
from typing import TypedDict, cast

from gauntlet.delivery import compose
from gauntlet.delivery.input import Finding, ReviewSummary
from gauntlet.location import delivery_allows

from tests.support.delivery import date_override_members, finding
from tests.support.pipeline import run_node


class RenderedSurfaces(TypedDict):
    findings: list[Finding]
    report: str
    summary_body: str
    review_summary: ReviewSummary
    summary: str
    inline: list[str]


def render_pr_surfaces() -> RenderedSurfaces:
    rows = [
        *date_override_members(),
        finding(
            id="FETCH",
            file="src/api/schedule.ts",
            line=42,
            end_line=42,
            severity="medium",
            title="A failed override fetch is ignored, so every slot shows as open",
            body="A rejected override request is treated as an empty list, so unavailable times are shown as open.",
            suggestion="Return an error when the override fetch fails.",
        ),
        finding(
            id="HELPER",
            file="src/availability/slots.ts",
            line=210,
            end_line=210,
            severity="low",
            dimension="simplification",
            agent="code-simplifier",
            quality_basis="simpler_equivalent",
            title="The end-of-day check can reuse the shared `isBefore` helper",
            body="The end-of-day comparison repeats the shared helper with the same inputs.",
            suggestion="Reuse isBefore with the same timestamps.",
            behavior_preserved="The shared helper compares the same two timestamps.",
            admission_assessments=[
                {"predicate": predicate, "status": status, "references": ["primary"]}
                for predicate, status in [
                    ("present_defect", "rejected"),
                    ("concrete_equivalent", "confirmed"),
                    ("behavior_preserved", "confirmed"),
                ]
            ],
        ),
        finding(
            id="ZONE",
            file="src/config/defaults.ts",
            line=12,
            end_line=12,
            severity="medium",
            diff_validation={"in_diff": False},
            title="The default time zone is read before the user's setting loads",
            body="The first request uses the default zone before the saved setting loads, shifting slots to the wrong day.",
            suggestion="Wait for the saved time zone before computing slots.",
        ),
        finding(
            id="HELD",
            validation="skipped",
            title="An unconfirmed observation",
            body="Private review diagnostics must stay in the local report.",
        ),
    ]
    result = cast(
        RenderedSurfaces,
        json.loads(
            run_node(
                """
import { disposeFindings } from './workflows/src/dispose.js';
import { evaluateAdmission } from './workflows/src/admission.js';
import { renderPublicSummary, renderReport } from './workflows/src/renderReport.js';
import { writerPayload } from './workflows/src/persistPlan.js';
import { reportInput } from './workflows/test/helpers/reportFixtures.js';
let source='';for await(const chunk of process.stdin)source+=chunk;
const findings=JSON.parse(source).map(f=>evaluateAdmission({...f,description:f.body,
 origin:'new',validation:f.validation??'validated',challenge:'survived',path_role:'source',
 policy_subject:'ordinary',diff_validation:f.diff_validation??{in_diff:true},dimension:f.dimension??'bug',confidence:90}));
const out=disposeFindings({findings,grouping_reply:{groups:[{
 finding_ids:findings.slice(0,4).map(f=>f.id),lead_id:findings[0].id}]}});
const input=reportInput({findings:out.findings,unverified:out.unverified,delivered:out.findings});
const {body:summary_body,...review_summary}=renderPublicSummary(input);
process.stdout.write(JSON.stringify({summary_body,review_summary,report:renderReport(input),
 findings:writerPayload({findings:out.findings}).findings}));
""",
                rows,
            ).stdout
        ),
    )
    findings = result["findings"]
    groups = compose.consolidate_delivery(findings)
    result["inline"] = [
        compose.render_group_body(group.primary, group.corroborators)
        for group in groups
        if delivery_allows(group.primary, "inline")
    ]
    skipped = [
        tuple(
            compose.SkippedEntry(f.get("file"), f.get("line"), f)
            for f in (group.primary, *group.corroborators)
        )
        for group in groups
        if not delivery_allows(group.primary, "inline")
    ]
    result["summary"] = compose.compose_review_body(
        result["summary_body"],
        skipped,
        platform="github",
        findings_count=len(findings),
        sha="f" * 40,
        index_findings=findings,
        review_summary=result["review_summary"],
    ).body
    return result
