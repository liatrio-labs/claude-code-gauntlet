#!/usr/bin/env python3
"""Tests for scripts/finding_dedup.py"""

import json
import os
import sys
import tempfile
import unittest

# Allow import from scripts/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.finding_dedup import (
    dedup_by_id,
    dedup_by_location,
    FindingStore,
    _id_key,
    _location_key,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _f(
    fid="F001",
    file="src/auth.py",
    line_start=10,
    line_end=15,
    dimension="security",
    title="SQL injection",
    severity="high",
    confidence=80,
    **extra,
) -> dict:
    f = {
        "id": fid,
        "file": file,
        "line_start": line_start,
        "line_end": line_end,
        "dimension": dimension,
        "title": title,
        "description": "User input passed directly to query.",
        "severity": severity,
        "confidence": confidence,
    }
    f.update(extra)
    return f


# ---------------------------------------------------------------------------
# _location_key
# ---------------------------------------------------------------------------

class TestLocationKey(unittest.TestCase):
    def test_returns_4_tuple(self):
        key = _location_key(_f())
        self.assertEqual(len(key), 4)

    def test_same_location_same_key(self):
        self.assertEqual(_location_key(_f(fid="A")), _location_key(_f(fid="B")))

    def test_different_file_different_key(self):
        self.assertNotEqual(_location_key(_f(file="a.py")), _location_key(_f(file="b.py")))

    def test_different_line_different_key(self):
        self.assertNotEqual(_location_key(_f(line_start=1)), _location_key(_f(line_start=2)))

    def test_different_dimension_different_key(self):
        self.assertNotEqual(
            _location_key(_f(dimension="security")),
            _location_key(_f(dimension="bug")),
        )

    def test_missing_fields_safe(self):
        key = _location_key({})
        self.assertIsInstance(key, tuple)


# ---------------------------------------------------------------------------
# dedup_by_id
# ---------------------------------------------------------------------------

class TestDedupById(unittest.TestCase):
    def test_no_collision(self):
        f1 = _f(fid="A")
        f2 = _f(fid="B", file="other.py")
        merged, dupes, dropped = dedup_by_id({"agent1": [f1]}, {"agent2": [f2]})
        self.assertEqual(len(merged), 2)
        self.assertEqual(dupes, 0)
        self.assertEqual(dropped, 0)

    def test_ndjson_wins_over_text(self):
        f_text = _f(fid="A", title="from text")
        f_ndjson = _f(fid="A", title="from ndjson")
        merged, dupes, dropped = dedup_by_id({"ndjson-agent": [f_ndjson]}, {"text-agent": [f_text]})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "from ndjson")
        self.assertEqual(dupes, 1)
        self.assertEqual(dropped, 0)

    def test_finding_without_id_tracked_as_dropped(self):
        f_no_id = {"file": "x.py", "title": "no id"}
        f_with_id = _f(fid="B")
        merged, dupes, dropped = dedup_by_id({"a": [f_no_id, f_with_id]}, {})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "B")
        self.assertEqual(dupes, 0)
        self.assertEqual(dropped, 1)

    def test_empty_inputs(self):
        merged, dupes, dropped = dedup_by_id({}, {})
        self.assertEqual(merged, [])
        self.assertEqual(dupes, 0)
        self.assertEqual(dropped, 0)

    def test_multiple_agents_same_id(self):
        f1 = _f(fid="X")
        f2 = _f(fid="X")
        merged, dupes, dropped = dedup_by_id({"a": [f1], "b": [f2]}, {})
        self.assertEqual(len(merged), 1)
        self.assertEqual(dupes, 1)
        self.assertEqual(dropped, 0)


# ---------------------------------------------------------------------------
# dedup_by_location
# ---------------------------------------------------------------------------

class TestDedupByLocation(unittest.TestCase):
    def test_no_collision(self):
        f1 = _f(fid="A", file="a.py")
        f2 = _f(fid="B", file="b.py")
        merged, dupes = dedup_by_location([f1, f2])
        self.assertEqual(len(merged), 2)
        self.assertEqual(dupes, 0)

    def test_same_location_higher_confidence_wins(self):
        f_high = _f(fid="A", confidence=90)
        f_low = _f(fid="B", confidence=50)
        merged, dupes = dedup_by_location([f_low, f_high])
        self.assertEqual(len(merged), 1)
        self.assertEqual(dupes, 1)
        self.assertEqual(merged[0]["confidence"], 90)

    def test_same_location_first_wins_on_tie(self):
        f1 = _f(fid="A", confidence=70, title="first")
        f2 = _f(fid="B", confidence=70, title="second")
        merged, _dupes = dedup_by_location([f1, f2])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "first")

    def test_different_dimension_different_slot(self):
        f1 = _f(fid="A", dimension="security")
        f2 = _f(fid="B", dimension="bug")
        merged, dupes = dedup_by_location([f1, f2])
        self.assertEqual(len(merged), 2)
        self.assertEqual(dupes, 0)

    def test_cross_agent_same_location_deduped(self):
        # Two agents report the same (file, line, dimension) with different IDs
        f_agent1 = _f(fid="SEC-001", confidence=75)
        f_agent2 = _f(fid="BUG-042", confidence=60)  # same location, different ID
        merged, _dupes = dedup_by_location([f_agent1, f_agent2])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "SEC-001")

    def test_locationless_findings_pass_through(self):
        f1 = {"title": "no location A"}
        f2 = {"title": "no location B"}
        merged, dupes = dedup_by_location([f1, f2])
        self.assertEqual(len(merged), 2)
        self.assertEqual(dupes, 0)

    def test_empty_list(self):
        merged, dupes = dedup_by_location([])
        self.assertEqual(merged, [])
        self.assertEqual(dupes, 0)

    def test_custom_prefer_field(self):
        # Lower severity number = worse; prefer_field="severity" with lower_is_better
        f_critical = _f(fid="A", confidence=60, severity="critical")
        f_low = _f(fid="B", confidence=80, severity="low")
        # With default (confidence, higher wins), f_low wins
        merged, _ = dedup_by_location([f_critical, f_low])
        self.assertEqual(merged[0]["severity"], "low")


# ---------------------------------------------------------------------------
# FindingStore
# ---------------------------------------------------------------------------

class TestFindingStore(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._store_path = os.path.join(self._tmpdir, "store.json")

    def test_empty_store_returns_all_findings_as_new(self):
        store = FindingStore(self._store_path)
        findings = [_f(fid="A"), _f(fid="B", file="other.py")]
        new, suppressed = store.filter_new(findings)
        self.assertEqual(len(new), 2)
        self.assertEqual(suppressed, 0)

    def test_save_then_load_suppresses_prior_findings(self):
        store = FindingStore(self._store_path)
        findings = [_f(fid="A")]
        store.save(findings)

        store2 = FindingStore(self._store_path)
        new, suppressed = store2.filter_new(findings)
        self.assertEqual(len(new), 0)
        self.assertEqual(suppressed, 1)

    def test_new_finding_passes_after_save(self):
        store = FindingStore(self._store_path)
        store.save([_f(fid="A")])

        store2 = FindingStore(self._store_path)
        new_finding = _f(fid="B", file="new.py", line_start=99)
        new, suppressed = store2.filter_new([_f(fid="A"), new_finding])
        self.assertEqual(len(new), 1)
        self.assertEqual(suppressed, 1)
        self.assertEqual(new[0]["file"], "new.py")

    def test_missing_store_file_is_safe(self):
        store = FindingStore("/tmp/nonexistent-store-xyz.json")
        new, suppressed = store.filter_new([_f(fid="A")])
        self.assertEqual(len(new), 1)
        self.assertEqual(suppressed, 0)

    def test_corrupted_store_file_is_safe(self):
        with open(self._store_path, "w") as fh:
            fh.write("not valid json {{{{")
        store = FindingStore(self._store_path)
        new, suppressed = store.filter_new([_f(fid="A")])
        self.assertEqual(len(new), 1)
        self.assertEqual(suppressed, 0)

    def test_location_hash_is_deterministic(self):
        h1 = FindingStore._finding_hash(_f(fid="A"))
        h2 = FindingStore._finding_hash(_f(fid="B"))  # same location, different ID
        self.assertEqual(h1, h2)  # location-based hash ignores ID

    def test_different_locations_different_hash(self):
        h1 = FindingStore._finding_hash(_f(file="a.py"))
        h2 = FindingStore._finding_hash(_f(file="b.py"))
        self.assertNotEqual(h1, h2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCLI(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def _write_findings(self, findings: list[dict]) -> str:
        path = os.path.join(self._tmpdir, "findings.json")
        with open(path, "w") as fh:
            json.dump({"findings": findings}, fh)
        return path

    def test_cli_location_mode(self):
        from scripts.finding_dedup import main
        f1 = _f(fid="A")
        f2 = _f(fid="B")  # same location
        path = self._write_findings([f1, f2])
        out_path = os.path.join(self._tmpdir, "out.json")
        ret = main([path, "--mode", "location", "--output", out_path])
        self.assertEqual(ret, 0)
        with open(out_path) as fh:
            result = json.load(fh)
        self.assertEqual(result["dedup_stats"]["output_count"], 1)
        self.assertEqual(result["dedup_stats"]["duplicates_resolved"], 1)

    def test_cli_id_mode_reports_dropped_no_id(self):
        from scripts.finding_dedup import main
        f1 = _f(fid="A")
        f2 = {"title": "no id"}
        path = self._write_findings([f1, f2])
        out_path = os.path.join(self._tmpdir, "out.json")
        ret = main([path, "--mode", "id", "--output", out_path])
        self.assertEqual(ret, 0)
        with open(out_path) as fh:
            result = json.load(fh)
        self.assertEqual(result["dedup_stats"]["output_count"], 1)
        self.assertEqual(result["dedup_stats"]["dropped_no_id"], 1)

    def test_cli_id_mode(self):
        from scripts.finding_dedup import main
        f1 = _f(fid="A")
        f2 = _f(fid="A")  # same ID
        path = self._write_findings([f1, f2])
        out_path = os.path.join(self._tmpdir, "out.json")
        ret = main([path, "--mode", "id", "--output", out_path])
        self.assertEqual(ret, 0)
        with open(out_path) as fh:
            result = json.load(fh)
        self.assertEqual(result["dedup_stats"]["output_count"], 1)

    def test_cli_prior_session(self):
        from scripts.finding_dedup import main
        f1 = _f(fid="A")
        path = self._write_findings([f1])

        # Build a prior session store
        prior_path = os.path.join(self._tmpdir, "prior.json")
        store = FindingStore(prior_path)
        store.save([f1])

        out_path = os.path.join(self._tmpdir, "out.json")
        ret = main([path, "--prior-session", prior_path, "--output", out_path])
        self.assertEqual(ret, 0)
        with open(out_path) as fh:
            result = json.load(fh)
        self.assertEqual(result["dedup_stats"]["prior_session_suppressed"], 1)
        self.assertEqual(result["dedup_stats"]["output_count"], 0)

    def test_cli_accepts_bare_list(self):
        from scripts.finding_dedup import main
        path = os.path.join(self._tmpdir, "bare.json")
        with open(path, "w") as fh:
            json.dump([_f(fid="A")], fh)
        out_path = os.path.join(self._tmpdir, "out.json")
        ret = main([path, "--output", out_path])
        self.assertEqual(ret, 0)


if __name__ == "__main__":
    unittest.main()
