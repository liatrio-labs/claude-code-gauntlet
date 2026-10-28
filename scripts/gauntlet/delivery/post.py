#!/usr/bin/env python3
"""Deterministic PR/MR review delivery and dry-run capture."""

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, NoReturn, TypedDict, cast

from gauntlet import diff, proc
from gauntlet.cli import CliError, Command, Parser, warn
from gauntlet.delivery import compose, gate
from gauntlet.delivery.fold import (
    Platform,
    Surface,
    body_limit,
    utf8_len,
)
from gauntlet.delivery.gate import ApplySite, FixVerdict
from gauntlet.delivery.input import Finding, ReviewInput, validate_review_input
from gauntlet.diff import DiffFacts
from gauntlet.forge import (
    Forge,
    ForgeUnavailable,
    GitLab,
    PostRequest,
    ReviewTarget,
    detect_platform,
    gh_hostname,
    github_review_request,
    gitlab_discussion_request,
    gitlab_note_request,
    make_forge,
    origin_remote,
)
from gauntlet.fs import JsonReadError, read_json
from gauntlet.location import (
    anchor_matches_span,
    canonical_range,
    location_allows,
    normalize_location,
)
from gauntlet.marker import (
    SHA_RE,
    build_finding_marker,
    is_sha_shaped,
)
from gauntlet.prior_review import PriorDelivery, gitlab_prior_delivery_state
from gauntlet.text import prepare_line


class _GitHubCommentRequired(TypedDict):
    path: str
    line: int
    side: Literal["RIGHT"]
    body: str


class GitHubComment(_GitHubCommentRequired, total=False):
    start_line: int
    start_side: Literal["RIGHT"]


class GitHubReviewPayload(TypedDict):
    body: str
    event: Literal["COMMENT"]
    comments: list[GitHubComment]


class _GitLabPositionRequired(TypedDict):
    position_type: Literal["text"]
    base_sha: str
    head_sha: str
    start_sha: str
    new_path: str
    new_line: int


class GitLabPosition(_GitLabPositionRequired, total=False):
    old_line: int
    old_path: str


class GitLabDiscussionPayload(TypedDict):
    body: str
    position: GitLabPosition


class GitLabNotePayload(TypedDict):
    body: str


class GitHubDryRunPayload(TypedDict):
    platform: Literal["github"]
    endpoint: str
    method: Literal["POST"]
    payload: GitHubReviewPayload
    skipped: list[str]


class GitLabDryRunPayload(TypedDict):
    platform: Literal["gitlab"]
    summary: GitLabNotePayload
    discussions: list[GitLabDiscussionPayload | GitLabNotePayload]
    skipped: list[str]


DeliveryOutcome = Literal["posted", "already_present", "invalid", "failed"]


@dataclass(frozen=True, slots=True)
class Anchor:
    path: str
    line: int
    site: ApplySite


@dataclass(frozen=True, slots=True)
class AnchoredFinding:
    index: int
    finding: Finding
    path: str
    verdict: FixVerdict | None
    anchor: Anchor


@dataclass(frozen=True, slots=True)
class UnanchoredFinding:
    index: int
    finding: Finding
    path: str
    verdict: FixVerdict | None
    platform: Platform


FindingPlan = AnchoredFinding | UnanchoredFinding


@dataclass(frozen=True, slots=True)
class AnchoredGroup:
    group: compose.Group
    primary: AnchoredFinding


@dataclass(frozen=True, slots=True)
class DegradedGroup:
    group: compose.Group
    members: tuple[UnanchoredFinding, ...]
    warning: str


GroupPlan = AnchoredGroup | DegradedGroup


@dataclass(frozen=True, slots=True)
class InlinePlan:
    body: compose.InlineBody
    budget_error: str | None


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    groups: tuple[GroupPlan, ...]
    winning_ranges: tuple[gate.OverlapCandidate, ...]


@dataclass(frozen=True, slots=True)
class SummaryPlan:
    summary: compose.ComposedBody
    sha: str
    budget_error: str | None


class DeliverySession:
    def __init__(self, *, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.captured: list[PostRequest] = []
        self.skipped: list[str] = []
        self.kept_fixes = 0
        self.downgraded_fixes = 0

    def activate(self, member: FindingPlan) -> Finding:
        finding, verdict = member.finding, member.verdict
        # Corroborations carry no fence and must not count as render-site verdicts.
        if "suggested_fix_code" not in finding:
            return finding
        assert verdict is not None
        if verdict.keep:
            self.kept_fixes += 1
            return finding
        reason = verdict.downgrade_reason
        self.downgraded_fixes += 1
        self.warn_skip(gate.format_fix_warning(finding, reason, label="suggested-fix"))
        stripped = finding.copy()
        del stripped["suggested_fix_code"]
        return stripped

    def warn_skip(self, msg: str) -> None:
        self.skipped.append(msg)
        warn(msg)

    def try_post(
        self, request: PostRequest, *, forge: Forge
    ) -> tuple[object, str | None]:
        # A rejected discussion must leave sibling deliveries retryable.
        if self.dry_run:
            self.captured.append(request)
            return {}, None
        result = forge.submit(request)
        if result.warning is not None:
            warn(result.warning)
        return result.response, result.error

    def post(self, request: PostRequest, *, forge: Forge) -> object:
        # A rejected review or first summary fails the whole delivery.
        response, error = self.try_post(request, forge=forge)
        if error is not None:
            die(error)
        return response

    def print_fix_summary(self) -> None:
        # Render-site verdicts include deduplicated reruns, independent of HTTP success.
        kept = self.kept_fixes
        downgraded = self.downgraded_fixes
        if not (kept or downgraded):
            return
        print(f"  {kept} suggested fix(es) passed the apply-check.")
        print(f"  {downgraded} suggested fix(es) downgraded to prose.")

    def dry_run_payload(
        self, platform: Platform
    ) -> GitHubDryRunPayload | GitLabDryRunPayload:
        # GitLab captures rendered bodies; only live deliveries append finding markers.
        if platform == "github":
            cap = self.captured[0]
            return {
                "platform": "github",
                "endpoint": cap.endpoint,
                "method": cap.method,
                "payload": cast(GitHubReviewPayload, cap.payload),
                "skipped": list(self.skipped),
            }

        summary = self.captured[0].payload
        discussions = [cap.payload for cap in self.captured[1:]]
        return {
            "platform": "gitlab",
            "summary": cast(GitLabNotePayload, summary),
            "discussions": cast(
                list[GitLabDiscussionPayload | GitLabNotePayload], discussions
            ),
            "skipped": list(self.skipped),
        }

    def write_dry_run_payload(self, platform: Platform, findings_path: str) -> str:
        payload = self.dry_run_payload(platform)
        out_dir = os.path.dirname(os.path.abspath(findings_path))
        out_path = os.path.join(out_dir, "post-review-payload.json")
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return out_path


def die(msg: str) -> NoReturn:
    raise CliError(msg)


def ensure_available(forge: Forge) -> None:
    try:
        forge.ensure_available()
    except ForgeUnavailable as exc:
        die(str(exc))


def fetch_diff_facts(target: ReviewTarget, *, forge: Forge) -> DiffFacts | None:
    stdout, stderr, rc = forge.diff(target)
    if rc != 0:
        warn(
            f"Could not fetch diff (exit {rc}): {stderr.strip()}. "
            "Locations cannot be checked; findings will appear in the review body."
        )
        return None

    return diff.parse_diff(stdout, policy=diff.posting_policy(forge.platform))


def _site_verdict(
    finding: Mapping[str, object], anchor: Anchor | None, facts: DiffFacts | None
) -> FixVerdict:
    site = anchor.site if anchor is not None else None
    return gate.evaluate_fix(
        finding,
        apply_range=site.apply_range if site is not None else None,
        facts=facts,
        mismatch_reason="span_exceeds_platform_cap"
        if isinstance(site, gate.GitLabApplySite) and site.cap_exceeded
        else "anchor_mismatch",
    )


def _site_platform(site: gate.ApplySite) -> Platform:
    return "github" if isinstance(site, gate.GitHubApplySite) else "gitlab"


def _anchor(
    finding: Finding,
    path: str,
    facts: DiffFacts | None,
    platform: Platform,
) -> Anchor | None:
    if not location_allows(finding, "inline"):
        return None
    line, end = cast(tuple[int, int], canonical_range(finding))
    texts = diff.span_texts(facts, path, line, end) if facts is not None else None
    if texts is None or not anchor_matches_span(finding, texts):
        return None
    if not diff.is_line_valid(facts, path, line):
        return None
    site = (
        gate.github_apply_range(facts, path, line, finding.get("end_line"))
        if platform == "github"
        else gate.gitlab_apply_range(line, finding.get("end_line"), line)
    )
    return Anchor(path, line, site)


def _group_skip_warning(
    group: compose.Group, facts: DiffFacts | None, filepath: str | None
) -> str:
    primary = group.primary
    title = primary.get("title", "?")
    if filepath is None:
        message = f"Finding '{title}' has no line number — skipping."
    else:
        diag = ""
        vl = diff.valid_lines_for_file(facts, filepath)
        if vl is not None:
            diag = f" Valid lines for this file: {vl}"
        message = (
            f"Skipping finding '{title}' at {filepath}:{primary['line']} "
            f"— line not found in diff.{diag}"
        )
    members = [primary, *group.corroborators]
    if len(members) > 1:
        labels = [
            str(m["id"] if m.get("id") is not None else m.get("title", "?"))
            for m in members
        ]
        message += f" [group members: {', '.join(labels)}]"
    return message


def plan_delivery(
    data: ReviewInput, facts: DiffFacts | None, *, platform: Platform
) -> DeliveryPlan:
    findings: list[Finding] = []
    for raw in data["findings"]:
        finding = cast(Finding, normalize_location(raw))
        span = canonical_range(finding)
        if span is not None:
            path = diff.diff_path_spelling(facts, finding.get("file", "?"), span[0])
            texts = diff.span_texts(facts, path, *span) if facts is not None else None
            # Missing diff context limits delivery, not a location proved by Verify.
            if texts is not None and not anchor_matches_span(finding, texts):
                finding = cast(
                    Finding,
                    normalize_location({**finding, "location_status": "unconfirmed"}),
                )
        findings.append(finding)
    indexes = {id(finding): index for index, finding in enumerate(findings)}
    groups: list[GroupPlan] = []
    candidates: list[gate.OverlapCandidate] = []
    for group in compose.consolidate_delivery(findings):
        primary = group.primary
        line = primary.get("line")
        path = primary.get("file", "?")
        if line is not None:
            # GitHub rejects the whole review if paths use the wrong spelling.
            path = diff.diff_path_spelling(facts, path, line)
        anchor = _anchor(primary, path, facts, platform)
        if anchor is None:
            members = tuple(
                UnanchoredFinding(
                    indexes[id(member)],
                    member,
                    path if index == 0 else member.get("file", "?"),
                    _site_verdict(member, None, facts),
                    platform,
                )
                for index, member in enumerate((primary, *group.corroborators))
            )
            groups.append(
                DegradedGroup(
                    group,
                    members,
                    _group_skip_warning(
                        group, facts, path if line is not None else None
                    ),
                )
            )
            continue
        planned = AnchoredFinding(
            indexes[id(primary)],
            primary,
            path,
            _site_verdict(primary, anchor, facts),
            anchor,
        )
        verdict = planned.verdict
        if (
            "suggested_fix_code" in primary
            and verdict is not None
            and verdict.keep
            and verdict.apply_range is not None
        ):
            candidates.append(
                gate.OverlapCandidate(planned.index, anchor.path, verdict.apply_range)
            )
        groups.append(AnchoredGroup(group, planned))
    losers = gate.overlap_losers(candidates)
    for index, group_plan in enumerate(groups):
        if isinstance(group_plan, DegradedGroup):
            continue
        primary_plan = group_plan.primary
        verdict = primary_plan.verdict
        if primary_plan.index in losers and verdict is not None and verdict.keep:
            demoted = gate.demote(verdict)
            groups[index] = replace(
                group_plan,
                primary=replace(primary_plan, verdict=demoted),
            )
    return DeliveryPlan(
        tuple(groups),
        tuple(record for record in candidates if record.index not in losers),
    )


def _activate_degraded(
    group: DegradedGroup, *, session: DeliverySession
) -> tuple[compose.SkippedEntry, ...]:
    session.warn_skip(group.warning)
    return tuple(
        compose.SkippedEntry(
            member.path, member.finding.get("line"), session.activate(member)
        )
        for member in group.members
    )


def resolve_corroborators(
    group: AnchoredGroup, facts: DiffFacts | None
) -> tuple[FindingPlan, ...]:
    members: list[FindingPlan] = []
    platform = _site_platform(group.primary.anchor.site)
    for index, finding in enumerate(group.group.corroborators, 1):
        line = finding.get("line")
        path = finding.get("file", "?")
        anchor = None
        if line is not None:
            path = diff.diff_path_spelling(facts, path, line)
            anchor = _anchor(finding, path, facts, platform)
        members.append(
            AnchoredFinding(index, finding, path, None, anchor)
            if anchor is not None
            else UnanchoredFinding(index, finding, path, None, platform)
        )
    return tuple(members)


def render_inline(
    member: FindingPlan,
    corroborators: Sequence[Finding],
    *,
    surface: Surface,
    plan: DeliveryPlan,
    facts: DiffFacts | None,
    session: DeliverySession,
    marker_suffix: str = "",
) -> InlinePlan:
    positionless = surface == "note"
    platform: Platform
    if isinstance(member, AnchoredFinding):
        platform = _site_platform(member.anchor.site)
        anchor = None if positionless else member.anchor
    else:
        platform = member.platform
        anchor = None
    if positionless or member.verdict is None:
        verdict = _site_verdict(member.finding, anchor, facts)
        # Static winners occupy ranges even after failure; reactive sites claim none.
        if (
            anchor is not None
            and "suggested_fix_code" in member.finding
            and verdict.keep
            and verdict.apply_range is not None
            and any(
                record.path_lookup == anchor.path
                and gate.ranges_overlap(verdict.apply_range, record.apply_range)
                for record in plan.winning_ranges
            )
        ):
            verdict = gate.demote(verdict)
        member = replace(member, verdict=verdict)
    finding = session.activate(member)
    offsets = (
        anchor.site.offsets
        if anchor is not None and isinstance(anchor.site, gate.GitLabApplySite)
        else None
    )
    sections = compose.render_group_sections(
        finding, corroborators, fence_offsets=offsets
    )
    body = compose.compose_inline_body(
        sections, platform=platform, surface=surface, marker_suffix=marker_suffix
    )
    error = _body_budget_error(body.body + marker_suffix, platform, surface)
    if error is not None:
        error += (
            "; nothing was posted."
            if platform == "github"
            else "; skipping this delivery."
        )
    return InlinePlan(body, error)


def plan_summary(
    data: ReviewInput,
    skipped_groups: Sequence[Sequence[compose.SkippedEntry]],
    *,
    platform: Platform,
    inline_count: int | None = None,
) -> SummaryPlan:
    sha = resolve_marker_sha(data)
    summary = compose.compose_review_body(
        data.get("review_body", ""),
        skipped_groups,
        platform=platform,
        findings_count=len(data["findings"]),
        sha=sha,
        inline_count=inline_count,
    )
    error = _body_budget_error(summary.body, platform, "summary")
    return SummaryPlan(summary, sha, error + "; nothing was posted." if error else None)


def _delivery_marker_suffix(sha: object, keys: Sequence[str]) -> str:
    if not is_sha_shaped(sha) or not keys:
        return ""
    return "\n\n" + "\n".join(build_finding_marker(sha, key) for key in keys)


def _body_budget_error(body: str, platform: Platform, surface: Surface) -> str | None:
    limits = body_limit(platform, surface)
    actual = utf8_len(body)
    if actual <= limits.bytes:
        return None
    return (
        f"The composed {limits.surface} is {actual} bytes, over the "
        f"{limits.bytes}-byte {limits.label} body limit"
    )


def _report_inline_budget(
    composed: compose.InlineBody,
    platform: Platform,
    surface: Surface,
    filepath: object,
    line: object,
) -> None:
    # Budget notices leave stdout and dry-run skip capture unchanged.
    if not composed.folded_bytes:
        return
    limits = body_limit(platform, surface)
    path = filepath or "?"
    warn(
        f"Inline body folded by {composed.folded_bytes} bytes at {path}:{line}: "
        f"this {limits.surface} reached the {limits.bytes}-byte "
        f"{limits.label} body limit."
    )


def _report_summary_budget(composed: compose.ComposedBody, platform: Platform) -> None:
    # Budget omissions stay outside captured skip warnings.
    limits = body_limit(platform)
    if composed.omitted:
        print(
            f"  {composed.omitted} skipped finding(s) not shown: the "
            f"{limits.surface} reached the {limits.bytes}-byte "
            f"{limits.label} body limit."
        )
        for entry in composed.omitted_entries:
            warn(
                f"Skipped finding '{entry.title}' at {entry.location} not shown: the "
                f"{limits.surface} reached the {limits.bytes}-byte "
                f"{limits.label} body limit."
            )
    if composed.folded_bytes:
        print(
            f"  review_body folded by {composed.folded_bytes} bytes: the "
            f"{limits.surface} reached the {limits.bytes}-byte "
            f"{limits.label} body limit."
        )


def get_head_sha() -> str:
    stdout, _, rc = proc.output(["git", "rev-parse", "HEAD"])
    return stdout.strip() if rc == 0 else "unknown"


def resolve_marker_sha(data: ReviewInput) -> str:
    # Prefer the reviewed SHA so a later HEAD cannot mislabel the review.
    sha = data.get("sha")
    if isinstance(sha, str) and SHA_RE.fullmatch(sha.strip()):
        return sha.strip()
    head = get_head_sha()
    if not SHA_RE.fullmatch(head):
        # An unreadable marker prevents prior-review detection.
        warn(
            f"could not resolve a commit SHA for the review marker (git returned "
            f"{head!r}); the posted review will not be detectable as a prior review. "
            f"Set the 'sha' field in the findings JSON to avoid this."
        )
    return head


def post_github(
    data: ReviewInput,
    facts: DiffFacts | None,
    *,
    forge: Forge,
    session: DeliverySession,
) -> int:
    data = validate_review_input(cast(dict[str, object], data))
    ensure_available(forge)
    plan = plan_delivery(data, facts, platform="github")
    comments: list[GitHubComment] = []
    skipped_groups: list[tuple[compose.SkippedEntry, ...]] = []
    for group in plan.groups:
        if isinstance(group, DegradedGroup):
            skipped_groups.append(_activate_degraded(group, session=session))
            continue
        primary = group.primary
        anchor = primary.anchor
        inline = render_inline(
            primary,
            group.group.corroborators,
            surface="inline",
            plan=plan,
            facts=facts,
            session=session,
        )
        if inline.budget_error is not None:
            die(inline.budget_error)
        _report_inline_budget(inline.body, "github", "inline", anchor.path, anchor.line)
        comment: GitHubComment = {
            "path": anchor.path,
            "line": anchor.line,
            "side": "RIGHT",
            "body": inline.body.body,
        }
        if isinstance(anchor.site, gate.GitHubApplySite) and anchor.site.multiline:
            comment["start_line"] = anchor.line
            comment["start_side"] = "RIGHT"
            comment["line"] = anchor.site.apply_range[1]
        comments.append(comment)

    finalized = plan_summary(
        data, skipped_groups, platform="github", inline_count=len(comments)
    )
    summary = finalized.summary
    payload: GitHubReviewPayload = {
        "body": summary.body,
        "event": "COMMENT",
        "comments": comments,
    }
    if finalized.budget_error is not None:
        die(finalized.budget_error)
    resp = session.post(github_review_request(_target(data), payload), forge=forge)
    if session.dry_run:
        print("Review captured (dry-run).")
        print(f"  {len(comments)} inline comment(s) captured.")
    else:
        response = cast(Mapping[str, object], resp)
        url = response.get("html_url", response.get("id", "posted"))
        print(f"Review posted: {url}")
        print(f"  {len(comments)} inline comment(s) posted.")
    flat_skipped = [entry for group in skipped_groups for entry in group]
    if flat_skipped:
        shown = f"{summary.shown} of " if summary.omitted else ""
        print(
            f"  {shown}{len(flat_skipped)} finding(s) skipped inline "
            "(lines not in diff) — appended to review body."
        )
    _report_summary_budget(summary, "github")
    session.print_fix_summary()
    return 0


def fetch_gitlab_shas(
    target: ReviewTarget, *, forge: GitLab
) -> tuple[object, object, object]:
    ensure_available(forge)
    result = forge.diff_refs(target)
    if result.error is not None:
        die(result.error)
    versions = cast(Sequence[Mapping[str, object]], result.payload)

    if not versions:
        die("MR versions endpoint returned an empty list.")

    latest = versions[0]
    return (
        latest["base_commit_sha"],
        latest["head_commit_sha"],
        latest["start_commit_sha"],
    )


def gitlab_prior_delivery(
    owner: str,
    repo: str,
    mr_iid: int | str,
    sha: object,
    *,
    forge: Forge,
    session: DeliverySession,
) -> PriorDelivery:
    # Dry runs skip prior delivery reads to capture every finding, summary first.
    # Invalid SHAs cannot deduplicate; fetch failures risk duplicates over lost reviews.
    if session.dry_run or not is_sha_shaped(sha):
        return PriorDelivery(False, frozenset(), frozenset(), None)
    state = gitlab_prior_delivery_state(owner, repo, mr_iid, sha, forge=forge)
    if state.error:
        warn(
            f"could not check for an existing summary note or already-delivered inline "
            f"discussions ({state.error}); posting them."
        )
    return state


def post_gitlab(
    data: ReviewInput,
    facts: DiffFacts | None,
    *,
    forge: GitLab,
    session: DeliverySession,
) -> int:
    data = validate_review_input(cast(dict[str, object], data))
    target = _target(data)
    owner, repo, mr_iid = target.owner, target.repo, target.number

    ensure_available(forge)

    base_sha, head_sha, start_sha = fetch_gitlab_shas(target, forge=forge)

    validated_shas: list[str] = []
    for name, value in (
        ("base_sha", base_sha),
        ("head_sha", head_sha),
        ("start_sha", start_sha),
    ):
        if not isinstance(value, str) or not value.strip():
            die(
                f"MR version {name} is {value!r} — every inline position would be "
                f"rejected. Check that the MR has a version carrying all three SHAs."
            )
        validated_shas.append(value)
    base_sha, head_sha, start_sha = validated_shas

    plan = plan_delivery(data, facts, platform="gitlab")
    skipped_groups = [
        _activate_degraded(group, session=session)
        for group in plan.groups
        if isinstance(group, DegradedGroup)
    ]
    remaining = tuple(
        group for group in plan.groups if isinstance(group, AnchoredGroup)
    )
    finalized = plan_summary(data, skipped_groups, platform="gitlab")
    composed, sha = finalized.summary, finalized.sha
    summary_payload: GitLabNotePayload = {"body": composed.body}
    prior = gitlab_prior_delivery(
        owner, repo, mr_iid, sha, forge=forge, session=session
    )
    delivered_keys = prior.finding_keys
    if prior.summary_posted:
        print(f"MR summary note for {sha} already on the MR — skipping.")
    else:
        if finalized.budget_error is not None:
            die(finalized.budget_error)
        session.post(gitlab_note_request(target, summary_payload), forge=forge)
        print(
            "MR summary note captured (dry-run)."
            if session.dry_run
            else "MR summary note posted."
        )
        _report_summary_budget(composed, "gitlab")

    def member_key(member: FindingPlan) -> str:
        finding = member.finding
        anchor = member.anchor if isinstance(member, AnchoredFinding) else None
        raw_title = finding.get("title")
        title = prepare_line(raw_title) if isinstance(raw_title, str) else ""
        return compose.finding_key(
            anchor.path if anchor is not None else finding.get("file", "?"),
            anchor.line if anchor is not None else finding.get("line"),
            title,
            compose.key_material_body(finding),
        )

    def submit_inline(
        member: FindingPlan,
        inline: InlinePlan,
        marker_suffix: str,
        position: GitLabPosition | None = None,
    ) -> DeliveryOutcome:
        finding = member.finding
        surface: Surface = "discussion" if position is not None else "note"
        filepath = position["new_path"] if position else finding.get("file")
        line = position["new_line"] if position else finding.get("line")
        _report_inline_budget(inline.body, "gitlab", surface, filepath, line)
        body = inline.body.body + ("" if session.dry_run else marker_suffix)
        request = (
            gitlab_discussion_request(target, {"body": body, "position": position})
            if position is not None
            else gitlab_note_request(target, {"body": body})
        )
        _response, error = session.try_post(request, forge=forge)
        if error is not None:
            # A rejected member must not strand siblings after the summary lands.
            title = finding.get("title", "?")
            message = (
                f"Skipping finding '{title}' at {filepath}:{line} "
                "— GitLab rejected the inline discussion."
                if position is not None
                else f"Skipping corroborating finding '{title}' — GitLab "
                "rejected the position-less note."
            )
            session.warn_skip(f"{message}\n{error}")
            return "failed"
        return "posted"

    def deliver(
        member: FindingPlan,
        corroborators: Sequence[Finding],
        keys: tuple[str, ...],
    ) -> DeliveryOutcome:
        # Render before dedup for rerun counts; fold notices follow dedup.
        marker_suffix = _delivery_marker_suffix(sha, keys)
        inline = render_inline(
            member,
            corroborators,
            surface="discussion" if isinstance(member, AnchoredFinding) else "note",
            plan=plan,
            facts=facts,
            session=session,
            marker_suffix=marker_suffix,
        )
        if inline.budget_error is not None:
            warn(inline.budget_error)
            return "failed"
        if all(key in delivered_keys for key in keys):
            return "already_present"
        if isinstance(member, UnanchoredFinding):
            return submit_inline(member, inline, marker_suffix)
        anchor = member.anchor
        filepath, line = anchor.path, anchor.line
        # Never send line_code because GitLab derives it and rejects a synthesized one.
        position: GitLabPosition = {
            "position_type": "text",
            "base_sha": base_sha,
            "head_sha": head_sha,
            "start_sha": start_sha,
            "new_path": filepath,
            "new_line": line,
        }
        old_line = diff.old_line_for(facts, filepath, line)
        if old_line is not None:
            position["old_line"] = old_line
        # A new-file old_path causes HTTP 500 after creation.
        if not diff.is_new_file(facts, filepath):
            position["old_path"] = diff.old_path_for(facts, filepath)

        return submit_inline(member, inline, marker_suffix, position)

    def deliver_corroborator(member: FindingPlan, key: str) -> DeliveryOutcome:
        finding = member.finding
        line = finding.get("line")
        title = finding.get("title", "?")
        # Without a delivered primary, a positionless note would lose group context.
        if line is None:
            session.warn_skip(
                f"Skipping corroborating finding '{title}' — no line number to "
                f"anchor its own discussion on."
            )
            return "invalid"
        if isinstance(member, UnanchoredFinding):
            filepath = member.path
            session.warn_skip(
                f"Skipping corroborating finding '{title}' at {filepath}:{line} "
                f"— line not found in diff."
            )
            return "invalid"
        return deliver(member, (), (key,))

    counters: dict[DeliveryOutcome, int] = {
        "posted": 0,
        "already_present": 0,
        "invalid": 0,
        "failed": 0,
    }
    for group in remaining:
        primary_key = member_key(group.primary)
        # Unanchored members key their raw location, even within a grouped body, so
        # a member keeps one marker identity whether it posts grouped or split.
        corroborators = resolve_corroborators(group, facts)
        member_keys = (primary_key, *(member_key(m) for m in corroborators))
        if primary_key in prior.legacy_group_keys:
            # Unkeyed corroborators make the group body the whole delivery.
            counters["already_present"] += len(member_keys)
            continue
        if any(key in delivered_keys for key in member_keys) and not all(
            key in delivered_keys for key in member_keys
        ):
            counters[
                "already_present"
                if primary_key in delivered_keys
                else deliver(group.primary, (), (primary_key,))
            ] += 1
            for member, key in zip(corroborators, member_keys[1:], strict=True):
                if key in delivered_keys:
                    counters["already_present"] += 1
                elif isinstance(member, AnchoredFinding):
                    counters[deliver_corroborator(member, key)] += 1
                else:
                    counters[deliver(member, (), (key,))] += 1
            continue
        outcome = deliver(group.primary, group.group.corroborators, member_keys)
        if outcome == "failed":
            counters[outcome] += 1
            for member, key in zip(corroborators, member_keys[1:], strict=True):
                counters[deliver_corroborator(member, key)] += 1
        else:
            counters[outcome] += len(member_keys)

    posted = counters["posted"]
    already_present = counters["already_present"]
    invalid = counters["invalid"]
    failed = counters["failed"]

    delivery = "captured" if session.dry_run else "posted"
    print(f"  {posted} inline discussion(s) {delivery}.")
    flat_skipped = [entry for group in skipped_groups for entry in group]
    if flat_skipped:
        print(f"  {len(flat_skipped)} finding(s) skipped.")
    if already_present:
        print(
            f"  {already_present} inline discussion(s) already on the MR from an "
            f"earlier run — left alone."
        )
    if invalid:
        print(f"  {invalid} finding(s) had a malformed position (see warnings above).")
    session.print_fix_summary()

    standing = (
        f" {already_present} from an earlier run remain on the MR."
        if already_present
        else ""
    )

    if failed:
        print(f"  {failed} inline discussion(s) not delivered (see warnings above).")
        if posted == 0:
            # Prior deliveries and invalid positions are not attempts.
            die(
                f"all {failed} finding(s) attempted this run were not delivered "
                f"— nothing new was posted inline.{standing} The MR summary note "
                f"is on the MR; rerunning retries the inline comments without "
                f"duplicating what is already there."
            )

    # A budget failure can leave unanchored corroborators beside captured siblings.
    if session.dry_run:
        return 1 if invalid else 0
    return 0


def _target(data: ReviewInput) -> ReviewTarget:
    return ReviewTarget(
        data["owner"],
        data["repo"],
        data["pr_number"],
    )


def _parser() -> Parser:
    parser = Parser(
        prog="post_review", description="Post code-gauntlet findings as PR/MR comments."
    )
    parser.add_argument(
        "findings_json",
        help="Path to the findings JSON file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Capture the would-be API payloads to post-review-payload.json "
        "(next to the findings file) instead of posting. Line validation "
        "and read-only fetches still run. A captured GitLab discussion body "
        "omits the per-finding delivery marker the live post appends.",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        help="Read the rendered report Summary section as review_body when absent.",
    )
    parser.add_argument("--owner", help="Override the wrapper owner field.")
    parser.add_argument("--repo", help="Override the wrapper repo field.")
    parser.add_argument(
        "--pr-number", type=int, help="Override the wrapper PR/MR number."
    )
    parser.add_argument("--platform", help="Override the wrapper platform field.")
    parser.add_argument("--web-origin", help="Override the wrapper web_origin field.")
    parser.add_argument("--sha", help="Override the wrapper reviewed commit SHA.")
    return parser


_INPUT_FIELDS = ("owner", "repo", "pr_number", "platform", "sha", "web_origin")


def _execute(args: argparse.Namespace) -> int:

    # The environment also enforces capture when a headless caller omits the flag.
    session = DeliverySession(
        dry_run=args.dry_run or os.environ.get("CODE_GAUNTLET_POST_MODE") == "dry-run"
    )

    try:
        loaded = read_json(args.findings_json)
    except JsonReadError as exc:
        if isinstance(exc.cause, FileNotFoundError):
            die(f"Findings file not found: {args.findings_json}")
        if exc.kind == "parse":
            die(f"Invalid JSON in findings file: {exc.cause}")
        raise exc.cause from exc

    if isinstance(loaded, list):
        raw: dict[str, object] = {"review_body": "", "findings": loaded}
    elif isinstance(loaded, dict):
        raw = loaded
    else:
        die("Findings JSON must be an object or an array.")

    for name in _INPUT_FIELDS:
        value = getattr(args, name)
        if value is not None:
            raw[name] = value
    if args.report and not raw.get("review_body"):
        try:
            with open(args.report, encoding="utf-8") as fh:
                report = fh.read()
        except FileNotFoundError:
            die(f"Report file not found: {args.report}")
        try:
            raw["review_body"] = compose.summary_body_from_report(report)
        except compose.ReportShapeError as exc:
            die(str(exc))

    data = validate_review_input(raw)

    platform = data.get("platform")
    if platform:
        platform = platform.lower()
    else:
        detection = detect_platform(origin_remote())
        if detection.platform:
            platform = detection.platform
            print(f"Detected platform: {platform} (from git remote: {detection.host})")
        else:
            die(
                "Could not detect platform from git remote. "
                "Set 'platform' field in findings JSON to 'github' or 'gitlab'."
            )

    delivery_platform: Platform = "github" if platform == "github" else "gitlab"

    web_origin = data.get("web_origin")
    forge = make_forge(
        delivery_platform, gh_hostname(web_origin) if web_origin else None
    )
    facts = fetch_diff_facts(_target(data), forge=forge)

    if isinstance(forge, GitLab):
        status = post_gitlab(data, facts, forge=forge, session=session)
    else:
        status = post_github(data, facts, forge=forge, session=session)

    if session.dry_run:
        out_path = session.write_dry_run_payload(delivery_platform, args.findings_json)
        print(f"Dry run — no comments posted. Payload written to: {out_path}")

    return status


CLI = Command(parser=_parser(), main=_execute)
