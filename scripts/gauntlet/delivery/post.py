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
                                             #            (see _suggested_fix_gate); a failing patch is
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
    the review body / summary note (see build_skipped_section), so every finding
    still reaches the PR/MR unless the complete composed body exceeds its platform
    byte budget. One warning is emitted per skipped group, naming every member.

No external Python dependencies — stdlib only.
"""

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable
from typing import Any, NamedTuple, cast

from gauntlet import diff, proc
from gauntlet.cli import Command
from gauntlet.delivery import gate
from gauntlet.delivery.fold import (
    body_limit,
    fold_inline_body,
    fold_review_body,
    utf8_len,
)
from gauntlet.delivery.gate import ApplyRange, FenceOffsets, FixReason
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
from gauntlet.markdown import fence_run
from gauntlet.marker import (
    LEGACY_PRODUCT,
    SHA_RE,
    build_finding_marker,
    build_footer,
    build_prose_footer,
    is_sha_shaped,
)
from gauntlet.prior_review import PriorDelivery, gitlab_prior_delivery_state
from gauntlet.registry import (
    BRAND_MARK,
    BRAND_NAME,
    CODE_OWNED_HEADINGS,
    RULE_SOURCE_LABEL_FALLBACK,
    RULE_SOURCE_LABELS,
    SEVERITY_EMOJI,
    SEVERITY_EMOJI_FALLBACK,
)
from gauntlet.text import (
    normalize_report_severity,
    prepare_line,
    prepare_location,
    prepare_prose,
    prepared_prose,
    redact_secrets,
)

# ---------------------------------------------------------------------------
# Dry-run capture
# ---------------------------------------------------------------------------
DRY_RUN = False
_CAPTURED: list[PostRequest] = []
_SKIP_WARNINGS: list[str] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def warn_skip(msg):
    """Emit a skip warning and record it for dry-run payload capture."""
    _SKIP_WARNINGS.append(msg)
    warn(msg)


def ensure_available(forge: Forge) -> None:
    try:
        forge.ensure_available()
    except ForgeUnavailable as exc:
        die(str(exc))


def try_post_json(request: PostRequest, *, forge: Forge):
    """Return response/error without stranding siblings after a rejected position."""
    if DRY_RUN:
        _CAPTURED.append(request)
        return {}, None
    result = forge.submit(request)
    if result.warning is not None:
        warn(result.warning)
    return result.response, result.error


def post_json(request: PostRequest, *, forge: Forge):
    """Fail the whole delivery when the single review or first summary is rejected."""
    response, error = try_post_json(request, forge=forge)
    if error is not None:
        die(error)
    return response


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


def _is_plain_int(value):
    """True only for a real ``int`` — ``True`` and ``2.0`` both hash equal to the
    integer key, so they survive every dict lookup and equality check; type is the
    only thing that separates them from the integer they impersonate."""
    return isinstance(value, int) and not isinstance(value, bool)


def validate_position(position, shas, facts: DiffFacts | None, filepath, line):
    """Return the reasons *position* is malformed for GitLab; empty when it is sound.

    This is what makes a capture mean something. ``try_post_json`` short-circuits into
    ``_CAPTURED`` before a payload reaches the network, so a pre-flight that only counts
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
    expected = {
        "position_type": "text",
        "base_sha": base_sha,
        "head_sha": head_sha,
        "start_sha": start_sha,
        "new_path": filepath,
        "new_line": line,
    }
    expected_old_line = diff.old_line_for(facts, filepath, line)
    if expected_old_line is not None:
        expected["old_line"] = expected_old_line
    if not diff.is_new_file(facts, filepath):
        expected["old_path"] = diff.old_path_for(facts, filepath)

    problems = []

    # A bool or a float line number passes line validation AND the equality check
    # below, reaching the wire in its own spelling.
    new_line = position.get("new_line")
    if "new_line" in position and not _is_plain_int(new_line):
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


def _blockquote(text):
    """Raw CR/CRLF must not end a CommonMark line outside its quote prefix."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    out = []
    for line in lines:
        if line:
            out.append(f"> {line}")
        else:
            out.append(">")
    return "\n".join(out)


def _suggestion_fence(payload, *, offsets=None):
    """Return ``(open, close)`` fence lines; ``fence_run`` sets the length.

    *offsets* is GitLab's ``(above, below)`` pair: it makes the header
    ``suggestion:-m+n``, which widens what one click replaces to
    ``[anchor - m, anchor + n]``. The parser is fence-length blind, so
    the header composes with any length. ``None`` and ``(0, 0)`` both render
    the plain header — ``suggestion:-0+0`` is its exact synonym, and the plain
    spelling is the one every platform understands. This renderer is
    platform-blind: whether offsets are expressible at all is the poster's
    decision, made where the anchor is known.
    """
    fence = fence_run(payload)
    header = "suggestion"
    if offsets not in (None, (0, 0)):
        header = f"suggestion:-{offsets[0]}+{offsets[1]}"
    return f"{fence}{header}", fence


# ---------------------------------------------------------------------------
# suggested_fix_code — the deterministic apply-check
# ---------------------------------------------------------------------------
# A ```suggestion fence is a COMMITTABLE patch: one click replaces the comment's
# apply range with its bytes, unreviewed. So it renders if and only if that range
# is EXACTLY the range the fence-owning finding states, at the specific render
# site, and every content check passes. Everything else downgrades to the prose
# `suggestion`, which a human reads before acting on — no finding is lost, only
# its one-click affordance.

# Per-run patch-acceptance counters, reset by reset_run_state() alongside
# _CAPTURED and _SKIP_WARNINGS. n/(n+m) over these two is the acceptance rate,
# deterministic and readable from any run's stdout at no cost.
_FIX_COUNTS = {"kept": 0, "downgraded": 0}
# Per-reason downgrade tally, reset alongside _FIX_COUNTS. Delivery's own
# stdout readout (_print_fix_summary) does not consult this — it exists for a
# second gate caller (gauntlet.patches, the report-side apply-check)
# that renders a reason breakdown from it.
_FIX_REASON_COUNTS: dict[FixReason, int] = {}


def reset_run_state():
    """Clear every module-level counter/log a run accumulates.

    One entry point for both gate callers: main() (delivery) calls this in
    place of its old inline three-statement reset, and gauntlet.patches
    (the report-side apply-check, which never calls main()) calls it too —
    so a second caller of _gated_finding cannot start from state a prior
    caller in the same process left behind.
    """
    _CAPTURED.clear()
    _SKIP_WARNINGS.clear()
    _FIX_COUNTS.update(kept=0, downgraded=0)
    _FIX_REASON_COUNTS.clear()


def _gated_finding(
    finding: Any,
    apply_range: tuple[object, object] | None,
    facts: DiffFacts | None,
    *,
    mismatch_reason: FixReason = "anchor_mismatch",
    warn_label: str = "suggested-fix",
    demote_reason: FixReason | None = None,
) -> Any:
    # Corroborations carry no fence and must not count as render-site verdicts.
    if not isinstance(finding, dict) or "suggested_fix_code" not in finding:
        return finding
    verdict = gate.evaluate_fix(
        finding,
        apply_range=apply_range,
        facts=facts,
        mismatch_reason=mismatch_reason,
        demote_reason=demote_reason,
    )
    if verdict.keep:
        _FIX_COUNTS["kept"] += 1
        return finding
    reason = cast(FixReason, verdict.reason)
    _FIX_COUNTS["downgraded"] += 1
    _FIX_REASON_COUNTS[reason] = _FIX_REASON_COUNTS.get(reason, 0) + 1
    warn_skip(gate.format_fix_warning(finding, reason, label=warn_label))
    # Strip only on a shallow copy, preserving unknown caller fields.
    stripped = dict(finding)
    del stripped["suggested_fix_code"]
    return stripped


def _gitlab_anchored(
    finding: Any,
    anchor: Any,
    facts: DiffFacts | None,
    *,
    demote_reason: FixReason | None = None,
) -> tuple[Any, FenceOffsets | None]:
    # The position is single-line; offsets widen one click from this actual anchor.
    # The gate judges that realized range, falling back to the anchor alone.
    # Offsets stay out of band: a caller field cannot widen the approved range.
    site = gate.gitlab_apply_range(finding, anchor)
    gated = _gated_finding(
        finding,
        site.apply_range,
        facts,
        mismatch_reason="span_exceeds_platform_cap"
        if site.cap_exceeded
        else "anchor_mismatch",
        demote_reason=demote_reason,
    )
    return gated, site.offsets


def _degraded_entry(filepath, line, finding, facts: DiffFacts | None):
    """One entry for the body section, which has no anchor to apply against.

    Shared by both posters: a body-section entry never has an apply range, so
    ``_gated_finding`` is always called with ``None`` here regardless of
    platform.
    """
    return filepath, line, _gated_finding(finding, None, facts)


def _warn_group_skipped(group, facts: DiffFacts | None, filepath=None):
    """Record the one skip warning a group that cannot anchor inline gets.

    *filepath* is the primary's resolved diff spelling when its line is missing from
    the diff, and ``None`` when the primary has no line at all. The warning stands for
    the whole group, so a group of several names every member: the title alone would
    leave the corroborators untraceable.
    """
    primary = group["primary"]
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
    members = [primary, *group["corroborators"]]
    if len(members) > 1:
        labels = [
            str(m["id"] if m.get("id") is not None else m.get("title", "?"))
            for m in members
        ]
        message += f" [group members: {', '.join(labels)}]"
    warn_skip(message)


def _key_material_finding(finding):
    """Return the copy whose render seeds a DELIVERY KEY.

    ``suggested_fix_code`` and ``rule_source`` come off UNCONDITIONALLY — not gated —
    so a key does not depend on either field at all: it is the same whether the finding ships
    grouped or individually, and the same whichever way the apply-check went. This preserves
    the fence/rule-source exclusion guarantee for keys across delivery shapes.
    Prior-delivery dedup is retry-safe only while keys are stable
    across runs and across delivery shapes; making the GATE deterministic would
    not be enough, because the gate's inputs (the diff, the render site) are not.

    The brand trailer is excluded the same way and for the same reason — it is not a
    field, so it is excluded structurally by :func:`key_material_body` calling
    :func:`_finding_sections`, never by stripping a suffix.
    """
    if not isinstance(finding, dict) or not any(
        field in finding for field in ("suggested_fix_code", "rule_source")
    ):
        return finding
    stripped = dict(finding)
    stripped.pop("suggested_fix_code", None)
    stripped.pop("rule_source", None)
    return stripped


def _github_overlap_records(groups, facts: DiffFacts | None):
    """Return the CANDIDATE ``(index, path_lookup, apply_range)`` records for
    post_github's overlap pre-pass.

    *groups* is ``consolidate_delivery``'s own output (or the benchmark
    mirror's single-member-group equivalent — it models no consolidation) —
    the returned ``index`` is ``enumerate(groups)``'s index into THAT list,
    the same index post_github's render loop later checks with
    ``index in losers``. A candidate is a group whose primary carries
    ``suggested_fix_code``, anchors on a line the diff has, and whose fence
    passes the SAME pure gate (:func:`_fence_verdict`) at the SAME apply
    range (:func:`_github_apply_range`) the render loop itself will apply —
    "candidate" and "would render a kept fence" are one computation, not two
    that could disagree. Called by post_github's pre-pass and by the
    benchmark's payload mirror — never duplicated.
    """
    records = []
    for index, group in enumerate(groups):
        primary = group["primary"]
        if not isinstance(primary, dict) or "suggested_fix_code" not in primary:
            continue
        line = primary.get("line")
        if line is None:
            continue
        filepath = diff.diff_path_spelling(facts, primary.get("file", "?"), line)
        if not diff.is_line_valid(facts, filepath, line):
            continue
        site = gate.github_apply_range(facts, filepath, line, primary.get("end_line"))
        verdict = gate.evaluate_fix(primary, apply_range=site.apply_range, facts=facts)
        if verdict.keep:
            records.append(
                gate.OverlapCandidate(
                    index, filepath, cast(ApplyRange, site.apply_range)
                )
            )
    return records


def _gitlab_overlap_records(remaining, facts: DiffFacts | None):
    """Return the CANDIDATE ``(index, path_lookup, apply_range)`` records for
    post_gitlab's overlap pre-pass.

    *remaining* is post_gitlab's own pre-partitioned list of ``(filepath,
    group)`` pairs — every skip decision already made — so the returned
    ``index`` is ``enumerate(remaining)``'s index into THAT list, the same
    index post_gitlab's render loop later checks with ``index in losers``.
    Feeding this the unfiltered ``findings``/``consolidate_delivery`` list
    instead would silently re-key every record against the wrong basis — a
    skipped (e.g. off-diff) finding shifts ``remaining``'s positions but
    never occupies one itself. A candidate is a primary carrying
    ``suggested_fix_code`` whose fence passes the SAME pure gate the render
    loop applies, anchored at its own ``line``. Called by post_gitlab's
    pre-pass and by the benchmark's payload mirror.
    """
    records = []
    for index, (filepath, group) in enumerate(remaining):
        primary = group["primary"]
        if not isinstance(primary, dict) or "suggested_fix_code" not in primary:
            continue
        site = gate.gitlab_apply_range(primary, primary["line"])
        verdict = gate.evaluate_fix(primary, apply_range=site.apply_range, facts=facts)
        if verdict.keep:
            records.append(
                gate.OverlapCandidate(
                    index, filepath, cast(ApplyRange, site.apply_range)
                )
            )
    return records


def _print_fix_summary():
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
    kept = _FIX_COUNTS["kept"]
    downgraded = _FIX_COUNTS["downgraded"]
    if not (kept or downgraded):
        return
    print(f"  {kept} suggested fix(es) passed the apply-check.")
    print(f"  {downgraded} suggested fix(es) downgraded to prose.")


# One mark per delivered SURFACE, never per element: an inline comment/discussion body
# carries the trailer once at the end; the summary body carries the header instead, and the
# skipped-findings entries inside it are rendered unbranded.
BRAND_TRAILER = f"{BRAND_MARK} *{BRAND_NAME}*"
BRAND_SUMMARY_HEADER = f"### {BRAND_MARK} {BRAND_NAME}"


class ComposedBody(NamedTuple):
    body: str
    shown: int
    omitted: int
    folded_bytes: int
    omitted_entries: tuple[tuple[str, str], ...]


class InlineBody(NamedTuple):
    body: str
    folded_bytes: int


def _normalize_report_severity(raw):
    return normalize_report_severity(raw, SEVERITY_EMOJI)


def _finding_sections(finding, *, fence_offsets=None):
    """Key material excludes branding so identity changes cannot change delivery keys.

    Fence offsets depend on the posting anchor and belong to the caller, not the finding.
    """
    severity = _normalize_report_severity(finding.get("severity"))
    emoji = SEVERITY_EMOJI.get(severity, SEVERITY_EMOJI_FALLBACK)

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
    rule_label = RULE_SOURCE_LABEL_FALLBACK
    if clause_rule:
        source = finding.get("rule_source")
        if isinstance(source, str):
            rule_label = RULE_SOURCE_LABELS.get(source, RULE_SOURCE_LABEL_FALLBACK)
    if rule_text:
        parts += ["", f"**{rule_label}:**", _blockquote(rule_text)]

    # criticality, failure_scenario, evidence, confidence and dimension belong to
    # artifacts/reports; posted comments exclude them.
    #
    # The gate removes ineligible patches at each render site so inline and degraded
    # bodies can differ. Patch content stays exact except for credential redaction.
    # Normalize once: a second pass removes another LF and changes the approved patch.
    if suggested_fix:
        suggested_fix = redact_secrets(suggested_fix)
        open_f, close_f = _suggestion_fence(suggested_fix, offsets=fence_offsets)
        parts += ["", open_f, suggested_fix, close_f]

    return "\n".join(parts)


def render_comment_body(finding, *, fence_offsets=None):
    """Return the unfolded, marker-free rendering used by tests and delivery-key logic.

    It is byte-identical to a posted body only when no fold and no live marker
    applies; delivery composes the sections with ``compose_inline_body`` and
    appends live markers separately.
    """
    return (
        _render_group_sections(finding, [], fence_offsets=fence_offsets)
        + f"\n\n{BRAND_TRAILER}"
    )


def key_material_body(finding):
    """The bytes ``finding_key`` hashes: sections only, no trailer, ``suggested_fix_code``
    stripped (:func:`_key_material_finding`).

    Changing this rendering re-keys each affected finding on every open PR/MR.
    Rendering normalizes severity labels, so off-enum, blank or missing labels
    key as their normalized form. Canonical severities and case variants keep
    their keys; posted title, body, suggestion, rule and absent-title values key
    from their rendered forms.
    """
    return _finding_sections(_key_material_finding(finding))


def consolidate_delivery(findings):
    """Group *findings* for the posted delivery payload.

    Findings stay distinct in the caller's array — this only groups them for
    rendering. A finding carrying a truthy ``consolidation_key`` joins the group
    for that key; ``consolidation_primary: true`` marks which member anchors the
    group's single posted comment. A finding with no (or falsy) key becomes its
    own single-member group — this is what keeps output byte-identical to today
    for findings without stamps (older artifacts, degraded pipelines).

    Returns a list of ``{"primary": finding, "corroborators": [finding, ...]}``
    dicts, one per group, in the order each group's FIRST member appears in
    *findings* — deterministic regardless of which member within a group is
    the primary.
    """
    groups = []
    key_to_group = {}
    for f in findings:
        key = f.get("consolidation_key") if isinstance(f, dict) else None
        if not key:
            groups.append({"primary": f, "corroborators": []})
            continue
        group = key_to_group.get(key)
        if group is None:
            group = {"primary": None, "corroborators": []}
            key_to_group[key] = group
            groups.append(group)
        if f.get("consolidation_primary"):
            if group["primary"] is None:
                group["primary"] = f
            else:
                # A second consolidation_primary in the same group must not
                # overwrite (and drop) the first — demote it to corroborator.
                group["corroborators"].append(f)
        else:
            group["corroborators"].append(f)
    # Reachable only for hand-assembled payloads: filterFindings.js stamps
    # exactly one consolidation_primary per
    # group. If a caller's data has none, don't drop the group's first-seen
    # member — treat it as the primary rather than surface `None`.
    for group in groups:
        if group["primary"] is None and group["corroborators"]:
            group["primary"] = group["corroborators"].pop(0)
    return groups


def _render_corroboration(finding):
    """Render one non-primary group member as a corroborating section."""
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


def render_group_body(primary, corroborators, *, fence_offsets=None):
    """Build the unfolded, marker-free markdown body for one consolidation group.

    This is the marker-free rendering used by tests and delivery-key logic. It is
    byte-identical to a posted body only when no fold and no live marker applies.
    Delivery composes the sections with ``compose_inline_body`` and appends live
    markers separately. The identity trailer is appended ONCE, after the
    corroborations — one mark per delivered SURFACE, never one per element. With
    no *corroborators* the result is byte-identical to ``render_comment_body``,
    which keeps unstamped findings and degraded pipelines unaffected.
    *fence_offsets* reaches the primary's fence only: a corroborator never
    renders one (see :func:`_render_corroboration`), so a group body carries at
    most the one header, measured from the anchor the group is posted at.
    Each corroborator is appended as its own section; that appended text is
    finding-controlled, so it is run through the same ``<!--`` neutralization
    ``build_skipped_section`` applies (see its docstring). The primary's fields
    are prepared before this group is assembled.
    """
    return _render_group_sections(
        primary, corroborators, fence_offsets=fence_offsets
    ) + (f"\n\n{BRAND_TRAILER}")


def _render_group_sections(primary, corroborators, *, fence_offsets=None):
    """Render one group's sections without the identity trailer."""
    body = _finding_sections(primary, fence_offsets=fence_offsets)
    if not corroborators:
        return body
    section = "\n\n".join(_render_corroboration(c) for c in corroborators)
    section = section.replace("<!--", "&lt;!--")
    return f"{body}\n\n---\n\n{section}"


def _skipped_location(filepath, line):
    return f"{filepath}:{line}" if line is not None else str(filepath or "?")


def _plural(count, singular, plural=None):
    return singular if count == 1 else (plural or singular + "s")


def _skipped_frame(n, shown, inline_count):
    """Render the fixed part of the skipped section without any entries."""
    group_note = (
        " A finding listed here may not have an anchoring problem of its own — a "
        "consolidation group whose primary could not be anchored inline is listed "
        "here in full, corroborators included."
    )
    if inline_count is None:
        intro = (
            f"The following {shown} {_plural(shown, 'finding')} "
            f"{_plural(shown, 'references', 'reference')} lines outside this diff and "
            f"{_plural(shown, 'is', 'are')} included here instead of as inline comments:{group_note}"
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


def _skipped_piece(filepath, line, finding):
    """Render and neutralize one whole skipped entry, including its heading.

    The finding fields are prepared by their renderers. The whole piece also
    neutralizes any remaining marker opener, including one in its heading.
    """
    location = _skipped_location(filepath, line)
    piece = f"\n\n#### {prepare_location(location)}\n\n{_finding_sections(finding)}"
    return piece.replace("<!--", "&lt;!--")


def _closing_line(m, n, platform):
    limits = body_limit(platform)
    return (
        f"_{m} of these {n} {_plural(n, 'finding')} {_plural(m, 'is', 'are')} not shown: this {limits.surface} "
        f"reached the {limits.bytes}-byte {limits.label} body limit._"
    )


def build_skipped_section(skipped, inline_count=None):
    """Render the trailing section for findings that could not be anchored inline.

    *skipped* is a list of ``(filepath, line, finding)`` tuples. A member is not always
    here for its own reason: a consolidation group whose primary could not be anchored
    degrades as a whole, so corroborators are listed here too. *inline_count*, when
    given, is how many comments landed. GitLab passes no inline count because its
    summary note posts before the per-finding loop, so the landed count is not yet
    known. Entries are unbranded and use the shared frame and piece renderers. Each
    piece neutralizes its own ``<!--`` markers before fitting; that neutralization
    lives in ``_skipped_piece``. ``gauntlet.patches`` and
    ``gauntlet.marker`` cite ``build_skipped_section`` as the precedent.
    Returns ``""`` for an empty *skipped* list.
    """
    if not skipped:
        return ""
    n = len(skipped)
    return _skipped_frame(n, n, inline_count) + "".join(
        _skipped_piece(filepath, line, finding) for filepath, line, finding in skipped
    )


def _delivery_marker_suffix(sha, keys):
    """Return the live-only suffix for the findings carried by one delivery."""
    if not is_sha_shaped(sha) or not keys:
        return ""
    return "\n\n" + "\n".join(build_finding_marker(sha, key) for key in keys)


def compose_inline_body(sections, *, platform, surface, marker_suffix=""):
    """Compose and budget one complete inline body, reserving its live markers."""
    limits = body_limit(platform, surface)
    trailer = f"\n\n{BRAND_TRAILER}"
    allowance = limits.bytes - utf8_len(trailer) - utf8_len(marker_suffix)
    if utf8_len(sections) <= allowance:
        return InlineBody(sections + trailer, 0)
    folded, folded_bytes = fold_inline_body(sections, allowance, platform, surface)
    return InlineBody(folded + trailer, folded_bytes)


def _inline_body_over_limit(composed, marker_suffix, platform, surface):
    """Handle an inline envelope too small even for its synthetic fold."""
    limits = body_limit(platform, surface)
    actual = utf8_len(composed.body + marker_suffix)
    if actual <= limits.bytes:
        return False
    message = (
        f"The composed {limits.surface} is {actual} bytes, over the "
        f"{limits.bytes}-byte {limits.label} body limit"
    )
    if platform == "github":
        die(message + "; nothing was posted.")
    warn(message + "; skipping this delivery.")
    return True


def _report_inline_budget(composed, platform, surface, filepath, line):
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


def _bounded_section(n, shown_entries, inline_count, platform):
    section = _skipped_frame(n, len(shown_entries), inline_count)
    section += "".join(
        _skipped_piece(filepath, line, finding)
        for filepath, line, finding in shown_entries
    )
    omitted = n - len(shown_entries)
    if omitted:
        section += f"\n\n{_closing_line(omitted, n, platform)}"
    return section


def _compose_fragments(review_body, section, footer):
    fragments = [BRAND_SUMMARY_HEADER]
    if review_body:
        fragments.append(review_body)
    if section:
        fragments.append(section)
    return "\n\n".join(fragments) + footer


def _standalone_footer_lines(review_body, sha):
    if not isinstance(review_body, str):
        return ""
    sha_text = str(sha)
    if not sha_text:
        return ""
    footer_prefix = build_prose_footer(sha).split(sha_text, 1)[0]
    prefixes = (footer_prefix, f"Generated by {LEGACY_PRODUCT} | Reviewed up to: ")
    return "\n".join(
        line.lstrip(" \t")
        for line in review_body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.lstrip(" \t").startswith(prefixes)
    )


def _skipped_reserve(n, inline_count, platform):
    # The widest count and both grammar forms bound every possible shown count.
    frame = max(utf8_len(_skipped_frame(n, shown, inline_count)) for shown in (0, n))
    closing = utf8_len(_closing_line(n, n, platform))
    return frame + closing


def compose_review_body(
    review_body, skipped_groups, *, platform, findings_count, sha, inline_count=None
):
    """Deduplicate prose footers against standalone prepared lines with the same SHA.

    The bounded path reserves the complete envelope and uses a canonical footer.
    Skipped groups stay indivisible and their text cannot drive footer deduplication.
    """
    review_body = prepare_prose(review_body)
    limits = body_limit(platform)
    skipped = [entry for group in skipped_groups for entry in group]
    n = len(skipped)
    section = build_skipped_section(skipped, inline_count)
    footer = build_footer(
        findings_count,
        sha,
        body="",
        prose_body=_standalone_footer_lines(review_body, sha),
    )
    body = _compose_fragments(review_body, section, footer)
    if utf8_len(body) <= limits.bytes:
        return ComposedBody(body, n, 0, 0, ())

    footer = build_footer(findings_count, sha, body="")
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
            tuple(
                (_skipped_location(filepath, line), finding.get("title", "Finding"))
                for filepath, line, finding in skipped
            ),
        )

    remaining = allowance - (utf8_len(review_body) if review_body else 0)
    shown_entries = []
    omitted_entries = []
    for group in skipped_groups:
        pieces = [_skipped_piece(*entry) for entry in group]
        group_size = sum(utf8_len(piece) for piece in pieces)
        if group_size <= remaining:
            shown_entries.extend(group)
            remaining -= group_size
        else:
            omitted_entries.extend(group)
    section = _bounded_section(n, shown_entries, inline_count, platform) if n else ""
    body = _compose_fragments(review_body, section, footer)
    return ComposedBody(
        body,
        len(shown_entries),
        len(omitted_entries),
        0,
        tuple(
            (_skipped_location(filepath, line), finding.get("title", "Finding"))
            for filepath, line, finding in omitted_entries
        ),
    )


def _refuse_over_limit(body, platform):
    """Refuse a summary body that still exceeds its platform limit."""
    limits = body_limit(platform)
    actual = utf8_len(body)
    if actual > limits.bytes:
        die(
            f"The composed {limits.surface} is {actual} bytes, over the "
            f"{limits.bytes}-byte {limits.label} body limit; nothing was posted."
        )


def _report_summary_budget(composed, platform):
    """Report omitted entries and prose folding without changing dry-run capture."""
    limits = body_limit(platform)
    if composed.omitted:
        print(
            f"  {composed.omitted} skipped finding(s) not shown: the "
            f"{limits.surface} reached the {limits.bytes}-byte "
            f"{limits.label} body limit."
        )
        for location, title in composed.omitted_entries:
            warn(
                f"Skipped finding '{title}' at {location} not shown: the "
                f"{limits.surface} reached the {limits.bytes}-byte "
                f"{limits.label} body limit."
            )
    if composed.folded_bytes:
        print(
            f"  review_body folded by {composed.folded_bytes} bytes: the "
            f"{limits.surface} reached the {limits.bytes}-byte "
            f"{limits.label} body limit."
        )


def summary_body_from_report(report):
    """Return the body between the rendered Summary and next code-owned heading."""
    marker = "## Summary\n\n"
    start = report.find(marker)
    if start < 0:
        die("Report does not contain a rendered Summary section.")
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
        if line in CODE_OWNED_HEADINGS:
            body = report[body_start:cursor]
            return body[:-2] if body.endswith("\n\n") else body
        cursor = next_cursor
    die("Report does not contain a following code-owned heading after Summary.")


def finding_key(filepath, line, title, body):
    """Return the 16-hex delivery key recorded in a posted inline discussion's marker.

    Derived from what a reader can see on the wire — the path and line the comment is
    anchored to, the title, and the RENDERED body — never from an upstream finding
    ``id``: the input schema documented at the top of this file does not require one, so
    keying on it would make dedup depend on a field a caller need not supply. Content
    derivation also gives the right answer when it changes: an edited finding is a
    different comment and gets posted, rather than being suppressed as "already there".
    """
    material = "\x00".join((str(filepath), str(line), str(title), body))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Metadata footer
# ---------------------------------------------------------------------------
# ``build_footer`` is imported from gauntlet.marker and re-exported here,
# so this module has no second definition of the
# signal it writes.


def get_head_sha():
    stdout, _, rc = proc.output(["git", "rev-parse", "HEAD"])
    return stdout.strip() if rc == 0 else "unknown"


def resolve_marker_sha(data):
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


def post_github(data, facts: DiffFacts | None, *, forge: Forge):
    owner = data["owner"]
    repo = data["repo"]
    pr_number = data["pr_number"]
    findings = data.get("findings", [])

    ensure_available(forge)

    # consolidate_delivery(findings) is materialized ONCE: the pre-pass
    # below and the render loop that follows it walk the SAME list of groups by
    # index, so a demotion decided by the pre-pass lands on the exact group the
    # render loop later renders.
    groups = consolidate_delivery(findings)

    # Pure, SILENT pre-pass: decide which kept fences would collide, on
    # GitLab's own closed-interval overlap semantic, with another kept fence in
    # the same file. No warn_skip, no tally, no _gated_finding call here — every
    # existing warning still fires exactly once, from its existing render-loop
    # site below, in loop order (byte-stability constraint: a findings set with
    # no overlapping kept fences produces zero losers and is untouched by this
    # pass). The candidate predicate and index basis are `_github_overlap_records`'s
    # own docstring — this poster and the benchmark mirror both call it rather
    # than each keeping their own copy.
    overlap_records = _github_overlap_records(groups, facts)
    losers = gate.overlap_losers(overlap_records)

    comments = []
    skipped_groups = []  # one list of (filepath, line, finding) per degraded group
    # One posted comment per consolidation group: findings without a stamp
    # are each their own single-member group, so this loop is unchanged for them.
    for index, group in enumerate(groups):
        primary = group["primary"]
        corroborators = group["corroborators"]
        line = primary.get("line")
        if line is None:
            _warn_group_skipped(group, facts)
            skipped_groups.append(
                [_degraded_entry(primary.get("file", "?"), None, primary, facts)]
            )
            # The primary can't anchor, so the whole group degrades into the
            # skipped section as individual entries — the corroborators never
            # merged into a comment that itself never gets posted.
            for c in corroborators:
                skipped_groups[-1].append(
                    _degraded_entry(c.get("file", "?"), c.get("line"), c, facts)
                )
            continue

        # Resolve to the diff's own spelling BEFORE validating: a prefixed
        # finding that only the STRIPPED form validates must ship that
        # stripped path as `comment["path"]`, or GitHub 422s the whole review
        # on a path the PR does not have.
        filepath = diff.diff_path_spelling(facts, primary["file"], line)
        if not diff.is_line_valid(facts, filepath, line):
            _warn_group_skipped(group, facts, filepath)
            skipped_groups.append([_degraded_entry(filepath, line, primary, facts)])
            for c in corroborators:
                skipped_groups[-1].append(
                    _degraded_entry(c.get("file", "?"), c.get("line"), c, facts)
                )
            continue

        # The multi-line anchor decision is made HERE, ABOVE the body render, and
        # the assembly below consumes these same locals — the decision moved, it is
        # not duplicated. The apply-check has to see the range the comment will
        # REALLY apply at, and that range is only known once this has run.
        # `_github_apply_range` is the one function that makes it, so
        # this loop, the overlap pre-pass above, and the benchmark's payload
        # mirror all call it rather than each computing their own copy.
        #
        # start_line is added for multi-line comments, but only when the whole range
        # sits inside one hunk — GitHub rejects the ENTIRE review POST (losing every
        # finding, not just this one) with a 422 "Line could not be resolved" if
        # end_line falls outside every hunk, even though `line` alone was valid.
        end_line = primary.get("end_line")
        site = gate.github_apply_range(facts, filepath, line, end_line)
        multiline, apply_range = site.multiline, site.apply_range
        gated = _gated_finding(
            primary,
            apply_range,
            facts,
            demote_reason=("overlaps_kept_fence" if index in losers else None),
        )
        composed = compose_inline_body(
            _render_group_sections(gated, corroborators),
            platform="github",
            surface="inline",
        )
        _inline_body_over_limit(composed, "", "github", "inline")
        _report_inline_budget(composed, "github", "inline", filepath, line)
        comment = {
            "path": filepath,
            "line": line,
            "side": "RIGHT",
            "body": composed.body,
        }
        if multiline:
            comment["start_line"] = line
            comment["start_side"] = "RIGHT"
            comment["line"] = end_line

        comments.append(comment)

    # Only standalone prepared prose-footer lines carrying this SHA drive dedup.
    # The bounded path uses a canonical footer; skipped text cannot suppress it.
    sha = resolve_marker_sha(data)
    review_body = data.get("review_body", "")
    composed = compose_review_body(
        review_body,
        skipped_groups,
        platform="github",
        findings_count=len(findings),
        sha=sha,
        inline_count=len(comments),
    )

    payload = {
        "body": composed.body,
        "event": "COMMENT",
        "comments": comments,
    }

    _refuse_over_limit(composed.body, "github")
    resp = post_json(
        github_review_request(ReviewTarget(owner, repo, pr_number), payload),
        forge=forge,
    )
    if DRY_RUN:
        print("Review captured (dry-run).")
        print(f"  {len(comments)} inline comment(s) captured.")
    else:
        url = resp.get("html_url", resp.get("id", "posted"))
        print(f"Review posted: {url}")
        print(f"  {len(comments)} inline comment(s) posted.")
    flat_skipped = [entry for group in skipped_groups for entry in group]
    if flat_skipped:
        if composed.omitted:
            skipped_line = (
                f"  {composed.shown} of {len(flat_skipped)} finding(s) skipped inline "
                "(lines not in diff) — appended to review body."
            )
        else:
            skipped_line = (
                f"  {len(flat_skipped)} finding(s) skipped inline (lines not in diff) — "
                "appended to review body."
            )
        print(skipped_line)
    _report_summary_budget(composed, "github")
    _print_fix_summary()
    return 0


# ---------------------------------------------------------------------------
# GitLab delivery
# ---------------------------------------------------------------------------


def fetch_gitlab_shas(target: ReviewTarget, *, forge: GitLab):
    """Fetch latest MR version SHAs from GitLab."""
    ensure_available(forge)
    result = forge.diff_refs(target)
    if result.error is not None:
        die(result.error)
    versions: Any = result.payload

    if not versions:
        die("MR versions endpoint returned an empty list.")

    latest = versions[0]
    return (
        latest["base_commit_sha"],
        latest["head_commit_sha"],
        latest["start_commit_sha"],
    )


def gitlab_prior_delivery(
    owner: str, repo: str, mr_iid: int | str, sha: object, *, forge: Forge
) -> PriorDelivery:
    """Read this SHA's summary, finding keys, and legacy group keys from one snapshot.

    Dry-run skips the read so every finding is captured and the summary stays first.
    An invalid SHA cannot key deduplication. Fetch failure warns and delivers everything
    because a possible duplicate beats a silently dropped review.
    """
    if DRY_RUN or not is_sha_shaped(sha):
        return PriorDelivery(False, frozenset(), frozenset(), None)
    state = gitlab_prior_delivery_state(owner, repo, mr_iid, sha, forge=forge)
    if state.error:
        warn(
            f"could not check for an existing summary note or already-delivered inline "
            f"discussions ({state.error}); posting them."
        )
    return state


def post_gitlab(data, facts: DiffFacts | None, *, forge: GitLab):
    owner = data["owner"]
    repo = data["repo"]
    mr_iid = data["pr_number"]
    target = ReviewTarget(owner, repo, mr_iid)
    findings = data.get("findings", [])

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

    def body_factory(
        finding: Any,
        corroborators: Any = (),
        *,
        demote_reason: FixReason | None = None,
    ) -> Callable[[Any], Any]:
        """Return the body renderer ``deliver`` calls with the anchor it posts at.

        A GitLab position is always single-line; the ```suggestion:-m+n header is
        what widens the apply range to ``[anchor - m, anchor + n]``. Those
        offsets are therefore a property of the ANCHOR, not of the finding — and
        handing ``deliver`` a renderer instead of rendered bytes is what stops a
        body from being built for one anchor and posted at another.

        *demote_reason* passes straight through to ``_gitlab_anchored`` —
        the caller's set-level overlap decision for THIS finding, made once by
        the pre-pass below and threaded here rather than recomputed per anchor.
        """

        def make_body(anchor: Any) -> Any:
            gated, offsets = _gitlab_anchored(
                finding, anchor, facts, demote_reason=demote_reason
            )
            return _render_group_sections(gated, corroborators, fence_offsets=offsets)

        return make_body

    # Pre-partition the deterministic skips (no line number, or a line the diff never
    # touched) BEFORE the summary note is composed — the note is posted first, so the
    # skipped section must already be known. Both checks are pure functions of facts
    # already fetched above (facts, the finding's own file/line), so this is
    # exactly the decision the inline loop below would make; it is just made early for
    # the findings that will never reach that loop. `remaining` carries each finding's
    # resolved filepath through to the loop so it is not re-derived.
    skipped_groups = []  # one list of (filepath, line, finding) per degraded group
    remaining = []  # (filepath, group) — groups that reach the inline loop
    groups = consolidate_delivery(findings)
    # One posted discussion per consolidation group: findings without a
    # stamp are each their own single-member group, so this loop is unchanged
    # for them.
    for group in groups:
        primary = group["primary"]
        corroborators = group["corroborators"]
        line = primary.get("line")
        if line is None:
            _warn_group_skipped(group, facts)
            skipped_groups.append(
                [_degraded_entry(primary.get("file", "?"), None, primary, facts)]
            )
            # The primary can't anchor, so the whole group degrades into the
            # skipped section as individual entries.
            for c in corroborators:
                skipped_groups[-1].append(
                    _degraded_entry(c.get("file", "?"), c.get("line"), c, facts)
                )
            continue

        # Same spelling resolution the loop below applies — see its comment.
        filepath = diff.diff_path_spelling(facts, primary["file"], line)
        if not diff.is_line_valid(facts, filepath, line):
            _warn_group_skipped(group, facts, filepath)
            skipped_groups.append([_degraded_entry(filepath, line, primary, facts)])
            for c in corroborators:
                skipped_groups[-1].append(
                    _degraded_entry(c.get("file", "?"), c.get("line"), c, facts)
                )
            continue

        remaining.append((filepath, group))

    # Pure, SILENT pre-pass, same shape and same discipline as GitHub's —
    # BEFORE the summary note or any discussion posts, so the decision is made
    # once, statically, and never depends on what has or hasn't gone out live
    # yet (the demoted set is a pure function of findings + diff,
    # rerun-stable, dry-run == live). The candidate predicate and index basis
    # are `_gitlab_overlap_records`'s own docstring — this poster and the
    # benchmark mirror both call it rather than each keeping their own copy.
    # `kept_intervals` is the read-only map `deliver_corroborator`'s reactive
    # fence sites consult — it names every WINNING candidate's apply
    # range per path, never a loser's (the `if index not in losers:` guard
    # below is load-bearing: a loser's own range must never occupy anything).
    overlap_records = _gitlab_overlap_records(remaining, facts)
    losers = gate.overlap_losers(overlap_records)
    kept_intervals: dict[Any, list[Any]] = {}
    for record in overlap_records:
        if record.index not in losers:
            kept_intervals.setdefault(record.path_lookup, []).append(record.apply_range)

    sha = resolve_marker_sha(data)
    review_body = data.get("review_body", "")
    # Only standalone prepared prose-footer lines carrying this SHA drive dedup.
    # The bounded path uses a canonical footer; skipped text cannot suppress it.
    composed = compose_review_body(
        review_body,
        skipped_groups,
        platform="gitlab",
        findings_count=len(findings),
        sha=sha,
    )

    summary_payload = {"body": composed.body}
    prior = gitlab_prior_delivery(owner, repo, mr_iid, sha, forge=forge)
    delivered_keys = prior.finding_keys
    legacy_group_keys = prior.legacy_group_keys
    # Same predicate that makes gitlab_prior_delivery skip the fetch: a marker built
    # from a non-SHA-shaped sha (get_head_sha's "unknown" fallback) is one
    # find_finding_marker is guaranteed to reject, so appending it would leave an
    # unreadable comment on every discussion and dedup nothing.
    if prior.summary_posted:
        print(f"MR summary note for {sha} already on the MR — skipping.")
    else:
        _refuse_over_limit(composed.body, "gitlab")
        post_json(gitlab_note_request(target, summary_payload), forge=forge)
        print(
            "MR summary note captured (dry-run)."
            if DRY_RUN
            else "MR summary note posted."
        )
        _report_summary_budget(composed, "gitlab")

    # Post each finding as an inline discussion. Every finding lands in exactly one of
    # the five counters below, so the outcome reported at the end is a partition of
    # `findings` — a run cannot both under-report and claim success. The flattened
    # skipped groups above are reported after the summary-note decision; `remaining`
    # carries only what that pre-partition let through, filepath already resolved.
    def member_key(m, filepath, line):
        """Return the delivery key for ONE finding, independent of what posts it.

        Always derived from the member's own anchor and its SINGLE-finding render,
        never from the group body it may happen to ride in. That is what makes the
        two delivery shapes interchangeable for dedup: a corroborator posted on its
        own by one run is recognized by the group discussion of the next, and vice
        versa.

        The key render also drops ``suggested_fix_code`` UNCONDITIONALLY (see
        :func:`_key_material_finding`), so a key is fence-independent: the apply-check
        can strip a fence here and keep it there without ever moving a delivery key.
        """
        raw_title = m.get("title")
        title = prepare_line(raw_title) if isinstance(raw_title, str) else ""
        return finding_key(
            filepath,
            line,
            title,
            key_material_body(m),
        )

    def deliver(f, filepath, line, make_body, keys):
        """Post ONE discussion and return its outcome counter name.

        The group's single discussion and each fallback per-corroborator
        discussion go through this same validate → POST → dedup → count path,
        so a corroborator that is posted on its own is accounted for exactly
        like any never-grouped finding. *keys* is the delivery key of every
        finding this one discussion carries — the caller owns the mapping,
        because only it knows which findings share a body.

        *make_body* is called with the SAME *line* written into
        ``position.new_line`` below, so a fence's offsets cannot be measured
        from an anchor the discussion is not posted at.
        """
        # Render before the dedup check: the apply-check runs at render sites, so
        # the body is still gated even when a rerun posts nothing. The fold notice
        # belongs after dedup because it describes a delivery that was attempted.
        sections = make_body(line)
        marker_suffix = _delivery_marker_suffix(sha, keys)
        composed = compose_inline_body(
            sections,
            platform="gitlab",
            surface="discussion",
            marker_suffix=marker_suffix,
        )
        if _inline_body_over_limit(composed, marker_suffix, "gitlab", "discussion"):
            return "failed"
        if keys and all(k in delivered_keys for k in keys):
            # An earlier run already delivered every finding in this discussion for
            # this sha. Reposting it is duplication, not a
            # failure. A PARTIAL match never reaches here: post_gitlab splits such a
            # group into its missing members before calling.
            return "already_present"

        position = {
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
        old_line = diff.old_line_for(facts, filepath, line)
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
            warn_skip(
                f"Skipping finding '{f.get('title', '?')}' at {filepath}:{line} "
                f"— malformed GitLab position: {'; '.join(problems)}."
            )
            return "invalid"
        _report_inline_budget(composed, "gitlab", "discussion", filepath, line)

        payload = {
            "body": composed.body if DRY_RUN else composed.body + marker_suffix,
            "position": position,
        }

        _response, error = try_post_json(
            gitlab_discussion_request(target, payload), forge=forge
        )
        if error is not None:
            # One rejected position must not strand the findings behind it: the summary
            # note is already on the MR, so exiting here leaves partial, non-retryable
            # state.
            warn_skip(
                f"Skipping finding '{f.get('title', '?')}' at {filepath}:{line} "
                f"— GitLab rejected the inline discussion.\n{error}"
            )
            return "failed"
        return "posted"

    def member_key_for(m, anchor):
        """Return the delivery key for ONE group member, anchored or not.

        An anchorable member's key is `member_key` at its own resolved
        position — the same key its individual fallback discussion would
        carry. A member with no anchor (no line, or a line outside the diff)
        can only ever be delivered inside its group's body, so it has no
        resolved position to key on; it is keyed on its own raw, unresolved
        `file`/`line` instead. That is deterministic and is exactly what the
        group body's marker for this member must also use — the round-trip
        `deliver()` relies on for every member key.
        """
        if anchor:
            return member_key(m, *anchor)
        return member_key(m, m.get("file", "?"), m.get("line"))

    def deliver_unanchored(c, key):
        """Post an unanchorable corroborator's content as a position-less MR note.

        Reached only when *c* has no line to anchor an inline discussion on
        (or its line is outside the diff) AND the group's single discussion
        already delivered its anchored siblings on an earlier run — the group
        body cannot be reposted without duplicating those siblings, so this is
        the only delivery vehicle left. Mirrors the summary note's own
        position-less POST to the same `/notes` endpoint, with the same
        per-finding marker `deliver()` appends to an inline discussion, so a
        later rerun recognizes this exactly as it would an inline one.

        A position-less note has no anchor at all, so no fence it carried could
        ever be applied — the gate below strips one unconditionally here.
        """
        gated = _gated_finding(c, None, facts)
        marker_suffix = _delivery_marker_suffix(sha, [key])
        composed = compose_inline_body(
            _finding_sections(gated),
            platform="gitlab",
            surface="note",
            marker_suffix=marker_suffix,
        )
        if _inline_body_over_limit(composed, marker_suffix, "gitlab", "note"):
            return "failed"
        _report_inline_budget(composed, "gitlab", "note", c.get("file"), c.get("line"))
        payload = {"body": composed.body if DRY_RUN else composed.body + marker_suffix}
        _response, error = try_post_json(
            gitlab_note_request(target, payload), forge=forge
        )
        if error is not None:
            warn_skip(
                f"Skipping corroborating finding '{c.get('title', '?')}' — GitLab "
                f"rejected the position-less note.\n{error}"
            )
            return "failed"
        return "posted"

    def corroborator_anchor(c):
        """Return ``(filepath, line)`` a corroborator can be anchored at on its own.

        ``None`` when it cannot be — no line, or a line the diff does not touch.
        Silent: this is the predicate, and :func:`deliver_corroborator` is the
        delivery that reports the same losses to the operator.
        """
        line = c.get("line")
        if line is None:
            return None
        filepath = diff.diff_path_spelling(facts, c.get("file", "?"), line)
        if not diff.is_line_valid(facts, filepath, line):
            return None
        return filepath, line

    def deliver_corroborator(c: Any) -> Any:
        """Fall back to a corroborator's OWN individual discussion.

        Reached when the group's discussion is lost late (malformed primary
        position, or a rejected POST), and when a partially-delivered group is
        split into the members an earlier run did not deliver. Its file/line are
        resolved and gated here exactly as the pre-partition gates a primary's.

        This is a REACTIVE fence site: its own anchor was never a
        candidate in the pre-pass above (the pre-pass only ever sees a group's
        primary), so it QUERIES `kept_intervals` read-only — never claims an
        interval of its own — and demotes when its stated closed interval
        intersects an already-KEPT one on the same path. Two residuals remain:
        a corroborator that collides only with another reactive
        corroborator (this call site cannot see a sibling it has not been
        called for yet), and a corroborator that collides with a WINNING
        primary whose own discussion is lost late (malformed position, or a
        rejected POST) — that primary still occupies its interval in
        `kept_intervals`, so the corroborator can lose its affordance to a
        fence that never actually landed. Both are accepted: the map is built
        once by the pure pre-pass and deliberately never depends on live
        delivery outcomes.
        """
        line = c.get("line")
        title = c.get("title", "?")
        if line is None:
            warn_skip(
                f"Skipping corroborating finding '{title}' — no line number to "
                f"anchor its own discussion on."
            )
            return "invalid"
        filepath = diff.diff_path_spelling(facts, c.get("file", "?"), line)
        if not diff.is_line_valid(facts, filepath, line):
            warn_skip(
                f"Skipping corroborating finding '{title}' at {filepath}:{line} "
                f"— line not found in diff."
            )
            return "invalid"
        demote_reason: FixReason | None = None
        if "suggested_fix_code" in c:
            site = gate.gitlab_apply_range(c, line)
            if any(
                gate.ranges_overlap(cast(ApplyRange, site.apply_range), kept)
                for kept in kept_intervals.get(filepath, ())
            ):
                demote_reason = "overlaps_kept_fence"
        return deliver(
            c,
            filepath,
            line,
            body_factory(c, demote_reason=demote_reason),
            [member_key(c, filepath, line)],
        )

    counters = {
        "posted": 0,
        "already_present": 0,
        "invalid": 0,
        "failed": 0,
    }
    for index, (filepath, group) in enumerate(remaining):
        f = group["primary"]
        corroborators = group["corroborators"]
        # Decided once by the pure pre-pass above — independent of
        # everything below (prior-delivery state, live-POST outcomes), so a
        # rerun always reaches the same verdict for this same index.
        demote_reason: FixReason | None = (
            "overlaps_kept_fence" if index in losers else None
        )
        primary_key = member_key(f, filepath, f["line"])
        # Every member gets a key — even one with no anchor of its own, which can
        # only ever be delivered by its group's body (see member_key_for). Without
        # this, an unanchorable corroborator's key was simply absent, so the
        # all-keys-present check below could never see it as missing: the group
        # was declared already_present while that member's content had never
        # landed anywhere (unanchored corroborators lost on rerun).
        anchors = {id(c): corroborator_anchor(c) for c in corroborators}
        corroborator_keys = {
            id(c): member_key_for(c, anchors[id(c)]) for c in corroborators
        }
        member_keys = [primary_key] + [corroborator_keys[id(c)] for c in corroborators]
        if primary_key in legacy_group_keys:
            # This group's primary key was found on an older group body that
            # rendered a corroborator's content without ever giving it a key of
            # its own (see prior_delivery_from_entries). That body IS this group's
            # whole delivery — every member it renders is provably already on
            # the MR, missing keys included — so treat the whole group as
            # already_present rather than let the "some but not all" branch
            # below post the missing member a second time.
            counters["already_present"] += len(member_keys)
            continue
        if any(k in delivered_keys for k in member_keys) and not all(
            k in delivered_keys for k in member_keys
        ):
            # An earlier run delivered SOME of this group — its fallback posted
            # individual discussions for part of it. Posting the group now would put
            # that content on the MR twice, so deliver only what is
            # missing, each on its own. A missing member without an anchor cannot
            # get its own inline discussion (deliver_corroborator requires a line),
            # so it is posted as a position-less note instead — the group body it
            # would otherwise ride in was already delivered by the earlier run.
            counters[
                "already_present"
                if primary_key in delivered_keys
                else deliver(
                    f,
                    filepath,
                    f["line"],
                    body_factory(f, demote_reason=demote_reason),
                    [primary_key],
                )
            ] += 1
            for c in corroborators:
                anchor = anchors[id(c)]
                key = corroborator_keys[id(c)]
                if key in delivered_keys:
                    counters["already_present"] += 1
                elif anchor:
                    counters[deliver_corroborator(c)] += 1
                else:
                    counters[deliver_unanchored(c, key)] += 1
            continue
        outcome = deliver(
            f,
            filepath,
            f["line"],
            body_factory(f, corroborators, demote_reason=demote_reason),
            member_keys,
        )
        if outcome in ("invalid", "failed"):
            # The group's single discussion is lost, but its corroborators were
            # never given a chance of their own — post each as its own
            # discussion rather than dropping validated findings. The primary
            # still counts 1 toward its own loss; each corroborator is counted
            # on its own merits. (Unlike the partial-prior-delivery branch
            # above, nothing from this group has landed on the MR yet, so
            # `deliver_corroborator`'s own no-line/off-diff diagnostics are
            # the right report here — not the position-less note fallback,
            # which exists only to reach a member whose group body already
            # went out without it.)
            counters[outcome] += 1
            for c in corroborators:
                counters[deliver_corroborator(c)] += 1
        else:
            # One discussion carries the whole group, so every member shares
            # its outcome.
            counters[outcome] += 1 + len(corroborators)

    posted = counters["posted"]
    already_present = counters["already_present"]
    invalid = counters["invalid"]
    failed = counters["failed"]

    if DRY_RUN:
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
    _print_fix_summary()

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
    if DRY_RUN:
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


def build_dry_run_payload(platform):
    """Transform the captured API calls + skip warnings into the payload shape.

    GitHub posts a single review, so the payload exposes ``endpoint`` / ``method``
    / ``payload`` for that one call. GitLab posts a summary note followed by one
    discussion per finding, so the first capture becomes ``summary`` and the rest
    become ``discussions``. A ``discussions`` body is the rendered comment alone —
    the per-finding delivery marker is appended on the live wire only.
    """
    if platform == "github":
        cap = _CAPTURED[0] if _CAPTURED else PostRequest("github", "", "POST", (), {})
        return {
            "platform": "github",
            "endpoint": cap.endpoint,
            "method": cap.method,
            "payload": cap.payload,
            "skipped": list(_SKIP_WARNINGS),
        }

    summary = _CAPTURED[0].payload if _CAPTURED else {}
    discussions = [cap.payload for cap in _CAPTURED[1:]]
    return {
        "platform": "gitlab",
        "summary": summary,
        "discussions": discussions,
        "skipped": list(_SKIP_WARNINGS),
    }


def write_dry_run_payload(platform, findings_path):
    """Write the dry-run payload JSON next to *findings_path*. Returns its path."""
    payload = build_dry_run_payload(platform)
    out_dir = os.path.dirname(os.path.abspath(findings_path))
    out_path = os.path.join(out_dir, "post-review-payload.json")
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    global DRY_RUN

    parser = argparse.ArgumentParser(
        description="Post code-gauntlet findings as PR/MR comments."
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
    args = parser.parse_args()

    # Defense-in-depth: CODE_GAUNTLET_POST_MODE=dry-run self-enforces dry-run so a
    # headless Phase 8 invocation that omits --dry-run cannot live-post. The flag wins
    # when present; env "live" or unset changes nothing without the flag.
    DRY_RUN = args.dry_run or os.environ.get("CODE_GAUNTLET_POST_MODE") == "dry-run"
    reset_run_state()

    try:
        loaded = read_json(args.findings_json)
    except JsonReadError as exc:
        if isinstance(exc.cause, FileNotFoundError):
            die(f"Findings file not found: {args.findings_json}")
        if exc.kind == "parse":
            die(f"Invalid JSON in findings file: {exc.cause}")
        raise exc.cause from exc

    if isinstance(loaded, list):
        data = {}
        for name, value in (
            ("owner", args.owner),
            ("repo", args.repo),
            ("pr_number", args.pr_number),
            ("sha", args.sha),
            ("platform", args.platform),
        ):
            if value is not None:
                data[name] = value
        data["review_body"] = ""
        data["findings"] = loaded
    elif isinstance(loaded, dict):
        data = loaded
    else:
        die("Findings JSON must be an object or an array.")

    for name, value in (
        ("owner", args.owner),
        ("repo", args.repo),
        ("pr_number", args.pr_number),
        ("platform", args.platform),
        ("sha", args.sha),
    ):
        if value is not None:
            data[name] = value

    if args.report and not data.get("review_body"):
        try:
            with open(args.report, encoding="utf-8") as fh:
                data["review_body"] = summary_body_from_report(fh.read())
        except FileNotFoundError:
            die(f"Report file not found: {args.report}")

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

    forge = make_forge(platform)
    target = ReviewTarget(data["owner"], data["repo"], data["pr_number"])
    facts = fetch_diff_facts(target, forge=forge)

    # Deliver. A poster RETURNS its exit status instead of exiting, so a payload defect
    # it found cannot pre-empt the dry-run payload write below — that file is the artifact
    # an operator reads to see what the run would have sent.
    if isinstance(forge, GitLab):
        status = post_gitlab(data, facts, forge=forge)
    else:
        status = post_github(data, facts, forge=forge)

    if DRY_RUN:
        out_path = write_dry_run_payload(platform, args.findings_json)
        print(f"Dry run — no comments posted. Payload written to: {out_path}")

    if status:
        sys.exit(status)


CLI = Command.legacy(main, prog="post_review.py")
