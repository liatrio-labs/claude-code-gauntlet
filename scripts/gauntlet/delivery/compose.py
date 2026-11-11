"""Pure delivery bodies, grouping, and key material."""

import hashlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from gauntlet import registry
from gauntlet.delivery import gate
from gauntlet.delivery.fold import (
    Platform,
    Surface,
    body_limit,
    fold_inline_body,
    fold_review_body,
    utf8_len,
)
from gauntlet.delivery.gate import FenceOffsets
from gauntlet.delivery.input import Finding, ReviewSummary, SummaryBullet
from gauntlet.location import canonical_range, delivery_allows, parse_admission
from gauntlet.markdown import fence_run
from gauntlet.marker import LEGACY_PRODUCT, build_footer, build_prose_footer
from gauntlet.prior_review import site_key
from gauntlet.text import (
    neutralize_comment_openers,
    normalize_report_severity,
    prepare_line,
    prepare_location,
    prepare_prose,
    prepare_summary_title,
    prepared_prose,
    redact_secrets,
)

__all__ = [
    "BRAND_SUMMARY_HEADER",
    "BRAND_TRAILER",
    "CommentStyle",
    "ComposedBody",
    "Group",
    "InlineBody",
    "OmittedEntry",
    "ReportShapeError",
    "SkippedEntry",
    "compose_inline_body",
    "compose_review_body",
    "consolidate_delivery",
    "delivery_finding",
    "digest_groups",
    "finding_key",
    "key_material_body",
    "legacy_summary",
    "render_comment_body",
    "render_finding_sections",
    "render_group_body",
    "render_group_sections",
]

# One mark per delivered surface: inline bodies end with the trailer, the
# summary opens with the header, and skipped entries inside it are unbranded.
BRAND_TRAILER = f"{registry.BRAND_MARK} *{registry.BRAND_NAME}*"
BRAND_SUMMARY_HEADER = f"### {registry.BRAND_MARK} {registry.BRAND_NAME}"


@dataclass(frozen=True, slots=True)
class CommentStyle:
    trailer: str
    severity_emoji: Mapping[str, str]
    severity_emoji_fallback: str
    rule_source_labels: Mapping[str, str]
    rule_source_label_fallback: str


@dataclass(frozen=True, slots=True)
class Group:
    primary: Finding
    corroborators: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class SkippedEntry:
    filepath: object
    line: object
    finding: Mapping[str, object]
    additional_sites: tuple[Mapping[str, object], ...] | None = None
    finding_index: int | None = None


@dataclass(frozen=True, slots=True)
class OmittedEntry:
    location: str
    title: object


@dataclass(frozen=True, slots=True)
class InlineBody:
    body: str
    folded_bytes: int


@dataclass(frozen=True, slots=True)
class ComposedBody:
    body: str
    shown: int
    omitted: int
    folded_bytes: int
    omitted_entries: tuple[OmittedEntry, ...]
    shown_entries: tuple[SkippedEntry, ...]


class ReportShapeError(Exception):
    pass


def _default_style() -> CommentStyle:
    return CommentStyle(
        BRAND_TRAILER,
        registry.SEVERITY_EMOJI,
        registry.SEVERITY_EMOJI_FALLBACK,
        registry.RULE_SOURCE_LABELS,
        registry.RULE_SOURCE_LABEL_FALLBACK,
    )


def _blockquote(text: str) -> str:
    # Raw CR must not terminate a line outside its quote prefix.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    out = []
    for line in lines:
        if line:
            out.append(f"> {line}")
        else:
            out.append(">")
    return "\n".join(out)


def _suggestion_fence(
    payload: str, *, offsets: FenceOffsets | None = None
) -> tuple[str, str]:
    # GitLab parses offsets independently of fence length; zero is the plain synonym.
    # Only the poster, which knows the anchor, can decide if offsets are expressible.
    fence = fence_run(payload)
    header = "suggestion"
    if offsets not in (None, (0, 0)):
        header = f"suggestion:-{offsets[0]}+{offsets[1]}"
    return f"{fence}{header}", fence


def _key_material_finding(finding: Mapping[str, object]) -> Mapping[str, object]:
    # Gate outcomes and provenance must not re-key a retry.
    if not isinstance(finding, dict) or not any(
        field in finding for field in ("suggested_fix_code", "rule_source")
    ):
        return finding
    stripped = dict(finding)
    stripped.pop("suggested_fix_code", None)
    stripped.pop("rule_source", None)
    return stripped


def render_finding_sections(
    finding: Mapping[str, object],
    *,
    fence_offsets: FenceOffsets | None = None,
    style: CommentStyle | None = None,
) -> str:
    style = style if style is not None else _default_style()
    severity = normalize_report_severity(finding.get("severity"), style.severity_emoji)
    emoji = style.severity_emoji.get(severity, style.severity_emoji_fallback)

    raw_title = finding.get("title")
    title = prepare_line(raw_title) if isinstance(raw_title, str) else ""
    body = prepare_prose(finding.get("body", ""))
    suggested_fix = gate.fix_code_text(finding.get("suggested_fix_code"))

    parts = [f"**{emoji} [{severity.upper()}] {title or 'Finding'}**", "", body]

    suggestion_text = prepared_prose(finding.get("suggestion"))
    if suggestion_text:
        parts += ["", "**Suggested fix:**", suggestion_text]

    # A comment-only rule must not block the fallback or select its source label.
    clause_rule = prepared_prose(finding.get("claude_md_rule"), cap=True)
    rule_text = clause_rule or prepared_prose(finding.get("spec_text"), cap=True)
    rule_label = style.rule_source_label_fallback
    if clause_rule:
        source = finding.get("rule_source")
        if isinstance(source, str):
            rule_label = style.rule_source_labels.get(
                source, style.rule_source_label_fallback
            )
    if rule_text:
        parts += ["", f"**{rule_label}:**", _blockquote(rule_text)]

    # The gate removes ineligible patches at each render site so inline and degraded
    # bodies can differ. Patch content stays exact except for credential redaction.
    # Normalize once: a second pass removes another LF and changes the approved patch.
    if suggested_fix and delivery_allows(finding, "fence"):
        suggested_fix = redact_secrets(suggested_fix)
        open_f, close_f = _suggestion_fence(suggested_fix, offsets=fence_offsets)
        parts += ["", open_f, suggested_fix, close_f]

    return "\n".join(parts)


# Folds and live markers change these unfolded bodies before posting.
def render_comment_body(
    finding: Mapping[str, object],
    *,
    fence_offsets: FenceOffsets | None = None,
    style: CommentStyle | None = None,
) -> str:
    return render_group_body(finding, [], fence_offsets=fence_offsets, style=style)


def key_material_body(finding: Mapping[str, object]) -> str:
    # Rendering changes re-key affected findings; branding never enters this seam.
    return render_finding_sections(_key_material_finding(finding))


def render_group_body(
    primary: Mapping[str, object],
    corroborators: Sequence[Mapping[str, object]],
    *,
    fence_offsets: FenceOffsets | None = None,
    style: CommentStyle | None = None,
) -> str:
    style = style if style is not None else _default_style()
    return render_group_sections(
        primary, corroborators, fence_offsets=fence_offsets, style=style
    ) + (f"\n\n{style.trailer}")


def location_text(finding: Mapping[str, object]) -> str:
    span = canonical_range(finding)
    assert span is not None
    start, end = span
    return f"{finding.get('file')}:{start}" + (f"-{end}" if end != start else "")


def group_sites(
    primary: Mapping[str, object], corroborators: Sequence[Mapping[str, object]]
) -> tuple[Mapping[str, object], ...]:
    seen = {site_key(primary)} if canonical_range(primary) is not None else set()
    sites = []
    for finding in (primary, *corroborators):
        candidates = list(
            cast(Sequence[Mapping[str, object]], finding.get("sites", []))
        )
        if finding is not primary:
            candidates.insert(0, finding)
        for site in candidates:
            located = {
                **site,
                "disposition": finding.get("disposition"),
                "admission": finding.get("admission"),
            }
            if not delivery_allows(located, "sourceLink") or site_key(site) in seen:
                continue
            seen.add(site_key(site))
            sites.append(site)
    return tuple(sites)


def delivery_finding(
    primary: Mapping[str, object], corroborators: Sequence[Mapping[str, object]] = ()
) -> Mapping[str, object]:
    if not corroborators:
        return primary
    members = (primary, *corroborators)
    severity = min(
        (
            normalize_report_severity(member.get("severity"), registry.SEVERITY_EMOJI)
            for member in members
        ),
        key=registry.SEVERITY_ORDER.index,
    )
    tier = "suggestion"
    for member in members:
        assessed = member.get("report_tag", member.get("report_destination"))
        if assessed is None:
            admission = parse_admission(member)
            assessed = admission["tier"] if admission else "main"
        if assessed == "main":
            tier = "main"
            break
    return {
        **primary,
        "severity": severity,
        "report_tag": tier,
        "report_destination": tier,
    }


def render_group_sections(
    primary: Mapping[str, object],
    corroborators: Sequence[Mapping[str, object]],
    *,
    fence_offsets: FenceOffsets | None = None,
    style: CommentStyle | None = None,
    additional_sites: Sequence[Mapping[str, object]] | None = None,
) -> str:
    body = render_finding_sections(
        delivery_finding(primary, corroborators),
        fence_offsets=fence_offsets,
        style=style,
    )
    sites = (
        group_sites(primary, corroborators)
        if additional_sites is None
        else additional_sites
    )
    if sites:
        body += "\n\n**Additional sites:**\n" + "\n".join(
            f"- {prepare_location(location_text(site))}" for site in sites
        )
    claims = list(
        dict.fromkeys(
            (prepare_line(member.get("title", "")) or "Finding")
            for member in corroborators
            if str(member.get("dimension", "")).lower() == "security"
        )
    )
    if claims:
        body += "\n\n**Security claim:** " + "; ".join(claims)
    agents = {
        agent.strip().lower()
        for finding in (primary, *corroborators)
        if isinstance(agent := finding.get("agent"), str) and agent.strip()
    }
    if len(agents) >= 2:
        body += f"\n\n_Found independently by {len(agents)} reviewers._"
    return body


def _skipped_location(filepath: object, finding: Mapping[str, object]) -> str:
    if not delivery_allows(finding, "sourceLink"):
        return f"{filepath or '?'} (location unconfirmed)"
    span = canonical_range(finding)
    assert span is not None
    return f"{filepath}:{span[0]}"


def _omitted_entries(entries: Sequence[SkippedEntry]) -> tuple[OmittedEntry, ...]:
    return tuple(
        OmittedEntry(
            _skipped_location(entry.filepath, entry.finding),
            entry.finding.get("title", "Finding"),
        )
        for entry in entries
    )


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or singular + "s")


def _skipped_frame(n: int) -> str:
    return f"---\n\n### {n} {_plural(n, 'issue')} described here"


def _digest_pieces(entries: Sequence[SkippedEntry]) -> str:
    if not entries:
        return ""
    lead, *others = entries
    location = _skipped_location(lead.filepath, lead.finding)
    sections = render_group_sections(
        lead.finding,
        [entry.finding for entry in others],
        additional_sites=lead.additional_sites,
    )
    return neutralize_comment_openers(
        f"\n\n#### {prepare_location(location)}\n\n{sections}"
    )


def _ordered_skipped_groups(
    groups: Sequence[Sequence[SkippedEntry]],
) -> tuple[Sequence[SkippedEntry], ...]:
    return tuple(group for group in groups if group)


def digest_groups(
    groups: Sequence[Sequence[SkippedEntry]], fits: Callable[[str], bool]
) -> Iterator[tuple[Sequence[SkippedEntry], str | None]]:
    """Yield whole issue groups without exposing other members' prose."""
    for group in _ordered_skipped_groups(groups):
        fragment = _digest_pieces(group)
        yield group, fragment if fits(fragment) else None


def _closing_line(m: int, n: int) -> str:
    return f"_{m} of these {n} {_plural(n, 'issue')} did not fit in this comment._"


def compose_inline_body(
    sections: str, *, platform: Platform, surface: Surface, marker_suffix: str = ""
) -> InlineBody:
    limits = body_limit(platform, surface)
    trailer = f"\n\n{BRAND_TRAILER}"
    allowance = limits.bytes - utf8_len(trailer) - utf8_len(marker_suffix)
    if utf8_len(sections) <= allowance:
        return InlineBody(sections + trailer, 0)
    folded, folded_bytes = fold_inline_body(sections, allowance, platform, surface)
    return InlineBody(folded + trailer, folded_bytes)


def _bounded_section(
    n: int,
    shown_groups: Sequence[Sequence[SkippedEntry]],
) -> str:
    section = _skipped_frame(n)
    section += "".join(_digest_pieces(group) for group in shown_groups)
    omitted = n - len(shown_groups)
    if omitted:
        section += f"\n\n{_closing_line(omitted, n)}"
    return section


def _compose_fragments(review_body: str, section: str, footer: str) -> str:
    fragments = [BRAND_SUMMARY_HEADER]
    if review_body:
        fragments.append(review_body)
    if section:
        fragments.append(section)
    return "\n\n".join(fragments) + footer


def _standalone_footer_lines(review_body: str) -> str:
    footer_prefix = build_prose_footer("")
    prefixes = (footer_prefix, f"Generated by {LEGACY_PRODUCT} | Reviewed up to: ")
    return "\n".join(
        line.lstrip(" \t")
        for line in review_body.split("\n")
        if line.lstrip(" \t").startswith(prefixes)
    )


def _skipped_reserve(n: int) -> int:
    return utf8_len(_skipped_frame(n)) + utf8_len(_closing_line(n, n))


def _summary_ref(finding: Mapping[str, object], index: int | None) -> str | int:
    value = finding.get("id")
    if isinstance(value, str) and value:
        return value
    if index is None:
        raise ReportShapeError("Summary lead has neither an id nor a source index.")
    return index


def render_summary(
    summary: ReviewSummary,
    findings: Sequence[Finding],
    skipped_groups: Sequence[Sequence[SkippedEntry]],
) -> str:
    if not isinstance(summary, dict) or set(summary) != {
        "counts",
        "index",
        "omitted",
        "reasons",
    }:
        raise ReportShapeError("Invalid review_summary contract.")
    if (
        not isinstance(summary["counts"], str)
        or not isinstance(summary["index"], list)
        or not gate.is_plain_int(summary["omitted"])
        or summary["omitted"] < 0
        or not isinstance(summary["reasons"], list)
        or not all(isinstance(reason, str) for reason in summary["reasons"])
    ):
        raise ReportShapeError("Invalid review_summary fields.")
    positions = {id(finding): index for index, finding in enumerate(findings)}
    groups = consolidate_delivery(findings)
    known = {
        _summary_ref(group.primary, positions[id(group.primary)]) for group in groups
    }
    expected = {
        _summary_ref(group.primary, positions[id(group.primary)])
        for group in groups
        if delivery_allows(group.primary, "inline")
    }
    seen: set[str | int] = set()
    for row in summary["index"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"lead_id", "bullet"}
            or not isinstance(row["bullet"], str)
        ):
            raise ReportShapeError("Invalid summary bullet record.")
        ref = row["lead_id"]
        if not (isinstance(ref, str) or gate.is_plain_int(ref)) or ref not in known:
            raise ReportShapeError("Unknown summary lead id.")
        if ref in seen:
            raise ReportShapeError("Duplicate summary lead id.")
        seen.add(ref)
    if expected - seen:
        raise ReportShapeError("Missing summary bullet for a delivered lead.")
    texts = [
        summary["counts"],
        *summary["reasons"],
        *(row["bullet"] for row in summary["index"]),
    ]
    if any(prepare_prose(text) != text for text in texts):
        raise ReportShapeError("Uncontained summary text.")
    blocked = {
        _summary_ref(group[0].finding, group[0].finding_index)
        for group in skipped_groups
    }
    bullets: list[str] = []
    size = 0
    omitted = summary["omitted"]
    reasons = list(summary["reasons"])
    cut = False
    for row in summary["index"]:
        if row["lead_id"] not in expected or row["lead_id"] in blocked:
            continue
        added = len(row["bullet"]) + bool(bullets)
        if cut or size + added > registry.REPORT_FOLD_LIMITS["summaryIndexChars"]:
            cut = True
            omitted += 1
            continue
        bullets.append(row["bullet"])
        size += added
    body = summary["counts"] + ("\n\n" + "\n".join(bullets) if bullets else "")
    if cut:
        reasons.append("did not fit in this comment")
    if omitted:
        body += "\n\n" + registry.PUBLIC_SUMMARY["remainder"].format(
            count=omitted, issues=_plural(omitted, "issue"), reasons="; ".join(reasons)
        )
    if prepare_prose(body) != body:
        raise ReportShapeError("Uncontained summary text.")
    return body


def legacy_summary(findings: Sequence[Finding]) -> ReviewSummary:
    groups = consolidate_delivery(
        [finding for finding in findings if delivery_allows(finding, "body")]
    )
    delivered = [
        delivery_finding(group.primary, group.corroborators) for group in groups
    ]
    severities = [
        normalize_report_severity(finding.get("severity"), registry.SEVERITY_EMOJI)
        for finding in delivered
    ]
    breakdown = ", ".join(
        f"{severities.count(severity)} {severity}"
        for severity in registry.SEVERITY_ORDER
        if severity in severities
    )
    n = len(groups)
    sentence = registry.PUBLIC_SUMMARY["counts"].format(
        count=n,
        issues=_plural(n, "issue"),
        breakdown=f": {breakdown}" if breakdown else "",
    )
    suggestions = sum(
        finding.get("report_tag", finding.get("report_destination")) == "suggestion"
        for finding in delivered
    )
    if suggestions == 1:
        sentence += registry.PUBLIC_SUMMARY["improvement"].format(count=suggestions)
    elif suggestions > 1:
        sentence += registry.PUBLIC_SUMMARY["improvements"].format(count=suggestions)
    positions = {id(finding): index for index, finding in enumerate(findings)}
    index: list[SummaryBullet] = []
    for group, finding in zip(groups, delivered, strict=True):
        if not delivery_allows(group.primary, "inline"):
            continue
        severity = normalize_report_severity(
            finding.get("severity"), registry.SEVERITY_EMOJI
        )
        bullet = registry.PUBLIC_SUMMARY["index"].format(
            mark=registry.SEVERITY_EMOJI[severity],
            severity=severity.upper(),
            location=prepare_location(location_text(finding)),
            suggestion=registry.PUBLIC_SUMMARY["suggestion"]
            if finding.get("report_tag", finding.get("report_destination"))
            == "suggestion"
            else "",
            title=prepare_summary_title(finding.get("title", "")),
        )
        index.append(
            {
                "lead_id": _summary_ref(group.primary, positions[id(group.primary)]),
                "bullet": prepare_prose(bullet),
            }
        )
    return {"counts": sentence, "index": index, "omitted": 0, "reasons": []}


def compose_review_body(
    review_body: object,
    skipped_groups: Sequence[Sequence[SkippedEntry]],
    *,
    platform: Platform,
    findings_count: int,
    sha: object,
    index_findings: Sequence[Finding] = (),
    review_summary: ReviewSummary | None = None,
) -> ComposedBody:
    # Standalone prepared summary lines alone drive same-SHA footer deduplication.
    # Reserve the complete envelope; groups are indivisible and prose folds first.
    skipped_groups = _ordered_skipped_groups(skipped_groups)
    review_body = (
        render_summary(review_summary, index_findings, skipped_groups)
        if review_summary is not None
        else prepare_prose(review_body)
    )
    limits = body_limit(platform)
    skipped = [entry for group in skipped_groups for entry in group]
    n = len(skipped_groups)
    section = _bounded_section(n, skipped_groups) if n else ""
    footer = build_footer(
        findings_count,
        sha,
        prose_body=_standalone_footer_lines(review_body),
    )
    body = _compose_fragments(review_body, section, footer)
    if utf8_len(body) <= limits.bytes:
        return ComposedBody(body, n, 0, 0, (), tuple(skipped))

    footer = build_footer(findings_count, sha)
    fixed = utf8_len(BRAND_SUMMARY_HEADER) + utf8_len(footer)
    if review_body:
        fixed += utf8_len("\n\n")
    if n:
        fixed += utf8_len("\n\n\n\n") + _skipped_reserve(n)
    allowance = limits.bytes - fixed

    if review_body and utf8_len(review_body) > allowance:
        effective_body, folded_bytes = fold_review_body(
            review_body, allowance, platform
        )
        section = _bounded_section(n, []) if n else ""
        body = _compose_fragments(effective_body, section, footer)
        return ComposedBody(
            body,
            0,
            n,
            folded_bytes,
            _omitted_entries([group[0] for group in skipped_groups]),
            (),
        )

    remaining = allowance - (utf8_len(review_body) if review_body else 0)
    shown_groups: list[Sequence[SkippedEntry]] = []
    omitted_groups: list[Sequence[SkippedEntry]] = []

    def fits(fragment: str) -> bool:
        nonlocal remaining
        group_size = utf8_len(fragment)
        if group_size > remaining:
            return False
        remaining -= group_size
        return True

    for group, fragment in digest_groups(skipped_groups, fits):
        if fragment is not None:
            shown_groups.append(group)
        else:
            omitted_groups.append(group)
    section = _bounded_section(n, shown_groups) if n else ""
    body = _compose_fragments(review_body, section, footer)
    return ComposedBody(
        body,
        len(shown_groups),
        len(omitted_groups),
        0,
        _omitted_entries([group[0] for group in omitted_groups]),
        tuple(entry for group in shown_groups for entry in group),
    )


def finding_key(filepath: object, line: object, title: object, body: str) -> str:
    # Visible content makes edited findings distinct even without upstream ids.
    material = "\x00".join((str(filepath), str(line), str(title), body))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def consolidate_delivery(findings: Sequence[Finding]) -> list[Group]:
    members_by_group: list[list[Finding]] = []
    key_to_group: dict[str, int] = {}
    for finding in findings:
        stamp = finding.get("delivery_group")
        key = stamp["issue_key"] if stamp else finding.get("consolidation_key")
        if not key or not delivery_allows(finding, "body"):
            members_by_group.append([finding])
            continue
        index = key_to_group.get(key)
        if index is None:
            index = len(members_by_group)
            key_to_group[key] = index
            members_by_group.append([finding])
        else:
            members_by_group[index].append(finding)

    groups: list[Group] = []
    for members in members_by_group:
        stamp = members[0].get("delivery_group")
        if stamp:
            finding_ids = stamp["finding_ids"]
            members.sort(key=lambda member: finding_ids.index(str(member.get("id"))))
        primary_index = next(
            (
                index
                for index, member in enumerate(members)
                if (
                    member.get("id") == stamp["lead_id"]
                    if stamp
                    else member.get("consolidation_primary")
                )
            ),
            0,
        )
        primary = members[primary_index]
        corroborators = tuple(
            member for index, member in enumerate(members) if index != primary_index
        )
        groups.append(Group(primary, corroborators))
    return groups
