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
  - TestReviewMarkerRoundTripThroughRealPoster — issue #39 requirement 6's
    headline "write signal == read signal" guarantee, proven against the REAL
    post_github/post_gitlab DRY_RUN capture path rather than a
    re-implementation of their footer composition. See the class docstring.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.post_review as post_review
import scripts.review_marker as review_marker
from scripts.post_review import (
    build_footer,
    detect_platform,
    gitlab_project_id,
    is_line_valid,
    old_line_for,
    parse_diff_lines,
    render_comment_body,
    resolve_marker_sha,
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
        mock_run.return_value = (
            "git@gitlab.internal.company.com:team/project.git\n",
            "",
            0,
        )
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
        valid_lines, new_files, _ = parse_diff_lines("github", "myorg", "myrepo", 42)
        self.assertIsNotNone(valid_lines)
        self.assertEqual(new_files, set())
        call_args = mock_run.call_args[0][0]
        self.assertEqual(call_args[0], "gh")
        self.assertEqual(call_args[1], "pr")
        self.assertEqual(call_args[2], "diff")

    @patch("scripts.post_review.run_api")
    def test_gitlab_dispatches_to_glab_mr_diff(self, mock_run):
        """platform='gitlab' must call glab mr diff."""
        diff = "+++ b/bar.py\n@@ -5,1 +5,2 @@\n ctx\n+new_line\n"
        mock_run.return_value = (diff, "", 0)
        valid_lines, _, _ = parse_diff_lines("gitlab", "myorg", "myrepo", 7)
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
        valid_lines, new_files, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertIn(("src/app.py", 1), valid_lines)
        self.assertIn(("src/app.py", 2), valid_lines)
        self.assertEqual(new_files, set())

    @patch("scripts.post_review.run_api")
    def test_new_file_detected_via_dev_null_old_header(self, mock_run):
        """The ``/dev/null`` branch, exercised by header-shaped input with no hunk.

        SYNTHETIC FIXTURE, stated honestly: real ``git`` emits NO ``---``/``+++`` lines at
        all for an empty added file — just ``diff --git``, ``new file mode`` and ``index``
        (verified against real git). So the shape below is not one gh is known to emit
        today; the ``/dev/null`` branch is defence-in-depth for header-shaped input, and
        it is the ``@@ -0,0`` hunk signal that real added-file diffs actually trigger.
        The branch is still worth pinning: with a hunk present the ``-0,0`` signal alone
        satisfies the assertion, so mutating ``current_file_is_new`` would otherwise leave
        the suite green.
        """
        diff = (
            "diff --git a/empty_new.py b/empty_new.py\n"
            "new file mode 100644\n"
            "index 0000000..e69de29\n"
            "--- /dev/null\n"
            "+++ b/empty_new.py\n"
        )
        mock_run.return_value = (diff, "", 0)
        _, new_files, _ = parse_diff_lines("github", "o", "r", 1)
        self.assertEqual(new_files, {"empty_new.py"})

    @patch("scripts.post_review.run_api")
    def test_new_file_detected_from_hunk_header_glab_style(self, mock_run):
        """`glab mr diff` never writes `--- /dev/null` — it repeats the path on both
        sides, so `@@ -0,0` is the only added-file signal.

        The previous fixture paired a GitHub-only `/dev/null` header with glab-only
        unprefixed paths, a combination neither CLI emits, which is why the suite passed
        while `new_files` was permanently empty on GitLab (#127 D2).
        """
        diff = "--- src/added.py\n+++ src/added.py\n@@ -0,0 +1,1 @@\n+content\n"
        mock_run.return_value = (diff, "", 0)
        _, new_files, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(new_files, {"src/added.py"})

    @patch("scripts.post_review.run_api")
    def test_added_file_detected_end_of_multi_file_glab_diff(self, mock_run):
        """The added file is the SECOND file in the diff, and the modified one that
        precedes it must not be swept into new_files with it."""
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        _, new_files, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(new_files, {"src/added.py"})

    @patch("scripts.post_review.run_api")
    def test_deleted_file_does_not_add_dev_null_to_valid_lines(self, mock_run):
        """``+++ /dev/null`` (deleted file) must not produce phantom entries.

        The ``@@ -1,2 +0,0 @@`` header must not read as an added file either: it is the
        NEW side that is 0 here, and only an old-side start of 0 means "added".
        """
        diff = "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-line1\n-line2\n"
        mock_run.return_value = (diff, "", 0)
        valid_lines, new_files, _ = parse_diff_lines("github", "o", "r", 1)
        self.assertIsInstance(valid_lines, dict)
        self.assertEqual(valid_lines, {})
        self.assertEqual(new_files, set())

    @patch("scripts.post_review.run_api")
    def test_empty_file_gaining_content_is_treated_as_added_a_known_limitation(
        self, mock_run
    ):
        """A pre-existing EMPTY file gaining content emits `@@ -0,0 +1,N @@` with no
        /dev/null — byte-identical to a real added file in plain `glab mr diff` output.

        DOCUMENTED LIMITATION, pinned here deliberately: we read it as added, because
        sending `old_path` into a genuinely new file is the documented HTTP 500 and real
        added files are the common case (and the #127 defect).
        """
        diff = "--- a/empty.py\n+++ b/empty.py\n@@ -0,0 +1,2 @@\n+first\n+second\n"
        mock_run.return_value = (diff, "", 0)
        _, new_files, _ = parse_diff_lines("github", "o", "r", 1)
        self.assertEqual(new_files, {"empty.py"})

    @patch("scripts.post_review.run_api")
    def test_omitted_hunk_counts_default_to_one(self, mock_run):
        """``@@ -0,0 +1 @@`` — a one-line added file, as real git writes it.

        A unified-diff count is omitted exactly when that side holds ONE line, so the
        parser's ``1 if count is None else int(count)`` default carries real traffic.
        Both defaults previously had zero coverage: mutating them to 0 left the whole
        suite green, and under that mutation a one-line added file's only commentable
        line vanishes from ``valid_lines`` — every finding on it silently dropped.
        """
        diff = "--- oneline.txt\n+++ oneline.txt\n@@ -0,0 +1 @@\n+only\n"
        mock_run.return_value = (diff, "", 0)
        valid_lines, new_files, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertIn(("oneline.txt", 1), valid_lines)
        self.assertIsNone(valid_lines[("oneline.txt", 1)])
        self.assertIn("oneline.txt", new_files)

    @patch("scripts.post_review.run_api")
    def test_deleted_file_body_drains_budgets_so_the_next_file_parses(self, mock_run):
        """A deleted file's body must consume its budgets even though it records nothing.

        ``+++ /dev/null`` leaves ``current_file`` None, but skipping the body outright
        (``if current_file is None: continue``) leaves the old-side budget undrained, so
        the NEXT file's headers arrive while the parser is still in the hunk-body zone —
        where headers are not matched — and every comment target in that file is eaten.
        """
        diff = (
            "diff --git a/gone.py b/gone.py\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,3 +0,0 @@\n"
            "-a\n"
            "-b\n"
            "-c\n"
            "diff --git a/next.py b/next.py\n"
            "--- a/next.py\n"
            "+++ b/next.py\n"
            "@@ -5,2 +5,2 @@\n"
            " keep\n"
            "-x\n"
            "+y\n"
        )
        mock_run.return_value = (diff, "", 0)
        valid_lines, _, _ = parse_diff_lines("github", "o", "r", 1)
        self.assertEqual(valid_lines[("next.py", 5)], 5)
        self.assertIsNone(valid_lines[("next.py", 6)])
        self.assertEqual([k for k in valid_lines if k[0] == "gone.py"], [])

    # -- hunk-body budget tracking (headers are body content too) -----------

    @patch("scripts.post_review.run_api")
    def test_form_feed_line_content_does_not_split_the_hunk(self, mock_run):
        """A form feed is diff CONTENT; it must not invent a line boundary.

        ``str.splitlines()`` breaks on \\x0c (and \\x0b, \\x85, U+2028/U+2029) — git never
        emitted a boundary there. The extra line drains the declared budgets one line
        early, flips the header/body zone boundary, and ships a WRONG old_line for
        everything after it: here the ADDED last line would be reported as context on
        old line 3.
        """
        # Real `git diff` shape for changing p2 -> p2X in a file whose middle line is a
        # form feed (built in Python so the control character is explicit).
        diff = "--- ff.py\n+++ ff.py\n@@ -1,3 +1,3 @@\n p1\n \x0c\n-p2\n+p2X\n"
        mock_run.return_value = (diff, "", 0)
        valid_lines, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertIn(("ff.py", 3), valid_lines)
        self.assertIsNone(valid_lines[("ff.py", 3)])
        self.assertEqual(valid_lines[("ff.py", 2)], 2)

    @patch("scripts.post_review.run_api")
    def test_removed_line_content_starting_with_dashes_is_not_a_file_header(
        self, mock_run
    ):
        """Removing `-- deprecated: drop me` renders `--- deprecated: drop me`.

        Matched as an old-side file header it consumed no OLD number, desyncing
        `old_line` for every later line of the hunk — a WRONG old_line on the wire,
        worse than the 400 it replaces.
        """
        diff = (
            "diff --git a/db/schema.sql b/db/schema.sql\n"
            "--- db/schema.sql\n"
            "+++ db/schema.sql\n"
            "@@ -10,4 +10,3 @@\n"
            " CREATE TABLE t (\n"
            # A removed line whose CONTENT is `-- deprecated: drop me`.
            "--- deprecated: drop me\n"
            "   id INT,\n"
            " );\n"
        )
        mock_run.return_value = (diff, "", 0)
        valid_lines, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        # new 10 = old 10 (context), then the removal eats old 11 with no new number,
        # so new 11 must map to old 12 — not to 11.
        self.assertEqual(valid_lines[("db/schema.sql", 10)], 10)
        self.assertEqual(valid_lines[("db/schema.sql", 11)], 12)
        self.assertEqual(valid_lines[("db/schema.sql", 12)], 13)

    @patch("scripts.post_review.run_api")
    def test_added_line_content_starting_with_pluses_is_not_a_file_header(
        self, mock_run
    ):
        """Adding `++ x` renders `+++ x`. Matched as a new-side file header it
        retargeted `current_file` at the literal text and reset both counters."""
        diff = (
            "diff --git a/src/app.c b/src/app.c\n"
            "--- src/app.c\n"
            "+++ src/app.c\n"
            "@@ -20,2 +20,3 @@\n"
            " int i = 0;\n"
            "+++ x\n"
            " use(i);\n"
        )
        mock_run.return_value = (diff, "", 0)
        valid_lines, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(valid_lines[("src/app.c", 20)], 20)
        self.assertIsNone(valid_lines[("src/app.c", 21)])
        # The context line after it still records under the original file, with the
        # numbers the added line advanced.
        self.assertEqual(valid_lines[("src/app.c", 22)], 21)
        self.assertEqual({fp for fp, _ in valid_lines}, {"src/app.c"})

    @patch("scripts.post_review.run_api")
    def test_binary_file_prose_is_not_admitted_as_a_valid_line(self, mock_run):
        """`Binary files … differ` carries no hunk; it must not become a context line."""
        diff = (
            "diff --git a/img.png b/img.png\n"
            "--- a/img.png\n"
            "+++ b/img.png\n"
            "Binary files a/img.png and b/img.png differ\n"
        )
        mock_run.return_value = (diff, "", 0)
        valid_lines, _, _ = parse_diff_lines("github", "o", "r", 1)
        self.assertEqual([k for k in valid_lines if k[0] == "img.png"], [])

    @patch("scripts.post_review.run_api")
    def test_pre_hunk_lines_are_not_admitted(self, mock_run):
        """`diff --git` / `index` lines between files are not commentable lines.

        They used to fall through to the context branch and be admitted under the
        PREVIOUS file at its next new-side number.
        """
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        valid_lines, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(
            sorted(valid_lines),
            [
                ("src/added.py", 1),
                ("src/added.py", 2),
                ("src/edited.py", 61),
                ("src/edited.py", 62),
                ("src/edited.py", 63),
            ],
        )

    # -- old-side tracking (issue #127 D1) ---------------------------------

    @patch("scripts.post_review.run_api")
    def test_valid_lines_is_a_mapping_of_new_line_to_old_line(self, mock_run):
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        valid_lines, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertIsInstance(valid_lines, dict)

    @patch("scripts.post_review.run_api")
    def test_context_line_maps_to_its_old_side_number(self, mock_run):
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        valid_lines, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(valid_lines[("src/edited.py", 61)], 50)

    @patch("scripts.post_review.run_api")
    def test_added_line_maps_to_none(self, mock_run):
        """An added line exists only on the new side — present as a key, valued None."""
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        valid_lines, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertIn(("src/edited.py", 62), valid_lines)
        self.assertIsNone(valid_lines[("src/edited.py", 62)])

    @patch("scripts.post_review.run_api")
    def test_removed_line_advances_the_old_side_only(self, mock_run):
        """52, not 51: the ``-removed`` line consumed an OLD number and no new one."""
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        valid_lines, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(valid_lines[("src/edited.py", 63)], 52)

    @patch("scripts.post_review.run_api")
    def test_old_side_counter_resets_between_files(self, mock_run):
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        valid_lines, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertIsNone(valid_lines[("src/added.py", 1)])
        # The `diff --git` line introducing src/added.py sits between hunks; it used to
        # be read as a context line and admitted as a phantom target on the file before.
        self.assertNotIn(("src/edited.py", 64), valid_lines)
        self.assertEqual(
            {v for (fp, _), v in valid_lines.items() if fp == "src/added.py"},
            {None},
            "an added file's lines must carry no old-side leftovers from the file before",
        )

    @patch("scripts.post_review.run_api")
    def test_no_newline_marker_advances_neither_counter(self, mock_run):
        diff = (
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            " a\n"
            "\\ No newline at end of file\n"
            " b\n"
        )
        mock_run.return_value = (diff, "", 0)
        valid_lines, _, _ = parse_diff_lines("github", "o", "r", 1)
        self.assertEqual(valid_lines[("f.py", 2)], 2)

    @patch("scripts.post_review.run_api")
    def test_renamed_file_old_side_path_is_captured(self, mock_run):
        """A rename's `---` path must survive parsing keyed by the NEW path.

        The parser previously read the old-side header only to compare it to
        ``/dev/null`` and threw the path away, so a renamed file's position shipped the
        post-rename path as ``old_path`` — a path that does not exist on the old side
        (#130). The `rename from`/`rename to`/`similarity index` lines sit in the header
        zone and must stay no-ops.
        """
        mock_run.return_value = (GL_DIFF_RENAME, "", 0)
        valid_lines, _, old_paths = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(old_paths, {"new_name.py": "old_name.py"})
        self.assertEqual(valid_lines[("new_name.py", 3)], 3)

    @patch("scripts.post_review.run_api")
    def test_added_file_absent_from_old_paths(self, mock_run):
        """`--- /dev/null` means there is no old side — record no mapping at all."""
        diff = (
            "diff --git a/added.py b/added.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/added.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+content\n"
        )
        mock_run.return_value = (diff, "", 0)
        _, new_files, old_paths = parse_diff_lines("github", "o", "r", 1)
        self.assertEqual(new_files, {"added.py"})
        self.assertNotIn("added.py", old_paths)

    @patch("scripts.post_review.run_api")
    def test_unrenamed_file_old_path_maps_to_itself(self, mock_run):
        """For a plain modified file both sides name the same path — pin the coincide
        case, so the mapping is provably a no-op there rather than accidentally right."""
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        _, _, old_paths = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(old_paths["src/edited.py"], "src/edited.py")

    @patch("scripts.post_review.run_api")
    def test_nonzero_rc_returns_none(self, mock_run):
        """A non-zero exit code from the CLI tool must return (None, None, None)."""
        mock_run.return_value = ("", "fatal: not a git repository", 128)
        valid_lines, new_files, old_paths = parse_diff_lines(
            "github", "myorg", "myrepo", 1
        )
        self.assertIsNone(valid_lines)
        self.assertIsNone(new_files)
        self.assertIsNone(old_paths)

    def test_unknown_platform_returns_none(self):
        """An unknown platform must return (None, None, None) without calling run_api."""
        valid_lines, new_files, old_paths = parse_diff_lines(
            "bitbucket", "myorg", "myrepo", 1
        )
        self.assertIsNone(valid_lines)
        self.assertIsNone(new_files)
        self.assertIsNone(old_paths)


# ---------------------------------------------------------------------------
# is_line_valid
# ---------------------------------------------------------------------------


class TestIsLineValid(unittest.TestCase):
    def test_none_valid_lines_always_true(self):
        self.assertTrue(is_line_valid(None, "any.py", 999))

    def test_exact_match(self):
        valid = {("src/app.py", 42): 30}
        self.assertTrue(is_line_valid(valid, "src/app.py", 42))

    def test_no_match(self):
        valid = {("src/app.py", 42): 30}
        self.assertFalse(is_line_valid(valid, "src/app.py", 43))

    def test_stripped_path(self):
        valid = {("src/app.py", 10): 4}
        self.assertTrue(is_line_valid(valid, "a/src/app.py", 10))
        self.assertTrue(is_line_valid(valid, "b/src/app.py", 10))

    def test_context_key_membership_is_unaffected_by_the_value(self):
        """``valid_lines`` is a mapping now (issue #127): an added line's ``None``
        value must still read as "this line can carry a comment"."""
        self.assertTrue(is_line_valid({("src/app.py", 7): None}, "src/app.py", 7))
        self.assertTrue(is_line_valid({("src/app.py", 7): 5}, "src/app.py", 7))


# ---------------------------------------------------------------------------
# old_line_for (issue #127 D1)
# ---------------------------------------------------------------------------


class TestOldLineFor(unittest.TestCase):
    """GitLab addresses a context line only when the position carries BOTH sides.

    ``old_line_for`` is the lookup that supplies the old-side number; returning None
    means "send no ``old_line``".
    """

    def test_returns_old_line_for_exact_key(self):
        self.assertEqual(old_line_for({("src/app.py", 61): 50}, "src/app.py", 61), 50)

    def test_returns_none_for_added_line(self):
        self.assertIsNone(old_line_for({("src/app.py", 61): None}, "src/app.py", 61))

    def test_strips_leading_ab_prefix(self):
        # A finding path carrying the diff prefix passes is_line_valid through the
        # stripped form; a raw-key-only lookup here would answer None and re-arm the 400.
        self.assertEqual(old_line_for({("src/app.py", 61): 50}, "b/src/app.py", 61), 50)
        self.assertEqual(old_line_for({("src/app.py", 61): 50}, "a/src/app.py", 61), 50)

    def test_returns_none_when_validation_skipped(self):
        self.assertIsNone(old_line_for(None, "f.py", 1))

    def test_returns_none_for_a_legacy_set_container(self):
        """A set has no old-side data — degrade like ``new_files=None`` rather than
        raising AttributeError on a caller that never migrated."""
        self.assertIsNone(old_line_for({("f.py", 1)}, "f.py", 1))

    def test_returns_none_for_unknown_path(self):
        self.assertIsNone(old_line_for({("f.py", 1): 1}, "other.py", 1))


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
        finding = {
            "severity": "high",
            "title": "Bug",
            "body": "Description of the bug.",
        }
        body = render_comment_body(finding)
        self.assertIn("[HIGH]", body)
        self.assertIn("\U0001f7e0", body)  # 🟠

    def test_medium_severity_emoji(self):
        finding = {
            "severity": "medium",
            "title": "Issue",
            "body": "Description of the issue.",
        }
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

    # -- suggestion (issue #47) -------------------------------------------

    def test_suggestion_present_renders_prose_block(self):
        finding = {
            "severity": "high",
            "title": "Bug",
            "body": "desc",
            "suggestion": "Use parameterized queries instead.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**", body)
        self.assertIn("Use parameterized queries instead.", body)

    def test_suggestion_absent_no_heading(self):
        finding = {"severity": "high", "title": "Bug", "body": "desc"}
        body = render_comment_body(finding)
        self.assertNotIn("Suggested fix:", body)

    def test_suggestion_empty_string_no_heading(self):
        finding = {"severity": "high", "title": "Bug", "body": "desc", "suggestion": ""}
        body = render_comment_body(finding)
        self.assertNotIn("Suggested fix:", body)

    def test_suggestion_none_no_heading(self):
        finding = {
            "severity": "high",
            "title": "Bug",
            "body": "desc",
            "suggestion": None,
        }
        body = render_comment_body(finding)
        self.assertNotIn("Suggested fix:", body)

    def test_suggestion_whitespace_only_no_heading(self):
        finding = {
            "severity": "high",
            "title": "Bug",
            "body": "desc",
            "suggestion": "   \n  ",
        }
        body = render_comment_body(finding)
        self.assertNotIn("Suggested fix:", body)

    # -- claude_md_rule / spec_text (issue #47) ---------------------------

    def test_claude_md_rule_present_renders_cited_rule(self):
        finding = {
            "severity": "medium",
            "title": "Convention violation",
            "body": "desc",
            "claude_md_rule": "Scripts must be stdlib-only Python.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:** Scripts must be stdlib-only Python.", body)

    def test_spec_text_present_claude_md_rule_absent_renders_as_cited_rule(self):
        finding = {
            "severity": "medium",
            "title": "Intent mismatch",
            "body": "desc",
            "spec_text": "The spec says X must happen before Y.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:** The spec says X must happen before Y.", body)

    def test_both_claude_md_rule_and_spec_text_present_rule_wins(self):
        finding = {
            "severity": "medium",
            "title": "Both",
            "body": "desc",
            "claude_md_rule": "The CLAUDE.md rule.",
            "spec_text": "The spec text.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:** The CLAUDE.md rule.", body)
        self.assertNotIn("The spec text.", body)

    def test_neither_claude_md_rule_nor_spec_text_no_heading(self):
        finding = {"severity": "medium", "title": "Neither", "body": "desc"}
        body = render_comment_body(finding)
        self.assertNotIn("Cited rule:", body)

    def test_claude_md_rule_empty_falls_back_to_spec_text(self):
        finding = {
            "severity": "medium",
            "title": "Fallback",
            "body": "desc",
            "claude_md_rule": "",
            "spec_text": "The spec text wins here.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:** The spec text wins here.", body)

    # -- ordering / combinations -------------------------------------------

    def test_suggestion_and_suggested_fix_code_both_render_prose_before_fence(self):
        finding = {
            "severity": "high",
            "title": "Fix",
            "body": "desc",
            "suggestion": "Explain the fix in words.",
            "suggested_fix_code": "return None",
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**", body)
        self.assertIn("Explain the fix in words.", body)
        self.assertIn("```suggestion", body)
        self.assertIn("return None", body)
        prose_idx = body.index("**Suggested fix:**")
        fence_idx = body.index("```suggestion")
        self.assertLess(
            prose_idx,
            fence_idx,
            "the prose suggestion block must come before the fence",
        )
        # The fence is still the last thing in the body.
        self.assertTrue(body.rstrip("\n").endswith("```"))

    def test_multiline_suggestion_renders_without_corrupting_markdown(self):
        finding = {
            "severity": "medium",
            "title": "Fix",
            "body": "desc",
            "suggestion": "First do this.\nThen do that.\nFinally this.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**", body)
        self.assertIn("First do this.\nThen do that.\nFinally this.", body)

    def test_non_string_suggestion_and_rule_do_not_crash(self):
        finding = {
            "severity": "medium",
            "title": "Weird types",
            "body": "desc",
            "suggestion": 42,
            "claude_md_rule": 7,
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**", body)
        self.assertIn("42", body)
        self.assertIn("**Cited rule:** 7", body)

    def test_no_new_fields_produces_byte_identical_output(self):
        """Regression pin: a finding with none of the new fields must produce
        exactly the same output as before this change, so the addition cannot
        silently reflow an ordinary comment."""
        finding = {
            "severity": "high",
            "title": "SQL Injection",
            "body": "User input is not sanitized before being passed to the database query.",
        }
        body = render_comment_body(finding)
        self.assertEqual(
            body,
            "**\U0001f7e0 [HIGH] SQL Injection**\n\n"
            "User input is not sanitized before being passed to the database query.",
        )

    def test_whitespace_only_claude_md_rule_falls_back_to_spec_text(self):
        # Symmetry with the empty-string fallback above: blank-but-present must be
        # indistinguishable from absent on BOTH halves of the cited-rule lookup, or an
        # agent that emits "  " suppresses the spec_text it should have deferred to.
        finding = {
            "severity": "low",
            "title": "Intent drift",
            "body": "desc",
            "claude_md_rule": "   ",
            "spec_text": "The endpoint MUST return 422 on a schema violation.",
        }
        body = render_comment_body(finding)
        self.assertIn(
            "**Cited rule:** The endpoint MUST return 422 on a schema violation.", body
        )

    def test_non_string_spec_text_does_not_crash(self):
        finding = {"severity": "low", "title": "T", "body": "b", "spec_text": 9}
        self.assertIn("**Cited rule:** 9", render_comment_body(finding))

    def test_leading_newlines_are_stripped_from_rendered_values(self):
        # The sections are joined with their own blank lines, so a value padded at the FRONT
        # (a model that opens its suggestion with a newline) put a second blank line under the
        # heading. Only newlines are stripped — leading spaces belong to the value.
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "suggestion": "\n\n  indented advice",
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**\n  indented advice", body)

    def test_trailing_newlines_are_stripped_from_rendered_values(self):
        # _rendered_text promises this; without it a multi-line suggestion pushes a blank
        # line into whatever section follows (and, at the end, trails the comment).
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "suggestion": "Line one\nLine two\n\n",
            "claude_md_rule": "Rule text\n",
        }
        body = render_comment_body(finding)
        self.assertIn(
            "**Suggested fix:**\nLine one\nLine two\n\n**Cited rule:** Rule text", body
        )
        self.assertFalse(body.endswith("\n"))

    def test_non_string_suggested_fix_code_does_not_crash(self):
        # Pre-#47 this reached .rstrip() and raised AttributeError. The field now goes
        # through the same normalizer as the prose fields.
        finding = {
            "severity": "low",
            "title": "T",
            "body": "b",
            "suggested_fix_code": 123,
        }
        self.assertIn("```suggestion\n123\n```", render_comment_body(finding))

    def test_whitespace_only_suggested_fix_code_renders_no_fence(self):
        # A whitespace-only replacement would render a one-click-apply block that BLANKS
        # the cited lines — treat it as absent, like every other optional field.
        finding = {
            "severity": "low",
            "title": "T",
            "body": "b",
            "suggested_fix_code": "   \n  ",
        }
        self.assertNotIn("```suggestion", render_comment_body(finding))

    def test_artifact_only_fields_never_reach_the_comment_body(self):
        # A deliberate scoping decision from issue #47, pinned so it is a decision and not
        # a comment: these fields are carried end-to-end to the artifact and the report, but
        # the posted comment stays short. Changing that should require changing this test.
        finding = {
            "severity": "high",
            "title": "Missing rollback test",
            "body": "The rollback path is untested.",
            "suggestion": "Add a test that raises PaymentGatewayError.",
            "criticality": 9,
            "failure_scenario": "SENTINEL_FAILURE_SCENARIO",
            "evidence": "SENTINEL_EVIDENCE",
            "confidence": 90,
            "dimension": "test_coverage",
            "origin": "new",
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**", body)
        for sentinel in (
            "SENTINEL_FAILURE_SCENARIO",
            "SENTINEL_EVIDENCE",
            "criticality",
            "test_coverage",
            "90",
        ):
            self.assertNotIn(
                sentinel, body, f"{sentinel!r} leaked into the comment body"
            )


# ---------------------------------------------------------------------------
# build_footer
# ---------------------------------------------------------------------------


class TestBuildFooter(unittest.TestCase):
    def test_footer_contains_metadata(self):
        footer = build_footer(5, "abc1234")
        self.assertIn("code-gauntlet-findings:", footer)
        self.assertIn('"findings_count":5', footer)
        self.assertIn('"sha":"abc1234"', footer)
        self.assertIn("<!--", footer)
        self.assertIn("-->", footer)

    def test_footer_valid_json(self):
        footer = build_footer(3, "def5678")
        # Extract the JSON from the HTML comment
        import re

        m = re.search(r"code-gauntlet-findings:\s*({.*})", footer)
        self.assertIsNotNone(m)
        data = json.loads(m.group(1))
        self.assertEqual(data["findings_count"], 3)
        self.assertEqual(data["sha"], "def5678")
        self.assertEqual(data["version"], "3.0")


# ---------------------------------------------------------------------------
# resolve_marker_sha (Issue #39 D6)
# ---------------------------------------------------------------------------


class TestResolveMarkerSha(unittest.TestCase):
    """The persisted payload's own ``sha`` (the commit the review actually ran
    against) is preferred over a freshly re-resolved HEAD, so a HEAD that moved
    between the workflow run and the post cannot mislabel the marker."""

    @patch("scripts.post_review.get_head_sha", return_value="deadbeef")
    def test_prefers_data_sha_when_sha_shaped(self, mock_head):
        data = {"sha": "0f1e2d3c4b5a69788716253413121110090807a"}
        self.assertEqual(resolve_marker_sha(data), data["sha"])
        mock_head.assert_not_called()

    @patch("scripts.post_review.get_head_sha", return_value="deadbeef")
    def test_falls_back_to_head_when_sha_absent(self, mock_head):
        self.assertEqual(resolve_marker_sha({}), "deadbeef")
        mock_head.assert_called_once()

    @patch("scripts.post_review.get_head_sha", return_value="deadbeef")
    def test_falls_back_to_head_when_sha_none(self, mock_head):
        self.assertEqual(resolve_marker_sha({"sha": None}), "deadbeef")

    @patch("scripts.post_review.get_head_sha", return_value="deadbeef")
    def test_rejects_non_sha_shaped_value_and_falls_back(self, mock_head):
        self.assertEqual(resolve_marker_sha({"sha": "not-a-real-sha!!"}), "deadbeef")

    @patch("scripts.post_review.get_head_sha", return_value="deadbeef")
    def test_rejects_non_string_sha_and_falls_back(self, mock_head):
        self.assertEqual(resolve_marker_sha({"sha": 12345}), "deadbeef")

    @patch("scripts.post_review.get_head_sha", return_value="deadbeef")
    def test_accepts_short_sha_shaped_value(self, mock_head):
        self.assertEqual(resolve_marker_sha({"sha": "abc1234"}), "abc1234")
        mock_head.assert_not_called()

    @patch("scripts.post_review.warn")
    @patch("scripts.post_review.get_head_sha", return_value="unknown")
    def test_degraded_fallback_when_head_sha_itself_unresolvable(
        self, mock_head, mock_warn
    ):
        """git rev-parse HEAD failing makes get_head_sha() return "unknown" —
        not SHA-shaped, so the caller must be warned the posted marker will be
        undetectable rather than have the failure pass silently."""
        self.assertEqual(resolve_marker_sha({}), "unknown")
        mock_warn.assert_called_once()
        self.assertIn("unknown", mock_warn.call_args[0][0])


# ---------------------------------------------------------------------------
# Review-marker round trip through the REAL poster (Issue #39 Requirement 6)
# ---------------------------------------------------------------------------


class TestReviewMarkerRoundTripThroughRealPoster(unittest.TestCase):
    """THE test that discharges issue #39 requirement 6 unambiguously: the
    "write signal == read signal" guarantee proven against the bytes
    post_review.py actually posts.

    tests/test_review_marker.py's TestRoundTrip re-implements post_github's
    and post_gitlab's footer composition inline
    (``review_body += build_footer(len(findings), sha, body=review_body)``)
    and then parses ITS OWN re-implementation. That proves the
    re-implementation round-trips — it would keep passing even if
    post_github/post_gitlab stopped appending the footer entirely, since
    nothing in that test ever calls the real posting functions.

    This test instead drives the REAL ``post_review.post_github`` and
    ``post_review.post_gitlab`` with ``DRY_RUN`` mode (the same capture
    technique bench/tests/test_adapter.py's reference-payload builders use:
    set ``post_review.DRY_RUN = True``, call the poster, then read
    ``post_review._CAPTURED``), pulls the posted body straight out of the
    captured payload — GitHub: ``_CAPTURED[0]["payload"]["body"]``; GitLab:
    the summary note is the first capture, same path — and feeds THAT EXACT
    STRING to ``scripts.review_marker.detect_signal``, asserting the
    recovered sha equals the sha the poster was given. Covers both platforms
    and both an empty ``review_body`` (the ``workflows/src/stages.js``
    default) and a non-empty one. If post_github/post_gitlab stopped
    appending the footer, THIS test would fail; the re-implementation-based
    one would not.
    """

    def setUp(self):
        post_review.DRY_RUN = True
        post_review._CAPTURED.clear()
        post_review._SKIP_WARNINGS.clear()

    def tearDown(self):
        post_review.DRY_RUN = False
        post_review._CAPTURED.clear()
        post_review._SKIP_WARNINGS.clear()

    @patch("scripts.post_review.check_tool")
    def test_github_empty_review_body(self, _tool):
        sha = "a" * 40
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "sha": sha,
            "review_body": "",
            "findings": [],
        }
        post_review.post_github(data, {})
        body = post_review._CAPTURED[0]["payload"]["body"]
        signal = review_marker.detect_signal(body)
        self.assertIsNotNone(signal, f"no signal recovered from posted body: {body!r}")
        self.assertEqual(signal["sha"], sha)

    @patch("scripts.post_review.check_tool")
    def test_github_non_empty_review_body(self, _tool):
        sha = "b" * 40
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "sha": sha,
            "review_body": "## Summary\nSome pre-existing narrative text.\n",
            "findings": [],
        }
        post_review.post_github(data, {})
        body = post_review._CAPTURED[0]["payload"]["body"]
        signal = review_marker.detect_signal(body)
        self.assertIsNotNone(signal, f"no signal recovered from posted body: {body!r}")
        self.assertEqual(signal["sha"], sha)

    @patch("scripts.post_review.check_tool")
    @patch(
        "scripts.post_review.fetch_gitlab_shas", return_value=("base", "head", "start")
    )
    def test_gitlab_empty_review_body(self, _shas, _tool):
        sha = "c" * 40
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "sha": sha,
            "review_body": "",
            "findings": [],
        }
        post_review.post_gitlab(data, {})
        # The summary note is posted before any per-finding discussion, so
        # with findings=[] it is also the only capture.
        body = post_review._CAPTURED[0]["payload"]["body"]
        signal = review_marker.detect_signal(body)
        self.assertIsNotNone(signal, f"no signal recovered from posted body: {body!r}")
        self.assertEqual(signal["sha"], sha)

    @patch("scripts.post_review.check_tool")
    @patch(
        "scripts.post_review.fetch_gitlab_shas", return_value=("base", "head", "start")
    )
    def test_gitlab_non_empty_review_body(self, _shas, _tool):
        sha = "d" * 40
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "sha": sha,
            "review_body": "## MR Review\nContext for the reviewer.\n",
            "findings": [],
        }
        post_review.post_gitlab(data, {})
        body = post_review._CAPTURED[0]["payload"]["body"]
        signal = review_marker.detect_signal(body)
        self.assertIsNotNone(signal, f"no signal recovered from posted body: {body!r}")
        self.assertEqual(signal["sha"], sha)


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
        # Production shape since #127: (path, new_line) -> old_line, None for added
        # lines. The diagnostic still lists bare NEW-side numbers, which is what the
        # "Valid lines for this file: [...]" warning promises the reader.
        valid = {
            ("src/app.py", 10): None,
            ("src/app.py", 3): 3,
            ("src/app.py", 7): None,
            ("other.py", 1): 1,
        }
        result = valid_lines_for_file(valid, "src/app.py")
        self.assertEqual(result, [3, 7, 10])

    def test_returns_at_most_10(self):
        valid = {("f.py", i): i for i in range(1, 21)}
        result = valid_lines_for_file(valid, "f.py")
        self.assertEqual(len(result), 10)
        self.assertEqual(result, list(range(1, 11)))

    def test_strips_leading_ab_prefix(self):
        valid = {("src/app.py", 5): 2}
        result = valid_lines_for_file(valid, "a/src/app.py")
        self.assertEqual(result, [5])

    def test_empty_when_no_match(self):
        valid = {("other.py", 1): None}
        result = valid_lines_for_file(valid, "missing.py")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Diagnostic logging in skip warnings
# ---------------------------------------------------------------------------


class TestSkipWarningDiagnostics(unittest.TestCase):
    """Verify that skip warnings include valid-line diagnostics."""

    @patch(
        "scripts.post_review.get_head_sha",
        return_value="abc1234def5678abc1234def5678abc1234def56",
    )
    @patch("scripts.post_review.check_tool")
    @patch(
        "scripts.post_review.post_json", return_value={"html_url": "http://example.com"}
    )
    @patch("scripts.post_review.warn")
    def test_github_skip_includes_valid_lines(self, mock_warn, _post, _tool, _sha):
        from scripts.post_review import post_github

        valid_lines = {("src/app.py", 10): 10, ("src/app.py", 20): None}
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [{"file": "src/app.py", "line": 99, "title": "Bug"}],
        }
        post_github(data, valid_lines)
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn("Valid lines for this file:", msg)
        self.assertIn("10", msg)
        self.assertIn("20", msg)

    @patch(
        "scripts.post_review.get_head_sha",
        return_value="abc1234def5678abc1234def5678abc1234def56",
    )
    @patch("scripts.post_review.check_tool")
    @patch(
        "scripts.post_review.post_json", return_value={"html_url": "http://example.com"}
    )
    @patch("scripts.post_review.warn")
    def test_github_skip_no_diag_when_valid_lines_none(
        self, mock_warn, _post, _tool, _sha
    ):
        from scripts.post_review import post_github

        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [{"file": "src/app.py", "line": 99, "title": "Bug"}],
        }
        # valid_lines=None means validation was skipped, so is_line_valid returns True
        # and the skip branch is never entered. An EMPTY mapping is the shape that
        # reaches the skip branch: validation ran and found nothing for this line.
        post_github(data, {})
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn("line not found in diff.", msg)
        # With an empty mapping the valid-lines list is [] not None, so the diagnostic
        # is present but empty.
        self.assertIn("Valid lines for this file: []", msg)

    @patch(
        "scripts.post_review.get_head_sha",
        return_value="abc1234def5678abc1234def5678abc1234def56",
    )
    @patch("scripts.post_review.check_tool")
    @patch("scripts.post_review.post_json", return_value={})
    @patch("scripts.post_review.fetch_gitlab_shas", return_value=("b", "h", "s"))
    # The live path now asks detect_prior_review whether the summary note is already on
    # the MR; that read shells out to `glab`, so it is stubbed here rather than left to
    # reach a real forge from a unit test.
    @patch("scripts.post_review.gitlab_note_exists_for_sha", return_value=(False, None))
    @patch("scripts.post_review.warn")
    def test_gitlab_skip_includes_valid_lines(
        self, mock_warn, _exists, _shas, _post, _tool, _sha
    ):
        from scripts.post_review import post_gitlab

        valid_lines = {("src/app.py", 5): 5, ("src/app.py", 15): None}
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
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

    Unit level — ``valid_lines``/``new_files`` are injected. End-to-end detection from
    a real ``glab mr diff`` is pinned by ``TestGitlabPositionContract``; injecting
    ``new_files`` here is exactly why #127 D2 shipped, so this class must never be the
    only cover.
    """

    def _capture_position(self, data, valid_lines, new_files):
        """Run post_gitlab and return the position dict from the discussion call."""
        from scripts.post_review import post_gitlab

        captured = []

        # Patching the non-fatal core covers BOTH callers: the summary note still goes
        # through the real post_json wrapper, and the per-finding loop calls this
        # directly, so the capture order (summary, then discussions) is unchanged.
        def fake_try_post_json(cmd_prefix, payload):
            captured.append((cmd_prefix, payload))
            return {}, None

        # Faithful to real git: `git rev-parse HEAD` always yields either a
        # full 40-hex-char object id or the literal "unknown" (get_head_sha's
        # own fallback on failure) — never a 6-char string. "abc123" would be
        # the same class of fidelity bug flagged elsewhere in this change (a
        # get_head_sha mock only 6 hex chars long).
        with (
            patch("scripts.post_review.get_head_sha", return_value="deadbeef" * 5),
            patch("scripts.post_review.check_tool"),
            patch(
                "scripts.post_review.fetch_gitlab_shas",
                return_value=("base", "head", "start"),
            ),
            patch("scripts.post_review.try_post_json", side_effect=fake_try_post_json),
            # The live path asks detect_prior_review whether the summary note is
            # already on the MR; that read shells out to `glab`, so it is stubbed
            # rather than left to reach a real forge from a unit test.
            patch(
                "scripts.post_review.gitlab_note_exists_for_sha",
                return_value=(False, None),
            ),
        ):
            post_gitlab(data, valid_lines, new_files)

        # First post_json call is the summary note; second is the discussion.
        self.assertGreaterEqual(
            len(captured), 2, "expected summary + at least one discussion call"
        )
        return captured[1][1]["position"]

    def test_new_file_position_omits_old_path(self):
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [
                {"file": "src/added.py", "line": 5, "title": "Bug", "body": "x"}
            ],
        }
        valid_lines = {("src/added.py", 5): None}
        new_files = {"src/added.py"}
        position = self._capture_position(data, valid_lines, new_files)
        self.assertNotIn(
            "old_path", position, "old_path must be omitted for newly-added files"
        )
        self.assertNotIn("old_line", position, "an added file's lines have no old side")
        self.assertEqual(position["new_path"], "src/added.py")
        self.assertEqual(position["new_line"], 5)

    def test_modified_file_position_includes_old_path(self):
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [
                {"file": "src/edited.py", "line": 10, "title": "Bug", "body": "x"}
            ],
        }
        valid_lines = {("src/edited.py", 10): 7}
        new_files = set()
        position = self._capture_position(data, valid_lines, new_files)
        self.assertEqual(position["old_path"], "src/edited.py")
        self.assertEqual(position["new_path"], "src/edited.py")
        self.assertEqual(position["new_line"], 10)
        # Both sides, or GitLab answers 400 `line_code can't be blank` (#127 D1).
        self.assertEqual(position["old_line"], 7)

    def test_new_files_none_falls_back_to_modified_behavior(self):
        """If new_files is None (e.g., diff fetch failed), retain old_path.

        Better to risk a 500 on a new-file finding than to lose anchoring on
        modified-file findings — and the diff-fetch-failed path is rare.
        """
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [
                {"file": "src/edited.py", "line": 10, "title": "Bug", "body": "x"}
            ],
        }
        position = self._capture_position(data, valid_lines=None, new_files=None)
        self.assertEqual(position["old_path"], "src/edited.py")
        # A skipped validation has no old-side data to send, so the position degrades to
        # today's shape rather than crashing on a direct `valid_lines[...]` index.
        self.assertNotIn("old_line", position)


# ---------------------------------------------------------------------------
# --dry-run payload capture
# ---------------------------------------------------------------------------

# A GitHub diff (gh pr diff) that makes foo.py lines 1 (context) and 2 (added)
# valid for inline comments.
GH_DIFF = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,2 @@\n"
    " existing\n"
    "+added\n"
)

# A GitLab diff (glab mr diff) that makes bar.py lines 1 and 2 valid.
GL_DIFF = (
    "diff --git a/bar.py b/bar.py\n"
    "--- a/bar.py\n"
    "+++ b/bar.py\n"
    "@@ -1,1 +1,2 @@\n"
    " ctx\n"
    "+newline\n"
)


# Real `glab mr diff` shape, captured from the issue #127 report: NO a/ b/ prefixes, the
# SAME path on both sides for an added file, and `@@ -0,0` as the only added-file signal.
# src/edited.py: new 61 = old 50 (context), new 62 = added, new 63 = old 52 (context).
# src/added.py:  new 1, 2 — added file.
GL_DIFF_CONTRACT = (
    "diff --git a/src/edited.py b/src/edited.py\n"
    "--- src/edited.py\n"
    "+++ src/edited.py\n"
    "@@ -50,3 +61,3 @@\n"
    " unchanged_ctx\n"
    "-removed\n"
    "+added\n"
    " tail_ctx\n"
    "diff --git a/src/added.py b/src/added.py\n"
    "--- src/added.py\n"
    "+++ src/added.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+first\n"
    "+second\n"
)

# A RENAMED file, glab-flavoured (unprefixed headers): the `---` header names the
# PRE-rename path and the `+++` header the post-rename one. That old-side path is what
# GitLab needs in `position.old_path` (#130).
# new 3 = old 3 (context), new 4 = added, new 5 = old 5 (context).
GL_DIFF_RENAME = (
    "diff --git a/old_name.py b/new_name.py\n"
    "similarity index 87%\n"
    "rename from old_name.py\n"
    "rename to new_name.py\n"
    "--- old_name.py\n"
    "+++ new_name.py\n"
    "@@ -3,3 +3,3 @@\n"
    " ctx\n"
    "-x\n"
    "+y\n"
    " ctx2\n"
)

# One finding on each position kind GL_DIFF_RENAME produces: a context line and an
# added line, both inside the renamed file.
GL_RENAME_FINDINGS = [
    {
        "file": "new_name.py",
        "line": 3,
        "severity": "high",
        "title": "Context-line finding in a renamed file",
        "body": "Body one",
    },
    {
        "file": "new_name.py",
        "line": 4,
        "severity": "medium",
        "title": "Added-line finding in a renamed file",
        "body": "Body two",
    },
]

GL_CONTRACT_VERSIONS = [
    {
        "base_commit_sha": "base1",
        "head_commit_sha": "head1",
        "start_commit_sha": "start1",
    }
]

# One finding on each of the three position kinds GL_DIFF_CONTRACT produces: a context
# line, an added line in a modified file, and a line in an added file.
GL_CONTRACT_FINDINGS = [
    {
        "file": "src/edited.py",
        "line": 61,
        "severity": "high",
        "title": "Context-line finding",
        "body": "Body one",
    },
    {
        "file": "src/edited.py",
        "line": 62,
        "severity": "medium",
        "title": "Added-line finding",
        "body": "Body two",
    },
    {
        "file": "src/added.py",
        "line": 1,
        "severity": "low",
        "title": "New-file finding",
        "body": "Body three",
    },
]

# Verbatim from the issue #127 report — the warning-content test asserts against what an
# operator really sees, not a paraphrase.
GLAB_400_STDERR = (
    "glab: 400 Bad request - Note "
    '{:line_code=>["can\'t be blank", "must be a valid line code"]} (HTTP 400)'
)


def _fake_run(
    diff="",
    versions=None,
    remote="git@github.com:o/r.git\n",
    head_sha="deadbeefcafe\n",
    note_rc=0,
    discussion_rcs=None,
    calls=None,
):
    """Build a ``subprocess.run`` side_effect that mocks the read-only CLI calls.

    Handles ``which``, ``git remote get-url``, ``git rev-parse``, ``gh pr diff``,
    ``glab mr diff``, and the GitLab ``.../versions`` GET. Any other command
    (i.e. a POST) returns an empty JSON object — but in dry-run mode ``post_json``
    short-circuits before reaching ``subprocess.run`` for POSTs.

    The live GitLab POSTs are steerable so the fault-tolerance path is exercisable:
    *note_rc* is the summary note's exit code, and *discussion_rcs* is consumed one per
    inline-discussion POST (default 0 once exhausted). A non-zero discussion rc comes
    back with the verbatim glab 400. *calls* collects every argv when given.
    """
    rcs = iter(discussion_rcs or [])

    def _run(cmd, *a, **k):
        def res(out="", err="", rc=0):
            return SimpleNamespace(stdout=out, stderr=err, returncode=rc)

        if calls is not None:
            calls.append(cmd)
        if cmd[0] == "which":
            return res(out="/usr/bin/" + cmd[1])
        if cmd[:3] == ["git", "remote", "get-url"]:
            return res(out=remote)
        if cmd[:2] == ["git", "rev-parse"]:
            return res(out=head_sha)
        if cmd[:3] == ["gh", "pr", "diff"]:
            return res(out=diff)
        if cmd[:3] == ["glab", "mr", "diff"]:
            return res(out=diff)
        if cmd[:2] == ["glab", "api"] and "--method" in cmd:
            if any(tok.endswith("/discussions") for tok in cmd):
                rc = next(rcs, 0)
                return res(out="{}") if rc == 0 else res(err=GLAB_400_STDERR, rc=rc)
            if any(tok.endswith("/notes") for tok in cmd):
                if note_rc:
                    return res(err="glab: 401 Unauthorized (HTTP 401)", rc=note_rc)
                return res(out="{}")
        if cmd[:2] == ["glab", "api"] and cmd[-1].endswith("/versions"):
            return res(out=json.dumps(versions if versions is not None else []))
        return res(out="{}", rc=0)

    return _run


def _gitlab_posts(mock_run, suffix):
    """Calls on *mock_run* that POST to a GitLab endpoint whose path ends in *suffix*."""
    return [
        c
        for c in mock_run.call_args_list
        if "--method" in c.args[0] and any(t.endswith(suffix) for t in c.args[0])
    ]


def _discussion_posts(mock_run):
    return _gitlab_posts(mock_run, "/discussions")


def _note_posts(mock_run):
    return _gitlab_posts(mock_run, "/notes")


class _DryRunTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.findings_path = os.path.join(self.tmp, "findings.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        post_review.DRY_RUN = False
        post_review._CAPTURED.clear()
        post_review._SKIP_WARNINGS.clear()

    def _write(self, data):
        with open(self.findings_path, "w") as f:
            json.dump(data, f)

    def _payload(self):
        with open(os.path.join(self.tmp, "post-review-payload.json")) as f:
            return json.load(f)


class TestDryRunGitHub(_DryRunTestBase):
    def test_dry_run_captures_payload_and_makes_no_post(self):
        finding_a = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "Bug A",
            "body": "Body A",
        }
        finding_b = {
            "file": "foo.py",
            "line": 99,
            "severity": "low",
            "title": "Bug B",
            "body": "Body B",
        }
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [finding_a, finding_b],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF),
            ) as mock_run,
        ):
            post_review.main()

        post_calls = [
            c
            for c in mock_run.call_args_list
            if "--method" in c.args[0] and "POST" in c.args[0]
        ]
        self.assertEqual(post_calls, [], "no POST subprocess call in dry-run")

        payload_path = os.path.join(self.tmp, "post-review-payload.json")
        self.assertTrue(os.path.exists(payload_path))
        cap = self._payload()

        self.assertEqual(cap["platform"], "github")
        self.assertEqual(cap["endpoint"], "repos/o/r/pulls/5/reviews")
        self.assertEqual(cap["method"], "POST")
        self.assertEqual(cap["payload"]["event"], "COMMENT")
        # Comments must match the live-path rendering byte-for-byte.
        self.assertEqual(len(cap["payload"]["comments"]), 1)
        comment = cap["payload"]["comments"][0]
        self.assertEqual(comment["body"], render_comment_body(finding_a))
        self.assertEqual(comment["path"], "foo.py")
        self.assertEqual(comment["line"], 2)
        self.assertEqual(comment["side"], "RIGHT")

    def test_invalid_line_lands_in_skipped_not_comments(self):
        finding_a = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "Bug A",
            "body": "Body A",
        }
        finding_b = {
            "file": "foo.py",
            "line": 99,
            "severity": "low",
            "title": "Bug B",
            "body": "Body B",
        }
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [finding_a, finding_b],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF),
            ),
        ):
            post_review.main()

        cap = self._payload()
        expected = (
            "Skipping finding 'Bug B' at foo.py:99 "
            "— line not found in diff. Valid lines for this file: [1, 2]"
        )
        self.assertIn(expected, cap["skipped"])
        bodies = [c["body"] for c in cap["payload"]["comments"]]
        self.assertNotIn(render_comment_body(finding_b), bodies)


class TestDryRunGitLab(_DryRunTestBase):
    def test_dry_run_captures_summary_and_discussions(self):
        finding_x = {
            "file": "bar.py",
            "line": 2,
            "severity": "medium",
            "title": "Issue X",
            "body": "Desc X",
        }
        self._write(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": [finding_x],
            }
        )
        versions = [
            {
                "base_commit_sha": "base1",
                "head_commit_sha": "head1",
                "start_commit_sha": "start1",
            }
        ]
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GL_DIFF, versions=versions),
            ) as mock_run,
        ):
            post_review.main()

        post_calls = [
            c
            for c in mock_run.call_args_list
            if "--method" in c.args[0] and "POST" in c.args[0]
        ]
        self.assertEqual(post_calls, [], "no POST subprocess call in dry-run")

        versions_calls = [
            c
            for c in mock_run.call_args_list
            if c.args[0][:2] == ["glab", "api"] and c.args[0][-1].endswith("/versions")
        ]
        self.assertTrue(
            versions_calls, "fetch_gitlab_shas versions GET must still run in dry-run"
        )

        cap = self._payload()
        self.assertEqual(cap["platform"], "gitlab")
        self.assertIn("MR review", cap["summary"]["body"])
        self.assertEqual(len(cap["discussions"]), 1)
        disc = cap["discussions"][0]
        self.assertEqual(disc["body"], render_comment_body(finding_x))
        self.assertEqual(disc["position"]["new_path"], "bar.py")
        self.assertEqual(disc["position"]["new_line"], 2)


class TestLivePathUnchanged(_DryRunTestBase):
    def test_without_flag_posts_and_writes_no_payload_file(self):
        finding_a = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "Bug A",
            "body": "Body A",
        }
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [finding_a],
            }
        )
        # Pin CODE_GAUNTLET_POST_MODE off so an ambient bench value (the harness pins it
        # to dry-run) cannot flip this live-path assertion.
        with (
            patch.object(sys, "argv", ["post_review.py", self.findings_path]),
            patch.dict(os.environ, {}, clear=False),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF),
            ) as mock_run,
        ):
            os.environ.pop("CODE_GAUNTLET_POST_MODE", None)
            post_review.main()

        post_calls = [
            c
            for c in mock_run.call_args_list
            if "--method" in c.args[0] and "POST" in c.args[0]
        ]
        self.assertTrue(post_calls, "live path must issue the reviews POST")
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp, "post-review-payload.json"))
        )


class TestDryRunStdout(_DryRunTestBase):
    """In dry-run, the post paths must not claim anything was posted."""

    def _run_main_capturing_stdout(self, data, diff, versions=None):
        self._write(data)
        stdout = io.StringIO()
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=diff, versions=versions),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            post_review.main()
        return stdout.getvalue()

    def test_github_dry_run_stdout_has_no_posted_claim(self):
        out = self._run_main_capturing_stdout(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [
                    {
                        "file": "foo.py",
                        "line": 2,
                        "severity": "high",
                        "title": "Bug A",
                        "body": "Body A",
                    }
                ],
            },
            diff=GH_DIFF,
        )
        self.assertNotIn("Review posted:", out)
        self.assertNotIn("comment(s) posted.", out)
        self.assertIn("Review captured (dry-run).", out)
        self.assertIn("inline comment(s) captured.", out)

    def test_gitlab_dry_run_stdout_has_no_posted_claim(self):
        versions = [
            {"base_commit_sha": "b", "head_commit_sha": "h", "start_commit_sha": "s"}
        ]
        out = self._run_main_capturing_stdout(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": [
                    {
                        "file": "bar.py",
                        "line": 2,
                        "severity": "medium",
                        "title": "Issue X",
                        "body": "Desc X",
                    }
                ],
            },
            diff=GL_DIFF,
            versions=versions,
        )
        self.assertNotIn("note posted.", out)
        self.assertNotIn("discussion(s) posted.", out)
        self.assertIn("MR summary note captured (dry-run).", out)
        self.assertIn("inline discussion(s) captured.", out)


class TestLivePathStdout(_DryRunTestBase):
    """The live path's stdout is unchanged: it still claims posts."""

    def test_github_live_path_prints_posted(self):
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [
                    {
                        "file": "foo.py",
                        "line": 2,
                        "severity": "high",
                        "title": "Bug A",
                        "body": "Body A",
                    }
                ],
            }
        )
        stdout = io.StringIO()
        with (
            patch.object(sys, "argv", ["post_review.py", self.findings_path]),
            patch.dict(os.environ, {}, clear=False),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            os.environ.pop("CODE_GAUNTLET_POST_MODE", None)
            post_review.main()
        out = stdout.getvalue()
        self.assertIn("Review posted:", out)
        self.assertIn("inline comment(s) posted.", out)
        self.assertNotIn("captured", out)


# ---------------------------------------------------------------------------
# CODE_GAUNTLET_POST_MODE env-enforced dry-run
# ---------------------------------------------------------------------------


class TestPostModeEnv(_DryRunTestBase):
    """CODE_GAUNTLET_POST_MODE=dry-run self-enforces dry-run even without --dry-run.

    The env var is part of the headless contract and the bench harness pins it to
    dry-run; a headless Phase 8 invocation that omits the flag must still capture the
    payload and post nothing. The flag wins when present; env "live" or unset changes
    nothing on its own.
    """

    def _write_gh(self):
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [
                    {
                        "file": "foo.py",
                        "line": 2,
                        "severity": "high",
                        "title": "Bug A",
                        "body": "Body A",
                    }
                ],
            }
        )

    def _post_calls(self, mock_run):
        return [
            c
            for c in mock_run.call_args_list
            if "--method" in c.args[0] and "POST" in c.args[0]
        ]

    def _payload_exists(self):
        return os.path.exists(os.path.join(self.tmp, "post-review-payload.json"))

    def test_env_dry_run_alone_captures_payload_no_posts(self):
        self._write_gh()
        with (
            patch.object(sys, "argv", ["post_review.py", self.findings_path]),
            patch.dict(os.environ, {"CODE_GAUNTLET_POST_MODE": "dry-run"}),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF),
            ) as mock_run,
        ):
            post_review.main()
        self.assertEqual(
            self._post_calls(mock_run), [], "env dry-run must issue no POST"
        )
        self.assertTrue(self._payload_exists())
        self.assertEqual(self._payload()["platform"], "github")

    def test_flag_alone_dry_run_when_env_unset(self):
        self._write_gh()
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch.dict(os.environ, {}, clear=False),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF),
            ) as mock_run,
        ):
            os.environ.pop("CODE_GAUNTLET_POST_MODE", None)
            post_review.main()
        self.assertEqual(self._post_calls(mock_run), [])
        self.assertTrue(self._payload_exists())

    def test_neither_flag_nor_env_posts_live(self):
        self._write_gh()
        with (
            patch.object(sys, "argv", ["post_review.py", self.findings_path]),
            patch.dict(os.environ, {}, clear=False),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF),
            ) as mock_run,
        ):
            os.environ.pop("CODE_GAUNTLET_POST_MODE", None)
            post_review.main()
        self.assertTrue(
            self._post_calls(mock_run), "live path must issue the reviews POST"
        )
        self.assertFalse(self._payload_exists())

    def test_env_live_without_flag_posts_live(self):
        self._write_gh()
        with (
            patch.object(sys, "argv", ["post_review.py", self.findings_path]),
            patch.dict(os.environ, {"CODE_GAUNTLET_POST_MODE": "live"}),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF),
            ) as mock_run,
        ):
            post_review.main()
        self.assertTrue(
            self._post_calls(mock_run), "env=live with no flag must post live"
        )
        self.assertFalse(self._payload_exists())


class TestWriterWrapperByteParity(_DryRunTestBase):
    """V3.1 L3/D16 acceptance: the writer-persisted post_review wrapper drives
    post_review.py to a byte-identical --dry-run payload vs the manually-assembled
    Phase-8 wrap, for identical findings and identity.

    The writer's wrapper is { owner, repo, pr_number, sha, review_body, findings }
    (see writerPayload in workflows/src/stages.js): `sha` is the marker sha the
    script prefers when it's SHA-shaped (falling back to its own HEAD otherwise —
    see resolve_marker_sha), `platform` is absent (auto-detected from the git
    remote — mocked github here), review_body matches what Phase 8 would set.
    The fixture's sha deliberately matches the mocked `git rev-parse` output, so
    the manual and wrapper payloads stay byte-identical either way.
    """

    FINDINGS: ClassVar[list[dict]] = [
        {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "Bug A",
            "body": "Body A",
        },
        {
            "file": "foo.py",
            "line": 1,
            "end_line": 2,
            "severity": "low",
            "title": "Bug B",
            "body": "Body B",
        },
    ]

    def _dry_run_payload_bytes(self, data):
        self._write(data)
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF),
            ),
        ):
            post_review.main()
        payload_path = os.path.join(self.tmp, "post-review-payload.json")
        with open(payload_path, "rb") as f:
            raw = f.read()
        os.unlink(payload_path)
        post_review._CAPTURED.clear()
        post_review._SKIP_WARNINGS.clear()
        post_review.DRY_RUN = False
        return raw

    def test_wrapper_and_manual_wrap_produce_byte_identical_payloads(self):
        manual = {
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "review_body": "Summary",
            "findings": self.FINDINGS,
        }
        # The writer-emitted wrapper: same fields plus the marker sha the script
        # prefers (see resolve_marker_sha). Key order intentionally matches
        # writerPayload's emission; the fixture's sha matches the mocked HEAD so
        # both payloads stay byte-identical either way.
        wrapper = {
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "sha": "deadbeefcafe",
            "review_body": "Summary",
            "findings": self.FINDINGS,
        }
        self.assertEqual(
            self._dry_run_payload_bytes(manual),
            self._dry_run_payload_bytes(wrapper),
            "wrapper form must drive a byte-identical dry-run payload",
        )


# ---------------------------------------------------------------------------
# Both footer halves posted (Issue #39 D3) — GitHub review body and GitLab
# summary note both get the prose footer AND the machine marker, and an
# already-prose'd review_body does not get a duplicate.
# ---------------------------------------------------------------------------


class TestBothFooterHalvesPosted(_DryRunTestBase):
    def test_github_review_body_contains_both_prose_and_marker(self):
        finding_a = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "Bug A",
            "body": "Body A",
        }
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [finding_a],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF),
            ),
        ):
            post_review.main()

        body = self._payload()["payload"]["body"]
        self.assertIn("Generated by code-gauntlet", body)
        self.assertIn("Reviewed up to:", body)
        self.assertIn("code-gauntlet-findings:", body)
        self.assertIn("Summary", body, "the original review_body must still be present")

    def test_gitlab_summary_note_contains_both_prose_and_marker(self):
        finding_x = {
            "file": "bar.py",
            "line": 2,
            "severity": "medium",
            "title": "Issue X",
            "body": "Desc X",
        }
        self._write(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": [finding_x],
            }
        )
        versions = [
            {
                "base_commit_sha": "base1",
                "head_commit_sha": "head1",
                "start_commit_sha": "start1",
            }
        ]
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GL_DIFF, versions=versions),
            ),
        ):
            post_review.main()

        body = self._payload()["summary"]["body"]
        self.assertIn("Generated by code-gauntlet", body)
        self.assertIn("Reviewed up to:", body)
        self.assertIn("code-gauntlet-findings:", body)
        self.assertIn("MR review", body)

    def test_review_body_with_the_same_prose_sha_gets_no_second_copy(self):
        """The idempotence guard (Issue #39 D3): a footer already naming THIS
        commit is left alone rather than duplicated."""
        pre_existing = (
            "Some notes.\n\n---\n"
            "Generated by code-gauntlet | Reviewed up to: deadbeefcafe\n"
        )
        finding_a = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "Bug A",
            "body": "Body A",
        }
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": pre_existing,
                "findings": [finding_a],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF),
            ),
        ):
            post_review.main()
        body = self._payload()["payload"]["body"]
        self.assertEqual(
            body.count("Generated by code-gauntlet"),
            1,
            f"an exact-sha match must not duplicate: {body!r}",
        )

    def test_review_body_with_a_stale_prose_sha_still_gets_the_real_one(self):
        """A hand-composed footer naming a DIFFERENT commit must not suppress the
        real one: suppressing it would leave the posted review advertising a
        commit the review never examined. Two lines is the honest outcome — the
        reader takes the last — and only an exact-sha match is deduplicated
        (see test_review_body_with_the_same_prose_sha_gets_no_second_copy)."""
        pre_existing = (
            "Some notes.\n\nGenerated by code-gauntlet | Reviewed up to: abc1234\n"
        )
        finding_a = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "Bug A",
            "body": "Body A",
        }
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": pre_existing,
                "findings": [finding_a],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF),
            ),
        ):
            post_review.main()

        body = self._payload()["payload"]["body"]
        # The real head sha must be present and last, so parse_prose_footer (and
        # any human) reads the commit actually reviewed, not the stale one.
        self.assertIn("Reviewed up to: deadbeefcafe", body)
        self.assertGreater(body.rindex("deadbeefcafe"), body.rindex("abc1234"))
        signal = review_marker.detect_signal(body)
        self.assertEqual(signal["sha"], "deadbeefcafe")
        # The mechanical marker is still appended (the guards are independent).
        self.assertIn("code-gauntlet-findings:", body)


# ---------------------------------------------------------------------------
# GitLab position contract, end-to-end from a real `glab mr diff` (issue #127)
# ---------------------------------------------------------------------------


class TestGitlabPositionContract(_DryRunTestBase):
    """The position payload as the REAL parser produces it.

    ``TestGitlabPositionPayload`` injects ``valid_lines``/``new_files`` and so could
    never have caught #127 D2 (glab-style added-file detection) — the fixture supplied
    the answer the parser was failing to compute. This class drives ``main()`` in
    dry-run against ``GL_DIFF_CONTRACT``, so ``parse_diff_lines`` feeds ``post_gitlab``
    and every asserted key is one the production chain actually emitted.
    """

    def setUp(self):
        super().setUp()
        self._write(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": GL_CONTRACT_FINDINGS,
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(
                    diff=GL_DIFF_CONTRACT, versions=GL_CONTRACT_VERSIONS
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            post_review.main()

    def _positions(self):
        return [d["position"] for d in self._payload()["discussions"]]

    def test_context_line_position_carries_the_correct_old_line(self):
        position = self._positions()[0]
        self.assertEqual(position["new_line"], 61)
        self.assertEqual(position["old_line"], 50)
        self.assertEqual(position["old_path"], "src/edited.py")

    def test_added_line_position_omits_old_line(self):
        position = self._positions()[1]
        self.assertEqual(position["new_line"], 62)
        self.assertNotIn("old_line", position)
        # The FILE is still modified, so old_path stays.
        self.assertEqual(position["old_path"], "src/edited.py")

    def test_added_file_position_omits_old_path_and_old_line(self):
        """The test the old suite could not provide: ``new_files`` comes from the
        parser here, not from a fixture that asserted the conclusion."""
        position = self._positions()[2]
        self.assertEqual(position["new_path"], "src/added.py")
        self.assertNotIn("old_path", position)
        self.assertNotIn("old_line", position)

    def test_line_code_never_appears_anywhere_in_the_gitlab_payload(self):
        """``line_code`` is derived server-side. Both documented attempts to compute it
        client-side (a position sibling, and inside ``line_range``) reproduced the
        identical 400 — one scan covers positions, bodies and the summary note."""
        self.assertNotIn("line_code", json.dumps(self._payload()))

    def test_new_line_is_always_sent(self):
        for position in self._positions():
            self.assertIsInstance(position["new_line"], int)


class TestGitlabRenamedFilePositionContract(_DryRunTestBase):
    """A renamed file's position, end-to-end through the real parser (issue #130).

    ``old_path`` was hard-wired to the finding's (post-rename) path, which the old side
    of the diff does not contain. Driving ``main()`` in dry-run against
    ``GL_DIFF_RENAME`` proves the pre-rename path travels parser -> poster -> payload.
    """

    def setUp(self):
        super().setUp()
        self._write(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": GL_RENAME_FINDINGS,
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(
                    diff=GL_DIFF_RENAME, versions=GL_CONTRACT_VERSIONS
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            post_review.main()

    def _positions(self):
        return [d["position"] for d in self._payload()["discussions"]]

    def test_renamed_file_position_anchors_old_path_to_the_pre_rename_path(self):
        position = self._positions()[0]
        self.assertEqual(position["old_path"], "old_name.py")
        self.assertEqual(position["new_path"], "new_name.py")
        self.assertEqual(position["old_line"], 3)
        self.assertEqual(position["new_line"], 3)

    def test_added_line_in_a_renamed_file_keeps_the_pre_rename_old_path(self):
        position = self._positions()[1]
        self.assertEqual(position["old_path"], "old_name.py")
        self.assertNotIn("old_line", position)
        self.assertEqual(position["new_line"], 4)


class TestGitlabFindingPathNormalization(_DryRunTestBase):
    """A `b/`-prefixed finding path must ship UNPREFIXED in the position.

    `is_line_valid`/`old_line_for` strip the prefix for their lookups, so such a finding
    passed validation and got a correct `old_line` — but the position then shipped the
    raw `b/src/edited.py`, a path GitLab does not know. Normalize once, at the top of the
    loop, and use it for the lookup, the position and the warnings alike.
    """

    def test_prefixed_finding_path_ships_normalized_position(self):
        self._write(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": [
                    {
                        "file": "b/src/edited.py",
                        "line": 61,
                        "severity": "high",
                        "title": "Prefixed-path finding",
                        "body": "Body",
                    }
                ],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(
                    diff=GL_DIFF_CONTRACT, versions=GL_CONTRACT_VERSIONS
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            post_review.main()
        position = self._payload()["discussions"][0]["position"]
        self.assertEqual(position["new_path"], "src/edited.py")
        self.assertEqual(position["old_path"], "src/edited.py")
        self.assertEqual(position["old_line"], 50)


# ---------------------------------------------------------------------------
# GitLab per-finding fault tolerance (issue #127 D3)
# ---------------------------------------------------------------------------


class TestGitlabFaultTolerance(_DryRunTestBase):
    """A single rejected position must not strand the findings behind it.

    The summary note is posted FIRST, so aborting mid-loop left partial,
    non-retryable state on the MR. The loop now warns, counts and continues; the run
    only exits non-zero when EVERY attempted post was rejected.
    """

    def _run_main(self, dry_run=False, findings=None, **fake_run_kwargs):
        self._write(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "sha": "a" * 40,
                "review_body": "MR review",
                "findings": GL_CONTRACT_FINDINGS if findings is None else findings,
            }
        )
        argv = ["post_review.py", self.findings_path]
        if dry_run:
            argv.append("--dry-run")
        stdout, stderr = io.StringIO(), io.StringIO()
        exit_code = None
        with (
            patch.object(sys, "argv", argv),
            patch.dict(os.environ, {}, clear=False),
            # The idempotency fetch is not the subject here; pin it to "not posted yet"
            # so every run reaches the per-finding loop.
            patch(
                "scripts.post_review.gitlab_note_exists_for_sha",
                return_value=(False, None),
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(
                    diff=GL_DIFF_CONTRACT,
                    versions=GL_CONTRACT_VERSIONS,
                    **fake_run_kwargs,
                ),
            ) as mock_run,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            os.environ.pop("CODE_GAUNTLET_POST_MODE", None)
            try:
                post_review.main()
            except SystemExit as exc:
                exit_code = exc.code
        return SimpleNamespace(
            mock_run=mock_run,
            out=stdout.getvalue(),
            err=stderr.getvalue(),
            exit_code=exit_code,
        )

    def test_one_rejection_does_not_abort_the_remaining_findings(self):
        run = self._run_main(discussion_rcs=[1, 0, 0])
        self.assertIsNone(
            run.exit_code, "a partial delivery is a success with warnings"
        )
        self.assertEqual(len(_discussion_posts(run.mock_run)), 3)
        self.assertIn("  2 inline discussion(s) posted.", run.out)
        self.assertIn("  1 inline discussion(s) rejected by GitLab", run.out)

    def test_rejection_warning_names_the_finding_and_the_api_error(self):
        run = self._run_main(discussion_rcs=[1, 0, 0])
        self.assertIn("Context-line finding", run.err)
        self.assertIn("src/edited.py:61", run.err)
        self.assertIn("line_code", run.err)

    def test_all_rejected_exits_non_zero_after_attempting_every_finding(self):
        run = self._run_main(discussion_rcs=[1, 1, 1])
        self.assertEqual(run.exit_code, 1)
        # The exit is a REPORT, not an abort: every finding was attempted first.
        self.assertEqual(len(_discussion_posts(run.mock_run)), 3)
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("  3 inline discussion(s) rejected", run.out)

    def test_zero_posted_with_no_rejections_exits_zero(self):
        off_diff = [dict(f, line=999) for f in GL_CONTRACT_FINDINGS]
        run = self._run_main(findings=off_diff)
        self.assertIsNone(run.exit_code)
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("  3 finding(s) skipped.", run.out)
        self.assertNotIn("rejected", run.out)

    def test_summary_note_failure_is_still_fatal(self):
        run = self._run_main(note_rc=1)
        self.assertEqual(run.exit_code, 1)
        self.assertEqual(
            _discussion_posts(run.mock_run),
            [],
            "auth/MR failure dooms every inline post behind it — do not attempt them",
        )

    def test_dry_run_never_reports_rejections(self):
        run = self._run_main(dry_run=True, discussion_rcs=[1, 1, 1])
        self.assertIsNone(run.exit_code)
        self.assertIn("  3 inline discussion(s) captured.", run.out)
        self.assertNotIn("rejected", run.out)
        self.assertEqual(len(self._payload()["discussions"]), 3)


# ---------------------------------------------------------------------------
# GitLab summary-note idempotency (issue #127 D4)
# ---------------------------------------------------------------------------


class TestGitlabSummaryIdempotency(_DryRunTestBase):
    """A rerun after a partial delivery must not stack a second summary note.

    ``scripts.post_review.gitlab_note_exists_for_sha`` — the name bound INTO this
    module — is what these tests patch. ``post_review`` imports the bare
    ``detect_prior_review`` while ``tests/test_detect_prior_review.py`` imports
    ``scripts.detect_prior_review``: two distinct module objects in one pytest process,
    so patching the other one would not be seen here.
    """

    def _run_main(self, exists, data=None, dry_run=False, head_sha="deadbeefcafe\n"):
        self._write(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "sha": "a" * 40,
                "review_body": "MR review",
                "findings": GL_CONTRACT_FINDINGS,
            }
            if data is None
            else data
        )
        argv = ["post_review.py", self.findings_path]
        if dry_run:
            argv.append("--dry-run")
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch.dict(os.environ, {}, clear=False),
            patch(
                "scripts.post_review.gitlab_note_exists_for_sha", return_value=exists
            ) as mock_exists,
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(
                    diff=GL_DIFF_CONTRACT,
                    versions=GL_CONTRACT_VERSIONS,
                    head_sha=head_sha,
                ),
            ) as mock_run,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            os.environ.pop("CODE_GAUNTLET_POST_MODE", None)
            post_review.main()
        return SimpleNamespace(
            mock_exists=mock_exists,
            mock_run=mock_run,
            out=stdout.getvalue(),
            err=stderr.getvalue(),
        )

    def test_summary_skipped_when_this_shas_marker_is_already_on_the_mr(self):
        run = self._run_main(exists=(True, None))
        self.assertEqual(_note_posts(run.mock_run), [])
        # The retry still delivers the inline comments — that is the whole point.
        self.assertEqual(len(_discussion_posts(run.mock_run)), 3)
        self.assertIn("already on the MR", run.out)
        self.assertNotIn("MR summary note posted.", run.out)
        run.mock_exists.assert_called_once_with("o", "r", 5, "a" * 40)

    def test_summary_posted_when_the_marker_records_a_different_sha(self):
        run = self._run_main(exists=(False, None))
        self.assertEqual(len(_note_posts(run.mock_run)), 1)
        self.assertIn("MR summary note posted.", run.out)

    def test_notes_fetch_failure_degrades_to_posting(self):
        run = self._run_main(
            exists=(False, "gitlab notes: fetch failed (exit 1): boom")
        )
        self.assertEqual(len(_note_posts(run.mock_run)), 1)
        self.assertIn("could not check for an existing summary note", run.err)
        self.assertIn("boom", run.err)

    def test_dry_run_makes_no_idempotency_call_and_always_captures_the_summary(self):
        """The hard "no network in dry-run" pin: the check would say "skip" if it were
        consulted, and build_dry_run_payload's "first capture is the summary" shape
        depends on the note being captured regardless."""
        run = self._run_main(exists=(True, None), dry_run=True)
        run.mock_exists.assert_not_called()
        self.assertIn("code-gauntlet-findings:", self._payload()["summary"]["body"])
        self.assertEqual(
            post_review._CAPTURED[0]["payload"]["body"],
            self._payload()["summary"]["body"],
        )

    def test_unresolvable_sha_skips_the_check_and_posts(self):
        """get_head_sha's "unknown" fallback is not a usable dedup key."""
        run = self._run_main(
            exists=(True, None),
            data={
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": GL_CONTRACT_FINDINGS,
            },
            head_sha="unknown\n",
        )
        run.mock_exists.assert_not_called()
        self.assertEqual(len(_note_posts(run.mock_run)), 1)

    def test_summary_check_delegates_to_the_reader_module(self):
        """post_review must not grow its own parse of the signal it writes."""
        self.assertEqual(
            post_review.gitlab_note_exists_for_sha.__module__, "detect_prior_review"
        )


if __name__ == "__main__":
    unittest.main()
