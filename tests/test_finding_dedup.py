#!/usr/bin/env python3
"""Tests for scripts/finding_dedup.py"""

import os
import sys
import unittest

# Allow import from scripts/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.finding_dedup import dedup_by_id


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
        merged, dupes, dropped = dedup_by_id(
            {"ndjson-agent": [f_ndjson]}, {"text-agent": [f_text]}
        )
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

    def test_equal_priority_keeps_first_seen_content(self):
        """Same id + same priority: first-seen content survives; dupe is counted."""
        first = _f(fid="X", title="first-seen")
        second = _f(fid="X", title="should-not-win")
        # Both in the ndjson channel → both priority 2 (equal). Dict insertion
        # order means "first" is processed before "second".
        merged, dupes, dropped = dedup_by_id({"a": [first], "b": [second]}, {})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "first-seen")
        self.assertEqual(dupes, 1)
        self.assertEqual(dropped, 0)


if __name__ == "__main__":
    unittest.main()
