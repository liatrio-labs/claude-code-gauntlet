"""Tests for bench/adapter/adapt.py — dry-run payload -> scorer candidates.

No network, no keys. The reference payload builders here drive the *real*
``scripts/post_review.py`` capture path (``post_json`` in DRY_RUN mode ->
``build_dry_run_payload``), so the committed fixtures under
``fixtures/adapter/`` are byte-identical to what ``post_review.py --dry-run``
emits for the call shapes each one covers — one documented exception is the
skipped-section prose ``build_skipped_section`` renders into a real review
body: these builders inject ``skip_warnings`` straight into the top-level
``skipped`` list instead. ``TestFixtureFidelity`` re-derives each payload
from these builders and asserts equality with the committed fixture,
guarding against drift in the post_review payload shape it covers.
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
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.post_review as post_review  # noqa: E402
from bench.adapter.adapt import merge_candidates, payload_to_candidates  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adapter"

GITHUB_FIXTURE = FIXTURES / "github_4_comments_2_skipped.json"
GITHUB_EMPTY_FIXTURE = FIXTURES / "github_empty.json"
GITLAB_FIXTURE = FIXTURES / "gitlab_shape.json"
GITHUB_PREFIXED_PATH_FIXTURE = FIXTURES / "github_prefixed_path.json"
GITLAB_PREFIXED_PATH_FIXTURE = FIXTURES / "gitlab_prefixed_path.json"

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
# every line any finding above cites, matching `_range_is_valid`'s real multiline
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

# Neither GL_FINDINGS entry carries suggested_fix_code today, so _gated_finding
# short-circuits before consulting either mapping — empty, not None, so the
# builder still exercises the "a diff WAS parsed" path rather than the
# validation-skipped one.
_GL_VALID_LINES: dict[tuple[str, int], int] = {}
_GL_LINE_TEXTS: dict[tuple[str, int], str] = {}


def _reset_post_review():
    post_review.reset_run_state()
    post_review.DRY_RUN = False


def _github_comment(f, valid_lines=_GH_VALID_LINES, line_texts=_GH_LINE_TEXTS):
    """Mirror post_github's per-finding comment construction exactly.

    Including the apply-check: `apply_range` is computed by the SAME formula
    `post_github` uses (issue #63 D2 — the multiline decision is made once,
    above the body render, and consumed by it), and the finding is gated
    through `_gated_finding` before `render_comment_body` ever sees it. A
    finding whose `suggested_fix_code` this gate would downgrade must render
    WITHOUT its fence here too, or this fixture is fiction the real poster
    never produces.
    """
    line = f["line"]
    filepath = post_review.diff_path_spelling(valid_lines, f["file"], line)
    end_line = f.get("end_line")
    multiline = (
        isinstance(end_line, int)
        and end_line >= line
        and end_line != line
        and post_review._range_is_valid(valid_lines, filepath, line, end_line)
    )
    apply_range = (line, end_line) if multiline else (line, line)
    gated = post_review._gated_finding(f, apply_range, valid_lines, line_texts)
    comment = {
        "path": filepath,
        "line": line,
        "side": "RIGHT",
        "body": post_review.render_comment_body(gated),
    }
    if multiline:
        comment["start_line"] = line
        comment["start_side"] = "RIGHT"
        comment["line"] = end_line
    return comment


def build_reference_github_payload(
    comment_findings,
    skip_warnings,
    owner="withastro",
    repo="astro",
    pr_number=1234,
    review_body="Automated review summary.",
    valid_lines=_GH_VALID_LINES,
    line_texts=_GH_LINE_TEXTS,
):
    """Build a GitHub dry-run payload via post_review's real capture path."""
    _reset_post_review()
    post_review.DRY_RUN = True
    comments = [_github_comment(f, valid_lines, line_texts) for f in comment_findings]
    total = len(comment_findings) + len(skip_warnings)
    body = review_body + post_review.build_footer(total, _GH_SHA)
    payload = {"body": body, "event": "COMMENT", "comments": comments}
    cmd_prefix = [
        "gh",
        "api",
        "--method",
        "POST",
        "-H",
        "Accept: application/vnd.github+json",
        f"repos/{owner}/{repo}/pulls/{pr_number}/reviews",
    ]
    post_review.post_json(cmd_prefix, payload)
    for w in skip_warnings:
        post_review._SKIP_WARNINGS.append(w)
    out = post_review.build_dry_run_payload("github")
    _reset_post_review()
    return out


def _gitlab_discussion(
    f, new_files=None, valid_lines=_GL_VALID_LINES, line_texts=_GL_LINE_TEXTS
):
    """Mirror post_gitlab's per-finding discussion payload — resolved path and
    OLD-side line number included, matching what the real poster sends.
    Rename-aware ``old_path`` is NOT mirrored: this mirror has no rename
    model, so a modified file's ``old_path`` is always the same resolved path
    as its ``new_path``.

    A GitLab position is always single-line, but a ```suggestion:-m+n header
    widens what one click replaces (#219). The apply range and the header's
    offsets are both taken from `_gitlab_anchored` — post_gitlab's own decision,
    called rather than copied, so this mirror cannot drift into fiction that
    stays green.
    """
    line = f["line"]
    gated, offsets = post_review._gitlab_anchored(f, line, valid_lines, line_texts)
    filepath = post_review.diff_path_spelling(valid_lines, f["file"], line)
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
    old_line = post_review.old_line_for(valid_lines, filepath, line)
    if old_line is not None:
        position["old_line"] = old_line
    # A newly-added file has no old version at all — old_path is omitted for
    # it exactly as the real poster omits it, via the same is_new_file call.
    if not post_review.is_new_file(new_files, filepath):
        position["old_path"] = filepath
    return {
        "body": post_review.render_comment_body(gated, fence_offsets=offsets),
        "position": position,
    }


def build_reference_gitlab_payload(
    findings,
    skip_warnings=(),
    project="gitlab-org/gitlab",
    mr_iid=999,
    review_body="Automated review summary.",
    valid_lines=_GL_VALID_LINES,
    line_texts=_GL_LINE_TEXTS,
    new_files=None,
):
    """Build a GitLab dry-run payload via post_review's real capture path."""
    _reset_post_review()
    post_review.DRY_RUN = True
    body = review_body + post_review.build_footer(len(findings), _GH_SHA)
    notes_cmd = [
        "glab",
        "api",
        "--method",
        "POST",
        f"projects/{project}/merge_requests/{mr_iid}/notes",
    ]
    post_review.post_json(notes_cmd, {"body": body})
    disc_cmd = [
        "glab",
        "api",
        "--method",
        "POST",
        f"projects/{project}/merge_requests/{mr_iid}/discussions",
    ]
    for f in findings:
        post_review.post_json(
            disc_cmd,
            _gitlab_discussion(
                f, new_files=new_files, valid_lines=valid_lines, line_texts=line_texts
            ),
        )
    for w in skip_warnings:
        post_review._SKIP_WARNINGS.append(w)
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


def _minimal_fake_subprocess_run(diff, versions=None):
    """Build a bare ``subprocess.run`` stand-in for one ``--dry-run main()`` call.

    Answers exactly the read-only calls a dry run makes for one platform: the
    tool-availability check, the diff fetch, and (GitLab only) the MR-versions
    GET. ``git remote get-url`` and ``git rev-parse`` are answered defensively
    — the findings JSON below always pins ``platform`` and ``sha``, so neither
    call should fire — but an unmodeled command raises rather than returning a
    fake success or failure: a silently degraded fake here would collapse
    ``valid_lines`` to None on a fetch failure and let every assertion below
    pass while validating nothing.
    """

    def _run(cmd, *args, **kwargs):
        def res(out=""):
            return SimpleNamespace(stdout=out, stderr="", returncode=0)

        if cmd[0] == "which":
            return res(out="/usr/bin/" + cmd[1])
        if cmd[:3] == ["gh", "pr", "diff"] or cmd[:3] == ["glab", "mr", "diff"]:
            return res(out=diff)
        if cmd[:2] == ["glab", "api"] and cmd[-1].endswith("/versions"):
            return res(out=json.dumps(versions if versions is not None else []))
        if cmd[:3] == ["git", "remote", "get-url"]:
            return res(out="git@github.com:o/r.git\n")
        if cmd[:2] == ["git", "rev-parse"]:
            return res(out="deadbeefcafe\n")
        raise AssertionError(f"unmodeled subprocess call in fixture driver: {cmd!r}")

    return _run


class _RealPosterTestCase(unittest.TestCase):
    """Drives the REAL ``post_review.main()`` over a tempdir findings file.

    A ``SystemExit`` from inside ``main()`` (e.g. a ``die()``) must still run
    cleanup, so the tempdir removal and the module-state reset are both
    registered with ``addCleanup`` rather than left to a plain ``tearDown``
    a raised exception could skip.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(_reset_post_review)

    def _run_main(self, findings_data, diff, versions=None):
        findings_path = os.path.join(self.tmp, "findings.json")
        with open(findings_path, "w", encoding="utf-8") as f:
            json.dump(findings_data, f)
        argv = ["post_review.py", findings_path, "--dry-run"]
        exit_code = None
        with (
            patch.object(sys, "argv", argv),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_minimal_fake_subprocess_run(diff, versions=versions),
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
        payload_path = os.path.join(self.tmp, "post-review-payload.json")
        with open(payload_path, encoding="utf-8") as f:
            return json.load(f)


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

        valid_lines, _, _, line_texts = post_review.parse_diff_text(
            "github", GH_DIFF_PREFIXED_PATH
        )
        self.assertTrue(valid_lines)
        self.assertIn(("src/edited.py", 2), valid_lines)

        mirror = build_reference_github_payload(
            GH_PREFIXED_PATH_FINDINGS,
            [],
            owner=GH_PREFIXED_PATH_OWNER,
            repo=GH_PREFIXED_PATH_REPO,
            pr_number=GH_PREFIXED_PATH_PR,
            valid_lines=valid_lines,
            line_texts=line_texts,
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

        valid_lines, _, _, line_texts = post_review.parse_diff_text(
            "github", GH_DIFF_TWO_FILE_COLLISION
        )
        mirror = build_reference_github_payload(
            [GH_TWO_FILE_COLLISION_FINDING],
            [],
            owner=GH_COLLISION_OWNER,
            repo=GH_COLLISION_REPO,
            pr_number=GH_COLLISION_PR,
            valid_lines=valid_lines,
            line_texts=line_texts,
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

        valid_lines, new_files, _, line_texts = post_review.parse_diff_text(
            "gitlab", GL_DIFF_PREFIXED_PATH
        )
        mirror = build_reference_gitlab_payload(
            GL_PREFIXED_PATH_FINDINGS,
            project=GL_PREFIXED_PATH_PROJECT,
            mr_iid=GL_PREFIXED_PATH_MR_IID,
            valid_lines=valid_lines,
            line_texts=line_texts,
            new_files=new_files,
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
            finding, valid_lines=valid_lines, line_texts=line_texts
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
        valid_lines, _, _, line_texts = post_review.parse_diff_text(
            "github", GH_DIFF_PREFIXED_PATH
        )
        expected = build_reference_github_payload(
            GH_PREFIXED_PATH_FINDINGS,
            [],
            owner=GH_PREFIXED_PATH_OWNER,
            repo=GH_PREFIXED_PATH_REPO,
            pr_number=GH_PREFIXED_PATH_PR,
            valid_lines=valid_lines,
            line_texts=line_texts,
        )
        self.assertEqual(_load_fixture(GITHUB_PREFIXED_PATH_FIXTURE), expected)

    def test_gitlab_prefixed_path_fixture_matches_post_review(self):
        valid_lines, new_files, _, line_texts = post_review.parse_diff_text(
            "gitlab", GL_DIFF_PREFIXED_PATH
        )
        expected = build_reference_gitlab_payload(
            GL_PREFIXED_PATH_FINDINGS,
            project=GL_PREFIXED_PATH_PROJECT,
            mr_iid=GL_PREFIXED_PATH_MR_IID,
            valid_lines=valid_lines,
            line_texts=line_texts,
            new_files=new_files,
        )
        self.assertEqual(_load_fixture(GITLAB_PREFIXED_PATH_FIXTURE), expected)

    def test_fixture_top_level_keys_are_the_dry_run_shape(self):
        gh = _load_fixture(GITHUB_FIXTURE)
        self.assertEqual(
            set(gh), {"platform", "endpoint", "method", "payload", "skipped"}
        )
        gl = _load_fixture(GITLAB_FIXTURE)
        self.assertEqual(set(gl), {"platform", "summary", "discussions", "skipped"})


if __name__ == "__main__":
    unittest.main()
