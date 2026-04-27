"""
Tests for scripts/post_review.py

Covers:
  - detect_platform: GitHub SSH, GitHub HTTPS, GitLab SSH, GitLab HTTPS,
    unknown host, malformed URL
  - parse_diff_lines: (post_review version) same diff parsing as verify_findings
  - is_line_valid: exact match, stripped path, None valid_lines
  - render_comment_body: all severity emojis, with/without suggestion block
  - build_footer: metadata JSON in HTML comment
  - gitlab_project_id: URL encoding of owner/repo
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.post_review import (
    detect_platform,
    is_line_valid,
    parse_diff_lines,
    render_comment_body,
    build_footer,
    gitlab_project_id,
    valid_lines_for_file,
)


# ---------------------------------------------------------------------------
# detect_platform
# ---------------------------------------------------------------------------

class TestDetectPlatform(unittest.TestCase):

    @patch("scripts.post_review.run_api")
    def test_github_ssh(self, mock_run):
        mock_run.return_value = ("git@github.com:myorg/myrepo.git\n", "", 0)
        platform, host = detect_platform()
        self.assertEqual(platform, "github")
        self.assertEqual(host, "github.com")

    @patch("scripts.post_review.run_api")
    def test_github_https(self, mock_run):
        mock_run.return_value = ("https://github.com/myorg/myrepo.git\n", "", 0)
        platform, host = detect_platform()
        self.assertEqual(platform, "github")
        self.assertIn("github.com", host)

    @patch("scripts.post_review.run_api")
    def test_gitlab_ssh(self, mock_run):
        mock_run.return_value = ("git@gitlab.com:team/project.git\n", "", 0)
        platform, host = detect_platform()
        self.assertEqual(platform, "gitlab")

    @patch("scripts.post_review.run_api")
    def test_gitlab_https(self, mock_run):
        mock_run.return_value = ("https://gitlab.com/team/project.git\n", "", 0)
        platform, host = detect_platform()
        self.assertEqual(platform, "gitlab")

    @patch("scripts.post_review.run_api")
    def test_self_hosted_gitlab(self, mock_run):
        mock_run.return_value = ("git@gitlab.internal.company.com:team/project.git\n", "", 0)
        platform, host = detect_platform()
        self.assertEqual(platform, "gitlab")
        self.assertEqual(host, "gitlab.internal.company.com")

    @patch("scripts.post_review.run_api")
    def test_unknown_host(self, mock_run):
        mock_run.return_value = ("https://bitbucket.org/team/repo.git\n", "", 0)
        platform, host = detect_platform()
        self.assertIsNone(platform)
        self.assertEqual(host, "bitbucket.org")

    @patch("scripts.post_review.run_api")
    def test_git_remote_failure(self, mock_run):
        mock_run.return_value = ("", "fatal: not a git repository", 128)
        platform, host = detect_platform()
        self.assertIsNone(platform)
        self.assertIsNone(host)

    @patch("scripts.post_review.run_api")
    def test_malformed_url(self, mock_run):
        mock_run.return_value = ("not-a-url\n", "", 0)
        platform, host = detect_platform()
        self.assertIsNone(platform)
        self.assertIsNone(host)

    @patch("scripts.post_review.run_api")
    def test_github_ssh_without_git_suffix(self, mock_run):
        mock_run.return_value = ("git@github.com:myorg/myrepo\n", "", 0)
        platform, host = detect_platform()
        self.assertEqual(platform, "github")

    @patch("scripts.post_review.run_api")
    def test_github_https_without_git_suffix(self, mock_run):
        mock_run.return_value = ("https://github.com/myorg/myrepo\n", "", 0)
        platform, host = detect_platform()
        self.assertEqual(platform, "github")


# ---------------------------------------------------------------------------
# parse_diff_lines (post_review version)
# ---------------------------------------------------------------------------

class TestParseDiffLinesPostReview(unittest.TestCase):
    """Tests for parse_diff_lines in post_review, which dispatches via run_api."""

    @patch("scripts.post_review.run_api")
    def test_github_dispatches_to_gh_pr_diff(self, mock_run):
        """platform='github' must call gh pr diff."""
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " existing\n"
            "+added\n"
        )
        mock_run.return_value = (diff, "", 0)
        valid_lines, new_files = parse_diff_lines("github", "myorg", "myrepo", 42)
        self.assertIsNotNone(valid_lines)
        self.assertEqual(new_files, set())
        call_args = mock_run.call_args[0][0]
        self.assertEqual(call_args[0], "gh")
        self.assertEqual(call_args[1], "pr")
        self.assertEqual(call_args[2], "diff")

    @patch("scripts.post_review.run_api")
    def test_gitlab_dispatches_to_glab_mr_diff(self, mock_run):
        """platform='gitlab' must call glab mr diff."""
        diff = (
            "+++ b/bar.py\n"
            "@@ -5,1 +5,2 @@\n"
            " ctx\n"
            "+new_line\n"
        )
        mock_run.return_value = (diff, "", 0)
        valid_lines, _ = parse_diff_lines("gitlab", "myorg", "myrepo", 7)
        self.assertIsNotNone(valid_lines)
        call_args = mock_run.call_args[0][0]
        self.assertEqual(call_args[0], "glab")
        self.assertEqual(call_args[1], "mr")
        self.assertEqual(call_args[2], "diff")

    @patch("scripts.post_review.run_api")
    def test_glab_no_prefix_headers_are_parsed(self, mock_run):
        """`glab mr diff` emits headers without the `a/` / `b/` prefix.

        Regression: the regex previously required ``+++ b/<path>`` and dropped
        every header from glab, leaving valid_lines empty so all findings were
        rejected as ``line not found in diff``.
        """
        diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- src/app.py\n"
            "+++ src/app.py\n"
            "@@ -1,1 +1,2 @@\n"
            " ctx\n"
            "+added\n"
        )
        mock_run.return_value = (diff, "", 0)
        valid_lines, new_files = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertIn(("src/app.py", 1), valid_lines)
        self.assertIn(("src/app.py", 2), valid_lines)
        self.assertEqual(new_files, set())

    @patch("scripts.post_review.run_api")
    def test_new_file_detected_via_dev_null_old_header(self, mock_run):
        """Files added in the diff (``--- /dev/null``) must populate new_files."""
        diff = (
            "diff --git a/newfile.py b/newfile.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/newfile.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+line1\n"
            "+line2\n"
            "diff --git a/existing.py b/existing.py\n"
            "--- a/existing.py\n"
            "+++ b/existing.py\n"
            "@@ -1,1 +1,2 @@\n"
            " ctx\n"
            "+added\n"
        )
        mock_run.return_value = (diff, "", 0)
        valid_lines, new_files = parse_diff_lines("github", "o", "r", 1)
        self.assertEqual(new_files, {"newfile.py"})
        self.assertIn(("newfile.py", 1), valid_lines)
        self.assertIn(("existing.py", 2), valid_lines)

    @patch("scripts.post_review.run_api")
    def test_new_file_detected_with_no_prefix_headers(self, mock_run):
        """New-file detection must work for glab-style (no-prefix) headers too."""
        diff = (
            "--- /dev/null\n"
            "+++ src/added.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+content\n"
        )
        mock_run.return_value = (diff, "", 0)
        _, new_files = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(new_files, {"src/added.py"})

    @patch("scripts.post_review.run_api")
    def test_deleted_file_does_not_add_dev_null_to_valid_lines(self, mock_run):
        """``+++ /dev/null`` (deleted file) must not produce phantom entries."""
        diff = (
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-line1\n"
            "-line2\n"
        )
        mock_run.return_value = (diff, "", 0)
        valid_lines, new_files = parse_diff_lines("github", "o", "r", 1)
        self.assertEqual(valid_lines, set())
        self.assertEqual(new_files, set())

    @patch("scripts.post_review.run_api")
    def test_nonzero_rc_returns_none(self, mock_run):
        """A non-zero exit code from the CLI tool must return (None, None)."""
        mock_run.return_value = ("", "fatal: not a git repository", 128)
        valid_lines, new_files = parse_diff_lines("github", "myorg", "myrepo", 1)
        self.assertIsNone(valid_lines)
        self.assertIsNone(new_files)

    def test_unknown_platform_returns_none(self):
        """An unknown platform must return (None, None) without calling run_api."""
        valid_lines, new_files = parse_diff_lines("bitbucket", "myorg", "myrepo", 1)
        self.assertIsNone(valid_lines)
        self.assertIsNone(new_files)


# ---------------------------------------------------------------------------
# is_line_valid
# ---------------------------------------------------------------------------

class TestIsLineValid(unittest.TestCase):

    def test_none_valid_lines_always_true(self):
        self.assertTrue(is_line_valid(None, "any.py", 999))

    def test_exact_match(self):
        valid = {("src/app.py", 42)}
        self.assertTrue(is_line_valid(valid, "src/app.py", 42))

    def test_no_match(self):
        valid = {("src/app.py", 42)}
        self.assertFalse(is_line_valid(valid, "src/app.py", 43))

    def test_stripped_path(self):
        valid = {("src/app.py", 10)}
        self.assertTrue(is_line_valid(valid, "a/src/app.py", 10))
        self.assertTrue(is_line_valid(valid, "b/src/app.py", 10))


# ---------------------------------------------------------------------------
# render_comment_body
# ---------------------------------------------------------------------------

class TestRenderCommentBody(unittest.TestCase):

    def test_critical_severity_emoji(self):
        finding = {
            "severity": "critical",
            "title": "SQL Injection",
            "body": "User input is not sanitized before being passed to the database query.",
        }
        body = render_comment_body(finding)
        self.assertIn("[CRITICAL]", body)
        self.assertIn("\U0001f534", body)  # 🔴

    def test_high_severity_emoji(self):
        finding = {"severity": "high", "title": "Bug", "body": "Description of the bug."}
        body = render_comment_body(finding)
        self.assertIn("[HIGH]", body)
        self.assertIn("\U0001f7e0", body)  # 🟠

    def test_medium_severity_emoji(self):
        finding = {"severity": "medium", "title": "Issue", "body": "Description of the issue."}
        body = render_comment_body(finding)
        self.assertIn("[MEDIUM]", body)
        self.assertIn("\U0001f7e1", body)  # 🟡

    def test_low_severity_emoji(self):
        finding = {"severity": "low", "title": "Nit", "body": "Minor issue."}
        body = render_comment_body(finding)
        self.assertIn("[LOW]", body)
        self.assertIn("\U0001f4a1", body)  # 💡

    def test_with_suggestion_block(self):
        finding = {
            "severity": "high",
            "title": "Fix",
            "body": "Need to fix this.",
            "suggested_fix_code": "return None",
        }
        body = render_comment_body(finding)
        self.assertIn("```suggestion", body)
        self.assertIn("return None", body)

    def test_without_suggestion_block(self):
        finding = {
            "severity": "medium",
            "title": "Issue",
            "body": "Some description.",
        }
        body = render_comment_body(finding)
        self.assertNotIn("```suggestion", body)

    def test_missing_body(self):
        finding = {"severity": "low", "title": "Nit"}
        body = render_comment_body(finding)
        self.assertIn("[LOW]", body)
        self.assertIn("Nit", body)

    def test_unknown_severity_falls_back_to_bulb(self):
        finding = {"severity": "unknown", "title": "Thing", "body": "desc"}
        body = render_comment_body(finding)
        self.assertIn("\U0001f4a1", body)  # 💡 fallback
        self.assertIn("[UNKNOWN]", body)

    def test_empty_suggested_fix_code_treated_as_absent(self):
        finding = {
            "severity": "high",
            "title": "Bug",
            "body": "desc",
            "suggested_fix_code": "",
        }
        body = render_comment_body(finding)
        self.assertNotIn("```suggestion", body)

    def test_suggested_fix_code_none_treated_as_absent(self):
        finding = {
            "severity": "high",
            "title": "Bug",
            "body": "desc",
            "suggested_fix_code": None,
        }
        body = render_comment_body(finding)
        self.assertNotIn("```suggestion", body)

    def test_multiline_suggested_fix_code(self):
        finding = {
            "severity": "medium",
            "title": "Fix",
            "body": "desc",
            "suggested_fix_code": "line1\nline2\nline3",
        }
        body = render_comment_body(finding)
        self.assertIn("```suggestion", body)
        self.assertIn("line1\nline2\nline3", body)


# ---------------------------------------------------------------------------
# build_footer
# ---------------------------------------------------------------------------

class TestBuildFooter(unittest.TestCase):

    def test_footer_contains_metadata(self):
        footer = build_footer(5, "abc1234")
        self.assertIn("deep-review-findings:", footer)
        self.assertIn('"findings_count":5', footer)
        self.assertIn('"sha":"abc1234"', footer)
        self.assertIn("<!--", footer)
        self.assertIn("-->", footer)

    def test_footer_valid_json(self):
        footer = build_footer(3, "def5678")
        # Extract the JSON from the HTML comment
        import re
        m = re.search(r"deep-review-findings:\s*({.*})", footer)
        self.assertIsNotNone(m)
        data = json.loads(m.group(1))
        self.assertEqual(data["findings_count"], 3)
        self.assertEqual(data["sha"], "def5678")
        self.assertEqual(data["version"], "3.0")


# ---------------------------------------------------------------------------
# gitlab_project_id
# ---------------------------------------------------------------------------

class TestGitlabProjectId(unittest.TestCase):

    def test_simple_path(self):
        result = gitlab_project_id("myorg", "myrepo")
        self.assertEqual(result, "myorg%2Fmyrepo")

    def test_nested_path(self):
        result = gitlab_project_id("myorg/team", "myrepo")
        self.assertEqual(result, "myorg%2Fteam%2Fmyrepo")


# ---------------------------------------------------------------------------
# valid_lines_for_file
# ---------------------------------------------------------------------------

class TestValidLinesForFile(unittest.TestCase):

    def test_returns_none_when_valid_lines_is_none(self):
        self.assertIsNone(valid_lines_for_file(None, "foo.py"))

    def test_returns_sorted_lines_for_exact_file(self):
        valid = {("src/app.py", 10), ("src/app.py", 3), ("src/app.py", 7), ("other.py", 1)}
        result = valid_lines_for_file(valid, "src/app.py")
        self.assertEqual(result, [3, 7, 10])

    def test_returns_at_most_10(self):
        valid = {("f.py", i) for i in range(1, 21)}
        result = valid_lines_for_file(valid, "f.py")
        self.assertEqual(len(result), 10)
        self.assertEqual(result, list(range(1, 11)))

    def test_strips_leading_ab_prefix(self):
        valid = {("src/app.py", 5)}
        result = valid_lines_for_file(valid, "a/src/app.py")
        self.assertEqual(result, [5])

    def test_empty_when_no_match(self):
        valid = {("other.py", 1)}
        result = valid_lines_for_file(valid, "missing.py")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Diagnostic logging in skip warnings
# ---------------------------------------------------------------------------

class TestSkipWarningDiagnostics(unittest.TestCase):
    """Verify that skip warnings include valid-line diagnostics."""

    @patch("scripts.post_review.get_head_sha", return_value="abc123")
    @patch("scripts.post_review.check_tool")
    @patch("scripts.post_review.post_json", return_value={"html_url": "http://example.com"})
    @patch("scripts.post_review.warn")
    def test_github_skip_includes_valid_lines(self, mock_warn, _post, _tool, _sha):
        from scripts.post_review import post_github
        valid_lines = {("src/app.py", 10), ("src/app.py", 20)}
        data = {
            "owner": "o", "repo": "r", "pr_number": 1,
            "findings": [{"file": "src/app.py", "line": 99, "title": "Bug"}],
        }
        post_github(data, valid_lines)
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn("Valid lines for this file:", msg)
        self.assertIn("10", msg)
        self.assertIn("20", msg)

    @patch("scripts.post_review.get_head_sha", return_value="abc123")
    @patch("scripts.post_review.check_tool")
    @patch("scripts.post_review.post_json", return_value={"html_url": "http://example.com"})
    @patch("scripts.post_review.warn")
    def test_github_skip_no_diag_when_valid_lines_none(self, mock_warn, _post, _tool, _sha):
        from scripts.post_review import post_github
        data = {
            "owner": "o", "repo": "r", "pr_number": 1,
            "findings": [{"file": "src/app.py", "line": 99, "title": "Bug"}],
        }
        # valid_lines=None means validation was skipped, so is_line_valid returns True
        # and the skip branch is never entered. We need a set that doesn't contain
        # the line to trigger the skip, but None means no validation so no skip.
        # Instead, use an empty set so the line is not found.
        post_github(data, set())
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn("line not found in diff.", msg)
        # With an empty set, valid lines list is [] not None, so diag is present but empty
        self.assertIn("Valid lines for this file: []", msg)

    @patch("scripts.post_review.get_head_sha", return_value="abc123")
    @patch("scripts.post_review.check_tool")
    @patch("scripts.post_review.post_json", return_value={})
    @patch("scripts.post_review.fetch_gitlab_shas", return_value=("b", "h", "s"))
    @patch("scripts.post_review.warn")
    def test_gitlab_skip_includes_valid_lines(self, mock_warn, _shas, _post, _tool, _sha):
        from scripts.post_review import post_gitlab
        valid_lines = {("src/app.py", 5), ("src/app.py", 15)}
        data = {
            "owner": "o", "repo": "r", "pr_number": 1,
            "findings": [{"file": "src/app.py", "line": 99, "title": "Bug"}],
        }
        post_gitlab(data, valid_lines)
        # First call is for the summary note, skip warning is the second call
        found_diag = False
        for call in mock_warn.call_args_list:
            msg = call[0][0]
            if "Valid lines for this file:" in msg:
                found_diag = True
                self.assertIn("5", msg)
                self.assertIn("15", msg)
        self.assertTrue(found_diag, "Expected diagnostic in skip warning")


# ---------------------------------------------------------------------------
# is_new_file
# ---------------------------------------------------------------------------

class TestIsNewFile(unittest.TestCase):

    def test_none_new_files_returns_false(self):
        from scripts.post_review import is_new_file
        self.assertFalse(is_new_file(None, "any.py"))

    def test_empty_new_files_returns_false(self):
        from scripts.post_review import is_new_file
        self.assertFalse(is_new_file(set(), "any.py"))

    def test_exact_match(self):
        from scripts.post_review import is_new_file
        self.assertTrue(is_new_file({"src/added.py"}, "src/added.py"))

    def test_stripped_prefix_match(self):
        from scripts.post_review import is_new_file
        self.assertTrue(is_new_file({"src/added.py"}, "b/src/added.py"))
        self.assertTrue(is_new_file({"src/added.py"}, "a/src/added.py"))

    def test_no_match_returns_false(self):
        from scripts.post_review import is_new_file
        self.assertFalse(is_new_file({"src/added.py"}, "src/other.py"))


# ---------------------------------------------------------------------------
# GitLab discussion payload — new file vs modified file
# ---------------------------------------------------------------------------

class TestGitlabPositionPayload(unittest.TestCase):
    """Regression tests for GitLab's discussions API payload shape.

    GitLab returns HTTP 500 (after silently creating the discussion record,
    which then dangles as a hung thread) when a position object includes
    ``old_path`` for a file that's newly added in the MR. ``post_gitlab``
    must omit ``old_path`` for new files and include it for modified files.
    """

    def _capture_position(self, data, valid_lines, new_files):
        """Run post_gitlab and return the position dict from the discussion call."""
        from scripts.post_review import post_gitlab
        captured = []

        def fake_post_json(cmd_prefix, payload):
            captured.append((cmd_prefix, payload))
            return {}

        with patch("scripts.post_review.get_head_sha", return_value="abc123"), \
             patch("scripts.post_review.check_tool"), \
             patch("scripts.post_review.fetch_gitlab_shas", return_value=("base", "head", "start")), \
             patch("scripts.post_review.post_json", side_effect=fake_post_json):
            post_gitlab(data, valid_lines, new_files)

        # First post_json call is the summary note; second is the discussion.
        self.assertGreaterEqual(len(captured), 2, "expected summary + at least one discussion call")
        return captured[1][1]["position"]

    def test_new_file_position_omits_old_path(self):
        data = {
            "owner": "o", "repo": "r", "pr_number": 1,
            "findings": [{"file": "src/added.py", "line": 5, "title": "Bug", "body": "x"}],
        }
        valid_lines = {("src/added.py", 5)}
        new_files = {"src/added.py"}
        position = self._capture_position(data, valid_lines, new_files)
        self.assertNotIn("old_path", position,
                         "old_path must be omitted for newly-added files")
        self.assertEqual(position["new_path"], "src/added.py")
        self.assertEqual(position["new_line"], 5)

    def test_modified_file_position_includes_old_path(self):
        data = {
            "owner": "o", "repo": "r", "pr_number": 1,
            "findings": [{"file": "src/edited.py", "line": 10, "title": "Bug", "body": "x"}],
        }
        valid_lines = {("src/edited.py", 10)}
        new_files = set()
        position = self._capture_position(data, valid_lines, new_files)
        self.assertEqual(position["old_path"], "src/edited.py")
        self.assertEqual(position["new_path"], "src/edited.py")
        self.assertEqual(position["new_line"], 10)

    def test_new_files_none_falls_back_to_modified_behavior(self):
        """If new_files is None (e.g., diff fetch failed), retain old_path.

        Better to risk a 500 on a new-file finding than to lose anchoring on
        modified-file findings — and the diff-fetch-failed path is rare.
        """
        data = {
            "owner": "o", "repo": "r", "pr_number": 1,
            "findings": [{"file": "src/edited.py", "line": 10, "title": "Bug", "body": "x"}],
        }
        position = self._capture_position(data, valid_lines=None, new_files=None)
        self.assertEqual(position["old_path"], "src/edited.py")


if __name__ == "__main__":
    unittest.main()
