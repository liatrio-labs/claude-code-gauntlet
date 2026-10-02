"""Tests for bench/adjudicator/adjudicate.py.

No network, no keys: the HTTP transport is injected. Covers the two pure
context builders (``slice_hunk`` boundary/nearest/missing-path behavior and
``file_context`` clamping) and the ``adjudicate`` parse/retry contract.
"""

import json
import re
import sys
import unittest
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench.adjudicator.adjudicate import (  # noqa: E402
    FROZEN_PROMPT,
    _parse_verdict,
    _recover_verdict,
    adjudicate,
    file_context,
    slice_hunk,
)

# The exact reply that sank --score-only smoke-20260721-172943-0b24d95: the adjudicator's
# "reason" quotes the Ruby snippet `"." << website_host` with an UNescaped inner double-quote
# (the `"` after the first `.`), which json.loads reads as the closing quote -> the next char
# `<` trips "Expecting ',' delimiter: line 1 column 113 (char 112)". Captured verbatim.
FAILING_REPLY = (
    '{"bucket":"valid_extra","failed_check":null,"reason":"The comment correctly '
    'identifies that line 147 uses `\\"." << website_host\\"` which is the mutating '
    "String#<< operator on a string literal, and the concern about "
    'readability/unintentional mutation appearance is concrete and actionable."}'
)

DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -10,3 +10,4 @@ def foo():\n"
    " a\n"
    " b\n"
    "+c\n"
    " d\n"
    "@@ -40,2 +41,3 @@ def bar():\n"
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


HUNK_ONE = "@@ -10,3 +10,4 @@ def foo():\n a\n b\n+c\n d\n"
HUNK_TWO = "@@ -40,2 +41,3 @@ def bar():\n e\n+f\n g\n"
OTHER_HUNK = "@@ -1,2 +1,3 @@\n x\n+y\n z\n"
START_BOUNDARY_DIFF = (
    "+++ b/boundary.py\n@@ -10 +10 @@\n-first\n+first\n@@ -9,2 +9,2 @@\n before\n at\n"
)
END_BOUNDARY_DIFF = (
    "+++ b/boundary.py\n@@ -10,2 +10,2 @@\n before\n last\n"
    "@@ -11 +11 @@\n-second\n+second\n"
)


@pytest.mark.parametrize(
    "diff_text, path, line, expected",
    [
        pytest.param(
            START_BOUNDARY_DIFF,
            "boundary.py",
            10,
            "@@ -10 +10 @@\n-first\n+first\n",
            id="start-boundary",
        ),
        pytest.param(
            END_BOUNDARY_DIFF,
            "boundary.py",
            11,
            "@@ -10,2 +10,2 @@\n before\n last\n",
            id="end-boundary",
        ),
        pytest.param(DIFF, "src/app.py", 42, HUNK_TWO, id="second-hunk"),
        pytest.param(DIFF, "src/app.py", 30, HUNK_TWO, id="between-hunks-nearest"),
        pytest.param(
            DIFF,
            "src/app.py",
            25,
            HUNK_ONE,
            id="between-hunks-strict-nearer-first",
        ),
        pytest.param(DIFF, "src/app.py", 27, HUNK_ONE, id="nearest-tie-first"),
        pytest.param(
            "+++ b/f\n@@ -10 +10 @@\n-a\n+a\n@@ -9,3 +9,3 @@\n x\n y\n z\n",
            "f",
            10,
            "@@ -10 +10 @@\n-a\n+a\n",
            id="first-covering-wins-over-deeper-cover",
        ),
        pytest.param(
            "+++ b/zero.py\n@@ -3 +3 @@\n-later\n+later\n@@ -2,0 +2,0 @@\n",
            "zero.py",
            2,
            "@@ -2,0 +2,0 @@\n",
            id="zero-count-span-is-one",
        ),
        pytest.param(
            "+++ b/f\n@@ -5,2 +4,0 @@\n-a\n-b\n@@ -12 +10 @@\n-x\n+y\n",
            "f",
            7,
            "@@ -5,2 +4,0 @@\n-a\n-b\n",
            id="zero-count-nearest-span-is-one",
        ),
        pytest.param(DIFF, "other.py", 2, OTHER_HUNK, id="second-file-isolated"),
        pytest.param(
            "+++ src/b/file.py\n@@ -1 +1 @@\n-old\n+new\n",
            "src/b/file.py",
            1,
            "@@ -1 +1 @@\n-old\n+new\n",
            id="non-leading-b-prefix-preserved",
        ),
        pytest.param(
            '--- "a/f "\n+++ "b/f "\n@@ -1 +1 @@\n-old\n+new\n',
            "f",
            1,
            "@@ -1 +1 @@\n-old\n+new\n",
            id="quoted-trailing-space-matches-stripped-candidate",
        ),
        pytest.param(
            '--- "a/caf\\303\\251.py"\n'
            '+++ "b/caf\\303\\251.py"\n'
            "@@ -1 +1 @@\n-old\n+new\n",
            "café.py",
            1,
            "@@ -1 +1 @@\n-old\n+new\n",
            id="quoted-path-decodes-to-candidate",
        ),
        pytest.param(
            '--- "a/caf\\303\\251.py"\n'
            '+++ "b/caf\\303\\251.py"\n'
            "@@ -1 +1 @@\n-old\n+new\n",
            '"b/caf\\303\\251.py"',
            1,
            None,
            id="quoted-path-literal-spelling-does-not-match",
        ),
        pytest.param(
            "--- a/crlf.py\r\n+++ b/crlf.py\r\n@@ -1 +1 @@\n-old\r\n+new\r\n",
            "crlf.py",
            1,
            "@@ -1 +1 @@\n-old\r\n+new\r\n",
            id="crlf-header",
        ),
        pytest.param(
            "+++  b/double.py\n@@ -0,0 +1 @@\n+new\n",
            "double.py",
            1,
            "@@ -0,0 +1 @@\n+new\n",
            id="double-space-after-header-marker",
        ),
        pytest.param(
            "+++ b/trailing.py \t\n@@ -0,0 +1 @@\n+new\n",
            "trailing.py",
            1,
            "@@ -0,0 +1 @@\n+new\n",
            id="trailing-space-before-tab",
        ),
        pytest.param(
            "--- /dev/null\n+++ /dev/null\n@@ -0,0 +0,0 @@\n",
            "/dev/null",
            1,
            None,
            id="dev-null-is-not-a-new-path",
        ),
        pytest.param(DIFF, "nope/missing.py", 5, None, id="missing-path"),
        pytest.param(
            "@@ -0,0 +1 @@\n+new\n",
            "",
            1,
            None,
            id="unkeyed-hunk-does-not-match-empty-path",
        ),
    ],
)
def test_slice_hunk(diff_text: str, path: str, line: int, expected: str | None) -> None:
    if expected is None:
        with pytest.raises(ValueError, match=re.escape(repr(path))):
            slice_hunk(diff_text, path, line)
    else:
        assert slice_hunk(diff_text, path, line) == expected


class FileContextTests(unittest.TestCase):
    def setUp(self):
        self.lines = [f"line{i}" for i in range(1, 21)]  # 20 lines

    def test_window_marks_target_line(self):
        out = file_context(self.lines, 10, radius=2)
        rows = out.splitlines()
        self.assertEqual(
            rows,
            [
                "  8: line8",
                "  9: line9",
                "> 10: line10",
                "  11: line11",
                "  12: line12",
            ],
        )

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
        transport = FakeTransport(
            ['{"bucket":"noise","failed_check":3,"reason":"vague"}']
        )
        verdict = adjudicate("c", "hunk", "ctx", "pin-x", "key-x", transport=transport)
        self.assertEqual(
            verdict, {"bucket": "noise", "failed_check": 3, "reason": "vague"}
        )
        self.assertEqual(len(transport.calls), 1)

    def test_retry_on_garbage_then_valid(self):
        transport = FakeTransport(
            [
                "not json at all",
                '{"bucket":"valid_extra","failed_check":null,"reason":"grounded"}',
            ]
        )
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
        transport = FakeTransport(
            [
                "[]",
                '{"bucket":"noise","failed_check":2,"reason":"ungrounded"}',
            ]
        )
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
        transport = FakeTransport(
            [
                '{"bucket":"maybe","reason":"x"}',
                '{"bucket":"noise","failed_check":1,"reason":"ok"}',
            ]
        )
        verdict = adjudicate("c", "h", "x", "pin-x", "key-x", transport=transport)
        self.assertEqual(verdict["bucket"], "noise")
        self.assertEqual(len(transport.calls), 2)

    def test_strips_markdown_code_fences(self):
        transport = FakeTransport(
            [
                '```json\n{"bucket":"valid_extra","failed_check":null,"reason":"ok"}\n```',
            ]
        )
        verdict = adjudicate("c", "h", "x", "pin-x", "key-x", transport=transport)
        self.assertEqual(verdict["bucket"], "valid_extra")

    def test_request_shape_pins_model_temp0_and_bearer_auth(self):
        transport = FakeTransport(['{"bucket":"noise","failed_check":4,"reason":"r"}'])
        adjudicate(
            "the comment",
            "the hunk",
            "the ctx",
            "opus-pin",
            "secret",
            transport=transport,
        )
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


class VerdictRecoveryTests(unittest.TestCase):
    """Recovery of a malformed reply (unescaped inner quotes in "reason") — the
    owner-approved (2026-07-21) fix for the 172943 deterministic parse failure."""

    def test_captured_failing_reply_is_genuinely_malformed(self):
        # Guard the fixture: it must actually break strict JSON at the observed spot,
        # else the recovery path below would never be exercised.
        with self.assertRaises(json.JSONDecodeError) as ctx:
            json.loads(FAILING_REPLY)
        self.assertIn("delimiter", str(ctx.exception))

    def test_recovery_extracts_bucket_from_failing_reply(self):
        verdict = _parse_verdict(FAILING_REPLY)
        self.assertEqual(verdict["bucket"], "valid_extra")
        self.assertIsNone(verdict["failed_check"])
        self.assertIn("mutating String#<< operator", verdict["reason"])

    def test_adjudicate_recovers_on_first_reply_no_retry(self):
        # Recovery succeeds on the first attempt, so no retry is spent (unlike the
        # old behavior, which retried once then raised on the second identical reply).
        transport = FakeTransport([FAILING_REPLY])
        verdict = adjudicate("c", "h", "x", "pin-x", "key-x", transport=transport)
        self.assertEqual(verdict["bucket"], "valid_extra")
        self.assertEqual(len(transport.calls), 1)

    def test_recover_verdict_reads_string_typed_failed_check(self):
        text = '{"bucket":"noise","failed_check":"3","reason":"has "quotes" inside"}'
        verdict = _recover_verdict(text)
        self.assertEqual(verdict["bucket"], "noise")
        self.assertEqual(verdict["failed_check"], 3)

    def test_unrecoverable_reply_without_bucket_still_raises(self):
        # No valid bucket anywhere -> recovery raises -> the retry-then-raise contract
        # is preserved (two attempts, then ValueError), not silently bucketed.
        transport = FakeTransport(['{"reason":"broken', '{"reason":"still broken'])
        with self.assertRaises(ValueError) as ctx:
            adjudicate("c", "h", "x", "pin-x", "key-x", transport=transport)
        self.assertIn("unparseable JSON twice", str(ctx.exception))
        self.assertEqual(len(transport.calls), 2)

    def test_wellformed_replies_use_strict_path_unchanged(self):
        # The strict json.loads path must be byte-identical for well-formed replies,
        # including one with PROPERLY escaped inner quotes (valid JSON, no recovery).
        cases = [
            (
                '{"bucket":"noise","failed_check":3,"reason":"vague"}',
                {"bucket": "noise", "failed_check": 3, "reason": "vague"},
            ),
            (
                '{"bucket":"valid_extra","failed_check":null,"reason":"ok"}',
                {"bucket": "valid_extra", "failed_check": None, "reason": "ok"},
            ),
            (
                r'{"bucket":"noise","failed_check":1,"reason":"uses \"foo\" here"}',
                {"bucket": "noise", "failed_check": 1, "reason": 'uses "foo" here'},
            ),
        ]
        for reply, expected in cases:
            self.assertEqual(_parse_verdict(reply), expected)


if __name__ == "__main__":
    unittest.main()
