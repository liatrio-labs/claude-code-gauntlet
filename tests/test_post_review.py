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
    render_comment_body,
    build_footer,
    gitlab_project_id,
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

    def test_high_severity_emoji(self):
        finding = {"severity": "high", "title": "Bug", "body": "Description of the bug."}
        body = render_comment_body(finding)
        self.assertIn("[HIGH]", body)

    def test_medium_severity_emoji(self):
        finding = {"severity": "medium", "title": "Issue", "body": "Description of the issue."}
        body = render_comment_body(finding)
        self.assertIn("[MEDIUM]", body)

    def test_low_severity_emoji(self):
        finding = {"severity": "low", "title": "Nit", "body": "Minor issue."}
        body = render_comment_body(finding)
        self.assertIn("[LOW]", body)

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


if __name__ == "__main__":
    unittest.main()
