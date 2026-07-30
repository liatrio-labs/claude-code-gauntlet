"""
Tests for scripts/verify_findings.py

Covers:
  - parse_diff_lines: context, added, removed lines; multi-file diffs; edge cases
  - classify_blame: new/surfaced classification, cross-file refs, file-not-found,
    blame failures, short SHA matching, severity downgrade
  - verify_factual: file exists, file missing, binary file, no lines, out-of-range,
    symbol found/missing
  - _extract_symbols: tiered extraction (V5-05)
  - validate_diff_lines: in-diff, out-of-diff, skipped, no line reference
  - is_line_in_diff: exact match, stripped path match, None valid_lines
  - batch_findings: grouping by file, min/max bounds, tail merging, empty input
"""

import os
import subprocess
import json
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
    get_diff,
    _extract_symbols,
    _write_output,
    _coerce_numeric_fields,
    load_input,
    run,
    run_verification,
    build_deltas,
    deltas_checksum,
    _delta_confidence,
    _DELTA_FIELDS,
    InputError,
    REPO_ROOT,
)
# JS_MAX_SAFE_INTEGER is the same constant _delta_confidence refuses to exceed --
# imported from the sibling module rather than re-hardcoded so the two never drift.
from scripts.assemble_artifacts import JS_MAX_SAFE_INTEGER

# Absolute path to the script file, for the delta-echo tests that must invoke the REAL
# CLI as a subprocess rather than calling main() in-process with a patched argv: the
# result key ORDER on disk and the sibling assemble_artifacts import both depend on how
# the interpreter actually loads and runs this file, which an in-process main() call
# (used by TestReceipt above) cannot exercise.
SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts", "verify_findings.py")
)


# ---------------------------------------------------------------------------
# parse_diff_lines
# ---------------------------------------------------------------------------

class TestParseDiffLines(unittest.TestCase):
    """Test unified diff parsing into (file, line) tuples."""

    def test_empty_input_returns_empty_set(self):
        # RF-04: empty diff string means diff was retrieved but has no content;
        # return empty set so all findings are tagged "surfaced" (not skipped).
        self.assertEqual(parse_diff_lines(""), set())

    def test_none_input_returns_none(self):
        # RF-04: None means diff retrieval failed; return None to skip validation.
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
# _extract_symbols (V5-05 tiered extraction)
# ---------------------------------------------------------------------------

class TestExtractSymbols(unittest.TestCase):
    """V5-05: Tiered symbol extraction tests."""

    def test_backtick_symbols_extracted(self):
        """Tier 1: backtick-delimited identifiers are extracted."""
        symbols = _extract_symbols(
            "The `calculate_total` function calls `process_item`",
            "",
        )
        self.assertIn("calculate_total", symbols)
        self.assertIn("process_item", symbols)

    def test_triple_backtick_code_block_extracted(self):
        """Tier 1: identifiers inside triple-backtick code blocks are extracted."""
        desc = (
            "The code does:\n"
            "```python\n"
            "result = my_function(arg_value)\n"
            "```\n"
        )
        symbols = _extract_symbols(desc, "")
        self.assertIn("my_function", symbols)
        self.assertIn("arg_value", symbols)

    def test_dotted_backtick_path_split(self):
        """Tier 1: dotted paths in backticks are split and each part extracted."""
        symbols = _extract_symbols("Uses `os.path.join` to build paths", "")
        self.assertIn("path", symbols)
        self.assertIn("join", symbols)

    def test_snake_case_extracted(self):
        """Tier 2: snake_case tokens are extracted (contain underscore)."""
        symbols = _extract_symbols(
            "The get_user_data function is called",
            "",
        )
        self.assertIn("get_user_data", symbols)

    def test_code_punctuation_extracted(self):
        """Tier 2: tokens with code punctuation (., (), ::) are extracted."""
        symbols = _extract_symbols(
            "Calling obj.method() and Foo::bar are common patterns",
            "",
        )
        self.assertIn("obj", symbols)
        self.assertIn("method", symbols)
        self.assertIn("Foo", symbols)
        self.assertIn("bar", symbols)

    def test_complex_chained_methods_split_correctly(self):
        """V7-04: complex expressions like grantTypeShortcut.equals(substring(3, 5))
        should split on punctuation to extract separate identifiers, not garbled
        concatenations like 'equalssubstring35'."""
        symbols = _extract_symbols(
            "The expression grantTypeShortcut.equals(substring(3, 5)) evaluates the condition",
            "",
        )
        # Should extract: grantTypeShortcut, equals, substring
        self.assertIn("grantTypeShortcut", symbols)
        self.assertIn("equals", symbols)
        self.assertIn("substring", symbols)
        # Should NOT extract garbled concatenations
        self.assertNotIn("equalssubstring", symbols)
        self.assertNotIn("equalssubstring35", symbols)

    def test_deeply_nested_parentheses_split(self):
        """Complex nested method calls should split on all punctuation boundaries."""
        symbols = _extract_symbols(
            "Processing with foo.bar(baz.qux(nested.value))",
            "",
        )
        self.assertIn("foo", symbols)
        self.assertIn("bar", symbols)
        self.assertIn("baz", symbols)
        self.assertIn("qux", symbols)
        self.assertIn("nested", symbols)
        self.assertIn("value", symbols)
        # Ensure no concatenations occur
        self.assertNotIn("barbaznested", symbols)

    def test_camelcase_only_english_words_skipped(self):
        """Tier 3: pure CamelCase English words (no code punctuation) are NOT extracted."""
        symbols = _extract_symbols(
            "Concrete evidence shows that Between the lines However "
            "the Implementation seems fine. Additionally the Response "
            "was unexpected.",
            "",
        )
        # None of these pure CamelCase words should be extracted
        self.assertNotIn("Concrete", symbols)
        self.assertNotIn("Between", symbols)
        self.assertNotIn("However", symbols)
        self.assertNotIn("Implementation", symbols)
        self.assertNotIn("Additionally", symbols)
        self.assertNotIn("Response", symbols)

    def test_camelcase_in_backticks_extracted(self):
        """Tier 1 overrides Tier 3: CamelCase in backticks IS extracted."""
        symbols = _extract_symbols(
            "The `MyClass` handles requests",
            "",
        )
        self.assertIn("MyClass", symbols)

    def test_camelcase_in_triple_backticks_extracted(self):
        """Tier 1: CamelCase inside fenced code blocks IS extracted."""
        symbols = _extract_symbols(
            "Example:\n```\nMyHandler handler = new MyHandler();\n```",
            "",
        )
        self.assertIn("MyHandler", symbols)

    def test_skip_symbols_filtered(self):
        """Common English/Python words in backticks are still filtered out."""
        symbols = _extract_symbols("Uses `self` and `None` values", "")
        self.assertNotIn("self", symbols)
        self.assertNotIn("None", symbols)

    def test_short_tokens_filtered(self):
        """Tokens with 2 or fewer characters are filtered out."""
        symbols = _extract_symbols("The `x` and `ab` values", "")
        self.assertNotIn("x", symbols)
        self.assertNotIn("ab", symbols)

    def test_empty_text_returns_empty(self):
        """No text produces no symbols."""
        symbols = _extract_symbols("", "")
        self.assertEqual(symbols, set())

    def test_none_inputs_return_empty(self):
        """None description and evidence should return empty set without error."""
        symbols = _extract_symbols(None, None)
        self.assertEqual(symbols, set())

    def test_evidence_field_also_scanned(self):
        """Evidence field is included in symbol extraction."""
        symbols = _extract_symbols("", "see `important_func` at line 5")
        self.assertIn("important_func", symbols)


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

    def test_missing_symbol_reduces_confidence_proportionally(self):
        """V5-05: Missing symbols reduce confidence proportionally, not to zero."""
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
                # V5-05: confidence reduced proportionally, not zeroed
                self.assertGreater(finding["confidence"], 0)
                self.assertGreaterEqual(finding["confidence"], 30)  # floor
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

    def test_no_extractable_symbols_skips_verification(self):
        """V5-05: When no symbols can be extracted, skip symbol verification entirely."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("x = 1\ny = 2\n")
            tmppath = f.name
        try:
            finding = {
                "file": tmppath,
                "line_start": 1,
                "line_end": 2,
                "description": "This code has a problem",
                "evidence": "The values are wrong",
                "confidence": 80,
            }
            result = verify_factual(finding)
            self.assertTrue(result)
            # Confidence unchanged — no symbols to check
            self.assertEqual(finding["confidence"], 80)
            self.assertTrue(finding["factual_verification"]["verified"])
            self.assertIn(
                "no extractable symbols",
                finding["factual_verification"]["reason"],
            )
        finally:
            os.unlink(tmppath)

    def test_proportional_reduction_partial_match(self):
        """V5-05: 3 of 4 symbols found → small reduction; 1 of 4 found → large reduction."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            # File contains func_a, func_b, func_c but NOT func_d
            f.write("def func_a():\n    pass\n")
            tmppath = f.name
        try:
            # Case: 1 of 2 symbols missing → 50% miss ratio → reduction ~35
            with patch("scripts.verify_findings.run") as mock_run:
                def grep_side_effect(cmd, check=False, timeout=None, cwd=None):
                    # func_a is in the code_at_lines (fast path), so only func_d is grepped
                    # func_d not found
                    return ("", "", 1)

                mock_run.side_effect = grep_side_effect
                finding = {
                    "file": tmppath,
                    "line_start": 1,
                    "line_end": 2,
                    "description": "The `func_a` and `func_d` functions conflict",
                    "evidence": "",
                    "confidence": 80,
                }
                result = verify_factual(finding)
                self.assertTrue(result)
                # 1 of 2 symbols missing → miss_ratio=0.5 → reduction=round(0.5*70)=35
                # 80 - 35 = 45
                self.assertEqual(finding["confidence"], 45)
        finally:
            os.unlink(tmppath)

    def test_confidence_floor_at_30(self):
        """V5-05: Confidence never goes below 30 on symbol check alone."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("x = 1\n")
            tmppath = f.name
        try:
            with patch("scripts.verify_findings.run") as mock_run:
                mock_run.return_value = ("", "", 1)
                finding = {
                    "file": tmppath,
                    "line_start": 1,
                    "line_end": 1,
                    "description": "The `totally_fake_symbol` is broken",
                    "evidence": "",
                    "confidence": 40,
                }
                result = verify_factual(finding)
                self.assertTrue(result)
                # Even with 100% miss ratio and low starting confidence, floor at 30
                self.assertEqual(finding["confidence"], 30)
        finally:
            os.unlink(tmppath)

    def test_confidence_floor_at_30_high_starting_confidence(self):
        """V5-05: 100% miss ratio from high confidence still floors at 30."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("x = 1\n")
            tmppath = f.name
        try:
            with patch("scripts.verify_findings.run") as mock_run:
                mock_run.return_value = ("", "", 1)
                finding = {
                    "file": tmppath,
                    "line_start": 1,
                    "line_end": 1,
                    "description": "The `totally_fake_symbol` is broken",
                    "evidence": "",
                    "confidence": 90,
                }
                result = verify_factual(finding)
                self.assertTrue(result)
                # 100% miss ratio → reduction=round(1.0*70)=70 → max(30, 90-70)=30
                self.assertEqual(finding["confidence"], 30)
        finally:
            os.unlink(tmppath)

    def test_all_symbols_found_no_confidence_change(self):
        """V5-05: When all symbols are found, confidence stays unchanged."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def real_function():\n    return real_value\n")
            tmppath = f.name
        try:
            with patch("scripts.verify_findings.run") as mock_run:
                # grep finds the symbol
                mock_run.return_value = ("found.py:1:match\n", "", 0)
                finding = {
                    "file": tmppath,
                    "line_start": 1,
                    "line_end": 2,
                    "description": "The `real_function` returns `real_value`",
                    "evidence": "",
                    "confidence": 75,
                }
                result = verify_factual(finding)
                self.assertTrue(result)
                # All symbols found in code_at_lines or via grep
                self.assertEqual(finding["confidence"], 75)
                self.assertTrue(finding["factual_verification"]["verified"])
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
            "severity": "medium",  # already downgraded by blame (post-blame state)
            "blame_metadata": {"classification": "surfaced"},
        }
        validate_diff_lines(finding, valid)
        # Blame already classified as surfaced, so no additional downgrade
        self.assertEqual(finding["severity"], "medium")


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


# ---------------------------------------------------------------------------
# RF-01: grep uses REPO_ROOT, not CWD
# ---------------------------------------------------------------------------

class TestRepoRoot(unittest.TestCase):
    """REPO_ROOT is resolved at module load time and must be an absolute path."""

    def test_repo_root_is_absolute(self):
        self.assertTrue(os.path.isabs(REPO_ROOT))

    def test_repo_root_is_directory(self):
        self.assertTrue(os.path.isdir(REPO_ROOT))

    def test_grep_called_with_repo_root(self):
        """verify_factual must pass REPO_ROOT as cwd to git grep."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def missing_func():\n    pass\n")
            tmppath = f.name
        try:
            captured_kwargs = []

            def mock_run(cmd, check=False, timeout=None, cwd=None):
                captured_kwargs.append({"cmd": cmd, "cwd": cwd})
                return ("", "", 1)  # symbol not found

            with patch("scripts.verify_findings.run", side_effect=mock_run):
                finding = {
                    "file": tmppath,
                    "line_start": 1,
                    "line_end": 2,
                    "description": "The `SomeClass` does bad things",
                    "evidence": "",
                }
                verify_factual(finding)

            git_grep_calls = [k for k in captured_kwargs if k["cmd"][:2] == ["git", "grep"]]
            self.assertTrue(git_grep_calls, "Expected at least one git grep call")
            for call in git_grep_calls:
                # cwd must be REPO_ROOT (absolute), not None or "."
                self.assertIsNotNone(call["cwd"], msg="git grep called without cwd")
                self.assertTrue(os.path.isabs(call["cwd"]), msg=f"git grep cwd is not absolute: {call['cwd']}")
        finally:
            os.unlink(tmppath)


# ---------------------------------------------------------------------------
# RF-03: grep rc=2 skips symbol check instead of silently zeroing confidence
# ---------------------------------------------------------------------------

class TestVerifyFactualGrepError(unittest.TestCase):

    def test_grep_rc2_skips_symbol_not_zeros_confidence(self):
        """RF-03: grep exit code 2 (I/O error) must not add symbol to missing list."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def my_func():\n    pass\n")
            tmppath = f.name
        try:
            with patch("scripts.verify_findings.run") as mock_run:
                # rc=2 simulates an I/O error from grep
                mock_run.return_value = ("", "grep: permission denied", 2)
                finding = {
                    "file": tmppath,
                    "line_start": 1,
                    "line_end": 2,
                    "description": "The `ExternalClass` causes issues",
                    "evidence": "",
                    "confidence": 75,
                }
                result = verify_factual(finding)
                # Confidence must NOT be zeroed when grep returns rc=2
                self.assertTrue(result)
                self.assertEqual(finding.get("confidence", 75), 75)
                # factual_verification should be verified=True (no missing symbols recorded)
                self.assertTrue(finding["factual_verification"]["verified"])
        finally:
            os.unlink(tmppath)

    def test_grep_rc1_still_records_missing_symbol(self):
        """RF-03: grep exit code 1 (no match) must still flag the symbol as missing."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def my_func():\n    pass\n")
            tmppath = f.name
        try:
            with patch("scripts.verify_findings.run") as mock_run:
                # rc=1 means grep ran successfully but found no match
                mock_run.return_value = ("", "", 1)
                finding = {
                    "file": tmppath,
                    "line_start": 1,
                    "line_end": 2,
                    "description": "The `MissingClass` is broken",
                    "evidence": "",
                    "confidence": 75,
                }
                result = verify_factual(finding)
                # V5-05: rc=1 still reduces confidence (symbol not found)
                # but proportionally, not to zero
                self.assertTrue(result)  # kept but degraded
                self.assertLess(finding["confidence"], 75)
                self.assertGreaterEqual(finding["confidence"], 30)
                self.assertFalse(finding["factual_verification"]["verified"])
        finally:
            os.unlink(tmppath)


# ---------------------------------------------------------------------------
# git grep symbol verification with timeout
# ---------------------------------------------------------------------------

class TestVerifyFactualGitGrep(unittest.TestCase):
    """Tests for git grep symbol verification with timeout."""

    def test_symbol_timeout_no_confidence_reduction(self):
        """Timed-out symbol search must not reduce confidence."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def my_func():\n    pass\n")
            tmppath = f.name
        try:
            with patch("scripts.verify_findings.run") as mock_run:
                mock_run.return_value = ("", "", -1)
                finding = {
                    "file": tmppath,
                    "line_start": 1,
                    "line_end": 2,
                    "description": "The `ExternalWidget` causes issues",
                    "evidence": "",
                    "confidence": 80,
                }
                result = verify_factual(finding)
                self.assertTrue(result)
                self.assertEqual(finding["confidence"], 80)
                self.assertTrue(finding["factual_verification"]["verified"])
        finally:
            os.unlink(tmppath)

    def test_git_grep_called_with_timeout_and_cwd(self):
        """Symbol search must call git grep with timeout=3 and cwd=REPO_ROOT."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("x = 1\n")
            tmppath = f.name
        try:
            with patch("scripts.verify_findings.run") as mock_run:
                mock_run.return_value = ("", "", 1)
                with patch("scripts.verify_findings.REPO_ROOT", "/fake/root"):
                    finding = {
                        "file": tmppath,
                        "line_start": 1,
                        "line_end": 1,
                        "description": "The `UnknownSymbol` is problematic",
                        "evidence": "",
                        "confidence": 75,
                    }
                    verify_factual(finding)
                    call_args = mock_run.call_args
                    cmd = call_args[0][0]
                    self.assertEqual(cmd[0], "git")
                    self.assertEqual(cmd[1], "grep")
                    self.assertIn("-l", cmd)
                    self.assertEqual(call_args[1].get("timeout"), 3)
                    self.assertEqual(call_args[1].get("cwd"), "/fake/root")
        finally:
            os.unlink(tmppath)

    def test_git_grep_fatal_error_skips_symbol(self):
        """git grep fatal error (rc=128) must skip symbol, not penalize."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("x = 1\n")
            tmppath = f.name
        try:
            with patch("scripts.verify_findings.run") as mock_run:
                mock_run.return_value = ("", "fatal: not a git repository", 128)
                finding = {
                    "file": tmppath,
                    "line_start": 1,
                    "line_end": 1,
                    "description": "The `SomeClass` is unused",
                    "evidence": "",
                    "confidence": 85,
                }
                result = verify_factual(finding)
                self.assertTrue(result)
                self.assertEqual(finding["confidence"], 85)
        finally:
            os.unlink(tmppath)


# ---------------------------------------------------------------------------
# RF-05: sha_in_pr dead branch removed (tested via classify_blame)
# ---------------------------------------------------------------------------

class TestShaInPrDeadBranch(unittest.TestCase):
    """RF-05: classify_blame must correctly match blamed (short) SHA against PR full SHAs."""

    @patch("scripts.verify_findings.os.path.exists", return_value=True)
    @patch("scripts.verify_findings.run")
    def test_short_blamed_sha_matches_full_pr_sha(self, mock_run, _mock_exists):
        """A 7-char blamed SHA should match when a full PR SHA starts with it."""
        def run_side_effect(cmd, check=False):
            if cmd[1] == "log":
                return ("abc1234567890abcdef1234567890abcdef123456\n", "", 0)
            if cmd[1] == "blame":
                return ("abc1234 (Author 2026-03-30 10:00:00 +0000 1) code\n", "", 0)
            return ("", "", 0)

        mock_run.side_effect = run_side_effect
        finding = {"file": "f.py", "line_start": 1, "line_end": 1, "severity": "high"}
        result = classify_blame(finding, "main")
        # abc1234 is a prefix of the PR commit — should be "new"
        self.assertEqual(result, "new")

    @patch("scripts.verify_findings.os.path.exists", return_value=True)
    @patch("scripts.verify_findings.run")
    def test_full_blamed_sha_does_not_match_short_pr_sha(self, mock_run, _mock_exists):
        """A full blamed SHA should NOT match a shorter PR SHA (removed dead branch).

        Before RF-05 the dead branch ``blamed_sha.startswith(full_sha)`` would
        have caused a false-positive match when the 'full' PR SHA is actually
        shorter than the blamed SHA.  After the fix, only full_sha.startswith
        (blamed_sha) is checked, so this case must return 'surfaced'.
        """
        def run_side_effect(cmd, check=False):
            if cmd[1] == "log":
                # Simulate a short/truncated PR SHA (would only match via dead branch)
                return ("abc123\n", "", 0)
            if cmd[1] == "blame":
                # Full-length blamed SHA that starts with abc123 — old dead branch
                # would match; new code should NOT
                return ("abc1234567890def (Author 2026-03-30 10:00:00 +0000 1) code\n", "", 0)
            return ("", "", 0)

        mock_run.side_effect = run_side_effect
        finding = {"file": "f.py", "line_start": 1, "line_end": 1, "severity": "high"}
        result = classify_blame(finding, "main")
        # "abc123" does NOT start with "abc1234567890def" → surfaced
        self.assertEqual(result, "surfaced")


# ---------------------------------------------------------------------------
# BF-11: get_diff fallback chain
# ---------------------------------------------------------------------------

class TestGetDiff(unittest.TestCase):
    """BF-11: Tests for the robust diff fallback chain in get_diff()."""

    def test_diff_file_read_successfully(self):
        """R01.1: --diff-file path is read and its content returned."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as f:
            f.write("diff --git a/foo.py b/foo.py\n+added line\n")
            tmppath = f.name
        try:
            result = get_diff("main", diff_file=tmppath)
            self.assertIn("added line", result)
        finally:
            os.unlink(tmppath)

    def test_diff_file_not_found_returns_none(self):
        """R01.1: Missing --diff-file returns None gracefully."""
        result = get_diff("main", diff_file="/nonexistent/path/diff.txt")
        self.assertIsNone(result)

    @patch("scripts.verify_findings.run")
    def test_three_dot_success_returns_diff(self, mock_run):
        """Three-dot success path: returns stdout directly."""
        mock_run.return_value = ("diff content\n", "", 0)
        result = get_diff("main")
        self.assertEqual(result, "diff content\n")
        mock_run.assert_called_once_with(["git", "diff", "main...HEAD"])

    @patch("scripts.verify_findings.run")
    def test_two_dot_fallback_when_three_dot_fails(self, mock_run):
        """R01.2: Two-dot fallback triggered when three-dot diff fails."""
        def run_side_effect(cmd, check=False):
            if cmd == ["git", "diff", "main...HEAD"]:
                return ("", "fatal: no merge base", 128)
            if cmd == ["git", "diff", "main", "HEAD"]:
                return ("two-dot diff content\n", "", 0)
            return ("", "", 0)

        mock_run.side_effect = run_side_effect
        result = get_diff("main")
        self.assertEqual(result, "two-dot diff content\n")
        # Verify both commands were called
        calls = [c[0][0] for c in mock_run.call_args_list]
        self.assertIn(["git", "diff", "main...HEAD"], calls)
        self.assertIn(["git", "diff", "main", "HEAD"], calls)

    @patch("scripts.verify_findings.run")
    def test_none_returned_when_both_diffs_fail(self, mock_run):
        """R01.3: Returns None when both three-dot and two-dot diffs fail."""
        mock_run.return_value = ("", "fatal: bad revision", 128)
        result = get_diff("main")
        self.assertIsNone(result)

    @patch("scripts.verify_findings.run")
    def test_git_diff_head_not_called(self, mock_run):
        """R01.5: git diff HEAD fallback is removed entirely."""
        mock_run.return_value = ("", "fatal: bad revision", 128)
        get_diff("main")
        # Ensure no call was made with just ["git", "diff", "HEAD"]
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            self.assertNotEqual(cmd, ["git", "diff", "HEAD"],
                                msg="git diff HEAD must not be called")

    @patch("scripts.verify_findings.run")
    def test_diff_source_logging_three_dot(self, mock_run):
        """R01.4: Logs diff source on stderr for three-dot success."""
        mock_run.return_value = ("diff data\n", "", 0)
        import io
        with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            get_diff("main")
            stderr_output = mock_stderr.getvalue()
        self.assertIn("Diff source:", stderr_output)
        self.assertIn("three-dot", stderr_output)
        self.assertIn("bytes", stderr_output)

    @patch("scripts.verify_findings.run")
    def test_diff_source_logging_two_dot(self, mock_run):
        """R01.4: Logs diff source on stderr for two-dot fallback."""
        def run_side_effect(cmd, check=False):
            if cmd == ["git", "diff", "main...HEAD"]:
                return ("", "fatal: no merge base", 128)
            if cmd == ["git", "diff", "main", "HEAD"]:
                return ("two-dot data\n", "", 0)
            return ("", "", 0)

        mock_run.side_effect = run_side_effect
        import io
        with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            get_diff("main")
            stderr_output = mock_stderr.getvalue()
        self.assertIn("Diff source:", stderr_output)
        self.assertIn("two-dot", stderr_output)
        self.assertIn("bytes", stderr_output)

    def test_diff_file_logging_includes_bytes(self):
        """R01.4: --diff-file source logs path and byte count."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as f:
            f.write("x" * 50)
            tmppath = f.name
        try:
            import io
            with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                get_diff("main", diff_file=tmppath)
                stderr_output = mock_stderr.getvalue()
            self.assertIn("Diff source:", stderr_output)
            self.assertIn("--diff-file", stderr_output)
            self.assertIn("50 bytes", stderr_output)
        finally:
            os.unlink(tmppath)


# ---------------------------------------------------------------------------
# run() helper — timeout and cwd
# ---------------------------------------------------------------------------

class TestRunTimeout(unittest.TestCase):
    """Tests for run() helper timeout and cwd parameters."""

    def test_timeout_returns_sentinel(self):
        """run() with timeout returns (-1) returncode on TimeoutExpired."""
        stdout, stderr, rc = run(["sleep", "10"], timeout=0.1)
        self.assertEqual(rc, -1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_timeout_none_no_limit(self):
        """run() without timeout behaves as before."""
        stdout, stderr, rc = run(["echo", "hello"])
        self.assertEqual(rc, 0)
        self.assertIn("hello", stdout)

    def test_cwd_changes_directory(self):
        """run() with cwd runs command in specified directory."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            stdout, stderr, rc = run(["pwd"], cwd=d)
            self.assertEqual(rc, 0)
            self.assertEqual(os.path.realpath(stdout.strip()),
                             os.path.realpath(d))

    def test_backward_compat_no_new_params(self):
        """run() still works with only (cmd) or (cmd, check) args."""
        stdout, stderr, rc = run(["echo", "hi"])
        self.assertEqual(rc, 0)
        stdout2, stderr2, rc2 = run(["echo", "hi"], check=True)
        self.assertEqual(rc2, 0)


class TestVerifyOutputFlag(unittest.TestCase):
    """Tests for --output flag writing JSON to file."""

    def test_output_flag_writes_json_to_file(self):
        """--output writes valid JSON to the specified file."""
        import json
        output = {
            "verified": [{"id": "bug-1", "origin": "new"}],
            "eliminated": [],
            "batches": [["bug-1"]],
            "stats": {"total": 1, "new": 1, "surfaced": 0, "eliminated": 0},
        }
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            outpath = f.name
        try:
            _write_output(output, outpath)
            with open(outpath) as f:
                written = json.load(f)
            self.assertEqual(written["verified"][0]["id"], "bug-1")
            self.assertEqual(written["stats"]["total"], 1)
        finally:
            os.unlink(outpath)

    def test_output_none_prints_to_stdout(self):
        """When output_path is None, JSON goes to stdout."""
        import io
        output = {"verified": [], "eliminated": [], "batches": [], "stats": {}}
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            _write_output(output, None)
            result = mock_stdout.getvalue()
        self.assertIn('"verified"', result)


class TestReceipt(unittest.TestCase):
    """Tests for the --input/--nonce/--head-sha receipt envelope (Task 11).

    The receipt path is the workflow-facing contract: the JS verify stage trusts
    the result only when status=='ok' and the receipt echoes the nonce, head sha,
    and input finding count it dispatched. These findings use nonexistent files
    and carry no line_start so verification is fully deterministic with zero git
    subprocess dependency (classify_blame short-circuits on os.path.exists; the
    empty --diff-file makes diff validation a no-op for line-less findings).
    """

    def _findings(self):
        return [
            {"id": "bug-1", "dimension": "bug", "severity": "high", "confidence": 75,
             "file": "nope/does-not-exist-xyz.py", "title": "t", "description": "d",
             "evidence": "e", "cross_file_refs": []},
            {"id": "bug-2", "dimension": "bug", "severity": "low", "confidence": 50,
             "file": "nope/does-not-exist-abc.py", "title": "t2", "description": "d2",
             "evidence": "e2", "cross_file_refs": []},
        ]

    def _run_main(self, argv):
        """Invoke main() with a controlled argv, stderr suppressed."""
        import io
        from scripts.verify_findings import main
        with patch.object(sys, "argv", argv), \
                patch("sys.stderr", new_callable=io.StringIO):
            main()

    def test_receipt_envelope_shape_and_legacy_parity(self):
        import json
        findings = self._findings()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"findings": findings, "base_branch": "main"}, f)
            findings_path = f.name
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
            empty_diff = f.name  # empty diff -> parse_diff_lines returns set()
        legacy_out = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        receipt_out = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        try:
            # Legacy positional path (the baseline the receipt must reproduce).
            self._run_main([
                "verify_findings.py", findings_path,
                "--diff-file", empty_diff, "--output", legacy_out,
            ])
            with open(legacy_out) as fh:
                legacy = json.load(fh)

            # Receipt path: --input replaces the positional, --nonce/--head-sha echoed.
            self._run_main([
                "verify_findings.py", "--input", findings_path,
                "--diff-file", empty_diff, "--output", receipt_out,
                "--nonce", "NONCE-123", "--head-sha", "deadbeef",
            ])
            with open(receipt_out) as fh:
                envelope = json.load(fh)

            # (a) envelope shape + receipt fields. deltas_checksum is the delta echo's
            # content proof (issue #25 PR2) and input_checksum the slice-input one
            # (PR3) -- both ride alongside sha/n_in/nonce. input_trailing_bytes is
            # absent on a clean read, which is what makes its presence meaningful.
            self.assertEqual(envelope["status"], "ok")
            self.assertEqual(
                set(envelope["receipt"].keys()),
                {"sha", "n_in", "nonce", "deltas_checksum", "input_checksum"},
            )
            self.assertEqual(envelope["receipt"]["sha"], "deadbeef")
            self.assertEqual(envelope["receipt"]["n_in"], len(findings))
            self.assertEqual(envelope["receipt"]["nonce"], "NONCE-123")
            self.assertRegex(envelope["receipt"]["deltas_checksum"], r"^fnv1a32:0x[0-9a-f]{8}$")
            self.assertRegex(envelope["receipt"]["input_checksum"], r"^fnv1a32:0x[0-9a-f]{8}$")

            # (b) result.verified is exactly what the legacy path produces
            self.assertEqual(envelope["result"]["verified"], legacy["verified"])
            self.assertEqual(envelope["result"]["stats"], legacy["stats"])
        finally:
            for p in (findings_path, empty_diff, legacy_out, receipt_out):
                os.unlink(p)

    def test_receipt_failure_envelope_is_schema_valid(self):
        """An uncaught exception mid-verification yields a status=='failed' envelope
        on stdout with exit 0 (honest failure is schema-valid, never fabricated)."""
        import io, json
        findings = self._findings()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"findings": findings}, f)
            findings_path = f.name
        try:
            # Force run_verification to blow up after loading, exercising the wrapper.
            with patch("scripts.verify_findings.run_verification",
                       side_effect=RuntimeError("boom")), \
                    patch("sys.stderr", new_callable=io.StringIO), \
                    patch("sys.stdout", new_callable=io.StringIO) as out, \
                    patch.object(sys, "argv", [
                        "verify_findings.py", "--input", findings_path,
                        "--nonce", "N", "--head-sha", "abc",
                    ]):
                from scripts.verify_findings import main
                main()  # must NOT raise; must print the failed envelope
                printed = out.getvalue()
            envelope = json.loads(printed)
            self.assertEqual(envelope["status"], "failed")
            self.assertEqual(envelope["exitCode"], 1)
            self.assertIn("boom", envelope["stderr"])
        finally:
            os.unlink(findings_path)

    def _receipt_for(self, text):
        """Run receipt mode over `text` as the --input file; return the envelope."""
        import io, json
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(text)
            in_path = f.name
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        try:
            with patch("sys.stderr", new_callable=io.StringIO), \
                    patch.object(sys, "argv", [
                        "verify_findings.py", "--input", in_path,
                        "--output", out_path, "--nonce", "N.0", "--head-sha", "abc1234",
                    ]):
                from scripts.verify_findings import main
                main()  # must NOT raise SystemExit
            with open(out_path) as fh:
                return json.load(fh)
        finally:
            os.unlink(in_path)
            os.unlink(out_path)

    def test_malformed_input_yields_the_honest_failure_envelope_not_silence(self):
        """A die() condition in receipt mode must WRITE the failure envelope.

        Regression this guards, measured live on smoke-20260729-191253-8ae2ee3:
        load_input reported a corrupt slice input through die(), which called sys.exit
        -- SystemExit is a BaseException, so it flew past `except Exception` and the
        script exited having written NO output file. The executor found nothing to
        read, so the slice degraded with "no file" instead of the reason, and
        diagnosing the run meant re-running the command by hand.

        THE FIXTURE MOVED, THE GUARANTEE DID NOT (issue #25 PR3). This used to use the
        incident's exact bytes -- a complete document with one `}` appended -- but that
        input now RECOVERS through the lenient parse and is asserted separately by
        test_trailing_bytes_recover_with_the_same_content_proof. A TRUNCATED document
        is still fatal (raw_decode demands a complete value), so it is the fixture that
        keeps exercising this path. Both halves matter: the recovery must not be able
        to swallow a genuinely unreadable file, and a genuinely unreadable file must
        still report itself.
        """
        truncated = json.dumps({"findings": self._findings(), "base_branch": "main"})[:-25]
        envelope = self._receipt_for(truncated)
        self.assertEqual(envelope["status"], "failed")
        self.assertEqual(envelope["exitCode"], 1)
        # The REAL reason, not a placeholder — this is the whole point.
        self.assertIn("Invalid JSON in findings file", envelope["stderr"])
        # ...and a machine-readable code, so the workflow's re-materialize decision is
        # structural rather than a match on this prose (issue #25 PR3).
        self.assertEqual(envelope["reason"], "input_unparseable")

    def test_trailing_bytes_recover_with_the_same_content_proof(self):
        """The measured corruption class recovers, and PROVES it recovered.

        4 of the 31 verify slice-input files this repo has retained (12.9%) are
        unparseable, and every one is a complete document followed by bytes the
        artifact-writer appended after its final byte. Each cost a whole slice its
        classification. The lenient parse takes the leading document; what makes that
        a recovery rather than a guess is that `input_checksum` over the recovered
        value is IDENTICAL to the checksum of the clean document -- so the workflow,
        which compares it against the content it dispatched, can prove the file it got
        is the file it sent before acting on it.
        """
        clean = json.dumps({"findings": self._findings(), "base_branch": "main"})
        clean_env = self._receipt_for(clean)
        corrupt_env = self._receipt_for(clean + "}")

        self.assertEqual(clean_env["status"], "ok")
        self.assertEqual(corrupt_env["status"], "ok")
        self.assertEqual(
            corrupt_env["receipt"]["input_checksum"],
            clean_env["receipt"]["input_checksum"],
        )
        # The divergence is still REPORTED — recovery is never silent.
        self.assertEqual(corrupt_env["receipt"]["input_trailing_bytes"], 1)
        self.assertNotIn("input_trailing_bytes", clean_env["receipt"])

    def test_the_real_recorded_corruption_signature_recovers(self):
        """The other recorded signature, byte for byte: `</content>\\n</invoke>\\n`
        appended after the document (custom-20260723-070640-c1dd46f). Kept distinct
        from the stray-`}` case so a fix for one cannot silently stop covering the
        other."""
        clean = json.dumps({"findings": self._findings(), "base_branch": "main"})
        envelope = self._receipt_for(clean + "\n</content>\n</invoke>\n")
        self.assertEqual(envelope["status"], "ok")
        self.assertEqual(
            envelope["receipt"]["input_checksum"],
            self._receipt_for(clean)["receipt"]["input_checksum"],
        )
        self.assertEqual(envelope["receipt"]["input_trailing_bytes"], len("\n</content>\n</invoke>\n"))

    def test_missing_input_file_is_tagged_unreadable(self):
        """A missing --input file is an input fault and says so with a code."""
        import io, json
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        missing = os.path.join(tempfile.mkdtemp(), "never-written.json")
        try:
            with patch("sys.stderr", new_callable=io.StringIO), \
                    patch.object(sys, "argv", [
                        "verify_findings.py", "--input", missing,
                        "--output", out_path, "--nonce", "N.0", "--head-sha", "abc1234",
                    ]):
                from scripts.verify_findings import main
                main()
            with open(out_path) as fh:
                envelope = json.load(fh)
            self.assertEqual(envelope["status"], "failed")
            self.assertEqual(envelope["reason"], "input_unreadable")
        finally:
            os.unlink(out_path)

    def test_a_shape_violation_is_tagged_invalid(self):
        """A well-formed JSON document of the wrong shape is still an input fault."""
        envelope = self._receipt_for(json.dumps({"no_findings_key": True}))
        self.assertEqual(envelope["status"], "failed")
        self.assertEqual(envelope["reason"], "input_invalid")

    def test_a_failure_inside_the_verification_body_carries_no_input_reason_code(self):
        """THE BOUNDARY THIS DESIGN DEPENDS ON (issue #25 PR3).

        The workflow spends an artifact-writer re-materialize dispatch on exactly the
        INPUT_* reason codes. If a bug anywhere in run_verification/build_deltas could
        also carry one, a run would re-write a perfectly good slice-input file --
        which carries its own measured drift risk -- and would then blame "input
        corrupted" for a code defect. _run_receipt splits its try/except in two so
        only the input stage can tag; this test is what stops a future
        'simplification' collapsing them back into one.

        THE FIXTURE RAISES InputError, NOT RuntimeError, AND THAT IS THE WHOLE POINT.
        A collapsed single try/except reads `except InputError -> tag` /
        `except Exception -> do not tag`, so a RuntimeError from the verification body
        comes back untagged under BOTH the correct split and the collapse this guards
        against — the obvious version of this test passes against the bug it names.
        Only an InputError raised from the VERIFICATION stage separates them: the split
        must leave it untagged (it did not come from the input file), while a collapse
        tags it. The first version of this test used RuntimeError and was proven
        non-discriminating during the adversarial review pass.
        """
        import io, json
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"findings": self._findings(), "base_branch": "main"}, f)
            in_path = f.name
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        try:
            with patch("sys.stderr", new_callable=io.StringIO), \
                    patch("scripts.verify_findings.run_verification",
                          side_effect=InputError("boom inside the verification body",
                                                 "input_unparseable")), \
                    patch.object(sys, "argv", [
                        "verify_findings.py", "--input", in_path,
                        "--output", out_path, "--nonce", "N.0", "--head-sha", "abc1234",
                    ]):
                from scripts.verify_findings import main
                main()
            with open(out_path) as fh:
                envelope = json.load(fh)
            self.assertEqual(envelope["status"], "failed")
            self.assertIn("boom inside the verification body", envelope["stderr"])
            self.assertNotIn(
                "reason", envelope,
                "an InputError raised from the VERIFICATION stage must not be tagged as "
                "an input fault — the two try/excepts have been collapsed into one",
            )
        finally:
            os.unlink(in_path)
            os.unlink(out_path)

    def test_a_plain_crash_in_the_verification_body_is_still_untagged(self):
        """The ordinary case, kept beside the discriminating one above."""
        import io, json
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"findings": self._findings(), "base_branch": "main"}, f)
            in_path = f.name
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        try:
            with patch("sys.stderr", new_callable=io.StringIO), \
                    patch("scripts.verify_findings.run_verification",
                          side_effect=RuntimeError("boom")), \
                    patch.object(sys, "argv", [
                        "verify_findings.py", "--input", in_path,
                        "--output", out_path, "--nonce", "N.0", "--head-sha", "abc1234",
                    ]):
                from scripts.verify_findings import main
                main()
            with open(out_path) as fh:
                envelope = json.load(fh)
            self.assertEqual(envelope["status"], "failed")
            self.assertNotIn("reason", envelope)
        finally:
            os.unlink(in_path)
            os.unlink(out_path)

    def test_a_bom_prefixed_slice_input_is_read_rather_than_refused(self):
        """The Write tool may prepend a UTF-8 BOM. `json.load` under the default
        encoding reports that as unparseable JSON, which would cost a slice its
        classification over a byte no human put there — so the input is opened as
        utf-8-sig. Pinned because it is a deliberate behaviour change with no other
        coverage, and because the proof must be computed over the document WITHOUT the
        BOM: a BOM-prefixed file and a clean one are the same document, so they must
        produce the same checksum or the tolerance would just relocate the failure.
        """
        clean = json.dumps({"findings": self._findings(), "base_branch": "main"})
        clean_env = self._receipt_for(clean)
        bom_env = self._receipt_for("﻿" + clean)
        self.assertEqual(bom_env["status"], "ok")
        self.assertEqual(
            bom_env["receipt"]["input_checksum"], clean_env["receipt"]["input_checksum"],
        )

    def test_an_unrepresentable_number_makes_the_proof_absent_not_fatal(self):
        """A float anywhere in the document costs the PROOF, never the slice.

        js_stringify_pretty refuses any float (JS and Python spell them differently),
        so the checksum must degrade to None the way deltas_checksum already does. A
        bare call would bubble through _run_receipt's except and mark every finding in
        the slice origin=unknown over a value this script does not even read.

        Measured context for why this is a guard and not a routine path: across all 27
        parseable slice-input files this repo has retained, js_stringify_pretty
        succeeded on every one -- i.e. none contained a float.
        """
        findings = self._findings()
        findings[0]["confidence"] = 90.5
        envelope = self._receipt_for(
            json.dumps({"findings": findings, "base_branch": "main"})
        )
        self.assertEqual(envelope["status"], "ok")
        self.assertIsNone(envelope["receipt"]["input_checksum"])

    def test_legacy_path_reads_a_bom_prefixed_file_rather_than_dying_on_it(self):
        """The one LEGACY-path behaviour change this PR makes, pinned rather than left
        to be discovered.

        `load_input` and the receipt path share `read_input_document`, which opens
        utf-8-sig — so the positional CLI inherited BOM tolerance. Before, a BOM'd file
        died with "Invalid JSON in findings file: Unexpected UTF-8 BOM"; now it parses.
        That is the better behaviour (a BOM is not a content error) but it IS a change,
        so it gets a test instead of a claim that nothing changed. Everything else about
        this path is unchanged, including that trailing bytes are still fatal here — the
        legacy path passes lenient=False because it has no content proof to make a
        lenient parse safe.
        """
        import io
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write("﻿" + json.dumps({"findings": self._findings()}))
            bom_path = f.name
        try:
            with patch("sys.stderr", new_callable=io.StringIO) as err, \
                    patch("sys.stdout", new_callable=io.StringIO), \
                    patch.object(sys, "argv", ["verify_findings.py", bom_path]):
                from scripts.verify_findings import main
                main()  # must NOT exit non-zero
            self.assertNotIn("Invalid JSON in findings file", err.getvalue())
        finally:
            os.unlink(bom_path)

    def test_legacy_path_still_exits_nonzero_on_malformed_input(self):
        """The other half of the same change: die() raising InputError instead of calling
        sys.exit must not soften the LEGACY positional path, which has always exited 1
        with the message on stderr and nothing on stdout."""
        import io, json
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps({"findings": self._findings()}) + "}")
            corrupt_path = f.name
        try:
            with patch("sys.stderr", new_callable=io.StringIO) as err, \
                    patch("sys.stdout", new_callable=io.StringIO) as out, \
                    patch.object(sys, "argv", ["verify_findings.py", corrupt_path]):
                from scripts.verify_findings import main
                with self.assertRaises(SystemExit) as ctx:
                    main()
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("Invalid JSON in findings file", err.getvalue())
            self.assertEqual(out.getvalue(), "")
        finally:
            os.unlink(corrupt_path)


class TestBuildDeltas(unittest.TestCase):
    """Unit tests for build_deltas: the per-finding delta list keyed by id and ordered
    by the INPUT findings array (issue #25 req 1/2). These pin the exact contract the
    workflow's join (joinVerifyDeltas in stages.js) relies on -- a regression here
    would silently corrupt every verify slice's echo without any test elsewhere
    noticing, since the JS side trusts whatever shape this script emits.
    """

    def test_one_entry_per_finding_in_input_order_with_identity_based_verified(self):
        # Regression this guards: ordering deltas verified-then-eliminated (rather than
        # by input order) would still pass the order-blind checksum, but would break
        # anything downstream that assumes delta[i] answers for findings[i] -- and
        # would make "verified" look like anything other than a straight per-finding
        # boolean. f2 is "eliminated" by simply not appearing in the verified list,
        # by object identity, not by matching its id.
        f1 = {"id": "bug-1"}
        f2 = {"id": "bug-2"}
        f3 = {"id": "bug-3"}
        findings = [f1, f2, f3]
        verified = [f1, f3]
        deltas = build_deltas(findings, verified)
        self.assertEqual([d["id"] for d in deltas], ["bug-1", "bug-2", "bug-3"])
        self.assertEqual([d["verified"] for d in deltas], [True, False, True])
        for d in deltas:
            self.assertIsInstance(d["verified"], bool)

    def test_verified_membership_is_by_identity_not_by_id(self):
        # Regression this guards: keying membership on id (instead of object identity)
        # would flip BOTH twins of a duplicated id "verified", mis-reporting an
        # eliminated finding as kept just because some OTHER dict shares its id.
        # merge/persistDerivable are supposed to have already rejected duplicate ids
        # upstream, but build_deltas's own docstring calls this out explicitly, so it
        # must hold even on input the pipeline should never have produced.
        dup_a = {"id": "same-id"}
        dup_b = {"id": "same-id"}
        deltas = build_deltas([dup_a, dup_b], [dup_a])
        self.assertEqual(deltas[0]["verified"], True)
        self.assertEqual(deltas[1]["verified"], False)

    def test_elimination_reason_present_only_on_the_eliminated_finding(self):
        # Regression this guards: the delta must carry run_verification's
        # elimination_reason stamp for a REAL elimination and must NOT invent one for
        # a finding that was kept. Runs the actual pipeline (not a synthetic dict) so
        # a future refactor of the stamping site in run_verification is caught here,
        # not just in a hand-built fixture that could drift from reality.
        eliminated_finding = {
            "id": "bug-1", "dimension": "bug", "severity": "high", "confidence": 75,
            "file": "nope/does-not-exist-xyz.py", "line_start": 1, "title": "t",
            "description": "d", "evidence": "e", "cross_file_refs": [],
        }
        verified_finding = {
            "id": "bug-2", "dimension": "bug", "severity": "low", "confidence": 50,
            "file": "nope/does-not-exist-abc.py", "title": "t2", "description": "d2",
            "evidence": "e2", "cross_file_refs": [],
        }
        findings = [eliminated_finding, verified_finding]
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
            empty_diff = f.name  # empty diff -> parse_diff_lines returns set()
        try:
            import io
            with patch("sys.stderr", new_callable=io.StringIO):
                result = run_verification(findings, "main", diff_file=empty_diff)
            self.assertEqual(len(result["eliminated"]), 1)
            self.assertEqual(len(result["verified"]), 1)
            deltas = build_deltas(findings, result["verified"])
            by_id = {d["id"]: d for d in deltas}
            self.assertIn("elimination_reason", by_id["bug-1"])
            self.assertTrue(by_id["bug-1"]["elimination_reason"].strip())
            self.assertNotIn("elimination_reason", by_id["bug-2"])
        finally:
            os.unlink(empty_diff)

    def test_only_delta_fields_appear_excluded_keys_absent(self):
        # Regression this guards: a delta key outside {id, verified} union
        # _DELTA_FIELDS would silently widen what the executor has to echo back,
        # reintroducing the transcription risk this boundary exists to remove.
        # blame_metadata/factual_verification/diff_validation are this script's own
        # audit trail (no workflow schema declares them); agent is merge-injected
        # identity withheld at this boundary ON PURPOSE (#25 req 1) -- see the audit
        # comment above _DELTA_FIELDS in verify_findings.py for both rationales.
        finding = {
            "id": "bug-1",
            "origin": "surfaced", "severity": "low", "confidence": 42,
            "elimination_reason": "evidence does not match file content",
            "blame_metadata": {"classification": "surfaced"},
            "factual_verification": {"verified": True},
            "diff_validation": {"in_diff": True},
            "agent": "bug-detector",
        }
        deltas = build_deltas([finding], [])
        self.assertEqual(len(deltas), 1)
        allowed = {"id", "verified"} | set(_DELTA_FIELDS)
        delta_keys = set(deltas[0].keys())
        self.assertTrue(delta_keys.issubset(allowed), delta_keys - allowed)
        for excluded in ("blame_metadata", "factual_verification", "diff_validation", "agent"):
            self.assertNotIn(excluded, deltas[0])

    def test_missing_blank_or_non_string_id_is_skipped(self):
        # Regression this guards: the delta is keyed by id, so a finding with nothing
        # usable to key it on must be OMITTED rather than emitted with a fabricated
        # or coerced id -- per build_deltas's docstring, the workflow's id-coverage
        # guard then sees an uncovered dispatched id and degrades that slice honestly,
        # which is the right outcome for input the merge stage should have dropped.
        findings = [
            {"dimension": "bug"},               # no id key at all
            {"id": "", "dimension": "bug"},      # blank string
            {"id": "   ", "dimension": "bug"},   # whitespace-only
            {"id": 123, "dimension": "bug"},     # non-string id
            {"id": "bug-ok", "dimension": "bug"},
        ]
        deltas = build_deltas(findings, [])
        self.assertEqual([d["id"] for d in deltas], ["bug-ok"])


class TestDeltaConfidence(unittest.TestCase):
    """_delta_confidence canonicalises confidence for the checksummed delta, or
    returns None to omit the key entirely. Per the docstring: this function is a
    no-op on every REAL pipeline path (confidence is always an int by contract) --
    it exists purely so a non-integral or out-of-range value can never reach the
    delta, where it would make the checksum diverge between JS and Python spelling.
    """

    def test_int_passes_through_unchanged(self):
        # The common case: nothing to canonicalise, nothing to reject.
        self.assertEqual(_delta_confidence(75), 75)
        self.assertEqual(_delta_confidence(0), 0)

    def test_float_rounds_half_up_to_int(self):
        # Regression this guards: Python's round() is half-to-even (banker's
        # rounding), which would make the delta's spelling of a .5 value depend on
        # whether the integer part is odd or even. 74.5 is the discriminating case --
        # round(74.5) == 74 in Python (74 is even) but explicit half-up must give 75.
        self.assertEqual(_delta_confidence(75.5), 76)
        self.assertEqual(_delta_confidence(75.4), 75)
        self.assertEqual(_delta_confidence(75.6), 76)
        self.assertEqual(_delta_confidence(74.5), 75)
        self.assertNotEqual(round(74.5), _delta_confidence(74.5))

    def test_bool_returns_none(self):
        # Regression this guards: bool is an int subclass in Python (isinstance(True,
        # int) is True), so a naive isinstance(value, (int, float)) check would let a
        # stray boolean confidence through as 1/0 instead of being omitted.
        self.assertIsNone(_delta_confidence(True))
        self.assertIsNone(_delta_confidence(False))

    def test_non_number_returns_none(self):
        for value in ("80", None, [], {}, object()):
            self.assertIsNone(_delta_confidence(value))

    def test_nan_and_inf_return_none(self):
        # Regression this guards: NaN/Infinity have no JSON.stringify spelling JS and
        # Python agree on (JS emits null; json.dumps would emit the bare token NaN,
        # which is not valid JSON) -- these must never reach the checksummed delta.
        self.assertIsNone(_delta_confidence(float("nan")))
        self.assertIsNone(_delta_confidence(float("inf")))
        self.assertIsNone(_delta_confidence(float("-inf")))

    def test_out_of_js_safe_range_int_returns_none(self):
        # Regression this guards: an integer outside JS's safe range would have been
        # parsed lossily on the JS side, so the two runtimes would no longer agree on
        # the value even though both spell it without a dot or exponent.
        self.assertIsNone(_delta_confidence(JS_MAX_SAFE_INTEGER + 1))
        self.assertIsNone(_delta_confidence(-(JS_MAX_SAFE_INTEGER + 1)))
        self.assertEqual(_delta_confidence(JS_MAX_SAFE_INTEGER), JS_MAX_SAFE_INTEGER)


class TestDeltasChecksum(unittest.TestCase):
    """deltas_checksum is fnv1a32(js_stringify_pretty(deltas)) -- the SAME checksum
    pair the persist boundary's content proofs use (CLAUDE.md: "one checksum
    definition in the plugin and one parity test guarding it"). These tests pin that
    the workflow's trustSlice recomputation is an actual CONTENT proof and not a
    constant that would validate any echo regardless of what it says.
    """

    def test_checksum_changes_when_a_delta_value_is_mutated(self):
        # Regression this guards: if the checksum were computed over something
        # coarser than delta content (e.g. just the id list, or a fixed schema
        # marker), an executor that echoed a wrong origin/severity/confidence would
        # still pass trustSlice's content-proof check.
        deltas = [{"id": "bug-1", "verified": True, "origin": "new", "severity": "high"}]
        original = deltas_checksum(deltas)
        mutated = [dict(deltas[0], origin="surfaced")]
        self.assertNotEqual(original, deltas_checksum(mutated))

    def test_checksum_is_deterministic_for_identical_content(self):
        # The flip side of the mutation test: two structurally-equal-but-distinct
        # delta lists must produce the SAME checksum, or the workflow's recomputed
        # proof would never match even an honest, faithful echo.
        deltas = [{"id": "bug-1", "verified": False, "elimination_reason": "x"}]
        self.assertEqual(deltas_checksum(deltas), deltas_checksum([dict(deltas[0])]))

    def test_checksum_format(self):
        self.assertRegex(deltas_checksum([]), r"^fnv1a32:0x[0-9a-f]{8}$")

    def test_checksum_returns_none_for_unserialisable_deltas(self):
        # Regression this guards: deltas_checksum's docstring promises None (not a raise)
        # when the deltas contain something the JS/Python serialisation pair cannot spell
        # identically — specifically so the envelope stays status:'ok' with a
        # disclosed-missing proof rather than the whole receipt collapsing to a failure
        # envelope that would throw away a legitimate verified/eliminated result. A float
        # origin is the cheapest JS-unspellable value assert_js_reproducible rejects.
        deltas = [{"id": "bug-1", "verified": True, "origin": 1.5, "severity": "high"}]
        self.assertIsNone(deltas_checksum(deltas))

    def test_build_deltas_checksum_none_when_finding_carries_float_severity(self):
        # Same None-return path, reached through build_deltas rather than a hand-built
        # delta list: a finding whose severity is a non-string float (bypassing the
        # normal string-producing classify_blame / downgrade paths) must still produce a
        # delta list whose checksum is None, not an exception.
        finding = {
            "id": "bug-1", "verified": True, "origin": "new",
            "severity": 3.14, "confidence": 75,
        }
        deltas = build_deltas([finding], [finding])
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["severity"], 3.14)
        self.assertIsNone(deltas_checksum(deltas))


class TestReceiptDeltaEchoEndToEnd(unittest.TestCase):
    """Real-subprocess coverage for the delta echo (issue #25 PR2). Everything above
    calls build_deltas/_delta_confidence/deltas_checksum directly; these tests instead
    invoke the actual CLI the way the workflow's Bash dispatch does, so a wiring bug in
    _run_receipt (wrong key order, a broken argv wire-up, a broken sibling import) is
    caught even when every unit-level function above stays correct in isolation.
    """

    def _findings(self):
        # Same zero-git-dependency shape TestReceipt uses above: nonexistent files,
        # no line_start, so classify_blame short-circuits on os.path.exists and
        # verify_factual skips (no line reference to check) -- both findings end up
        # verified, deterministically, with no git subprocess involved.
        return [
            {"id": "bug-1", "dimension": "bug", "severity": "high", "confidence": 75,
             "file": "nope/does-not-exist-xyz.py", "title": "t", "description": "d",
             "evidence": "e", "cross_file_refs": []},
            {"id": "bug-2", "dimension": "bug", "severity": "low", "confidence": 50,
             "file": "nope/does-not-exist-abc.py", "title": "t2", "description": "d2",
             "evidence": "e2", "cross_file_refs": []},
        ]

    def _write_input(self, findings):
        import json
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"findings": findings, "base_branch": "main"}, f)
            return f.name

    def _empty_diff(self):
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
            return f.name

    def test_result_key_order_starts_with_deltas_full_arrays_unchanged(self):
        # Regression this guards: the executor's Read of this file is length-capped
        # with NO truncation notice (CLAUDE.md), so the delta MUST be a prefix of the
        # document rather than sitting after two full finding arrays. list(dict)
        # preserves json.load's insertion order, so this is a real structural
        # assertion on the file as written, not a restatement of the docstring.
        import json
        findings = self._findings()
        in_path = self._write_input(findings)
        empty_diff = self._empty_diff()
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        try:
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--input", in_path,
                 "--diff-file", empty_diff, "--output", out_path,
                 "--nonce", "N-1", "--head-sha", "deadbeef"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out_path) as fh:
                envelope = json.load(fh)
            self.assertEqual(envelope["status"], "ok", envelope)
            result_keys = list(envelope["result"])
            self.assertEqual(result_keys[0], "deltas")
            self.assertEqual(
                set(result_keys), {"deltas", "verified", "eliminated", "batches", "stats"}
            )
            # The full v2/bench-consumed arrays are unchanged in shape: two findings
            # in, both verified (no line_start -> verify_factual skips), zero
            # eliminated -- the same result the legacy positional path would produce.
            self.assertEqual(len(envelope["result"]["verified"]), 2)
            self.assertEqual(envelope["result"]["eliminated"], [])
            self.assertEqual(envelope["result"]["stats"]["total"], 2)
            self.assertEqual(len(envelope["result"]["deltas"]), 2)
            self.assertRegex(
                envelope["receipt"]["deltas_checksum"], r"^fnv1a32:0x[0-9a-f]{8}$"
            )
        finally:
            for p in (in_path, empty_diff, out_path):
                os.unlink(p)

    def test_legacy_positional_path_emits_no_deltas_and_no_checksum(self):
        # Regression this guards: the legacy CLI path's output shape must stay
        # byte-for-byte what it always was -- bench and v2 consumers read this file
        # directly off disk and have no envelope-unwrapping logic of their own.
        import json
        findings = self._findings()
        in_path = self._write_input(findings)
        empty_diff = self._empty_diff()
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        try:
            proc = subprocess.run(
                [sys.executable, SCRIPT, in_path,
                 "--diff-file", empty_diff, "--output", out_path],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out_path) as fh:
                output = json.load(fh)
            self.assertEqual(set(output.keys()), {"verified", "eliminated", "batches", "stats"})
            self.assertNotIn("deltas", output)
            self.assertNotIn("receipt", output)
            self.assertNotIn("status", output)
        finally:
            for p in (in_path, empty_diff, out_path):
                os.unlink(p)

    def test_runs_as_subprocess_from_an_unrelated_cwd(self):
        # Regression this guards: "the one failure mode that would break every verify
        # slice of every run" per the comment above the sys.path.append in
        # verify_findings.py -- the sibling `from assemble_artifacts import ...` must
        # resolve via os.path.dirname(os.path.abspath(__file__)), which must NOT
        # depend on the interpreter's cwd. Run from a tempdir that is neither the
        # repo root nor scripts/, with every path passed absolute so this test
        # isolates the import concern from ordinary path-resolution concerns.
        import json
        findings = self._findings()
        in_path = self._write_input(findings)
        empty_diff = self._empty_diff()
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        unrelated_cwd = tempfile.mkdtemp()
        try:
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--input", in_path,
                 "--diff-file", empty_diff, "--output", out_path,
                 "--nonce", "N-2", "--head-sha", "cafef00d"],
                capture_output=True, text=True, cwd=unrelated_cwd,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("ImportError", proc.stderr)
            self.assertNotIn("ModuleNotFoundError", proc.stderr)
            with open(out_path) as fh:
                envelope = json.load(fh)
            self.assertEqual(envelope["status"], "ok", envelope)
            self.assertRegex(
                envelope["receipt"]["deltas_checksum"], r"^fnv1a32:0x[0-9a-f]{8}$"
            )
        finally:
            for p in (in_path, empty_diff, out_path):
                os.unlink(p)
            os.rmdir(unrelated_cwd)

    def test_unserialisable_severity_keeps_ok_envelope_with_null_checksum(self):
        # Regression this guards: when a delta value cannot be spelled identically by
        # JS and Python (here a float severity that classification never overwrote —
        # classify_blame always writes a string origin, but severity is only assigned
        # on a downgrade), deltas_checksum returns None and the receipt must still be
        # status:'ok' with deltas_checksum:null rather than collapsing to
        # status:'failed' and throwing away the legitimate verified/eliminated arrays.
        import json
        findings = self._findings()
        findings[0]["severity"] = 3.14  # JS-unspellable; no downgrade will overwrite it
        in_path = self._write_input(findings)
        empty_diff = self._empty_diff()
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        try:
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--input", in_path,
                 "--diff-file", empty_diff, "--output", out_path,
                 "--nonce", "N-3", "--head-sha", "deadbeef"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out_path) as fh:
                envelope = json.load(fh)
            self.assertEqual(envelope["status"], "ok", envelope)
            self.assertIsNone(envelope["receipt"]["deltas_checksum"])
            self.assertEqual(len(envelope["result"]["verified"]), 2)
            self.assertEqual(envelope["result"]["eliminated"], [])
            # The delta still carries the unserialisable value — the proof is what
            # disclosed the gap, not a silent drop of the field.
            self.assertEqual(envelope["result"]["deltas"][0]["severity"], 3.14)
        finally:
            for p in (in_path, empty_diff, out_path):
                os.unlink(p)


class TestCoerceNumericFields(unittest.TestCase):
    """The --input-boundary int-cast that stops a quoted number ("153") from
    crashing the receipt-path arithmetic ('str' - 'int'). Casts clean integer
    strings; leaves None, real numbers, and non-numeric junk untouched so the
    script's own range/existence guards still fire."""

    def test_string_integers_cast_to_int(self):
        f = {"line_start": "153", "line_end": "155", "confidence": "80", "line": "1", "end_line": "9"}
        _coerce_numeric_fields(f)
        self.assertEqual(f, {"line_start": 153, "line_end": 155, "confidence": 80, "line": 1, "end_line": 9})
        for v in f.values():
            self.assertIsInstance(v, int)

    def test_non_integral_floats_are_normalised_at_the_input_boundary(self):
        # Regression this guards, found by the adversarial review of #25 PR2 and
        # reproduced end to end: a fractional confidence is REACHABLE (registry.js types
        # confidence as JSON-Schema `number` and names legacy/checkpoint-resume findings
        # as a source), and verify_factual's proportional reduction keeps it fractional
        # (max(30, 82.5 - 18) == 64.5). Normalising at the OUTPUT boundary instead would
        # leave this script's on-disk verified[] carrying 64.5 while the delta the
        # workflow joins carried 65 — a silent half-point divergence between what the
        # script computed and what the run delivers. Normalised here, before any
        # verification arithmetic, all four copies agree.
        f = {"confidence": 82.5, "line_start": 4.6, "line_end": 9.2}
        _coerce_numeric_fields(f)
        self.assertEqual(f, {"confidence": 83, "line_start": 5, "line_end": 9})
        for v in f.values():
            self.assertIsInstance(v, int)

    def test_integral_floats_and_nan_inf_keep_the_prior_behaviour(self):
        # An integral float is still normalised to int (harmless, and keeps the delta's
        # JS-reproducible-integer precondition true); NaN/inf are left alone so the
        # script's own range guards reject them exactly as they did before.
        f = {"confidence": 80.0, "line_start": float("nan"), "line_end": float("inf")}
        _coerce_numeric_fields(f)
        self.assertEqual(f["confidence"], 80)
        self.assertIsInstance(f["confidence"], int)
        self.assertNotEqual(f["line_start"], f["line_start"])  # still NaN
        self.assertEqual(f["line_end"], float("inf"))

    def test_signed_and_whitespace_padded_strings_cast(self):
        f = {"line_start": " 42 ", "line_end": "-3"}
        _coerce_numeric_fields(f)
        self.assertEqual(f["line_start"], 42)
        self.assertEqual(f["line_end"], -3)

    def test_non_numeric_and_none_and_real_numbers_untouched(self):
        f = {"line_start": None, "line_end": 12, "confidence": "high", "title": "t", "line": "1.5"}
        _coerce_numeric_fields(f)
        self.assertIsNone(f["line_start"])
        self.assertEqual(f["line_end"], 12)
        self.assertEqual(f["confidence"], "high")  # non-integer string left alone
        self.assertEqual(f["line"], "1.5")          # float string is not a clean int -> left alone
        self.assertEqual(f["title"], "t")

    def test_load_input_casts_numeric_fields(self):
        import json
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"findings": [{"id": "b1", "line_start": "10", "line_end": "12"}]}, f)
            path = f.name
        try:
            data = load_input(path)
            self.assertEqual(data["findings"][0]["line_start"], 10)
            self.assertEqual(data["findings"][0]["line_end"], 12)
        finally:
            os.unlink(path)


class TestReceiptStringLineNumbers(unittest.TestCase):
    """Regression for the live-smoke verify crash: a slice written the way
    verifyStage's writer prompt specifies ({findings, base_branch}) with quoted
    numeric fields must NOT degrade the slice to a status='failed' envelope
    ('unsupported operand type(s) for -: 'str' and 'int'')."""

    def _run_main(self, argv):
        import io
        from scripts.verify_findings import main
        with patch.object(sys, "argv", argv), \
                patch("sys.stderr", new_callable=io.StringIO):
            main()

    def test_receipt_ok_with_string_typed_line_numbers(self):
        import json
        # A real file with enough lines so the (in-range) line reference reaches the
        # arithmetic that crashed on a string line_start; description has no extractable
        # symbols, so no git grep is needed.
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as srcf:
            srcf.write("\n".join(f"line {i}" for i in range(1, 51)) + "\n")
            src_path = srcf.name
        slice_input = {
            "findings": [{
                "id": "bug-1", "dimension": "bug", "severity": "high",
                "confidence": "80", "file": src_path,
                "line_start": "5", "line_end": "7",  # quoted numbers — the crash trigger
                "title": "t", "description": "d", "evidence": "e", "cross_file_refs": [],
            }],
            "base_branch": "main",
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(slice_input, f)
            in_path = f.name
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
            empty_diff = f.name
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        try:
            self._run_main([
                "verify_findings.py", "--input", in_path,
                "--diff-file", empty_diff, "--output", out_path,
                "--nonce", "N.0", "--head-sha", "abc1234",
            ])
            with open(out_path) as fh:
                envelope = json.load(fh)
            self.assertEqual(envelope["status"], "ok", f"expected ok, got {envelope}")
            self.assertEqual(envelope["receipt"]["n_in"], 1)
            verified = envelope["result"]["verified"]
            self.assertEqual(len(verified), 1)
            self.assertEqual(verified[0]["line_start"], 5)  # cast to int at the boundary
        finally:
            for p in (src_path, in_path, empty_diff, out_path):
                os.unlink(p)


class TestEliminationReasonStamp(unittest.TestCase):
    """Cross-runtime invariant (V3.1 item 2, D1): every eliminated finding carries a
    non-empty elimination_reason stamp.

    The JS verify-echo fidelity gate (trustSlice in workflows/src/stages.js) keys on
    this stamp: an eliminated[] entry without a non-empty elimination_reason is treated
    as a fabricated elimination and degrades the whole slice to UNVERIFIED. If this
    script ever grows an elimination branch that omits the stamp, honest eliminations
    would silently start degrading — this test pins the contract on the Python side.
    """

    def test_every_eliminated_finding_carries_elimination_reason_stamp(self):
        import json
        from scripts.verify_findings import run_verification

        # Nonexistent file -> classify_blame short-circuits on os.path.exists and
        # verify_factual eliminates deterministically (no git subprocess dependency).
        finding = {
            "id": "bug-1", "dimension": "bug", "severity": "high", "confidence": 75,
            "file": "nope/does-not-exist-xyz.py", "line_start": 1, "title": "t",
            "description": "d", "evidence": "e", "cross_file_refs": [],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
            empty_diff = f.name  # empty diff -> parse_diff_lines returns set()
        try:
            import io
            with patch("sys.stderr", new_callable=io.StringIO):
                result = run_verification([finding], "main", diff_file=empty_diff)
            verified, eliminated = result["verified"], result["eliminated"]
            self.assertEqual(verified, [])
            self.assertEqual(len(eliminated), 1)
            reason = eliminated[0].get("elimination_reason")
            self.assertIsInstance(reason, str)
            self.assertTrue(reason.strip())
        finally:
            os.unlink(empty_diff)


if __name__ == "__main__":
    unittest.main()
