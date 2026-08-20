"""
Tests for scripts/post_review.py

Covers:
  - detect_platform: GitHub SSH, GitHub HTTPS, GitLab SSH, GitLab HTTPS,
    unknown host, malformed URL
  - parse_diff_lines: (post_review version) platform header semantics over the shared
    diff_lines.walk_diff
  - is_line_valid: exact match, stripped path, None valid_lines
  - render_comment_body: all severity emojis, with/without suggestion block
  - build_footer: metadata JSON in HTML comment
  - gitlab_project_id: URL encoding of owner/repo
  - TestReviewMarkerRoundTripThroughRealPoster — issue #39 requirement 6's
    headline "write signal == read signal" guarantee, proven against the REAL
    post_github/post_gitlab DRY_RUN capture path rather than a
    re-implementation of their footer composition. See the class docstring.
  - TestGitlabInlineDiscussionIdempotency — issue #132: a rerun after a partial
    GitLab delivery must not duplicate the inline discussions that landed, and
    the outcome it reports must stay honest about what THIS run attempted.
"""

import contextlib
import inspect
import io
import json
import os
import re
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
    _blockquote,
    _cap_rule_text,
    _redact_secrets,
    _sanitize_outbound_prose,
    _suggestion_fence,
    build_footer,
    build_skipped_section,
    consolidate_delivery,
    detect_platform,
    gitlab_project_id,
    is_line_valid,
    old_line_for,
    parse_diff_lines,
    parse_diff_text,
    render_comment_body,
    render_group_body,
    resolve_marker_sha,
    valid_lines_for_file,
    validate_position,
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
        valid_lines, new_files, _, _ = parse_diff_lines("github", "myorg", "myrepo", 42)
        self.assertIsNotNone(valid_lines)
        self.assertEqual(new_files, set())
        call_args = mock_run.call_args[0][0]
        self.assertEqual(call_args[0], "gh")
        self.assertEqual(call_args[1], "pr")
        self.assertEqual(call_args[2], "diff")

    @patch("scripts.post_review.run_api")
    def test_gitlab_dispatches_to_glab_mr_diff(self, mock_run):
        """platform='gitlab' must call glab mr diff."""
        # glab-faithful: an unconditional `---`/`+++` pair, paths verbatim, no `a/`/`b/`.
        diff = "--- bar.py\n+++ bar.py\n@@ -5,1 +5,2 @@\n ctx\n+new_line\n"
        mock_run.return_value = (diff, "", 0)
        valid_lines, _, _, _ = parse_diff_lines("gitlab", "myorg", "myrepo", 7)
        self.assertIsNotNone(valid_lines)
        call_args = mock_run.call_args[0][0]
        self.assertEqual(call_args[0], "glab")
        self.assertEqual(call_args[1], "mr")
        self.assertEqual(call_args[2], "diff")
        self.assertNotIn(
            "--raw",
            call_args,
            "the parser reads glab's own reconstruction of the MR versions API; --raw "
            "streams git's diff instead, reintroducing a/ b/ prefixes and /dev/null",
        )

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
        valid_lines, new_files, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertIn(("src/app.py", 1), valid_lines)
        self.assertIn(("src/app.py", 2), valid_lines)
        self.assertEqual(new_files, set())

    @patch("scripts.post_review.run_api")
    def test_github_diff_prefixes_are_still_stripped(self, mock_run):
        """`gh pr diff` writes git's synthetic `a/` / `b/`: those ARE diff syntax.

        The platform split that stopped stripping them on GitLab must not stop
        stripping them here — the keys GitHub findings are matched against, and the
        `path` shipped to its API, are prefix-free.
        """
        diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " ctx\n"
            "-x\n"
            "+y\n"
        )
        mock_run.return_value = (diff, "", 0)
        valid_lines, _, old_paths, _ = parse_diff_lines("github", "o", "r", 1)
        self.assertIn(("src/app.py", 1), valid_lines)
        self.assertEqual(old_paths, {"src/app.py": "src/app.py"})
        self.assertEqual({fp for fp, _ in valid_lines}, {"src/app.py"})

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
        _, new_files, _, _ = parse_diff_lines("github", "o", "r", 1)
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
        _, new_files, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(new_files, {"src/added.py"})

    @patch("scripts.post_review.run_api")
    def test_added_file_detected_end_of_multi_file_glab_diff(self, mock_run):
        """The added file is the SECOND file in the diff, and the modified one that
        precedes it must not be swept into new_files with it."""
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        _, new_files, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(new_files, {"src/app/clients/api/__init__.py"})

    @patch("scripts.post_review.run_api")
    def test_deleted_file_does_not_add_dev_null_to_valid_lines(self, mock_run):
        """``+++ /dev/null`` (deleted file) must not produce phantom entries.

        The ``@@ -1,2 +0,0 @@`` header must not read as an added file either: it is the
        NEW side that is 0 here, and only an old-side start of 0 means "added".
        """
        diff = "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-line1\n-line2\n"
        mock_run.return_value = (diff, "", 0)
        valid_lines, new_files, _, _ = parse_diff_lines("github", "o", "r", 1)
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
        _, new_files, _, _ = parse_diff_lines("github", "o", "r", 1)
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
        valid_lines, new_files, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
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
        valid_lines, _, _, _ = parse_diff_lines("github", "o", "r", 1)
        self.assertEqual(valid_lines[("next.py", 5)], 5)
        self.assertIsNone(valid_lines[("next.py", 6)])
        self.assertEqual([k for k in valid_lines if k[0] == "gone.py"], [])

    @patch("scripts.post_review.run_api")
    def test_gitlab_deleted_file_records_no_targets_of_its_own(self, mock_run):
        """`glab mr diff` has no `+++ /dev/null`: a deletion repeats the path on BOTH
        headers, so ``current_file`` stays LIVE through the deleted file's body.

        On GitHub the new-side header blanks ``current_file``, and a mis-drained body can
        only lose the NEXT file's lines. Here nothing blanks it, so the same fault also
        writes keys onto a file that no longer exists — inline comments aimed at a
        deleted path. Draining the old-side budget is the only thing that ends the body.
        """
        mock_run.return_value = (GL_DIFF_DELETED_THEN_MODIFIED, "", 0)
        valid_lines, new_files, old_paths, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual([k for k in valid_lines if k[0] == "src/removed.py"], [])
        self.assertEqual(valid_lines[("src/edited.py", 61)], 50)
        self.assertIsNone(valid_lines[("src/edited.py", 62)])
        self.assertEqual(valid_lines[("src/edited.py", 63)], 52)
        # `@@ -1,3 +0,0 @@` is a deletion: only an OLD-side start of 0 means added. The
        # repeated path still yields an old_paths entry, harmlessly mapping to itself.
        self.assertEqual(new_files, set())
        self.assertEqual(
            old_paths,
            {"src/removed.py": "src/removed.py", "src/edited.py": "src/edited.py"},
        )

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
        valid_lines, _, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
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
        valid_lines, _, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
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
        valid_lines, _, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
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
        valid_lines, _, _, _ = parse_diff_lines("github", "o", "r", 1)
        self.assertEqual([k for k in valid_lines if k[0] == "img.png"], [])

    @patch("scripts.post_review.run_api")
    def test_between_hunk_lines_are_not_admitted(self, mock_run):
        """Between-hunk lines are not commentable lines: the key set is EXACTLY the
        hunk bodies' addressable lines, with nothing admitted from around them.

        In plain `glab mr diff` the only lines between two hunks are the next file's
        `---`/`+++` pair — an under-drained hunk budget reads them as body content and
        admits a phantom target on the file before. Git's own decoration (`diff --git`,
        `index`, `Binary files … differ`) never appears in this output; it reaches the
        parser through `gh pr diff`, and the github-platform binary-prose test above
        owns that arm of the same catch-all.
        """
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        valid_lines, _, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(
            sorted(valid_lines),
            [("src/app/clients/api/__init__.py", n) for n in range(1, 17)]
            + [
                ("src/edited.py", 61),
                ("src/edited.py", 62),
                ("src/edited.py", 63),
            ],
        )

    # -- old-side tracking (issue #127 D1) ---------------------------------

    @patch("scripts.post_review.run_api")
    def test_valid_lines_is_a_mapping_of_new_line_to_old_line(self, mock_run):
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        valid_lines, _, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertIsInstance(valid_lines, dict)

    @patch("scripts.post_review.run_api")
    def test_context_line_maps_to_its_old_side_number(self, mock_run):
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        valid_lines, _, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(valid_lines[("src/edited.py", 61)], 50)

    @patch("scripts.post_review.run_api")
    def test_added_line_maps_to_none(self, mock_run):
        """An added line exists only on the new side — present as a key, valued None."""
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        valid_lines, _, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertIn(("src/edited.py", 62), valid_lines)
        self.assertIsNone(valid_lines[("src/edited.py", 62)])

    @patch("scripts.post_review.run_api")
    def test_removed_line_advances_the_old_side_only(self, mock_run):
        """52, not 51: the ``-removed`` line consumed an OLD number and no new one."""
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        valid_lines, _, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(valid_lines[("src/edited.py", 63)], 52)

    @patch("scripts.post_review.run_api")
    def test_old_side_counter_resets_between_files(self, mock_run):
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        valid_lines, _, _, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertIsNone(valid_lines[("src/app/clients/api/__init__.py", 1)])
        # The added file's `---`/`+++` headers sit between hunks. If the modified file's
        # budgets under-drain, the parser is still in its body zone when they arrive and
        # reads them as content — admitting a phantom target on the file before.
        self.assertNotIn(("src/edited.py", 64), valid_lines)
        self.assertEqual(
            {
                v
                for (fp, _), v in valid_lines.items()
                if fp == "src/app/clients/api/__init__.py"
            },
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
        valid_lines, _, _, _ = parse_diff_lines("github", "o", "r", 1)
        self.assertEqual(valid_lines[("f.py", 2)], 2)

    @patch("scripts.post_review.run_api")
    def test_renamed_file_old_side_path_is_captured(self, mock_run):
        """A rename's `---` path must survive parsing keyed by the NEW path.

        The parser previously read the old-side header only to compare it to
        ``/dev/null`` and threw the path away, so a renamed file's position shipped the
        post-rename path as ``old_path`` — a path that does not exist on the old side
        (#130). A rename is the ONE case where glab's two headers name different paths;
        it emits no `rename from`/`rename to`/`similarity index` lines to corroborate
        them.
        """
        mock_run.return_value = (GL_DIFF_RENAME, "", 0)
        valid_lines, _, old_paths, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(old_paths, {"new_name.py": "old_name.py"})
        self.assertEqual(valid_lines[("new_name.py", 3)], 3)
        # A BLANK context line is a lone space, and is addressable like any other.
        self.assertEqual(valid_lines[("new_name.py", 6)], 6)

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
        _, new_files, old_paths, _ = parse_diff_lines("github", "o", "r", 1)
        self.assertEqual(new_files, {"added.py"})
        self.assertNotIn("added.py", old_paths)

    @patch("scripts.post_review.run_api")
    def test_unrenamed_file_old_path_maps_to_itself(self, mock_run):
        """For a plain modified file both sides name the same path — pin the coincide
        case, so the mapping is provably a no-op there rather than accidentally right."""
        mock_run.return_value = (GL_DIFF_CONTRACT, "", 0)
        _, _, old_paths, _ = parse_diff_lines("gitlab", "o", "r", 1)
        self.assertEqual(old_paths["src/edited.py"], "src/edited.py")

    @patch("scripts.post_review.run_api")
    def test_nonzero_rc_returns_none(self, mock_run):
        """A non-zero exit code from the CLI tool must return (None, None, None)."""
        mock_run.return_value = ("", "fatal: not a git repository", 128)
        valid_lines, new_files, old_paths, _ = parse_diff_lines(
            "github", "myorg", "myrepo", 1
        )
        self.assertIsNone(valid_lines)
        self.assertIsNone(new_files)
        self.assertIsNone(old_paths)

    def test_unknown_platform_returns_none(self):
        """An unknown platform must return (None, None, None) without calling run_api."""
        valid_lines, new_files, old_paths, _ = parse_diff_lines(
            "bitbucket", "myorg", "myrepo", 1
        )
        self.assertIsNone(valid_lines)
        self.assertIsNone(new_files)
        self.assertIsNone(old_paths)

    @patch("scripts.post_review.run_api")
    def test_delegates_to_parse_diff_text_for_a_github_diff(self, mock_run):
        """A successful CLI fetch must hand the SAME bytes to
        ``parse_diff_text(platform, stdout)`` and return exactly what it returns —
        no second, divergent parse living inside the CLI wrapper."""
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " existing\n"
            "+added\n"
        )
        mock_run.return_value = (diff, "", 0)
        got = parse_diff_lines("github", "myorg", "myrepo", 42)
        self.assertEqual(got, parse_diff_text("github", diff))

    @patch("scripts.post_review.run_api")
    def test_delegates_to_parse_diff_text_for_a_glab_fixture(self, mock_run):
        """Same guarantee, for a real ``glab mr diff`` fixture (no ``diff --git``
        line, unprefixed headers)."""
        diff = _glab_fixture("modified.diff")
        mock_run.return_value = (diff, "", 0)
        got = parse_diff_lines("gitlab", "myorg", "myrepo", 7)
        self.assertEqual(got, parse_diff_text("gitlab", diff))


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


class TestOutboundSanitizeHelpers(unittest.TestCase):
    """Issue #122 — unit pins for each outbound transform (mutate whole helper)."""

    def test_terminated_html_comment_stripped(self):
        self.assertEqual(
            _sanitize_outbound_prose("before <!-- hide --> after"),
            "before  after",
        )

    def test_unterminated_html_comment_stripped_to_eos(self):
        self.assertEqual(
            _sanitize_outbound_prose("before <!-- forever"),
            "before ",
        )

    def test_entity_decoded_comment_then_stripped(self):
        # &#60;!-- … --&#62; must become a real comment then vanish (order fixture).
        self.assertEqual(
            _sanitize_outbound_prose("x&#60;!-- hidden --&#62;y"),
            "xy",
        )

    def test_hex_entity_decoded_comment_then_stripped(self):
        # &#x3C;!-- … --&#x3E; pins the _ENTITY_HEX_RE / _hex path.
        self.assertEqual(
            _sanitize_outbound_prose("x&#x3C;!-- hidden --&#x3E;y"),
            "xy",
        )

    def test_multiline_newlines_preserved_invisibles_stripped(self):
        raw = "line1\nline2\u200b\nline3\u202e"
        out = _sanitize_outbound_prose(raw)
        self.assertEqual(out, "line1\nline2\nline3")
        self.assertIn("\n", out)

    def test_backtick_run_collapsed_to_two(self):
        self.assertEqual(_sanitize_outbound_prose("a````b"), "a``b")

    def test_tab_and_newline_not_stripped_as_c0(self):
        self.assertEqual(_sanitize_outbound_prose("a\tb\nc"), "a\tb\nc")

    def test_carriage_return_stripped_as_c0(self):
        # CR is a CommonMark line ending; leaving it lets markdown after a
        # single '>' escape the blockquote. Design: C0 minus \\t\\n only.
        self.assertEqual(_sanitize_outbound_prose("a\rb\rc"), "abc")

    def test_non_ascii_numeric_entity_dropped(self):
        # &#8212; em-dash dropped (printable-ASCII-only decode).
        self.assertEqual(_sanitize_outbound_prose("a&#8212;b"), "ab")

    def test_redact_github_and_gitlab_tokens(self):
        ghp = "ghp_" + ("A" * 36)
        glpat = "glpat-" + ("B" * 20)
        out = _redact_secrets(f"tok {ghp} and {glpat} end")
        self.assertEqual(out, "tok [REDACTED] and [REDACTED] end")

    def test_redact_all_eight_credential_prefixes(self):
        prefixes = (
            "ghp_",
            "gho_",
            "ghs_",
            "ghr_",
            "ghu_",
            "github_pat_",
            "glpat-",
            "glrt-",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                body = "A" * 20 if prefix.endswith("-") else "A" * 36
                token = prefix + body
                self.assertEqual(_redact_secrets(f"x {token} y"), "x [REDACTED] y")

    def test_bare_glpat_prefix_survives_without_credential_body(self):
        # Prefix alone or followed by space/short token — not ≥20 hyphenated word chars.
        text = "Document the glpat- prefix in CLAUDE.md examples."
        self.assertEqual(_redact_secrets(text), text)

    def test_cap_appends_marker_outside_limit(self):
        text = "x" * 510
        out = _cap_rule_text(text, limit=500)
        self.assertTrue(out.endswith("…[truncated]"))
        self.assertEqual(out[:500], "x" * 500)
        self.assertEqual(len(out), 500 + len("…[truncated]"))

    def test_cap_exact_limit_no_marker(self):
        text = "y" * 500
        self.assertEqual(_cap_rule_text(text, limit=500), text)

    def test_cap_postcondition_no_backtick_run_ge_3(self):
        # Vacuous today after collapse-before-cap; kept so cap-before-sanitize
        # reorder goes red. Feed already-sanitized text with only `` runs.
        text = "ab``cd" * 100  # length > 500, max run 2
        out = _cap_rule_text(text, limit=500)
        self.assertIsNone(re.search(r"`{3,}", out))
        self.assertTrue(out.endswith("…[truncated]"))

    def test_blockquote_prefixes_every_line_bare_gt_on_blank(self):
        self.assertEqual(
            _blockquote("a\n\nb"),
            "> a\n>\n> b",
        )

    def test_blockquote_normalizes_cr_before_prefix(self):
        # Defense in depth: even if a CR reached _blockquote, it must not
        # become a line ending after a single '>' that escapes the quote.
        self.assertEqual(_blockquote("a\rb"), "> a\n> b")
        self.assertEqual(_blockquote("a\r\nb"), "> a\n> b")

    def test_cited_rule_cr_cannot_escape_blockquote(self):
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "claude_md_rule": "keep\rescape **bold**",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        # CR stripped by sanitize → single line inside the quote.
        self.assertIn("> keepescape **bold**", body)
        self.assertNotIn("\r", body)

    def test_suggestion_fence_lengthens_for_inner_triple(self):
        payload = "line1\n```\nline3"
        open_f, close_f = _suggestion_fence(payload)
        self.assertEqual(open_f, "````suggestion")
        self.assertEqual(close_f, "````")

    def test_suggestion_fence_stays_three_without_backticks(self):
        open_f, close_f = _suggestion_fence("return None")
        self.assertEqual(open_f, "```suggestion")
        self.assertEqual(close_f, "```")

    def test_suggestion_fence_four_inner_needs_five(self):
        payload = "````"
        open_f, close_f = _suggestion_fence(payload)
        self.assertEqual(open_f, "`````suggestion")
        self.assertEqual(close_f, "`````")

    def test_offsets_are_stated_in_the_header(self):
        open_f, close_f = _suggestion_fence("return None", offsets=(0, 2))
        self.assertEqual(open_f, "```suggestion:-0+2")
        self.assertEqual(close_f, "```")

    def test_both_offsets_are_stated_even_when_one_is_zero(self):
        """GitLab's parser takes ``-m`` and ``+n`` independently, so either could
        be omitted — emitting both makes the header state the whole range."""
        open_f, _ = _suggestion_fence("x", offsets=(2, 0))
        self.assertEqual(open_f, "```suggestion:-2+0")

    def test_no_offsets_and_zero_offsets_are_the_plain_header(self):
        """``suggestion:-0+0`` is an exact synonym for ``suggestion``, so the
        single-line bytes every platform understands are what ships."""
        for offsets in (None, (0, 0)):
            with self.subTest(offsets=offsets):
                open_f, _ = _suggestion_fence("x", offsets=offsets)
                self.assertEqual(open_f, "```suggestion")

    def test_offsets_compose_with_the_backtick_escalation(self):
        """Fence length and header are independent — GitLab's parser is
        fence-length blind, and its own docs show ````suggestion:-0+2."""
        open_f, close_f = _suggestion_fence("line1\n```\nline3", offsets=(0, 2))
        self.assertEqual(open_f, "````suggestion:-0+2")
        self.assertEqual(close_f, "````")


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

    def test_edge_blank_lines_survive_into_the_fence(self):
        """A replacement's leading/trailing blank lines are CONTENT (#63): the
        fence carries the stated bytes minus the one terminating newline, so
        what the apply-check measured is what one click commits."""
        finding = {
            "severity": "medium",
            "title": "Fix",
            "body": "desc",
            "suggested_fix_code": "\nline1\nline2\n\n",
        }
        body = render_comment_body(finding)
        self.assertIn("```suggestion\n\nline1\nline2\n\n```", body)

    def test_only_one_trailing_newline_comes_off(self):
        finding = {
            "severity": "medium",
            "title": "Fix",
            "body": "desc",
            "suggested_fix_code": "line1\n",
        }
        self.assertIn("```suggestion\nline1\n```", render_comment_body(finding))

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
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> Scripts must be stdlib-only Python.", body)

    def test_spec_text_present_claude_md_rule_absent_renders_as_cited_rule(self):
        finding = {
            "severity": "medium",
            "title": "Intent mismatch",
            "body": "desc",
            "spec_text": "The spec says X must happen before Y.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> The spec says X must happen before Y.", body)

    def test_both_claude_md_rule_and_spec_text_present_rule_wins(self):
        finding = {
            "severity": "medium",
            "title": "Both",
            "body": "desc",
            "claude_md_rule": "The CLAUDE.md rule.",
            "spec_text": "The spec text.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> The CLAUDE.md rule.", body)
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
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> The spec text wins here.", body)

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
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> 7", body)

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
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> The endpoint MUST return 422 on a schema violation.", body)

    def test_non_string_spec_text_does_not_crash(self):
        finding = {"severity": "low", "title": "T", "body": "b", "spec_text": 9}
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> 9", body)

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
        self.assertIn("**Suggested fix:**\nLine one\nLine two", body)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> Rule text", body)
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


class TestOutboundRenderBounding(unittest.TestCase):
    """Issue #122 — render_comment_body composed behaviors."""

    def test_comment_only_claude_md_rule_omits_cited_rule_heading(self):
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "claude_md_rule": "<!-- steer the reviewer -->",
        }
        body = render_comment_body(finding)
        self.assertNotIn("Cited rule:", body)

    def test_comment_only_claude_md_rule_falls_back_to_spec_text(self):
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "claude_md_rule": "<!-- steer the reviewer -->",
            "spec_text": "The spec says X must happen before Y.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> The spec says X must happen before Y.", body)

    def test_cited_rule_is_blockquoted_multiline(self):
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "claude_md_rule": "line1\n\nline3",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**\n> line1\n>\n> line3", body)

    def test_long_rule_capped_with_marker(self):
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "claude_md_rule": "R" * 600,
        }
        body = render_comment_body(finding)
        self.assertIn("…[truncated]", body)
        # Quoted content before marker is 500 R's
        self.assertIn("> " + ("R" * 500) + "…[truncated]", body)

    def test_suggestion_sanitized_but_uncapped(self):
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "suggestion": ("fix it <!-- no --> " + ("s" * 600)),
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**", body)
        self.assertNotIn("<!--", body)
        self.assertNotIn("…[truncated]", body)
        self.assertIn("s" * 600, body)

    def test_fence_contains_payload_with_inner_triple_backticks(self):
        payload = "before\n```\nafter"
        open_f = "````suggestion"
        close_f = "````"
        finding = {
            "severity": "low",
            "title": "T",
            "body": "b",
            "suggested_fix_code": payload,
        }
        body = render_comment_body(finding)
        self.assertIn(open_f, body)
        self.assertIn(close_f, body)
        # Parse: content between first open and last close equals payload
        start = body.index(open_f) + len(open_f) + 1  # +1 for newline
        end = body.rindex("\n" + close_f)
        self.assertEqual(body[start:end], payload)

    def test_suggested_fix_code_token_redacted_inside_fence(self):
        tok = "ghp_" + ("C" * 36)
        payload = f"token = '{tok}'"
        finding = {
            "severity": "low",
            "title": "T",
            "body": "b",
            "suggested_fix_code": payload,
        }
        body = render_comment_body(finding)
        self.assertNotIn(tok, body)
        self.assertIn("[REDACTED]", body)
        self.assertIn("```suggestion", body)  # no backticks in redacted form → 3-fence


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
        post_review.post_github(data, {}, {})
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
        post_review.post_github(data, {}, {})
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
        post_review.post_gitlab(data, {}, set(), {}, {})
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
        post_review.post_gitlab(data, {}, set(), {}, {})
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
        post_github(data, valid_lines, {})
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
    def test_github_skip_empty_valid_lines_reports_an_empty_diag_list(
        self, mock_warn, _post, _tool, _sha
    ):
        from scripts.post_review import post_github

        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [{"file": "src/app.py", "line": 99, "title": "Bug"}],
        }
        # Empty mapping: validation ran and found nothing for this line → skip + empty diag.
        post_github(data, {}, {})
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        self.assertIn("line not found in diff.", msg)
        self.assertIn("Valid lines for this file: []", msg)

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
        self, mock_warn, mock_post, _tool, _sha
    ):
        from scripts.post_review import post_github

        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [{"file": "src/app.py", "line": 99, "title": "Bug"}],
        }
        # valid_lines=None: validation skipped → is_line_valid returns True → finding posts.
        post_github(data, None, None)
        mock_post.assert_called_once()
        payload = mock_post.call_args[0][1]
        self.assertEqual(len(payload["comments"]), 1)
        self.assertEqual(payload["comments"][0]["path"], "src/app.py")
        self.assertEqual(payload["comments"][0]["line"], 99)
        for call in mock_warn.call_args_list:
            self.assertNotIn(
                "Valid lines for this file:",
                call[0][0],
                "None must not emit a valid-lines diagnostic",
            )

    @patch(
        "scripts.post_review.get_head_sha",
        return_value="abc1234def5678abc1234def5678abc1234def56",
    )
    @patch("scripts.post_review.check_tool")
    @patch("scripts.post_review.post_json", return_value={})
    @patch("scripts.post_review.fetch_gitlab_shas", return_value=("b", "h", "s"))
    # The live path now asks detect_prior_review what the MR already carries; that read
    # shells out to `glab`, so it is stubbed here rather than left to reach a real forge
    # from a unit test.
    @patch(
        "scripts.post_review.gitlab_prior_delivery_state",
        return_value=(False, set(), frozenset(), None),
    )
    @patch("scripts.post_review.warn")
    def test_gitlab_skip_includes_valid_lines(
        self, mock_warn, _prior, _shas, _post, _tool, _sha
    ):
        from scripts.post_review import post_gitlab

        valid_lines = {("src/app.py", 5): 5, ("src/app.py", 15): None}
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [{"file": "src/app.py", "line": 99, "title": "Bug"}],
        }
        post_gitlab(data, valid_lines, set(), {}, {})
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

    def test_unresolved_prefix_no_longer_falls_back(self):
        """The sole caller pre-resolves via diff_path_spelling before calling here;
        is_new_file itself does exact-match only. A raw synthetic-prefixed path that
        was never resolved is correctly NOT treated as a match.
        """
        from scripts.post_review import is_new_file

        self.assertFalse(is_new_file({"src/added.py"}, "b/src/added.py"))
        self.assertFalse(is_new_file({"src/added.py"}, "a/src/added.py"))

    def test_no_match_returns_false(self):
        from scripts.post_review import is_new_file

        self.assertFalse(is_new_file({"src/added.py"}, "src/other.py"))

    def test_real_a_prefix_does_not_collide_with_stripped_new_file(self):
        """A modified file under a real top-level `a/` directory must not be mistaken
        for an unrelated new file that happens to share its stripped basename.
        """
        from scripts.post_review import is_new_file

        # "a/foo.py" (modified, real a/ directory) is itself absent from new_files;
        # only the unrelated new top-level "foo.py" is present. A stripped-prefix
        # fallback would wrongly report the modified file as new.
        self.assertFalse(is_new_file({"foo.py"}, "a/foo.py"))


# ---------------------------------------------------------------------------
# validate_position — the position gate
# ---------------------------------------------------------------------------


def _parse_fixture(diff, platform="gitlab"):
    """Return ``parse_diff_lines`` output for *diff*.

    The ground truth a position is checked against comes from the REAL parser here: a
    hand-written ``valid_lines`` would let the gate be tested against the answer the test
    wanted rather than the one the pipeline produces.
    """
    with patch("scripts.post_review.run_api", return_value=(diff, "", 0)):
        return parse_diff_lines(platform, "o", "r", 1)


_MR_SHAS = ("base1", "head1", "start1")

# The four keys every position carries whatever the finding is. Spelled here so a test
# case states only the fields its own case is about.
_POSITION_INVARIANTS = {
    "position_type": "text",
    "base_sha": "base1",
    "head_sha": "head1",
    "start_sha": "start1",
}


def _position(**fields):
    """A position carrying the invariant keys plus *fields*."""
    return dict(_POSITION_INVARIANTS, **fields)


class TestValidatePosition(unittest.TestCase):
    """One test per violation branch, each mutating a sound position in exactly ONE way.

    Every case asserts a single problem, so a check that fires on the wrong input shows
    up as an extra entry rather than hiding inside a truthy list.
    """

    # A sound context-line position for a modified file. Every violation test below
    # starts here and breaks one field.
    def _sound(self, **fields):
        return _position(
            new_path="src/edited.py",
            new_line=61,
            old_line=50,
            old_path="src/edited.py",
            **fields,
        )

    def _check(self, position, **overrides):
        kwargs = {
            "shas": _MR_SHAS,
            "valid_lines": {("src/edited.py", 61): 50},
            "new_files": set(),
            "old_paths": {"src/edited.py": "src/edited.py"},
            "filepath": "src/edited.py",
            "line": 61,
        }
        kwargs.update(overrides)
        return validate_position(position, **kwargs)

    def _only_problem(self, position, **overrides):
        problems = self._check(position, **overrides)
        self.assertEqual(len(problems), 1, problems)
        return problems[0]

    def test_sound_position_reports_nothing(self):
        self.assertEqual(self._check(self._sound()), [])

    # -- new_line ----------------------------------------------------------

    def test_new_line_missing(self):
        position = self._sound()
        del position["new_line"]
        self.assertIn("new_line is missing", self._only_problem(position))

    def test_new_line_non_integer(self):
        """A float line number survives every lookup: ``61.0`` hashes and compares equal
        to ``61``, so it passes line validation and reaches the wire as ``61.0``."""
        self.assertTrue(
            is_line_valid({("src/edited.py", 61): 50}, "src/edited.py", 61.0)
        )
        position = dict(self._sound(), new_line=61.0)
        problem = self._only_problem(position, line=61.0)
        self.assertIn("new_line must be an integer", problem)

    def test_new_line_boolean(self):
        """``True`` is an ``int`` to ``isinstance`` and hashes equal to ``1``, so a
        boolean line number passes line validation and ships as JSON ``true``."""
        self.assertTrue(is_line_valid({("f.py", 1): None}, "f.py", True))
        position = _position(new_path="f.py", new_line=True, old_path="f.py")
        problem = self._only_problem(
            position,
            valid_lines={("f.py", 1): None},
            old_paths={},
            filepath="f.py",
            line=True,
        )
        self.assertIn("new_line must be an integer", problem)

    def test_new_line_disagrees_with_the_finding(self):
        position = dict(self._sound(), new_line=62)
        problem = self._only_problem(position)
        self.assertIn("new_line is 62, expected 61", problem)

    # -- line_code ---------------------------------------------------------

    def test_line_code_present(self):
        position = dict(self._sound(), line_code="abc_50_61")
        self.assertIn("line_code", self._only_problem(position))

    # -- old_line ----------------------------------------------------------

    def test_old_line_absent_on_a_context_line(self):
        position = self._sound()
        del position["old_line"]
        self.assertIn("old_line is missing, expected 50", self._only_problem(position))

    def test_old_line_present_on_an_added_line(self):
        """An added line has no old side; sending one anchors the comment to a line the
        old revision never had."""
        position = dict(self._sound(), new_line=62, old_line=51)
        problem = self._only_problem(
            position,
            valid_lines={("src/edited.py", 62): None},
            line=62,
        )
        self.assertIn("old_line must not be sent for this position", problem)

    def test_old_line_wrong_value(self):
        position = dict(self._sound(), old_line=49)
        self.assertIn("old_line is 49, expected 50", self._only_problem(position))

    # -- old_path ----------------------------------------------------------

    def test_old_path_absent_on_a_modified_file(self):
        position = self._sound()
        del position["old_path"]
        self.assertIn(
            "old_path is missing, expected 'src/edited.py'",
            self._only_problem(position),
        )

    def test_old_path_present_on_an_added_file(self):
        position = _position(
            new_path="src/added.py",
            new_line=1,
            old_path="src/added.py",
        )
        problem = self._only_problem(
            position,
            valid_lines={("src/added.py", 1): None},
            new_files={"src/added.py"},
            old_paths={},
            filepath="src/added.py",
            line=1,
        )
        self.assertIn("old_path must not be sent for this position", problem)

    def test_old_path_carries_the_post_rename_path(self):
        """The rename class: the post-rename path is a path the old side does not
        contain, and presence alone cannot tell it from the pre-rename one."""
        valid_lines, new_files, old_paths, _ = _parse_fixture(GL_DIFF_RENAME)
        position = _position(
            new_path="new_name.py",
            new_line=3,
            old_line=3,
            old_path="new_name.py",
        )
        problem = self._only_problem(
            position,
            valid_lines=valid_lines,
            new_files=new_files,
            old_paths=old_paths,
            filepath="new_name.py",
            line=3,
        )
        self.assertIn("old_path is 'new_name.py', expected 'old_name.py'", problem)

    # -- the loop-invariant keys -------------------------------------------

    def test_sha_key_missing(self):
        """A dropped SHA is a guaranteed 400 that no per-finding fact can reveal — the
        position must be checked for CARRYING the fetched value, not just for the value
        being usable."""
        for key in ("base_sha", "head_sha", "start_sha"):
            with self.subTest(key=key):
                position = self._sound()
                del position[key]
                self.assertIn(f"{key} is missing", self._only_problem(position))

    def test_sha_value_disagrees_with_the_fetch(self):
        position = dict(self._sound(), head_sha="wrong")
        self.assertIn(
            "head_sha is 'wrong', expected 'head1'", self._only_problem(position)
        )

    def test_position_type_wrong(self):
        position = dict(self._sound(), position_type="txet")
        self.assertIn(
            "position_type is 'txet', expected 'text'", self._only_problem(position)
        )

    def test_new_path_disagrees_with_the_resolved_path(self):
        position = dict(self._sound(), new_path="b/src/edited.py")
        self.assertIn("new_path is 'b/src/edited.py'", self._only_problem(position))

    def test_unrecognised_key(self):
        """`line_range` is `line_code`'s sibling: both are derived server-side and both
        answer the identical 400. Nothing enumerates them — an unexpected key is a
        malformed position whether or not anyone knew to name it."""
        position = dict(self._sound(), line_range={"start": {}})
        self.assertIn("line_range must not be sent", self._only_problem(position))

    # -- no false positives ------------------------------------------------

    def test_legitimate_positions_report_nothing(self):
        """Every position kind the poster legitimately builds, against parser output."""
        contract = _parse_fixture(GL_DIFF_CONTRACT)[:3]
        rename = _parse_fixture(GL_DIFF_RENAME)[:3]
        real_a_dir = _parse_fixture(GL_DIFF_REAL_A_DIR)[:3]
        cases = [
            (
                "context line in a modified file",
                contract,
                "src/edited.py",
                61,
                _position(
                    new_path="src/edited.py",
                    new_line=61,
                    old_line=50,
                    old_path="src/edited.py",
                ),
            ),
            (
                "added line in a modified file",
                contract,
                "src/edited.py",
                62,
                _position(
                    new_path="src/edited.py",
                    new_line=62,
                    old_path="src/edited.py",
                ),
            ),
            (
                "line in a newly added file",
                contract,
                "src/app/clients/api/__init__.py",
                1,
                _position(new_path="src/app/clients/api/__init__.py", new_line=1),
            ),
            (
                "context line in a renamed file",
                rename,
                "new_name.py",
                3,
                _position(
                    new_path="new_name.py",
                    new_line=3,
                    old_line=3,
                    old_path="old_name.py",
                ),
            ),
            (
                "literal a/ directory path",
                real_a_dir,
                "a/foo.py",
                1,
                _position(
                    new_path="a/foo.py",
                    new_line=1,
                    old_line=1,
                    old_path="a/foo.py",
                ),
            ),
        ]
        for label, parsed, filepath, line, position in cases:
            valid_lines, new_files, old_paths = parsed
            with self.subTest(case=label):
                self.assertEqual(
                    validate_position(
                        position,
                        _MR_SHAS,
                        valid_lines,
                        new_files,
                        old_paths,
                        filepath,
                        line,
                    ),
                    [],
                )

    def test_skipped_validation_position_reports_nothing(self):
        """With no diff to consult the poster ships the finding's raw path and no
        old_line; the gate must not invent an expectation it cannot have."""
        position = _position(new_path="b/x.py", new_line=3, old_path="b/x.py")
        self.assertEqual(
            validate_position(position, _MR_SHAS, None, None, None, "b/x.py", 3),
            [],
        )


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
            # The live path asks detect_prior_review what the MR already carries;
            # that read shells out to `glab`, so it is stubbed rather than left to
            # reach a real forge from a unit test.
            patch(
                "scripts.post_review.gitlab_prior_delivery_state",
                return_value=(False, set(), frozenset(), None),
            ),
        ):
            post_gitlab(data, valid_lines, new_files, {}, {})

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

    def test_real_a_dir_modified_file_keeps_old_path_despite_stripped_collision(self):
        """A modified file under a real top-level `a/` directory must keep old_path
        even when an unrelated new file shares its stripped basename.

        Regression for the is_new_file stripped-prefix fallback: "a/foo.py" is
        modified (real a/ directory, GitLab verbatim spelling) while "foo.py" is a
        DIFFERENT, newly-added top-level file in the same diff. Before the fix,
        is_new_file's independent `^[ab]/` strip matched "foo.py" against new_files
        and wrongly reported the modified file as new, dropping old_path.
        """
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [{"file": "a/foo.py", "line": 10, "title": "Bug", "body": "x"}],
        }
        valid_lines = {("a/foo.py", 10): 7}
        new_files = {"foo.py"}
        position = self._capture_position(data, valid_lines, new_files)
        self.assertEqual(position["old_path"], "a/foo.py")
        self.assertEqual(position["new_path"], "a/foo.py")

    def test_skipped_validation_ships_the_findings_raw_path(self):
        """With no diff to consult, the finding's own spelling travels untouched.

        Pre-branch main shipped the raw path here and delivery worked; an
        unconditional `^[ab]/` strip would rewrite a real `b/`-rooted path into one the
        forge does not have, and there is no parsed key left to catch the mistake.
        """
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [{"file": "b/x.py", "line": 3, "title": "Bug", "body": "x"}],
        }
        position = self._capture_position(data, valid_lines=None, new_files=None)
        self.assertEqual(position["new_path"], "b/x.py")
        self.assertEqual(position["old_path"], "b/x.py")


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

# A GitLab diff (glab mr diff) that makes bar.py lines 1 and 2 valid. The `---`/`+++`
# headers are UNPREFIXED because that is what glab emits. The leading `diff --git` line is
# NOT part of plain `glab mr diff` output (tests/fixtures/glab_diff/README.md has the
# shape and its sources); it survives here because these are delivery-path tests that do
# not turn on diff shape, and the parser ignores the line either way.
GL_DIFF = (
    "diff --git a/bar.py b/bar.py\n"
    "--- bar.py\n"
    "+++ bar.py\n"
    "@@ -1,1 +1,2 @@\n"
    " ctx\n"
    "+newline\n"
)


_GLAB_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "glab_diff")


def _glab_fixture(name):
    """Read one `glab mr diff` fixture verbatim.

    The shape these files record — and which bytes of it are a real capture — is
    documented in tests/fixtures/glab_diff/README.md. They are read rather than inlined
    so the parser's contract has ONE spelling that a real capture can later replace
    without touching a test.
    """
    with open(os.path.join(_GLAB_FIXTURE_DIR, name), encoding="utf-8") as fh:
        return fh.read()


# A modified file followed by an added one, in the shape plain `glab mr diff` emits.
# src/edited.py: new 61 = old 50 (context), new 62 = added, new 63 = old 52 (context).
# src/app/clients/api/__init__.py: new 1..16 — added file, signalled only by `@@ -0,0`.
GL_DIFF_CONTRACT = _glab_fixture("modified.diff") + _glab_fixture("added.diff")

# A deleted file followed by a modified one. glab repeats the path on BOTH headers for a
# deletion (there is no `+++ /dev/null` to blank `current_file`), so the deleted file's
# hunk budget draining is the only thing keeping the next file's headers out of its body.
GL_DIFF_DELETED_THEN_MODIFIED = _glab_fixture("deleted.diff") + _glab_fixture(
    "modified.diff"
)

# A RENAMED file: the `---` header names the PRE-rename path and the `+++` header the
# post-rename one. That old-side path is what GitLab needs in `position.old_path` (#130).
# new 3 = old 3 (context), new 4 = added, new 5 = old 5 (context), new 6 = old 6 (a BLANK
# context line, which a unified diff spells as a lone space).
GL_DIFF_RENAME = _glab_fixture("rename.diff")


class TestGlabFixtureBytes(unittest.TestCase):
    """The fixtures above are a RECORD, so their bytes are the contract — including the
    bytes no parser assertion can see.

    ``parse_diff_lines`` reads a blank context line (a lone space) and a stripped one
    (empty) through the same catch-all and produces the identical key, so a whitespace
    fixer could rewrite the record with every other test in this file still green. These
    assertions are what make the `.pre-commit-config.yaml` exclusion enforceable rather
    than advisory: drop the exclusion, or add any hook that normalises these files, and
    the record's loss is reported here instead of going unnoticed.
    """

    def _fixtures(self):
        names = sorted(n for n in os.listdir(_GLAB_FIXTURE_DIR) if n.endswith(".diff"))
        self.assertTrue(names, "no fixture files to hold to their recorded bytes")
        return [(n, _glab_fixture(n)) for n in names]

    def test_a_blank_context_line_is_recorded_as_a_lone_space(self):
        blanks = []
        for name, text in self._fixtures():
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    continue
                self.assertEqual(
                    line,
                    " ",
                    f"{name}:{lineno}: a unified diff spells a blank context line as a "
                    "lone space; an empty line is a stripped record, not glab output",
                )
                blanks.append(f"{name}:{lineno}")
        self.assertTrue(
            blanks,
            "no fixture carries a blank context line any more, so the assertion above "
            "passes over nothing and defends nothing",
        )

    def test_every_fixture_ends_with_exactly_one_newline(self):
        """glab appends no separator between two files: it writes the `---`/`+++` pair
        and then the API's diff body, so one file's last line runs straight into the
        next file's `---` unless that body is newline-terminated.

        The multi-file constants above concatenate these files directly, which is real
        output only under that property — the one part of the recorded shape that is
        inferred rather than captured (see the fixture README). Asserting it here keeps
        the concatenation from quietly becoming a shape no CLI emits.
        """
        for name, text in self._fixtures():
            self.assertTrue(
                text.endswith("\n"), f"{name}: file boundary needs a final newline"
            )
            self.assertFalse(
                text.endswith("\n\n"),
                f"{name}: a trailing blank line is not glab output — a blank context "
                "line is a lone space and a blank added line is a bare `+`",
            )


# A glab diff for a repo with a REAL top-level `a/` directory. `glab mr diff` prints
# paths verbatim, so `a/foo.py` here is a directory named `a` — not git's synthetic
# old-side prefix. The `diff --git` decoration is not glab's (see GL_DIFF above).
# new 1 = old 1 (context), new 2 = added.
GL_DIFF_REAL_A_DIR = (
    "diff --git a/a/foo.py b/a/foo.py\n"
    "--- a/foo.py\n"
    "+++ a/foo.py\n"
    "@@ -1,2 +1,2 @@\n"
    " ctx\n"
    "-x\n"
    "+y\n"
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
        "file": "src/app/clients/api/__init__.py",
        "line": 1,
        "severity": "low",
        "title": "New-file finding",
        "body": "Body three",
    },
]

_GL_CONSOLIDATION_KEY = "src/edited.py:60"


def _gl_primary(line=61):
    """The stamped primary of a consolidation group over the contract diff."""
    return dict(
        GL_CONTRACT_FINDINGS[0],
        line=line,
        consolidation_key=_GL_CONSOLIDATION_KEY,
        consolidation_primary=True,
    )


def _gl_corroborator(tag, line):
    """A stamped non-primary member of the same group as ``_gl_primary``."""
    return {
        "file": "src/edited.py",
        "line": line,
        "severity": "medium",
        "title": f"Corroborator {tag}",
        "body": f"Body corr {tag}",
        "agent": "bug-detector",
        "dimension": "correctness",
        "confidence": 70,
        "consolidation_key": _GL_CONSOLIDATION_KEY,
        "consolidation_primary": False,
    }


def _member_key(member):
    """The delivery key one group member carries, whatever shape delivers it.

    Derived from the member's OWN anchor and single-finding render — the same
    inputs the individual-discussion path uses — which is what makes a group
    discussion and an individual one interchangeable for rerun dedup.

    ``suggested_fix_code`` is dropped before the render, unconditionally, because
    a delivery key must not depend on the apply-check's verdict (#63 D2). Spelled
    out here rather than borrowed from post_review so this helper cannot agree
    with a broken implementation by construction.
    """
    material = {k: v for k, v in member.items() if k != "suggested_fix_code"}
    return post_review.finding_key(
        member["file"],
        member["line"],
        member["title"],
        post_review.render_comment_body(material),
    )


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
    diff_rc=0,
    discussion_rcs=None,
    calls=None,
    payloads=None,
):
    """Build a ``subprocess.run`` side_effect that mocks the read-only CLI calls.

    Handles ``which``, ``git remote get-url``, ``git rev-parse``, ``gh pr diff``,
    ``glab mr diff``, and the GitLab ``.../versions`` GET. Any other command
    (i.e. a POST) returns an empty JSON object — but in dry-run mode ``post_json``
    short-circuits before reaching ``subprocess.run`` for POSTs.

    The live GitLab POSTs are steerable so the fault-tolerance path is exercisable:
    *note_rc* is the summary note's exit code, and *discussion_rcs* is consumed one per
    inline-discussion POST (default 0 once exhausted). A non-zero discussion rc comes
    back with the verbatim glab 400. *diff_rc* is the diff fetch's exit code — a
    non-zero one is the real "no diff oracle" condition (``parse_diff_lines`` then
    returns all-``None``), not something a test can fake by passing an empty diff.
    *calls* collects every argv when given, and *payloads* collects the JSON body of
    every live POST.
    """
    rcs = iter(discussion_rcs or [])

    def _run(cmd, *a, **k):
        def res(out="", err="", rc=0):
            return SimpleNamespace(stdout=out, stderr=err, returncode=rc)

        if calls is not None:
            calls.append(cmd)
        if payloads is not None and "--input" in cmd:
            # The live path hands its JSON to gh/glab through a temp file that is
            # unlinked the moment the call returns, so reading it here is the only
            # place a test can see the bytes that actually go on the wire.
            with open(cmd[cmd.index("--input") + 1]) as fh:
                payloads.append(json.load(fh))
        if cmd[0] == "which":
            return res(out="/usr/bin/" + cmd[1])
        if cmd[:3] == ["git", "remote", "get-url"]:
            return res(out=remote)
        if cmd[:2] == ["git", "rev-parse"]:
            return res(out=head_sha)
        if cmd[:3] == ["gh", "pr", "diff"] or cmd[:3] == ["glab", "mr", "diff"]:
            if diff_rc:
                return res(err="fatal: could not read the diff", rc=diff_rc)
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


def _normalize_prior(prior):
    """Pad a legacy 3-tuple ``(summary_posted, keys, error)`` ``prior`` fixture to the
    current 4-tuple ``gitlab_prior_delivery_state`` contract — ``(summary_posted, keys,
    legacy_group_keys, error)`` — so the many existing fixtures that predate legacy-group
    -body detection don't all need rewriting. A 4-tuple (a test that DOES want to steer
    legacy_group_keys) passes through unchanged.
    """
    if len(prior) == 4:
        return prior
    summary_posted, keys, error = prior
    return summary_posted, keys, frozenset(), error


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
        post_review._FIX_COUNTS.update(kept=0, downgraded=0)

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


class TestConsolidateDelivery(unittest.TestCase):
    """Pure grouping helper for the delivery payload (#22 D2). Findings stay
    distinct in the array; this only groups them for rendering."""

    def test_findings_without_stamps_each_become_a_singleton_group(self):
        a = {"file": "foo.py", "line": 2, "title": "A"}
        b = {"file": "foo.py", "line": 3, "title": "B"}
        groups = consolidate_delivery([a, b])
        self.assertEqual(
            groups,
            [
                {"primary": a, "corroborators": []},
                {"primary": b, "corroborators": []},
            ],
        )

    def test_shared_consolidation_key_groups_primary_and_corroborators(self):
        primary = {
            "file": "foo.py",
            "line": 2,
            "title": "A",
            "consolidation_key": "foo.py:0",
            "consolidation_primary": True,
        }
        corroborator = {
            "file": "foo.py",
            "line": 3,
            "title": "B",
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
        }
        groups = consolidate_delivery([primary, corroborator])
        self.assertEqual(
            groups, [{"primary": primary, "corroborators": [corroborator]}]
        )

    def test_group_position_is_the_primarys_first_occurrence(self):
        """A group occupies the array position of its FIRST member, whichever
        that is — order stays deterministic even when the primary is not the
        first element carrying the key."""
        corroborator = {
            "title": "B",
            "consolidation_key": "k",
            "consolidation_primary": False,
        }
        other = {"title": "C"}
        primary = {
            "title": "A",
            "consolidation_key": "k",
            "consolidation_primary": True,
        }
        groups = consolidate_delivery([corroborator, other, primary])
        self.assertEqual(
            groups,
            [
                {"primary": primary, "corroborators": [corroborator]},
                {"primary": other, "corroborators": []},
            ],
        )

    def test_multiple_corroborators_preserve_relative_order(self):
        primary = {
            "title": "A",
            "consolidation_key": "k",
            "consolidation_primary": True,
        }
        c1 = {"title": "B", "consolidation_key": "k", "consolidation_primary": False}
        c2 = {"title": "C", "consolidation_key": "k", "consolidation_primary": False}
        groups = consolidate_delivery([primary, c1, c2])
        self.assertEqual(groups[0]["corroborators"], [c1, c2])

    def test_distinct_keys_produce_distinct_groups(self):
        a = {"title": "A", "consolidation_key": "k1", "consolidation_primary": True}
        b = {"title": "B", "consolidation_key": "k2", "consolidation_primary": True}
        groups = consolidate_delivery([a, b])
        self.assertEqual(len(groups), 2)

    def test_second_primary_in_a_group_is_demoted_not_dropped(self):
        p1 = {"title": "A", "consolidation_key": "k", "consolidation_primary": True}
        c1 = {"title": "B", "consolidation_key": "k", "consolidation_primary": False}
        p2 = {"title": "C", "consolidation_key": "k", "consolidation_primary": True}
        groups = consolidate_delivery([p1, c1, p2])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["primary"], p1)
        self.assertEqual(groups[0]["corroborators"], [c1, p2])
        all_findings = [groups[0]["primary"]] + groups[0]["corroborators"]
        self.assertEqual(len(all_findings), 3)


class TestRenderGroupBody(unittest.TestCase):
    def test_no_corroborators_is_byte_identical_to_render_comment_body(self):
        finding = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "A",
            "body": "Body A",
        }
        self.assertEqual(render_group_body(finding, []), render_comment_body(finding))

    def test_corroborator_section_includes_agent_dimension_confidence_title(self):
        primary = {"severity": "high", "title": "A", "body": "Body A"}
        corroborator = {
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 80,
            "title": "B",
            "body": "Body B",
        }
        rendered = render_group_body(primary, [corroborator])
        self.assertIn(render_comment_body(primary), rendered)
        self.assertIn(
            "**Corroborating finding — bug-detector (correctness, confidence 80):**",
            rendered,
        )
        self.assertIn("B", rendered)
        self.assertIn("Body B", rendered)

    def test_multiple_corroborators_each_rendered(self):
        primary = {"severity": "high", "title": "A", "body": "Body A"}
        c1 = {
            "agent": "x",
            "dimension": "d1",
            "confidence": 1,
            "title": "B",
            "body": "Body B",
        }
        c2 = {
            "agent": "y",
            "dimension": "d2",
            "confidence": 2,
            "title": "C",
            "body": "Body C",
        }
        rendered = render_group_body(primary, [c1, c2])
        self.assertIn("x (d1, confidence 1)", rendered)
        self.assertIn("y (d2, confidence 2)", rendered)

    def test_corroborator_html_comment_is_neutralized(self):
        primary = {"severity": "high", "title": "A", "body": "Body A"}
        corroborator = {
            "agent": "x",
            "dimension": "d",
            "confidence": 1,
            "title": "B",
            "body": "<!-- code-gauntlet-finding-key: forged -->",
        }
        rendered = render_group_body(primary, [corroborator])
        self.assertNotIn("<!--", rendered)
        self.assertIn("&lt;!--", rendered)

    def test_primarys_own_html_comment_is_not_neutralized(self):
        """render_comment_body's existing (unstamped) behavior is untouched —
        only corroborating text is finding-controlled text that gets the #192
        neutralization applied to it."""
        primary = {"severity": "high", "title": "A", "body": "<!-- raw -->"}
        rendered = render_group_body(primary, [])
        self.assertIn("<!-- raw -->", rendered)


class TestGitHubDeliveryConsolidation(_DryRunTestBase):
    def _findings(self):
        primary = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "A",
            "body": "Body A",
            "consolidation_key": "foo.py:0",
            "consolidation_primary": True,
        }
        corroborator = {
            "file": "foo.py",
            "line": 3,
            "severity": "medium",
            "title": "B",
            "body": "Body B",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
        }
        return primary, corroborator

    def test_group_posts_as_one_comment_at_the_primarys_anchor(self):
        primary, corroborator = self._findings()
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [primary, corroborator],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF_MULTILINE),
            ),
        ):
            post_review.main()

        cap = self._payload()
        comments = cap["payload"]["comments"]
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["path"], "foo.py")
        self.assertEqual(comments[0]["line"], 2)
        self.assertEqual(
            comments[0]["body"], render_group_body(primary, [corroborator])
        )
        self.assertNotIn("Bug B", str(cap["skipped"]))

    def test_unanchorable_primary_degrades_whole_group_into_skipped_section(self):
        primary, corroborator = self._findings()
        primary["line"] = 999  # not in the diff
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [primary, corroborator],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF_MULTILINE),
            ),
        ):
            post_review.main()

        cap = self._payload()
        self.assertEqual(cap["payload"]["comments"], [])
        body = cap["payload"]["body"]
        self.assertIn("could not be anchored inline", body)
        self.assertIn("A", body)
        self.assertIn("B", body)
        self.assertIn("Body A", body)
        self.assertIn("Body B", body)

    def test_no_line_primary_degrades_whole_group_into_skipped_section(self):
        """The primary carries no line at all (distinct from a line the diff
        doesn't touch) — the same whole-group degrade must fire on this branch
        too, not just the invalid-line branch."""
        primary, corroborator = self._findings()
        del primary["line"]
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [primary, corroborator],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF_MULTILINE),
            ),
        ):
            post_review.main()

        cap = self._payload()
        self.assertEqual(cap["payload"]["comments"], [])
        body = cap["payload"]["body"]
        self.assertIn("A", body)
        self.assertIn("B", body)
        self.assertIn("Body A", body)
        self.assertIn(
            "Body B", body, "the corroborator must fan out into the skipped section too"
        )


class TestGitLabDeliveryConsolidation(_DryRunTestBase):
    def _findings(self):
        primary = {
            "file": "bar.py",
            "line": 1,
            "severity": "high",
            "title": "A",
            "body": "Body A",
            "consolidation_key": "bar.py:0",
            "consolidation_primary": True,
        }
        corroborator = {
            "file": "bar.py",
            "line": 2,
            "severity": "medium",
            "title": "B",
            "body": "Body B",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "bar.py:0",
            "consolidation_primary": False,
        }
        return primary, corroborator

    def _versions(self):
        return [
            {
                "base_commit_sha": "base1",
                "head_commit_sha": "head1",
                "start_commit_sha": "start1",
            }
        ]

    def test_group_posts_as_one_discussion_at_the_primarys_anchor(self):
        primary, corroborator = self._findings()
        self._write(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": [primary, corroborator],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GL_DIFF, versions=self._versions()),
            ),
        ):
            post_review.main()

        cap = self._payload()
        self.assertEqual(len(cap["discussions"]), 1)
        disc = cap["discussions"][0]
        self.assertEqual(disc["body"], render_group_body(primary, [corroborator]))
        self.assertEqual(disc["position"]["new_path"], "bar.py")
        self.assertEqual(disc["position"]["new_line"], 1)

    def test_unanchorable_primary_degrades_whole_group_into_skipped_section(self):
        primary, corroborator = self._findings()
        primary["line"] = 999  # not in the diff
        self._write(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": [primary, corroborator],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GL_DIFF, versions=self._versions()),
            ),
        ):
            post_review.main()

        cap = self._payload()
        self.assertEqual(cap["discussions"], [])
        body = cap["summary"]["body"]
        self.assertIn("could not be anchored inline", body)
        self.assertIn("Body A", body)
        self.assertIn("Body B", body)

    def test_no_line_primary_degrades_whole_group_into_skipped_section(self):
        """The primary carries no line at all (distinct from a line the diff
        doesn't touch) — the same whole-group degrade must fire on this branch
        too, not just the invalid-line branch."""
        primary, corroborator = self._findings()
        del primary["line"]
        self._write(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": [primary, corroborator],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GL_DIFF, versions=self._versions()),
            ),
        ):
            post_review.main()

        cap = self._payload()
        self.assertEqual(cap["discussions"], [])
        body = cap["summary"]["body"]
        self.assertIn("Body A", body)
        self.assertIn(
            "Body B", body, "the corroborator must fan out into the skipped section too"
        )


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
        self.assertEqual(position["new_path"], "src/app/clients/api/__init__.py")
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


class TestGitlabRealADirectoryPath(_DryRunTestBase):
    """A GitLab repo may contain a REAL top-level `a/` or `b/` directory.

    `glab mr diff` never writes git's synthetic prefixes, so stripping `^[ab]/` on the
    GitLab side — in the parser's header regexes AND unconditionally at the top of
    post_gitlab's loop — truncated `a/foo.py` to `foo.py`. Pre-branch main shipped the
    raw finding path and delivery worked; under the strip those findings 400 and get
    warn-skipped. Parser key and shipped position are asserted together, so reverting
    either half is red.
    """

    def test_gitlab_real_a_directory_path_is_preserved(self):
        with patch(
            "scripts.post_review.run_api", return_value=(GL_DIFF_REAL_A_DIR, "", 0)
        ):
            valid_lines, new_files, old_paths, _ = parse_diff_lines(
                "gitlab", "o", "r", 1
            )
        self.assertIn(("a/foo.py", 1), valid_lines)
        self.assertEqual(new_files, set())
        self.assertEqual(old_paths, {"a/foo.py": "a/foo.py"})

        self._write(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": [
                    {
                        "file": "a/foo.py",
                        "line": 1,
                        "severity": "high",
                        "title": "Finding in a real a/ directory",
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
                    diff=GL_DIFF_REAL_A_DIR, versions=GL_CONTRACT_VERSIONS
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            post_review.main()
        discussions = self._payload()["discussions"]
        self.assertEqual(len(discussions), 1, "the finding must not be warn-skipped")
        position = discussions[0]["position"]
        self.assertEqual(position["new_path"], "a/foo.py")
        self.assertEqual(position["old_path"], "a/foo.py")
        self.assertEqual(position["old_line"], 1)


class TestGitlabFindingPathNormalization(_DryRunTestBase):
    """A `b/`-prefixed finding path must ship UNPREFIXED in the position.

    `is_line_valid`/`old_line_for` strip the prefix for their lookups, so such a finding
    passed validation and got a correct `old_line` — but the position then shipped the
    raw `b/src/edited.py`, a path GitLab does not know. This now exercises the STRIPPED
    FALLBACK arm of `diff_path_spelling`: the raw spelling is not a diff key, the
    stripped one is, so the stripped one is what travels to the position, the lookup and
    the warnings alike.
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
# GitLab delivery outcomes, end-to-end through main()
# ---------------------------------------------------------------------------


class _GitlabLiveRunBase(_DryRunTestBase):
    """Drives post_review.main() over the GitLab contract fixtures.

    Carries no tests of its own — a subclass of a TestCase inherits its tests, and
    the classes below need the same runner for different subjects (the position gate,
    per-finding fault tolerance, and per-finding idempotency).
    """

    def _run_main(
        self,
        dry_run=False,
        findings=None,
        prior=None,
        sha="a" * 40,
        versions=None,
        **fake_run_kwargs,
    ):
        data = {
            "platform": "gitlab",
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "review_body": "MR review",
            "findings": GL_CONTRACT_FINDINGS if findings is None else findings,
        }
        # sha=None omits the key entirely, so resolve_marker_sha falls through to
        # `git rev-parse HEAD` — the only way to reach its "unknown" outcome.
        if sha is not None:
            data["sha"] = sha
        self._write(data)
        argv = ["post_review.py", self.findings_path]
        if dry_run:
            argv.append("--dry-run")
        stdout, stderr = io.StringIO(), io.StringIO()
        exit_code = None
        with (
            patch.object(sys, "argv", argv),
            patch.dict(os.environ, {}, clear=False),
            # The idempotency fetch defaults to "nothing delivered yet" so every run
            # reaches the per-finding loop; TestGitlabInlineDiscussionIdempotency
            # steers it to exercise the dedup gate.
            patch(
                "scripts.post_review.gitlab_prior_delivery_state",
                return_value=(False, set(), frozenset(), None)
                if prior is None
                else _normalize_prior(prior),
            ) as mock_prior,
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(
                    diff=GL_DIFF_CONTRACT,
                    versions=GL_CONTRACT_VERSIONS if versions is None else versions,
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
            mock_prior=mock_prior,
            out=stdout.getvalue(),
            err=stderr.getvalue(),
            exit_code=exit_code,
        )


class TestGitlabPositionGate(_GitlabLiveRunBase):
    """The gate on the real delivery path: same call in both modes, no exit before the
    dry-run payload is on disk."""

    def test_validate_position_is_called_once_per_delivered_finding(self):
        """With the guards intact, deleting the call site changes not one byte of the
        output — a call count is the only thing that catches its removal."""
        real = post_review.validate_position
        calls = []

        def spy(*args):
            calls.append(args)
            return real(*args)

        with patch("scripts.post_review.validate_position", side_effect=spy):
            run = self._run_main(dry_run=True)
        self.assertIsNone(run.exit_code)
        self.assertEqual(len(calls), len(GL_CONTRACT_FINDINGS))
        self.assertEqual(len(self._payload()["discussions"]), 3)

    def test_empty_sha_dies_before_the_summary_note(self):
        """A loop-invariant config failure is reported once, before anything reaches the
        MR — not as one rejection per finding after the note is already on it."""
        run = self._run_main(
            versions=[
                {
                    "base_commit_sha": "base1",
                    "head_commit_sha": "",
                    "start_commit_sha": "start1",
                }
            ]
        )
        self.assertEqual(run.exit_code, 1)
        self.assertEqual(_note_posts(run.mock_run), [])
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertIn("head_sha", run.err)

    def test_dry_run_malformed_position_exits_one_and_still_writes_the_payload(self):
        """A float line number reaches the position dict unchanged — the exact class of
        payload that used to be reported as "captured" and then 400 on the live run."""
        findings = [dict(GL_CONTRACT_FINDINGS[0], line=61.0)]
        run = self._run_main(dry_run=True, findings=findings)
        self.assertEqual(run.exit_code, 1)
        self.assertIn("malformed GitLab position", run.err)
        self.assertIn("new_line must be an integer", run.err)
        self.assertIn("  1 finding(s) had a malformed position", run.out)
        # Its own counter and its own line: the skip counter's meaning is pinned
        # elsewhere and must not absorb this.
        self.assertNotIn("finding(s) skipped.", run.out)
        self.assertIn("Dry run — no comments posted", run.out)
        payload = self._payload()
        self.assertEqual(payload["discussions"], [])
        self.assertTrue(
            any("malformed GitLab position" in w for w in payload["skipped"]),
            "the payload must say why the finding is absent from it",
        )

    def test_live_malformed_position_is_never_sent(self):
        """Live, a malformed position is a per-finding loss like a rejection: the sound
        findings still land, and the run stays a success with warnings. Inline
        discussions have no idempotency key, so exiting non-zero on a PARTIAL delivery
        would invite the rerun that double-posts everything that already landed."""
        findings = [dict(GL_CONTRACT_FINDINGS[0], line=61.0), GL_CONTRACT_FINDINGS[1]]
        run = self._run_main(findings=findings)
        self.assertIsNone(run.exit_code)
        posts = _discussion_posts(run.mock_run)
        self.assertEqual(len(posts), 1, "only the sound position may be posted")
        self.assertIn("  1 inline discussion(s) posted.", run.out)
        self.assertIn("  1 finding(s) had a malformed position", run.out)

    def test_malformed_position_loss_counts_the_whole_group_not_just_the_primary(self):
        """A consolidation group's loss counters must reflect every finding in the
        group, not just the primary that anchors the position — posted + skipped +
        invalid + failed + already_present must sum to the total finding count.

        The primary's loss is its own (1 invalid); each corroborator then falls
        back to its own individual discussion and is counted on its own merits.
        """
        primary = _gl_primary(line=61.0)  # malformed -> validate_position rejects
        run = self._run_main(
            dry_run=True,
            findings=[primary, _gl_corroborator("A", 61), _gl_corroborator("B", 61)],
        )
        # Dry-run reports a malformed position as a non-zero exit (pinned above);
        # what changed is the COUNT — only the primary is lost to it.
        self.assertEqual(run.exit_code, 1)
        self.assertIn("  1 finding(s) had a malformed position", run.out)
        self.assertIn("  2 inline discussion(s) captured.", run.out)

    def test_group_fallback_partial_position_failure(self):
        """A malformed primary position must not take its validated corroborators
        down with it — they fall back to their own individual discussions."""
        run = self._run_main(
            findings=[
                _gl_primary(line=61.0),
                _gl_corroborator("A", 61),
                _gl_corroborator("B", 62),
            ]
        )
        self.assertIsNone(run.exit_code)
        self.assertIn("  2 inline discussion(s) posted.", run.out)
        self.assertIn("  1 finding(s) had a malformed position", run.out)
        posts = _discussion_posts(run.mock_run)
        self.assertEqual(len(posts), 2, "one individual discussion per corroborator")

    def test_group_fallback_partial_post_failure(self):
        """A rejected GROUP discussion falls back to one individual discussion per
        corroborator: the primary counts 1 failed, the corroborators post on their
        own."""
        payloads = []
        run = self._run_main(
            findings=[
                _gl_primary(),
                _gl_corroborator("A", 61),
                _gl_corroborator("B", 62),
            ],
            discussion_rcs=[1, 0, 0],
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        self.assertIn("  2 inline discussion(s) posted.", run.out)
        self.assertIn("  1 inline discussion(s) rejected by GitLab", run.out)
        posts = _discussion_posts(run.mock_run)
        self.assertEqual(
            len(posts), 3, "one rejected group attempt + one POST per corroborator"
        )
        bodies = [p["body"] for p in payloads if "position" in p]
        self.assertEqual(len(bodies), 3)
        # The two fallback discussions each carry ONE finding, rendered by the
        # single-finding renderer — not the group body.
        self.assertIn("Corroborating finding", bodies[0])
        self.assertNotIn("Corroborating finding", bodies[1])
        self.assertNotIn("Corroborating finding", bodies[2])
        self.assertIn("Corroborator A", bodies[1])
        self.assertIn("Corroborator B", bodies[2])

    def test_group_fallback_total_failure(self):
        """When the primary AND every corroborator lose on their own merits, the
        losses still sum to the group size — nothing vanishes."""
        run = self._run_main(
            findings=[
                _gl_primary(line=61.0),  # invalid
                _gl_corroborator("A", 61),  # rejected below -> failed
                _gl_corroborator("B", 999),  # not in the diff -> invalid
            ],
            discussion_rcs=[1],
        )
        self.assertEqual(run.exit_code, 1)
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("  2 finding(s) had a malformed position", run.out)
        self.assertIn("  1 inline discussion(s) rejected by GitLab", run.out)

    def test_group_success_counts_every_member_as_posted(self):
        """One discussion carries the whole group, so the posted counter reports the
        group size, not 1."""
        run = self._run_main(findings=[_gl_primary(), _gl_corroborator("A", 61)])
        self.assertIsNone(run.exit_code)
        self.assertEqual(len(_discussion_posts(run.mock_run)), 1)
        self.assertIn("  2 inline discussion(s) posted.", run.out)

    def test_group_already_delivered_counts_every_member(self):
        """A group whose single discussion is already on the MR counts all of its
        findings as already-present — one discussion, group_size findings."""
        primary = _gl_primary()
        corroborator = _gl_corroborator("A", 61)
        run = self._run_main(
            findings=[primary, corroborator],
            prior=(True, {_member_key(primary), _member_key(corroborator)}, None),
        )
        self.assertIsNone(run.exit_code)
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertIn(
            "  2 inline discussion(s) already on the MR from an earlier run", run.out
        )

    def test_group_rerun_after_full_delivery_posts_nothing(self):
        """Every member's key is what a delivered group leaves behind, so a rerun
        recognizes all of them and issues no discussion POST at all."""
        primary = _gl_primary()
        corrs = [_gl_corroborator("A", 61), _gl_corroborator("B", 62)]
        keys = {_member_key(m) for m in [primary, *corrs]}
        run = self._run_main(findings=[primary, *corrs], prior=(True, keys, None))
        self.assertIsNone(run.exit_code)
        self.assertEqual(len(_discussion_posts(run.mock_run)), 0)
        self.assertIn(
            "  3 inline discussion(s) already on the MR from an earlier run", run.out
        )

    def test_group_partial_prior_delivery_posts_only_missing_members(self):
        """A prior run's fallback landed the corroborators individually. Reposting the
        GROUP would duplicate them, so only the missing primary is delivered — on its
        own, with the single-finding body."""
        primary = _gl_primary()
        corrs = [_gl_corroborator("A", 61), _gl_corroborator("B", 62)]
        payloads = []
        run = self._run_main(
            findings=[primary, *corrs],
            prior=(True, {_member_key(c) for c in corrs}, None),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        posts = _discussion_posts(run.mock_run)
        self.assertEqual(len(posts), 1, "only the undelivered primary may be posted")
        self.assertIn("  1 inline discussion(s) posted.", run.out)
        self.assertIn(
            "  2 inline discussion(s) already on the MR from an earlier run", run.out
        )
        body = next(p["body"] for p in payloads if "position" in p)
        self.assertNotIn("Corroborating finding", body)
        self.assertIn(
            post_review.build_finding_marker("a" * 40, _member_key(primary)), body
        )

    def test_group_body_carries_a_marker_for_every_member(self):
        """The group's single discussion is the delivery record for all of its
        findings, so it carries one finding-key marker per member — a later rerun
        matches each member individually, whatever shape delivered it."""
        primary = _gl_primary()
        corrs = [_gl_corroborator("A", 61), _gl_corroborator("B", 62)]
        payloads = []
        run = self._run_main(findings=[primary, *corrs], payloads=payloads)
        self.assertIsNone(run.exit_code)
        body = next(p["body"] for p in payloads if "position" in p)
        for member in [primary, *corrs]:
            self.assertIn(
                post_review.build_finding_marker("a" * 40, _member_key(member)),
                body,
                f"missing marker for {member['title']}",
            )

    def test_group_body_carries_a_marker_for_unanchorable_corroborator_too(self):
        """A corroborator with no line of its own can only ever be delivered inside
        the group body — its marker must be there too, or a rerun can never
        recognize it as delivered (unanchored corroborators lost on rerun)."""
        primary = _gl_primary()
        unanchored = _gl_corroborator("A", None)
        payloads = []
        run = self._run_main(findings=[primary, unanchored], payloads=payloads)
        self.assertIsNone(run.exit_code)
        body = next(p["body"] for p in payloads if "position" in p)
        self.assertIn(
            post_review.build_finding_marker("a" * 40, _member_key(unanchored)),
            body,
            "the unanchorable corroborator's marker must round-trip through the "
            "group body",
        )

    def test_rerun_recognizes_unanchorable_corroborator_from_group_body(self):
        """Given the round-trip above, a rerun that sees both markers on the MR
        must treat the WHOLE group — unanchorable member included — as already
        delivered, and post nothing new."""
        primary = _gl_primary()
        unanchored = _gl_corroborator("A", None)
        keys = {_member_key(primary), _member_key(unanchored)}
        run = self._run_main(findings=[primary, unanchored], prior=(True, keys, None))
        self.assertIsNone(run.exit_code)
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertIn(
            "  2 inline discussion(s) already on the MR from an earlier run",
            run.out,
        )

    def test_unanchorable_corroborator_delivered_when_siblings_already_posted(self):
        """The primary was delivered individually by an earlier fallback run; the
        unanchorable corroborator was not (it has no anchor of its own, so it
        never got its own fallback discussion). It must still reach the MR —
        as a position-less note — and count toward delivery rather than being
        silently dropped as `already_present`."""
        primary = _gl_primary()
        unanchored = _gl_corroborator("A", None)
        payloads = []
        run = self._run_main(
            findings=[primary, unanchored],
            prior=(True, {_member_key(primary)}, None),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        note_bodies = [p["body"] for p in payloads if "position" not in p]
        self.assertTrue(
            any("Corroborator A" in b for b in note_bodies),
            "the unanchorable corroborator's content must land on the MR "
            "somewhere, not vanish",
        )
        self.assertIn(
            post_review.build_finding_marker("a" * 40, _member_key(unanchored)),
            "".join(note_bodies),
        )
        self.assertIn("  1 inline discussion(s) already on the MR", run.out)
        self.assertIn("  1 inline discussion(s) posted.", run.out)

    def test_legacy_group_body_rerun_posts_nothing_and_counts_the_whole_group(self):
        """A pre-#208 group body already carries the unanchorable corroborator's
        CONTENT (rendered into its corroboration section) even though it never
        carried that corroborator's KEY. A rerun must recognize the primary's
        key landing in such a body as proof the whole group is delivered — not
        attempt to `deliver_unanchored` a duplicate (Bugbot: rerun duplicates
        prior group content)."""
        primary = _gl_primary()
        unanchored = _gl_corroborator("A", None)
        payloads = []
        run = self._run_main(
            findings=[primary, unanchored],
            # legacy_group_keys carries the primary's key: detect_prior_review
            # decided this is a legacy under-marked group body. delivered_keys
            # carries ONLY the primary's key too — the unanchorable member's
            # key was never written by the pre-fix code that posted this note.
            prior=(True, {_member_key(primary)}, {_member_key(primary)}, None),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        self.assertEqual(_discussion_posts(run.mock_run), [])
        note_bodies = [p["body"] for p in payloads if "position" not in p]
        self.assertFalse(
            any("Corroborator A" in b for b in note_bodies),
            "the legacy group body already carries this content — posting it "
            "again would duplicate it on the MR",
        )
        self.assertIn(
            "  2 inline discussion(s) already on the MR from an earlier run",
            run.out,
        )

    def test_non_legacy_partial_delivery_still_delivers_the_missing_member(self):
        """Pin that legacy-group detection does NOT over-fire: an ordinary
        partial delivery (anchored siblings posted individually, no legacy
        group body involved) must still deliver the member that was left
        behind, exactly as before."""
        primary = _gl_primary()
        unanchored = _gl_corroborator("A", None)
        payloads = []
        run = self._run_main(
            findings=[primary, unanchored],
            prior=(True, {_member_key(primary)}, set(), None),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        note_bodies = [p["body"] for p in payloads if "position" not in p]
        self.assertTrue(
            any("Corroborator A" in b for b in note_bodies),
            "with no legacy group body on the MR, the missing member must "
            "still be delivered",
        )

    def test_live_all_malformed_exits_one_with_nothing_posted(self):
        findings = [dict(f, line=float(f["line"])) for f in GL_CONTRACT_FINDINGS]
        run = self._run_main(findings=findings)
        self.assertEqual(run.exit_code, 1)
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("nothing new was posted inline", run.err)


# ---------------------------------------------------------------------------
# GitLab per-finding fault tolerance (issue #127 D3)
# ---------------------------------------------------------------------------


class TestGitlabFaultTolerance(_GitlabLiveRunBase):
    """A single rejected position must not strand the findings behind it.

    The summary note is posted FIRST, so aborting mid-loop left partial,
    non-retryable state on the MR. The loop now warns, counts and continues; the run
    only exits non-zero when nothing NEW reached the MR and something was rejected.
    """

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

    ``scripts.post_review.gitlab_prior_delivery_state`` — the name bound INTO this
    module — is what these tests patch. ``post_review`` imports the bare
    ``detect_prior_review`` while ``tests/test_detect_prior_review.py`` imports
    ``scripts.detect_prior_review``: two distinct module objects in one pytest process,
    so patching the other one would not be seen here.
    """

    def _run_main(self, prior, data=None, dry_run=False, head_sha="deadbeefcafe\n"):
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
                "scripts.post_review.gitlab_prior_delivery_state",
                return_value=_normalize_prior(prior),
            ) as mock_prior,
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
            mock_prior=mock_prior,
            mock_run=mock_run,
            out=stdout.getvalue(),
            err=stderr.getvalue(),
        )

    def test_summary_skipped_when_this_shas_marker_is_already_on_the_mr(self):
        run = self._run_main(prior=(True, set(), None))
        self.assertEqual(_note_posts(run.mock_run), [])
        # The retry still delivers the inline comments — that is the whole point.
        self.assertEqual(len(_discussion_posts(run.mock_run)), 3)
        self.assertIn("already on the MR", run.out)
        self.assertNotIn("MR summary note posted.", run.out)
        run.mock_prior.assert_called_once_with("o", "r", 5, "a" * 40)

    def test_summary_posted_when_the_marker_records_a_different_sha(self):
        run = self._run_main(prior=(False, set(), None))
        self.assertEqual(len(_note_posts(run.mock_run)), 1)
        self.assertIn("MR summary note posted.", run.out)

    def test_one_fetch_serves_both_idempotency_checks(self):
        """Issue #132: the summary check and the finding-key set come from ONE fetch.
        A second round trip would also be a second, possibly inconsistent view of the
        MR — one where the summary is already there but the discussions are not."""
        run = self._run_main(prior=(True, set(), None))
        self.assertEqual(run.mock_prior.call_count, 1)

    def test_notes_fetch_failure_degrades_to_posting(self):
        run = self._run_main(
            prior=(False, set(), "gitlab notes: fetch failed (exit 1): boom")
        )
        self.assertEqual(len(_note_posts(run.mock_run)), 1)
        self.assertIn("could not check for an existing summary note", run.err)
        self.assertIn("boom", run.err)

    def test_dry_run_makes_no_idempotency_call_and_always_captures_the_summary(self):
        """The hard "no network in dry-run" pin: the check would say "skip" if it were
        consulted, and build_dry_run_payload's "first capture is the summary" shape
        depends on the note being captured regardless."""
        run = self._run_main(prior=(True, set(), None), dry_run=True)
        run.mock_prior.assert_not_called()
        self.assertIn("code-gauntlet-findings:", self._payload()["summary"]["body"])
        self.assertEqual(
            post_review._CAPTURED[0]["payload"]["body"],
            self._payload()["summary"]["body"],
        )

    def test_unresolvable_sha_skips_the_check_and_posts(self):
        """get_head_sha's "unknown" fallback is not a usable dedup key."""
        run = self._run_main(
            prior=(True, set(), None),
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
        run.mock_prior.assert_not_called()
        self.assertEqual(len(_note_posts(run.mock_run)), 1)

    def test_summary_check_delegates_to_the_reader_module(self):
        """post_review must not grow its own parse of the signals it writes."""
        self.assertEqual(
            post_review.gitlab_prior_delivery_state.__module__, "detect_prior_review"
        )


# ---------------------------------------------------------------------------
# GitLab per-finding delivery idempotency (issue #132)
# ---------------------------------------------------------------------------


class TestGitlabInlineDiscussionIdempotency(_GitlabLiveRunBase):
    """A rerun after a partial delivery must not duplicate the inline discussions
    that DID land — issue #132, the half issue #127 D4 left open for the summary.

    The expected keys below are LITERAL constants, computed once and hardcoded.
    Deriving them in the assertions by calling ``post_review.finding_key`` would make
    every test here agree with the implementation by construction — including a
    broken implementation that keys every finding identically.
    """

    # GL_CONTRACT_FINDINGS, in order: src/edited.py:61, src/edited.py:62, and line 1 of
    # the added file, whose path is whatever `added.diff` records — a key is over the
    # path, so re-recording that fixture re-pins these literals.
    CONTEXT_LINE_KEY = "f87d51ec25846a5e"
    ADDED_LINE_KEY = "ee15b1fc2a6db296"
    NEW_FILE_KEY = "a9cb7253f6710b82"
    ALL_KEYS: ClassVar[set[str]] = {CONTEXT_LINE_KEY, ADDED_LINE_KEY, NEW_FILE_KEY}

    @staticmethod
    def _discussion_payloads(payloads):
        return [p for p in payloads if "position" in p]

    def test_keys_match_their_pinned_literals(self):
        """The tautology guard itself: the derivation must reproduce hardcoded
        values, and the three findings must not collide onto one key."""
        keys = [
            post_review.finding_key(
                f["file"], f["line"], f["title"], render_comment_body(f)
            )
            for f in GL_CONTRACT_FINDINGS
        ]
        self.assertEqual(
            keys, [self.CONTEXT_LINE_KEY, self.ADDED_LINE_KEY, self.NEW_FILE_KEY]
        )

    def test_a_finding_already_on_the_mr_is_not_reposted(self):
        payloads = []
        run = self._run_main(
            prior=(True, {self.CONTEXT_LINE_KEY}, None), payloads=payloads
        )
        self.assertIsNone(run.exit_code)
        self.assertEqual(len(_discussion_posts(run.mock_run)), 2)
        bodies = [p["body"] for p in self._discussion_payloads(payloads)]
        self.assertEqual(len(bodies), 2)
        self.assertNotIn(
            "Context-line finding",
            "\n".join(bodies),
            "the finding whose key is already on the MR must not be reposted",
        )
        self.assertIn("  2 inline discussion(s) posted.", run.out)
        self.assertIn("1 inline discussion(s) already on the MR", run.out)
        self.assertNotIn("rejected", run.out)

    def test_rerun_with_everything_already_present_posts_nothing_and_exits_zero(self):
        run = self._run_main(prior=(True, set(self.ALL_KEYS), None))
        self.assertIsNone(run.exit_code, "a fully-delivered rerun is a success")
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertEqual(_note_posts(run.mock_run), [])
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("3 inline discussion(s) already on the MR", run.out)

    def test_already_present_plus_one_rejection_fails_honestly(self):
        """The bare ``posted == 0`` die used to call this "all 1 inline discussion(s)
        were rejected — nothing was posted inline", which was wrong twice: one
        rejection out of three findings is not "all", and two of this review's
        discussions ARE on the MR."""
        run = self._run_main(
            prior=(True, {self.CONTEXT_LINE_KEY, self.ADDED_LINE_KEY}, None),
            discussion_rcs=[1],
        )
        self.assertEqual(run.exit_code, 1)
        self.assertIn("attempted this run were rejected", run.err)
        self.assertIn("2 from an earlier run remain on the MR", run.err)
        self.assertNotIn("nothing was posted inline", run.err)

    def test_already_present_plus_one_malformed_fails_honestly(self):
        """The malformed-position exit reports the same outcome as the rejection exit
        above — nothing NEW landed — so it owes the operator the same true statement
        about what an earlier run left standing. A malformed position is caught before
        the wire, so it is never one of the "attempted" discussions."""
        run = self._run_main(
            findings=[
                GL_CONTRACT_FINDINGS[0],
                GL_CONTRACT_FINDINGS[1],
                dict(GL_CONTRACT_FINDINGS[2], line=1.0),
            ],
            prior=(True, {self.CONTEXT_LINE_KEY, self.ADDED_LINE_KEY}, None),
        )
        self.assertEqual(run.exit_code, 1)
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertIn("had a malformed position", run.err)
        self.assertIn("2 from an earlier run remain on the MR", run.err)
        self.assertNotIn("nothing was posted inline", run.err)

    def test_fetch_failure_delivers_every_finding(self):
        """Availability over dedup: a failed read must never be taken for "already
        delivered" — a possible duplicate beats a silently dropped review."""
        run = self._run_main(
            prior=(False, set(), "gitlab notes: fetch failed (exit 1): boom")
        )
        self.assertIsNone(run.exit_code)
        self.assertEqual(len(_discussion_posts(run.mock_run)), 3)
        self.assertEqual(len(_note_posts(run.mock_run)), 1)
        self.assertIn("could not check for an existing summary note", run.err)
        self.assertIn("boom", run.err)

    def test_live_posted_bodies_carry_a_marker_the_reader_recovers(self):
        """The round trip that makes the rerun possible: what the live wire carries
        must parse back to the same key, through the real writer and real reader."""
        payloads = []
        run = self._run_main(payloads=payloads)
        self.assertIsNone(run.exit_code)
        bodies = [p["body"] for p in self._discussion_payloads(payloads)]
        self.assertEqual(len(bodies), 3)
        for body, finding, key in zip(
            bodies,
            GL_CONTRACT_FINDINGS,
            (self.CONTEXT_LINE_KEY, self.ADDED_LINE_KEY, self.NEW_FILE_KEY),
            strict=True,
        ):
            self.assertTrue(
                body.startswith(render_comment_body(finding)),
                "the marker is appended — the rendered comment is untouched",
            )
            self.assertEqual(
                review_marker.find_finding_marker(body),
                {"sha": "a" * 40, "key": key},
            )

    def test_key_is_derived_from_the_diff_spelling_not_the_raw_finding_path(self):
        """The key must be built from the path the position ships. A finding whose
        raw path needs resolving must land on the SAME key as its resolved twin —
        otherwise the marker written on the wire and the marker a rerun looks up
        drift apart the moment a `b/`-prefixed path appears."""
        payloads = []
        prefixed = dict(GL_CONTRACT_FINDINGS[0], file="b/src/edited.py")
        run = self._run_main(findings=[prefixed], payloads=payloads)
        self.assertIsNone(run.exit_code)
        discussion = self._discussion_payloads(payloads)[0]
        self.assertEqual(discussion["position"]["new_path"], "src/edited.py")
        self.assertEqual(
            review_marker.find_finding_marker(discussion["body"]),
            {"sha": "a" * 40, "key": self.CONTEXT_LINE_KEY},
        )

    def test_an_unmarkable_sha_posts_without_writing_an_unreadable_marker(self):
        """`git rev-parse` failing yields "unknown", which find_finding_marker is
        guaranteed to reject. Delivery still happens — it just carries no marker,
        rather than a permanent one nothing can read."""
        payloads = []
        run = self._run_main(sha=None, head_sha="unknown\n", payloads=payloads)
        self.assertIsNone(run.exit_code)
        run.mock_prior.assert_not_called()
        bodies = [p["body"] for p in self._discussion_payloads(payloads)]
        self.assertEqual(len(bodies), 3)
        for body, finding in zip(bodies, GL_CONTRACT_FINDINGS, strict=True):
            self.assertEqual(body, render_comment_body(finding))

    def test_dry_run_fetches_nothing_captures_everything_and_stays_marker_free(self):
        """bench pins dry-run and scores the captured bodies as candidate text, so a
        marker in a capture would change what is scored. The capture must also ignore
        the dedup state entirely — every finding is captured, none deduped away."""
        run = self._run_main(dry_run=True, prior=(True, set(self.ALL_KEYS), None))
        run.mock_prior.assert_not_called()
        captured = self._payload()
        self.assertEqual(len(captured["discussions"]), 3)
        for disc, finding in zip(
            captured["discussions"], GL_CONTRACT_FINDINGS, strict=True
        ):
            self.assertEqual(disc["body"], render_comment_body(finding))
        self.assertNotIn(
            review_marker.FINDING_MARKER_TOKEN,
            json.dumps(captured),
            "no dry-run capture may carry the delivery marker",
        )


# ---------------------------------------------------------------------------
# Issue #192 — skipped findings degrade into the review body, they are never
# silently dropped.
# ---------------------------------------------------------------------------


class TestBuildSkippedSection(unittest.TestCase):
    def test_empty_list_returns_empty_string(self):
        self.assertEqual(build_skipped_section([], 0), "")

    def test_renders_location_title_and_both_counts(self):
        finding = {
            "file": "src/app.py",
            "line": 216,
            "severity": "high",
            "title": "SQL injection risk",
            "body": "Untrusted input reaches the query.",
        }
        section = build_skipped_section([("src/app.py", 216, finding)], 4)
        self.assertIn("### ⚠️ 1 finding(s) could not be anchored inline", section)
        self.assertIn("4 inline comment(s) were posted", section)
        self.assertIn("following 1 finding(s)", section)
        self.assertIn("`src/app.py:216`", section)
        self.assertIn("SQL injection risk", section)
        self.assertIn(render_comment_body(finding), section)

    def test_no_line_finding_renders_bare_path(self):
        finding = {"file": "src/app.py", "title": "No line", "body": "b"}
        section = build_skipped_section([("src/app.py", None, finding)], 0)
        self.assertIn("`src/app.py`", section)
        self.assertNotIn("src/app.py:None", section)

    def test_reuses_render_comment_body_for_redaction(self):
        """The section must go through the SAME sanitize/redact path as an inline
        comment — a second rendering path is exactly the drift this guards against."""
        finding = {
            "file": "src/app.py",
            "line": 5,
            "title": "Leaked token",
            "body": "b",
            "suggestion": "Rotate the token: ghp_" + "a" * 36,
        }
        section = build_skipped_section([("src/app.py", 5, finding)], 0)
        self.assertIn("[REDACTED]", section)
        self.assertNotIn("ghp_" + "a" * 36, section)


class TestGitHubSkippedFindingsDegrade(_DryRunTestBase):
    def _findings(self):
        inline = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "Inline bug",
            "body": "Body A",
        }
        off_diff = {
            "file": "foo.py",
            "line": 99,
            "severity": "medium",
            "title": "Off-diff bug",
            "body": "Body B",
        }
        return inline, off_diff

    def test_skipped_finding_lands_in_body_with_both_counts(self):
        inline, off_diff = self._findings()
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [inline, off_diff],
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
        body = cap["payload"]["body"]
        self.assertIn("### ⚠️ 1 finding(s) could not be anchored inline", body)
        self.assertIn("1 inline comment(s) were posted", body)
        self.assertIn("Off-diff bug", body)
        self.assertIn("foo.py:99", body)
        # Excluded from the inline comments payload.
        comment_bodies = [c["body"] for c in cap["payload"]["comments"]]
        self.assertNotIn(render_comment_body(off_diff), comment_bodies)
        self.assertEqual(len(cap["payload"]["comments"]), 1)
        # Footer stays last and intact.
        self.assertTrue(body.rstrip().endswith("-->"))
        self.assertIn(review_marker.MARKER_TOKEN, body)

    def test_no_skips_body_unchanged(self):
        inline, _ = self._findings()
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [inline],
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
        self.assertNotIn("could not be anchored inline", body)

    def test_no_line_finding_degrades_others_still_post(self):
        """post_github must mirror post_gitlab: a finding with no ``line`` key
        (bare `f["line"]` subscript would raise KeyError and abort the whole run,
        losing every finding) degrades into the skipped section instead."""
        inline, _ = self._findings()
        no_line = {"file": "foo.py", "title": "No-line bug", "body": "Body C"}
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [inline, no_line],
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
            post_review.main()  # must not raise

        cap = self._payload()
        body = cap["payload"]["body"]
        self.assertIn("### ⚠️ 1 finding(s) could not be anchored inline", body)
        self.assertIn("No-line bug", body)
        self.assertIn("`foo.py`", body)
        self.assertEqual(len(cap["payload"]["comments"]), 1)

    def test_no_line_no_file_finding_renders_placeholder_no_raise(self):
        inline, _ = self._findings()
        mystery = {"title": "Mystery bug", "body": "Body D"}
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [inline, mystery],
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
            post_review.main()  # must not raise

        body = self._payload()["payload"]["body"]
        self.assertIn("`?`", body)
        self.assertIn("Mystery bug", body)


# A five-line hunk so a multi-line comment's end_line can land either inside or
# outside the same hunk as its (already-valid) start line.
GH_DIFF_MULTILINE = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,5 @@\n"
    " existing\n"
    "+added2\n"
    "+added3\n"
    "+added4\n"
    "+added5\n"
)


class TestGitHubMultiLineRangeValidation(_DryRunTestBase):
    """Issue #192 follow-up: a live run 422'd because ``end_line`` was never
    validated — GitHub rejects the WHOLE review POST when a multi-line comment's
    range crosses out of the diff's hunk, even though the start line was valid."""

    def _finding(self, line, end_line):
        return {
            "file": "foo.py",
            "line": line,
            "end_line": end_line,
            "severity": "high",
            "title": "Range bug",
            "body": "Body",
        }

    def _post(self, finding):
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [finding],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF_MULTILINE),
            ),
        ):
            post_review.main()
        return self._payload()["payload"]["comments"][0]

    def test_end_line_outside_the_diff_falls_back_to_single_line(self):
        comment = self._post(self._finding(line=2, end_line=940))
        self.assertNotIn("start_line", comment)
        self.assertNotIn("start_side", comment)
        self.assertEqual(comment["line"], 2)

    def test_end_line_inside_the_same_hunk_preserves_the_range(self):
        comment = self._post(self._finding(line=2, end_line=4))
        self.assertEqual(comment["start_line"], 2)
        self.assertEqual(comment["start_side"], "RIGHT")
        self.assertEqual(comment["line"], 4)

    def test_validation_skipped_passes_the_range_through_unchanged(self):
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "findings": [self._finding(line=2, end_line=9999)],
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.parse_diff_lines",
                return_value=(None, None, None, None),
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=GH_DIFF_MULTILINE),
            ),
        ):
            post_review.main()
        comment = self._payload()["payload"]["comments"][0]
        self.assertEqual(comment["start_line"], 2)
        self.assertEqual(comment["line"], 9999)


class TestGitlabSkippedFindingsDegrade(_GitlabLiveRunBase):
    def _summary_note_body(self, payloads):
        notes = [p for p in payloads if "position" not in p]
        self.assertEqual(len(notes), 1)
        return notes[0]["body"]

    def test_skipped_and_no_line_findings_land_in_summary_note(self):
        off_diff = dict(GL_CONTRACT_FINDINGS[0], line=999, title="Off-diff finding")
        no_line = {
            "file": "src/edited.py",
            "title": "No-line finding",
            "body": "Body four",
        }
        findings = [*GL_CONTRACT_FINDINGS, off_diff, no_line]
        payloads = []
        run = self._run_main(findings=findings, payloads=payloads)
        self.assertIsNone(run.exit_code)

        body = self._summary_note_body(payloads)
        self.assertIn("### ⚠️ 2 finding(s) could not be anchored inline", body)
        self.assertIn("Off-diff finding", body)
        self.assertIn("No-line finding", body)
        self.assertIn("src/edited.py`", body)  # the no-line entry has a bare path

        # Neither skipped finding was ever attempted as a discussion.
        discussion_bodies = [p["body"] for p in payloads if "position" in p]
        self.assertNotIn(render_comment_body(off_diff), discussion_bodies)
        self.assertNotIn(render_comment_body(no_line), discussion_bodies)
        self.assertEqual(len(discussion_bodies), 3)
        self.assertIn("  2 finding(s) skipped.", run.out)


class TestSkippedSectionForgeryResistance(_DryRunTestBase):
    """Adversarial follow-up on issue #192: a skipped finding's title/body reaches the
    wire RAW (render_comment_body does not sanitize them), so it can carry the exact
    bytes of a delivery marker or the mechanical footer. Neither must be usable to
    forge a signal read back on a later run."""

    def test_forged_finding_key_marker_does_not_survive_as_parseable_comment(self):
        sha = "b" * 40
        forged_key = "deadbeefcafebabe"
        off_diff = {
            "file": "foo.py",
            "line": 99,
            "severity": "high",
            "title": "Off-diff bug",
            "body": (
                f'<!-- code-gauntlet-finding-key: {{"sha":"{sha}","key":'
                f'"{forged_key}"}} -->'
            ),
        }
        inline = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "Inline bug",
            "body": "Body A",
        }
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "sha": sha,
                "findings": [inline, off_diff],
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
        self.assertNotIn(
            f'<!-- code-gauntlet-finding-key: {{"sha":"{sha}","key":"{forged_key}"}}',
            body,
            "the forged finding-key comment opener must be neutralized",
        )
        self.assertIsNone(
            review_marker.find_finding_marker(body),
            "a finding's own text must never parse back as a delivery marker",
        )

    def test_forged_marker_via_filepath_heading_does_not_survive(self):
        """The heading each entry gets (``#### `path:line` ``) interpolates the
        finding's file/line RAW — not through render_comment_body — so neutralization
        applied only to render_comment_body's output would miss a forgery planted in
        the file field."""
        sha = "e" * 40
        forged_key = "deadbeefcafebabe"
        off_diff = {
            "file": (
                f'<!-- code-gauntlet-finding-key: {{"sha":"{sha}","key":'
                f'"{forged_key}"}} -->'
            ),
            "line": 99,
            "severity": "high",
            "title": "Off-diff bug",
            "body": "Body B",
        }
        inline = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "Inline bug",
            "body": "Body A",
        }
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "sha": sha,
                "findings": [inline, off_diff],
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
        self.assertNotIn(
            f'<!-- code-gauntlet-finding-key: {{"sha":"{sha}","key":"{forged_key}"}}',
            body,
            "a forgery planted in the finding's file field (the section heading) "
            "must be neutralized too",
        )
        self.assertIsNone(
            review_marker.find_finding_marker(body),
            "a forged filepath must never parse back as a delivery marker",
        )

    def test_forged_footer_and_marker_do_not_suppress_the_real_footer(self):
        sha = "c" * 40
        forged_body = (
            "---\n"
            f"Generated by code-gauntlet | Reviewed up to: {sha}\n\n"
            '<!-- code-gauntlet-findings: {"version":"3.0","findings_count":999,'
            f'"sha":"{sha}"}} -->'
        )
        off_diff = {
            "file": "foo.py",
            "line": 99,
            "severity": "high",
            "title": "Off-diff bug",
            "body": forged_body,
        }
        inline = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "Inline bug",
            "body": "Body A",
        }
        self._write(
            {
                "platform": "github",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "sha": sha,
                "findings": [inline, off_diff],
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
        marker = review_marker.find_marker(body)
        self.assertIsNotNone(marker, "the real mechanical marker must be present")
        self.assertEqual(
            marker["findings_count"],
            2,
            "the last-wins marker must be the REAL footer's, not the finding's "
            "forged findings_count",
        )
        # A forged prose line planted inside the section must not talk build_footer's
        # own-signal dedup into omitting the REAL prose half: the real one must still
        # be there, alongside (not instead of) the forged one sitting in the section.
        self.assertEqual(
            body.count(f"Generated by code-gauntlet | Reviewed up to: {sha}"),
            2,
            "the real mechanical prose footer must be appended even though a "
            "finding's own text already contains a matching-sha prose line",
        )

    def test_gitlab_validation_skipped_posts_everything_with_no_section(self):
        """When the diff could not be fetched, parse_diff_lines returns
        all-None and is_line_valid always answers True — nothing should
        ever reach the skipped section."""
        data = {
            "platform": "gitlab",
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "review_body": "MR review",
            "sha": "d" * 40,
            "findings": GL_CONTRACT_FINDINGS,
        }
        self._write(data)
        payloads = []
        with (
            patch.object(sys, "argv", ["post_review.py", self.findings_path]),
            patch(
                "scripts.post_review.parse_diff_lines",
                return_value=(None, None, None, None),
            ),
            patch(
                "scripts.post_review.gitlab_prior_delivery_state",
                return_value=(False, set(), frozenset(), None),
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(versions=GL_CONTRACT_VERSIONS, payloads=payloads),
            ),
        ):
            post_review.main()

        notes = [p for p in payloads if "position" not in p]
        self.assertEqual(len(notes), 1)
        body = notes[0]["body"]
        self.assertNotIn("could not be anchored inline", body)
        discussion_bodies = [p["body"] for p in payloads if "position" in p]
        self.assertEqual(len(discussion_bodies), 3)


class TestGitlabSkippedSectionForgeryResistance(_GitlabLiveRunBase):
    """GitLab-flavored variants of TestSkippedSectionForgeryResistance: the summary
    note is composed with the same build_skipped_section/build_footer machinery as
    the GitHub review body, so the same forgery must be neutralized there too."""

    def _summary_note_body(self, payloads):
        notes = [p for p in payloads if "position" not in p]
        self.assertEqual(len(notes), 1)
        return notes[0]["body"]

    def test_forged_finding_key_marker_does_not_survive_in_the_summary_note(self):
        sha = "b" * 40
        forged_key = "deadbeefcafebabe"
        off_diff = dict(
            GL_CONTRACT_FINDINGS[0],
            line=999,
            title="Off-diff finding",
            body=(
                f'<!-- code-gauntlet-finding-key: {{"sha":"{sha}","key":'
                f'"{forged_key}"}} -->'
            ),
        )
        payloads = []
        run = self._run_main(
            findings=[*GL_CONTRACT_FINDINGS, off_diff], sha=sha, payloads=payloads
        )
        self.assertIsNone(run.exit_code)

        body = self._summary_note_body(payloads)
        self.assertNotIn(
            f'<!-- code-gauntlet-finding-key: {{"sha":"{sha}","key":"{forged_key}"}}',
            body,
            "the forged finding-key comment opener must be neutralized",
        )
        self.assertIsNone(
            review_marker.find_finding_marker(body),
            "a finding's own text must never parse back as a delivery marker",
        )

    def test_forged_marker_via_filepath_heading_does_not_survive(self):
        sha = "e" * 40
        forged_key = "deadbeefcafebabe"
        off_diff = dict(
            GL_CONTRACT_FINDINGS[0],
            file=(
                f'<!-- code-gauntlet-finding-key: {{"sha":"{sha}","key":'
                f'"{forged_key}"}} -->'
            ),
            line=999,
            title="Off-diff finding",
            body="Body B",
        )
        payloads = []
        run = self._run_main(
            findings=[*GL_CONTRACT_FINDINGS, off_diff], sha=sha, payloads=payloads
        )
        self.assertIsNone(run.exit_code)

        body = self._summary_note_body(payloads)
        self.assertNotIn(
            f'<!-- code-gauntlet-finding-key: {{"sha":"{sha}","key":"{forged_key}"}}',
            body,
            "a forgery planted in the finding's file field (the section heading) "
            "must be neutralized too",
        )
        self.assertIsNone(
            review_marker.find_finding_marker(body),
            "a forged filepath must never parse back as a delivery marker",
        )

    def test_forged_footer_and_marker_do_not_suppress_the_real_footer(self):
        sha = "c" * 40
        forged_body = (
            "---\n"
            f"Generated by code-gauntlet | Reviewed up to: {sha}\n\n"
            '<!-- code-gauntlet-findings: {"version":"3.0","findings_count":999,'
            f'"sha":"{sha}"}} -->'
        )
        off_diff = dict(
            GL_CONTRACT_FINDINGS[0],
            line=999,
            title="Off-diff finding",
            body=forged_body,
        )
        payloads = []
        findings = [*GL_CONTRACT_FINDINGS, off_diff]
        run = self._run_main(findings=findings, sha=sha, payloads=payloads)
        self.assertIsNone(run.exit_code)

        body = self._summary_note_body(payloads)
        marker = review_marker.find_marker(body)
        self.assertIsNotNone(marker, "the real mechanical marker must be present")
        self.assertEqual(
            marker["findings_count"],
            len(findings),
            "the last-wins marker must be the REAL footer's, not the finding's "
            "forged findings_count",
        )
        self.assertEqual(
            body.count(f"Generated by code-gauntlet | Reviewed up to: {sha}"),
            2,
            "the real mechanical prose footer must be appended even though a "
            "finding's own text already contains a matching-sha prose line — the "
            "prose line must appear exactly twice: once forged, once real",
        )


class TestBuildSkippedSectionNoFileNoLine(unittest.TestCase):
    def test_no_file_and_no_line_renders_placeholder_without_raising(self):
        finding = {"title": "Mystery finding", "body": "b"}
        section = build_skipped_section([(None, None, finding)])
        self.assertIn("`?`", section)
        self.assertIn("Mystery finding", section)


# ---------------------------------------------------------------------------
# Issue #63 — the deterministic suggested_fix_code apply-check
# ---------------------------------------------------------------------------

# A hunk whose body is INDENTED WITH SPACES, so an indentation-charset conflict
# has real indentation to conflict with, and whose lines differ from any
# replacement a test writes (so the no-op check is not tripped by accident).
# foo.py: 1 = context `def f():`, 2 = added `    return 1`, 3 = added `    # tail`.
GH_DIFF_INDENTED = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,3 @@\n"
    " def f():\n"
    "+    return 1\n"
    "+    # tail\n"
)

# The same hunk in plain `glab mr diff` shape — paths verbatim, no `a/` / `b/`.
GL_DIFF_INDENTED = (
    "--- foo.py\n+++ foo.py\n@@ -1,1 +1,3 @@\n def f():\n+    return 1\n+    # tail\n"
)

# The same hunk again, but the hunk BODY is CRLF-terminated (only the body — the
# parser splits the whole stdout on "\n" only, so a body line ending "\r\n" leaves
# a trailing "\r" in that line's parsed text; header lines stay plain "\n" so the
# path keys this fixture produces are the ordinary, un-suffixed ones). This is what
# a real CRLF-in-the-repo diff hands the parser: transport, not content.
GH_DIFF_INDENTED_CRLF_BODY = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,3 @@\n"
    " def f():\n"
    "+    return 1\r\n"
    "+    # tail\r\n"
)

# The same file, long enough for a finding to STATE a span wider than GitLab's
# offset cap: every line 1..(cap + 3) is an added, addressable line, so
# `range_not_in_diff` — which precedes the anchor check — cannot be what a
# cap-exceeded span reports.
_GL_LONG_LINE_COUNT = post_review._GITLAB_SUGGESTION_OFFSET_CAP + 3
GL_DIFF_LONG = (
    f"--- foo.py\n+++ foo.py\n@@ -0,0 +1,{_GL_LONG_LINE_COUNT} @@\n"
    + "".join(f"+    line{n}\n" for n in range(1, _GL_LONG_LINE_COUNT + 1))
)

_FENCE = "```suggestion"


class TestParseDiffLinesLineTexts(unittest.TestCase):
    """``parse_diff_lines`` also returns each addressable line's NEW-SIDE TEXT.

    The parser already read that text off every ``+``/context line and threw it
    away. It is the only content oracle the apply-check can trust: by construction
    it is the content the platform's anchor points at, at the same head SHA the
    position carries. ``git show`` is not — the local HEAD is usually the base
    branch, and a shallow clone has no object to show at all.
    """

    def _parse(self, diff, platform="gitlab"):
        with patch("scripts.post_review.run_api", return_value=(diff, "", 0)):
            return parse_diff_lines(platform, "o", "r", 1)

    def test_line_texts_is_a_dict_parallel_to_valid_lines(self):
        """A PARALLEL dict, not a richer ``valid_lines`` value: every existing
        consumer of that mapping keeps reading exactly what it read before."""
        valid_lines, _, _, line_texts = self._parse(GL_DIFF_CONTRACT)
        self.assertIsInstance(line_texts, dict)
        self.assertEqual(set(line_texts), set(valid_lines))
        self.assertEqual(valid_lines[("src/edited.py", 61)], 50)

    def test_context_line_text_drops_the_marker_column(self):
        _, _, _, line_texts = self._parse(GL_DIFF_CONTRACT)
        self.assertEqual(line_texts[("src/edited.py", 61)], "unchanged_ctx")

    def test_added_line_text_drops_the_marker_column(self):
        _, _, _, line_texts = self._parse(GL_DIFF_CONTRACT)
        self.assertEqual(line_texts[("src/edited.py", 62)], "added")

    def test_blank_context_line_records_the_empty_string(self):
        """A unified diff spells a blank context line as a lone space — its
        content is the empty string, not a space."""
        _, _, _, line_texts = self._parse(GL_DIFF_RENAME)
        self.assertEqual(line_texts[("new_name.py", 6)], "")

    def test_leading_whitespace_is_preserved_verbatim(self):
        """Indentation is the whole point of the content oracle — a fix that
        re-indents a span is judged against the bytes the file really carries."""
        _, _, _, line_texts = self._parse(GH_DIFF_INDENTED, platform="github")
        self.assertEqual(line_texts[("foo.py", 2)], "    return 1")

    def test_no_newline_marker_records_no_text(self):
        """``\\ No newline at end of file`` belongs to neither side, so it must
        not be recorded as a line's content."""
        diff = (
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            " a\n"
            "\\ No newline at end of file\n"
            " b\n"
        )
        _, _, _, line_texts = self._parse(diff, platform="github")
        self.assertEqual(line_texts, {("f.py", 1): "a", ("f.py", 2): "b"})

    def test_removed_line_text_is_not_recorded(self):
        """A removed line has no new-side number — it must contribute no text."""
        _, _, _, line_texts = self._parse(GL_DIFF_CONTRACT)
        self.assertNotIn("removed", line_texts.values())

    def test_deleted_file_records_no_text(self):
        _, _, _, line_texts = self._parse(GL_DIFF_DELETED_THEN_MODIFIED)
        self.assertEqual([k for k in line_texts if k[0] == "src/removed.py"], [])

    def test_skipped_validation_returns_no_texts(self):
        valid_lines, _, _, line_texts = parse_diff_lines("bitbucket", "o", "r", 1)
        self.assertIsNone(valid_lines)
        self.assertIsNone(line_texts)


class TestParseDiffLinesHeaderDecoding(unittest.TestCase):
    """``parse_diff_lines`` now walks headers through ``diff_lines.walk_diff``, which
    undoes git's wire spelling of a header path (the TAB terminator after a path
    containing a space, and C-quoting of control/non-ASCII bytes) BEFORE this module's
    platform-specific ``a/``/``b/`` prefix strip runs.

    Pre-migration, the hand-rolled regexes (``^--- (?:a/)?(.+)$`` and friends) captured
    everything up to end-of-line verbatim: a trailing TAB stayed part of the key, and a
    C-quoted path stayed a literal quoted-and-escaped string that no finding could ever
    name. Cases (a), (b) and (d) below reproduce exactly that failure — each is reasoned
    through against the pre-migration regex in its own docstring, and was confirmed to
    fail by running it against ``git show origin/main:scripts/post_review.py``. Case (c)
    does not regress under the old code (GitLab's header regex never stripped a prefix),
    but pins the platform split itself — the direct regression test for "make the prefix
    strip unconditional for GitLab too". It pins BOTH sides (an ``a/``- and a ``b/``-rooted
    real directory each survive on their own file) and BOTH mappings the walk feeds
    (``old_paths`` for the old side, ``line_texts`` for the new side), keyed
    independently — the failure mode a shared walk could introduce is an unconditional
    strip landing on only one side or only one mapping while the other still looks correct.
    """

    def _parse(self, diff, platform="github"):
        with patch("scripts.post_review.run_api", return_value=(diff, "", 0)):
            return parse_diff_lines(platform, "o", "r", 1)

    def test_github_path_with_a_space_decodes_past_the_tab_terminator(self):
        """git appends a TAB after a header path containing a space, so the field
        does not run into the classic timestamp column.

        Pre-migration: ``(?:a/)?(.+)$`` is greedy to end-of-line, so ``group(1)``
        keeps the trailing ``"\\t"`` and the key lands as
        ``("dir with space/x.py\\t", 1)`` — nothing a finding names ever matches it.
        """
        diff = (
            "--- a/dir with space/x.py\t\n"
            "+++ b/dir with space/x.py\t\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        valid_lines, _, _, _ = self._parse(diff, platform="github")
        self.assertIn(("dir with space/x.py", 1), valid_lines)
        self.assertNotIn(("dir with space/x.py\t", 1), valid_lines)

    def test_github_c_quoted_non_ascii_path_decodes_then_strips_the_prefix(self):
        """A non-ASCII path is C-quoted with the octal-escaped UTF-8 bytes; the
        synthetic ``b/`` prefix sits INSIDE the quotes, so it must be stripped
        AFTER decoding, not matched against the raw quoted text.

        Pre-migration: the line does not start with ``b/`` (it starts with ``"``),
        so ``(?:b/)?`` matches nothing and ``group(1)`` is the literal
        ``'"b/caf\\\\303\\\\251.py"'`` — quotes, backslashes and octal digits as
        themselves. The key that lands is that literal string, not ``café.py``.
        """
        diff = (
            '--- "a/caf\\303\\251.py"\n'
            '+++ "b/caf\\303\\251.py"\n'
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        valid_lines, _, _, _ = self._parse(diff, platform="github")
        self.assertIn(("café.py", 1), valid_lines)

    def test_gitlab_keeps_real_a_and_b_slash_directories(self):
        """``glab mr diff`` writes paths verbatim: a leading ``a/`` OR ``b/`` there is a
        real top-level directory, not git's synthetic prefix, so both must survive the
        header decode and the (github-only) prefix strip. The old side is pinned through
        ``old_paths``, the new side through ``valid_lines`` AND ``line_texts`` — the two
        mappings are keyed independently."""
        diff = (
            "--- a/real/x.py\n+++ a/real/x.py\n@@ -1 +1 @@\n-old\n+new\n"
            "--- b/real/y.py\n+++ b/real/y.py\n@@ -1 +1 @@\n-old\n+fresh\n"
        )
        valid_lines, _, old_paths, line_texts = self._parse(diff, platform="gitlab")
        self.assertEqual(set(valid_lines), {("a/real/x.py", 1), ("b/real/y.py", 1)})
        self.assertEqual(
            old_paths, {"a/real/x.py": "a/real/x.py", "b/real/y.py": "b/real/y.py"}
        )
        self.assertEqual(line_texts[("a/real/x.py", 1)], "new")
        self.assertEqual(line_texts[("b/real/y.py", 1)], "fresh")

    def test_a_body_line_before_any_file_header_is_not_recorded(self):
        """`current_file` is None until a `+++` header names one; a line read before that
        has no path to key on and must record nothing."""
        valid_lines, _, _, line_texts = self._parse("@@ -0,0 +1 @@\n+orphan\n")
        self.assertEqual(valid_lines, {})
        self.assertEqual(line_texts, {})

    def test_gitlab_quoted_path_is_decoded_like_gits_wire_spelling(self):
        """This pins a documented choice, not a requirement: the header decode in
        ``diff_lines._decode_header_path`` runs on every producer, including GitLab's
        verbatim-path ``glab mr diff`` output. So a literal quote-wrapped name in a
        GitLab diff — which ``glab`` would never itself need to quote, but which this
        walk cannot distinguish from git's own C-quoting — is decoded the same way a
        real git wire-spelling would be. See the accepted-limitation note in
        ``_decode_header_path``'s docstring for why this is not fixed."""
        diff = '--- "notes"\n+++ "notes"\n@@ -1 +1,2 @@\n c\n+z\n'
        valid_lines, _, _, _ = self._parse(diff, platform="gitlab")
        self.assertIn(("notes", 1), valid_lines)

    def test_github_renamed_file_old_path_is_decoded_before_recording(self):
        """A rename's ``---`` header names the pre-rename path, recorded in
        ``old_paths`` under the NEW path's key. Quoted, that pre-rename path must be
        decoded — GitLab's ``position.old_path`` (#130) is a real filesystem path,
        not git's C-quoted wire spelling of one.

        Pre-migration: the old-side line does not start with ``a/`` (it starts with
        ``"``), so the captured, un-decoded literal ``'"a/caf\\\\303\\\\251
        old.py"'`` is what ``old_paths`` would carry — never a path the pre-rename
        file actually had.
        """
        diff = (
            'diff --git "a/caf\\303\\251 old.py" b/new.py\n'
            "similarity index 100%\n"
            'rename from "caf\\303\\251 old.py"\n'
            "rename to new.py\n"
            '--- "a/caf\\303\\251 old.py"\n'
            "+++ b/new.py\n"
            "@@ -1 +1 @@\n"
            " ctx\n"
        )
        _, _, old_paths, _ = self._parse(diff, platform="github")
        self.assertEqual(old_paths, {"new.py": "café old.py"})


class TestSuggestedFixGate(unittest.TestCase):
    """The pure gate helper: one case per reason in the closed vocabulary.

    Ground truth comes from the REAL parser (``_parse_fixture``), not a
    hand-written mapping — a gate checked against the answer the test wanted
    would agree with a broken parser.
    """

    def setUp(self):
        parsed = _parse_fixture(GH_DIFF_INDENTED, platform="github")
        self.valid_lines, _, _, self.line_texts = parsed

    def _finding(self, **over):
        finding = {
            "file": "foo.py",
            "line": 2,
            "end_line": 3,
            "suggested_fix_code": "    return 2\n    # done",
        }
        finding.update(over)
        return finding

    def _gate(self, finding, apply_range=(2, 3), **over):
        kwargs = {
            "apply_range": apply_range,
            "line_texts": self.line_texts,
            "valid_lines": self.valid_lines,
            "path_lookup": "foo.py",
        }
        kwargs.update(over)
        return post_review._suggested_fix_gate(finding, **kwargs)

    def _reason(self, finding, **over):
        ok, reason = self._gate(finding, **over)
        self.assertFalse(ok, f"expected a downgrade, got ok with reason {reason!r}")
        self.assertIn(
            reason,
            post_review._FIX_REASONS,
            "every downgrade must name a member of the closed vocabulary",
        )
        return reason

    # -- the passing cases -------------------------------------------------

    def test_absent_field_is_not_a_downgrade(self):
        finding = self._finding()
        del finding["suggested_fix_code"]
        self.assertEqual(self._gate(finding), (True, None))

    def test_sound_multi_line_fix_passes(self):
        self.assertEqual(self._gate(self._finding()), (True, None))

    def test_sound_single_line_fix_passes(self):
        finding = self._finding(line=2, end_line=2, suggested_fix_code="    return 2")
        self.assertEqual(self._gate(finding, apply_range=(2, 2)), (True, None))

    def test_legitimate_reindentation_passes(self):
        """The indentation check is deliberately weak: only a tab/space charset
        conflict is caught, so re-indenting a span is not a downgrade."""
        finding = self._finding(suggested_fix_code="        return 2\n        # done")
        self.assertEqual(self._gate(finding), (True, None))

    def test_a_partial_content_oracle_reads_as_no_oracle(self):
        """A span the texts cannot fully answer is NO oracle, never "no
        difference" — the latter would silently skip the content checks and
        pass the fence through unchecked. The range oracle alone is not
        enough: a patch is downgraded, not rendered, when the content oracle
        cannot answer for every line of the stated span.
        """
        finding = self._finding(suggested_fix_code="    return 1\n    # tail")
        self.assertEqual(
            self._gate(finding, line_texts={("foo.py", 2): "    return 1"}),
            (False, "no_diff_oracle"),
        )

    def test_a_mixed_path_spelling_range_has_no_content_oracle(self):
        """``_range_is_valid``/``is_line_valid`` tolerate a per-line mix of the
        exact diff key and its ``a/``/``b/``-stripped form, so a mixed-spelling
        range validates line-by-line. ``_span_texts`` demands ONE spelling for
        every line, so the very same range makes the span ``None`` — no
        content oracle, so this must downgrade rather than silently skip the
        content checks.
        """
        valid_lines = {("b/x.py", 10): 10, ("x.py", 11): 11, ("x.py", 12): 12}
        line_texts = {
            ("b/x.py", 10): "line10",
            ("x.py", 11): "line11",
            ("x.py", 12): "line12",
        }
        finding = {
            "file": "b/x.py",
            "line": 10,
            "end_line": 12,
            "suggested_fix_code": "a\nb\nc",
        }
        self.assertEqual(
            post_review._suggested_fix_gate(
                finding,
                apply_range=(10, 12),
                line_texts=line_texts,
                valid_lines=valid_lines,
                path_lookup="b/x.py",
            ),
            (False, "no_diff_oracle"),
        )

    # -- 1. non_string -----------------------------------------------------

    def test_non_string_fix(self):
        self.assertEqual(
            self._reason(self._finding(suggested_fix_code=42)), "non_string"
        )

    def test_null_fix_is_non_string(self):
        """The contracts say OMIT, never null — a null that arrives anyway is
        not a string and is downgraded, not rendered."""
        self.assertEqual(
            self._reason(self._finding(suggested_fix_code=None)), "non_string"
        )

    # -- 2. empty ----------------------------------------------------------

    def test_whitespace_only_fix_is_empty(self):
        self.assertEqual(
            self._reason(self._finding(suggested_fix_code="  \n  ")), "empty"
        )

    # -- 3. carriage_return --------------------------------------------------

    def test_an_interior_lone_cr_downgrades(self):
        """CommonMark treats a lone ``\\r`` as a line ending — ``"foo\\rbar"`` is
        ONE line to this gate's ``split("\\n")`` but TWO lines in the rendered
        fence and the applied patch. That gap is evadable (it dodges the no-op,
        indentation, and line-count checks entirely), so any interior ``\\r``
        fails closed before those measurements run."""
        finding = self._finding(suggested_fix_code="foo\rbar\rbaz")
        self.assertEqual(self._reason(finding), "carriage_return")

    def test_a_crlf_terminated_replacement_downgrades(self):
        """A replacement whose lines end ``\\r\\n`` is exactly the ambiguous
        CRLF-file case a one-click apply must not ship — the prose suggestion
        still carries the fix."""
        finding = self._finding(suggested_fix_code="    return 2\r\n    # done\r\n")
        self.assertEqual(self._reason(finding), "carriage_return")

    # -- 4. redacted -------------------------------------------------------

    def test_a_fix_the_redactor_rewrites_is_never_shipped(self):
        """One click would commit the literal ``[REDACTED]`` into the file."""
        secret = "ghp_" + "A" * 24
        finding = self._finding(suggested_fix_code=f"    token = '{secret}'")
        self.assertEqual(self._reason(finding), "redacted")

    # -- 5. missing_end_line -----------------------------------------------

    def test_absent_end_line(self):
        finding = self._finding()
        del finding["end_line"]
        self.assertEqual(self._reason(finding, apply_range=(2, 2)), "missing_end_line")

    def test_null_end_line_is_absent(self):
        """#205 DELETES ``line_end`` when a span exceeds ``maxLineSpan``; a null
        left behind by anything else must read the same way."""
        self.assertEqual(
            self._reason(self._finding(end_line=None), apply_range=(2, 2)),
            "missing_end_line",
        )

    # -- 6. invalid_range --------------------------------------------------

    def test_end_line_before_line(self):
        self.assertEqual(
            self._reason(self._finding(line=3, end_line=2), apply_range=(3, 2)),
            "invalid_range",
        )

    def test_non_integer_line(self):
        self.assertEqual(
            self._reason(self._finding(line=2.0), apply_range=(2.0, 3)),
            "invalid_range",
        )

    def test_boolean_line_is_not_an_integer(self):
        """``True`` is an ``int`` to ``isinstance`` and hashes equal to ``1`` —
        the same trap ``validate_position`` documents."""
        self.assertEqual(
            self._reason(
                self._finding(line=True, end_line=True), apply_range=(True, True)
            ),
            "invalid_range",
        )

    def test_line_below_one(self):
        self.assertEqual(
            self._reason(self._finding(line=0, end_line=1), apply_range=(0, 1)),
            "invalid_range",
        )

    # -- 7. no_diff_oracle -------------------------------------------------

    def test_a_missing_diff_fails_closed(self):
        """A failed diff fetch leaves NO oracle, so the range and content checks
        cannot run at all. The ANCHOR fails open there — a wrong anchor costs a
        misplaced comment. A patch cannot: a wrong patch corrupts the file, and
        the prose suggestion carries the same content at no risk.
        """
        self.assertEqual(
            self._reason(self._finding(), valid_lines=None, line_texts=None),
            "no_diff_oracle",
        )

    def test_valid_lines_alone_is_not_an_oracle(self):
        self.assertEqual(
            self._reason(self._finding(), line_texts=None), "no_diff_oracle"
        )

    def test_line_texts_alone_is_not_an_oracle(self):
        self.assertEqual(
            self._reason(self._finding(), valid_lines=None), "no_diff_oracle"
        )

    # -- 8. range_not_in_diff ----------------------------------------------

    def test_end_line_outside_the_diff(self):
        self.assertEqual(
            self._reason(self._finding(end_line=940), apply_range=(2, 940)),
            "range_not_in_diff",
        )

    def test_unknown_path(self):
        self.assertEqual(
            self._reason(self._finding(), path_lookup="other.py"),
            "range_not_in_diff",
        )

    # -- 9. anchor_mismatch ------------------------------------------------

    def test_apply_range_narrower_than_the_stated_range(self):
        """The site's one click really replaces less than the patch states, so
        applying it would overwrite one line and leave the other. Reached with a
        wrong anchor, and wherever no wider apply range can be expressed at all —
        a GitLab span past the platform offset cap renames THIS outcome
        (``span_exceeds_platform_cap``) rather than adding a check."""
        self.assertEqual(
            self._reason(self._finding(), apply_range=(2, 2)), "anchor_mismatch"
        )

    def test_no_apply_range_at_all(self):
        """A position-less note and the degraded body section carry no anchor —
        a fence there can never be applied."""
        self.assertEqual(
            self._reason(self._finding(), apply_range=None), "anchor_mismatch"
        )

    # -- 10. no_op_replacement ----------------------------------------------

    def test_replacement_equal_to_the_span(self):
        finding = self._finding(suggested_fix_code="    return 1\n    # tail")
        self.assertEqual(self._reason(finding), "no_op_replacement")

    def test_no_op_ignores_a_transport_carriage_return(self):
        """A CRLF diff leaves a trailing ``\\r`` on every parsed line's SPAN
        text. That is transport, not content, so it must not make a no-op
        look like a change. Parsed by the REAL parser (``_parse_fixture``),
        not a hand-built dict — G1 now downgrades any REPLACEMENT carrying a
        ``\\r`` before this check even runs, so a replacement-side ``\\r`` can
        no longer pin this tolerance; only the span side can, which G1 leaves
        untouched.
        """
        valid_lines, _, _, line_texts = _parse_fixture(
            GH_DIFF_INDENTED_CRLF_BODY, platform="github"
        )
        self.assertEqual(line_texts[("foo.py", 2)], "    return 1\r")
        self.assertEqual(line_texts[("foo.py", 3)], "    # tail\r")
        finding = self._finding(suggested_fix_code="    return 1\n    # tail")
        ok, reason = self._gate(finding, valid_lines=valid_lines, line_texts=line_texts)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_op_replacement")

    # -- 11. indentation_mismatch ------------------------------------------

    def test_tabs_into_a_space_indented_span(self):
        finding = self._finding(suggested_fix_code="\treturn 2\n\t# done")
        self.assertEqual(self._reason(finding), "indentation_mismatch")

    def test_spaces_into_a_tab_indented_span(self):
        """The symmetric case, against a tab-indented span."""
        tabbed = (
            "diff --git a/t.py b/t.py\n"
            "--- a/t.py\n"
            "+++ b/t.py\n"
            "@@ -1,1 +1,2 @@\n"
            " def f():\n"
            "+\treturn 1\n"
        )
        valid_lines, _, _, line_texts = _parse_fixture(tabbed, platform="github")
        finding = {
            "file": "t.py",
            "line": 2,
            "end_line": 2,
            "suggested_fix_code": "    return 2",
        }
        ok, reason = post_review._suggested_fix_gate(
            finding,
            apply_range=(2, 2),
            line_texts=line_texts,
            valid_lines=valid_lines,
            path_lookup="t.py",
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "indentation_mismatch")

    def test_an_unindented_span_conflicts_with_nothing(self):
        """Lines without leading whitespace say nothing about the file's
        indentation style, so they contribute nothing to the charset."""
        valid_lines, _, _, line_texts = _parse_fixture(
            GH_DIFF_MULTILINE, platform="github"
        )
        finding = {
            "file": "foo.py",
            "line": 2,
            "end_line": 3,
            "suggested_fix_code": "\tfixed2\n\tfixed3",
        }
        self.assertEqual(
            post_review._suggested_fix_gate(
                finding,
                apply_range=(2, 3),
                line_texts=line_texts,
                valid_lines=valid_lines,
                path_lookup="foo.py",
            ),
            (True, None),
        )

    # -- edge blank lines are content --------------------------------------

    def test_a_leading_blank_line_is_content_not_padding(self):
        """The fence normalizer takes the terminator off and NOTHING else, so a
        stated leading blank line survives — which makes this a real change
        against the same two lines, not a no-op."""
        finding = self._finding(suggested_fix_code="\n    return 1\n    # tail")
        self.assertEqual(self._gate(finding), (True, None))

    def test_a_second_trailing_newline_is_content(self):
        finding = self._finding(suggested_fix_code="    return 1\n    # tail\n\n")
        self.assertEqual(self._gate(finding), (True, None))

    def test_exactly_one_trailing_newline_is_the_terminator(self):
        finding = self._finding(suggested_fix_code="    return 1\n    # tail\n")
        self.assertEqual(self._reason(finding), "no_op_replacement")

    # -- 12. replacement_too_large ----------------------------------------

    def test_too_many_lines(self):
        body = "\n".join(f"    line{n}" for n in range(post_review._FIX_MAX_LINES + 1))
        self.assertEqual(
            self._reason(self._finding(suggested_fix_code=body)),
            "replacement_too_large",
        )

    def test_too_many_characters(self):
        body = "    " + "x" * post_review._FIX_MAX_CHARS
        self.assertEqual(
            self._reason(self._finding(suggested_fix_code=body)),
            "replacement_too_large",
        )

    def test_exactly_at_the_bounds_passes(self):
        body = "\n".join(f"    line{n}" for n in range(post_review._FIX_MAX_LINES))
        self.assertLessEqual(len(body), post_review._FIX_MAX_CHARS)
        self.assertEqual(
            self._gate(self._finding(suggested_fix_code=body)), (True, None)
        )

    def test_the_terminator_does_not_count_as_a_line(self):
        """ONE definition of lines and chars everywhere (#63): both are measured
        on the NORMALIZED text — ``split("\\n")`` elements, and ``len()`` in code
        points. The terminating newline is not a 101st line."""
        body = "\n".join(f"    line{n}" for n in range(post_review._FIX_MAX_LINES))
        self.assertEqual(
            self._gate(self._finding(suggested_fix_code=body + "\n")), (True, None)
        )

    def test_a_blank_line_past_the_terminator_does_count(self):
        body = "\n".join(f"    line{n}" for n in range(post_review._FIX_MAX_LINES))
        self.assertEqual(
            self._reason(self._finding(suggested_fix_code=body + "\n\n")),
            "replacement_too_large",
        )

    def test_the_terminator_does_not_count_toward_the_char_bound(self):
        body = "    " + "x" * (post_review._FIX_MAX_CHARS - 4)
        self.assertEqual(len(body), post_review._FIX_MAX_CHARS)
        self.assertEqual(
            self._gate(self._finding(suggested_fix_code=body + "\n")), (True, None)
        )

    # -- the vocabulary is closed -----------------------------------------

    def test_every_reason_constant_is_in_the_closed_set(self):
        self.assertEqual(
            post_review._FIX_REASONS,
            frozenset(
                {
                    "non_string",
                    "empty",
                    "carriage_return",
                    "redacted",
                    "missing_end_line",
                    "invalid_range",
                    "no_diff_oracle",
                    "range_not_in_diff",
                    "anchor_mismatch",
                    "span_exceeds_platform_cap",
                    "no_op_replacement",
                    "indentation_mismatch",
                    "replacement_too_large",
                }
            ),
        )

    def test_the_closed_vocabulary_has_thirteen_members(self):
        """Adding a reason is a deliberate act — this is the tripwire that says
        so out loud."""
        self.assertEqual(len(post_review._FIX_REASONS), 13)


class TestGatedFindingRejectsUnknownReason(unittest.TestCase):
    """``_gated_finding`` consults ``_FIX_REASONS`` at every downgrade.

    A typo'd reason string in a future edit to ``_suggested_fix_gate`` must
    fail loudly at the FIRST downgrade it produces, not get silently recorded
    into the stable warning line. This is what makes ``_FIX_REASONS`` more
    than a comment other tests happen to pin.
    """

    def test_a_reason_outside_the_closed_vocabulary_raises(self):
        finding = {"file": "foo.py", "line": 2, "suggested_fix_code": "x"}
        with (
            patch(
                "scripts.post_review._suggested_fix_gate",
                return_value=(False, "bogus"),
            ),
            self.assertRaises(ValueError) as ctx,
        ):
            post_review._gated_finding(finding, (2, 2), {}, {})
        self.assertIn("bogus", str(ctx.exception))

    def test_a_renamed_anchor_failure_is_checked_against_the_same_set(self):
        """``mismatch_reason`` renames one gate outcome; it cannot widen the
        vocabulary the warning line's readers rely on."""
        finding = {"file": "foo.py", "line": 2, "suggested_fix_code": "x"}
        with (
            patch(
                "scripts.post_review._suggested_fix_gate",
                return_value=(False, "anchor_mismatch"),
            ),
            self.assertRaises(ValueError) as ctx,
        ):
            post_review._gated_finding(finding, (2, 2), {}, {}, mismatch_reason="bogus")
        self.assertIn("bogus", str(ctx.exception))


class TestGitLabFenceOffsets(unittest.TestCase):
    """``_gitlab_fence_offsets``: the one producer of GitLab's ``-m+n`` pair.

    GitLab resolves the header against ``position.new_line``, so the pair is a
    function of the ANCHOR and the stated range — never of the finding alone.
    """

    def test_a_single_line_range_needs_no_offsets(self):
        self.assertEqual(post_review._gitlab_fence_offsets(2, 2, 2), ((0, 0), False))

    def test_a_span_below_the_anchor(self):
        self.assertEqual(post_review._gitlab_fence_offsets(2, 2, 4), ((0, 2), False))

    def test_a_span_above_the_anchor(self):
        """Unit-only: every delivery path anchors a finding at its own ``line``,
        so ``m`` is 0 everywhere it is reachable today. The helper still answers
        for an anchor inside the range, because the anchor is its input."""
        self.assertEqual(post_review._gitlab_fence_offsets(4, 2, 4), ((2, 0), False))

    def test_an_anchor_before_the_range_is_unrealizable(self):
        self.assertEqual(post_review._gitlab_fence_offsets(1, 2, 4), (None, False))

    def test_an_anchor_after_the_range_is_unrealizable(self):
        self.assertEqual(post_review._gitlab_fence_offsets(5, 2, 4), (None, False))

    def test_the_cap_is_inclusive(self):
        cap = post_review._GITLAB_SUGGESTION_OFFSET_CAP
        self.assertEqual(
            post_review._gitlab_fence_offsets(2, 2, 2 + cap), ((0, cap), False)
        )

    def test_one_line_past_the_cap_is_cap_exceeded_not_unrealizable(self):
        """GitLab CLAMPS an offset above the cap instead of rejecting it, so a
        header carrying one would apply a range it does not state. The second
        return is what lets that failure be named."""
        cap = post_review._GITLAB_SUGGESTION_OFFSET_CAP
        self.assertEqual(post_review._gitlab_fence_offsets(2, 2, 3 + cap), (None, True))

    def test_an_above_offset_past_the_cap_is_cap_exceeded(self):
        cap = post_review._GITLAB_SUGGESTION_OFFSET_CAP
        anchor = 2 + cap + 1
        self.assertEqual(
            post_review._gitlab_fence_offsets(anchor, 2, anchor), (None, True)
        )

    def test_a_non_integer_bound_is_not_a_cap_failure(self):
        """A missing or non-integer bound is the gate's business
        (``missing_end_line`` / ``invalid_range``); the helper only declines to
        answer, which leaves the single anchored line as the apply range."""
        for end_line in (None, "3", 3.0, True):
            with self.subTest(end_line=end_line):
                self.assertEqual(
                    post_review._gitlab_fence_offsets(2, 2, end_line), (None, False)
                )


class TestGitLabAnchoredDecision(unittest.TestCase):
    """``_gitlab_anchored`` — the whole GitLab render-site decision, once.

    Both the poster and the benchmark's payload mirror call it, so the mirror
    cannot drift into fiction that stays green.
    """

    def setUp(self):
        parsed = _parse_fixture(GL_DIFF_INDENTED, platform="gitlab")
        self.valid_lines, _, _, self.line_texts = parsed

    def _finding(self, **over):
        finding = {
            "file": "foo.py",
            "line": 2,
            "end_line": 3,
            "title": "T",
            "body": "b",
            "suggested_fix_code": "    return 2\n    # done",
        }
        finding.update(over)
        return finding

    def _anchored(self, finding, anchor=2):
        return post_review._gitlab_anchored(
            finding, anchor, self.valid_lines, self.line_texts
        )

    def test_a_kept_fence_comes_with_the_offsets_that_realize_its_range(self):
        gated, offsets = self._anchored(self._finding())
        self.assertIn("suggested_fix_code", gated)
        self.assertEqual(offsets, (0, 1))

    def test_it_never_mutates_the_finding_it_is_given(self):
        """Offsets travel out of band. A key written onto the finding would move
        every delivery key it seeds — `_key_material_finding` renders the
        ORIGINAL dict, not this copy."""
        finding = self._finding()
        before = dict(finding)
        self._anchored(finding)
        self.assertEqual(finding, before)

    def test_a_downgrade_leaves_the_input_untouched(self):
        """The strip happens on a copy — the caller's dict still carries the
        field, and the render-time offsets are moot once the fence is gone."""
        finding = self._finding(suggested_fix_code="    return 1\n    # tail")
        gated, offsets = self._anchored(finding)
        self.assertNotIn("suggested_fix_code", gated)
        self.assertIn("suggested_fix_code", finding)
        self.assertEqual(offsets, (0, 1))

    def test_an_unrealizable_span_falls_back_to_the_single_anchored_line(self):
        """The anchor is outside the stated range (unreachable from the poster,
        which anchors every finding at its own line), so no header expresses it:
        the gate judges the one line the position really carries."""
        gated, offsets = self._anchored(self._finding(), anchor=1)
        self.assertNotIn("suggested_fix_code", gated)
        self.assertIsNone(offsets)


class TestPosterOraclesAreRequiredArguments(unittest.TestCase):
    """Neither poster may take a parsed-diff argument by default.

    A default let a caller omit ``line_texts`` and silently disable half the
    apply-check — the content oracle absent, every fence downgraded for a
    reason the diff would have answered. ``parse_diff_lines`` returns all four
    together or all four ``None``; the signature is what makes a caller say so.
    """

    def _defaults(self, func):
        return {
            name: p.default
            for name, p in inspect.signature(func).parameters.items()
            if p.default is not inspect.Parameter.empty
        }

    def test_post_github_has_no_defaulted_arguments(self):
        self.assertEqual(self._defaults(post_review.post_github), {})

    def test_post_gitlab_has_no_defaulted_arguments(self):
        self.assertEqual(self._defaults(post_review.post_gitlab), {})


class _FixGateRunBase(_DryRunTestBase):
    """Drives the real ``main()`` over a diff and returns payload + streams."""

    PLATFORM = "github"
    DIFF = GH_DIFF_INDENTED

    def _run(
        self,
        findings,
        dry_run=True,
        diff=None,
        versions=None,
        payloads=None,
        prior=None,
        **fake_run_kwargs,
    ):
        self._write(
            {
                "platform": self.PLATFORM,
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "sha": "a" * 40,
                "findings": findings,
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
            patch(
                "scripts.post_review.gitlab_prior_delivery_state",
                return_value=(False, set(), frozenset(), None)
                if prior is None
                else prior,
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(
                    diff=self.DIFF if diff is None else diff,
                    versions=versions,
                    payloads=payloads,
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
            payload=self._payload() if dry_run else None,
            out=stdout.getvalue(),
            err=stderr.getvalue(),
            mock_run=mock_run,
            exit_code=exit_code,
        )

    def _finding(self, **over):
        finding = {
            "file": "foo.py",
            "line": 2,
            "end_line": 3,
            "severity": "high",
            "title": "Range bug",
            "body": "Body",
            "suggestion": "Return two instead.",
            "suggested_fix_code": "    return 2\n    # done",
        }
        finding.update(over)
        return finding

    def _comment_body(self, run):
        return run.payload["payload"]["comments"][0]["body"]

    def _assert_downgraded(self, run, reason, where="foo.py:2"):
        self.assertIn(
            f"suggested-fix downgraded: {where} ({reason})", run.payload["skipped"]
        )


class TestGitHubSuggestedFixGate(_FixGateRunBase):
    """The GitHub inline path: the fence survives only at the anchor it states."""

    def test_multi_line_fix_is_kept_at_a_matching_multi_line_anchor(self):
        run = self._run([self._finding()])
        comment = run.payload["payload"]["comments"][0]
        self.assertEqual(comment["start_line"], 2)
        self.assertEqual(comment["line"], 3)
        self.assertIn(_FENCE, comment["body"])
        self.assertIn("    return 2\n    # done", comment["body"])
        self.assertEqual(run.payload["skipped"], [])

    def test_single_line_fix_is_kept_at_a_single_line_anchor(self):
        run = self._run([self._finding(end_line=2, suggested_fix_code="    return 2")])
        comment = run.payload["payload"]["comments"][0]
        self.assertNotIn("start_line", comment)
        self.assertIn(_FENCE, comment["body"])

    def test_a_range_outside_the_diff_loses_the_fence_with_the_range(self):
        """The anchor decision is made ABOVE the body render, so the gate sees the
        range the comment really applies at. ``end_line`` outside the hunk degrades
        the comment to single-line — and ``range_not_in_diff`` precedes
        ``anchor_mismatch``, so the range failure is what the downgrade names.
        """
        run = self._run([self._finding(end_line=940)])
        comment = run.payload["payload"]["comments"][0]
        self.assertNotIn("start_line", comment)
        self.assertEqual(comment["line"], 2)
        self.assertNotIn(_FENCE, comment["body"])
        self._assert_downgraded(run, "range_not_in_diff")

    def test_the_gate_sees_the_anchor_the_comment_really_carries(self):
        """Why the anchor decision is hoisted above the body render.

        A gate that judged the STATED range instead would agree with the anchor
        only by luck: here the second finding states 2..940 and the comment it
        produces applies at line 2 alone.
        """
        seen = []
        real = post_review._suggested_fix_gate

        def spy(finding, **kwargs):
            seen.append(kwargs["apply_range"])
            return real(finding, **kwargs)

        with patch("scripts.post_review._suggested_fix_gate", side_effect=spy):
            run = self._run([self._finding(), self._finding(end_line=940)])
        anchors = [
            (c.get("start_line", c["line"]), c["line"])
            for c in run.payload["payload"]["comments"]
        ]
        self.assertEqual(anchors, [(2, 3), (2, 2)])
        self.assertEqual(seen, anchors)

    def test_the_prose_suggestion_still_renders_after_a_downgrade(self):
        run = self._run([self._finding(end_line=940)])
        body = self._comment_body(run)
        self.assertIn("**Suggested fix:**", body)
        self.assertIn("Return two instead.", body)

    def test_a_failed_diff_fetch_downgrades_the_fence(self):
        """The diff IS the oracle. When the fetch fails there is nothing to
        check the patch against, so the committable fence must not ship — the
        prose suggestion carries the same content at no risk (#63)."""
        run = self._run([self._finding()], diff_rc=1)
        body = self._comment_body(run)
        self.assertNotIn(_FENCE, body)
        self.assertIn("Return two instead.", body)
        self._assert_downgraded(run, "no_diff_oracle")

    def test_edge_blank_lines_reach_the_fence_intact(self):
        """Stated == checked == applied: the gate measured these bytes, so the
        fence carries exactly them (less the one terminating newline)."""
        code = "\n    return 2\n    # done\n\n"
        run = self._run([self._finding(suggested_fix_code=code)])
        self.assertEqual(run.payload["skipped"], [])
        self.assertIn(
            "```suggestion\n\n    return 2\n    # done\n\n```", self._comment_body(run)
        )

    def test_group_primary_keeps_its_fence_in_the_group_body(self):
        """A group comment anchors on the PRIMARY's range, and only the primary's
        fence exists in the body (`_render_corroboration` emits none)."""
        primary = self._finding(
            consolidation_key="foo.py:0", consolidation_primary=True
        )
        corroborator = {
            "file": "foo.py",
            "line": 3,
            "severity": "medium",
            "title": "B",
            "body": "Body B",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
        }
        run = self._run([primary, corroborator])
        body = self._comment_body(run)
        self.assertIn(_FENCE, body)
        self.assertEqual(body.count(_FENCE), 1)
        self.assertIn("Corroborating finding", body)

    def test_body_section_entries_lose_the_fence(self):
        """A corroborator with a perfectly valid range degrades into the review
        body because its group's PRIMARY could not be anchored. Nothing in that
        section is one-click-appliable, so its fence goes — and with every
        earlier check passing, ``anchor_mismatch`` is what names it."""
        primary = {
            "file": "foo.py",
            "line": 999,
            "severity": "high",
            "title": "A",
            "body": "Body A",
            "consolidation_key": "foo.py:0",
            "consolidation_primary": True,
        }
        corroborator = self._finding(
            title="B",
            consolidation_key="foo.py:0",
            consolidation_primary=False,
        )
        run = self._run([primary, corroborator])
        self.assertEqual(run.payload["payload"]["comments"], [])
        body = run.payload["payload"]["body"]
        self.assertIn("could not be anchored inline", body)
        self.assertNotIn(_FENCE, body)
        self._assert_downgraded(run, "anchor_mismatch")

    # -- one integration case per remaining reason -------------------------

    def test_reasons_reachable_through_the_delivery_path(self):
        secret = "ghp_" + "A" * 24
        cases = [
            ("non_string", self._finding(suggested_fix_code=42)),
            ("empty", self._finding(suggested_fix_code="   ")),
            (
                "redacted",
                self._finding(suggested_fix_code=f"    token = '{secret}'"),
            ),
            ("missing_end_line", self._finding(end_line=None)),
            ("invalid_range", self._finding(line=3, end_line=2)),
            (
                "no_op_replacement",
                self._finding(suggested_fix_code="    return 1\n    # tail"),
            ),
            (
                "indentation_mismatch",
                self._finding(suggested_fix_code="\treturn 2\n\t# done"),
            ),
            (
                "replacement_too_large",
                self._finding(
                    suggested_fix_code="\n".join(
                        f"    line{n}" for n in range(post_review._FIX_MAX_LINES + 1)
                    )
                ),
            ),
        ]
        for reason, finding in cases:
            with self.subTest(reason=reason):
                run = self._run([finding])
                where = f"foo.py:{finding.get('line')}"
                self.assertNotIn(_FENCE, self._comment_body(run))
                self._assert_downgraded(run, reason, where=where)

    # -- the apply-check readout -------------------------------------------

    def test_stdout_reports_both_halves_of_the_acceptance_rate(self):
        run = self._run([self._finding(), self._finding(end_line=940, line=2)])
        self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)
        self.assertIn("  1 suggested fix(es) downgraded to prose.", run.out)

    def test_the_readout_claims_no_delivery_in_either_mode(self):
        """These two lines report the GATE's verdict, not an outcome on the
        forge — so neither carries a delivery verb, and both read the same live
        and dry-run. The per-platform count lines above own delivery."""
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                run = self._run([self._finding()], dry_run=dry_run)
                self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)
                self.assertNotIn("fence(s) posted", run.out)
                self.assertNotIn("fence(s) captured", run.out)

    def test_nothing_printed_when_no_finding_carried_the_field(self):
        finding = self._finding()
        del finding["suggested_fix_code"]
        run = self._run([finding])
        self.assertNotIn("apply-check", run.out)
        self.assertNotIn("downgraded to prose", run.out)

    def test_the_gate_is_identical_under_dry_run_and_live(self):
        """``validate_position``'s precedent: a check that runs only under
        --dry-run cannot be the thing that makes --dry-run trustworthy."""
        findings = [self._finding(), self._finding(line=2, end_line=940)]
        dry = self._run(findings)
        payloads = []
        live = self._run(findings, dry_run=False, payloads=payloads)
        live_bodies = [c["body"] for c in payloads[0]["comments"]]
        dry_bodies = [c["body"] for c in dry.payload["payload"]["comments"]]
        self.assertEqual(live_bodies, dry_bodies)
        self.assertIn("(range_not_in_diff)", live.err)

    def test_caller_supplied_fences_go_through_the_same_gate(self):
        """One path, no fork: a hand-assembled findings JSON is gated exactly as
        an agent-emitted one (requirement 1 — previously unvalidated fences
        posted straight through)."""
        run = self._run([self._finding(suggested_fix_code="    return 1\n    # tail")])
        self.assertNotIn(_FENCE, self._comment_body(run))
        self._assert_downgraded(run, "no_op_replacement")


class TestGitLabSuggestedFixGate(_FixGateRunBase):
    """A GitLab position is ALWAYS single-line; the fence header is what widens
    the apply range. ``suggestion:-m+n`` replaces ``[anchor - m, anchor + n]``
    (#219), so the offsets are derived from the anchor the discussion is posted
    at and the gate judges the range they realize."""

    PLATFORM = "gitlab"
    DIFF = GL_DIFF_INDENTED

    VERSIONS: ClassVar[list] = [
        {
            "base_commit_sha": "base1",
            "head_commit_sha": "head1",
            "start_commit_sha": "start1",
        }
    ]

    def _run(self, findings, **kw):
        kw.setdefault("versions", self.VERSIONS)
        return super()._run(findings, **kw)

    def test_single_line_fix_is_kept(self):
        run = self._run([self._finding(end_line=2, suggested_fix_code="    return 2")])
        self.assertIn(_FENCE, run.payload["discussions"][0]["body"])
        self.assertEqual(run.payload["skipped"], [])

    def test_multi_line_fix_ships_an_offset_header(self):
        """The finding anchors at 2 and states 2..3, so one click must replace
        both lines — which the HEADER says, not the position (#219)."""
        run = self._run([self._finding()])
        self.assertIn(
            "```suggestion:-0+1\n    return 2\n    # done\n```",
            run.payload["discussions"][0]["body"],
        )
        self.assertEqual(run.payload["skipped"], [])
        self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)

    def test_the_offset_header_leaves_the_position_alone(self):
        """The widening lives entirely in the fence: GitLab resolves the header
        against ``position.new_line``, so the position is byte-identical to the
        single-line case — no ``line_range``, no new keys."""
        multi = self._run([self._finding()])
        single = self._run(
            [self._finding(end_line=2, suggested_fix_code="    return 2")]
        )
        self.assertEqual(
            multi.payload["discussions"][0]["position"],
            single.payload["discussions"][0]["position"],
        )

    def test_every_emitted_header_is_centred_on_the_position_it_ships_with(self):
        """The wire-level invariant behind the body factory: ``deliver`` renders
        with the SAME line it writes into ``position.new_line``, so a header can
        never state a range that anchor does not centre."""
        findings = [
            self._finding(),
            self._finding(end_line=2, suggested_fix_code="    return 2"),
        ]
        run = self._run(findings)
        headers = 0
        for finding, discussion in zip(
            findings, run.payload["discussions"], strict=True
        ):
            match = re.search(r"```suggestion:-(\d+)\+(\d+)", discussion["body"])
            if match is None:
                continue
            anchor = discussion["position"]["new_line"]
            self.assertEqual(anchor - int(match.group(1)), finding["line"])
            self.assertEqual(anchor + int(match.group(2)), finding["end_line"])
            headers += 1
        self.assertEqual(headers, 1)

    def test_the_offset_header_is_identical_under_dry_run_and_live(self):
        """``validate_position``'s precedent, extended to the new path: the live
        body is the dry-run body plus the delivery marker, nothing else."""
        findings = [self._finding()]
        dry_body = self._run(findings).payload["discussions"][0]["body"]
        payloads = []
        self._run(findings, dry_run=False, payloads=payloads)
        live_body = next(p["body"] for p in payloads if "position" in p)
        self.assertIn("```suggestion:-0+1", dry_body)
        self.assertTrue(live_body.startswith(dry_body))

    def test_a_span_past_the_platform_cap_is_downgraded_by_name(self):
        """Hand-assembled: the pipeline's own ``maxLineSpan`` intake bound
        (default 100) drops a span this wide upstream, so only caller-supplied
        JSON reaches here. GitLab CLAMPS an offset above its cap rather than
        rejecting it, so the header would apply a range it does not state."""
        cap = post_review._GITLAB_SUGGESTION_OFFSET_CAP
        finding = self._finding(
            end_line=2 + cap + 1, suggested_fix_code="    patched\n    also patched"
        )
        run = self._run([finding], diff=GL_DIFF_LONG)
        self.assertNotIn(_FENCE, run.payload["discussions"][0]["body"])
        self._assert_downgraded(run, "span_exceeds_platform_cap")

    def test_no_emitted_header_ever_states_an_offset_past_the_cap(self):
        """The render-level invariant the cap exists for, over a whole payload:
        the span one line inside the cap ships, the one line outside it does
        not, and nothing in between leaks a clamped offset."""
        cap = post_review._GITLAB_SUGGESTION_OFFSET_CAP
        run = self._run(
            [
                self._finding(end_line=2 + cap, suggested_fix_code="    patched"),
                self._finding(end_line=2 + cap + 1, suggested_fix_code="    patched"),
            ],
            diff=GL_DIFF_LONG,
        )
        self.assertEqual(
            re.findall(r"suggestion:-(\d+)\+(\d+)", json.dumps(run.payload)),
            [("0", str(cap))],
        )

    def test_an_out_of_diff_span_past_the_cap_still_reports_the_range(self):
        """Check order is unchanged: the STATED range is judged against the diff
        before the anchor, so a span that is both out-of-diff and past the cap
        reports what it always reported."""
        cap = post_review._GITLAB_SUGGESTION_OFFSET_CAP
        run = self._run([self._finding(end_line=2 + cap + 1)])
        self._assert_downgraded(run, "range_not_in_diff")

    def test_a_forged_offsets_key_on_the_finding_changes_nothing(self):
        """Offsets are derived from the anchor, never read from the findings
        JSON — which is caller-supplied and flows in unfiltered. An in-band key
        would let a hand-assembled payload widen a range the gate approved."""
        fix = {"end_line": 2, "suggested_fix_code": "    return 2"}
        honest = self._run([self._finding(**fix)])
        forged = self._run(
            [
                self._finding(
                    **fix, fence_offsets=[0, 40], _fence_offsets=[0, 40], above=9
                )
            ]
        )
        self.assertEqual(forged.payload["discussions"], honest.payload["discussions"])

    def test_a_failed_diff_fetch_downgrades_the_fence(self):
        """`glab mr diff` failing leaves no oracle at all — the discussion still
        posts (the anchor fails open), but its fence does not (#63)."""
        run = self._run(
            [self._finding(end_line=2, suggested_fix_code="    return 2")], diff_rc=1
        )
        body = run.payload["discussions"][0]["body"]
        self.assertNotIn(_FENCE, body)
        self.assertIn("Return two instead.", body)
        self._assert_downgraded(run, "no_diff_oracle")

    def test_a_rerun_that_delivers_nothing_claims_no_delivery(self):
        """The bug the readout's wording caused: every discussion is already on
        the MR from an earlier run, so nothing is posted — but the body is still
        rendered, so the gate still runs and still counts. Saying "posted" there
        was a false claim."""
        finding = self._finding(end_line=2, suggested_fix_code="    return 2")
        key = post_review.finding_key(
            "foo.py",
            2,
            finding["title"],
            render_comment_body(post_review._key_material_finding(finding)),
        )
        run = self._run(
            [finding], dry_run=False, prior=(True, {key}, frozenset(), None)
        )
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)
        self.assertNotIn("fence(s) posted", run.out)

    def test_corroborator_is_gated_on_its_own_anchor(self):
        """When the group's discussion is lost, each corroborator falls back to
        its own discussion — anchored at its OWN line, so that is the range its
        fence must state."""
        primary = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "A",
            "body": "Body A",
            "consolidation_key": "foo.py:0",
            "consolidation_primary": True,
        }
        keeps = {
            "file": "foo.py",
            "line": 3,
            "end_line": 3,
            "severity": "medium",
            "title": "Keeps",
            "body": "Body B",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
            "suggested_fix_code": "    # replaced",
        }
        payloads = []
        run = self._run(
            [primary, keeps],
            dry_run=False,
            payloads=payloads,
            discussion_rcs=[1],
        )
        # The failed GROUP body renders the corroborator's title too, so the
        # fallback is the one that is NOT a group body.
        bodies = [p["body"] for p in payloads if "position" in p]
        fallback = [
            b for b in bodies if "Keeps" in b and "Corroborating finding" not in b
        ]
        self.assertEqual(len(fallback), 1)
        self.assertIn(_FENCE, fallback[0])
        self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)

    def test_a_group_body_carries_the_primarys_offset_header_once(self):
        """One discussion carries the whole group, anchored on the PRIMARY — so
        the primary's offsets are the body's, and a corroborator (which renders
        no fence at all) adds none."""
        primary = self._finding(
            consolidation_key="foo.py:0", consolidation_primary=True
        )
        corroborator = {
            "file": "foo.py",
            "line": 3,
            "end_line": 3,
            "severity": "medium",
            "title": "B",
            "body": "Body B",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
            "suggested_fix_code": "    # replaced",
        }
        body = self._run([primary, corroborator]).payload["discussions"][0]["body"]
        self.assertEqual(body.count(_FENCE), 1)
        self.assertIn("```suggestion:-0+1", body)
        self.assertIn("Corroborating finding", body)

    def test_a_fence_bearing_rerun_still_recognizes_its_own_delivery(self):
        """A finding that ships an offset header today was keyed, before #219,
        off a render that never carried a fence at all — `_key_material_finding`
        strips the field unconditionally, so the standing marker still matches
        and the discussion is not posted a second time (#132)."""
        finding = self._finding()
        key = post_review.finding_key(
            "foo.py",
            2,
            finding["title"],
            render_comment_body(post_review._key_material_finding(finding)),
        )
        run = self._run(
            [finding], dry_run=False, prior=(True, {key}, frozenset(), None)
        )
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("  1 inline discussion(s) already on the MR", run.out)
        self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)

    def test_corroborator_with_a_multi_line_range_is_offset_at_its_own_anchor(self):
        """The primary sits at line 2 and the corroborator's span at 5..6 — a
        header measured from the PRIMARY's anchor (3, 4) would be a different,
        unrealizable claim than one measured from the corroborator's own line
        (0, 1). Sharing an anchor between the two would leave this unable to
        tell which one the offsets actually came from."""
        primary = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "A",
            "body": "Body A",
            "consolidation_key": "foo.py:0",
            "consolidation_primary": True,
        }
        spanning = {
            "file": "foo.py",
            "line": 5,
            "end_line": 6,
            "severity": "medium",
            "title": "Spans",
            "body": "Body B",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
            "suggested_fix_code": "    return 2\n    # done",
        }
        payloads = []
        self._run(
            [primary, spanning],
            dry_run=False,
            diff=GL_DIFF_LONG,
            payloads=payloads,
            discussion_rcs=[1],
        )
        bodies = [p["body"] for p in payloads if "position" in p]
        fallback = [
            b for b in bodies if "Spans" in b and "Corroborating finding" not in b
        ]
        self.assertEqual(len(fallback), 1)
        # Its own fallback discussion anchors at its OWN line, so that is the
        # anchor the header's offsets are measured from.
        self.assertIn("```suggestion:-0+1", fallback[0])

    def test_unanchored_note_never_carries_a_fence(self):
        """A position-less ``/notes`` POST has nothing to apply against."""
        primary = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "A",
            "body": "Body A",
            "consolidation_key": "foo.py:0",
            "consolidation_primary": True,
        }
        unanchored = {
            "file": "foo.py",
            "severity": "medium",
            "title": "Unanchored",
            "body": "Body B",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
            "suggested_fix_code": "    # replaced",
        }
        primary_key = post_review.finding_key(
            "foo.py", 2, "A", render_comment_body(primary)
        )
        payloads = []
        self._run(
            [primary, unanchored],
            dry_run=False,
            payloads=payloads,
            prior=(True, {primary_key}, frozenset(), None),
        )
        notes = [p["body"] for p in payloads if "position" not in p]
        self.assertTrue(any("Unanchored" in b for b in notes))
        self.assertNotIn(_FENCE, "".join(notes))

    def test_body_section_entries_lose_the_fence(self):
        run = self._run([self._finding(line=999, end_line=999)])
        self.assertEqual(run.payload["discussions"], [])
        self.assertNotIn(_FENCE, run.payload["summary"]["body"])
        self._assert_downgraded(run, "range_not_in_diff", where="foo.py:999")


class TestDeliveryKeysAreFenceIndependent(_GitlabLiveRunBase):
    """Delivery keys must not depend on ``suggested_fix_code`` AT ALL (#63 D2).

    Prior-delivery dedup (#132/#208) is retry-safe only while a finding's key is
    the same across runs and across delivery shapes. Gating the field before the
    key render would make the key depend on the gate's verdict; stripping it
    unconditionally makes it depend on nothing.
    """

    def _keys(self, findings):
        payloads = []
        run = self._run_main(findings=findings, payloads=payloads)
        self.assertIsNone(run.exit_code)
        markers = [
            review_marker.find_finding_marker(p["body"])
            for p in payloads
            if "position" in p
        ]
        return [m["key"] for m in markers if m]

    def test_key_is_byte_equal_with_and_without_the_field(self):
        plain = dict(GL_CONTRACT_FINDINGS[0])
        with_fix = dict(plain, end_line=61, suggested_fix_code="patched_ctx")
        self.assertEqual(self._keys([with_fix]), self._keys([plain]))

    def test_key_is_byte_equal_whether_the_gate_kept_or_stripped_the_fence(self):
        # The stripped arm states a range the diff does not contain: since #219 a
        # 61..62 span is KEPT on GitLab (the header widens the apply range), so a
        # merely-wider range would leave both arms on the same side of the gate
        # and say nothing.
        plain = dict(GL_CONTRACT_FINDINGS[0])
        kept = dict(plain, end_line=61, suggested_fix_code="patched_ctx")
        stripped = dict(plain, end_line=999, suggested_fix_code="patched_ctx")

        def _keys_and_body(findings):
            payloads = []
            run = self._run_main(findings=findings, payloads=payloads)
            self.assertIsNone(run.exit_code)
            body = next(p["body"] for p in payloads if "position" in p)
            markers = [
                review_marker.find_finding_marker(p["body"])
                for p in payloads
                if "position" in p
            ]
            return [m["key"] for m in markers if m], body

        kept_keys, kept_body = _keys_and_body([kept])
        stripped_keys, stripped_body = _keys_and_body([stripped])
        # The two arms must actually straddle the gate — a byte-equal key over
        # two runs that landed on the SAME side of it would say nothing about
        # the field being stripped from the key render before the gate's
        # verdict is even known.
        self.assertIn(_FENCE, kept_body)
        self.assertNotIn(_FENCE, stripped_body)
        self.assertEqual(kept_keys, stripped_keys)

    def test_key_is_byte_equal_grouped_and_individual(self):
        """The #132 invariant: a member's key is its own single-finding render,
        whichever shape ships it."""
        member = dict(
            GL_CONTRACT_FINDINGS[1],
            end_line=62,
            suggested_fix_code="patched_add",
        )
        individual = self._keys([member])
        primary = dict(
            GL_CONTRACT_FINDINGS[0],
            consolidation_key="src/edited.py:60",
            consolidation_primary=True,
        )
        grouped_member = dict(
            member,
            consolidation_key="src/edited.py:60",
            consolidation_primary=False,
        )
        payloads = []
        run = self._run_main(findings=[primary, grouped_member], payloads=payloads)
        self.assertIsNone(run.exit_code)
        group_body = next(p["body"] for p in payloads if "position" in p)
        self.assertIn(
            post_review.build_finding_marker("a" * 40, individual[0]), group_body
        )


class TestFenceRun(unittest.TestCase):
    """``_fence_run`` — the factored-out fence-length rule ``_suggestion_fence``
    now delegates to (issue #226). Pinned directly, and cross-checked against
    ``_suggestion_fence``'s own open/close pair so the two can never drift."""

    def test_no_backticks_uses_the_minimum_of_three(self):
        self.assertEqual(post_review._fence_run("plain text"), "```")

    def test_a_three_run_needs_a_four_run_fence(self):
        self.assertEqual(post_review._fence_run("a ``` run"), "````")

    def test_a_four_run_needs_a_five_run_fence(self):
        self.assertEqual(post_review._fence_run("a ```` run"), "`````")

    def test_matches_suggestion_fence_open_and_close(self):
        for payload in ("plain", "has ``` three", "has ```` four"):
            fence = post_review._fence_run(payload)
            open_line, close_line = post_review._suggestion_fence(payload)
            self.assertEqual(close_line, fence)
            self.assertEqual(open_line, f"{fence}suggestion")


class TestResetRunState(unittest.TestCase):
    """``reset_run_state()`` — the one entry point that clears every
    module-level counter/log a run accumulates (issue #226): main() calls it
    instead of its old inline reset, and a second gate caller
    (scripts/report_patches.py) that never calls main() calls it directly."""

    def tearDown(self):
        post_review.reset_run_state()

    def test_clears_all_four(self):
        post_review._CAPTURED.append({"poison": True})
        post_review._SKIP_WARNINGS.append("poison")
        post_review._FIX_COUNTS["kept"] = 7
        post_review._FIX_COUNTS["downgraded"] = 3
        post_review._FIX_REASON_COUNTS["empty"] = 5

        post_review.reset_run_state()

        self.assertEqual(post_review._CAPTURED, [])
        self.assertEqual(post_review._SKIP_WARNINGS, [])
        self.assertEqual(post_review._FIX_COUNTS, {"kept": 0, "downgraded": 0})
        self.assertEqual(post_review._FIX_REASON_COUNTS, {})

    def test_main_still_resets_stale_state_from_a_prior_call(self):
        """Regression for a dropped reset_run_state() call in main(): pollute
        every counter as a PRIOR run would have left them, then drive a real
        (dry-run) main() call and assert nothing from the pollution survives
        into this run's own capture. A main() missing the reset call would
        leave the poison entries in place."""
        post_review._CAPTURED.append({"cmd_prefix": "poison", "payload": {}})
        post_review._SKIP_WARNINGS.append("poison warning from a prior run")
        post_review._FIX_COUNTS["kept"] = 99
        post_review._FIX_COUNTS["downgraded"] = 99
        post_review._FIX_REASON_COUNTS["empty"] = 99

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        findings_path = os.path.join(tmp, "findings.json")
        with open(findings_path, "w") as fh:
            json.dump(
                {
                    "platform": "github",
                    "owner": "o",
                    "repo": "r",
                    "pr_number": 1,
                    "review_body": "",
                    "findings": [],
                },
                fh,
            )

        with (
            patch.object(sys, "argv", ["post_review.py", findings_path, "--dry-run"]),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=""),
            ),
        ):
            post_review.main()

        self.assertNotIn({"cmd_prefix": "poison", "payload": {}}, post_review._CAPTURED)
        self.assertNotIn("poison warning from a prior run", post_review._SKIP_WARNINGS)
        self.assertEqual(post_review._FIX_COUNTS, {"kept": 0, "downgraded": 0})
        self.assertEqual(post_review._FIX_REASON_COUNTS, {})


class TestGatedFindingWarnLabel(unittest.TestCase):
    """``_gated_finding``'s ``warn_label`` keyword (issue #226): the default
    keeps delivery's warning bytes unchanged; a caller (the report-side gate)
    can substitute its own label so the two records stay distinguishable."""

    def setUp(self):
        post_review.reset_run_state()
        self.addCleanup(post_review.reset_run_state)

    def _finding(self):
        return {
            "file": "f.py",
            "line": 3,
            "suggested_fix_code": "",  # empty after normalization -> "empty"
        }

    def test_default_label_matches_delivery_bytes_exactly(self):
        with patch("scripts.post_review.warn_skip") as mock_warn:
            post_review._gated_finding(self._finding(), (3, 3), {}, {})
        mock_warn.assert_called_once_with("suggested-fix downgraded: f.py:3 (empty)")

    def test_custom_label_replaces_only_the_leading_word(self):
        with patch("scripts.post_review.warn_skip") as mock_warn:
            post_review._gated_finding(
                self._finding(), (3, 3), {}, {}, warn_label="report-patch"
            )
        mock_warn.assert_called_once_with("report-patch downgraded: f.py:3 (empty)")

    def test_custom_label_never_leaks_into_the_default_caller(self):
        """warn_label is per-call, not a module-level toggle: a caller that
        passes it must not change what a caller relying on the default sees."""
        post_review._gated_finding(
            self._finding(), (3, 3), {}, {}, warn_label="report-patch"
        )
        self.assertIn(
            "report-patch downgraded: f.py:3 (empty)", post_review._SKIP_WARNINGS
        )
        post_review._gated_finding(self._finding(), (3, 3), {}, {})
        self.assertIn(
            "suggested-fix downgraded: f.py:3 (empty)", post_review._SKIP_WARNINGS
        )


class TestFixReasonCounts(unittest.TestCase):
    """``_FIX_REASON_COUNTS`` — the per-reason tally the report-side gate
    (scripts/report_patches.py) reads to render a downgrade breakdown."""

    def setUp(self):
        post_review.reset_run_state()
        self.addCleanup(post_review.reset_run_state)

    def test_starts_empty(self):
        self.assertEqual(post_review._FIX_REASON_COUNTS, {})

    def test_tallies_by_reason_across_multiple_downgrades(self):
        empty = {"file": "a.py", "line": 1, "suggested_fix_code": ""}
        also_empty = {"file": "b.py", "line": 2, "suggested_fix_code": "   "}
        no_end_line = {
            "file": "c.py",
            "line": 1,
            "suggested_fix_code": "x",
        }  # no end_line -> missing_end_line

        post_review._gated_finding(empty, (1, 1), {}, {})
        post_review._gated_finding(also_empty, (2, 2), {}, {})
        post_review._gated_finding(no_end_line, None, {}, {})

        self.assertEqual(
            post_review._FIX_REASON_COUNTS,
            {"empty": 2, "missing_end_line": 1},
        )

    def test_a_kept_finding_does_not_tally(self):
        valid_lines, _, _, line_texts = _parse_fixture(
            GH_DIFF_INDENTED, platform="github"
        )
        finding = {
            "file": "foo.py",
            "line": 2,
            "end_line": 3,
            "suggested_fix_code": "    return 2\n    # done",
        }
        result = post_review._gated_finding(finding, (2, 3), valid_lines, line_texts)
        self.assertIn("suggested_fix_code", result)
        self.assertEqual(post_review._FIX_REASON_COUNTS, {})

    def test_reset_run_state_clears_the_tally(self):
        post_review._gated_finding(
            {"file": "a.py", "line": 1, "suggested_fix_code": ""}, (1, 1), {}, {}
        )
        self.assertTrue(post_review._FIX_REASON_COUNTS)
        post_review.reset_run_state()
        self.assertEqual(post_review._FIX_REASON_COUNTS, {})


if __name__ == "__main__":
    unittest.main()
