#!/usr/bin/env python3
"""
post_review.py — Deterministic PR/MR comment delivery for code-gauntlet.

Usage:
    python3 post_review.py <findings_json_path> [--dry-run] [--report PATH]

    --dry-run captures the would-be GitHub/GitLab API payloads to
    post-review-payload.json (written next to the findings file) instead of
    posting. Line validation and read-only fetches (diff, MR versions) still run.
    One capture is deliberately NOT the live bytes: a GitLab discussion body is
    captured as the rendered comment alone, without the per-finding delivery
    marker the live post appends (see the marker note in post_gitlab).
    --report reads the rendered Summary body when the input wrapper does not
    already carry review_body. The owner/repo/pr-number/platform/sha flags fill
    or override the corresponding wrapper fields, including for a bare findings
    array.

Input JSON schema:
    {
        "review_body": "...",
        "findings": [
            {
                "file": "src/foo.py",
                "line": 42,
                "end_line": 45,          # optional
                "severity": "high",
                "title": "SQL injection risk",
                "body": "...",
                "suggestion": "...",         # optional — **Suggested fix:**; sanitized + redacted; uncapped
                "claude_md_rule": "...",     # optional — **Cited rule:** (wins over spec_text); sanitized, redacted, capped at 500, blockquoted
                "rule_source": "documented_rule", # optional — label key when claude_md_rule is rendered
                "spec_text": "...",          # optional — **Cited rule:** when no claude_md_rule; same treatment
                "suggested_fix_code": "..."  # optional — the ```suggestion fence: a COMMITTABLE patch
                                             #            replacing exactly lines [line, end_line]. Rendered
                                             #            only when the deterministic apply-check passes
                                             #            (see gate.evaluate_fix); a failing patch is
                                             #            downgraded to the prose `suggestion` and the
                                             #            reason recorded. Secret-redacted; outer fence
                                             #            lengthened; payload otherwise byte-exact
                                             #            (structural sanitize off).
            }
        ],
        "platform": "github",            # optional — github.com/gitlab.com hosts only; else required
        "owner": "myorg",
        "repo": "myrepo",
        "pr_number": 7,
        "sha": "0f1e2d3..."              # optional — the commit the review ran
                                         # against. Recorded in the review marker
                                         # when SHA-shaped; falls back to
                                         # `git rev-parse HEAD` when absent, so a
                                         # HEAD that moved between the review and
                                         # the post cannot mislabel the marker.
    }

Platform detection:
    Recognizes validated public github.com/gitlab.com hosts and dot subdomains.
    Override with "platform" field: "github" or "gitlab".

GitHub path:
    Single POST /repos/{owner}/{repo}/pulls/{n}/reviews with comments array,
    event: "COMMENT", via gh api --input.

GitLab path:
    Fetches MR version SHAs (GET /projects/{id}/merge_requests/{iid}/versions).
    Asks gauntlet.prior_review — the only reader — what this SHA's review already left on
    the MR, so a rerun after a partial delivery duplicates neither the summary note nor the
    inline discussions that did land. Posts per-finding discussions with a position object,
    via glab api --input; a rejected position warns and skips that finding rather than
    aborting the batch. Each posted discussion carries a delivery marker keyed on its own
    rendered content, which is what makes the retry recognizable.

    Every position is checked against the diff facts before it is sent OR captured — see
    validate_position for what that does and does not cover. Any malformed position fails
    a --dry-run; live, it is a per-finding loss like a rejection, so it exits non-zero
    only when nothing NEW was posted inline.

Line validation:
    Parses diff to validate each finding line is in the diff. A finding whose line
    cannot be anchored inline (line not in the diff, or no line at all) is not
    dropped: it degrades into a trailing "could not be anchored inline" section on
    the review body / summary note (see compose.build_skipped_section), so every finding
    still reaches the PR/MR unless the complete composed body exceeds its platform
    byte budget. One warning is emitted per skipped group, naming every member.

No external Python dependencies — stdlib only.
"""

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, NoReturn, TypedDict, cast

from gauntlet import diff, proc
from gauntlet.cli import CliError, Command, Parser
from gauntlet.delivery import compose, gate
from gauntlet.delivery.fold import (
    Platform,
    Surface,
    body_limit,
    utf8_len,
)
from gauntlet.delivery.gate import ApplySite, FixReason, FixVerdict, is_plain_int
from gauntlet.diff import DiffFacts
from gauntlet.forge import (
    Forge,
    ForgeUnavailable,
    GitLab,
    PostRequest,
    ReviewTarget,
    detect_platform,
    github_review_request,
    gitlab_discussion_request,
    gitlab_note_request,
    make_forge,
    origin_remote,
)
from gauntlet.fs import JsonReadError, read_json
from gauntlet.marker import (
    SHA_RE,
    build_finding_marker,
    is_sha_shaped,
)
from gauntlet.prior_review import PriorDelivery, gitlab_prior_delivery_state
from gauntlet.text import prepare_line

# ---------------------------------------------------------------------------
# Dry-run capture
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _GitHubCommentRequired(TypedDict):
    path: str
    line: object
    side: Literal["RIGHT"]
    body: str


class GitHubComment(_GitHubCommentRequired, total=False):
    start_line: object
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
    new_line: object


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


Disposition = Literal["inline", "degraded"]
DeliveryOutcome = Literal["posted", "already_present", "invalid", "failed"]


@dataclass(frozen=True, slots=True)
class Anchor:
    path: str
    line: object
    site: ApplySite


@dataclass(frozen=True, slots=True)
class FindingPlan:
    index: int
    finding: Mapping[str, object]
    disposition: Disposition
    path: object
    anchor: Anchor | None
    verdict: FixVerdict | None


@dataclass(frozen=True, slots=True)
class GroupPlan:
    group: compose.Group
    members: tuple[FindingPlan, ...]
    skip_warning: str | None


@dataclass(frozen=True, slots=True)
class InlinePlan:
    finding: FindingPlan
    body: compose.InlineBody
    member_keys: tuple[str, ...]
    marker_suffix: str
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
        self.outcomes: dict[DeliveryOutcome, int] = {
            "posted": 0,
            "already_present": 0,
            "invalid": 0,
            "failed": 0,
        }

    def activate(self, member: FindingPlan) -> Mapping[str, object]:
        finding, verdict = member.finding, member.verdict
        # Corroborations carry no fence and must not count as render-site verdicts.
        if not isinstance(finding, dict) or "suggested_fix_code" not in finding:
            return finding
        assert verdict is not None
        if verdict.keep:
            self.kept_fixes += 1
            return finding
        reason = cast(FixReason, verdict.reason)
        self.downgraded_fixes += 1
        self.warn_skip(gate.format_fix_warning(finding, reason, label="suggested-fix"))
        # Strip only on a shallow copy, preserving unknown caller fields.
        stripped = dict(finding)
        del stripped["suggested_fix_code"]
        return stripped

    def warn_skip(self, msg: str) -> None:
        """Emit a skip warning and record it for dry-run payload capture."""
        self.skipped.append(msg)
        warn(msg)

    def try_post(
        self, request: PostRequest, *, forge: Forge
    ) -> tuple[Mapping[str, object] | None, str | None]:
        """Return response/error without stranding siblings after a rejected position."""
        if self.dry_run:
            self.captured.append(request)
            return {}, None
        result = forge.submit(request)
        if result.warning is not None:
            warn(result.warning)
        return cast(Mapping[str, object] | None, result.response), result.error

    def post(
        self, request: PostRequest, *, forge: Forge
    ) -> Mapping[str, object] | None:
        """Fail the whole delivery when the single review or first summary is rejected."""
        response, error = self.try_post(request, forge=forge)
        if error is not None:
            die(error)
        return response

    def print_fix_summary(self) -> None:
        """Print the run's patch-acceptance readout, or nothing.

        These two lines are the APPLY-CHECK's verdicts at this run's render sites,
        and nothing else — they carry no delivery verb, and read identically live
        and under --dry-run. Delivery is the per-platform count lines' business: a
        GitLab rerun whose discussions are all already on the MR renders (and so
        gates, and so counts) every fence while posting nothing, and "N fences
        posted" was a false claim there. A run that renders nothing counts nothing
        and prints nothing.

        Both halves print together whenever ANY finding carried the field, so
        n/(n+m) is readable from any run's stdout.
        """
        kept = self.kept_fixes
        downgraded = self.downgraded_fixes
        if not (kept or downgraded):
            return
        print(f"  {kept} suggested fix(es) passed the apply-check.")
        print(f"  {downgraded} suggested fix(es) downgraded to prose.")

    def dry_run_payload(
        self, platform: Platform
    ) -> GitHubDryRunPayload | GitLabDryRunPayload:
        """Transform the captured API calls + skip warnings into the payload shape.

        GitHub posts a single review, so the payload exposes ``endpoint`` / ``method``
        / ``payload`` for that one call. GitLab posts a summary note followed by one
        discussion per finding, so the first capture becomes ``summary`` and the rest
        become ``discussions``. A ``discussions`` body is the rendered comment alone —
        the per-finding delivery marker is appended on the live wire only.
        """
        if platform == "github":
            cap = (
                self.captured[0]
                if self.captured
                else PostRequest("github", "", "POST", (), {})
            )
            return {
                "platform": "github",
                "endpoint": cap.endpoint,
                "method": cap.method,
                "payload": cast(GitHubReviewPayload, cap.payload),
                "skipped": list(self.skipped),
            }

        summary = self.captured[0].payload if self.captured else {}
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
        """Write the dry-run payload JSON next to *findings_path*. Returns its path."""
        payload = self.dry_run_payload(platform)
        out_dir = os.path.dirname(os.path.abspath(findings_path))
        out_path = os.path.join(out_dir, "post-review-payload.json")
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return out_path


def die(msg: str) -> NoReturn:
    raise CliError(msg)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def ensure_available(forge: Forge) -> None:
    try:
        forge.ensure_available()
    except ForgeUnavailable as exc:
        die(str(exc))


# ---------------------------------------------------------------------------
# Diff parsing — line validation
# ---------------------------------------------------------------------------


def fetch_diff_facts(target: ReviewTarget, *, forge: Forge) -> DiffFacts | None:
    """Fetch facts, preserving skipped validation on a nonzero status."""
    stdout, stderr, rc = forge.diff(target)
    if rc != 0:
        warn(
            f"Could not fetch diff (exit {rc}): {stderr.strip()}. "
            "Skipping line validation — all findings will be posted."
        )
        return None

    return diff.parse_diff(stdout, policy=diff.posting_policy(forge.platform))


def validate_position(
    position: Mapping[str, object],
    shas: tuple[str, str, str],
    facts: DiffFacts | None,
    filepath: str,
    line: object,
) -> list[str]:
    """Return the reasons *position* is malformed for GitLab; empty when it is sound.

    This is what makes a capture mean something. ``try_post_json`` short-circuits into
    ``session.captured`` before a payload reaches the network, so a pre-flight that only counts
    captures reports twelve discussions "captured" immediately before the live run
    answers 400 on all twelve. Its caller runs this UNCONDITIONALLY — one gate for both
    modes by construction, because a check that runs only under --dry-run cannot be the
    thing that makes --dry-run trustworthy.

    The gate is an EXACT-SHAPE comparison against a full expected position, in both
    directions: a missing key, an unexpected key and a wrong value are the same kind of
    400, and only a whole-shape check catches the ones nobody thought to enumerate.
    ``line_code`` is the known case — GitLab derives it server-side — but its sibling
    ``line_range`` reproduces the identical 400, and a key-by-key gate stays silent on
    every field added to the assembly after it was written. Two-directional presence
    matters for the same reason: an if-present-check-equality test passes every fixture
    while saying nothing about the omission it exists to catch, which is precisely how a
    dropped conditional attach reaches the wire.

    *shas* is the ``fetch_gitlab_shas`` triple. Whether the fetched values are usable at
    all is a loop-invariant question answered once at the fetch; whether each position
    CARRIES them is per-position structure, and only this gate sees that.

    Every expectation is recomputed here from the SAME facts the assembly consumed,
    rather than shared with it: a gate that derives its answer through the code under
    test moves with the bug and passes it.

    SCOPE, stated plainly: this catches a regression in the assembly below, or a
    malformed finding. It cannot catch a parser defect: ``facts`` is the
    ground truth BOTH sides are derived from, so a wrong answer
    there is compared against itself and passes.
    """
    base_sha, head_sha, start_sha = shas
    expected: dict[str, object] = {
        "position_type": "text",
        "base_sha": base_sha,
        "head_sha": head_sha,
        "start_sha": start_sha,
        "new_path": filepath,
        "new_line": line,
    }
    expected_old_line = diff.old_line_for(facts, filepath, cast(int | None, line))
    if expected_old_line is not None:
        expected["old_line"] = expected_old_line
    if not diff.is_new_file(facts, filepath):
        expected["old_path"] = diff.old_path_for(facts, filepath)

    problems = []

    # A bool or a float line number passes line validation AND the equality check
    # below, reaching the wire in its own spelling.
    new_line = position.get("new_line")
    if "new_line" in position and not is_plain_int(new_line):
        problems.append(f"new_line must be an integer, got {new_line!r}")

    for key in sorted(set(expected) - set(position)):
        problems.append(f"{key} is missing, expected {expected[key]!r}")
    for key in sorted(set(position) - set(expected)):
        problems.append(f"{key} must not be sent for this position")
    for key in sorted(set(position) & set(expected)):
        if position[key] != expected[key]:
            problems.append(f"{key} is {position[key]!r}, expected {expected[key]!r}")

    return problems


# ---------------------------------------------------------------------------
# Comment body rendering
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# suggested_fix_code — the deterministic apply-check
# ---------------------------------------------------------------------------
# A ```suggestion fence is a COMMITTABLE patch: one click replaces the comment's
# apply range with its bytes, unreviewed. So it renders if and only if that range
# is EXACTLY the range the fence-owning finding states, at the specific render
# site, and every content check passes. Everything else downgrades to the prose
# `suggestion`, which a human reads before acting on — no finding is lost, only
# its one-click affordance.


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


def _anchor(
    finding: Mapping[str, object],
    path: str,
    facts: DiffFacts | None,
    platform: Platform,
) -> Anchor | None:
    line = finding.get("line")
    if line is None or not diff.is_line_valid(facts, path, cast(int | None, line)):
        return None
    site = (
        gate.github_apply_range(facts, path, line, finding.get("end_line"))
        if platform == "github"
        else gate.gitlab_apply_range(finding, line)
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
    data: Mapping[str, object], facts: DiffFacts | None, *, platform: Platform
) -> DeliveryPlan:
    findings = cast(Sequence[object], data.get("findings", []))
    indexes = {id(finding): index for index, finding in enumerate(findings)}
    groups: list[GroupPlan] = []
    candidates: list[gate.OverlapCandidate] = []
    for group in compose.consolidate_delivery(findings):
        primary = group.primary
        line = primary.get("line")
        path = (
            diff.diff_path_spelling(facts, cast(str, primary["file"]), cast(int, line))
            if line is not None
            else primary.get("file", "?")
        )
        anchor = _anchor(primary, cast(str, path), facts, platform)
        disposition: Disposition = "inline" if anchor is not None else "degraded"
        members = tuple(
            FindingPlan(
                indexes[id(member)],
                member,
                disposition,
                path if index == 0 else member.get("file", "?"),
                anchor if index == 0 else None,
                _site_verdict(member, anchor if index == 0 else None, facts)
                if index == 0 or disposition == "degraded"
                else None,
            )
            for index, member in enumerate((primary, *group.corroborators))
        )
        planned = members[0]
        verdict = planned.verdict
        if (
            anchor is not None
            and isinstance(primary, dict)
            and "suggested_fix_code" in primary
            and verdict is not None
            and verdict.keep
            and verdict.apply_range is not None
        ):
            candidates.append(
                gate.OverlapCandidate(planned.index, anchor.path, verdict.apply_range)
            )
        groups.append(
            GroupPlan(
                group,
                members,
                _group_skip_warning(
                    group, facts, cast(str, path) if line is not None else None
                )
                if anchor is None
                else None,
            )
        )
    losers = gate.overlap_losers(candidates)
    for index, group_plan in enumerate(groups):
        primary_plan = group_plan.members[0]
        verdict = primary_plan.verdict
        if primary_plan.index in losers and verdict is not None and verdict.keep:
            demoted = replace(verdict, keep=False, reason="overlaps_kept_fence")
            groups[index] = replace(
                group_plan,
                members=(
                    replace(primary_plan, verdict=demoted),
                    *group_plan.members[1:],
                ),
            )
    return DeliveryPlan(
        tuple(groups),
        tuple(record for record in candidates if record.index not in losers),
    )


def _activate_degraded(
    group: GroupPlan, *, session: DeliverySession
) -> tuple[compose.SkippedEntry, ...]:
    if group.skip_warning is not None:
        session.warn_skip(group.skip_warning)
    return tuple(
        compose.SkippedEntry(
            member.path, member.finding.get("line"), session.activate(member)
        )
        for member in group.members
    )


def plan_inline(
    group: GroupPlan,
    *,
    member_index: int,
    grouped: bool,
    positionless: bool,
    plan: DeliveryPlan,
    facts: DiffFacts | None,
    platform: Platform,
    session: DeliverySession,
    member_keys: tuple[str, ...] = (),
    marker_suffix: str = "",
) -> InlinePlan:
    member = group.members[member_index]
    anchor = None if positionless else member.anchor
    if positionless or member.verdict is None:
        verdict = _site_verdict(member.finding, anchor, facts)
        # Reactive sites query the static winners, including failed deliveries.
        # They never claim a range that could demote another reactive site.
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
            verdict = replace(verdict, keep=False, reason="overlaps_kept_fence")
        member = replace(member, anchor=anchor, verdict=verdict)
    finding = session.activate(member)
    offsets = (
        anchor.site.offsets
        if anchor is not None and isinstance(anchor.site, gate.GitLabApplySite)
        else None
    )
    sections = compose.render_group_sections(
        finding, group.group.corroborators if grouped else (), fence_offsets=offsets
    )
    surface: Surface = (
        "inline" if platform == "github" else "note" if positionless else "discussion"
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
    return InlinePlan(member, body, member_keys, marker_suffix, error)


def plan_summary(
    data: Mapping[str, object],
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
        findings_count=len(cast(Sequence[object], data.get("findings", []))),
        sha=sha,
        inline_count=inline_count,
    )
    error = _body_budget_error(summary.body, platform, "summary")
    return SummaryPlan(summary, sha, error + "; nothing was posted." if error else None)


def _delivery_marker_suffix(sha: object, keys: Sequence[str]) -> str:
    """Return the live-only suffix for the findings carried by one delivery."""
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


def _activate_inline_budget(inline: InlinePlan, platform: Platform) -> bool:
    if inline.budget_error is None:
        return False
    if platform == "github":
        die(inline.budget_error)
    warn(inline.budget_error)
    return True


def _report_inline_budget(
    composed: compose.InlineBody,
    platform: Platform,
    surface: Surface,
    filepath: object,
    line: object,
) -> None:
    """Report an inline fold without changing stdout or dry-run capture."""
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
    """Report omitted entries and prose folding without changing dry-run capture."""
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


def resolve_marker_sha(data: Mapping[str, object]) -> str:
    """Return the SHA to record in the review marker.

    Prefers the ``sha`` pinned in the payload — the commit the review actually ran
    against — so a HEAD that moved between the workflow run and the post cannot
    record a SHA no review ever examined (which would scope a later incremental
    diff wrongly). Falls back to ``git rev-parse HEAD`` when the field is absent
    or not SHA-shaped.
    """
    sha = data.get("sha")
    if isinstance(sha, str) and SHA_RE.fullmatch(sha.strip()):
        return sha.strip()
    head = get_head_sha()
    if not SHA_RE.fullmatch(head):
        # get_head_sha() yields "unknown" when git fails. Writing that produces a
        # marker the reader is guaranteed to reject, so the review posts but the
        # next run cannot detect it. Say so rather than failing silently.
        warn(
            f"could not resolve a commit SHA for the review marker (git returned "
            f"{head!r}); the posted review will not be detectable as a prior review. "
            f"Set the 'sha' field in the findings JSON to avoid this."
        )
    return head


# ---------------------------------------------------------------------------
# GitHub delivery
# ---------------------------------------------------------------------------


def post_github(
    data: Mapping[str, object],
    facts: DiffFacts | None,
    *,
    forge: Forge,
    session: DeliverySession,
) -> int:
    ensure_available(forge)
    plan = plan_delivery(data, facts, platform="github")
    comments: list[GitHubComment] = []
    skipped_groups: list[tuple[compose.SkippedEntry, ...]] = []
    for group in plan.groups:
        primary = group.members[0]
        anchor = primary.anchor
        if anchor is None:
            skipped_groups.append(_activate_degraded(group, session=session))
            continue
        inline = plan_inline(
            group,
            member_index=0,
            grouped=True,
            positionless=False,
            plan=plan,
            facts=facts,
            platform="github",
            session=session,
        )
        _activate_inline_budget(inline, "github")
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
            comment["line"] = primary.finding.get("end_line")
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
        if summary.omitted:
            skipped_line = (
                f"  {summary.shown} of {len(flat_skipped)} finding(s) skipped inline "
                "(lines not in diff) — appended to review body."
            )
        else:
            skipped_line = (
                f"  {len(flat_skipped)} finding(s) skipped inline (lines not in diff) — "
                "appended to review body."
            )
        print(skipped_line)
    _report_summary_budget(summary, "github")
    session.print_fix_summary()
    return 0


# ---------------------------------------------------------------------------
# GitLab delivery
# ---------------------------------------------------------------------------


def fetch_gitlab_shas(target: ReviewTarget, *, forge: GitLab) -> tuple[str, str, str]:
    """Fetch latest MR version SHAs from GitLab."""
    ensure_available(forge)
    result = forge.diff_refs(target)
    if result.error is not None:
        die(result.error)
    versions = cast(Sequence[Mapping[str, str]], result.payload)

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
    """Read this SHA's summary, finding keys, and legacy group keys from one snapshot.

    Dry-run skips the read so every finding is captured and the summary stays first.
    An invalid SHA cannot key deduplication. Fetch failure warns and delivers everything
    because a possible duplicate beats a silently dropped review.
    """
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
    data: Mapping[str, object],
    facts: DiffFacts | None,
    *,
    forge: GitLab,
    session: DeliverySession,
) -> int:
    target = _target(data)
    owner, repo, mr_iid = target.owner, target.repo, target.number

    ensure_available(forge)

    shas = fetch_gitlab_shas(target, forge=forge)
    base_sha, head_sha, start_sha = shas

    # fetch_gitlab_shas dies when the FETCH fails but never inspects the field values. An
    # empty sha is a loop-invariant configuration failure — every position built below
    # carries the same three — so it is reported ONCE, here, before the summary note
    # lands on the MR, rather than as N per-finding rejections after it.
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

    plan = plan_delivery(data, facts, platform="gitlab")
    skipped_groups = [
        _activate_degraded(group, session=session)
        for group in plan.groups
        if group.members[0].disposition == "degraded"
    ]
    remaining = tuple(
        group for group in plan.groups if group.members[0].disposition == "inline"
    )
    finalized = plan_summary(data, skipped_groups, platform="gitlab")
    composed, sha = finalized.summary, finalized.sha
    summary_payload: GitLabNotePayload = {"body": composed.body}
    prior = gitlab_prior_delivery(
        owner, repo, mr_iid, sha, forge=forge, session=session
    )
    delivered_keys = prior.finding_keys
    legacy_group_keys = prior.legacy_group_keys
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
        anchor = member.anchor
        raw_title = finding.get("title")
        title = prepare_line(raw_title) if isinstance(raw_title, str) else ""
        return compose.finding_key(
            anchor.path if anchor is not None else finding.get("file", "?"),
            anchor.line if anchor is not None else finding.get("line"),
            title,
            compose.key_material_body(finding),
        )

    def deliver(
        group: GroupPlan, member_index: int, keys: tuple[str, ...], *, grouped: bool
    ) -> DeliveryOutcome:
        member = group.members[member_index]
        anchor = member.anchor
        assert anchor is not None
        filepath, line = anchor.path, anchor.line
        finding = member.finding
        # Render before dedup so full-key reruns retain their apply-check counts.
        # Fold notices describe an attempted delivery with a valid position.
        inline = plan_inline(
            group,
            member_index=member_index,
            grouped=grouped,
            positionless=False,
            plan=plan,
            facts=facts,
            platform="gitlab",
            session=session,
            member_keys=keys,
            marker_suffix=_delivery_marker_suffix(sha, keys),
        )
        if _activate_inline_budget(inline, "gitlab"):
            return "failed"
        if keys and all(key in delivered_keys for key in keys):
            return "already_present"
        position: GitLabPosition = {
            "position_type": "text",
            "base_sha": base_sha,
            "head_sha": head_sha,
            "start_sha": start_sha,
            "new_path": filepath,
            "new_line": line,
        }
        # An UNCHANGED (context) line is addressable only when the position carries
        # both sides; new_line alone is rejected with 400 `line_code can't be blank`.
        # An added line has no old side — omit the key rather than
        # sending null. NEVER synthesize `line_code`: it is derived server-side, and
        # both documented attempts to compute it client-side (position sibling, and
        # inside line_range) reproduced the identical 400.
        old_line = diff.old_line_for(facts, filepath, cast(int | None, line))
        if old_line is not None:
            position["old_line"] = old_line
        # Newly-added files have no old version. GitLab's discussions API
        # returns HTTP 500 (after silently creating the discussion) when
        # ``old_path`` is set on a position pointing into a new file. Omit
        # ``old_path`` for added files; include it for modified files so the
        # position stays anchored to the diff.
        if not diff.is_new_file(facts, filepath):
            # A rename needs its pre-rename path; skipped validation uses the
            # finding's path because no old-side spelling is available.
            position["old_path"] = diff.old_path_for(facts, filepath)

        problems = validate_position(position, shas, facts, filepath, line)
        if problems:
            session.warn_skip(
                f"Skipping finding '{finding.get('title', '?')}' at {filepath}:{line} "
                f"— malformed GitLab position: {'; '.join(problems)}."
            )
            return "invalid"
        _report_inline_budget(inline.body, "gitlab", "discussion", filepath, line)
        payload: GitLabDiscussionPayload = {
            "body": inline.body.body
            if session.dry_run
            else inline.body.body + inline.marker_suffix,
            "position": position,
        }
        _response, error = session.try_post(
            gitlab_discussion_request(target, payload), forge=forge
        )
        if error is not None:
            # One rejected position must not strand the findings behind it: the summary
            # note is already on the MR, so exiting here leaves partial, non-retryable
            # state.
            session.warn_skip(
                f"Skipping finding '{finding.get('title', '?')}' at {filepath}:{line} "
                f"— GitLab rejected the inline discussion.\n{error}"
            )
            return "failed"
        return "posted"

    def deliver_unanchored(
        group: GroupPlan, member_index: int, key: str
    ) -> DeliveryOutcome:
        member = group.members[member_index]
        finding = member.finding
        inline = plan_inline(
            group,
            member_index=member_index,
            grouped=False,
            positionless=True,
            plan=plan,
            facts=facts,
            platform="gitlab",
            session=session,
            member_keys=(key,),
            marker_suffix=_delivery_marker_suffix(sha, (key,)),
        )
        if _activate_inline_budget(inline, "gitlab"):
            return "failed"
        _report_inline_budget(
            inline.body, "gitlab", "note", finding.get("file"), finding.get("line")
        )
        payload: GitLabNotePayload = {
            "body": inline.body.body
            if session.dry_run
            else inline.body.body + inline.marker_suffix
        }
        _response, error = session.try_post(
            gitlab_note_request(target, payload), forge=forge
        )
        if error is not None:
            session.warn_skip(
                f"Skipping corroborating finding '{finding.get('title', '?')}' — GitLab "
                f"rejected the position-less note.\n{error}"
            )
            return "failed"
        return "posted"

    def deliver_corroborator(
        group: GroupPlan, member_index: int, key: str
    ) -> DeliveryOutcome:
        member = group.members[member_index]
        finding = member.finding
        line = finding.get("line")
        title = finding.get("title", "?")
        if line is None:
            session.warn_skip(
                f"Skipping corroborating finding '{title}' — no line number to "
                f"anchor its own discussion on."
            )
            return "invalid"
        if member.anchor is None:
            filepath = diff.diff_path_spelling(
                facts, cast(str, finding.get("file", "?")), cast(int, line)
            )
            session.warn_skip(
                f"Skipping corroborating finding '{title}' at {filepath}:{line} "
                f"— line not found in diff."
            )
            return "invalid"
        return deliver(group, member_index, (key,), grouped=False)

    counters = session.outcomes
    for group in remaining:
        primary_key = member_key(group.members[0])
        # Member anchors and key renders stay after the summary request.
        # An unanchored member keys its raw location, even inside a group body.
        members = [group.members[0]]
        for member in group.members[1:]:
            finding = member.finding
            line = finding.get("line")
            anchor = None
            if line is not None:
                filepath = diff.diff_path_spelling(
                    facts, cast(str, finding.get("file", "?")), cast(int, line)
                )
                anchor = _anchor(finding, filepath, facts, "gitlab")
            members.append(replace(member, anchor=anchor))
        group = replace(group, members=tuple(members))
        member_keys = (
            primary_key,
            *(member_key(member) for member in group.members[1:]),
        )
        if primary_key in legacy_group_keys:
            counters["already_present"] += len(member_keys)
            continue
        if any(key in delivered_keys for key in member_keys) and not all(
            key in delivered_keys for key in member_keys
        ):
            counters[
                "already_present"
                if primary_key in delivered_keys
                else deliver(group, 0, (primary_key,), grouped=False)
            ] += 1
            for member_index, key in enumerate(member_keys[1:], 1):
                if key in delivered_keys:
                    counters["already_present"] += 1
                elif group.members[member_index].anchor is not None:
                    counters[deliver_corroborator(group, member_index, key)] += 1
                else:
                    counters[deliver_unanchored(group, member_index, key)] += 1
            continue
        outcome = deliver(group, 0, member_keys, grouped=True)
        if outcome in ("invalid", "failed"):
            counters[outcome] += 1
            for member_index, key in enumerate(member_keys[1:], 1):
                counters[deliver_corroborator(group, member_index, key)] += 1
        else:
            counters[outcome] += len(member_keys)

    posted = counters["posted"]
    already_present = counters["already_present"]
    invalid = counters["invalid"]
    failed = counters["failed"]

    if session.dry_run:
        print(f"  {posted} inline discussion(s) captured.")
    else:
        print(f"  {posted} inline discussion(s) posted.")
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

    # Both "nothing new landed" exits below report the same outcome for two different
    # losses, so both owe the operator the same true statement about what is already
    # there: on a rerun, "nothing was posted inline" is a lie whenever this review's
    # discussions are standing on the MR from an earlier run.
    standing = (
        f" {already_present} from an earlier run remain on the MR."
        if already_present
        else ""
    )

    if failed:
        print(f"  {failed} inline discussion(s) not delivered (see warnings above).")
        if posted == 0:
            # Every attempt was made first — this exit reports the outcome, it does not
            # abandon the batch. A partial delivery is a success with warnings, but a
            # run that delivered nothing NEW and had a rejection is a failure, and the
            # message counts only what THIS run attempted: discussions an earlier run
            # already placed are neither successes of this one nor part of the total,
            # and a malformed position never reached the wire to be "attempted".
            die(
                f"all {failed} finding(s) attempted this run were not delivered "
                f"— nothing new was posted inline.{standing} The MR summary note "
                f"is on the MR; rerunning retries the inline comments without "
                f"duplicating what is already there."
            )

    # A malformed position is a payload defect, not a delivery outcome, and catching one
    # before the live post is the whole point of the pre-flight: ANY of them fails the
    # dry run, however many other findings captured cleanly. Returned rather than exited
    # on, so main() still writes the dry-run payload that shows what was wrong.
    if session.dry_run:
        return 1 if invalid else 0

    # Live, the rule the rejection branch above already follows applies whichever way a
    # finding was lost: a partial delivery is a success with warnings. Inline discussions
    # carry no idempotency key — only the summary note is deduplicated — so a non-zero
    # exit here invites the rerun that double-posts every discussion that landed.
    if invalid and posted == 0:
        die(
            f"{invalid} finding(s) had a malformed position — nothing new was posted "
            f"inline.{standing} The MR summary note is on the MR; rerunning retries the "
            f"inline comments without duplicating what is already there."
        )
    return 0


# ---------------------------------------------------------------------------
# Dry-run payload assembly
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _target(data: Mapping[str, object]) -> ReviewTarget:
    return ReviewTarget(
        cast(str, data["owner"]),
        cast(str, data["repo"]),
        cast(int | str, data["pr_number"]),
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
    parser.add_argument("--sha", help="Override the wrapper reviewed commit SHA.")
    return parser


_INPUT_FIELDS = ("owner", "repo", "pr_number", "platform", "sha")


def _execute(args: argparse.Namespace) -> int:

    # Defense-in-depth: CODE_GAUNTLET_POST_MODE=dry-run self-enforces dry-run so a
    # headless Phase 8 invocation that omits --dry-run cannot live-post. The flag wins
    # when present; env "live" or unset changes nothing without the flag.
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
        data: dict[str, Any] = {}
        for name in (*_INPUT_FIELDS[:3], *_INPUT_FIELDS[3:][::-1]):
            value = getattr(args, name)
            if value is not None:
                data[name] = value
        data["review_body"] = ""
        data["findings"] = loaded
    elif isinstance(loaded, dict):
        data = loaded
    else:
        die("Findings JSON must be an object or an array.")

    for name in _INPUT_FIELDS:
        value = getattr(args, name)
        if value is not None:
            data[name] = value

    if args.report and not data.get("review_body"):
        try:
            with open(args.report, encoding="utf-8") as fh:
                report = fh.read()
        except FileNotFoundError:
            die(f"Report file not found: {args.report}")
        try:
            data["review_body"] = compose.summary_body_from_report(report)
        except compose.ReportShapeError as exc:
            die(str(exc))

    for field in ("owner", "repo", "pr_number"):
        if field not in data:
            die(f"Missing required field in findings JSON: '{field}'")

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

    if platform not in ("github", "gitlab"):
        die(f"Unsupported platform: '{platform}'. Use 'github' or 'gitlab'.")

    forge = make_forge(cast(Platform, platform))
    target = _target(data)
    facts = fetch_diff_facts(target, forge=forge)

    # Deliver. A poster RETURNS its exit status instead of exiting, so a payload defect
    # it found cannot pre-empt the dry-run payload write below — that file is the artifact
    # an operator reads to see what the run would have sent.
    if isinstance(forge, GitLab):
        status = post_gitlab(data, facts, forge=forge, session=session)
    else:
        status = post_github(data, facts, forge=forge, session=session)

    if session.dry_run:
        out_path = session.write_dry_run_payload(
            cast(Platform, platform), args.findings_json
        )
        print(f"Dry run — no comments posted. Payload written to: {out_path}")

    return status


CLI = Command(parser=_parser(), main=_execute)
