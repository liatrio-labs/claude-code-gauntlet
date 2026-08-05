"""
Tests for scripts/apply_validations.py

Covers:
  - load_findings: bare array, verified-envelope, findings-envelope, missing file,
    invalid JSON, wrong shape
  - load_validations: bare array, validations-envelope, missing file, invalid JSON,
    wrong shape
  - apply_validations: confidence updated, original_confidence saved, justification
    copied, pass-through for unmatched findings, unmatched validation ids,
    confidence clamping (0-100), missing id in validation, missing confidence
    in validation, non-integer confidence
  - main() CLI integration: stdout output, --output file, unmatched warnings,
    envelope key preservation (eliminated, batches)
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.apply_validations import (
    apply_validations,
    load_findings,
    load_validations,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_finding(**kwargs):
    defaults = {
        "id": "bug-1",
        "file": "src/foo.py",
        "line_start": 10,
        "line_end": 12,
        "severity": "high",
        "confidence": 80,
        "title": "Null pointer dereference",
        "description": "The function does not guard against null input.",
    }
    defaults.update(kwargs)
    return defaults


def _write_json(data):
    """Write data to a temp file, return the path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        return f.name


# ---------------------------------------------------------------------------
# load_findings
# ---------------------------------------------------------------------------


class TestLoadFindings(unittest.TestCase):
    def test_bare_array(self):
        findings = [_make_finding(id="bug-1"), _make_finding(id="bug-2")]
        path = _write_json(findings)
        try:
            result, envelope = load_findings(path)
            self.assertEqual(len(result), 2)
            self.assertIsNone(envelope)
        finally:
            os.unlink(path)

    def test_verified_envelope(self):
        """verify_findings.py output shape: {"verified": [...], "eliminated": [...]}"""
        data = {
            "verified": [_make_finding(id="bug-1")],
            "eliminated": [_make_finding(id="bug-99")],
            "batches": [["bug-1"]],
            "stats": {"total": 2},
        }
        path = _write_json(data)
        try:
            result, envelope = load_findings(path)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["id"], "bug-1")
            # Envelope preserved for round-tripping extra keys
            self.assertIn("eliminated", envelope)
            self.assertIn("batches", envelope)
        finally:
            os.unlink(path)

    def test_findings_envelope(self):
        """Generic {"findings": [...]} envelope."""
        data = {"findings": [_make_finding(id="sec-1")]}
        path = _write_json(data)
        try:
            result, envelope = load_findings(path)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["id"], "sec-1")
            self.assertIsNotNone(envelope)
        finally:
            os.unlink(path)

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            load_findings("/nonexistent/path/findings.json")

    def test_invalid_json_exits(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{not valid json}")
        try:
            with self.assertRaises(SystemExit):
                load_findings(f.name)
        finally:
            os.unlink(f.name)

    def test_wrong_shape_exits(self):
        path = _write_json("just a string")
        try:
            with self.assertRaises(SystemExit):
                load_findings(path)
        finally:
            os.unlink(path)

    def test_dict_without_findings_or_verified_exits(self):
        path = _write_json({"other_key": []})
        try:
            with self.assertRaises(SystemExit):
                load_findings(path)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# load_validations
# ---------------------------------------------------------------------------


class TestLoadValidations(unittest.TestCase):
    def test_bare_array(self):
        data = [{"id": "bug-1", "confidence": 72}]
        path = _write_json(data)
        try:
            result = load_validations(path)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["id"], "bug-1")
        finally:
            os.unlink(path)

    def test_validations_envelope(self):
        data = {"validations": [{"id": "bug-1", "confidence": 85}]}
        path = _write_json(data)
        try:
            result = load_validations(path)
            self.assertEqual(len(result), 1)
        finally:
            os.unlink(path)

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            load_validations("/nonexistent/validations.json")

    def test_invalid_json_exits(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json")
        try:
            with self.assertRaises(SystemExit):
                load_validations(f.name)
        finally:
            os.unlink(f.name)

    def test_wrong_shape_exits(self):
        path = _write_json(42)
        try:
            with self.assertRaises(SystemExit):
                load_validations(path)
        finally:
            os.unlink(path)

    def test_dict_without_validations_key_exits(self):
        path = _write_json({"results": []})
        try:
            with self.assertRaises(SystemExit):
                load_validations(path)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# apply_validations — core logic
# ---------------------------------------------------------------------------


class TestApplyValidations(unittest.TestCase):
    def test_confidence_updated(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "bug-1", "confidence": 55}]
        adjusted, unmatched = apply_validations(findings, validations)
        self.assertEqual(adjusted, 1)
        self.assertEqual(findings[0]["confidence"], 55)

    def test_original_confidence_saved_before_update(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "bug-1", "confidence": 55}]
        apply_validations(findings, validations)
        self.assertEqual(findings[0]["original_confidence"], 80)

    def test_original_confidence_saved_even_when_unchanged(self):
        """original_confidence set even if new confidence == old confidence."""
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "bug-1", "confidence": 80}]
        apply_validations(findings, validations)
        self.assertIn("original_confidence", findings[0])
        self.assertEqual(findings[0]["original_confidence"], 80)

    def test_validator_confidence_field_set(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "bug-1", "confidence": 65}]
        apply_validations(findings, validations)
        self.assertEqual(findings[0]["validator_confidence"], 65)

    def test_justification_copied_when_present(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [
            {"id": "bug-1", "confidence": 72, "justification": "Code path confirmed."}
        ]
        apply_validations(findings, validations)
        self.assertEqual(
            findings[0]["validation_justification"], "Code path confirmed."
        )

    def test_justification_not_set_when_absent(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "bug-1", "confidence": 72}]
        apply_validations(findings, validations)
        self.assertNotIn("validation_justification", findings[0])

    def test_pass_through_for_unvalidated_finding(self):
        """Findings without a matching validation are returned unchanged."""
        findings = [
            _make_finding(id="bug-1", confidence=80),
            _make_finding(id="sec-1", confidence=90),
        ]
        validations = [{"id": "bug-1", "confidence": 60}]
        adjusted, unmatched = apply_validations(findings, validations)
        self.assertEqual(adjusted, 1)
        # sec-1 passes through: confidence unchanged, no original_confidence
        sec = findings[1]
        self.assertEqual(sec["confidence"], 90)
        self.assertNotIn("original_confidence", sec)

    def test_unmatched_validation_id_reported(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "ghost-99", "confidence": 50}]
        adjusted, unmatched = apply_validations(findings, validations)
        self.assertEqual(adjusted, 0)
        self.assertIn("ghost-99", unmatched)

    def test_confidence_clamped_to_zero(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "bug-1", "confidence": -10}]
        apply_validations(findings, validations)
        self.assertEqual(findings[0]["confidence"], 0)

    def test_confidence_clamped_to_100(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "bug-1", "confidence": 150}]
        apply_validations(findings, validations)
        self.assertEqual(findings[0]["confidence"], 100)

    def test_validation_missing_id_skipped(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"confidence": 50}]  # no id
        adjusted, unmatched = apply_validations(findings, validations)
        self.assertEqual(adjusted, 0)
        self.assertEqual(findings[0]["confidence"], 80)

    def test_validation_missing_confidence_skipped(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "bug-1"}]  # no confidence
        adjusted, unmatched = apply_validations(findings, validations)
        self.assertEqual(adjusted, 0)
        self.assertEqual(findings[0]["confidence"], 80)

    def test_non_integer_confidence_skipped(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "bug-1", "confidence": "high"}]
        adjusted, unmatched = apply_validations(findings, validations)
        self.assertEqual(adjusted, 0)
        self.assertEqual(findings[0]["confidence"], 80)

    def test_multiple_findings_multiple_validations(self):
        findings = [
            _make_finding(id="bug-1", confidence=80),
            _make_finding(id="bug-2", confidence=90),
            _make_finding(id="sec-1", confidence=75),
        ]
        validations = [
            {"id": "bug-1", "confidence": 55},
            {"id": "bug-2", "confidence": 95},
        ]
        adjusted, unmatched = apply_validations(findings, validations)
        self.assertEqual(adjusted, 2)
        self.assertEqual(unmatched, [])
        self.assertEqual(findings[0]["confidence"], 55)
        self.assertEqual(findings[0]["original_confidence"], 80)
        self.assertEqual(findings[1]["confidence"], 95)
        self.assertEqual(findings[1]["original_confidence"], 90)
        # sec-1 untouched
        self.assertEqual(findings[2]["confidence"], 75)
        self.assertNotIn("original_confidence", findings[2])

    def test_empty_findings_list(self):
        adjusted, unmatched = apply_validations([], [{"id": "x", "confidence": 50}])
        self.assertEqual(adjusted, 0)
        self.assertEqual(unmatched, ["x"])

    def test_empty_validations_list(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        adjusted, unmatched = apply_validations(findings, [])
        self.assertEqual(adjusted, 0)
        self.assertEqual(unmatched, [])
        self.assertEqual(findings[0]["confidence"], 80)

    def test_returns_adjusted_count(self):
        findings = [
            _make_finding(id="bug-1"),
            _make_finding(id="bug-2"),
        ]
        validations = [
            {"id": "bug-1", "confidence": 60},
            {"id": "bug-2", "confidence": 70},
        ]
        adjusted, _ = apply_validations(findings, validations)
        self.assertEqual(adjusted, 2)

    def test_finding_missing_confidence_treated_as_zero(self):
        """Finding with no 'confidence' key: original_confidence saved as 0."""
        finding = {"id": "bug-1", "file": "src/x.py", "title": "T"}
        validations = [{"id": "bug-1", "confidence": 65}]
        apply_validations([finding], validations)
        self.assertEqual(finding["original_confidence"], 0)
        self.assertEqual(finding["confidence"], 65)

    def test_float_confidence_rounded_to_int(self):
        """Validator emitting 72.5 — converted via int() to 72."""
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "bug-1", "confidence": 72.5}]
        apply_validations(findings, validations)
        self.assertIsInstance(findings[0]["confidence"], int)
        self.assertEqual(findings[0]["confidence"], 72)


# ---------------------------------------------------------------------------
# CLI integration — main()
# ---------------------------------------------------------------------------


class TestApplyValidationsMain(unittest.TestCase):
    def _run_main(self, findings_data, validations_data, extra_args=None):
        """Helper: write both inputs to temp files, run main(), return stdout."""
        import io
        from unittest.mock import patch

        from scripts.apply_validations import main

        findings_path = _write_json(findings_data)
        validations_path = _write_json(validations_data)
        try:
            argv = ["apply_validations.py", findings_path, validations_path]
            if extra_args:
                argv.extend(extra_args)
            captured = io.StringIO()
            with patch("sys.argv", argv), patch("sys.stdout", captured):
                main()
            return captured.getvalue()
        finally:
            os.unlink(findings_path)
            os.unlink(validations_path)

    def test_stdout_output_valid_json(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "bug-1", "confidence": 60}]
        output = self._run_main(findings, validations)
        result = json.loads(output)
        self.assertIn("findings", result)
        self.assertIn("stats", result)

    def test_output_findings_contains_all_findings(self):
        findings = [
            _make_finding(id="bug-1", confidence=80),
            _make_finding(id="bug-2", confidence=75),
        ]
        validations = [{"id": "bug-1", "confidence": 55}]
        output = self._run_main(findings, validations)
        result = json.loads(output)
        ids = {f["id"] for f in result["findings"]}
        self.assertEqual(ids, {"bug-1", "bug-2"})

    def test_stats_adjusted_count(self):
        findings = [
            _make_finding(id="bug-1", confidence=80),
            _make_finding(id="sec-1", confidence=90),
        ]
        validations = [{"id": "bug-1", "confidence": 60}]
        output = self._run_main(findings, validations)
        result = json.loads(output)
        self.assertEqual(result["stats"]["adjusted"], 1)
        self.assertEqual(result["stats"]["total"], 2)
        self.assertEqual(result["stats"]["pass_through"], 1)

    def test_stats_unmatched_count(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "ghost", "confidence": 50}]
        output = self._run_main(findings, validations)
        result = json.loads(output)
        self.assertEqual(result["stats"]["unmatched"], 1)

    def test_output_file_flag(self):
        findings = [_make_finding(id="bug-1", confidence=80)]
        validations = [{"id": "bug-1", "confidence": 65}]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as out_file:
            out_path = out_file.name

        try:
            self._run_main(findings, validations, extra_args=["--output", out_path])
            with open(out_path) as fh:
                result = json.loads(fh.read())
            self.assertIn("findings", result)
            self.assertEqual(result["findings"][0]["confidence"], 65)
        finally:
            os.unlink(out_path)

    def test_output_flag_leaves_stdout_empty(self):
        import io
        from unittest.mock import patch

        from scripts.apply_validations import main

        findings_path = _write_json([_make_finding(id="bug-1", confidence=80)])
        validations_path = _write_json([{"id": "bug-1", "confidence": 65}])
        out_path = tempfile.mktemp(suffix=".json")
        captured_out = io.StringIO()
        captured_err = io.StringIO()
        try:
            argv = [
                "apply_validations.py",
                findings_path,
                validations_path,
                "--output",
                out_path,
            ]
            with (
                patch("sys.argv", argv),
                patch("sys.stdout", captured_out),
                patch("sys.stderr", captured_err),
            ):
                main()
            self.assertEqual(captured_out.getvalue(), "")
            self.assertIn("Output written", captured_err.getvalue())
            self.assertTrue(os.path.exists(out_path))
        finally:
            os.unlink(findings_path)
            os.unlink(validations_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_envelope_keys_preserved_in_output(self):
        """eliminated and batches from verify_findings.py are preserved."""
        data = {
            "verified": [_make_finding(id="bug-1", confidence=80)],
            "eliminated": [_make_finding(id="bug-99")],
            "batches": [["bug-1"]],
            "stats": {"total": 2},
        }
        validations = [{"id": "bug-1", "confidence": 70}]
        findings_path = _write_json(data)
        validations_path = _write_json(validations)
        import io
        from unittest.mock import patch

        from scripts.apply_validations import main

        try:
            argv = ["apply_validations.py", findings_path, validations_path]
            captured = io.StringIO()
            with patch("sys.argv", argv), patch("sys.stdout", captured):
                main()
            result = json.loads(captured.getvalue())
            self.assertIn("eliminated", result)
            self.assertIn("batches", result)
        finally:
            os.unlink(findings_path)
            os.unlink(validations_path)

    def test_generated_at_present(self):
        findings = [_make_finding(id="bug-1")]
        validations = [{"id": "bug-1", "confidence": 70}]
        output = self._run_main(findings, validations)
        result = json.loads(output)
        self.assertIn("generated_at", result)

    def test_verified_envelope_input(self):
        """Input from verify_findings.py uses "verified" key."""
        data = {
            "verified": [_make_finding(id="bug-1", confidence=85)],
            "eliminated": [],
            "batches": [["bug-1"]],
            "stats": {"total": 1, "new": 1, "surfaced": 0, "eliminated": 0},
        }
        validations = [{"id": "bug-1", "confidence": 70}]
        findings_path = _write_json(data)
        validations_path = _write_json(validations)
        import io
        from unittest.mock import patch

        from scripts.apply_validations import main

        try:
            argv = ["apply_validations.py", findings_path, validations_path]
            captured = io.StringIO()
            with patch("sys.argv", argv), patch("sys.stdout", captured):
                main()
            result = json.loads(captured.getvalue())
            self.assertEqual(result["findings"][0]["confidence"], 70)
            self.assertEqual(result["findings"][0]["original_confidence"], 85)
        finally:
            os.unlink(findings_path)
            os.unlink(validations_path)


if __name__ == "__main__":
    unittest.main()
