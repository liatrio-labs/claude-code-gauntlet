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
    "build_skipped_section",
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


def _render_corroboration(finding: Mapping[str, object]) -> str:
    agent = prepare_line(finding.get("agent", "unknown"))
    dimension = prepare_line(finding.get("dimension", "unknown"))
    confidence = finding.get("confidence")
    conf_text = prepare_line(confidence) if confidence is not None else "?"
    raw_title = finding.get("title")
    title = prepare_line(raw_title) if isinstance(raw_title, str) else ""
    body = prepare_prose(finding.get("body", ""))
    parts = [
        f"**Corroborating finding — {agent} ({dimension}, confidence {conf_text}):**",
        "",
        f"**{title or 'Finding'}**",
    ]
    if body:
        parts += ["", body]
    return "\n".join(parts)


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


def render_group_sections(
    primary: Mapping[str, object],
    corroborators: Sequence[Mapping[str, object]],
    *,
    fence_offsets: FenceOffsets | None = None,
    style: CommentStyle | None = None,
) -> str:
    # Corroborators cannot supply patches or choose the primary's anchor offsets.
    body = render_finding_sections(primary, fence_offsets=fence_offsets, style=style)
    seen: set[str] = set()
    sites = []
    for finding in (primary, *corroborators):
        for site in cast(Sequence[Mapping[str, object]], finding.get("sites", [])):
            if not delivery_allows(
                {
                    **site,
                    "disposition": finding.get("disposition"),
                    "admission": finding.get("admission"),
                },
                "sourceLink",
            ):
                continue
            key = site_key(site)
            if key in seen:
                continue
            seen.add(key)
            sites.append(
                f"- {prepare_location(str(site['file']) + ':' + str(site['line_start']) + '-' + str(site['line_end']))}"
            )
    if sites:
        body += "\n\n**Additional sites:**\n" + "\n".join(sites)
    if not corroborators:
        return body
    section = "\n\n".join(_render_corroboration(c) for c in corroborators)
    section = neutralize_comment_openers(section)
    return f"{body}\n\n---\n\n{section}"


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


def _skipped_frame(n: int, shown: int, inline_count: int | None) -> str:
    group_note = (
        " A finding listed here may not have an anchoring problem of its own — a "
        "consolidation group whose primary could not be anchored inline is listed "
        "here in full, corroborators included."
    )
    if inline_count is None:
        intro = (
            f"The following {shown} {_plural(shown, 'finding')} "
            f"{_plural(shown, 'references', 'reference')} lines outside this diff and "
            f"{_plural(shown, 'is', 'are')} included here instead of as inline "
            f"comments:{group_note}"
        )
    else:
        intro = (
            f"{inline_count} inline {_plural(inline_count, 'comment')} "
            f"{_plural(inline_count, 'was', 'were')} posted; the following {shown} "
            f"{_plural(shown, 'finding')} {_plural(shown, 'references', 'reference')} "
            f"lines outside this diff and {_plural(shown, 'is', 'are')} included here "
            f"instead:{group_note}"
        )
    return "\n".join(
        [
            "---",
            "",
            f"### ⚠️ {n} {_plural(n, 'finding')} could not be anchored inline",
            "",
            intro,
        ]
    )


def _skipped_piece(filepath: object, finding: Mapping[str, object]) -> str:
    # Neutralize the assembled heading as well as its prepared finding fields.
    location = _skipped_location(filepath, finding)
    piece = (
        f"\n\n#### {prepare_location(location)}\n\n{render_group_sections(finding, [])}"
    )
    return neutralize_comment_openers(piece)


def _digest_dimension(finding: Mapping[str, object]) -> str | None:
    if finding.get("disposition") == "body":
        return prepare_line(finding.get("dimension", "unknown"))
    return None


def _digest_pieces(entries: Sequence[SkippedEntry], dimensions: set[str]) -> str:
    pieces = []
    for entry in entries:
        dimension = _digest_dimension(entry.finding)
        if dimension is not None and dimension not in dimensions:
            pieces.append(f"\n\n### {dimension}")
            dimensions.add(dimension)
        pieces.append(_skipped_piece(entry.filepath, entry.finding))
    return "".join(pieces)


def _ordered_skipped_groups(
    groups: Sequence[Sequence[SkippedEntry]],
) -> tuple[Sequence[SkippedEntry], ...]:
    return tuple(
        sorted(
            groups,
            key=lambda group: (
                _digest_dimension(group[0].finding) or "" if group else ""
            ),
        )
    )


def digest_groups(
    groups: Sequence[Sequence[SkippedEntry]], fits: Callable[[str], bool]
) -> Iterator[tuple[Sequence[SkippedEntry], str | None]]:
    """Yield ordered groups with accepted fragments, or None for omissions."""
    dimensions: set[str] = set()
    for group in _ordered_skipped_groups(groups):
        proposed = set(dimensions)
        fragment = _digest_pieces(group, proposed)
        if fits(fragment):
            dimensions = proposed
            yield group, fragment
        else:
            yield group, None


def _closing_line(m: int, n: int, platform: Platform) -> str:
    limits = body_limit(platform)
    return (
        f"_{m} of these {n} {_plural(n, 'finding')} {_plural(m, 'is', 'are')} "
        f"not shown: this {limits.surface} "
        f"reached the {limits.bytes}-byte {limits.label} body limit._"
    )


def build_skipped_section(
    skipped: Sequence[SkippedEntry], inline_count: int | None = None
) -> str:
    # GitLab summaries precede inline posts, so they cannot supply a landed count.
    if not skipped:
        return ""
    n = len(skipped)
    return _skipped_frame(n, n, inline_count) + _digest_pieces(skipped, set())


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
    shown_entries: Sequence[SkippedEntry],
    inline_count: int | None,
    platform: Platform,
) -> str:
    section = _skipped_frame(n, len(shown_entries), inline_count)
    section += _digest_pieces(shown_entries, set())
    omitted = n - len(shown_entries)
    if omitted:
        section += f"\n\n{_closing_line(omitted, n, platform)}"
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


def _skipped_reserve(n: int, inline_count: int | None, platform: Platform) -> int:
    # The widest count and both grammar forms bound every possible shown count.
    frame = max(utf8_len(_skipped_frame(n, shown, inline_count)) for shown in (0, n))
    closing = utf8_len(_closing_line(n, n, platform))
    return frame + closing


def compose_review_body(
    review_body: object,
    skipped_groups: Sequence[Sequence[SkippedEntry]],
    *,
    platform: Platform,
    findings_count: int,
    sha: object,
    inline_count: int | None = None,
) -> ComposedBody:
    # Standalone prepared summary lines alone drive same-SHA footer deduplication.
    # Reserve the complete envelope; groups are indivisible and prose folds first.
    skipped_groups = _ordered_skipped_groups(skipped_groups)
    review_body = prepare_prose(review_body)
    limits = body_limit(platform)
    skipped = [entry for group in skipped_groups for entry in group]
    n = len(skipped)
    section = build_skipped_section(skipped, inline_count)
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
        fixed += utf8_len("\n\n\n\n") + _skipped_reserve(n, inline_count, platform)
    allowance = limits.bytes - fixed

    if review_body and utf8_len(review_body) > allowance:
        effective_body, folded_bytes = fold_review_body(
            review_body, allowance, platform
        )
        section = _bounded_section(n, [], inline_count, platform) if n else ""
        body = _compose_fragments(effective_body, section, footer)
        return ComposedBody(
            body,
            0,
            n,
            folded_bytes,
            _omitted_entries(skipped),
            (),
        )

    remaining = allowance - (utf8_len(review_body) if review_body else 0)
    shown_entries: list[SkippedEntry] = []
    omitted_entries: list[SkippedEntry] = []

    def fits(fragment: str) -> bool:
        nonlocal remaining
        group_size = utf8_len(fragment)
        if group_size > remaining:
            return False
        remaining -= group_size
        return True

    for group, fragment in digest_groups(skipped_groups, fits):
        if fragment is not None:
            shown_entries.extend(group)
        else:
            omitted_entries.extend(group)
    section = _bounded_section(n, shown_entries, inline_count, platform) if n else ""
    body = _compose_fragments(review_body, section, footer)
    return ComposedBody(
        body,
        len(shown_entries),
        len(omitted_entries),
        0,
        _omitted_entries(omitted_entries),
        tuple(shown_entries),
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
