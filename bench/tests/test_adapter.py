"""Tests for bench/adapter/adapt.py — dry-run payload -> scorer candidates.

No network, no keys. The reference payload builders here drive the *real*
``scripts/post_review.py`` capture path (``post_json`` in DRY_RUN mode ->
``build_dry_run_payload``), so the committed fixtures under
``fixtures/adapter/`` are byte-identical to what ``post_review.py --dry-run``
emits. ``TestFixtureFidelity`` re-derives each payload from these builders and
asserts equality with the committed fixture, guarding against drift in the
post_review payload shape.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench.adapter import adapt  # noqa: E402
from bench.adapter.adapt import merge_candidates, payload_to_candidates  # noqa: E402
import scripts.post_review as post_review  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adapter"

GITHUB_FIXTURE = FIXTURES / "github_3_comments_1_skipped.json"
GITHUB_EMPTY_FIXTURE = FIXTURES / "github_empty.json"
GITLAB_FIXTURE = FIXTURES / "gitlab_shape.json"

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

# Three findings become inline comments; the second is multi-line (end_line set)
# so the emitted comment carries start_line and its ``line`` is the *end* line.
GH_COMMENT_FINDINGS = [
    {
        "file": "src/auth/session.py", "line": 42, "severity": "high",
        "title": "Missing null check on token",
        "body": ("load_token() returns None when the cookie is absent; the next "
                 "line dereferences it and raises AttributeError."),
    },
    {
        "file": "src/auth/session.py", "line": 88, "end_line": 92,
        "severity": "medium", "title": "Password hashed twice",
        "body": ("hash_password() runs here and again in save(); the double hash "
                 "makes the stored value fail verification."),
    },
    {
        "file": "src/api/routes.py", "line": 15, "severity": "critical",
        "title": "SQL injection via f-string",
        "body": "The user-supplied uid is interpolated straight into the SQL string.",
        "suggested_fix_code": (
            'cursor.execute("SELECT * FROM users WHERE id = %s", (uid,))'
        ),
    },
]
GH_SKIP_WARNINGS = [
    ("Skipping finding 'Docs typo' at README.md:999 — line not found in diff. "
     "Valid lines for this file: [3, 4, 5]"),
]

GL_FINDINGS = [
    {
        "file": "app/models/user.rb", "line": 27, "severity": "high",
        "title": "N+1 query in loop",
        "body": "user.posts is queried inside the each loop; preload before iterating.",
    },
    {
        "file": "app/controllers/sessions_controller.rb", "line": 5,
        "severity": "low", "title": "Unused parameter",
        "body": "The redirect_to param is never read.",
    },
]


def _reset_post_review():
    post_review.DRY_RUN = False
    post_review._CAPTURED.clear()
    post_review._SKIP_WARNINGS.clear()


def _github_comment(f):
    """Mirror post_github's per-finding comment construction exactly."""
    comment = {
        "path": f["file"],
        "line": f["line"],
        "side": "RIGHT",
        "body": post_review.render_comment_body(f),
    }
    end_line = f.get("end_line")
    if end_line and end_line != f["line"]:
        comment["start_line"] = f["line"]
        comment["start_side"] = "RIGHT"
        comment["line"] = end_line
    return comment


def build_reference_github_payload(comment_findings, skip_warnings,
                                   owner="withastro", repo="astro", pr_number=1234,
                                   review_body="Automated review summary."):
    """Build a GitHub dry-run payload via post_review's real capture path."""
    _reset_post_review()
    post_review.DRY_RUN = True
    comments = [_github_comment(f) for f in comment_findings]
    total = len(comment_findings) + len(skip_warnings)
    body = review_body + post_review.build_footer(total, _GH_SHA)
    payload = {"body": body, "event": "COMMENT", "comments": comments}
    cmd_prefix = [
        "gh", "api", "--method", "POST",
        "-H", "Accept: application/vnd.github+json",
        f"repos/{owner}/{repo}/pulls/{pr_number}/reviews",
    ]
    post_review.post_json(cmd_prefix, payload)
    for w in skip_warnings:
        post_review._SKIP_WARNINGS.append(w)
    out = post_review.build_dry_run_payload("github")
    _reset_post_review()
    return out


def _gitlab_discussion(f, new_file=False):
    """Mirror post_gitlab's per-finding discussion payload exactly."""
    position = {
        "position_type": "text",
        "base_sha": _GL_BASE,
        "head_sha": _GL_HEAD,
        "start_sha": _GL_START,
        "new_path": f["file"],
        "new_line": f["line"],
    }
    if not new_file:
        position["old_path"] = f["file"]
    return {"body": post_review.render_comment_body(f), "position": position}


def build_reference_gitlab_payload(findings, skip_warnings=(),
                                   project="gitlab-org/gitlab", mr_iid=999,
                                   review_body="Automated review summary."):
    """Build a GitLab dry-run payload via post_review's real capture path."""
    _reset_post_review()
    post_review.DRY_RUN = True
    body = review_body + post_review.build_footer(len(findings), _GH_SHA)
    notes_cmd = ["glab", "api", "--method", "POST",
                 f"projects/{project}/merge_requests/{mr_iid}/notes"]
    post_review.post_json(notes_cmd, {"body": body})
    disc_cmd = ["glab", "api", "--method", "POST",
                f"projects/{project}/merge_requests/{mr_iid}/discussions"]
    for f in findings:
        post_review.post_json(disc_cmd, _gitlab_discussion(f))
    for w in skip_warnings:
        post_review._SKIP_WARNINGS.append(w)
    out = post_review.build_dry_run_payload("gitlab")
    _reset_post_review()
    return out


def _load_fixture(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# payload_to_candidates — GitHub
# ---------------------------------------------------------------------------

class TestPayloadToCandidatesGitHub(unittest.TestCase):

    def test_three_comments_one_skipped(self):
        cands, stats = payload_to_candidates(str(GITHUB_FIXTURE), GOLDEN_A)
        self.assertEqual(stats, {"n_candidates": 3, "n_skipped": 1})
        self.assertIn(GOLDEN_A, cands)
        self.assertIn("deep-review", cands[GOLDEN_A])
        entries = cands[GOLDEN_A]["deep-review"]
        self.assertEqual(len(entries), 3)
        for e in entries:
            self.assertEqual(e["source"], "extracted")
            self.assertEqual(set(e), {"text", "path", "line", "source"})

    def test_order_and_verbatim_text(self):
        payload = _load_fixture(GITHUB_FIXTURE)
        posted = payload["payload"]["comments"]
        cands, _ = payload_to_candidates(payload, GOLDEN_A)
        entries = cands[GOLDEN_A]["deep-review"]
        # index i candidate corresponds to index i posted comment
        for i, (entry, comment) in enumerate(zip(entries, posted)):
            self.assertEqual(entry["text"], comment["body"],
                             f"candidate {i} text must be the body verbatim")
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
        cands, _ = payload_to_candidates(str(GITHUB_FIXTURE), GOLDEN_A,
                                         tool="deep-review-v2")
        self.assertIn("deep-review-v2", cands[GOLDEN_A])

    def test_accepts_path_object(self):
        cands, stats = payload_to_candidates(GITHUB_FIXTURE, GOLDEN_A)
        self.assertEqual(stats["n_candidates"], 3)

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
        for entry, disc in zip(entries, discussions):
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
        import shutil
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
        self.assertEqual(len(merged[GOLDEN_A]["deep-review"]), 3)
        self.assertEqual(len(merged[GOLDEN_B]["deep-review"]), 2)
        self.assertEqual(stats["n_candidates"], 5)
        self.assertEqual(stats["n_skipped"], 0)

    def test_merge_accepts_dicts_directly(self):
        c1, _ = payload_to_candidates(str(GITHUB_FIXTURE), GOLDEN_A)
        c2, _ = payload_to_candidates(str(GITLAB_FIXTURE), GOLDEN_B)
        merged, stats = merge_candidates([c1, c2])
        self.assertEqual(set(merged), {GOLDEN_A, GOLDEN_B})
        self.assertEqual(stats["n_candidates"], 5)

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
        self.assertEqual(len(merged[GOLDEN_A]["deep-review"]), 3)
        self.assertEqual(stats["n_candidates"], 3)

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
        expected = build_reference_github_payload(GH_COMMENT_FINDINGS,
                                                  GH_SKIP_WARNINGS)
        self.assertEqual(_load_fixture(GITHUB_FIXTURE), expected)

    def test_github_empty_fixture_matches_post_review(self):
        expected = build_reference_github_payload([], [])
        self.assertEqual(_load_fixture(GITHUB_EMPTY_FIXTURE), expected)

    def test_gitlab_fixture_matches_post_review(self):
        expected = build_reference_gitlab_payload(GL_FINDINGS)
        self.assertEqual(_load_fixture(GITLAB_FIXTURE), expected)

    def test_fixture_top_level_keys_are_the_dry_run_shape(self):
        gh = _load_fixture(GITHUB_FIXTURE)
        self.assertEqual(set(gh), {"platform", "endpoint", "method", "payload",
                                   "skipped"})
        gl = _load_fixture(GITLAB_FIXTURE)
        self.assertEqual(set(gl), {"platform", "summary", "discussions",
                                   "skipped"})


if __name__ == "__main__":
    unittest.main()
