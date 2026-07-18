"""Tests for bench/adjudicator/adjudicate.py.

No network, no keys: the HTTP transport is injected. Covers the two pure
context builders (``slice_hunk`` boundary/nearest/missing-path behavior and
``file_context`` clamping) and the ``adjudicate`` parse/retry contract.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench.adjudicator.adjudicate import (  # noqa: E402
    FROZEN_PROMPT,
    adjudicate,
    file_context,
    slice_hunk,
)

# Two-hunk unified diff for one file. New-file spans: hunk 1 = lines 10..14,
# hunk 2 = lines 41..44.
DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -10,4 +10,5 @@ def foo():\n"
    " a\n"
    " b\n"
    "+c\n"
    " d\n"
    "@@ -40,3 +41,4 @@ def bar():\n"
    " e\n"
    "+f\n"
    " g\n"
    "diff --git a/other.py b/other.py\n"
    "index 3333333..4444444 100644\n"
    "--- a/other.py\n"
    "+++ b/other.py\n"
    "@@ -1,2 +1,3 @@\n"
    " x\n"
    "+y\n"
    " z\n"
)


class FakeTransport:
    """Records POST calls and returns queued chat-completions replies."""

    def __init__(self, contents):
        # ``contents`` is a list of assistant message strings to hand back in order.
        self._contents = list(contents)
        self.calls = []

    def __call__(self, url, headers, payload):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        content = self._contents.pop(0)
        return {"choices": [{"message": {"content": content}}]}


class SliceHunkTests(unittest.TestCase):
    def test_line_at_hunk_start_boundary(self):
        hunk = slice_hunk(DIFF, "src/app.py", 10)
        self.assertIn("@@ -10,4 +10,5 @@", hunk)
        self.assertNotIn("+41,4", hunk)

    def test_line_at_hunk_end_boundary(self):
        # Line 14 is the last line of hunk 1's new-file span (10 + 5 - 1).
        hunk = slice_hunk(DIFF, "src/app.py", 14)
        self.assertIn("@@ -10,4 +10,5 @@", hunk)

    def test_line_in_second_hunk(self):
        hunk = slice_hunk(DIFF, "src/app.py", 41)
        self.assertIn("@@ -40,3 +41,4 @@", hunk)
        self.assertIn("+f", hunk)

    def test_line_between_hunks_returns_nearest(self):
        # Line 25: distance to hunk-1 end (14) = 11, to hunk-2 start (41) = 16.
        hunk = slice_hunk(DIFF, "src/app.py", 25)
        self.assertIn("@@ -10,4 +10,5 @@", hunk)

    def test_second_file_isolated_from_first(self):
        hunk = slice_hunk(DIFF, "other.py", 2)
        self.assertIn("@@ -1,2 +1,3 @@", hunk)
        self.assertIn("+y", hunk)
        self.assertNotIn("def foo", hunk)

    def test_missing_path_raises_informative_error(self):
        with self.assertRaises(ValueError) as ctx:
            slice_hunk(DIFF, "nope/missing.py", 5)
        self.assertIn("missing.py", str(ctx.exception))

    def test_hunk_text_is_only_that_hunk(self):
        hunk = slice_hunk(DIFF, "src/app.py", 12)
        # Body of hunk 1 only; must not bleed into hunk 2.
        self.assertIn("+c", hunk)
        self.assertNotIn("+f", hunk)


class FileContextTests(unittest.TestCase):
    def setUp(self):
        self.lines = ["line{}".format(i) for i in range(1, 21)]  # 20 lines

    def test_window_marks_target_line(self):
        out = file_context(self.lines, 10, radius=2)
        rows = out.splitlines()
        self.assertEqual(rows, [
            "  8: line8",
            "  9: line9",
            "> 10: line10",
            "  11: line11",
            "  12: line12",
        ])

    def test_clamps_at_file_start(self):
        out = file_context(self.lines, 2, radius=5)
        rows = out.splitlines()
        self.assertTrue(rows[0].startswith("  1:"))
        self.assertIn("> 2: line2", out)

    def test_clamps_at_file_end(self):
        out = file_context(self.lines, 20, radius=3)
        rows = out.splitlines()
        self.assertEqual(rows[-1], "> 20: line20")
        self.assertTrue(rows[0].startswith("  17:"))

    def test_accepts_string_input(self):
        out = file_context("a\nb\nc", 2, radius=5)
        self.assertEqual(out, "  1: a\n> 2: b\n  3: c")

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(file_context([], 5), "")
        self.assertEqual(file_context("", 5), "")

    def test_none_line_returns_empty_string(self):
        self.assertEqual(file_context(self.lines, None), "")


class AdjudicateTests(unittest.TestCase):
    def test_valid_first_reply_no_retry(self):
        transport = FakeTransport(['{"bucket":"noise","failed_check":3,"reason":"vague"}'])
        verdict = adjudicate("c", "hunk", "ctx", "pin-x", "key-x", transport=transport)
        self.assertEqual(verdict, {"bucket": "noise", "failed_check": 3, "reason": "vague"})
        self.assertEqual(len(transport.calls), 1)

    def test_retry_on_garbage_then_valid(self):
        transport = FakeTransport([
            "not json at all",
            '{"bucket":"valid_extra","failed_check":null,"reason":"grounded"}',
        ])
        verdict = adjudicate("c", "h", "x", "pin-x", "key-x", transport=transport)
        self.assertEqual(verdict["bucket"], "valid_extra")
        self.assertIsNone(verdict["failed_check"])
        self.assertEqual(len(transport.calls), 2)

    def test_garbage_twice_raises(self):
        transport = FakeTransport(["garbage one", "garbage two"])
        with self.assertRaises(ValueError):
            adjudicate("c", "h", "x", "pin-x", "key-x", transport=transport)
        self.assertEqual(len(transport.calls), 2)

    def test_parseable_non_object_retries_then_valid(self):
        # A JSON array/null/scalar parses but is not a verdict object; it must take
        # the retry path, not crash with AttributeError.
        transport = FakeTransport([
            "[]",
            '{"bucket":"noise","failed_check":2,"reason":"ungrounded"}',
        ])
        verdict = adjudicate("c", "h", "x", "pin-x", "key-x", transport=transport)
        self.assertEqual(verdict["bucket"], "noise")
        self.assertEqual(len(transport.calls), 2)

    def test_parseable_non_object_twice_raises_cleanly(self):
        transport = FakeTransport(["null", "[1, 2]"])
        with self.assertRaises(ValueError) as ctx:
            adjudicate("c", "h", "x", "pin-x", "key-x", transport=transport)
        self.assertIn("unparseable JSON twice", str(ctx.exception))
        self.assertEqual(len(transport.calls), 2)

    def test_invalid_bucket_value_triggers_retry(self):
        transport = FakeTransport([
            '{"bucket":"maybe","reason":"x"}',
            '{"bucket":"noise","failed_check":1,"reason":"ok"}',
        ])
        verdict = adjudicate("c", "h", "x", "pin-x", "key-x", transport=transport)
        self.assertEqual(verdict["bucket"], "noise")
        self.assertEqual(len(transport.calls), 2)

    def test_strips_markdown_code_fences(self):
        transport = FakeTransport([
            '```json\n{"bucket":"valid_extra","failed_check":null,"reason":"ok"}\n```',
        ])
        verdict = adjudicate("c", "h", "x", "pin-x", "key-x", transport=transport)
        self.assertEqual(verdict["bucket"], "valid_extra")

    def test_request_shape_pins_model_temp0_and_bearer_auth(self):
        transport = FakeTransport(['{"bucket":"noise","failed_check":4,"reason":"r"}'])
        adjudicate("the comment", "the hunk", "the ctx", "opus-pin", "secret", transport=transport)
        call = transport.calls[0]
        self.assertEqual(call["payload"]["model"], "opus-pin")
        self.assertEqual(call["payload"]["temperature"], 0)
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret")
        messages = call["payload"]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], FROZEN_PROMPT)
        self.assertIn("the comment", messages[1]["content"])
        self.assertIn("the hunk", messages[1]["content"])
        self.assertIn("the ctx", messages[1]["content"])

    def test_frozen_prompt_matches_committed_file(self):
        prompt_path = REPO_ROOT / "bench" / "adjudicator" / "prompt.txt"
        self.assertEqual(FROZEN_PROMPT, prompt_path.read_text(encoding="utf-8"))
        self.assertIn("strict review-comment auditor", FROZEN_PROMPT)


if __name__ == "__main__":
    unittest.main()
