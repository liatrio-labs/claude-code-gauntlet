"""
Tests for scripts/validate_ndjson.py.

The script is invoked by Phase 3 review agents as a final self-check:
exit 0 means the findings file is parseable NDJSON; exit 1 means at least
one line is malformed (typically a literal newline, tab, or carriage return
inside a JSON string value).
"""

import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.validate_ndjson import main, validate


class _TmpFile:
    """Create a temporary file with given bytes, yielding the path."""

    def __init__(self, content):
        self._content = content
        self._path = None

    def __enter__(self):
        fd, path = tempfile.mkstemp(suffix=".ndjson")
        with os.fdopen(fd, "wb") as fh:
            fh.write(self._content)
        self._path = path
        return path

    def __exit__(self, exc_type, exc, tb):
        if self._path and os.path.exists(self._path):
            os.unlink(self._path)


class TestValidateValidInput(unittest.TestCase):
    def test_missing_file_is_ok(self):
        """Agent emitted nothing — no findings file is a valid outcome."""
        path = "/nonexistent/path/findings.ndjson"
        with patch("sys.stderr", new=io.StringIO()) as err:
            rc = validate(path)
        self.assertEqual(rc, 0)
        self.assertIn("file not found", err.getvalue())

    def test_empty_file_is_ok(self):
        with _TmpFile(b"") as path, patch("sys.stderr", new=io.StringIO()):
            rc = validate(path)
        self.assertEqual(rc, 0)

    def test_whitespace_only_file_is_ok(self):
        with _TmpFile(b"\n\n  \n") as path, patch("sys.stderr", new=io.StringIO()):
            rc = validate(path)
        self.assertEqual(rc, 0)

    def test_single_valid_finding(self):
        line = b'{"id":"bug-1","title":"X","description":"Y"}\n'
        with _TmpFile(line) as path, patch("sys.stderr", new=io.StringIO()) as err:
            rc = validate(path)
        self.assertEqual(rc, 0)
        self.assertIn("1 valid finding", err.getvalue())

    def test_multiple_valid_findings(self):
        content = (
            b'{"id":"bug-1","title":"a"}\n'
            b'{"id":"bug-2","title":"b"}\n'
            b'{"id":"bug-3","title":"c"}\n'
        )
        with _TmpFile(content) as path, patch("sys.stderr", new=io.StringIO()) as err:
            rc = validate(path)
        self.assertEqual(rc, 0)
        self.assertIn("3 valid finding", err.getvalue())

    def test_blank_lines_between_findings_ok(self):
        content = b'{"id":"bug-1"}\n\n{"id":"bug-2"}\n'
        with _TmpFile(content) as path, patch("sys.stderr", new=io.StringIO()):
            rc = validate(path)
        self.assertEqual(rc, 0)

    def test_escaped_newline_in_string_is_valid(self):
        """`\\n` (two characters) inside a string is valid JSON — the real
        newline byte is what breaks NDJSON, not the escape."""
        line = b'{"id":"bug-1","description":"Line one.\\nLine two."}\n'
        with _TmpFile(line) as path, patch("sys.stderr", new=io.StringIO()):
            rc = validate(path)
        self.assertEqual(rc, 0)

    def test_unicode_apostrophe_escape_is_valid(self):
        line = b'{"id":"bug-1","description":"doesn\\u0027t check"}\n'
        with _TmpFile(line) as path, patch("sys.stderr", new=io.StringIO()):
            rc = validate(path)
        self.assertEqual(rc, 0)


class TestValidateInvalidInput(unittest.TestCase):
    def test_literal_newline_in_string_fails(self):
        """The headline bug — agent embeds a raw \\n inside a JSON string,
        producing two physical lines neither of which parses."""
        content = b'{"id":"bug-1","description":"First sentence.\nSecond sentence."}\n'
        with _TmpFile(content) as path, patch("sys.stderr", new=io.StringIO()) as err:
            rc = validate(path)
        self.assertEqual(rc, 1)
        out = err.getvalue()
        self.assertIn("invalid line", out)
        # Diagnostic should mention escape sequences so agent knows the fix.
        self.assertIn("\\n", out)

    def test_literal_tab_in_string_fails(self):
        # A real tab byte inside a JSON string is invalid JSON.
        content = b'{"id":"bug-1","description":"col1\tcol2"}\n'
        with _TmpFile(content) as path, patch("sys.stderr", new=io.StringIO()):
            rc = validate(path)
        self.assertEqual(rc, 1)

    def test_literal_cr_in_string_fails(self):
        # \r inside a JSON string is also invalid JSON, and split() on b"\n"
        # leaves it embedded so json.loads reliably rejects it.
        content = b'{"id":"bug-1","description":"line1\rline2"}\n'
        with _TmpFile(content) as path, patch("sys.stderr", new=io.StringIO()):
            rc = validate(path)
        self.assertEqual(rc, 1)

    def test_unterminated_string_fails(self):
        content = b'{"id":"bug-1","description":"never closes\n'
        with _TmpFile(content) as path, patch("sys.stderr", new=io.StringIO()):
            rc = validate(path)
        self.assertEqual(rc, 1)

    def test_one_invalid_among_valid_reports_count(self):
        content = (
            b'{"id":"bug-1"}\n'
            b'{"id":"bug-2","description":"split\n'
            b'across lines"}\n'
            b'{"id":"bug-3"}\n'
        )
        with _TmpFile(content) as path, patch("sys.stderr", new=io.StringIO()) as err:
            rc = validate(path)
        self.assertEqual(rc, 1)
        out = err.getvalue()
        # 2 valid findings (lines 1 + 4); 2 invalid lines (the split halves on 2 + 3).
        self.assertIn("2 valid", out)
        self.assertIn("2 invalid", out)

    def test_invalid_line_diagnostic_includes_line_number(self):
        content = b'{"id":"bug-1"}\nthis is not json at all\n'
        with _TmpFile(content) as path, patch("sys.stderr", new=io.StringIO()) as err:
            rc = validate(path)
        self.assertEqual(rc, 1)
        self.assertIn("line 2", err.getvalue())

    def test_invalid_line_diagnostic_truncates_long_snippet(self):
        long_payload = b"x" * 500
        content = b'{"id":"bug-1","description":"' + long_payload + b'\nbroken"}\n'
        with _TmpFile(content) as path, patch("sys.stderr", new=io.StringIO()) as err:
            rc = validate(path)
        self.assertEqual(rc, 1)
        # Snippet truncation prevents stderr from being flooded by a single
        # giant invalid line.
        self.assertIn("...", err.getvalue())


class TestMainEntrypoint(unittest.TestCase):
    def test_no_args_returns_usage_error(self):
        with patch("sys.stderr", new=io.StringIO()) as err:
            rc = main(["validate_ndjson.py"])
        self.assertEqual(rc, 2)
        self.assertIn("Usage", err.getvalue())

    def test_too_many_args_returns_usage_error(self):
        with patch("sys.stderr", new=io.StringIO()):
            rc = main(["validate_ndjson.py", "a", "b"])
        self.assertEqual(rc, 2)

    def test_main_dispatches_to_validate(self):
        with _TmpFile(b'{"ok":true}\n') as path, patch("sys.stderr", new=io.StringIO()):
            rc = main(["validate_ndjson.py", path])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
