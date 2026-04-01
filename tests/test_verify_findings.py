"""
Tests for scripts/verify_findings.py

Covers:
  - parse_diff_lines: context, added, removed lines; multi-file diffs; edge cases
  - classify_blame: new/surfaced classification, cross-file refs, file-not-found,
    blame failures, short SHA matching, severity downgrade
  - verify_factual: file exists, file missing, binary file, no lines, out-of-range,
    symbol found/missing
  - validate_diff_lines: in-diff, out-of-diff, skipped, no line reference
  - is_line_in_diff: exact match, stripped path match, None valid_lines
  - batch_findings: grouping by file, min/max bounds, tail merging, empty input
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add project root to path so we can import scripts as a module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.verify_findings import (
    parse_diff_lines,
    is_line_in_diff,
    classify_blame,
    verify_factual,
    validate_diff_lines,
    batch_findings,
)


# ---------------------------------------------------------------------------
# parse_diff_lines
# ---------------------------------------------------------------------------

class TestParseDiffLines(unittest.TestCase):
    """Test unified diff parsing into (file, line) tuples."""

    def test_empty_input(self):
        self.assertIsNone(parse_diff_lines(""))

    def test_none_input(self):
        self.assertIsNone(parse_diff_lines(None))

    def test_added_lines(self):
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "+added_line\n"
            " line2\n"
            " line3\n"
        )
        result = parse_diff_lines(diff)
        # Context line1 at new_line=1, added at 2, context line2 at 3, context line3 at 4
        self.assertIn(("foo.py", 1), result)   # context
        self.assertIn(("foo.py", 2), result)   # added
        self.assertIn(("foo.py", 3), result)   # context
        self.assertIn(("foo.py", 4), result)   # context

    def test_removed_lines_do_not_advance_new_line(self):
        diff = (
            "diff --git a/bar.py b/bar.py\n"
            "--- a/bar.py\n"
            "+++ b/bar.py\n"
            "@@ -1,4 +1,3 @@\n"
            " line1\n"
            "-removed\n"
            " line2\n"
            " line3\n"
        )
        result = parse_diff_lines(diff)
        # context line1 at 1, removed does NOT advance, context line2 at 2, context line3 at 3
        self.assertIn(("bar.py", 1), result)
        self.assertIn(("bar.py", 2), result)
        self.assertIn(("bar.py", 3), result)
        self.assertNotIn(("bar.py", 4), result)

    def test_multiple_files(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,2 +1,3 @@\n"
            " ctx\n"
            "+new_a\n"
            " ctx2\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -10,2 +10,3 @@\n"
            " ctx\n"
            "+new_b\n"
            " ctx2\n"
        )
        result = parse_diff_lines(diff)
        self.assertIn(("a.py", 2), result)   # added in a.py
        self.assertIn(("b.py", 11), result)  # added in b.py at line 11
        self.assertIn(("b.py", 10), result)  # context in b.py

    def test_hunk_with_offset(self):
        diff = (
            "+++ b/module.ts\n"
            "@@ -100,3 +200,4 @@\n"
            " existing\n"
            "+inserted\n"
            " existing2\n"
            " existing3\n"
        )
        result = parse_diff_lines(diff)
        # new_line starts at 200: context=200, added=201, context=202, context=203
        self.assertIn(("module.ts", 200), result)
        self.assertIn(("module.ts", 201), result)
        self.assertIn(("module.ts", 202), result)
        self.assertIn(("module.ts", 203), result)

    def test_no_newline_at_eof_ignored(self):
        diff = (
            "+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            " line1\n"
            "-old\n"
            "+new\n"
            "\\ No newline at end of file\n"
        )
        result = parse_diff_lines(diff)
        self.assertIn(("f.py", 1), result)
        self.assertIn(("f.py", 2), result)
        self.assertEqual(len(result), 2)

    def test_multiple_hunks_same_file(self):
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
        result = parse_diff_lines(diff)
        self.assertIn(("multi.py", 2), result)    # added in first hunk
        self.assertIn(("multi.py", 52), result)   # added in second hunk


# ---------------------------------------------------------------------------
# is_line_in_diff
# ---------------------------------------------------------------------------

class TestIsLineInDiff(unittest.TestCase):

    def test_none_valid_lines_always_true(self):
        self.assertTrue(is_line_in_diff(None, "any.py", 999))

    def test_exact_match(self):
        valid = {("src/foo.py", 10), ("src/foo.py", 11)}
        self.assertTrue(is_line_in_diff(valid, "src/foo.py", 10))
        self.assertFalse(is_line_in_diff(valid, "src/foo.py", 12))

    def test_stripped_path_match(self):
        valid = {("src/bar.py", 5)}
        # If filepath has a/ prefix, strip it and retry
        self.assertTrue(is_line_in_diff(valid, "a/src/bar.py", 5))
        self.assertTrue(is_line_in_diff(valid, "b/src/bar.py", 5))

    def test_no_match(self):
        valid = {("x.py", 1)}
        self.assertFalse(is_line_in_diff(valid, "y.py", 1))


# ---------------------------------------------------------------------------
# classify_blame
# ---------------------------------------------------------------------------

class TestClassifyBlame(unittest.TestCase):

    def test_cross_file_refs_always_surfaced(self):
        finding = {
            "file": "a.py",
            "line_start": 1,
            "severity": "high",
            "cross_file_refs": ["b.py:10"],
        }
        result = classify_blame(finding, "main")
        self.assertEqual(result, "surfaced")
        self.assertEqual(finding["blame_metadata"]["classification"], "surfaced")
        # Severity downgraded: high -> medium
        self.assertEqual(finding["severity"], "medium")

    def test_cross_file_refs_severity_downgrade_critical(self):
        finding = {
            "file": "a.py",
            "line_start": 1,
            "severity": "critical",
            "cross_file_refs": ["b.py:10"],
        }
        classify_blame(finding, "main")
        self.assertEqual(finding["severity"], "high")

    def test_cross_file_refs_severity_low_stays_low(self):
        finding = {
            "file": "a.py",
            "line_start": 1,
            "severity": "low",
            "cross_file_refs": ["b.py:10"],
        }
        classify_blame(finding, "main")
        self.assertEqual(finding["severity"], "low")

    @patch("scripts.verify_findings.os.path.exists", return_value=False)
    def test_file_not_found_returns_new(self, _mock_exists):
        finding = {
            "file": "nonexistent.py",
            "line_start": 5,
            "severity": "high",
        }
        result = classify_blame(finding, "main")
        self.assertEqual(result, "new")
        self.assertEqual(finding["blame_metadata"]["classification"], "new")

    @patch("scripts.verify_findings.os.path.exists", return_value=True)
    @patch("scripts.verify_findings.run")
    def test_git_log_failure_returns_new(self, mock_run, _mock_exists):
        mock_run.return_value = ("", "fatal: unknown revision", 128)
        finding = {
            "file": "f.py",
            "line_start": 1,
            "severity": "medium",
        }
        result = classify_blame(finding, "nonexistent-branch")
        self.assertEqual(result, "new")

    @patch("scripts.verify_findings.os.path.exists", return_value=True)
    @patch("scripts.verify_findings.run")
    def test_new_classification_when_blame_sha_in_pr(self, mock_run, _mock_exists):
        # First call: git log (PR commits)
        # Second call: git blame
        def run_side_effect(cmd, check=False):
            if cmd[0] == "git" and cmd[1] == "log":
                return ("abc1234567890abcdef1234567890abcdef123456\n", "", 0)
            if cmd[0] == "git" and cmd[1] == "blame":
                return (
                    "abc1234 (Author 2026-03-30 10:00:00 +0000 1) code\n",
                    "", 0,
                )
            return ("", "", 0)

        mock_run.side_effect = run_side_effect
        finding = {
            "file": "f.py",
            "line_start": 1,
            "line_end": 1,
            "severity": "high",
        }
        result = classify_blame(finding, "main")
        self.assertEqual(result, "new")
        self.assertEqual(finding["severity"], "high")  # no downgrade

    @patch("scripts.verify_findings.os.path.exists", return_value=True)
    @patch("scripts.verify_findings.run")
    def test_surfaced_classification_when_blame_sha_not_in_pr(self, mock_run, _mock_exists):
        def run_side_effect(cmd, check=False):
            if cmd[0] == "git" and cmd[1] == "log":
                return ("abc1234567890abcdef1234567890abcdef123456\n", "", 0)
            if cmd[0] == "git" and cmd[1] == "blame":
                return (
                    "fffaaaa (Author 2025-01-01 10:00:00 +0000 1) old_code\n",
                    "", 0,
                )
            return ("", "", 0)

        mock_run.side_effect = run_side_effect
        finding = {
            "file": "f.py",
            "line_start": 1,
            "line_end": 1,
            "severity": "high",
        }
        result = classify_blame(finding, "main")
        self.assertEqual(result, "surfaced")
        self.assertEqual(finding["severity"], "medium")  # downgraded

    @patch("scripts.verify_findings.os.path.exists", return_value=True)
    @patch("scripts.verify_findings.run")
    def test_blame_failure_returns_new(self, mock_run, _mock_exists):
        def run_side_effect(cmd, check=False):
            if cmd[0] == "git" and cmd[1] == "log":
                return ("abc123\n", "", 0)
            if cmd[0] == "git" and cmd[1] == "blame":
                return ("", "fatal: no such path", 128)
            return ("", "", 0)

        mock_run.side_effect = run_side_effect
        finding = {
            "file": "f.py",
            "line_start": 1,
            "severity": "medium",
        }
        result = classify_blame(finding, "main")
        self.assertEqual(result, "new")

    @patch("scripts.verify_findings.os.path.exists", return_value=True)
    @patch("scripts.verify_findings.run")
    def test_blame_binary_file_returns_new(self, mock_run, _mock_exists):
        def run_side_effect(cmd, check=False):
            if cmd[0] == "git" and cmd[1] == "log":
                return ("abc123\n", "", 0)
            if cmd[0] == "git" and cmd[1] == "blame":
                return ("", "fatal: binary file", 128)
            return ("", "", 0)

        mock_run.side_effect = run_side_effect
        finding = {
            "file": "image.png",
            "line_start": 1,
            "severity": "medium",
        }
        result = classify_blame(finding, "main")
        self.assertEqual(result, "new")


# ---------------------------------------------------------------------------
# verify_factual
# ---------------------------------------------------------------------------

class TestVerifyFactual(unittest.TestCase):

    def test_no_line_reference_skips(self):
        finding = {"file": "f.py", "description": "something"}
        result = verify_factual(finding)
        self.assertTrue(result)
        self.assertTrue(finding["factual_verification"]["verified"])
        self.assertIn("no line reference", finding["factual_verification"]["reason"])

    def test_file_not_found_eliminates(self):
        finding = {
            "file": "/nonexistent/path.py",
            "line_start": 1,
            "description": "bug here",
        }
        result = verify_factual(finding)
        self.assertFalse(result)
        self.assertEqual(finding["confidence"], 0)

    def test_empty_filepath_eliminates(self):
        finding = {
            "file": "",
            "line_start": 1,
            "description": "bug",
        }
        result = verify_factual(finding)
        self.assertFalse(result)

    def test_line_out_of_range_eliminates(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("line1\nline2\n")
            tmppath = f.name
        try:
            finding = {
                "file": tmppath,
                "line_start": 999,
                "description": "something at line 999",
            }
            result = verify_factual(finding)
            self.assertFalse(result)
            self.assertEqual(finding["confidence"], 0)
            self.assertIn("out of range", finding["factual_verification"]["reason"])
        finally:
            os.unlink(tmppath)

    def test_valid_file_and_lines_verified(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def hello():\n    pass\n")
            tmppath = f.name
        try:
            # Patch grep to simulate symbol found
            with patch("scripts.verify_findings.run") as mock_run:
                mock_run.return_value = ("found.py:1:hello\n", "", 0)
                finding = {
                    "file": tmppath,
                    "line_start": 1,
                    "line_end": 2,
                    "description": "The `hello` function does nothing",
                    "evidence": "see line 1",
                }
                result = verify_factual(finding)
                self.assertTrue(result)
                self.assertTrue(finding["factual_verification"]["verified"])
        finally:
            os.unlink(tmppath)

    def test_symbol_in_code_at_lines_fast_path(self):
        """Symbol found in the lines read from disk should skip grep for that symbol."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def calculate_total():\n    return 42\n")
            tmppath = f.name
        try:
            with patch("scripts.verify_findings.run") as mock_run:
                # grep returns a match for any symbol queried
                mock_run.return_value = ("match.py:1:found\n", "", 0)
                finding = {
                    "file": tmppath,
                    "line_start": 1,
                    "line_end": 2,
                    "description": "the `calculate_total` function always returns 42",
                    "evidence": "",
                }
                result = verify_factual(finding)
                self.assertTrue(result)
                self.assertTrue(finding["factual_verification"]["verified"])
        finally:
            os.unlink(tmppath)

    def test_missing_symbol_sets_confidence_zero(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def hello():\n    pass\n")
            tmppath = f.name
        try:
            with patch("scripts.verify_findings.run") as mock_run:
                # grep returns no match for every call
                mock_run.return_value = ("", "", 1)
                finding = {
                    "file": tmppath,
                    "line_start": 1,
                    "line_end": 2,
                    "description": "The `NonExistentClass` method fails",
                    "evidence": "",
                    "confidence": 85,
                }
                result = verify_factual(finding)
                self.assertTrue(result)  # kept but degraded
                self.assertEqual(finding["confidence"], 0)
                self.assertFalse(finding["factual_verification"]["verified"])
                self.assertIn(
                    "not found in codebase",
                    finding["factual_verification"]["reason"],
                )
        finally:
            os.unlink(tmppath)

    def test_binary_file_skips_verification(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"\x00\x01\x02\xff\xfe")
            tmppath = f.name
        try:
            finding = {
                "file": tmppath,
                "line_start": 1,
                "description": "binary issue",
            }
            result = verify_factual(finding)
            self.assertTrue(result)
            self.assertIn("binary", finding["factual_verification"]["reason"])
        finally:
            os.unlink(tmppath)


# ---------------------------------------------------------------------------
# validate_diff_lines
# ---------------------------------------------------------------------------

class TestValidateDiffLines(unittest.TestCase):

    def test_none_valid_lines_skips(self):
        finding = {"file": "f.py", "line_start": 10}
        result = validate_diff_lines(finding, None)
        self.assertTrue(result)
        self.assertIsNone(finding["diff_validation"]["in_diff"])

    def test_no_line_reference_passes(self):
        finding = {"file": "f.py"}
        result = validate_diff_lines(finding, set())
        self.assertTrue(result)
        self.assertTrue(finding["diff_validation"]["in_diff"])

    def test_line_in_diff(self):
        valid = {("src/app.py", 42)}
        finding = {"file": "src/app.py", "line_start": 42, "line_end": 42}
        result = validate_diff_lines(finding, valid)
        self.assertTrue(result)
        self.assertTrue(finding["diff_validation"]["in_diff"])

    def test_line_not_in_diff_tags_surfaced(self):
        valid = {("src/app.py", 100)}
        finding = {
            "file": "src/app.py",
            "line_start": 500,
            "line_end": 505,
            "origin": "new",
            "severity": "high",
        }
        result = validate_diff_lines(finding, valid)
        self.assertTrue(result)  # always True
        self.assertEqual(finding["origin"], "surfaced")
        self.assertFalse(finding["diff_validation"]["in_diff"])
        # Severity downgraded
        self.assertEqual(finding["severity"], "medium")

    def test_partial_overlap_counts_as_in_diff(self):
        valid = {("f.py", 12)}
        finding = {"file": "f.py", "line_start": 10, "line_end": 15}
        result = validate_diff_lines(finding, valid)
        self.assertTrue(result)
        self.assertTrue(finding["diff_validation"]["in_diff"])

    def test_no_double_downgrade_when_blame_already_surfaced(self):
        valid = {("x.py", 100)}
        finding = {
            "file": "x.py",
            "line_start": 50,
            "line_end": 55,
            "origin": "new",
            "severity": "high",
            "blame_metadata": {"classification": "surfaced"},
        }
        validate_diff_lines(finding, valid)
        # Blame already classified as surfaced, so no additional downgrade
        self.assertEqual(finding["severity"], "high")


# ---------------------------------------------------------------------------
# batch_findings
# ---------------------------------------------------------------------------

class TestBatchFindings(unittest.TestCase):

    def test_empty_input(self):
        self.assertEqual(batch_findings([]), [])

    def test_single_finding(self):
        findings = [{"id": "f1", "file": "a.py", "line_start": 1}]
        batches = batch_findings(findings)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0], ["f1"])

    def test_same_file_grouped(self):
        findings = [
            {"id": "f1", "file": "a.py", "line_start": 1},
            {"id": "f2", "file": "a.py", "line_start": 10},
            {"id": "f3", "file": "a.py", "line_start": 20},
        ]
        batches = batch_findings(findings)
        self.assertEqual(len(batches), 1)
        self.assertIn("f1", batches[0])
        self.assertIn("f2", batches[0])
        self.assertIn("f3", batches[0])

    def test_max_batch_size_respected(self):
        findings = [
            {"id": f"f{i}", "file": "a.py", "line_start": i}
            for i in range(1, 8)
        ]
        batches = batch_findings(findings, min_batch=3, max_batch=5)
        for batch in batches:
            self.assertLessEqual(len(batch), 5)

    def test_different_files_split(self):
        findings = [
            {"id": "a1", "file": "a.py", "line_start": 1},
            {"id": "a2", "file": "a.py", "line_start": 2},
            {"id": "a3", "file": "a.py", "line_start": 3},
            {"id": "b1", "file": "b.py", "line_start": 1},
            {"id": "b2", "file": "b.py", "line_start": 2},
            {"id": "b3", "file": "b.py", "line_start": 3},
        ]
        batches = batch_findings(findings, min_batch=3, max_batch=5)
        self.assertEqual(len(batches), 2)

    def test_tail_merging(self):
        """Small tail batch should merge into previous if combined fits max_batch."""
        findings = [
            {"id": "a1", "file": "a.py", "line_start": 1},
            {"id": "a2", "file": "a.py", "line_start": 2},
            {"id": "a3", "file": "a.py", "line_start": 3},
            {"id": "b1", "file": "b.py", "line_start": 1},
        ]
        batches = batch_findings(findings, min_batch=3, max_batch=5)
        # Tail batch [b1] has <3 items, should merge into previous
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 4)

    def test_tail_too_large_to_merge(self):
        """Small tail batch should stay separate if merging exceeds max_batch."""
        findings = [
            {"id": f"a{i}", "file": "a.py", "line_start": i}
            for i in range(1, 6)
        ] + [
            {"id": "b1", "file": "b.py", "line_start": 1},
            {"id": "b2", "file": "b.py", "line_start": 2},
        ]
        batches = batch_findings(findings, min_batch=3, max_batch=5)
        # First batch: 5 items (a.py), second batch: 2 items (b.py) - can't merge (7 > 5)
        self.assertEqual(len(batches), 2)

    def test_finding_id_fallback(self):
        """Findings without 'id' should use index as fallback."""
        findings = [
            {"file": "a.py", "line_start": 1},
            {"file": "a.py", "line_start": 2},
        ]
        batches = batch_findings(findings)
        self.assertEqual(len(batches), 1)
        # The batch should contain string IDs (index-based fallback)
        self.assertEqual(len(batches[0]), 2)

    def test_sort_order_by_file_then_line(self):
        findings = [
            {"id": "z1", "file": "z.py", "line_start": 1},
            {"id": "a1", "file": "a.py", "line_start": 100},
            {"id": "a2", "file": "a.py", "line_start": 1},
        ]
        batches = batch_findings(findings)
        # After sorting: a.py:1, a.py:100, z.py:1
        # All 3 in one batch (< min_batch=3, but only 3 total)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0][0], "a2")
        self.assertEqual(batches[0][1], "a1")
        self.assertEqual(batches[0][2], "z1")


if __name__ == "__main__":
    unittest.main()
