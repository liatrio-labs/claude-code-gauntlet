"""Tests for bench/adapter/adapt.py — dry-run payload -> scorer candidates.

No network, no keys. The reference payload builders here drive the *real*
``scripts/post_review.py`` capture path (``post_json`` in DRY_RUN mode ->
``build_dry_run_payload``), so the committed fixtures under
``fixtures/adapter/`` are byte-identical to what ``post_review.py --dry-run``
emits for the call shapes each one covers. The builders compose the complete
summary through ``compose_review_body`` and pass singleton skipped groups,
matching ``post_github``/``post_gitlab`` — the
one remaining exception is the legacy ``github_4_comments_2_skipped``
fixture, whose ``skip_warnings`` are pre-formed strings with no backing
finding to derive from, so they are still injected straight into the
top-level ``skipped`` list, appended after the loop (``gitlab_shape`` needs
no such injection — every GL_FINDINGS entry is fully anchorable, so its
``skipped`` list is empty). Neither builder models renames or consolidation
groups: a modified file's ``old_path`` is always its resolved ``new_path``,
and a skipped primary's corroborators are never fanned into the skipped
section the way the real posters do (#22 D2) — every finding here is its own
single-member group. Budget selection is covered by ``TestSummaryBodyBudget``;
these mirrors only pin the poster wiring. ``TestFixtureFidelity`` re-derives each payload from
these builders and asserts equality with the committed fixture, guarding
against drift in the post_review payload shape it covers.
``TestRealPosterMatchesPayloadMirror`` goes one step further: it drives the
real ``post_review.main()`` over an actual parsed diff and checks the
mirror's output against that live capture, not just against a fixture the
mirror could have drifted alongside.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gauntlet.delivery.post as post_review
import pytest
from gauntlet import diff as diff_api
from gauntlet import proc
from gauntlet.delivery import compose, gate
from gauntlet.forge import (
    JsonFetch,
    ReviewTarget,
    github_review_request,
    gitlab_discussion_request,
    gitlab_note_request,
)

from bench.adapter.adapt import merge_candidates, payload_to_candidates
from tests.support.diff import diff_facts
from tests.support.forge import FakeForge, FakeGitLab

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adapter"

GITHUB_FIXTURE = FIXTURES / "github_4_comments_2_skipped.json"
GITHUB_EMPTY_FIXTURE = FIXTURES / "github_empty.json"
GITLAB_FIXTURE = FIXTURES / "gitlab_shape.json"
GITHUB_PREFIXED_PATH_FIXTURE = FIXTURES / "github_prefixed_path.json"
GITLAB_PREFIXED_PATH_FIXTURE = FIXTURES / "gitlab_prefixed_path.json"
GITHUB_SKIPPED_SECTION_FIXTURE = FIXTURES / "github_skipped_section.json"
GITLAB_FENCED_SUGGESTION_FIXTURE = FIXTURES / "gitlab_fenced_suggestion.json"
GITHUB_OVERLAP_DEMOTION_FIXTURE = FIXTURES / "github_overlap_demotion.json"
GITLAB_OVERLAP_DEMOTION_FIXTURE = FIXTURES / "gitlab_overlap_demotion.json"

_GITHUB_DRY_RUN_KEYS = {"platform", "endpoint", "method", "payload", "skipped"}
_GITLAB_DRY_RUN_KEYS = {"platform", "summary", "discussions", "skipped"}

# Every committed fixture, mapped to the top-level key set its platform's
# build_dry_run_payload() shape must carry. TestFixtureFidelity's key-shape
# test asserts this dict's keys equal every *.json under FIXTURES before
# checking any of them, so an unregistered fixture fails loudly here instead
# of escaping the per-fixture loop below unseen (#234).
FIXTURE_KEY_SHAPES = {
    GITHUB_FIXTURE: _GITHUB_DRY_RUN_KEYS,
    GITHUB_EMPTY_FIXTURE: _GITHUB_DRY_RUN_KEYS,
    GITHUB_PREFIXED_PATH_FIXTURE: _GITHUB_DRY_RUN_KEYS,
    GITHUB_SKIPPED_SECTION_FIXTURE: _GITHUB_DRY_RUN_KEYS,
    GITHUB_OVERLAP_DEMOTION_FIXTURE: _GITHUB_DRY_RUN_KEYS,
    GITLAB_FIXTURE: _GITLAB_DRY_RUN_KEYS,
    GITLAB_PREFIXED_PATH_FIXTURE: _GITLAB_DRY_RUN_KEYS,
    GITLAB_FENCED_SUGGESTION_FIXTURE: _GITLAB_DRY_RUN_KEYS,
    GITLAB_OVERLAP_DEMOTION_FIXTURE: _GITLAB_DRY_RUN_KEYS,
}

GOLDEN_A = "https://github.com/withastro/astro/pull/1234"
GOLDEN_B = "https://gitlab.com/gitlab-org/gitlab/-/merge_requests/999"


# ---------------------------------------------------------------------------
# Reference payload builders — drive the real post_review.py capture path so the
# committed fixtures are byte-identical to build_dry_run_payload() output.
# ---------------------------------------------------------------------------

_GH_SHA = "deadbeefcafe1234deadbeefcafe1234deadbeef"
_GL_BASE = "ba5e0000000000000000000000000000000000ba"
_GL_HEAD = "43ad0000000000000000000000000000000043ad"
_GL_START = "57a27000000000000000000000000000000057a2"

# Four findings become inline comments; the second is multi-line (end_line set)
# so the emitted comment carries start_line and its ``line`` is the *end* line.
# The fourth carries suggested_fix_code but NO end_line at all — the apply-check
# (`_suggested_fix_gate`) fails this closed on `missing_end_line` before it ever
# reaches the diff oracle, so `_gated_finding` strips the field and the rendered
# comment falls back to the finding's prose `suggestion`. This is the mutation
# target for the `_gated_finding` routing in `_github_comment` below: revert
# that routing to `render_comment_body(f)` and this finding's comment keeps its
# raw fence, which no longer matches the committed fixture (TestFixtureFidelity
# goes red) and trips test_fourth_finding_downgrades_missing_end_line directly.
GH_COMMENT_FINDINGS = [
    {
        "file": "src/auth/session.py",
        "line": 42,
        "severity": "high",
        "title": "Missing null check on token",
        "body": (
            "load_token() returns None when the cookie is absent; the next "
            "line dereferences it and raises AttributeError."
        ),
    },
    {
        "file": "src/auth/session.py",
        "line": 88,
        "end_line": 92,
        "severity": "medium",
        "title": "Password hashed twice",
        "body": (
            "hash_password() runs here and again in save(); the double hash "
            "makes the stored value fail verification."
        ),
    },
    {
        "file": "src/api/routes.py",
        "line": 15,
        "severity": "critical",
        "title": "SQL injection via f-string",
        "body": "The user-supplied uid is interpolated straight into the SQL string.",
        "end_line": 15,
        "suggested_fix_code": (
            'cursor.execute("SELECT * FROM users WHERE id = %s", (uid,))'
        ),
    },
    {
        "file": "src/api/routes.py",
        "line": 21,
        "severity": "high",
        "title": "Missing pagination limit",
        "body": (
            "list_users() returns every row with no LIMIT clause; a large "
            "table makes this endpoint OOM the request worker."
        ),
        "suggestion": (
            "Add a LIMIT/OFFSET pair to the query and cap the page size server-side."
        ),
        # No end_line: this is the missing_end_line downgrade case, deliberately
        # oracle-independent (the gate fails on this before it ever consults
        # valid_lines/line_texts). The prose `suggestion` above is its fallback.
        "suggested_fix_code": (
            'cursor.execute("SELECT * FROM users LIMIT %s OFFSET %s", (limit, offset))'
        ),
    },
]
GH_SKIP_WARNINGS = [
    (
        "Skipping finding 'Docs typo' at README.md:999 — line not found in diff. "
        "Valid lines for this file: [3, 4, 5]"
    ),
]

# The diff oracle a real ``post_github`` would have parsed for this hypothetical PR —
# hand-built, not derived from an actual unified diff, because these findings are
# synthetic fixture data with no diff of their own (unlike tests/test_post_review.py's
# GH_DIFF_INDENTED, which backs real parsed-diff assertions). `valid_lines` covers
# every line any finding above cites, matching `range_is_valid`'s real multiline
# check — including routes.py:21 (the fourth finding), present so its downgrade is
# provably `missing_end_line` and not a `no_diff_oracle` in disguise; `line_texts`
# only needs the one line a `suggested_fix_code` actually gates against on the
# CONTENT checks (routes.py:15) — the OLD, vulnerable text the fence replaces. The
# fourth finding's fence never reaches those checks (missing_end_line fails it
# first), so it needs no `line_texts` entry of its own.
_GH_VALID_LINES = {
    ("src/auth/session.py", 42): 42,
    ("src/auth/session.py", 88): 88,
    ("src/auth/session.py", 89): 89,
    ("src/auth/session.py", 90): 90,
    ("src/auth/session.py", 91): 91,
    ("src/auth/session.py", 92): 92,
    ("src/api/routes.py", 15): 15,
    ("src/api/routes.py", 21): 21,
}
_GH_LINE_TEXTS = {
    (
        "src/api/routes.py",
        15,
    ): '    cursor.execute(f"SELECT * FROM users WHERE id = {uid}")',
}

GL_FINDINGS = [
    {
        "file": "app/models/user.rb",
        "line": 27,
        "severity": "high",
        "title": "N+1 query in loop",
        "body": "user.posts is queried inside the each loop; preload before iterating.",
    },
    {
        "file": "app/controllers/sessions_controller.rb",
        "line": 5,
        "severity": "low",
        "title": "Unused parameter",
        "body": "The redirect_to param is never read.",
    },
]

# Neither GL_FINDINGS entry carries suggested_fix_code, so _gated_finding still
# short-circuits before consulting either mapping — their bytes are unaffected
# by what these hold. They are populated now (#234): the mirror's partition
# calls is_line_valid directly, ahead of any gate, and an empty dict has no
# keys, so both legacy findings would resolve as unanchorable and land in the
# skipped section instead of as discussions. Both lines are ADDED (no old
# side), so their values are None — old_line_for reads that same None either
# way this resolves, which is what keeps every existing fixture byte-identical.
_GL_VALID_LINES: dict[tuple[str, int], int | None] = {
    ("app/models/user.rb", 27): None,
    ("app/controllers/sessions_controller.rb", 5): None,
}
_GL_LINE_TEXTS: dict[tuple[str, int], str] = {}


_GH_FACTS = diff_facts(_GH_VALID_LINES, line_texts=_GH_LINE_TEXTS)
_GL_FACTS = diff_facts(_GL_VALID_LINES, line_texts=_GL_LINE_TEXTS)


def _reset_post_review():
    post_review.reset_run_state()
    post_review.DRY_RUN = False


def _github_comment(
    f, facts: diff_api.DiffFacts | None = _GH_FACTS, *, demote_reason=None
):
    """Mirror the poster's apply range and gate to keep fixture fences real.

    The overlap decision is supplied by the same pre-pass as the poster."""
    line = f["line"]
    filepath = diff_api.diff_path_spelling(facts, f["file"], line)
    end_line = f.get("end_line")
    site = gate.github_apply_range(facts, filepath, line, end_line)
    multiline, apply_range = site.multiline, site.apply_range
    gated = post_review._gated_finding(
        f,
        apply_range,
        facts,
        demote_reason=demote_reason,
    )
    composed = compose.compose_inline_body(
        compose.render_group_sections(gated, []),
        platform="github",
        surface="inline",
    )
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
    return comment


def _skip_entry(f, facts):
    """Share the poster's skip order and diagnostics between both mirrors.

    GitHub interleaves skips and renders; GitLab partitions before rendering.
    Present partial defaults keep the fixture diagnostics unconditional."""
    line = f.get("line")
    if line is None:
        post_review.warn_skip(
            f"Finding '{f.get('title', '?')}' has no line number — skipping."
        )
        return post_review._degraded_entry(f.get("file", "?"), None, f, facts)
    filepath = diff_api.diff_path_spelling(facts, f["file"], line)
    if not diff_api.is_line_valid(facts, filepath, line):
        vl = diff_api.valid_lines_for_file(facts, filepath)
        post_review.warn_skip(
            f"Skipping finding '{f.get('title', '?')}' at {filepath}:{line} "
            f"— line not found in diff. Valid lines for this file: {vl}"
        )
        return post_review._degraded_entry(filepath, line, f, facts)
    return None


def _github_overlap_losers(findings, facts):
    """Use the poster's candidate predicate and group index basis.

    This mirror has no consolidation: singleton groups retain finding order."""
    groups = [compose.Group(f, ()) for f in findings]
    records = post_review._github_overlap_records(groups, facts)
    return gate.overlap_losers(records)


def build_reference_github_payload(
    findings,
    skip_warnings,
    owner="withastro",
    repo="astro",
    pr_number=1234,
    review_body="Automated review summary.",
    facts: diff_api.DiffFacts | None = _GH_FACTS,
):
    """Keep skip and downgrade warnings interleaved in finding order.

    Survivors use the poster's gate before rendering. The fixture's extra
    skip warnings have no backing findings, so they follow the loop."""
    _reset_post_review()
    post_review.DRY_RUN = True
    losers = _github_overlap_losers(findings, facts)
    comments = []
    skipped_groups = []  # singleton groups; this mirror models no consolidation
    for index, f in enumerate(findings):
        entry = _skip_entry(f, facts)
        if entry is not None:
            skipped_groups.append([entry])
            continue
        comments.append(
            _github_comment(
                f,
                facts,
                demote_reason=("overlaps_kept_fence" if index in losers else None),
            )
        )
    total = len(findings) + len(skip_warnings)
    body = compose.compose_review_body(
        review_body,
        skipped_groups,
        platform="github",
        findings_count=total,
        sha=_GH_SHA,
        inline_count=len(comments),
    ).body
    payload = {"body": body, "event": "COMMENT", "comments": comments}
    post_review.post_json(
        github_review_request(ReviewTarget(owner, repo, pr_number), payload),
        forge=FakeForge(),
    )
    for w in skip_warnings:
        post_review._SKIP_WARNINGS.append(w)
    out = post_review.build_dry_run_payload("github")
    _reset_post_review()
    return out


def _gitlab_discussion(
    f,
    facts: diff_api.DiffFacts | None = _GL_FACTS,
    *,
    demote_reason=None,
    sha=None,
):
    """Use the poster's fence offsets and old-side line addressing.

    This mirror has no rename model, so modified old_path equals new_path.
    The overlap decision is supplied by the same pre-pass as the poster."""
    line = f["line"]
    gated, offsets = post_review._gitlab_anchored(
        f,
        line,
        facts,
        demote_reason=demote_reason,
    )
    filepath = diff_api.diff_path_spelling(facts, f["file"], line)
    position = {
        "position_type": "text",
        "base_sha": _GL_BASE,
        "head_sha": _GL_HEAD,
        "start_sha": _GL_START,
        "new_path": filepath,
        "new_line": line,
    }
    # An UNCHANGED (context) line anchors on both sides; an added line has no
    # old side, and the key is omitted rather than sent as null.
    old_line = diff_api.old_line_for(facts, filepath, line)
    if old_line is not None:
        position["old_line"] = old_line
    # A newly-added file has no old version at all — old_path is omitted for
    # it exactly as the real poster omits it, via the same is_new_file call.
    if not diff_api.is_new_file(facts, filepath):
        position["old_path"] = filepath
    key = compose.finding_key(
        filepath,
        line,
        f.get("title", ""),
        compose.key_material_body(f),
    )
    marker_suffix = post_review._delivery_marker_suffix(sha, [key])
    composed = compose.compose_inline_body(
        compose.render_group_sections(gated, [], fence_offsets=offsets),
        platform="gitlab",
        surface="discussion",
        marker_suffix=marker_suffix,
    )
    return {
        "body": composed.body,
        "position": position,
    }


def _gitlab_overlap_losers(remaining, facts):
    """Use the poster's candidate predicate on the pre-partitioned survivors.

    Skipped findings cannot occupy an index in the survivor list."""
    pairs = [
        (
            diff_api.diff_path_spelling(facts, f.get("file", "?"), f["line"]),
            compose.Group(f, ()),
        )
        for f in remaining
    ]
    records = post_review._gitlab_overlap_records(pairs, facts)
    return gate.overlap_losers(records)


def build_reference_gitlab_payload(
    findings,
    project="gitlab-org/gitlab",
    mr_iid=999,
    review_body="Automated review summary.",
    facts: diff_api.DiffFacts | None = _GL_FACTS,
    sha=None,
):
    """Partition skips before composing the summary and rendering survivors.

    A degraded entry can downgrade during partition, in finding order.
    Only surviving fence downgrades wait until the render pass."""
    _reset_post_review()
    owner, _, repo = project.rpartition("/")
    post_review.DRY_RUN = True
    skipped_groups = []  # singleton groups; this mirror models no consolidation
    remaining = []  # findings that reach the inline discussion loop
    for f in findings:
        entry = _skip_entry(f, facts)
        if entry is not None:
            skipped_groups.append([entry])
            continue
        remaining.append(f)

    total = len(findings)
    body = compose.compose_review_body(
        review_body,
        skipped_groups,
        platform="gitlab",
        findings_count=total,
        sha=_GH_SHA,
    ).body
    post_review.post_json(
        gitlab_note_request(ReviewTarget(owner, repo, mr_iid), {"body": body}),
        forge=FakeGitLab(),
    )
    losers = _gitlab_overlap_losers(remaining, facts)
    for index, f in enumerate(remaining):
        post_review.post_json(
            gitlab_discussion_request(
                ReviewTarget(owner, repo, mr_iid),
                _gitlab_discussion(
                    f,
                    facts=facts,
                    sha=sha,
                    demote_reason=("overlaps_kept_fence" if index in losers else None),
                ),
            ),
            forge=FakeGitLab(),
        )
    out = post_review.build_dry_run_payload("gitlab")
    _reset_post_review()
    return out


def _load_fixture(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Real-poster byte-equality guards — each case anchors on a diff shape where
# a finding's own file spelling and the diff's PARSED spelling diverge, so
# the payload mirror's builders above and the real ``post_review.main()``
# cannot agree by accident. ``_RealPosterTestCase`` drives ``main()`` itself;
# the diff/finding data below is shared between that live capture and the
# mirror call each test compares it against.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("forge_factory")
class _RealPosterTestCase(unittest.TestCase):
    """Reset module state even when the real poster exits during delivery."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(_reset_post_review)

    def _run_main(self, findings_data, diff, versions=None):
        platform = findings_data["platform"]
        fake = (
            FakeForge(diffs=[(diff, "", 0)])
            if platform == "github"
            else FakeGitLab(
                diffs=[(diff, "", 0)],
                refs=[JsonFetch(versions if versions is not None else [], None)],
            )
        )
        self.forge_factory.configure(fake)
        findings_path = os.path.join(self.tmp, "findings.json")
        with open(findings_path, "w", encoding="utf-8") as f:
            json.dump(findings_data, f)
        argv = ["post_review.py", findings_path, "--dry-run"]
        exit_code = None
        with (
            patch.object(sys, "argv", argv),
            patch(
                "gauntlet.proc.run",
                side_effect=_git_run,
            ),
        ):
            try:
                post_review.main()
            except SystemExit as exc:
                exit_code = exc.code
        # main() only calls sys.exit() for a truthy status — a poster that
        # reports failure while still emitting an unchanged payload must not
        # pass silently here.
        self.assertFalse(exit_code, f"post_review.main() exited with {exit_code!r}")
        self.assertEqual(self.forge_factory.calls, [platform])
        self.assertFalse(any(call.method == "submit" for call in fake.calls))
        payload_path = os.path.join(self.tmp, "post-review-payload.json")
        with open(payload_path, encoding="utf-8") as f:
            return json.load(f)


def _git_run(command, **kwargs):
    if command == ["git", "rev-parse", "HEAD"]:
        return proc.CompletedProcess(command, 0, "deadbeefcafe\n", "")
    raise AssertionError(f"Unexpected Git call: {command}")


# One file, one context line and four added lines — a single-line finding
# and a multi-line one both anchor inside it. GitHub strips the synthetic
# ``a/``/``b/`` diff prefixes before keying ``valid_lines``, so a finding
# that spells its file WITH one only resolves through the stripped fallback.
GH_DIFF_PREFIXED_PATH = (
    "diff --git a/src/edited.py b/src/edited.py\n"
    "--- a/src/edited.py\n"
    "+++ b/src/edited.py\n"
    "@@ -1,1 +1,5 @@\n"
    " def handler():\n"
    "+    line1\n"
    "+    line2\n"
    "+    line3\n"
    "+    line4\n"
)

GH_PREFIXED_PATH_FINDINGS = [
    {
        "file": "b/src/edited.py",
        "line": 2,
        "severity": "high",
        "title": "Single line issue",
        "body": "Single-line body text.",
    },
    {
        "file": "b/src/edited.py",
        "line": 3,
        "end_line": 4,
        "severity": "medium",
        "title": "Multi line issue",
        "body": "Multi-line body text.",
    },
]

GH_PREFIXED_PATH_OWNER = "acme"
GH_PREFIXED_PATH_REPO = "widgets"
GH_PREFIXED_PATH_PR = 42

# Two SEPARATE top-level files that collide only under the UNRESOLVED
# spelling: `foo.py` carries lines {1, 2}; the real top-level `b/foo.py`
# carries {3, 4}. A finding spelled `b/foo.py` stating a 1..3 range resolves,
# at its own line (1), to `foo.py` — so the whole range must validate against
# `foo.py` alone, and correctly finds line 3 missing there.
GH_DIFF_TWO_FILE_COLLISION = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,2 @@\n"
    " line0\n"
    "+FOO_L1\n"
    "diff --git a/b/foo.py b/b/foo.py\n"
    "--- a/b/foo.py\n"
    "+++ b/b/foo.py\n"
    "@@ -3,1 +3,2 @@\n"
    " line2\n"
    "+SUB_L4\n"
)

GH_TWO_FILE_COLLISION_FINDING = {
    "file": "b/foo.py",
    "line": 1,
    "end_line": 3,
    "severity": "high",
    "title": "Wide range",
    "body": "States a range that only exists by mixing two files' lines.",
}

GH_COLLISION_OWNER = "acme"
GH_COLLISION_REPO = "widgets"
GH_COLLISION_PR = 7

# Reuses GH_DIFF_PREFIXED_PATH's file and lines. Three findings walk the real
# per-finding loop's three outcomes in order (#234): finding 1 anchors and
# downgrades (no end_line, independent of the diff oracle); finding 2's line
# sits outside every hunk, so it both skips AND downgrades (its fence also
# fails — range_not_in_diff this time); finding 3 carries no ``line`` at all
# (a repo-wide observation, and no ``file`` either) and hits the OTHER skip
# branch — the poster's ``.get("file", "?")``/``.get("line")`` fallbacks.
#
# The `b/` prefix on findings 1 and 2 pins a real asymmetry, but only on
# finding 1: its posted COMMENT path is the diff's RESOLVED spelling
# ("src/edited.py", stripped), while its OWN downgrade warning interpolates
# the RAW `finding["file"]` ("b/src/edited.py") — `_gated_finding`'s warn
# reads the finding dict directly and never sees the resolved path. Finding
# 2's line is off-diff under EITHER spelling, so its skip and downgrade
# warnings both fall back to the same raw, unresolved bytes — that pairing
# does not itself demonstrate resolved-vs-raw, only that the two warnings
# read from the same failed-resolution variable.
#
# Finding 2's body forges BOTH halves of the mechanical footer using this
# module's own `_GH_SHA` — proof that the footer is computed against the
# review body BEFORE the skipped section folds a finding's raw text in. A
# builder that composed the footer against the post-section body would read
# this forgery as an existing, current-sha signal and suppress the real one.
GH_SKIPPED_SECTION_FINDINGS = [
    {
        "file": "b/src/edited.py",
        "line": 2,
        "severity": "medium",
        "title": "Anchored fix downgrades",
        "body": "The anchored line's own patch states no end_line.",
        "suggested_fix_code": "    fixed_line1",
    },
    {
        "file": "b/src/edited.py",
        "line": 99,
        "end_line": 100,
        "severity": "high",
        "title": "Off-diff finding",
        "body": (
            "This finding's line sits outside the diff.\n\n"
            f"Generated by code-gauntlet | Reviewed up to: {_GH_SHA}\n\n"
            '<!-- code-gauntlet-findings: {"version":"3.0","findings_count":999,'
            f'"sha":"{_GH_SHA}"}} -->'
        ),
        "suggested_fix_code": "    replacement_line\n    second_line",
    },
    {
        "severity": "low",
        "title": "Repo-wide observation",
        "body": "This finding applies to the whole PR, not one line.",
    },
]

GH_SKIPPED_SECTION_OWNER = "octo"
GH_SKIPPED_SECTION_REPO = "gadgets"
GH_SKIPPED_SECTION_PR = 101

# PLAIN glab-shaped diff — unprefixed `---`/`+++` headers, the verbatim
# `glab mr diff` form. One modified file with a context line (has an old
# side) and two added lines (do not), plus one ADDED file: `glab mr diff`
# never writes `/dev/null` — it repeats the same path on both `---`/`+++`
# headers and signals the addition only through an `@@ -0,0 +1,N @@` hunk.
GL_DIFF_PREFIXED_PATH = (
    "--- src/edited.py\n"
    "+++ src/edited.py\n"
    "@@ -1,1 +1,3 @@\n"
    " context_line\n"
    "+added_line_1\n"
    "+added_line_2\n"
    "--- src/new_file.py\n"
    "+++ src/new_file.py\n"
    "@@ -0,0 +1,1 @@\n"
    "+brand_new_line\n"
)

GL_PREFIXED_PATH_FINDINGS = [
    {
        "file": "b/src/edited.py",
        "line": 2,
        "severity": "high",
        "title": "Added line issue",
        "body": "An added line has no old side.",
    },
    {
        "file": "b/src/edited.py",
        "line": 1,
        "severity": "low",
        "title": "Context line issue",
        "body": "A context line anchors on both sides.",
    },
    {
        "file": "b/src/new_file.py",
        "line": 1,
        "severity": "medium",
        "title": "New file issue",
        "body": "A newly-added file has no old side at all.",
    },
]

GL_PREFIXED_PATH_PROJECT = "acme/widgets"
GL_PREFIXED_PATH_MR_IID = 7

# `context_line` anchors on both sides (it has an old_line); the two added
# lines that follow have none. Four findings walk post_gitlab's real
# two-pass structure (#234): the THIRD (off-diff) and FOURTH (no-line)
# findings both pre-partition into the skipped section BEFORE the summary
# note is composed, in findings order, so both their skip warnings always
# precede the FIRST finding's downgrade — which fires only once the inline
# loop renders it, a full pass later. The first finding's fence states no
# end_line (an oracle-independent downgrade); the second's spans [2, 3] with
# a KEPT fence (it fires no warning at all), pinning `_gitlab_anchored`'s
# offsets against a live capture rather than only the fixture below. The
# fourth carries no `line` and no `file` key, mirroring the GitHub
# no-line-no-file case (`GH_SKIPPED_SECTION_FINDINGS`'s "Repo-wide
# observation") to pin post_gitlab's own `if line is None:` pre-partition
# branch and its `?` file fallback against a live capture.
GL_DIFF_FENCED_SUGGESTION = (
    "--- src/edited.py\n"
    "+++ src/edited.py\n"
    "@@ -1,1 +1,3 @@\n"
    " context_line\n"
    "+added_line_2\n"
    "+added_line_3\n"
)

GL_FENCED_FINDINGS = [
    {
        "file": "src/edited.py",
        "line": 1,
        "severity": "medium",
        "title": "Context line downgrades",
        "body": "The context line's own patch states no end_line.",
        "suggested_fix_code": "fixed_context_line",
    },
    {
        "file": "src/edited.py",
        "line": 2,
        "end_line": 3,
        "severity": "high",
        "title": "Fenced fix kept",
        "body": "The added span gets a kept one-click suggestion.",
        "suggested_fix_code": "fixed_line_2\nfixed_line_3",
    },
    {
        "file": "src/edited.py",
        "line": 99,
        "severity": "low",
        "title": "Off-diff finding",
        "body": "This finding's line sits outside the diff.",
    },
    {
        "severity": "low",
        "title": "Repo-wide observation",
        "body": "This finding applies to the whole MR, not one line.",
    },
]

GL_FENCED_PROJECT = "octo/gadgets"
GL_FENCED_MR_IID = 55

# ---------------------------------------------------------------------------
# #223: overlap demotion — two kept fences whose apply ranges overlap within
# one file demote the LATER (delivery-order) one to prose. Both findings sets
# below are UNSTAMPED: neither finding carries `consolidation_key`, matching
# every other fixture in this file (module docstring — this mirror models no
# consolidation groups at all).
# ---------------------------------------------------------------------------

# One file, wide enough (lines 1..6) for two findings' MULTI-line ranges to
# genuinely overlap.
GH_DIFF_OVERLAP = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,6 @@\n"
    " def f():\n"
    "+    line2\n"
    "+    line3\n"
    "+    line4\n"
    "+    line5\n"
    "+    line6\n"
)

# First is a fence-less finding at a distinct, non-overlapping line — a
# candidate the mirror's overlap pre-pass must SKIP without shifting the
# index it hands the two fenced findings after it (#223 R4: the index basis
# is the group index, never a separate "candidate ordinal" among only the
# gate-passing records). The second finding is delivery-order first among
# the FENCED pair and keeps its fence: [2, 4]. The third overlaps it at line
# 3-4 ([3, 5]) and demotes — its `suggestion` prose field is what the
# demoted comment falls back to.
GH_OVERLAP_FINDINGS = [
    {
        "file": "foo.py",
        "line": 6,
        "severity": "low",
        "title": "Index splitter",
        "body": "No suggested_fix_code — never a candidate, but still a group.",
    },
    {
        "file": "foo.py",
        "line": 2,
        "end_line": 4,
        "severity": "high",
        "title": "First overlapping fix",
        "body": "The higher-priority finding's fence is kept.",
        "suggested_fix_code": "    a2\n    a3\n    a4",
    },
    {
        "file": "foo.py",
        "line": 3,
        "end_line": 5,
        "severity": "medium",
        "title": "Second overlapping fix",
        "body": "This fence's range overlaps the first finding's and demotes to prose.",
        "suggestion": "Apply the equivalent three-line change by hand.",
        "suggested_fix_code": "    b3\n    b4\n    b5",
    },
]

GH_OVERLAP_OWNER = "acme"
GH_OVERLAP_REPO = "overlap"
GH_OVERLAP_PR = 223

# Plain glab-shaped diff, same shape, one line wider (lines 1..7) — enough
# room for a same-line single-line pair AND a touching-disjoint pair in one
# fixture.
GL_DIFF_OVERLAP = (
    "--- foo.py\n+++ foo.py\n@@ -1,1 +1,7 @@\n"
    " def f():\n"
    "+    line2\n"
    "+    line3\n"
    "+    line4\n"
    "+    line5\n"
    "+    line6\n"
    "+    line7\n"
)

# Five findings, delivery order = array order:
#   0. line 99 — OFF-DIFF: skipped before `remaining` is even built, so the
#      `findings` array index (0) and the `remaining` index of every finding
#      after it diverge by one (#223 R4: the pre-pass's records are keyed on
#      the `remaining` index, never the raw `findings` index).
#   1. line 2, single-line — kept (first).
#   2. line 2, single-line — SAME line as #1: GitLab's own Range#overlaps?
#      treats two identical single-line ranges as conflicting, so this one
#      demotes even though neither states a multi-line span.
#   3. lines 4-5 — kept: overlaps neither single-line finding above.
#   4. lines 6-7 — kept: TOUCHES #3's range at the 5/6 boundary but shares no
#      line index with it, so both #3 and #4 keep their fences (#223 R4's
#      touching-disjoint case, pinned end to end through the real poster).
GL_OVERLAP_FINDINGS = [
    {
        "file": "foo.py",
        "line": 99,
        "severity": "low",
        "title": "Off-diff finding",
        "body": "This finding's line sits outside the diff — skipped before `remaining` is built.",
    },
    {
        "file": "foo.py",
        "line": 2,
        "end_line": 2,
        "severity": "high",
        "title": "First single-line fix",
        "body": "The higher-priority finding at line 2 keeps its fence.",
        "suggested_fix_code": "    s2",
    },
    {
        "file": "foo.py",
        "line": 2,
        "end_line": 2,
        "severity": "medium",
        "title": "Second single-line fix",
        "body": "A second finding anchored at the SAME line as the first.",
        "suggestion": "Apply the same one-line change by hand.",
        "suggested_fix_code": "    s2b",
    },
    {
        "file": "foo.py",
        "line": 4,
        "end_line": 5,
        "severity": "high",
        "title": "Touching range A",
        "body": "Kept: overlaps neither single-line finding above.",
        "suggested_fix_code": "    t4\n    t5",
    },
    {
        "file": "foo.py",
        "line": 6,
        "end_line": 7,
        "severity": "low",
        "title": "Touching range B",
        "body": "Kept: touches range A's end but shares no line index with it.",
        "suggested_fix_code": "    t6\n    t7",
    },
]

GL_OVERLAP_PROJECT = "acme/overlap"
GL_OVERLAP_MR_IID = 223


class TestRealPosterMatchesPayloadMirror(_RealPosterTestCase):
    """The mirror must reconstruct exactly what post_review.py's real posters
    send — not a plausible-looking approximation of it.
    """

    def test_github_single_file_prefixed_path(self):
        findings_data = {
            "platform": "github",
            "owner": GH_PREFIXED_PATH_OWNER,
            "repo": GH_PREFIXED_PATH_REPO,
            "pr_number": GH_PREFIXED_PATH_PR,
            "review_body": "Automated review summary.",
            "sha": _GH_SHA,
            "findings": GH_PREFIXED_PATH_FINDINGS,
        }
        real = self._run_main(findings_data, GH_DIFF_PREFIXED_PATH)

        parsed_facts = diff_api.parse_diff(
            GH_DIFF_PREFIXED_PATH, policy=diff_api.posting_policy("github")
        )
        valid_lines = parsed_facts.valid_lines
        self.assertTrue(valid_lines)
        self.assertIn(("src/edited.py", 2), valid_lines)

        mirror = build_reference_github_payload(
            GH_PREFIXED_PATH_FINDINGS,
            [],
            owner=GH_PREFIXED_PATH_OWNER,
            repo=GH_PREFIXED_PATH_REPO,
            pr_number=GH_PREFIXED_PATH_PR,
            facts=parsed_facts,
        )

        self.assertEqual(real, mirror)
        self.assertEqual(real, _load_fixture(GITHUB_PREFIXED_PATH_FIXTURE))
        self.assertEqual(real["payload"]["comments"][0]["path"], "src/edited.py")

    def test_github_two_file_collision_anchors_on_resolved_file(self):
        findings_data = {
            "platform": "github",
            "owner": GH_COLLISION_OWNER,
            "repo": GH_COLLISION_REPO,
            "pr_number": GH_COLLISION_PR,
            "review_body": "Automated review summary.",
            "sha": _GH_SHA,
            "findings": [GH_TWO_FILE_COLLISION_FINDING],
        }
        real = self._run_main(findings_data, GH_DIFF_TWO_FILE_COLLISION)

        parsed_facts = diff_api.parse_diff(
            GH_DIFF_TWO_FILE_COLLISION, policy=diff_api.posting_policy("github")
        )
        mirror = build_reference_github_payload(
            [GH_TWO_FILE_COLLISION_FINDING],
            [],
            owner=GH_COLLISION_OWNER,
            repo=GH_COLLISION_REPO,
            pr_number=GH_COLLISION_PR,
            facts=parsed_facts,
        )

        self.assertEqual(real, mirror)
        comment = real["payload"]["comments"][0]
        self.assertEqual(comment["path"], "foo.py")
        self.assertNotIn("start_line", comment)

    def test_gitlab_prefixed_path_with_old_line(self):
        owner, repo = GL_PREFIXED_PATH_PROJECT.split("/")
        findings_data = {
            "platform": "gitlab",
            "owner": owner,
            "repo": repo,
            "pr_number": GL_PREFIXED_PATH_MR_IID,
            "review_body": "Automated review summary.",
            "sha": _GH_SHA,
            "findings": GL_PREFIXED_PATH_FINDINGS,
        }
        real = self._run_main(
            findings_data,
            GL_DIFF_PREFIXED_PATH,
            versions=[
                {
                    "base_commit_sha": _GL_BASE,
                    "head_commit_sha": _GL_HEAD,
                    "start_commit_sha": _GL_START,
                }
            ],
        )

        parsed_facts = diff_api.parse_diff(
            GL_DIFF_PREFIXED_PATH, policy=diff_api.posting_policy("gitlab")
        )
        mirror = build_reference_gitlab_payload(
            GL_PREFIXED_PATH_FINDINGS,
            project=GL_PREFIXED_PATH_PROJECT,
            mr_iid=GL_PREFIXED_PATH_MR_IID,
            sha=_GH_SHA,
            facts=parsed_facts,
        )

        self.assertEqual(real, mirror)
        self.assertEqual(real, _load_fixture(GITLAB_PREFIXED_PATH_FIXTURE))
        added, context, new_file = real["discussions"]
        self.assertEqual(added["position"]["new_path"], "src/edited.py")
        self.assertEqual(context["position"]["new_path"], "src/edited.py")
        self.assertNotIn("old_line", added["position"])
        self.assertIn("old_line", context["position"])
        self.assertEqual(new_file["position"]["new_path"], "src/new_file.py")
        self.assertNotIn("old_path", new_file["position"])
        self.assertNotIn("old_line", new_file["position"])

    def test_github_unanchorable_finding_composes_skipped_section(self):
        findings_data = {
            "platform": "github",
            "owner": GH_SKIPPED_SECTION_OWNER,
            "repo": GH_SKIPPED_SECTION_REPO,
            "pr_number": GH_SKIPPED_SECTION_PR,
            "review_body": "Automated review summary.",
            "sha": _GH_SHA,
            "findings": GH_SKIPPED_SECTION_FINDINGS,
        }
        real = self._run_main(findings_data, GH_DIFF_PREFIXED_PATH)

        parsed_facts = diff_api.parse_diff(
            GH_DIFF_PREFIXED_PATH, policy=diff_api.posting_policy("github")
        )
        mirror = build_reference_github_payload(
            GH_SKIPPED_SECTION_FINDINGS,
            [],
            owner=GH_SKIPPED_SECTION_OWNER,
            repo=GH_SKIPPED_SECTION_REPO,
            pr_number=GH_SKIPPED_SECTION_PR,
            facts=parsed_facts,
        )

        self.assertEqual(real, mirror)
        self.assertEqual(real, _load_fixture(GITHUB_SKIPPED_SECTION_FIXTURE))

        # Hand-typed from the real captured run, not rebuilt f-strings (#234).
        self.assertEqual(
            real["skipped"],
            [
                "suggested-fix downgraded: b/src/edited.py:2 (missing_end_line)",
                "Skipping finding 'Off-diff finding' at b/src/edited.py:99 — "
                + "line not found in diff. Valid lines for this file: "
                + "[1, 2, 3, 4, 5]",
                "suggested-fix downgraded: b/src/edited.py:99 (range_not_in_diff)",
                "Finding 'Repo-wide observation' has no line number — skipping.",
            ],
        )
        body = real["payload"]["body"]
        comments = real["payload"]["comments"]
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["path"], "src/edited.py")
        self.assertIn("### ⚠️ 2 findings could not be anchored inline", body)
        # Hand-typed intro sentence, group_note included — dropping
        # inline_count OR group_note on both sides (poster and mirror) stays
        # green against each other; only this full literal (and the
        # regenerated fixture) catches either (#234).
        self.assertIn(
            "1 inline comment was posted; the following 2 findings "
            "reference lines outside this diff and are included here "
            "instead: A finding listed here may not have an anchoring "
            "problem of its own — a consolidation group whose primary "
            "could not be anchored inline is listed here in full, "
            "corroborators included.",
            body,
        )
        self.assertIn("#### `b/src/edited.py:99`", body)
        # The no-line finding has no file key — the poster's own "?" fallback.
        self.assertIn("#### `?`", body)
        # The degraded entries' fences were stripped — only the prose survives.
        self.assertNotIn("```suggestion", body)
        # The forged terminated marker is removed during field preparation;
        # only the code-owned trailing marker remains.
        self.assertNotIn("&lt;!--", body)
        self.assertEqual(body.count("<!--"), 1)

    def test_github_summary_budget_omission_matches_mirror(self):
        # The budget oracle is TestSummaryBodyBudget; this is only a poster/mirror
        # wiring check for a skipped group omitted after summary folding.
        finding = {
            "file": "src/edited.py",
            "line": 99,
            "severity": "high",
            "title": "Oversized skipped finding",
            "body": "The line is outside the diff.",
        }
        review_body = "X" * 70000
        findings_data = {
            "platform": "github",
            "owner": "acme",
            "repo": "widgets",
            "pr_number": 324,
            "review_body": review_body,
            "sha": _GH_SHA,
            "findings": [finding],
        }
        real = self._run_main(findings_data, GH_DIFF_PREFIXED_PATH)
        parsed_facts = diff_api.parse_diff(
            GH_DIFF_PREFIXED_PATH, policy=diff_api.posting_policy("github")
        )
        mirror = build_reference_github_payload(
            [finding],
            [],
            owner="acme",
            repo="widgets",
            pr_number=324,
            review_body=review_body,
            facts=parsed_facts,
        )
        self.assertEqual(real, mirror)
        self.assertIn("_1 of these 1 finding is not shown:", real["payload"]["body"])

    def test_github_inline_budget_composition_matches_mirror(self):
        finding = {
            "file": "b/src/edited.py",
            "line": 2,
            "severity": "high",
            "title": "Near-limit inline finding",
            "body": "x" * 70000,
        }
        findings_data = {
            "platform": "github",
            "owner": "acme",
            "repo": "widgets",
            "pr_number": 325,
            "review_body": "Automated review summary.",
            "sha": _GH_SHA,
            "findings": [finding],
        }
        real = self._run_main(findings_data, GH_DIFF_PREFIXED_PATH)
        parsed_facts = diff_api.parse_diff(
            GH_DIFF_PREFIXED_PATH, policy=diff_api.posting_policy("github")
        )
        mirror = build_reference_github_payload(
            [finding],
            [],
            owner="acme",
            repo="widgets",
            pr_number=325,
            facts=parsed_facts,
        )
        self.assertEqual(real, mirror)
        self.assertEqual(len(real["payload"]["comments"]), 1)
        self.assertIn(
            "_[folded:",
            real["payload"]["comments"][0]["body"],
        )
        self.assertIn(
            "65536-byte GitHub body limit]_",
            real["payload"]["comments"][0]["body"],
        )

    def test_gitlab_inline_budget_composition_matches_mirror(self):
        finding = {
            "file": "b/src/edited.py",
            "line": 2,
            "severity": "high",
            "title": "Near-limit discussion finding",
            "body": "x" * 1050000,
        }
        findings_data = {
            "platform": "gitlab",
            "owner": "acme",
            "repo": "widgets",
            "pr_number": 326,
            "review_body": "Automated review summary.",
            "sha": _GH_SHA,
            "findings": [finding],
        }
        real = self._run_main(
            findings_data,
            GL_DIFF_PREFIXED_PATH,
            versions=[
                {
                    "base_commit_sha": _GL_BASE,
                    "head_commit_sha": _GL_HEAD,
                    "start_commit_sha": _GL_START,
                }
            ],
        )
        parsed_facts = diff_api.parse_diff(
            GL_DIFF_PREFIXED_PATH, policy=diff_api.posting_policy("gitlab")
        )
        mirror = build_reference_gitlab_payload(
            [finding],
            project="acme/widgets",
            mr_iid=326,
            sha=_GH_SHA,
            facts=parsed_facts,
        )
        self.assertEqual(real, mirror)
        self.assertEqual(len(real["discussions"]), 1)
        self.assertIn("_[folded:", real["discussions"][0]["body"])
        self.assertIn(
            "1000000-byte GitLab body limit]_",
            real["discussions"][0]["body"],
        )

    def test_gitlab_fenced_suggestion_and_skipped_summary(self):
        owner, repo = GL_FENCED_PROJECT.split("/")
        findings_data = {
            "platform": "gitlab",
            "owner": owner,
            "repo": repo,
            "pr_number": GL_FENCED_MR_IID,
            "review_body": "Automated review summary.",
            "sha": _GH_SHA,
            "findings": GL_FENCED_FINDINGS,
        }
        real = self._run_main(
            findings_data,
            GL_DIFF_FENCED_SUGGESTION,
            versions=[
                {
                    "base_commit_sha": _GL_BASE,
                    "head_commit_sha": _GL_HEAD,
                    "start_commit_sha": _GL_START,
                }
            ],
        )

        parsed_facts = diff_api.parse_diff(
            GL_DIFF_FENCED_SUGGESTION, policy=diff_api.posting_policy("gitlab")
        )
        mirror = build_reference_gitlab_payload(
            GL_FENCED_FINDINGS,
            project=GL_FENCED_PROJECT,
            mr_iid=GL_FENCED_MR_IID,
            sha=_GH_SHA,
            facts=parsed_facts,
        )

        self.assertEqual(real, mirror)
        self.assertEqual(real, _load_fixture(GITLAB_FENCED_SUGGESTION_FIXTURE))

        # Hand-typed from the real captured run, not rebuilt f-strings (#234).
        self.assertEqual(
            real["skipped"],
            [
                "Skipping finding 'Off-diff finding' at src/edited.py:99 — "
                + "line not found in diff. Valid lines for this file: [1, 2, 3]",
                "Finding 'Repo-wide observation' has no line number — skipping.",
                "suggested-fix downgraded: src/edited.py:1 (missing_end_line)",
            ],
        )
        self.assertEqual(len(real["discussions"]), 2)
        # discussions[0] is the context-line finding, downgraded to prose
        # first; discussions[1] is the kept fence — the inline loop posts in
        # findings order, and the fenced finding is listed second (#234).
        # The no-line finding pre-partitions straight into the skipped
        # section and posts no discussion at all.
        fenced = real["discussions"][1]
        self.assertIn("```suggestion:-0+1\n", fenced["body"])
        self.assertEqual(fenced["position"]["new_line"], 2)
        self.assertNotIn("old_line", fenced["position"])
        summary_body = real["summary"]["body"]
        self.assertIn("### ⚠️ 2 findings could not be anchored inline", summary_body)
        # Hand-typed intro sentence, group_note included — dropping
        # inline_count OR group_note on both sides (poster and mirror) stays
        # green against each other; only this full literal (and the
        # regenerated fixture) catches either (#234).
        self.assertIn(
            "The following 2 findings reference lines outside this diff "
            "and are included here instead of as inline comments: A "
            "finding listed here may not have an anchoring problem of its "
            "own — a consolidation group whose primary could not be "
            "anchored inline is listed here in full, corroborators "
            "included.",
            summary_body,
        )
        self.assertIn("#### `src/edited.py:99`", summary_body)
        # The no-line finding has no file key — the poster's own "?" fallback.
        self.assertIn("#### `?`", summary_body)

    def test_gitlab_summary_budget_omission_matches_mirror(self):
        # The budget oracle is TestSummaryBodyBudget; this is only a poster/mirror
        # wiring check for a skipped group omitted after summary folding.
        finding = {
            "file": "src/edited.py",
            "line": 99,
            "severity": "high",
            "title": "Oversized skipped finding",
            "body": "The line is outside the diff.",
        }
        review_body = "X" * 1100000
        findings_data = {
            "platform": "gitlab",
            "owner": "acme",
            "repo": "widgets",
            "pr_number": 324,
            "review_body": review_body,
            "sha": _GH_SHA,
            "findings": [finding],
        }
        real = self._run_main(
            findings_data,
            GL_DIFF_PREFIXED_PATH,
            versions=[
                {
                    "base_commit_sha": _GL_BASE,
                    "head_commit_sha": _GL_HEAD,
                    "start_commit_sha": _GL_START,
                }
            ],
        )
        parsed_facts = diff_api.parse_diff(
            GL_DIFF_PREFIXED_PATH, policy=diff_api.posting_policy("gitlab")
        )
        mirror = build_reference_gitlab_payload(
            [finding],
            project="acme/widgets",
            mr_iid=324,
            review_body=review_body,
            sha=_GH_SHA,
            facts=parsed_facts,
        )
        self.assertEqual(real, mirror)
        self.assertIn("_1 of these 1 finding is not shown:", real["summary"]["body"])

    def test_github_overlap_demotion(self):
        """#223: two findings whose stated ranges overlap in the same file.
        The first (higher-priority — array/delivery order) keeps its fence;
        the second demotes to prose. A fence-less finding ahead of the pair
        (#223 R4's index-splitter — its group index is 0, but it is never a
        CANDIDATE) pins that the mirror's overlap pre-pass keys its records
        on the group index, not a separate candidate ordinal. All three
        findings are UNSTAMPED (no `consolidation_key`) — this mirror models
        no consolidation groups.
        """
        findings_data = {
            "platform": "github",
            "owner": GH_OVERLAP_OWNER,
            "repo": GH_OVERLAP_REPO,
            "pr_number": GH_OVERLAP_PR,
            "review_body": "Automated review summary.",
            "sha": _GH_SHA,
            "findings": GH_OVERLAP_FINDINGS,
        }
        real = self._run_main(findings_data, GH_DIFF_OVERLAP)

        parsed_facts = diff_api.parse_diff(
            GH_DIFF_OVERLAP, policy=diff_api.posting_policy("github")
        )
        mirror = build_reference_github_payload(
            GH_OVERLAP_FINDINGS,
            [],
            owner=GH_OVERLAP_OWNER,
            repo=GH_OVERLAP_REPO,
            pr_number=GH_OVERLAP_PR,
            facts=parsed_facts,
        )

        self.assertEqual(real, mirror)
        self.assertEqual(real, _load_fixture(GITHUB_OVERLAP_DEMOTION_FIXTURE))

        # Hand-typed from the real captured run (#223's third oracle, per the
        # #234 pattern) — the exact warning line bytes.
        self.assertEqual(
            real["skipped"],
            ["suggested-fix downgraded: foo.py:3 (overlaps_kept_fence)"],
        )
        comments = real["payload"]["comments"]
        self.assertEqual(len(comments), 3)
        # comments[0]: the index-splitter — never had a fence, never a
        # candidate, no `overlaps_kept_fence` warning of its own.
        self.assertNotIn("```suggestion", comments[0]["body"])
        # The kept (survivor) fence.
        self.assertIn("```suggestion\n    a2\n    a3\n    a4\n```", comments[1]["body"])
        # The demoted (loser) comment: fence ABSENT, prose SUGGESTION
        # present — hand-typed, not re-derived — this is the exact body the
        # real poster sends when a finding's own gate passed but a sibling's
        # fence claimed its range first.
        self.assertEqual(
            comments[2]["body"],
            "**\U0001f7e1 [MEDIUM] Second overlapping fix**\n\n"
            "This fence's range overlaps the first finding's and demotes "
            "to prose.\n\n"
            "**Suggested fix:**\n"
            "Apply the equivalent three-line change by hand."
            # The identity trailer is part of the wire body — one mark per
            # delivered surface. Escapes, never a pasted glyph.
            "\n\n\u2694\ufe0f *Code Gauntlet*",
        )
        self.assertNotIn("```suggestion", comments[2]["body"])

    def test_gitlab_overlap_demotion(self):
        """#223's GitLab dry-run equivalent, including a SAME-LINE
        single-line pair (findings 1-2, GitLab's own `Range#overlaps?`
        collides two identical single-line ranges) and a TOUCHING DISJOINT
        pair that BOTH keep their fences (findings 3-4 — the third oracle
        pin for #223 R4's worked example, end to end through the real
        poster). An OFF-DIFF finding (0) is skipped before `remaining` is
        even built, so `findings`' own array index and `remaining`'s index
        diverge by one — pinning that the mirror's overlap pre-pass keys its
        records on the `remaining` index, not the raw `findings` index.
        Unstamped: no finding carries `consolidation_key`.
        """
        owner, repo = GL_OVERLAP_PROJECT.split("/")
        findings_data = {
            "platform": "gitlab",
            "owner": owner,
            "repo": repo,
            "pr_number": GL_OVERLAP_MR_IID,
            "review_body": "Automated review summary.",
            "sha": _GH_SHA,
            "findings": GL_OVERLAP_FINDINGS,
        }
        real = self._run_main(
            findings_data,
            GL_DIFF_OVERLAP,
            versions=[
                {
                    "base_commit_sha": _GL_BASE,
                    "head_commit_sha": _GL_HEAD,
                    "start_commit_sha": _GL_START,
                }
            ],
        )

        parsed_facts = diff_api.parse_diff(
            GL_DIFF_OVERLAP, policy=diff_api.posting_policy("gitlab")
        )
        mirror = build_reference_gitlab_payload(
            GL_OVERLAP_FINDINGS,
            project=GL_OVERLAP_PROJECT,
            mr_iid=GL_OVERLAP_MR_IID,
            sha=_GH_SHA,
            facts=parsed_facts,
        )

        self.assertEqual(real, mirror)
        self.assertEqual(real, _load_fixture(GITLAB_OVERLAP_DEMOTION_FIXTURE))

        # Hand-typed — the exact warning line bytes: the off-diff skip fires
        # first (pre-partition, before the summary note), the same-line
        # loser's downgrade fires later (the render/deliver loop).
        self.assertEqual(
            real["skipped"],
            [
                "Skipping finding 'Off-diff finding' at foo.py:99 — line not "
                + "found in diff. Valid lines for this file: "
                + "[1, 2, 3, 4, 5, 6, 7]",
                "suggested-fix downgraded: foo.py:2 (overlaps_kept_fence)",
            ],
        )
        discussions = real["discussions"]
        self.assertEqual(len(discussions), 4)
        # discussions[0]: same-line survivor, kept.
        self.assertIn("```suggestion\n    s2\n```", discussions[0]["body"])
        # discussions[1]: same-line loser, demoted — fence ABSENT, prose
        # SUGGESTION present. Hand-typed, not re-derived.
        self.assertEqual(
            discussions[1]["body"],
            "**\U0001f7e1 [MEDIUM] Second single-line fix**\n\n"
            "A second finding anchored at the SAME line as the first.\n\n"
            "**Suggested fix:**\n"
            "Apply the same one-line change by hand."
            # The identity trailer is part of the wire body — one mark per
            # delivered surface. Escapes, never a pasted glyph.
            "\n\n\u2694\ufe0f *Code Gauntlet*",
        )
        self.assertNotIn("```suggestion", discussions[1]["body"])
        # discussions[2] and [3]: the touching-disjoint pair — BOTH keep
        # their fences (#223 R4: [n, m] and [m+1, k] share no line index).
        self.assertIn("```suggestion:-0+1\n    t4\n    t5\n```", discussions[2]["body"])
        self.assertIn("```suggestion:-0+1\n    t6\n    t7\n```", discussions[3]["body"])


# ---------------------------------------------------------------------------
# payload_to_candidates — GitHub
# ---------------------------------------------------------------------------


class TestPayloadToCandidatesGitHub(unittest.TestCase):
    def test_four_comments_two_skipped(self):
        # n_skipped is 2: the pre-existing "Docs typo" line-not-in-diff skip,
        # plus the "suggested-fix downgraded" warning the apply-check emits for
        # the fourth finding (missing_end_line) — both land in the fixture's
        # top-level "skipped" list, which is what n_skipped counts.
        cands, stats = payload_to_candidates(str(GITHUB_FIXTURE), GOLDEN_A)
        self.assertEqual(stats, {"n_candidates": 4, "n_skipped": 2})
        self.assertIn(GOLDEN_A, cands)
        self.assertIn("deep-review", cands[GOLDEN_A])
        entries = cands[GOLDEN_A]["deep-review"]
        self.assertEqual(len(entries), 4)
        for e in entries:
            self.assertEqual(e["source"], "extracted")
            self.assertEqual(set(e), {"text", "path", "line", "source"})

    def test_order_and_verbatim_text(self):
        payload = _load_fixture(GITHUB_FIXTURE)
        posted = payload["payload"]["comments"]
        cands, _ = payload_to_candidates(payload, GOLDEN_A)
        entries = cands[GOLDEN_A]["deep-review"]
        # index i candidate corresponds to index i posted comment
        for i, (entry, comment) in enumerate(zip(entries, posted, strict=True)):
            self.assertEqual(
                entry["text"],
                comment["body"],
                f"candidate {i} text must be the body verbatim",
            )
            self.assertEqual(entry["path"], comment["path"])
            self.assertEqual(entry["line"], comment["line"])

    def test_multiline_comment_line_is_end_line(self):
        # The second finding is multi-line: its posted comment.line is the end
        # line (92), and the candidate copies that verbatim.
        cands, _ = payload_to_candidates(str(GITHUB_FIXTURE), GOLDEN_A)
        second = cands[GOLDEN_A]["deep-review"][1]
        self.assertEqual(second["path"], "src/auth/session.py")
        self.assertEqual(second["line"], 92)

    def test_default_tool_key(self):
        cands, _ = payload_to_candidates(str(GITHUB_FIXTURE), GOLDEN_A)
        self.assertEqual(list(cands[GOLDEN_A]), ["deep-review"])

    def test_custom_tool_key(self):
        cands, _ = payload_to_candidates(
            str(GITHUB_FIXTURE), GOLDEN_A, tool="deep-review-v2"
        )
        self.assertIn("deep-review-v2", cands[GOLDEN_A])

    def test_accepts_path_object(self):
        cands, stats = payload_to_candidates(GITHUB_FIXTURE, GOLDEN_A)
        self.assertEqual(stats["n_candidates"], 4)

    def test_empty_comments_yields_empty_list_not_missing_key(self):
        cands, stats = payload_to_candidates(str(GITHUB_EMPTY_FIXTURE), GOLDEN_A)
        self.assertEqual(stats, {"n_candidates": 0, "n_skipped": 0})
        self.assertIn(GOLDEN_A, cands)
        self.assertIn("deep-review", cands[GOLDEN_A])
        self.assertEqual(cands[GOLDEN_A]["deep-review"], [])


# ---------------------------------------------------------------------------
# payload_to_candidates — GitLab
# ---------------------------------------------------------------------------


class TestPayloadToCandidatesGitLab(unittest.TestCase):
    def test_discussions_mapped_in_order(self):
        payload = _load_fixture(GITLAB_FIXTURE)
        discussions = payload["discussions"]
        cands, stats = payload_to_candidates(payload, GOLDEN_B)
        self.assertEqual(stats, {"n_candidates": len(discussions), "n_skipped": 0})
        entries = cands[GOLDEN_B]["deep-review"]
        self.assertEqual(len(entries), len(discussions))
        for entry, disc in zip(entries, discussions, strict=True):
            self.assertEqual(entry["text"], disc["body"])
            self.assertEqual(entry["path"], disc["position"]["new_path"])
            self.assertEqual(entry["line"], disc["position"]["new_line"])
            self.assertEqual(entry["source"], "extracted")


# ---------------------------------------------------------------------------
# payload_to_candidates — error handling
# ---------------------------------------------------------------------------


class TestPayloadToCandidatesErrors(unittest.TestCase):
    def test_unknown_platform_raises(self):
        with self.assertRaises(ValueError):
            payload_to_candidates({"platform": "bitbucket"}, GOLDEN_A)

    def test_bad_type_raises(self):
        with self.assertRaises(TypeError):
            payload_to_candidates(1234, GOLDEN_A)


# ---------------------------------------------------------------------------
# merge_candidates
# ---------------------------------------------------------------------------


class TestMergeCandidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_candidates(self, name, obj):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return path

    def test_merge_two_pr_files_keys_by_golden_url(self):
        c1, _ = payload_to_candidates(str(GITHUB_FIXTURE), GOLDEN_A)
        c2, _ = payload_to_candidates(str(GITLAB_FIXTURE), GOLDEN_B)
        f1 = self._write_candidates("pr1.json", c1)
        f2 = self._write_candidates("pr2.json", c2)

        merged, stats = merge_candidates([f1, f2])
        self.assertEqual(set(merged), {GOLDEN_A, GOLDEN_B})
        self.assertEqual(len(merged[GOLDEN_A]["deep-review"]), 4)
        self.assertEqual(len(merged[GOLDEN_B]["deep-review"]), 2)
        self.assertEqual(stats["n_candidates"], 6)
        self.assertEqual(stats["n_skipped"], 0)

    def test_merge_accepts_dicts_directly(self):
        c1, _ = payload_to_candidates(str(GITHUB_FIXTURE), GOLDEN_A)
        c2, _ = payload_to_candidates(str(GITLAB_FIXTURE), GOLDEN_B)
        merged, stats = merge_candidates([c1, c2])
        self.assertEqual(set(merged), {GOLDEN_A, GOLDEN_B})
        self.assertEqual(stats["n_candidates"], 6)

    def test_merge_preserves_candidate_order_within_pr(self):
        c1, _ = payload_to_candidates(str(GITHUB_FIXTURE), GOLDEN_A)
        original = [e["text"] for e in c1[GOLDEN_A]["deep-review"]]
        merged, _ = merge_candidates([c1])
        merged_texts = [e["text"] for e in merged[GOLDEN_A]["deep-review"]]
        self.assertEqual(merged_texts, original)

    def test_merge_same_golden_url_unions_tool_lists(self):
        c1, _ = payload_to_candidates(str(GITHUB_FIXTURE), GOLDEN_A)
        c2, _ = payload_to_candidates(str(GITHUB_EMPTY_FIXTURE), GOLDEN_A)
        merged, stats = merge_candidates([c1, c2])
        # Same golden_url, same tool -> lists concatenated, no key collision loss.
        self.assertEqual(list(merged), [GOLDEN_A])
        self.assertEqual(len(merged[GOLDEN_A]["deep-review"]), 4)
        self.assertEqual(stats["n_candidates"], 4)

    def test_merge_empty_input(self):
        merged, stats = merge_candidates([])
        self.assertEqual(merged, {})
        self.assertEqual(stats, {"n_candidates": 0, "n_skipped": 0})


# ---------------------------------------------------------------------------
# Fixture fidelity — the committed fixtures byte-match post_review.py output
# ---------------------------------------------------------------------------


class TestFixtureFidelity(unittest.TestCase):
    """Guard: committed fixtures == build_dry_run_payload() output."""

    def tearDown(self):
        _reset_post_review()

    def test_github_fixture_matches_post_review(self):
        expected = build_reference_github_payload(GH_COMMENT_FINDINGS, GH_SKIP_WARNINGS)
        self.assertEqual(_load_fixture(GITHUB_FIXTURE), expected)

    def test_fourth_finding_downgrades_missing_end_line(self):
        """Direct regression for the `_gated_finding` routing in `_github_comment`.

        The fourth GH_COMMENT_FINDINGS entry carries `suggested_fix_code` with
        no `end_line`, so the apply-check fails it closed on `missing_end_line`
        before ever consulting the diff oracle — deterministic, independent of
        `_GH_VALID_LINES`/`_GH_LINE_TEXTS`. Unlike the byte-compare above (which
        would also catch this if the fixture were regenerated through the same
        unrouted path), this asserts the specific behavior by name: no fence,
        prose fallback intact. Routing `_github_comment` through
        `render_comment_body(f)` instead of `render_comment_body(gated)` makes
        this fail.
        """
        comment = _github_comment(GH_COMMENT_FINDINGS[3])
        self.assertNotIn("```suggestion", comment["body"])
        self.assertIn("**Suggested fix:**", comment["body"])
        self.assertIn(
            "Add a LIMIT/OFFSET pair to the query and cap the page size server-side.",
            comment["body"],
        )

    def test_gitlab_discussion_states_the_offsets_the_poster_would(self):
        """Direct regression for the `_gitlab_anchored` routing in
        `_gitlab_discussion`.

        A GitLab position is single-line, so a hard-coded ``(line, line)`` apply
        range downgrades every multi-line patch — which the real poster stopped
        doing at #219, when the ```suggestion:-m+n header became how the apply
        range is widened. GL_FINDINGS carries no fence at all, so the byte
        compare above cannot see this.
        """
        finding = {
            "file": "app/models/user.rb",
            "line": 27,
            "end_line": 28,
            "severity": "high",
            "title": "N+1 query in loop",
            "body": "Preload the association before iterating.",
            "suggested_fix_code": "  posts = preload(:posts)\n  posts.each do |p|",
        }
        valid_lines = {("app/models/user.rb", 27): 27, ("app/models/user.rb", 28): 28}
        line_texts = {
            ("app/models/user.rb", 27): "  user.posts.each do |p|",
            ("app/models/user.rb", 28): "    render p",
        }
        discussion = _gitlab_discussion(
            finding, facts=diff_facts(valid_lines, line_texts=line_texts)
        )
        self.assertIn("```suggestion:-0+1", discussion["body"])
        self.assertEqual(discussion["position"]["new_line"], 27)

    def test_github_empty_fixture_matches_post_review(self):
        expected = build_reference_github_payload([], [])
        self.assertEqual(_load_fixture(GITHUB_EMPTY_FIXTURE), expected)

    def test_gitlab_fixture_matches_post_review(self):
        expected = build_reference_gitlab_payload(GL_FINDINGS)
        self.assertEqual(_load_fixture(GITLAB_FIXTURE), expected)

    def test_github_prefixed_path_fixture_matches_post_review(self):
        parsed_facts = diff_api.parse_diff(
            GH_DIFF_PREFIXED_PATH, policy=diff_api.posting_policy("github")
        )
        expected = build_reference_github_payload(
            GH_PREFIXED_PATH_FINDINGS,
            [],
            owner=GH_PREFIXED_PATH_OWNER,
            repo=GH_PREFIXED_PATH_REPO,
            pr_number=GH_PREFIXED_PATH_PR,
            facts=parsed_facts,
        )
        self.assertEqual(_load_fixture(GITHUB_PREFIXED_PATH_FIXTURE), expected)

    def test_gitlab_prefixed_path_fixture_matches_post_review(self):
        parsed_facts = diff_api.parse_diff(
            GL_DIFF_PREFIXED_PATH, policy=diff_api.posting_policy("gitlab")
        )
        expected = build_reference_gitlab_payload(
            GL_PREFIXED_PATH_FINDINGS,
            project=GL_PREFIXED_PATH_PROJECT,
            mr_iid=GL_PREFIXED_PATH_MR_IID,
            facts=parsed_facts,
        )
        self.assertEqual(_load_fixture(GITLAB_PREFIXED_PATH_FIXTURE), expected)

    def test_github_skipped_section_fixture_matches_post_review(self):
        parsed_facts = diff_api.parse_diff(
            GH_DIFF_PREFIXED_PATH, policy=diff_api.posting_policy("github")
        )
        expected = build_reference_github_payload(
            GH_SKIPPED_SECTION_FINDINGS,
            [],
            owner=GH_SKIPPED_SECTION_OWNER,
            repo=GH_SKIPPED_SECTION_REPO,
            pr_number=GH_SKIPPED_SECTION_PR,
            facts=parsed_facts,
        )
        self.assertEqual(_load_fixture(GITHUB_SKIPPED_SECTION_FIXTURE), expected)

    def test_gitlab_fenced_suggestion_fixture_matches_post_review(self):
        parsed_facts = diff_api.parse_diff(
            GL_DIFF_FENCED_SUGGESTION, policy=diff_api.posting_policy("gitlab")
        )
        expected = build_reference_gitlab_payload(
            GL_FENCED_FINDINGS,
            project=GL_FENCED_PROJECT,
            mr_iid=GL_FENCED_MR_IID,
            facts=parsed_facts,
        )
        self.assertEqual(_load_fixture(GITLAB_FENCED_SUGGESTION_FIXTURE), expected)

    def test_github_overlap_demotion_fixture_matches_post_review(self):
        parsed_facts = diff_api.parse_diff(
            GH_DIFF_OVERLAP, policy=diff_api.posting_policy("github")
        )
        expected = build_reference_github_payload(
            GH_OVERLAP_FINDINGS,
            [],
            owner=GH_OVERLAP_OWNER,
            repo=GH_OVERLAP_REPO,
            pr_number=GH_OVERLAP_PR,
            facts=parsed_facts,
        )
        self.assertEqual(_load_fixture(GITHUB_OVERLAP_DEMOTION_FIXTURE), expected)

    def test_gitlab_overlap_demotion_fixture_matches_post_review(self):
        parsed_facts = diff_api.parse_diff(
            GL_DIFF_OVERLAP, policy=diff_api.posting_policy("gitlab")
        )
        expected = build_reference_gitlab_payload(
            GL_OVERLAP_FINDINGS,
            project=GL_OVERLAP_PROJECT,
            mr_iid=GL_OVERLAP_MR_IID,
            facts=parsed_facts,
        )
        self.assertEqual(_load_fixture(GITLAB_OVERLAP_DEMOTION_FIXTURE), expected)

    def test_fixture_top_level_keys_are_the_dry_run_shape(self):
        self.assertEqual(set(FIXTURE_KEY_SHAPES), set(FIXTURES.glob("*.json")))
        for path, expected_keys in FIXTURE_KEY_SHAPES.items():
            with self.subTest(fixture=path.name):
                self.assertEqual(set(_load_fixture(path)), expected_keys)


if __name__ == "__main__":
    unittest.main()
