"""
Tests for scripts/merge_findings.py

Covers:
  - parse_ndjson_file: valid lines, invalid JSON, empty file, missing file, multi-finding
  - parse_text_file: JSON blocks from prose, no JSON, malformed JSON, has_prose, has_skip
  - inject_agent_field: correct agent injected, overwrites existing field
  - deduplicate: NDJSON preferred over text on ID collision, unique IDs kept
  - validate_findings: all 9 known dimensions, unknown dimension (warn+keep),
    missing required fields (reject), missing optional fields (keep)
  - detect_truncation: empty NDJSON + prose → warning; empty NDJSON + skip → no warning;
    populated NDJSON → no warning
  - merge (integration): multi-agent, both channels, correct output schema
  - main (CLI): writes output file, returns 0
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.merge_findings import (
    parse_ndjson_file,
    parse_text_file,
    inject_agent_field,
    deduplicate,
    validate_findings,
    detect_truncation,
    assemble_output,
    merge,
    main,
    KNOWN_DIMENSIONS,
    REQUIRED_FIELDS,
    _ndjson_path,
    _text_path,
    _extract_json_blocks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_finding(**kwargs) -> dict:
    """Build a minimal valid finding dict with sensible defaults."""
    defaults = {
        "id": "bug-1",
        "dimension": "bug",
        "severity": "high",
        "confidence": 85,
        "file": "src/auth.py",
        "line_start": 42,
        "title": "Auth context null dereference",
        "description": "Auth context can be null on API key paths.",
    }
    defaults.update(kwargs)
    return defaults


def _write_ndjson(path: str, findings: list) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for f in findings:
            fh.write(json.dumps(f) + "\n")


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


# ---------------------------------------------------------------------------
# NDJSON parsing
# ---------------------------------------------------------------------------

class TestParseNdjsonFile(unittest.TestCase):

    def test_valid_single_finding(self):
        f = _make_finding()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as fh:
            fh.write(json.dumps(f) + "\n")
            path = fh.name
        try:
            findings, warns = parse_ndjson_file(path, "bug-detector")
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["id"], "bug-1")
            self.assertEqual(warns, [])
        finally:
            os.unlink(path)

    def test_multiple_findings(self):
        items = [_make_finding(id=f"bug-{i}") for i in range(3)]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as fh:
            for item in items:
                fh.write(json.dumps(item) + "\n")
            path = fh.name
        try:
            findings, warns = parse_ndjson_file(path, "bug-detector")
            self.assertEqual(len(findings), 3)
            self.assertEqual([f["id"] for f in findings], ["bug-0", "bug-1", "bug-2"])
        finally:
            os.unlink(path)

    def test_invalid_json_line_skipped_with_warning(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as fh:
            fh.write(json.dumps(_make_finding()) + "\n")
            fh.write("{not valid json}\n")
            fh.write(json.dumps(_make_finding(id="bug-2")) + "\n")
            path = fh.name
        try:
            findings, warns = parse_ndjson_file(path, "bug-detector")
            self.assertEqual(len(findings), 2)
            self.assertEqual(len(warns), 1)
            self.assertIn("invalid JSON", warns[0])
        finally:
            os.unlink(path)

    def test_non_object_line_skipped_with_warning(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as fh:
            fh.write('"just a string"\n')
            path = fh.name
        try:
            findings, warns = parse_ndjson_file(path, "bug-detector")
            self.assertEqual(len(findings), 0)
            self.assertEqual(len(warns), 1)
            self.assertIn("expected object", warns[0].lower())
        finally:
            os.unlink(path)

    def test_empty_file_returns_no_findings(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as fh:
            path = fh.name
        try:
            findings, warns = parse_ndjson_file(path, "bug-detector")
            self.assertEqual(findings, [])
            self.assertEqual(warns, [])
        finally:
            os.unlink(path)

    def test_blank_lines_ignored(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as fh:
            fh.write("\n")
            fh.write(json.dumps(_make_finding()) + "\n")
            fh.write("\n")
            path = fh.name
        try:
            findings, warns = parse_ndjson_file(path, "bug-detector")
            self.assertEqual(len(findings), 1)
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self):
        findings, warns = parse_ndjson_file("/tmp/does-not-exist-abc123.ndjson", "bug-detector")
        self.assertEqual(findings, [])
        self.assertEqual(warns, [])


# ---------------------------------------------------------------------------
# Text file fallback parsing
# ---------------------------------------------------------------------------

class TestParseTextFile(unittest.TestCase):

    def test_json_block_extracted_from_prose(self):
        f = _make_finding()
        content = (
            "I investigated the auth handler.\n\n"
            f"{json.dumps(f)}\n\n"
            "Moving to the next issue.\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as fh:
            fh.write(content)
            path = fh.name
        try:
            findings, warns, has_prose, has_skip = parse_text_file(path, "bug-detector")
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["id"], "bug-1")
            self.assertTrue(has_prose)
        finally:
            os.unlink(path)

    def test_multiple_json_blocks_extracted(self):
        items = [_make_finding(id=f"bug-{i}") for i in range(3)]
        content = "\n".join(json.dumps(item) for item in items)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as fh:
            fh.write(content)
            path = fh.name
        try:
            findings, warns, has_prose, has_skip = parse_text_file(path, "bug-detector")
            self.assertEqual(len(findings), 3)
        finally:
            os.unlink(path)

    def test_no_json_blocks_returns_empty(self):
        content = "I found no issues in this file. The code looks correct."
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as fh:
            fh.write(content)
            path = fh.name
        try:
            findings, warns, has_prose, has_skip = parse_text_file(path, "bug-detector")
            self.assertEqual(findings, [])
            self.assertTrue(has_prose)
            self.assertFalse(has_skip)
        finally:
            os.unlink(path)

    def test_skip_line_detected(self):
        content = "SKIP: off-by-one check was actually correct\nSKIP: boundary is safe"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as fh:
            fh.write(content)
            path = fh.name
        try:
            findings, warns, has_prose, has_skip = parse_text_file(path, "bug-detector")
            self.assertTrue(has_skip)
        finally:
            os.unlink(path)

    def test_json_without_id_field_ignored(self):
        content = '{"dimension": "bug", "severity": "high"}'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as fh:
            fh.write(content)
            path = fh.name
        try:
            findings, warns, has_prose, has_skip = parse_text_file(path, "bug-detector")
            self.assertEqual(findings, [])
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self):
        findings, warns, has_prose, has_skip = parse_text_file(
            "/tmp/does-not-exist-abc123.txt", "bug-detector"
        )
        self.assertEqual(findings, [])
        self.assertFalse(has_prose)
        self.assertFalse(has_skip)

    def test_empty_file_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as fh:
            path = fh.name
        try:
            findings, warns, has_prose, has_skip = parse_text_file(path, "bug-detector")
            self.assertEqual(findings, [])
            self.assertFalse(has_prose)
            self.assertFalse(has_skip)
        finally:
            os.unlink(path)

    def test_skip_line_case_insensitive(self):
        content = "skip: no issue found here"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as fh:
            fh.write(content)
            path = fh.name
        try:
            findings, warns, has_prose, has_skip = parse_text_file(path, "bug-detector")
            self.assertTrue(has_skip)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# _extract_json_blocks
# ---------------------------------------------------------------------------

class TestExtractJsonBlocks(unittest.TestCase):

    def test_extracts_valid_objects_with_id(self):
        f = _make_finding()
        text = f"some text {json.dumps(f)} more text"
        blocks = _extract_json_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["id"], "bug-1")

    def test_ignores_objects_without_id(self):
        text = '{"key": "value"}'
        blocks = _extract_json_blocks(text)
        self.assertEqual(blocks, [])

    def test_multiple_objects_extracted(self):
        items = [_make_finding(id=f"bug-{i}") for i in range(2)]
        text = " ".join(json.dumps(i) for i in items)
        blocks = _extract_json_blocks(text)
        self.assertEqual(len(blocks), 2)

    def test_malformed_json_skipped(self):
        text = '{not valid} {"id": "bug-1", "other": "value"}'
        blocks = _extract_json_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["id"], "bug-1")


# ---------------------------------------------------------------------------
# Agent field injection
# ---------------------------------------------------------------------------

class TestInjectAgentField(unittest.TestCase):

    def test_agent_injected_from_ndjson(self):
        ndjson = {"bug-detector": [_make_finding()]}
        text = {}
        inject_agent_field(ndjson, text)
        self.assertEqual(ndjson["bug-detector"][0]["agent"], "bug-detector")

    def test_agent_injected_from_text(self):
        ndjson = {}
        text = {"security-reviewer": [_make_finding(id="sec-1")]}
        inject_agent_field(ndjson, text)
        self.assertEqual(text["security-reviewer"][0]["agent"], "security-reviewer")

    def test_existing_agent_field_overwritten(self):
        finding = _make_finding(agent="wrong-agent")
        ndjson = {"bug-detector": [finding]}
        text = {}
        inject_agent_field(ndjson, text)
        self.assertEqual(ndjson["bug-detector"][0]["agent"], "bug-detector")

    def test_multiple_agents_each_get_correct_agent(self):
        ndjson = {
            "bug-detector": [_make_finding(id="bug-1")],
            "security-reviewer": [_make_finding(id="sec-1")],
        }
        text = {}
        inject_agent_field(ndjson, text)
        self.assertEqual(ndjson["bug-detector"][0]["agent"], "bug-detector")
        self.assertEqual(ndjson["security-reviewer"][0]["agent"], "security-reviewer")


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplicate(unittest.TestCase):

    def test_ndjson_preferred_over_text_on_id_collision(self):
        ndjson_finding = _make_finding(title="NDJSON version", severity="high")
        text_finding = _make_finding(title="Text version", severity="low")
        ndjson = {"bug-detector": [ndjson_finding]}
        text = {"bug-detector": [text_finding]}
        # Inject agents so we can track source
        inject_agent_field(ndjson, text)
        merged, dupes = deduplicate(ndjson, text)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "NDJSON version")
        self.assertEqual(dupes, 1)

    def test_unique_ids_all_kept(self):
        ndjson = {"bug-detector": [_make_finding(id="bug-1")]}
        text = {"security-reviewer": [_make_finding(id="sec-1")]}
        inject_agent_field(ndjson, text)
        merged, dupes = deduplicate(ndjson, text)
        self.assertEqual(len(merged), 2)
        self.assertEqual(dupes, 0)

    def test_finding_without_id_not_included(self):
        finding = {"severity": "high", "title": "no id"}
        ndjson = {"bug-detector": [finding]}
        text = {}
        inject_agent_field(ndjson, text)
        merged, dupes = deduplicate(ndjson, text)
        self.assertEqual(len(merged), 0)

    def test_multiple_agents_same_finding_in_both_channels(self):
        shared = _make_finding(id="shared-1")
        ndjson = {"bug-detector": [dict(shared, title="NDJSON")]}
        text = {"bug-detector": [dict(shared, title="Text")]}
        inject_agent_field(ndjson, text)
        merged, dupes = deduplicate(ndjson, text)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "NDJSON")
        self.assertGreater(dupes, 0)

    def test_empty_channels_returns_empty(self):
        merged, dupes = deduplicate({}, {})
        self.assertEqual(merged, [])
        self.assertEqual(dupes, 0)


# ---------------------------------------------------------------------------
# Dimension and field validation
# ---------------------------------------------------------------------------

class TestValidateFindings(unittest.TestCase):

    def test_all_known_dimensions_accepted(self):
        for dim in KNOWN_DIMENSIONS:
            findings = [_make_finding(id=f"f-{dim}", dimension=dim)]
            valid, warns = validate_findings(findings)
            self.assertEqual(len(valid), 1, f"Dimension {dim!r} should be accepted")
            # Warn only expected for missing/unknown; known dims should not warn about dim
            dim_warns = [w for w in warns if "dimension" in w.lower()]
            self.assertEqual(dim_warns, [])

    def test_unknown_dimension_warns_but_keeps_finding(self):
        findings = [_make_finding(dimension="foobar_unknown")]
        valid, warns = validate_findings(findings)
        self.assertEqual(len(valid), 1)
        self.assertTrue(any("foobar_unknown" in w for w in warns))

    def test_missing_dimension_warns_but_keeps_finding(self):
        f = _make_finding()
        del f["dimension"]
        valid, warns = validate_findings([f])
        self.assertEqual(len(valid), 1)
        self.assertTrue(any("dimension" in w.lower() for w in warns))

    def test_missing_id_rejects_finding(self):
        f = _make_finding()
        del f["id"]
        valid, warns = validate_findings([f])
        self.assertEqual(len(valid), 0)
        self.assertTrue(any("id" in w for w in warns))

    def test_missing_file_rejects_finding(self):
        f = _make_finding()
        del f["file"]
        valid, warns = validate_findings([f])
        self.assertEqual(len(valid), 0)
        self.assertTrue(any("file" in w for w in warns))

    def test_missing_line_start_rejects_finding(self):
        f = _make_finding()
        del f["line_start"]
        valid, warns = validate_findings([f])
        self.assertEqual(len(valid), 0)

    def test_missing_title_rejects_finding(self):
        f = _make_finding()
        del f["title"]
        valid, warns = validate_findings([f])
        self.assertEqual(len(valid), 0)

    def test_missing_description_rejects_finding(self):
        f = _make_finding()
        del f["description"]
        valid, warns = validate_findings([f])
        self.assertEqual(len(valid), 0)

    def test_missing_severity_rejects_finding(self):
        f = _make_finding()
        del f["severity"]
        valid, warns = validate_findings([f])
        self.assertEqual(len(valid), 0)

    def test_missing_confidence_rejects_finding(self):
        f = _make_finding()
        del f["confidence"]
        valid, warns = validate_findings([f])
        self.assertEqual(len(valid), 0)

    def test_missing_optional_fields_accepted(self):
        """evidence, suggestion, suggested_fix_code, cross_file_refs are optional."""
        f = _make_finding()
        # Ensure none of the optional fields are present
        for field in ("evidence", "suggestion", "suggested_fix_code", "cross_file_refs", "line_end"):
            f.pop(field, None)
        valid, warns = validate_findings([f])
        self.assertEqual(len(valid), 1)

    def test_empty_required_field_rejects(self):
        f = _make_finding(id="")
        valid, warns = validate_findings([f])
        self.assertEqual(len(valid), 0)


# ---------------------------------------------------------------------------
# Truncation detection
# ---------------------------------------------------------------------------

class TestDetectTruncation(unittest.TestCase):

    def _run(self, ndjson_raw_counts, text_findings, text_has_prose, text_has_skip):
        agents = (
            list(ndjson_raw_counts.keys())
            or list(text_has_prose.keys())
        )
        return detect_truncation(
            agents=agents,
            ndjson_raw_counts=ndjson_raw_counts,
            text_findings=text_findings,
            text_has_prose=text_has_prose,
            text_has_skip=text_has_skip,
            ndjson_paths={a: f"/tmp/{a}.ndjson" for a in agents},
        )

    def test_empty_ndjson_and_prose_triggers_warning(self):
        warns = self._run(
            ndjson_raw_counts={"bug-detector": 0},
            text_findings={"bug-detector": []},
            text_has_prose={"bug-detector": True},
            text_has_skip={"bug-detector": False},
        )
        self.assertEqual(len(warns), 1)
        self.assertIn("bug-detector", warns[0])
        self.assertIn("truncation", warns[0].lower())

    def test_empty_ndjson_with_skip_lines_no_warning(self):
        warns = self._run(
            ndjson_raw_counts={"bug-detector": 0},
            text_findings={"bug-detector": []},
            text_has_prose={"bug-detector": True},
            text_has_skip={"bug-detector": True},
        )
        self.assertEqual(warns, [])

    def test_populated_ndjson_no_warning(self):
        warns = self._run(
            ndjson_raw_counts={"bug-detector": 1},
            text_findings={"bug-detector": []},
            text_has_prose={"bug-detector": True},
            text_has_skip={"bug-detector": False},
        )
        self.assertEqual(warns, [])

    def test_empty_ndjson_no_prose_no_warning(self):
        warns = self._run(
            ndjson_raw_counts={"bug-detector": 0},
            text_findings={"bug-detector": []},
            text_has_prose={"bug-detector": False},
            text_has_skip={"bug-detector": False},
        )
        self.assertEqual(warns, [])

    def test_multiple_agents_truncation_detected_per_agent(self):
        agents = ["bug-detector", "security-reviewer"]
        warns = detect_truncation(
            agents=agents,
            ndjson_raw_counts={"bug-detector": 0, "security-reviewer": 1},
            text_findings={"bug-detector": [], "security-reviewer": []},
            text_has_prose={"bug-detector": True, "security-reviewer": True},
            text_has_skip={"bug-detector": False, "security-reviewer": False},
            ndjson_paths={a: f"/tmp/{a}.ndjson" for a in agents},
        )
        self.assertEqual(len(warns), 1)
        self.assertIn("bug-detector", warns[0])

    def test_m4_no_false_positive_when_ndjson_has_invalid_findings(self):
        """M4: findings that fail validation must not make NDJSON appear empty.

        Pre-validation raw count is 1 (NDJSON had content), so no truncation
        warning should be emitted even though post-validation count is 0.
        """
        warns = self._run(
            ndjson_raw_counts={"bug-detector": 1},  # raw count: file had content
            text_findings={"bug-detector": []},
            text_has_prose={"bug-detector": True},
            text_has_skip={"bug-detector": False},
        )
        self.assertEqual(warns, [])

    def test_m5_no_false_positive_when_text_has_valid_json_blocks(self):
        """M5: text fallback with valid JSON blocks must not trigger truncation.

        Even though NDJSON is empty, the text channel has valid findings, so
        there is no truncation — the agent delivered its output via text.
        """
        warns = self._run(
            ndjson_raw_counts={"bug-detector": 0},
            text_findings={"bug-detector": [_make_finding()]},  # text has findings
            text_has_prose={"bug-detector": True},
            text_has_skip={"bug-detector": False},
        )
        self.assertEqual(warns, [])


# ---------------------------------------------------------------------------
# Integration: merge()
# ---------------------------------------------------------------------------

class TestMerge(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.session_sha = "abc12345"
        self.agents = ["bug-detector", "security-reviewer"]

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _ndjson_path(self, agent):
        return os.path.join(self.tmpdir, f"deep-review-{agent}-{self.session_sha}.ndjson")

    def _text_path(self, agent):
        return os.path.join(self.tmpdir, f"deep-review-text-{agent}-{self.session_sha}.txt")

    def _run_merge(self):
        return merge(
            findings_dir=self.tmpdir,
            session_sha=self.session_sha,
            agents=self.agents,
            text_dir=self.tmpdir,
            base_branch="main",
            head_sha="abc123full",
            pr_number=42,
            owner="org",
            repo="myrepo",
        )

    def test_output_has_required_top_level_keys(self):
        result = self._run_merge()
        for key in ("findings", "base_branch", "head_sha", "pr_number", "owner", "repo", "methodology"):
            self.assertIn(key, result)

    def test_output_metadata_correct(self):
        result = self._run_merge()
        self.assertEqual(result["base_branch"], "main")
        self.assertEqual(result["head_sha"], "abc123full")
        self.assertEqual(result["pr_number"], 42)
        self.assertEqual(result["owner"], "org")
        self.assertEqual(result["repo"], "myrepo")

    def test_methodology_block_populated(self):
        result = self._run_merge()
        m = result["methodology"]
        self.assertIn("agents_dispatched", m)
        self.assertIn("findings_per_channel", m)
        self.assertIn("duplicates_resolved", m)
        self.assertIn("truncation_warnings", m)
        self.assertIn("validation_warnings", m)
        self.assertEqual(m["agents_dispatched"], self.agents)

    def test_ndjson_findings_included(self):
        _write_ndjson(self._ndjson_path("bug-detector"), [_make_finding(id="bug-1")])
        result = self._run_merge()
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["id"], "bug-1")
        self.assertEqual(result["findings"][0]["agent"], "bug-detector")

    def test_text_fallback_findings_included(self):
        f = _make_finding(id="sec-1", dimension="security")
        _write_text(self._text_path("security-reviewer"), json.dumps(f))
        result = self._run_merge()
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["id"], "sec-1")
        self.assertEqual(result["methodology"]["findings_per_channel"]["text_fallback"], 1)

    def test_ndjson_preferred_over_text_fallback(self):
        f_ndjson = _make_finding(id="bug-1", title="From NDJSON")
        f_text = _make_finding(id="bug-1", title="From Text")
        _write_ndjson(self._ndjson_path("bug-detector"), [f_ndjson])
        _write_text(self._text_path("bug-detector"), json.dumps(f_text))
        result = self._run_merge()
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["title"], "From NDJSON")
        self.assertEqual(result["methodology"]["duplicates_resolved"], 1)

    def test_multi_agent_findings_combined(self):
        _write_ndjson(self._ndjson_path("bug-detector"), [_make_finding(id="bug-1")])
        f2 = _make_finding(id="sec-1", dimension="security")
        _write_ndjson(self._ndjson_path("security-reviewer"), [f2])
        result = self._run_merge()
        self.assertEqual(len(result["findings"]), 2)

    def test_invalid_findings_excluded(self):
        """Findings missing required fields should be excluded."""
        bad = {"dimension": "bug", "severity": "high"}  # missing id, file, line_start, title, description, confidence
        _write_ndjson(self._ndjson_path("bug-detector"), [bad])
        result = self._run_merge()
        self.assertEqual(len(result["findings"]), 0)
        self.assertTrue(len(result["methodology"]["validation_warnings"]) > 0)

    def test_truncation_warning_in_output(self):
        # bug-detector has no NDJSON and has prose text
        _write_text(self._text_path("bug-detector"),
                    "I found a potential null dereference in the auth handler.")
        result = self._run_merge()
        self.assertTrue(len(result["methodology"]["truncation_warnings"]) > 0)

    def test_no_truncation_warning_when_skip_present(self):
        _write_text(self._text_path("bug-detector"), "SKIP: no issues found")
        result = self._run_merge()
        self.assertEqual(result["methodology"]["truncation_warnings"], [])

    def test_no_truncation_warning_when_ndjson_populated(self):
        _write_ndjson(self._ndjson_path("bug-detector"), [_make_finding(id="bug-1")])
        _write_text(self._text_path("bug-detector"), "I found a real issue.")
        result = self._run_merge()
        self.assertEqual(result["methodology"]["truncation_warnings"], [])

    def test_m4_no_false_positive_when_ndjson_findings_fail_validation(self):
        """M4: NDJSON findings that fail validation must not produce a truncation warning.

        The NDJSON file has content (one finding), but that finding lacks required
        fields and is rejected during validation.  Before the M4 fix, truncation
        detection used the post-validation ndjson_findings dict (now empty after
        filtering), which incorrectly triggered a truncation warning.
        With the fix, the pre-validation raw count (1) is used, so no warning fires.
        """
        # Write an NDJSON finding missing required fields (id, file, line_start, …)
        bad_finding = {"dimension": "bug", "severity": "high"}
        _write_ndjson(self._ndjson_path("bug-detector"), [bad_finding])
        # Also write prose text so the old code would have flagged truncation
        _write_text(self._text_path("bug-detector"),
                    "I investigated the handler and found a potential issue.")
        result = self._run_merge()
        self.assertEqual(result["methodology"]["truncation_warnings"], [])

    def test_m5_no_false_positive_when_text_has_valid_json_blocks(self):
        """M5: text fallback with valid JSON blocks must not trigger truncation.

        NDJSON is absent (empty), but the text file contains a valid finding as a
        JSON block.  The agent delivered its output via the text channel, so
        truncation should not be reported.
        """
        f = _make_finding(id="bug-1")
        # No NDJSON file — only text fallback with a valid JSON finding
        _write_text(
            self._text_path("bug-detector"),
            "I investigated the handler.\n" + json.dumps(f) + "\nLooks serious.",
        )
        result = self._run_merge()
        self.assertEqual(result["methodology"]["truncation_warnings"], [])

    def test_ndjson_count_in_methodology(self):
        _write_ndjson(self._ndjson_path("bug-detector"), [
            _make_finding(id="bug-1"),
            _make_finding(id="bug-2"),
        ])
        result = self._run_merge()
        self.assertEqual(result["methodology"]["findings_per_channel"]["ndjson"], 2)

    def test_empty_run_produces_valid_output(self):
        result = self._run_merge()
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["methodology"]["findings_per_channel"]["ndjson"], 0)
        self.assertEqual(result["methodology"]["findings_per_channel"]["text_fallback"], 0)
        self.assertEqual(result["methodology"]["duplicates_resolved"], 0)


# ---------------------------------------------------------------------------
# CLI: main()
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.session_sha = "deadbeef"
        self.agent = "bug-detector"
        ndjson_path = os.path.join(
            self.tmpdir, f"deep-review-{self.agent}-{self.session_sha}.ndjson"
        )
        _write_ndjson(ndjson_path, [_make_finding(id="bug-1")])
        self.output_path = os.path.join(self.tmpdir, "output.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _argv(self, **kwargs):
        defaults = {
            "--findings-dir": self.tmpdir,
            "--session-sha": self.session_sha,
            "--agents": [self.agent],
            "--text-dir": self.tmpdir,
            "--base-branch": "main",
            "--head-sha": "abc123",
            "--pr-number": "42",
            "--owner": "org",
            "--repo": "repo",
            "--output": self.output_path,
        }
        defaults.update(kwargs)
        argv = []
        for key, val in defaults.items():
            argv.append(key)
            if isinstance(val, list):
                argv.extend(val)
            else:
                argv.append(val)
        return argv

    def test_cli_exits_zero(self):
        rc = main(self._argv())
        self.assertEqual(rc, 0)

    def test_cli_writes_output_file(self):
        main(self._argv())
        self.assertTrue(os.path.exists(self.output_path))

    def test_cli_output_is_valid_json(self):
        main(self._argv())
        with open(self.output_path) as fh:
            data = json.load(fh)
        self.assertIn("findings", data)
        self.assertIn("methodology", data)

    def test_cli_output_contains_finding(self):
        main(self._argv())
        with open(self.output_path) as fh:
            data = json.load(fh)
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["id"], "bug-1")

    def test_cli_multiple_agents(self):
        second_agent = "security-reviewer"
        ndjson_path2 = os.path.join(
            self.tmpdir, f"deep-review-{second_agent}-{self.session_sha}.ndjson"
        )
        _write_ndjson(ndjson_path2, [_make_finding(id="sec-1", dimension="security")])
        rc = main(self._argv(**{"--agents": [self.agent, second_agent]}))
        self.assertEqual(rc, 0)
        with open(self.output_path) as fh:
            data = json.load(fh)
        self.assertEqual(len(data["findings"]), 2)


if __name__ == "__main__":
    unittest.main()
