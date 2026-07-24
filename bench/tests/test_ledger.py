"""
Tests for bench/runner/ledger.py

Covers the append-only ledger (spec H8):
  - append_row validates the 14 required keys; missing any -> raises
  - append is NDJSON: one JSON object per physical line
  - append-only guarantee: a second append never rewrites earlier lines
    (line 1 is byte-identical before and after the second append)
  - every line round-trips through json.loads back to the original row
  - the file is opened in append mode only (never truncated/rewritten)
  - the auth_mode cost-honesty helpers: an absent field reads as "api", and
    auth_mode stays OUT of the required keys so historical rows stay valid
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# Add bench/ to path so we can import the runner package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from runner.ledger import (
    append_row,
    cost_is_billable,
    DEFAULT_AUTH_MODE,
    REQUIRED_KEYS,
    row_auth_mode,
    SUBSCRIPTION_AUTH_MODE,
)


REQUIRED = [
    "run_id", "ts", "git_sha", "tier", "tool",
    "golden_recall", "valid_extra_rate", "noise_rate", "precision_strict",
    "tokens_total", "cost_usd", "judge_pin", "scorer_sha", "envelope",
]


def valid_row(**overrides):
    row = {
        "run_id": "subset-20260717T120000Z-8ecd975",
        "ts": "2026-07-17T12:00:00Z",
        "git_sha": "8ecd975",
        "tier": "subset",
        "tool": "deep-review-v2",
        "golden_recall": 0.41,
        "valid_extra_rate": 0.12,
        "noise_rate": 0.08,
        "precision_strict": 0.34,
        "tokens_total": 123456,
        "cost_usd": 4.20,
        "judge_pin": "claude-opus-4-8-20260101",
        "scorer_sha": "deadbeef",
        "envelope": {"cap": 25, "fixtures": [], "invocation": "headless"},
    }
    row.update(overrides)
    return row


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger_path = os.path.join(self._tmp.name, "experiments.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    # -- required keys expose the spec contract ---------------------------

    def test_required_keys_match_spec(self):
        self.assertEqual(sorted(REQUIRED_KEYS), sorted(REQUIRED))
        self.assertEqual(len(REQUIRED_KEYS), 14)

    # -- happy path --------------------------------------------------------

    def test_append_creates_single_line(self):
        append_row(self.ledger_path, valid_row())
        with open(self.ledger_path, "rb") as fh:
            data = fh.read()
        lines = data.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(data[-1:], b"\n")  # newline-terminated NDJSON

    def test_round_trips_through_json_loads_per_line(self):
        rows = [valid_row(run_id="r1"), valid_row(run_id="r2", cost_usd=9.99)]
        for row in rows:
            append_row(self.ledger_path, row)
        with open(self.ledger_path) as fh:
            parsed = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(parsed, rows)

    # -- required-key validation, parametrized over all 14 ----------------

    def test_missing_any_required_key_raises(self):
        for key in REQUIRED:
            with self.subTest(missing=key):
                row = valid_row()
                del row[key]
                with self.assertRaises(ValueError):
                    append_row(self.ledger_path, row)

    def test_missing_key_does_not_write_file(self):
        row = valid_row()
        del row["noise_rate"]
        with self.assertRaises(ValueError):
            append_row(self.ledger_path, row)
        self.assertFalse(os.path.exists(self.ledger_path))

    def test_extra_keys_allowed(self):
        # The full spec schema has more keys; extras must not be rejected.
        append_row(self.ledger_path, valid_row(f1_strict=0.37, change="none"))
        with open(self.ledger_path) as fh:
            record = json.loads(fh.readline())
        self.assertEqual(record["f1_strict"], 0.37)

    # -- append-only guarantee --------------------------------------------

    def test_second_append_leaves_first_line_byte_identical(self):
        append_row(self.ledger_path, valid_row(run_id="first"))
        with open(self.ledger_path, "rb") as fh:
            after_first = fh.read()

        append_row(self.ledger_path, valid_row(run_id="second"))
        with open(self.ledger_path, "rb") as fh:
            after_second = fh.read()

        lines = after_second.splitlines(keepends=True)
        self.assertEqual(len(lines), 2)
        # Line 1 is byte-for-byte what it was after the first append.
        self.assertEqual(lines[0], after_first)
        self.assertTrue(after_second.startswith(after_first))

    def test_opens_file_in_append_mode_only(self):
        real_open = open
        modes = []

        def spy_open(path, mode="r", *args, **kwargs):
            if os.fspath(path) == self.ledger_path:
                modes.append(mode)
            return real_open(path, mode, *args, **kwargs)

        with patch("runner.ledger.open", spy_open, create=True):
            append_row(self.ledger_path, valid_row())
            append_row(self.ledger_path, valid_row())

        self.assertTrue(modes, "ledger open() was not observed")
        for mode in modes:
            self.assertIn("a", mode)
            self.assertNotIn("w", mode)


class AuthModeTestCase(unittest.TestCase):
    """The auth_mode cost-honesty contract every ledger consumer reads through.

    A subscription-served run's ``cost_usd`` is not billable API spend, so the
    two helpers below are the single place that decides which rows may enter a
    billable aggregate. They are pinned here because report.py imports them
    rather than re-deriving the rule.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger_path = os.path.join(self._tmp.name, "experiments.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    def test_mode_constants(self):
        self.assertEqual(DEFAULT_AUTH_MODE, "api")
        self.assertEqual(SUBSCRIPTION_AUTH_MODE, "subscription")

    def test_auth_mode_is_not_a_required_key(self):
        # The ledger is append-only: requiring auth_mode would invalidate every
        # row written before the field existed.
        self.assertNotIn("auth_mode", REQUIRED_KEYS)

    def test_absent_auth_mode_reads_as_api(self):
        # Rows predating the field were all API-keyed.
        self.assertEqual(row_auth_mode(valid_row()), "api")
        self.assertTrue(cost_is_billable(valid_row()))

    def test_null_auth_mode_reads_as_api(self):
        # A manifest that carried an explicit null must not read as a third mode.
        self.assertEqual(row_auth_mode(valid_row(auth_mode=None)), "api")
        self.assertTrue(cost_is_billable(valid_row(auth_mode=None)))

    def test_explicit_api_is_billable(self):
        row = valid_row(auth_mode="api")
        self.assertEqual(row_auth_mode(row), "api")
        self.assertTrue(cost_is_billable(row))

    def test_subscription_is_not_billable(self):
        row = valid_row(auth_mode="subscription")
        self.assertEqual(row_auth_mode(row), "subscription")
        self.assertFalse(cost_is_billable(row))

    def test_unknown_mode_stays_billable(self):
        # Only subscription is known to be non-billable; anything else is a
        # metered credential and must keep counting as spend.
        self.assertTrue(cost_is_billable(valid_row(auth_mode="bedrock")))

    def test_historical_row_without_auth_mode_still_appends(self):
        append_row(self.ledger_path, valid_row())
        with open(self.ledger_path) as fh:
            record = json.loads(fh.readline())
        self.assertNotIn("auth_mode", record)

    def test_subscription_row_appends_with_its_cost_verbatim(self):
        append_row(self.ledger_path, valid_row(auth_mode="subscription"))
        with open(self.ledger_path) as fh:
            record = json.loads(fh.readline())
        self.assertEqual(record["auth_mode"], "subscription")
        self.assertEqual(record["cost_usd"], 4.20)


if __name__ == "__main__":
    unittest.main()
