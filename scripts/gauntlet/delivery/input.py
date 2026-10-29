"""Checked delivery input, preserving the caller's JSON objects."""

from typing import TypedDict, cast

from gauntlet.cli import CliError
from gauntlet.delivery.gate import is_plain_int
from gauntlet.forge import parse_web_origin
from gauntlet.location import normalize_location
from gauntlet.registry import LocationStatus


class Finding(TypedDict, total=False):
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
        key = finding.get("consolidation_key")
        if key is not None and not isinstance(key, str):
            raise CliError(
                f"findings[{index}].consolidation_key must be a string or null"
            )
    return cast(
        ReviewInput, {**data, "findings": [normalize_location(f) for f in findings]}
    )
