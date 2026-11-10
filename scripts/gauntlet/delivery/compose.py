"""Pure delivery bodies, grouping, and key material."""

import hashlib
import re
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
from gauntlet.delivery.input import Finding
from gauntlet.location import canonical_range, delivery_allows
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
    "digest_groups",
    "finding_key",
    "key_material_body",
    "render_comment_body",
    "render_finding_sections",
    "render_group_body",
    "render_group_sections",
    "summary_body_from_report",
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


def render_group_sections(
    primary: Mapping[str, object],
    corroborators: Sequence[Mapping[str, object]],
    *,
    fence_offsets: FenceOffsets | None = None,
    style: CommentStyle | None = None,
    additional_sites: Sequence[Mapping[str, object]] | None = None,
) -> str:
    body = render_finding_sections(primary, fence_offsets=fence_offsets, style=style)
    sites = (
        group_sites(primary, corroborators)
        if additional_sites is None
        else additional_sites
    )
    if sites:
        body += "\n\n**Additional sites:**\n" + "\n".join(
            f"- {prepare_location(location_text(site))}" for site in sites
        )
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
    return (
        f"---\n\n### {n} {_plural(n, 'issue')} could not be attached to a changed line, "
        "so it is described here"
        if n == 1
        else f"---\n\n### {n} issues could not be attached to a changed line, so they are described here"
    )


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


def _index_identity(finding: Mapping[str, object]) -> re.Pattern[str] | None:
    if canonical_range(finding) is None:
        return None
    where = re.escape(prepare_location(location_text(finding)))
    severity = normalize_report_severity(
        finding.get("severity"), registry.SEVERITY_EMOJI
    )
    values = {
        "mark": re.escape(
            registry.SEVERITY_EMOJI.get(severity, registry.SEVERITY_EMOJI_FALLBACK)
        ),
        "severity": severity.upper(),
        "location": rf"(?:{where}|\[{where}\]\([^\n]*\))",
        "suggestion": re.escape(
            registry.PUBLIC_SUMMARY["suggestion"]
            if finding.get("report_tag", finding.get("report_destination"))
            == "suggestion"
            else ""
        ),
        "title": re.escape(prepare_summary_title(finding.get("title", ""))),
    }
    pattern = re.escape(registry.PUBLIC_SUMMARY["index"])
    for key, value in values.items():
        pattern = pattern.replace(re.escape("{" + key + "}"), value)
    return re.compile(pattern)


def _index_matches(line: str, identity: re.Pattern[str]) -> bool:
    return identity.fullmatch(line) is not None


def _validate_public_index(review_body: str, findings: Sequence[Finding]) -> None:
    prefix = registry.PUBLIC_SUMMARY["counts"].split("{count}")[0]
    if not review_body.startswith(prefix):
        return
    identities = [
        identity
        for group in consolidate_delivery(findings)
        if (identity := _index_identity(group.primary)) is not None
    ]
    for line in review_body.split("\n")[1:]:
        if not line or re.fullmatch(
            r"[0-9]+ more issues? not listed here \([^\n]*\)\.", line
        ):
            continue
        if not any(_index_matches(line, identity) for identity in identities):
            raise ReportShapeError(
                "Summary index does not match its findings; regenerate the review summary."
            )


def _without_body_index(
    review_body: str,
    groups: Sequence[Sequence[SkippedEntry]],
    index_findings: Sequence[Finding],
) -> str:
    # Placement can fail after the workflow rendered its index, so reconcile exact owned bullets.
    _validate_public_index(review_body, index_findings)
    identities = []
    for group in groups:
        finding = group[0].finding
        originals = [
            original
            for original in index_findings
            if all(
                original.get(field) == finding.get(field)
                for field in ("id", "file", "title")
            )
        ]
        if len(originals) == 1:
            finding = originals[0]
        identity = _index_identity(finding)
        if identity is not None:
            identities.append(identity)
    lines = review_body.split("\n")
    kept = [
        line
        for line in lines
        if not any(_index_matches(line, identity) for identity in identities)
    ]
    return "\n".join(kept).rstrip() if len(kept) != len(lines) else review_body


def summary_for_delivery(report_summary: str, findings: Sequence[Finding]) -> str:
    """Legacy report inputs need the same public counts and inline-only index."""
    groups = consolidate_delivery(
        [finding for finding in findings if delivery_allows(finding, "body")]
    )
    severities = [
        normalize_report_severity(
            group.primary.get("severity"), registry.SEVERITY_EMOJI
        )
        for group in groups
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
        group.primary.get("report_tag", group.primary.get("report_destination"))
        == "suggestion"
        for group in groups
    )
    if suggestions == 1:
        sentence += registry.PUBLIC_SUMMARY["improvement"].format(count=suggestions)
    elif suggestions > 1:
        sentence += registry.PUBLIC_SUMMARY["improvements"].format(count=suggestions)
    identities = [
        identity
        for group in groups
        if delivery_allows(group.primary, "inline")
        and (identity := _index_identity(group.primary)) is not None
    ]
    bullets = [
        line
        for line in report_summary.split("\n")
        if any(_index_matches(line, identity) for identity in identities)
    ]
    body = sentence + ("\n\n" + "\n".join(bullets) if bullets else "")
    missing = len(identities) - len(bullets)
    if missing > 0:
        body += f"\n\n{missing} more {_plural(missing, 'issue')} not listed here (did not fit in this comment)."
    return body


def compose_review_body(
    review_body: object,
    skipped_groups: Sequence[Sequence[SkippedEntry]],
    *,
    platform: Platform,
    findings_count: int,
    sha: object,
    index_findings: Sequence[Finding] = (),
) -> ComposedBody:
    # Standalone prepared summary lines alone drive same-SHA footer deduplication.
    # Reserve the complete envelope; groups are indivisible and prose folds first.
    skipped_groups = _ordered_skipped_groups(skipped_groups)
    review_body = _without_body_index(
        prepare_prose(review_body), skipped_groups, index_findings
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


def summary_body_from_report(report: str) -> str:
    marker = "## Summary\n\n"
    start = report.find(marker)
    if start < 0:
        raise ReportShapeError("Report does not contain a rendered Summary section.")
    body_start = start + len(marker)
    cursor = body_start
    while cursor <= len(report):
        end = report.find("\n", cursor)
        if end < 0:
            line = report[cursor:]
            next_cursor = len(report) + 1
        else:
            line = report[cursor:end]
            next_cursor = end + 1
        if line.endswith("\r"):
            line = line[:-1]
        if line in registry.CODE_OWNED_HEADINGS:
            body = report[body_start:cursor]
            return body[:-2] if body.endswith("\n\n") else body
        cursor = next_cursor
    raise ReportShapeError(
        "Report does not contain a following code-owned heading after Summary."
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
