"""
Tests for bench/runner/checkpoint.py

Covers the PR-granular resume semantics (spec H3):
  - status() defaults to "pending" when no state file exists
  - mark() persists status + optional detail to a filesystem-safe per-PR file
  - mark() rejects statuses outside pending|ok|timeout|invalid|drifted|failed
  - pending()/failed() partition a mixed run per resume rules:
      plain resume skips ok/invalid/drifted AND timeout/failed;
      only "pending" URLs still need a run -> pending()
      timeout/failed are the --retry-failed set -> failed()
  - ordering preserved; filenames filesystem-safe; re-mark overwrites
"""

import json
import os
import re
import sys
import tempfile
import unittest

# Add bench/ to path so we can import the runner package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runner.checkpoint import Checkpoint


GOLDEN_URLS = [
    "https://github.com/keycloak/keycloak/pull/37634",
    "https://github.com/getsentry/sentry/pull/93824",
    "https://github.com/grafana/grafana/pull/79265",
    "https://github.com/discourse/discourse/pull/4",
    "https://github.com/calcom/cal.com/pull/14740",
    "https://github.com/ai-code-review-evaluation/sentry-greptile/pull/1",
]


class CheckpointTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = self._tmp.name
        self.cp = Checkpoint(self.run_dir)

    def tearDown(self):
        self._tmp.cleanup()

    # -- defaults ----------------------------------------------------------

    def test_status_defaults_to_pending(self):
        for url in GOLDEN_URLS:
            self.assertEqual(self.cp.status(url), "pending")

    # -- mark / status round-trip -----------------------------------------

    def test_mark_and_status_roundtrip(self):
        self.cp.mark(GOLDEN_URLS[0], "ok")
        self.assertEqual(self.cp.status(GOLDEN_URLS[0]), "ok")

    def test_all_valid_statuses_roundtrip(self):
        valid = ["pending", "ok", "timeout", "invalid", "drifted", "failed"]
        for status, url in zip(valid, GOLDEN_URLS):
            self.cp.mark(url, status)
            self.assertEqual(self.cp.status(url), status)

    def test_remark_overwrites(self):
        url = GOLDEN_URLS[0]
        self.cp.mark(url, "pending")
        self.cp.mark(url, "timeout")
        self.cp.mark(url, "ok")
        self.assertEqual(self.cp.status(url), "ok")

    # -- validation --------------------------------------------------------

    def test_mark_invalid_status_raises(self):
        with self.assertRaises(ValueError):
            self.cp.mark(GOLDEN_URLS[0], "bogus")
        # no file should have been written for a rejected status
        self.assertEqual(self.cp.status(GOLDEN_URLS[0]), "pending")

    # -- resume partitioning ----------------------------------------------

    def test_pending_and_failed_partition_mixed_run(self):
        # Assign one of each terminal + pending status across six URLs.
        assignment = {
            GOLDEN_URLS[0]: "ok",
            GOLDEN_URLS[1]: "invalid",
            GOLDEN_URLS[2]: "drifted",
            GOLDEN_URLS[3]: "timeout",
            GOLDEN_URLS[4]: "failed",
            GOLDEN_URLS[5]: "pending",
        }
        for url, status in assignment.items():
            self.cp.mark(url, status)

        # Plain resume: only the "pending" URL still needs a run.
        self.assertEqual(self.cp.pending(GOLDEN_URLS), [GOLDEN_URLS[5]])
        # --retry-failed re-runs exactly timeout + failed.
        self.assertEqual(
            self.cp.failed(GOLDEN_URLS), [GOLDEN_URLS[3], GOLDEN_URLS[4]]
        )

    def test_unmarked_urls_are_pending(self):
        # Never-marked URLs count as pending (default state == needs a run).
        self.cp.mark(GOLDEN_URLS[0], "ok")
        self.assertEqual(self.cp.pending(GOLDEN_URLS), GOLDEN_URLS[1:])

    def test_pending_preserves_input_order(self):
        self.cp.mark(GOLDEN_URLS[2], "ok")
        reordered = list(reversed(GOLDEN_URLS))
        result = self.cp.pending(reordered)
        expected = [u for u in reordered if u != GOLDEN_URLS[2]]
        self.assertEqual(result, expected)

    def test_failed_excludes_non_failed(self):
        self.cp.mark(GOLDEN_URLS[0], "ok")
        self.cp.mark(GOLDEN_URLS[1], "drifted")
        self.assertEqual(self.cp.failed(GOLDEN_URLS), [])

    # -- persistence details ----------------------------------------------

    def test_detail_persisted_to_state_file(self):
        self.cp.mark(GOLDEN_URLS[0], "drifted", detail="head SHA mismatch")
        state_files = [
            f for f in os.listdir(os.path.join(self.run_dir, "state"))
            if f.endswith(".json")
        ]
        self.assertEqual(len(state_files), 1)
        with open(os.path.join(self.run_dir, "state", state_files[0])) as fh:
            record = json.load(fh)
        self.assertEqual(record["status"], "drifted")
        self.assertEqual(record["detail"], "head SHA mismatch")

    def test_state_filename_is_filesystem_safe(self):
        self.cp.mark(GOLDEN_URLS[0], "ok")
        state_files = os.listdir(os.path.join(self.run_dir, "state"))
        self.assertEqual(len(state_files), 1)
        # No path separators or URL-hostile characters in the filename.
        self.assertRegex(state_files[0], r"^[A-Za-z0-9._-]+$")
        self.assertNotIn("/", state_files[0])
        self.assertNotIn(":", state_files[0])

    def test_filename_is_deterministic(self):
        self.cp.mark(GOLDEN_URLS[0], "ok")
        first = set(os.listdir(os.path.join(self.run_dir, "state")))
        self.cp.mark(GOLDEN_URLS[0], "failed")  # same URL -> same file
        second = set(os.listdir(os.path.join(self.run_dir, "state")))
        self.assertEqual(first, second)

    def test_distinct_urls_get_distinct_files(self):
        self.cp.mark(GOLDEN_URLS[0], "ok")
        self.cp.mark(GOLDEN_URLS[1], "ok")
        state_files = os.listdir(os.path.join(self.run_dir, "state"))
        self.assertEqual(len(state_files), 2)

    def test_status_survives_new_instance(self):
        # Resume across process restarts: a fresh Checkpoint reads prior state.
        self.cp.mark(GOLDEN_URLS[0], "ok")
        reopened = Checkpoint(self.run_dir)
        self.assertEqual(reopened.status(GOLDEN_URLS[0]), "ok")


if __name__ == "__main__":
    unittest.main()
