"""
Tests for scripts/diff_lines.py

Every case asserts the WHOLE event stream, not a membership sample: the defects this
walk exists to prevent (a body line read as a header, a budget drained one line early,
an unresolved hunk count) all show up as a wrong event somewhere in the middle of an
otherwise plausible list, which an assertIn-shaped test reads straight past.

Covers:
  - header zone: `---`/`+++`/`@@` recognition, verbatim paths, `/dev/null`, noise
  - header wire spelling: the TAB terminator and C-quoting git writes for a path
    holding a space, a control character or a non-ASCII byte
  - hunk budgets: resolved counts, omitted counts, drain across files
  - hunk body: added/removed/context line numbering, header-shaped body content,
    `\\ No newline at end of file`, form feeds and friends, a body cut short
  - line text: marker-column removal, the zero-prefixed context exception,
    header/hunk events carry none
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
# Header wire spelling
# ---------------------------------------------------------------------------


class TestHeaderPathSpelling(unittest.TestCase):
    """Every header below is what `git diff` really writes for that filename.

    A path git had to encode reaches the caller as a string no finding can name, so
    the file's whole set of addressable lines is keyed under a spelling nothing
    matches — the same silent "not in the diff" outcome as failing to match the
    header at all.
    """

    def test_a_space_in_the_path_ends_the_field_with_a_tab(self):
        diff = "--- a/My Docs/read me.md\t\n+++ b/My Docs/read me.md\t\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("old_path", path="a/My Docs/read me.md"),
                DiffEvent("new_path", path="b/My Docs/read me.md"),
            ],
        )

    def test_non_ascii_bytes_arrive_c_quoted_and_octal_escaped(self):
        # Default `core.quotePath`. The octal escapes name the two UTF-8 bytes of
        # "é" individually, so they resolve to one character, not two.
        diff = '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n'
        self.assertEqual(
            events(diff),
            [
                DiffEvent("old_path", path="a/café.py"),
                DiffEvent("new_path", path="b/café.py"),
            ],
        )

    def test_quoting_survives_a_tab_a_quote_and_a_backslash_in_the_name(self):
        # Control characters and the quoting characters themselves are C-quoted
        # whatever `core.quotePath` says.
        diff = (
            '+++ "b/tab\\there.txt"\n+++ "b/quo\\"te.txt"\n+++ "b/back\\\\slash.txt"\n'
        )
        self.assertEqual(
            events(diff),
            [
                DiffEvent("new_path", path="b/tab\there.txt"),
                DiffEvent("new_path", path='b/quo"te.txt'),
                DiffEvent("new_path", path="b/back\\slash.txt"),
            ],
        )

    def test_a_quoted_path_carries_its_tab_outside_the_closing_quote(self):
        diff = '+++ "b/caf\\303\\251 space.py"\t\n'
        self.assertEqual(
            events(diff),
            [DiffEvent("new_path", path="b/café space.py")],
        )

    def test_a_field_that_does_not_decode_is_yielded_verbatim(self):
        # None of these is an escape git writes: `\q` names nothing, `\400` is past a
        # byte, `\377` alone is not valid UTF-8, and a backslash cannot be the last
        # thing inside the quotes. Guessing at any of them would invent a path — and a
        # verbatim field just matches nothing, which is what an unreadable path is.
        diff = (
            '--- "a/bad\\q.py"\n'
            '+++ "b/\\377.py"\n'
            '+++ "b/\\400.py"\n'
            '+++ "b/trailing\\"\n'
        )
        self.assertEqual(
            events(diff),
            [
                DiffEvent("old_path", path='"a/bad\\q.py"'),
                DiffEvent("new_path", path='"b/\\377.py"'),
                DiffEvent("new_path", path='"b/\\400.py"'),
                DiffEvent("new_path", path='"b/trailing\\"'),
            ],
        )

    def test_quotes_inside_an_unquoted_path_are_left_alone(self):
        # `glab mr diff` writes paths verbatim; only a field git itself quoted opens
        # and closes with a quote.
        diff = '+++ say"hi".py\n+++ "quoted"/app.py\n'
        self.assertEqual(
            events(diff),
            [
                DiffEvent("new_path", path='say"hi".py'),
                DiffEvent("new_path", path='"quoted"/app.py'),
            ],
        )


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
                DiffEvent("line", old_line=10, new_line=20, text="ctx"),
                DiffEvent("line", new_line=21, text="added"),
                DiffEvent("line", old_line=11, new_line=22, text="tail"),
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
                DiffEvent("line", new_line=1, text="only"),
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
                DiffEvent("line", old_line=1, new_line=1, text="a"),
                DiffEvent("line", new_line=2, text="b"),
                DiffEvent("line", old_line=2, new_line=3, text="c"),
                DiffEvent("hunk", old_line=50, new_line=51, old_count=2, new_count=3),
                DiffEvent("line", old_line=50, new_line=51, text="d"),
                DiffEvent("line", new_line=52, text="e"),
                DiffEvent("line", old_line=51, new_line=53, text="f"),
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
                DiffEvent("line", old_line=7, new_line=7, text="ctx"),
                DiffEvent("line", old_line=8, text="gone"),
                DiffEvent("line", new_line=8, text="fresh"),
                DiffEvent("line", old_line=9, new_line=9, text="tail"),
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
                DiffEvent("line", old_line=1, new_line=1, text="a"),
                DiffEvent("line", old_line=2, text="b"),
                DiffEvent("line", new_line=2, text="b2"),
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
                DiffEvent("line", old_line=1, new_line=1, text="CREATE TABLE t ("),
                DiffEvent("line", old_line=2, text="-- deprecated: drop me"),
                DiffEvent("line", old_line=3, new_line=2, text=");"),
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
                DiffEvent("line", old_line=1, new_line=1, text="intro"),
                DiffEvent("line", new_line=2, text="++ x marks a diff-of-a-diff"),
                DiffEvent("line", old_line=2, new_line=3, text="outro"),
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
                DiffEvent("line", old_line=1, new_line=1, text="head"),
                DiffEvent("line", old_line=2, text="alpha\x0cbeta"),
                DiffEvent("line", new_line=2, text="gamma"),
                DiffEvent("line", old_line=3, new_line=3, text="middle"),
                DiffEvent("line", old_line=4, text="omega"),
            ],
        )

    def test_a_hunk_body_cut_short_mints_no_extra_line(self):
        # A diff can end mid-hunk — a `--diff-file` truncated to a byte budget, or one
        # page of a paginated API diff. The hunk here declares four new-side lines and
        # supplies two, so the terminating newline's tail is still inside the body zone
        # and reads as a context line: a line number the file may not even have, which
        # a finding can then match.
        diff = "--- a/f.py\n+++ b/f.py\n@@ -1,4 +1,4 @@\n ctx\n+added\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("old_path", path="a/f.py"),
                DiffEvent("new_path", path="b/f.py"),
                DiffEvent("hunk", old_line=1, new_line=1, old_count=4, new_count=4),
                DiffEvent("line", old_line=1, new_line=1, text="ctx"),
                DiffEvent("line", new_line=2, text="added"),
            ],
        )

    def test_a_stream_that_ends_without_a_newline_keeps_its_last_line(self):
        # Only a TERMINATING newline leaves a tail to drop; a stream that ends on
        # content ends with the line itself, which is a real body line.
        diff = "@@ -1,2 +1,2 @@\n ctx\n+added"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("hunk", old_line=1, new_line=1, old_count=2, new_count=2),
                DiffEvent("line", old_line=1, new_line=1, text="ctx"),
                DiffEvent("line", new_line=2, text="added"),
            ],
        )

    def test_a_body_line_that_is_empty_still_drains_its_budget(self):
        # The line before the guard above: an empty CONTEXT line reaches some readers
        # with its leading space stripped, and dropping it would drain nothing and shift
        # every line after it. Only the split artifact at the very end is not a line.
        diff = "@@ -1,3 +1,3 @@\n a\n\n+b\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("hunk", old_line=1, new_line=1, old_count=3, new_count=3),
                DiffEvent("line", old_line=1, new_line=1, text="a"),
                DiffEvent("line", old_line=2, new_line=2, text=""),
                DiffEvent("line", new_line=3, text="b"),
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
                DiffEvent("line", old_line=1, text="alpha"),
                DiffEvent("line", old_line=2, text="beta"),
                DiffEvent("line", old_line=3, text="gamma"),
                DiffEvent("old_path", path="a/next.py"),
                DiffEvent("new_path", path="b/next.py"),
                DiffEvent("hunk", old_line=10, new_line=10, old_count=1, new_count=2),
                DiffEvent("line", old_line=10, new_line=10, text="ctx"),
                DiffEvent("line", new_line=11, text="added"),
            ],
        )


# ---------------------------------------------------------------------------
# Line text
# ---------------------------------------------------------------------------


class TestLineText(unittest.TestCase):
    """``text`` is a ``"line"`` event's body with the marker column removed.

    Every case above already pins ``text`` alongside the numbering it exercises;
    these tests isolate the marker-stripping rule itself, one shape at a time.
    """

    def test_added_line_text_drops_the_leading_plus(self):
        diff = "@@ -0,0 +1 @@\n+hello\n"
        self.assertEqual(events(diff)[1], DiffEvent("line", new_line=1, text="hello"))

    def test_removed_line_text_drops_the_leading_minus(self):
        diff = "@@ -1 +0,0 @@\n-hello\n"
        self.assertEqual(events(diff)[1], DiffEvent("line", old_line=1, text="hello"))

    def test_context_line_text_drops_exactly_one_leading_space(self):
        diff = "@@ -1 +1 @@\n  indented\n"
        self.assertEqual(
            events(diff)[1],
            DiffEvent("line", old_line=1, new_line=1, text=" indented"),
        )

    def test_blank_context_line_written_as_a_lone_space_yields_empty_text(self):
        # A unified diff spells a blank context line as a lone space — its content
        # is the empty string, not a space.
        diff = "@@ -1 +1 @@\n \n"
        self.assertEqual(
            events(diff)[1], DiffEvent("line", old_line=1, new_line=1, text="")
        )

    def test_zero_prefixed_bare_context_line_yields_empty_text_and_drains(self):
        # Some producers write a blank context line with no marker column at all
        # (the bare empty string). Slicing an empty string is safe and would also
        # yield "" here, so this case alone does not distinguish sliced from
        # unsliced — it only pins that the empty line still drains the hunk's
        # declared budget, and that its text comes out as "" either way.
        diff = "@@ -1,2 +1,2 @@\n\n ctx\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("hunk", old_line=1, new_line=1, old_count=2, new_count=2),
                DiffEvent("line", old_line=1, new_line=1, text=""),
                DiffEvent("line", old_line=2, new_line=2, text="ctx"),
            ],
        )

    def test_bare_context_line_with_content_keeps_its_first_character(self):
        # A body line with no marker column at all is passed through, not sliced:
        # slicing would eat a content character as if it were a marker.
        diff = "@@ -1,2 +1,2 @@\nbare_ctx\n ctx\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("hunk", old_line=1, new_line=1, old_count=2, new_count=2),
                DiffEvent("line", old_line=1, new_line=1, text="bare_ctx"),
                DiffEvent("line", old_line=2, new_line=2, text="ctx"),
            ],
        )

    def test_leading_whitespace_after_the_marker_is_preserved_verbatim(self):
        diff = "@@ -0,0 +1 @@\n+    indented_add\n"
        self.assertEqual(
            events(diff)[1], DiffEvent("line", new_line=1, text="    indented_add")
        )

    def test_form_feed_inside_the_text_is_preserved(self):
        diff = "@@ -0,0 +1 @@\n+a\x0cb\n"
        self.assertEqual(events(diff)[1], DiffEvent("line", new_line=1, text="a\x0cb"))

    def test_no_newline_marker_yields_no_event(self):
        diff = "@@ -1,2 +1,2 @@\n a\n\\ No newline at end of file\n b\n"
        self.assertEqual(
            events(diff),
            [
                DiffEvent("hunk", old_line=1, new_line=1, old_count=2, new_count=2),
                DiffEvent("line", old_line=1, new_line=1, text="a"),
                DiffEvent("line", old_line=2, new_line=2, text="b"),
            ],
        )

    def test_header_and_hunk_events_carry_no_text(self):
        diff = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n ctx\n"
        old_path, new_path, hunk, line = events(diff)
        self.assertIsNone(old_path.text)
        self.assertIsNone(new_path.text)
        self.assertIsNone(hunk.text)
        self.assertEqual(line.text, "ctx")


if __name__ == "__main__":
    unittest.main()
