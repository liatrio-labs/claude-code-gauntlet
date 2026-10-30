"""Checked delivery input, preserving the caller's JSON objects."""

from typing import TypedDict, cast

from gauntlet.cli import CliError
from gauntlet.delivery.gate import is_plain_int
from gauntlet.forge import parse_web_origin
from gauntlet.location import normalize_location
from gauntlet.registry import (
    DIMENSION_ROWS,
    CandidateTestsItem,
    DeliveryGroup,
    Dimension,
    Disposition,
    DuplicateOf,
    EffectiveTestKind,
    LocationStatus,
    Precedent,
    ProductionElement,
    QualityBasis,
    ResolvedSitesItem,
    TestKind,
)


class Finding(TypedDict, total=False):
    origin_dimension: Dimension
    effective_test_kind: EffectiveTestKind
    test_kind: TestKind
    quality_basis: QualityBasis
    searched_paths: list[str]
    candidate_tests: list[CandidateTestsItem]
    duplicate_of: DuplicateOf
    precedent: Precedent
    production_element: ProductionElement
    delivery_group: DeliveryGroup
    sites: list[ResolvedSitesItem]
    disposition: Disposition
    disposition_reasons: list[str]
    file: str
    line_start: int
    line_end: int
    anchor: str
    location_status: LocationStatus
    claimed_line_start: int
    claimed_line_end: int
    line: int | None
    end_line: int | None
    consolidation_key: str | None
    title: object
    body: object
    severity: object
    suggested_fix_code: object
    suggestion: object
    claude_md_rule: object
    spec_text: object
    rule_source: object
    agent: object
    dimension: object
    confidence: object
    id: object
    consolidation_primary: object


class _RequiredReviewInput(TypedDict):
    owner: str
    repo: str
    pr_number: int | str
    findings: list[Finding]


class ReviewInput(_RequiredReviewInput, total=False):
    platform: str | None
    web_origin: str
    sha: object
    review_body: object


def validate_review_input(data: dict[str, object]) -> ReviewInput:
    for field in ("owner", "repo", "pr_number"):
        if field not in data:
            raise CliError(f"Missing required field in findings JSON: '{field}'")
    for field in ("owner", "repo"):
        if not isinstance(data[field], str):
            raise CliError(f"{field} must be a string")
    number = data["pr_number"]
    if not is_plain_int(number) and not isinstance(number, str):
        raise CliError("pr_number must be an integer or a string")
    platform = data.get("platform")
    if platform is not None and not isinstance(platform, str):
        raise CliError("platform must be a string or null")
    if platform and platform.lower() not in ("github", "gitlab"):
        raise CliError(
            f"Unsupported platform: '{platform.lower()}'. Use 'github' or 'gitlab'."
        )
    if "web_origin" in data:
        origin = data["web_origin"]
        if not isinstance(origin, str):
            raise CliError("web_origin must be a string")
        try:
            parse_web_origin(origin)
        except ValueError as exc:
            raise CliError(f"web_origin: {exc}") from exc
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        raise CliError("findings must be an array")
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise CliError(f"findings[{index}] must be an object")
        if "file" in finding and not isinstance(finding["file"], str):
            raise CliError(f"findings[{index}].file must be a string")
        dimensions = {row["dimension"]: row for row in DIMENSION_ROWS}
        if "origin_dimension" in finding and (
            not isinstance(finding["origin_dimension"], str)
            or finding["origin_dimension"] not in dimensions
        ):
            raise CliError(f"findings[{index}].origin_dimension is invalid")
        for row in DIMENSION_ROWS:
            admission = row["admission"]
            if not admission:
                continue
            for field in (admission["field"], "effective_test_kind"):
                if field == "effective_test_kind" and admission["field"] != "test_kind":
                    continue
                allowed = (
                    admission["kinds"]
                    if field == admission["field"]
                    else {
                        subject["kind"]
                        for subject in row["policySubjects"].values()
                        if subject.get("stamp") == field
                    }
                )
                if field in finding and (
                    not isinstance(finding[field], str) or finding[field] not in allowed
                ):
                    raise CliError(f"findings[{index}].{field} is invalid")
        key = finding.get("consolidation_key")
        if key is not None and not isinstance(key, str):
            raise CliError(
                f"findings[{index}].consolidation_key must be a string or null"
            )
        stamp = finding.get("delivery_group")
        if stamp is not None and (
            not isinstance(stamp, dict)
            or set(stamp) != {"issue_key", "lead_id", "finding_ids"}
            or not all(
                isinstance(stamp.get(k), str) and stamp[k]
                for k in ("issue_key", "lead_id")
            )
            or not isinstance(stamp.get("finding_ids"), list)
            or not all(isinstance(k, str) and k for k in stamp["finding_ids"])
            or len(set(stamp["finding_ids"])) != len(stamp["finding_ids"])
            or finding.get("id") not in stamp["finding_ids"]
            or stamp["lead_id"] not in stamp["finding_ids"]
        ):
            raise CliError(f"findings[{index}].delivery_group is malformed")
        sites = finding.get("sites", [])
        if not isinstance(sites, list) or any(
            not isinstance(site, dict)
            or not isinstance(site.get("file"), str)
            or not isinstance(site.get("anchor"), str)
            for site in sites
        ):
            raise CliError(f"findings[{index}].sites is malformed")
    normalized = [normalize_location(f) for f in findings]
    # Only a wholly unstamped caller input predates disposition enforcement.
    if not any("disposition" in f for f in findings):
        for finding in normalized:
            legacy_admission = any(
                row["admission"]
                and (
                    row["dimension"]
                    in (finding.get("origin_dimension"), finding.get("dimension"))
                    or row["admission"]["field"] in finding
                    or row["agentType"].split(":")[-1] == finding.get("agent")
                )
                for row in DIMENSION_ROWS
            )
            finding["disposition"] = "held_back" if legacy_admission else "deliver"
    return cast(ReviewInput, {**data, "findings": normalized})
