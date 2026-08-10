"""
Tests for scripts/diff_lines.py

Every case asserts the WHOLE event stream, not a membership sample: the defects this
walk exists to prevent (a body line read as a header, a budget drained one line early,
an unresolved hunk count) all show up as a wrong event somewhere in the middle of an
otherwise plausible list, which an assertIn-shaped test reads straight past.

Covers:
  - header zone: `---`/`+++`/`@@` recognition, verbatim paths, `/dev/null`, noise
  - hunk budgets: resolved counts, omitted counts, drain across files
  - hunk body: added/removed/context line numbering, header-shaped body content,
    `\\ No newline at end of file`, form feeds and friends
"""

import os
import sys
import unittest

# Add project root to path so we can import scripts as a module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.diff_lines import DiffEvent, walk_diff


def events(diff_text):
    return list(walk_diff(diff_text))


# ---------------------------------------------------------------------------
# Header zone
# ---------------------------------------------------------------------------


class TestHeaderZone(unittest.TestCase):
    def test_empty_text_yields_nothing(self):
        self.assertEqual(events(""), [])

    def test_paths_are_yielded_verbatim(self):
        # `gh pr diff` spelling. The synthetic `a/`/`b/` is diff syntax, but deciding
        # that is the caller's job — the walk must not strip it.
        diff = "--- a/src/app.py\n+++ b/src/app.py\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("old_path", path="a/src/app.py"),
                DiffEvent("new_path", path="b/src/app.py"),
            ],
        )

    def test_unprefixed_paths_are_yielded_verbatim(self):
        # `glab mr diff` spelling: paths verbatim, no synthetic prefix anywhere.
        diff = "--- src/app.py\n+++ src/app.py\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("old_path", path="src/app.py"),
                DiffEvent("new_path", path="src/app.py"),
            ],
        )

    def test_dev_null_is_a_path_like_any_other(self):
        diff = "--- /dev/null\n+++ b/added.py\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("old_path", path="/dev/null"),
                DiffEvent("new_path", path="b/added.py"),
            ],
        )

    def test_between_hunk_noise_yields_nothing(self):
        diff = (
            "diff --git a/logo.png b/logo.png\n"
            "old mode 100644\n"
            "new mode 100755\n"
            "index 1111111..2222222\n"
            "similarity index 94%\n"
            "Binary files a/logo.png and b/logo.png differ\n"
        )
        self.assertEqual(events(diff), [])


# ---------------------------------------------------------------------------
# Hunk headers
# ---------------------------------------------------------------------------


class TestHunkHeaders(unittest.TestCase):
    def test_counts_are_resolved_not_raw_groups(self):
        # The trailing section heading after the closing `@@` is git's, and is not part
        # of the match.
        diff = "@@ -10,2 +20,3 @@ def handler():\n ctx\n+added\n tail\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("hunk", old_line=10, new_line=20, old_count=2, new_count=3),
                DiffEvent("line", old_line=10, new_line=20),
                DiffEvent("line", new_line=21),
                DiffEvent("line", old_line=11, new_line=22),
            ],
        )

    def test_omitted_counts_default_to_one(self):
        # A unified diff omits a count exactly when that side holds ONE line. The
        # regex group is None there; a caller must never see that spelling — an
        # added-file signal compares the old count against 0, and `None` is not a
        # number. `@@ -0,0 +1 @@` is what real git writes for a one-line added file.
        diff = "@@ -0,0 +1 @@\n+only\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("hunk", old_line=0, new_line=1, old_count=0, new_count=1),
                DiffEvent("line", new_line=1),
            ],
        )

    def test_second_hunk_header_is_read_only_after_the_first_body_drains(self):
        diff = (
            "+++ b/multi.py\n"
            "@@ -1,2 +1,3 @@\n"
            " a\n"
            "+b\n"
            " c\n"
            "@@ -50,2 +51,3 @@\n"
            " d\n"
            "+e\n"
            " f\n"
        )
        self.assertEqual(
            events(diff),
            [
                DiffEvent("new_path", path="b/multi.py"),
                DiffEvent("hunk", old_line=1, new_line=1, old_count=2, new_count=3),
                DiffEvent("line", old_line=1, new_line=1),
                DiffEvent("line", new_line=2),
                DiffEvent("line", old_line=2, new_line=3),
                DiffEvent("hunk", old_line=50, new_line=51, old_count=2, new_count=3),
                DiffEvent("line", old_line=50, new_line=51),
                DiffEvent("line", new_line=52),
                DiffEvent("line", old_line=51, new_line=53),
            ],
        )


# ---------------------------------------------------------------------------
# Hunk body
# ---------------------------------------------------------------------------


class TestHunkBody(unittest.TestCase):
    def test_added_removed_and_context_lines_carry_the_sides_they_exist_on(self):
        diff = "@@ -7,3 +7,3 @@\n ctx\n-gone\n+fresh\n tail\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("hunk", old_line=7, new_line=7, old_count=3, new_count=3),
                DiffEvent("line", old_line=7, new_line=7),
                DiffEvent("line", old_line=8),
                DiffEvent("line", new_line=8),
                DiffEvent("line", old_line=9, new_line=9),
            ],
        )

    def test_no_newline_marker_belongs_to_neither_side(self):
        # Mid-body placement is deliberate: at the very end of a hunk the budgets are
        # already drained and the marker is skipped as header-zone noise instead, so
        # only this shape exercises the body-zone branch.
        diff = "@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+b2\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("hunk", old_line=1, new_line=1, old_count=2, new_count=2),
                DiffEvent("line", old_line=1, new_line=1),
                DiffEvent("line", old_line=2),
                DiffEvent("line", new_line=2),
            ],
        )

    def test_removed_line_whose_content_starts_with_dashes_is_not_a_header(self):
        # The SQL comment `-- deprecated: drop me`, removed, renders as
        # `--- deprecated: drop me`: by prefix alone it is a file header.
        diff = (
            "--- a/schema.sql\n"
            "+++ b/schema.sql\n"
            "@@ -1,3 +1,2 @@\n"
            " CREATE TABLE t (\n"
            "--- deprecated: drop me\n"
            " );\n"
        )
        self.assertEqual(
            events(diff),
            [
                DiffEvent("old_path", path="a/schema.sql"),
                DiffEvent("new_path", path="b/schema.sql"),
                DiffEvent("hunk", old_line=1, new_line=1, old_count=3, new_count=2),
                DiffEvent("line", old_line=1, new_line=1),
                DiffEvent("line", old_line=2),
                DiffEvent("line", old_line=3, new_line=2),
            ],
        )

    def test_added_line_whose_content_starts_with_pluses_is_not_a_header(self):
        # Symmetrically, an added `++ x` renders as `+++ x`. Read as a header it would
        # rename the file mid-hunk and every later line would be attributed to `x`.
        diff = (
            "--- a/notes.md\n"
            "+++ b/notes.md\n"
            "@@ -1,2 +1,3 @@\n"
            " intro\n"
            "+++ x marks a diff-of-a-diff\n"
            " outro\n"
        )
        self.assertEqual(
            events(diff),
            [
                DiffEvent("old_path", path="a/notes.md"),
                DiffEvent("new_path", path="b/notes.md"),
                DiffEvent("hunk", old_line=1, new_line=1, old_count=2, new_count=3),
                DiffEvent("line", old_line=1, new_line=1),
                DiffEvent("line", new_line=2),
                DiffEvent("line", old_line=2, new_line=3),
            ],
        )

    def test_form_feed_inside_a_hunk_body_is_content_not_a_line_break(self):
        # git splits lines on "\n" alone. str.splitlines() also breaks on \x0c, so the
        # single removed line below becomes two: the fragment `beta` reads as a CONTEXT
        # line, drains a new-side budget the hunk never owed and mints a line number
        # that does not exist. Every line after it inherits the shift, and the hunk runs
        # out of new-side budget one line early, so its last body line falls into the
        # header zone and vanishes.
        diff = "@@ -1,4 +1,3 @@\n head\n-alpha\x0cbeta\n+gamma\n middle\n-omega\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("hunk", old_line=1, new_line=1, old_count=4, new_count=3),
                DiffEvent("line", old_line=1, new_line=1),
                DiffEvent("line", old_line=2),
                DiffEvent("line", new_line=2),
                DiffEvent("line", old_line=3, new_line=3),
                DiffEvent("line", old_line=4),
            ],
        )

    def test_deleted_file_body_drains_so_the_next_file_parses(self):
        # `+++ /dev/null` records nothing, but its body still owes the old side three
        # lines. Skipping the body outright leaves the budget undrained, and the NEXT
        # file's headers then arrive inside the hunk-body zone where nothing matches
        # them — the whole following file disappears.
        diff = (
            "diff --git a/gone.py b/gone.py\n"
            "deleted file mode 100644\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,3 +0,0 @@\n"
            "-alpha\n"
            "-beta\n"
            "-gamma\n"
            "diff --git a/next.py b/next.py\n"
            "--- a/next.py\n"
            "+++ b/next.py\n"
            "@@ -10,1 +10,2 @@\n"
            " ctx\n"
            "+added\n"
        )
        self.assertEqual(
            events(diff),
            [
                DiffEvent("old_path", path="a/gone.py"),
                DiffEvent("new_path", path="/dev/null"),
                DiffEvent("hunk", old_line=1, new_line=0, old_count=3, new_count=0),
                DiffEvent("line", old_line=1),
                DiffEvent("line", old_line=2),
                DiffEvent("line", old_line=3),
                DiffEvent("old_path", path="a/next.py"),
                DiffEvent("new_path", path="b/next.py"),
                DiffEvent("hunk", old_line=10, new_line=10, old_count=1, new_count=2),
                DiffEvent("line", old_line=10, new_line=10),
                DiffEvent("line", new_line=11),
            ],
        )


if __name__ == "__main__":
    unittest.main()
