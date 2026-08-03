"""
Tests for scripts/await_workflow.py.

The script is the Phase 3 wait protocol. It blocks inside one Bash tool call until
the backgrounded Workflow task's output file holds a terminal `{ ok, ... }` object,
then prints that object and exits 0; short of that it prints a machine-readable
marker and exits 3 (attempts remain), 4 (exhausted) or 5 (the persisted artifacts
landed but the return was never observed).

Two properties carry most of the weight and are tested hardest:

* A false terminal is worse than a slow one. Several objects that live near the
  return also carry an `ok` key — the assemble receipt most of all — and accepting
  one would send Phase 8 off with the wrong object. TestFalseTerminalRejection is
  the guard.
* A parse failure is never an error. For most of the wait the target is absent or
  zero bytes, so a detector that raised or exited on unparseable input would turn
  the ordinary case into a lost review. TestPartialAndUnreadable is the guard.

Fixtures are distilled from real on-disk task output files rather than invented;
each says which one.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.await_workflow import (
    ARTIFACT_BASENAMES,
    COMPACT_RETURN_KEYS,
    DEFAULT_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    SCAN_MAX_CHARS,
    _newest,
    artifacts_state,
    build_next_command,
    build_parser,
    default_timeout_seconds,
    emit,
    find_terminal,
    is_terminal_return,
    main,
    resolve_target,
    terminal_from,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The success return, exactly as observed at `.result` in
# .../tasks/w3eeyrqqm.output (a real headless smoke run, 2026-07-28), with the
# stats/artifactPaths sub-objects trimmed to a representative key or two.
SUCCESS_RETURN = {
    "ok": True,
    "phaseReached": "report",
    "stats": {"discovered": 9, "merged": 9, "verified": True},
    "artifactPaths": {
        "findings": "/out/code-gauntlet-findings-da09bc08.json",
        "report": "/out/code-gauntlet-report-da09bc08.md",
    },
    "resolvedPolicy": {"subagentModel": None},
    "checkpoints": {"completed": ["summarize", "report"]},
    "gaps": [],
}

# The early-failure return, as observed in two 615-byte task files. It omits
# `resolvedPolicy` and `checkpoints` entirely — nothing had produced them yet —
# which is why neither may be required for terminal detection.
FAILURE_RETURN = {
    "ok": False,
    "phaseReached": "args",
    "failingPhase": "args",
    "error": "invalid args: reviewConfig must be an object",
    "stats": {},
    "artifactPaths": {},
    "gaps": ["invalid args: reviewConfig must be an object"],
}

# The assemble receipt, from a journal.jsonl `result` record. It carries `ok` and
# must NOT be mistaken for the return.
ASSEMBLE_RECEIPT = {
    "ok": True,
    "planVersion": 2,
    "planChecksum": "fnv1a32:0xa0480bf7",
    "verified": [{"path": "/out/x.json", "content_proof": "match"}],
    "written": [{"path": "/out/y.json", "chars": 18599}],
    "errors": [],
}

# The verify executor's receipt, from a real b-prefixed task file (46091 bytes).
# It has no `ok` at all — `status` is its success field.
VERIFY_RECEIPT = {
    "status": "ok",
    "receipt": {"sha": "267d8be1", "n_in": 16, "nonce": "fb2b1ded5014dde8.0"},
    "result": {"verified": [{"id": "bug-1"}]},
}


def envelope(result):
    """Wrap *result* in the Workflow tool's real output-file envelope.

    Key set and nesting copied from .../tasks/w3eeyrqqm.output: the tool writes
    {summary, agentCount, logs, result, workflowProgress, totalTokens,
    totalToolCalls} with the script's return value nested at `result`.
    """
    return {
        "summary": "code-gauntlet v3 pipeline: phases 3-8 orchestration",
        "agentCount": 19,
        "logs": [],
        "result": result,
        "workflowProgress": [
            {
                "type": "workflow_agent",
                "index": 1,
                "label": "summarize",
                "state": "done",
                "lastToolSummary": "ok",
            },
        ],
        "totalTokens": 669337,
        "totalToolCalls": 118,
    }


class _Workspace:
    """A throwaway directory, cleaned up on exit."""

    def __enter__(self):
        self._dir = tempfile.mkdtemp(prefix="await-workflow-")
        return self

    def __exit__(self, exc_type, exc, tb):
        for root, dirs, files in os.walk(self._dir, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self._dir)

    @property
    def path(self):
        return self._dir

    def write(self, name, content, mtime=None):
        """Write *content* (str or bytes) to *name*; optionally force its mtime."""
        path = os.path.join(self._dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if isinstance(content, bytes):
            with open(path, "wb") as fh:
                fh.write(content)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


def run_main(argv, environ=None):
    """Call main() with captured streams. Returns ``(code, stdout, stderr)``."""
    out, err = io.StringIO(), io.StringIO()
    with patch("sys.stdout", new=out), patch("sys.stderr", new=err):
        code = main(argv, environ if environ is not None else {})
    return code, out.getvalue(), err.getvalue()


def sole_json_line(stdout):
    """Assert stdout is exactly one line of JSON and return the parsed object."""
    lines = [line for line in stdout.split("\n") if line != ""]
    assert len(lines) == 1, f"expected exactly one stdout line, got {len(lines)}"
    return json.loads(lines[0])


# ---------------------------------------------------------------------------
# Terminal detection
# ---------------------------------------------------------------------------


class TestIsTerminalReturn(unittest.TestCase):
    """A boolean `ok` plus at least one field only the compact return carries."""

    def test_success_shape_is_terminal(self):
        self.assertTrue(is_terminal_return(SUCCESS_RETURN))

    def test_early_failure_shape_is_terminal(self):
        """ok:false is still terminal — the run is over, it just failed."""
        self.assertTrue(is_terminal_return(FAILURE_RETURN))

    def test_bare_ok_is_not_terminal(self):
        self.assertFalse(is_terminal_return({"ok": True}))

    def test_non_boolean_ok_is_not_terminal(self):
        self.assertFalse(is_terminal_return({"ok": "yes", "phaseReached": "report"}))
        self.assertFalse(is_terminal_return({"ok": 1, "phaseReached": "report"}))

    def test_non_dict_is_not_terminal(self):
        for value in (None, [], "ok", 7, True):
            self.assertFalse(is_terminal_return(value))

    def test_every_corroborator_alone_suffices(self):
        """Any one of them is enough — the failure shape omits several."""
        for key in COMPACT_RETURN_KEYS:
            self.assertTrue(is_terminal_return({"ok": True, key: None}), key)

    def test_error_is_not_a_corroborator(self):
        """`error` is too generic; on its own it must not qualify an object."""
        self.assertNotIn("error", COMPACT_RETURN_KEYS)
        self.assertFalse(is_terminal_return({"ok": False, "error": "boom"}))


class TestFalseTerminalRejection(unittest.TestCase):
    """Objects that live near the return and also carry `ok` must be rejected."""

    def test_assemble_receipt_rejected(self):
        self.assertFalse(is_terminal_return(ASSEMBLE_RECEIPT))
        found, _, _ = find_terminal(json.dumps(ASSEMBLE_RECEIPT))
        self.assertIsNone(found)

    def test_verify_receipt_rejected(self):
        """{status, receipt, result} — its `result` is not a compact return."""
        found, _, _ = find_terminal(json.dumps(VERIFY_RECEIPT))
        self.assertIsNone(found)

    def test_envelope_with_list_result_rejected(self):
        """.result is a list in 7 of 66 real workflow files — not our pipeline."""
        found, _, _ = find_terminal(json.dumps(envelope([1, 2, 3])))
        self.assertIsNone(found)

    def test_envelope_with_plain_string_result_rejected(self):
        """.result is a plain (non-JSON) string in 4 of 66 real files."""
        found, _, _ = find_terminal(json.dumps(envelope("a prose summary")))
        self.assertIsNone(found)

    def test_receipt_nested_in_progress_is_not_promoted(self):
        """A receipt buried in workflowProgress must not become the terminal."""
        env = envelope("not a return")
        env["workflowProgress"] = [{"state": "done", "result": ASSEMBLE_RECEIPT}]
        found, _, _ = find_terminal(json.dumps(env))
        self.assertIsNone(found)


class TestTerminalFrom(unittest.TestCase):
    """The three accepted shapes, and nothing deeper."""

    def test_result_as_object(self):
        found, _ = terminal_from(envelope(SUCCESS_RETURN))
        self.assertEqual(found, SUCCESS_RETURN)

    def test_result_as_json_string(self):
        found, _ = terminal_from(envelope(json.dumps(SUCCESS_RETURN)))
        self.assertEqual(found, SUCCESS_RETURN)

    def test_bare_return(self):
        found, _ = terminal_from(SUCCESS_RETURN)
        self.assertEqual(found, SUCCESS_RETURN)

    def test_result_wins_over_self(self):
        """When both could qualify, the nested return is the authoritative one."""
        outer = dict(SUCCESS_RETURN)
        outer["result"] = FAILURE_RETURN
        found, _ = terminal_from(outer)
        self.assertEqual(found, FAILURE_RETURN)


class TestFindTerminalWholeFile(unittest.TestCase):
    """The ordinary case: the whole file parses and the return is at .result."""

    def test_real_envelope_success(self):
        found, bare, skipped = find_terminal(json.dumps(envelope(SUCCESS_RETURN)))
        self.assertEqual(found, SUCCESS_RETURN)
        self.assertFalse(bare)
        self.assertFalse(skipped)

    def test_real_envelope_failure(self):
        found, _, _ = find_terminal(json.dumps(envelope(FAILURE_RETURN)))
        self.assertEqual(found, FAILURE_RETURN)

    def test_pretty_printed_envelope(self):
        """The tool writes indent=2; the detector must not depend on layout."""
        found, _, _ = find_terminal(json.dumps(envelope(SUCCESS_RETURN), indent=2))
        self.assertEqual(found, SUCCESS_RETURN)


class TestPartialAndUnreadable(unittest.TestCase):
    """Every unparseable input is "not yet terminal" — never an exception."""

    def test_empty_string(self):
        self.assertEqual(find_terminal(""), (None, False, None))

    def test_whitespace_only(self):
        self.assertEqual(find_terminal("   \n\t \n"), (None, False, None))

    def test_truncated_mid_object(self):
        text = json.dumps(envelope(SUCCESS_RETURN))[
            : len(json.dumps(envelope(SUCCESS_RETURN))) // 2
        ]
        found, _, _ = find_terminal(text)
        self.assertIsNone(found)

    def test_truncated_by_one_closing_brace(self):
        """A real b-prefixed sibling file was short exactly one trailing '}'."""
        text = json.dumps(envelope(SUCCESS_RETURN))[:-1]
        found, _, _ = find_terminal(text)
        # The inner .result object is still complete and decodable on its own, so
        # the embedded scan legitimately recovers it. What must never happen is a
        # raise or a wrong object.
        self.assertIn(found, (None, SUCCESS_RETURN))

    def test_not_json_at_all(self):
        found, _, _ = find_terminal("Background tasks still running after 600s\n")
        self.assertIsNone(found)

    def test_unbalanced_braces_do_not_hang_or_raise(self):
        found, _, _ = find_terminal("{" * 5000)
        self.assertIsNone(found)

    def test_deeply_nested_does_not_raise(self):
        found, _, _ = find_terminal("[" * 2000 + "]" * 2000)
        self.assertIsNone(found)


class TestFindTerminalEmbedded(unittest.TestCase):
    """Issue #26 R2: the object may be embedded in, or appended after, other output."""

    def test_prefixed_by_log_lines(self):
        text = "starting run\nagent-done: result\n" + json.dumps(
            envelope(SUCCESS_RETURN)
        )
        found, _, _ = find_terminal(text)
        self.assertEqual(found, SUCCESS_RETURN)

    def test_followed_by_trailing_text(self):
        text = json.dumps(envelope(SUCCESS_RETURN)) + "\nWATCHER: artifacts present\n"
        found, _, _ = find_terminal(text)
        self.assertEqual(found, SUCCESS_RETURN)

    def test_surrounded_on_both_sides(self):
        text = "== begin ==\n" + json.dumps(SUCCESS_RETURN) + "\n== end ==\n"
        found, _, _ = find_terminal(text)
        self.assertEqual(found, SUCCESS_RETURN)

    def test_ndjson_one_object_per_line(self):
        text = "\n".join(
            [
                json.dumps({"type": "started", "agentId": "a1"}),
                json.dumps(
                    {"type": "result", "agentId": "a1", "result": ASSEMBLE_RECEIPT}
                ),
                json.dumps(SUCCESS_RETURN),
            ]
        )
        found, _, _ = find_terminal(text)
        self.assertEqual(found, SUCCESS_RETURN)

    def test_last_qualifying_candidate_wins(self):
        """Terminal means final: a retried producer can leave two."""
        text = json.dumps(FAILURE_RETURN) + "\n" + json.dumps(SUCCESS_RETURN)
        found, _, _ = find_terminal(text)
        self.assertEqual(found, SUCCESS_RETURN)

    def test_crlf_line_endings(self):
        text = "log line\r\n" + json.dumps(SUCCESS_RETURN) + "\r\n"
        found, _, _ = find_terminal(text)
        self.assertEqual(found, SUCCESS_RETURN)

    def test_indented_object_at_line_start_is_not_a_document(self):
        """Leading spaces/tabs must not create probe sites.

        Pretty-printed nested values (indent=2) start their lines indented. If
        the scan skipped that whitespace, a torn mid-write envelope could promote
        a complete nested receipt as the compact return. Column zero only.
        """
        for indent in ("  ", "    ", "\t", " \t "):
            text = "preamble\n" + indent + json.dumps(SUCCESS_RETURN) + "\n"
            found, _, _ = find_terminal(text)
            self.assertIsNone(found, repr(indent))

    def test_utf8_bom_does_not_defeat_detection(self):
        """A BOM is neither whitespace nor '{', so an unhandled one would make the
        file read as never-terminal for the entire wait."""
        with _Workspace() as ws:
            path = ws.write(
                "w1.output",
                ("﻿" + json.dumps(envelope(SUCCESS_RETURN))).encode("utf-8"),
            )
            code, out, _ = run_main([path, "--timeout-seconds", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(sole_json_line(out), SUCCESS_RETURN)

    def test_no_trailing_newline(self):
        found, _, _ = find_terminal("log\n" + json.dumps(SUCCESS_RETURN))
        self.assertEqual(found, SUCCESS_RETURN)

    def test_escaped_object_inside_a_string_is_not_extracted(self):
        """A return quoted inside another field's string value is not terminal."""
        text = json.dumps({"resultPreview": json.dumps(SUCCESS_RETURN)})
        found, _, _ = find_terminal(text)
        self.assertIsNone(found)


class TestScanBounds(unittest.TestCase):
    """The scan is bounded before it allocates, not after."""

    def test_oversized_input_skips_the_scan(self):
        text = "x" * (SCAN_MAX_CHARS + 1) + json.dumps(SUCCESS_RETURN)
        found, _, stopped = find_terminal(text)
        self.assertIsNone(found)
        self.assertEqual(stopped, "max_chars")

    def test_oversized_but_whole_file_parseable_still_detects(self):
        """The whole-file path runs first, so a huge VALID file still resolves."""
        padded = dict(envelope(SUCCESS_RETURN))
        padded["summary"] = "y" * (SCAN_MAX_CHARS + 10)
        found, _, skipped = find_terminal(json.dumps(padded))
        self.assertEqual(found, SUCCESS_RETURN)
        self.assertFalse(skipped)


class TestScanCostIsBounded(unittest.TestCase):
    """Regression: probing at EVERY '{' made the scan quadratic.

    Bounding the successful decodes did not bound the cost, because every failed
    decode still paid to scan forward. `find_terminal('{' * 200_000)` took 7.96s
    and 400 KB of `{"` did not finish inside 15s — inside a wait loop that is a
    hang, not a slowdown. Probing only at document starts removes the probe sites
    entirely; SCAN_MAX_PROBES is the backstop behind it.
    """

    def _timed(self, text):
        started = time.time()
        found, _, _ = find_terminal(text)
        return found, time.time() - started

    def test_dense_open_braces_are_fast(self):
        found, elapsed = self._timed("{" * 200_000)
        self.assertIsNone(found)
        self.assertLess(elapsed, 1.0, f"{elapsed:.2f}s for 200k open braces")

    def test_dense_brace_quote_pairs_are_fast(self):
        found, elapsed = self._timed('{"' * 200_000)
        self.assertIsNone(found)
        self.assertLess(elapsed, 1.0, f"{elapsed:.2f}s for 200k brace-quote pairs")

    def test_many_line_leading_braces_are_fast(self):
        found, elapsed = self._timed('\n{"a":' * 50_000)
        self.assertIsNone(found)
        self.assertLess(elapsed, 2.0, f"{elapsed:.2f}s for 50k line-leading fragments")

    def test_a_real_return_after_heavy_noise_is_still_found(self):
        text = "{" * 50_000 + "\n" + json.dumps(SUCCESS_RETURN)
        found, elapsed = self._timed(text)
        self.assertEqual(found, SUCCESS_RETURN)
        self.assertLess(elapsed, 1.0)


class TestScanDoesNotDropLaterDocuments(unittest.TestCase):
    """Regression: bounding the scan must not cost it real results.

    Two over-corrections, both found by adversarial re-verification of the first
    fix. Each silently dropped a wholly well-formed terminal object that appeared
    later in the same file.
    """

    def test_a_failed_decode_does_not_skip_past_a_later_document(self):
        """The decoder's error `pos` is where it GAVE UP, which can be far past a
        later document start. Using it as a floor refused to probe that offset."""
        text = (
            '{"a": [\n'
            + json.dumps(SUCCESS_RETURN)
            + "\nBROKEN NON JSON GARBAGE THAT MAKES THE OUTER FAIL\n"
        )
        found, _, _ = find_terminal(text)
        self.assertEqual(found, SUCCESS_RETURN)

    def test_a_stack_blowing_candidate_does_not_abort_the_scan(self):
        """A later document start is at a different offset and does not
        re-descend the structure that exhausted the stack."""
        deep = "[" * 60_000 + "1" + "]" * 60_000
        text = '{"a": ' + deep + "}\n" + json.dumps(SUCCESS_RETURN) + "\n"
        found, _, _ = find_terminal(text)
        self.assertEqual(found, SUCCESS_RETURN)


class TestEveryBoundIsDisclosed(unittest.TestCase):
    """A bound that truncates the search must say so.

    The char cap always did; the three added later did not, so a real terminal
    object further down the file was dropped and the marker reported an ordinary
    "nothing here". That is the same silent-drop class twice over.
    """

    def _deep_line(self):
        return '{"a": ' + "[" * 60_000 + "1" + "]" * 60_000 + "}"

    def test_deep_candidate_bound_is_reported(self):
        text = "\n".join(self._deep_line() for _ in range(9)) + "\n"
        found, _, stopped = find_terminal(text)
        self.assertIsNone(found)
        self.assertEqual(stopped, "max_deep_candidates")

    def test_under_the_deep_bound_a_later_terminal_is_still_found(self):
        text = (
            "\n".join(self._deep_line() for _ in range(7))
            + "\n"
            + json.dumps(SUCCESS_RETURN)
            + "\n"
        )
        found, _, stopped = find_terminal(text)
        self.assertEqual(found, SUCCESS_RETURN)
        self.assertIsNone(stopped)

    def test_candidate_bound_is_reported(self):
        text = "\n".join(f'{{"n":{i}}}' for i in range(2000)) + "\n"
        found, _, stopped = find_terminal(text)
        self.assertIsNone(found)
        self.assertIn(stopped, ("max_candidates", "max_probes"))

    def test_a_bound_tripping_after_the_terminal_is_not_reported(self):
        """It cost nothing, so it is not a truncated search."""
        text = (
            json.dumps(SUCCESS_RETURN)
            + "\n"
            + "\n".join(f'{{"n":{i}}}' for i in range(2000))
            + "\n"
        )
        found, _, stopped = find_terminal(text)
        self.assertEqual(found, SUCCESS_RETURN)
        self.assertIsNone(stopped)

    def test_the_marker_carries_the_reason(self):
        with _Workspace() as ws:
            path = ws.write(
                "w1.output",
                "\n".join(self._deep_line() for _ in range(9)) + "\n",
            )
            _, out, _ = run_main([path, "--timeout-seconds", "0"])
        marker = sole_json_line(out)
        self.assertTrue(marker["scan_skipped"])
        self.assertEqual(marker["scan_stop_reason"], "max_deep_candidates")


class TestBrokenPipeDegradesToADocumentedCode(unittest.TestCase):
    """A reader that closes the pipe early must not surface as an exit-1 crash."""

    def test_broken_pipe_exits_four_not_one(self):
        with _Workspace() as ws:
            big = dict(SUCCESS_RETURN)
            big["stats"] = {"pad": "x" * 2_000_000}
            path = ws.write("w1.output", json.dumps(envelope(big)))
            script = os.path.join(REPO_ROOT, "scripts", "await_workflow.py")
            reader = subprocess.Popen(
                ["head", "-c", "20"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
            )
            writer = subprocess.Popen(
                ["python3", script, "--timeout-seconds", "0", "--", path],
                stdout=reader.stdin,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert reader.stdin is not None
            reader.stdin.close()
            _, err = writer.communicate()
            reader.wait()
        self.assertIn(writer.returncode, (0, 4), err)
        self.assertNotIn("BrokenPipeError", err)
        self.assertNotIn("Traceback", err)


class TestResolutionPrefersFreshness(unittest.TestCase):
    """Regression: lexicographic tie-breaking could return another session's run.

    Task ids are short and random, so a glob across every session directory can
    match more than one. Picking by path spelling is arbitrary — and arbitrarily
    dangerous, because the loser may be a COMPLETED run whose terminal result
    would be handed to Phase 8 as if it were this review's.
    """

    def test_newest_candidate_wins_over_the_alphabetically_first(self):
        with _Workspace() as ws:
            old = os.path.join(ws.path, "aaa", "tasks")
            new = os.path.join(ws.path, "zzz", "tasks")
            os.makedirs(old)
            os.makedirs(new)
            stale = os.path.join(old, "wdup123.output")
            fresh = os.path.join(new, "wdup123.output")
            for path, mtime in ((stale, time.time() - 3600), (fresh, time.time())):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("{}")
                os.utime(path, (mtime, mtime))
            self.assertEqual(_newest([stale, fresh]), fresh)
            self.assertEqual(_newest([fresh, stale]), fresh)

    def test_newest_tolerates_a_vanished_candidate(self):
        with _Workspace() as ws:
            real = ws.write("present.output", "{}")
            self.assertEqual(_newest(["/nonexistent/gone.output", real]), real)

    def test_newest_of_nothing_is_none(self):
        self.assertIsNone(_newest([]))


class TestTerminalPathIsSilentOnStderr(unittest.TestCase):
    """The documented caller is a Bash tool call, which merges the streams.

    A diagnostic line on the success path would arrive in FRONT of the payload,
    so "stdout is the terminal return" would stop being true for the only caller
    that matters.
    """

    def test_no_stderr_on_a_terminal_result(self):
        with _Workspace() as ws:
            path = ws.write("w1.output", json.dumps(envelope(SUCCESS_RETURN)))
            code, out, err = run_main([path, "--timeout-seconds", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(sole_json_line(out), SUCCESS_RETURN)

    def test_merged_streams_still_yield_exactly_one_line(self):
        with _Workspace() as ws:
            path = ws.write("w1.output", json.dumps(envelope(SUCCESS_RETURN)))
            proc = subprocess.run(
                [
                    "python3",
                    os.path.join(REPO_ROOT, "scripts", "await_workflow.py"),
                    "--timeout-seconds",
                    "0",
                    "--",
                    path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len([x for x in proc.stdout.split("\n") if x]), 1)
        self.assertEqual(json.loads(proc.stdout.strip()), SUCCESS_RETURN)


class TestPersistReturnIsElided(unittest.TestCase):
    """The RETURN persist channel's payload must not enter the caller's context.

    On that channel the compact return carries the artifacts themselves so the
    HARNESS can put them on disk without a model retyping them. This stdout is
    read by a Bash tool call, i.e. straight into the orchestrator's context, and
    `scripts/materialize_artifacts.py` reads the same payload out of the file —
    so printing it here is pure waste. What the caller does need is the path of
    the file it came from.
    """

    @staticmethod
    def _return_with_payload(text="x" * 5000):
        payload = dict(SUCCESS_RETURN)
        payload["persistReturn"] = {
            "channel": "return",
            "nonce": "nonce-abc",
            "planPath": "/out/code-gauntlet-persist-plan-da09bc08.json",
            "entries": [
                {"path": "/out/code-gauntlet-findings-da09bc08.json", "text": text},
                {"path": "/out/code-gauntlet-report-da09bc08.md", "text": "# report"},
            ],
        }
        return payload

    def test_the_entries_never_reach_stdout(self):
        with _Workspace() as ws:
            path = ws.write(
                "w1.output", json.dumps(envelope(self._return_with_payload()))
            )
            code, out, _err = run_main([path, "--timeout-seconds", "0"])
        self.assertEqual(code, 0)
        self.assertNotIn("x" * 5000, out)
        self.assertLess(len(out), 2000, "the bulk is still crossing the boundary")
        printed = sole_json_line(out)["persistReturn"]
        self.assertNotIn(
            "entries", printed, "an entry list without its text invites empty writes"
        )
        self.assertTrue(printed["elided"])
        self.assertEqual(
            printed["paths"],
            [
                "/out/code-gauntlet-findings-da09bc08.json",
                "/out/code-gauntlet-report-da09bc08.md",
            ],
        )

    def test_the_resolved_path_rides_back_so_the_caller_need_not_re_resolve(self):
        with _Workspace() as ws:
            path = ws.write(
                "w1.output", json.dumps(envelope(self._return_with_payload()))
            )
            _code, out, _err = run_main([path, "--timeout-seconds", "0"])
        self.assertEqual(sole_json_line(out)["persistReturn"]["resolvedPath"], path)

    def test_every_other_key_is_untouched(self):
        with _Workspace() as ws:
            path = ws.write(
                "w1.output", json.dumps(envelope(self._return_with_payload()))
            )
            _code, out, _err = run_main([path, "--timeout-seconds", "0"])
        printed = sole_json_line(out)
        del printed["persistReturn"]
        self.assertEqual(printed, SUCCESS_RETURN)

    def test_a_return_on_any_other_channel_is_printed_verbatim(self):
        with _Workspace() as ws:
            path = ws.write("w1.output", json.dumps(envelope(SUCCESS_RETURN)))
            _code, out, _err = run_main([path, "--timeout-seconds", "0"])
        self.assertEqual(sole_json_line(out), SUCCESS_RETURN)


class TestTruncatedFragmentIsNotPromoted(unittest.TestCase):
    """Regression: a torn read must not yield a nested receipt as the return.

    A half-written envelope can end just after a nested object closes. One such
    object — an agent's recorded receipt inside workflowProgress — carries a
    boolean `ok` and a key that overlaps COMPACT_RETURN_KEYS, and the old
    every-brace scan accepted it as the pipeline's compact return.
    """

    def test_nested_receipt_in_a_truncated_envelope_is_rejected(self):
        fragment = (
            '{"summary":"x","result":null,"workflowProgress":'
            '[{"type":"workflow_agent","lastToolSummary":'
            '{"ok":true,"stats":{"found":4}}'
        )
        found, _, _ = find_terminal(fragment)
        self.assertIsNone(found)

    def test_nested_return_in_a_truncated_envelope_is_rejected(self):
        """Even the genuine return, if only reachable as a mid-line fragment of a
        file that does not parse, is not trusted — the file is still being
        written, and the next poll will read it whole."""
        fragment = '{"summary":"x","result":' + json.dumps(SUCCESS_RETURN)
        found, _, _ = find_terminal(fragment)
        self.assertIsNone(found)

    def test_indented_array_element_in_torn_pretty_envelope_is_rejected(self):
        """Regression: indent=2 puts array-element `{` on its own indented line.

        Skipping that whitespace made a complete nested object with ok+stats a
        probe site on a mid-write read — promoting it as the compact return.
        """
        env = {
            "summary": "x",
            "result": None,
            "workflowProgress": [
                {"ok": True, "stats": {"found": 4}, "phaseReached": "report"},
            ],
        }
        pretty = json.dumps(env, indent=2)
        decoder = json.JSONDecoder()
        brace = pretty.find("{", pretty.find("["))
        _, end = decoder.raw_decode(pretty, brace)
        fragment = pretty[:end]
        found, _, _ = find_terminal(fragment)
        self.assertIsNone(found)

    def test_a_complete_envelope_is_unaffected(self):
        found, _, _ = find_terminal(json.dumps(envelope(SUCCESS_RETURN)))
        self.assertEqual(found, SUCCESS_RETURN)

    def test_a_complete_pretty_envelope_is_unaffected(self):
        found, _, _ = find_terminal(json.dumps(envelope(SUCCESS_RETURN), indent=2))
        self.assertEqual(found, SUCCESS_RETURN)


class TestNonRegularFileTargets(unittest.TestCase):
    """Regression: open() on a FIFO blocks forever and prints nothing."""

    def test_fifo_target_does_not_block(self):
        with _Workspace() as ws:
            fifo = os.path.join(ws.path, "afifo.output")
            os.mkfifo(fifo)
            try:
                code, out, _ = run_main([fifo, "--timeout-seconds", "0"])
            finally:
                os.unlink(fifo)
        self.assertEqual(code, 3)
        self.assertEqual(sole_json_line(out)["await"], "pending")

    def test_directory_target_does_not_raise(self):
        with _Workspace() as ws:
            os.mkdir(os.path.join(ws.path, "d.output"))
            code, out, _ = run_main(
                [os.path.join(ws.path, "d.output"), "--timeout-seconds", "0"]
            )
        self.assertEqual(code, 3)
        self.assertEqual(sole_json_line(out)["await"], "pending")


class TestArtifactFlagsMustBePaired(unittest.TestCase):
    """A safety net that silently turns itself off is worse than none."""

    def test_artifacts_dir_without_head_sha_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            run_main(["w1", "--timeout-seconds", "0", "--artifacts-dir", "/tmp/x"])
        self.assertEqual(caught.exception.code, 2)

    def test_head_sha_without_artifacts_dir_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            run_main(["w1", "--timeout-seconds", "0", "--head-sha", "abc12345"])
        self.assertEqual(caught.exception.code, 2)


class TestSawOkWithoutCorroborator(unittest.TestCase):
    """The tripwire for a future return-shape change."""

    def test_flag_set_for_bare_ok(self):
        _, bare, _ = find_terminal(json.dumps(envelope({"ok": True})))
        self.assertTrue(bare)

    def test_flag_clear_for_a_real_return(self):
        _, bare, _ = find_terminal(json.dumps(envelope(SUCCESS_RETURN)))
        self.assertFalse(bare)


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


class TestResolveTarget(unittest.TestCase):
    def test_absolute_path_used_verbatim(self):
        path, searched = resolve_target("/tmp/x/tasks/w1.output", {})
        self.assertEqual(path, "/tmp/x/tasks/w1.output")
        self.assertEqual(searched, [])

    def test_bare_output_filename_used_verbatim(self):
        path, _ = resolve_target("w1.output", {})
        self.assertEqual(path, "w1.output")

    def test_task_id_resolved_under_the_env_override(self):
        with _Workspace() as ws:
            ws.write("wabc123.output", "")
            path, searched = resolve_target(
                "wabc123", {"CODE_GAUNTLET_TASKS_DIR": ws.path}
            )
            self.assertEqual(path, os.path.join(ws.path, "wabc123.output"))
            self.assertTrue(searched)

    def test_unresolvable_id_returns_none_and_reports_what_it_tried(self):
        path, searched = resolve_target("wnosuchtask000", {})
        self.assertIsNone(path)
        self.assertTrue(all("wnosuchtask000.output" in p for p in searched))

    def test_searched_is_reported_even_when_no_root_exists(self):
        """A machine with no claude-<uid> temp dir at all must still say what it
        looked for. Filtering non-existent roots made `searched` empty there, so
        the marker named no reason and pointed at no fix — which is the whole
        job of that field. CI is exactly such a machine."""
        path, searched = resolve_target("wnosuchtask000", {"TMPDIR": "/no/such/tmp"})
        self.assertIsNone(path)
        self.assertTrue(searched, "searched must never be empty on a failure")
        self.assertTrue(any("/no/such/tmp" in p for p in searched))

    def test_env_override_miss_falls_through_to_the_globs(self):
        path, searched = resolve_target(
            "wnosuchtask000", {"CODE_GAUNTLET_TASKS_DIR": "/nonexistent-dir"}
        )
        self.assertIsNone(path)
        self.assertIn("/nonexistent-dir/wnosuchtask000.output", searched)


# ---------------------------------------------------------------------------
# The artifacts secondary signal
# ---------------------------------------------------------------------------


class TestArtifactsState(unittest.TestCase):
    def _write_all(self, ws, sha, mtime=None):
        for template in ARTIFACT_BASENAMES:
            ws.write(template.format(sha=sha), "content", mtime=mtime)

    def test_not_checked_without_dir_or_sha(self):
        state = artifacts_state(None, None, 0)
        self.assertFalse(state["checked"])
        self.assertFalse(state["complete"])

    def test_complete_when_all_four_are_present_and_fresh(self):
        with _Workspace() as ws:
            since = time.time() - 10
            self._write_all(ws, "abc12345")
            state = artifacts_state(ws.path, "abc12345", since)
            self.assertTrue(state["complete"])
            self.assertEqual(len(state["present"]), len(ARTIFACT_BASENAMES))
            self.assertEqual(state["missing"], [])

    def test_incomplete_when_one_is_missing(self):
        with _Workspace() as ws:
            since = time.time() - 10
            for template in ARTIFACT_BASENAMES[:-1]:
                ws.write(template.format(sha="abc12345"), "content")
            state = artifacts_state(ws.path, "abc12345", since)
            self.assertFalse(state["complete"])
            self.assertEqual(len(state["missing"]), 1)

    def test_empty_file_does_not_count_as_present(self):
        with _Workspace() as ws:
            since = time.time() - 10
            self._write_all(ws, "abc12345")
            ws.write(ARTIFACT_BASENAMES[0].format(sha="abc12345"), "")
            state = artifacts_state(ws.path, "abc12345", since)
            self.assertFalse(state["complete"])

    def test_stale_artifacts_do_not_count(self):
        """The anti-stale guard: a previous run at the same head SHA must not be
        delivered as if it were this one. Without the mtime floor a skipped
        Phase 2 stale_truncate would hand Phase 8 the wrong review."""
        with _Workspace() as ws:
            since = time.time()
            self._write_all(ws, "abc12345", mtime=since - 3600)
            state = artifacts_state(ws.path, "abc12345", since)
            self.assertFalse(state["complete"])
            self.assertEqual(len(state["missing"]), len(ARTIFACT_BASENAMES))

    def test_a_directory_named_like_an_artifact_is_not_fresh(self):
        with _Workspace() as ws:
            since = time.time() - 10
            self._write_all(ws, "abc12345")
            target = os.path.join(ws.path, ARTIFACT_BASENAMES[0].format(sha="abc12345"))
            os.unlink(target)
            os.mkdir(target)
            state = artifacts_state(ws.path, "abc12345", since)
            self.assertFalse(state["complete"])


# ---------------------------------------------------------------------------
# Timeout default
# ---------------------------------------------------------------------------


class TestDefaultTimeoutSeconds(unittest.TestCase):
    def test_unset_uses_the_constant(self):
        self.assertEqual(default_timeout_seconds({}), DEFAULT_TIMEOUT_SECONDS)

    def test_bash_ceiling_lowers_it_with_headroom(self):
        self.assertEqual(
            default_timeout_seconds({"BASH_MAX_TIMEOUT_MS": "300000"}), 240
        )

    def test_generous_ceiling_does_not_raise_it(self):
        self.assertEqual(
            default_timeout_seconds({"BASH_MAX_TIMEOUT_MS": "3600000"}),
            DEFAULT_TIMEOUT_SECONDS,
        )

    def test_tiny_ceiling_is_floored(self):
        self.assertEqual(
            default_timeout_seconds({"BASH_MAX_TIMEOUT_MS": "1000"}),
            MIN_TIMEOUT_SECONDS,
        )

    def test_unparseable_value_changes_nothing(self):
        for raw in ("", "abc", "None"):
            self.assertEqual(
                default_timeout_seconds({"BASH_MAX_TIMEOUT_MS": raw}),
                DEFAULT_TIMEOUT_SECONDS,
                raw,
            )


# ---------------------------------------------------------------------------
# Exit codes and the one-line stdout contract
# ---------------------------------------------------------------------------


class TestExitCodeContract(unittest.TestCase):
    """Every outcome: one JSON line on stdout, and the documented exit code."""

    def test_terminal_exits_zero_with_the_return_verbatim(self):
        with _Workspace() as ws:
            path = ws.write("w1.output", json.dumps(envelope(SUCCESS_RETURN)))
            code, out, err = run_main([path, "--timeout-seconds", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(sole_json_line(out), SUCCESS_RETURN)
        self.assertEqual(err, "", "the terminal path must not write to stderr")

    def test_terminal_failure_return_also_exits_zero(self):
        """ok:false is a terminal result, not a timeout."""
        with _Workspace() as ws:
            path = ws.write("w1.output", json.dumps(envelope(FAILURE_RETURN)))
            code, out, _ = run_main([path, "--timeout-seconds", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(sole_json_line(out)["ok"], False)

    def test_pending_exits_three_with_a_next_command(self):
        with _Workspace() as ws:
            path = ws.write("w1.output", "")
            code, out, _ = run_main(
                [
                    path,
                    "--timeout-seconds",
                    "0",
                    "--attempt",
                    "1",
                    "--max-attempts",
                    "4",
                ]
            )
        marker = sole_json_line(out)
        self.assertEqual(code, 3)
        self.assertEqual(marker["await"], "pending")
        self.assertIn("--attempt 2", marker["next_command"])
        self.assertNotIn("gap", marker)

    def test_exhausted_exits_four_with_the_workflow_timeout_gap(self):
        with _Workspace() as ws:
            path = ws.write("w1.output", "")
            code, out, _ = run_main(
                [
                    path,
                    "--timeout-seconds",
                    "0",
                    "--attempt",
                    "4",
                    "--max-attempts",
                    "4",
                ]
            )
        marker = sole_json_line(out)
        self.assertEqual(code, 4)
        self.assertEqual(marker["await"], "timeout")
        self.assertEqual(marker["gap"], "workflow-timeout")
        self.assertNotIn("next_command", marker)

    def test_missing_file_is_pending_not_a_crash(self):
        code, out, _ = run_main(
            ["/nonexistent/tasks/w1.output", "--timeout-seconds", "0"]
        )
        self.assertEqual(code, 3)
        self.assertEqual(sole_json_line(out)["await"], "pending")

    def test_unresolvable_id_reports_what_it_searched(self):
        code, out, _ = run_main(["wnosuchtask000", "--timeout-seconds", "0"])
        marker = sole_json_line(out)
        self.assertEqual(code, 3)
        self.assertIsNone(marker["resolved_path"])
        self.assertTrue(marker["searched"])

    def test_target_that_is_a_directory_is_pending_not_a_crash(self):
        with _Workspace() as ws:
            os.mkdir(os.path.join(ws.path, "w1.output"))
            code, out, _ = run_main(
                [os.path.join(ws.path, "w1.output"), "--timeout-seconds", "0"]
            )
        self.assertEqual(code, 3)
        self.assertEqual(sole_json_line(out)["await"], "pending")

    def test_undecodable_bytes_are_tolerated(self):
        with _Workspace() as ws:
            path = ws.write("w1.output", b"\xff\xfe\x00 not utf-8")
            code, out, _ = run_main([path, "--timeout-seconds", "0"])
        self.assertEqual(code, 3)
        self.assertEqual(sole_json_line(out)["await"], "pending")

    def test_unexpected_failure_still_prints_one_line_and_degrades(self):
        with patch(
            "scripts.await_workflow.await_terminal", side_effect=RuntimeError("boom")
        ):
            code, out, _ = run_main(["w1", "--timeout-seconds", "0"])
        marker = sole_json_line(out)
        self.assertEqual(code, 4)
        self.assertEqual(marker["await"], "error")
        self.assertEqual(marker["gap"], "workflow-timeout")
        self.assertIn("boom", marker["message"])

    def test_keyboard_interrupt_prints_interrupted_marker(self):
        """KeyboardInterrupt is not an Exception subclass — its branch is distinct."""
        with patch(
            "scripts.await_workflow.await_terminal", side_effect=KeyboardInterrupt()
        ):
            code, out, _ = run_main(["w1", "--timeout-seconds", "0"])
        marker = sole_json_line(out)
        self.assertEqual(code, 4)
        self.assertEqual(marker["await"], "error")
        self.assertEqual(marker["gap"], "workflow-timeout")
        self.assertEqual(marker["message"], "interrupted")

    def test_broken_pipe_during_error_report_exits_four(self):
        """Error-path emit must share the happy-path OSError degrade, not exit 1."""
        with (
            patch(
                "scripts.await_workflow.await_terminal", side_effect=KeyboardInterrupt()
            ),
            patch("builtins.print", side_effect=BrokenPipeError()),
        ):
            code, out, err = run_main(["w1", "--timeout-seconds", "0"])
        self.assertEqual(code, 4)
        self.assertNotIn("Traceback", err)

    def test_non_serializable_payload_emits_fallback_line(self):
        out = io.StringIO()
        with patch("sys.stdout", new=out):
            emit({"await": "ok", "bad": {1, 2, 3}})
        marker = sole_json_line(out.getvalue())
        self.assertEqual(marker["await"], "error")
        self.assertEqual(marker["gap"], "workflow-timeout")
        self.assertIn("serialize", marker["message"])

    def test_marker_fields_are_always_present(self):
        with _Workspace() as ws:
            path = ws.write("w1.output", "")
            _, out, _ = run_main([path, "--timeout-seconds", "0"])
        marker = sole_json_line(out)
        for key in (
            "await",
            "attempt",
            "max_attempts",
            "waited_seconds",
            "target",
            "resolved_path",
            "file_bytes",
            "since_epoch",
            "artifacts",
            "saw_ok_without_corroborator",
            "scan_skipped",
            "scan_stop_reason",
        ):
            self.assertIn(key, marker)


class TestArtifactsOnlyOutcome(unittest.TestCase):
    def _write_all(self, ws, sha):
        for template in ARTIFACT_BASENAMES:
            ws.write(template.format(sha=sha), "content")

    def test_unresolvable_target_with_artifacts_present_exits_five(self):
        """Resolution has failed, so the fallback is all there will ever be —
        stop as soon as the grace window closes instead of spending the budget."""
        with _Workspace() as ws:
            self._write_all(ws, "abc12345")
            code, out, _ = run_main(
                [
                    "wnosuchtask000",
                    "--timeout-seconds",
                    "0",
                    "--artifacts-grace-seconds",
                    "0",
                    "--artifacts-dir",
                    ws.path,
                    "--head-sha",
                    "abc12345",
                    "--since-epoch",
                    str(time.time() - 60),
                ]
            )
        marker = sole_json_line(out)
        self.assertEqual(code, 5)
        self.assertEqual(marker["await"], "artifacts_only")
        self.assertEqual(marker["gap"], "workflow-timeout")
        self.assertTrue(marker["artifacts"]["complete"])

    def test_a_terminal_return_beats_the_artifacts_signal(self):
        with _Workspace() as ws:
            self._write_all(ws, "abc12345")
            path = ws.write("w1.output", json.dumps(envelope(SUCCESS_RETURN)))
            code, out, _ = run_main(
                [
                    path,
                    "--timeout-seconds",
                    "0",
                    "--artifacts-dir",
                    ws.path,
                    "--head-sha",
                    "abc12345",
                    "--since-epoch",
                    str(time.time() - 60),
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(sole_json_line(out), SUCCESS_RETURN)

    def test_a_resolved_target_is_not_abandoned_on_a_non_final_attempt(self):
        """The return is still coming when we are watching the right file, so the
        grace window must not discard it — the fallback only converts the FINAL
        attempt's timeout, never an early one."""
        with _Workspace() as ws:
            self._write_all(ws, "abc12345")
            path = ws.write("w1.output", "")
            code, out, _ = run_main(
                [
                    path,
                    "--timeout-seconds",
                    "0",
                    "--attempt",
                    "1",
                    "--max-attempts",
                    "4",
                    "--artifacts-grace-seconds",
                    "0",
                    "--artifacts-dir",
                    ws.path,
                    "--head-sha",
                    "abc12345",
                    "--since-epoch",
                    str(time.time() - 60),
                ]
            )
        marker = sole_json_line(out)
        self.assertEqual(code, 3)
        self.assertEqual(marker["await"], "pending")
        self.assertTrue(marker["artifacts"]["complete"])

    def test_a_resolved_target_converts_the_final_timeout_into_a_delivery(self):
        with _Workspace() as ws:
            self._write_all(ws, "abc12345")
            path = ws.write("w1.output", "")
            code, out, _ = run_main(
                [
                    path,
                    "--timeout-seconds",
                    "0",
                    "--attempt",
                    "4",
                    "--max-attempts",
                    "4",
                    "--artifacts-dir",
                    ws.path,
                    "--head-sha",
                    "abc12345",
                    "--since-epoch",
                    str(time.time() - 60),
                ]
            )
        self.assertEqual(code, 5)
        self.assertEqual(sole_json_line(out)["await"], "artifacts_only")

    def test_stale_artifacts_do_not_trigger_the_fallback(self):
        with _Workspace() as ws:
            for template in ARTIFACT_BASENAMES:
                ws.write(
                    template.format(sha="abc12345"), "content", mtime=time.time() - 3600
                )
            code, out, _ = run_main(
                [
                    "wnosuchtask000",
                    "--timeout-seconds",
                    "0",
                    "--artifacts-dir",
                    ws.path,
                    "--head-sha",
                    "abc12345",
                    "--attempt",
                    "4",
                    "--max-attempts",
                    "4",
                ]
            )
        self.assertEqual(code, 4)
        self.assertEqual(sole_json_line(out)["await"], "timeout")


class TestNextCommand(unittest.TestCase):
    """The next attempt is copy-paste, not inference."""

    def _args(self, **over):
        target = over.pop("target", "w1")
        argv = ["--timeout-seconds", "0"]
        for key, value in over.items():
            argv += ["--" + key.replace("_", "-"), str(value)]
        # Target last, behind `--` — the same shape the script emits, and the
        # only shape that survives a target beginning with a dash.
        return build_parser({}).parse_args([*argv, "--", target])

    def test_increments_the_attempt(self):
        cmd = build_next_command(self._args(attempt=2, max_attempts=4), None, 1.0)
        self.assertIn("--attempt 3", cmd)
        self.assertIn("--max-attempts 4", cmd)

    def test_preserves_the_original_since_epoch(self):
        """Artifact freshness must survive across attempts, or a file written
        during attempt 1 stops counting as fresh by attempt 4."""
        cmd = build_next_command(self._args(), None, 1785280021.5)
        self.assertIn("--since-epoch 1785280021.5", cmd)

    def test_prefers_the_resolved_path_over_the_id(self):
        cmd = build_next_command(self._args(target="wabc"), "/tmp/t/w.output", 1.0)
        self.assertIn("/tmp/t/w.output", cmd)

    def test_carries_the_artifacts_flags_forward(self):
        args = self._args(artifacts_dir="/out", head_sha="abc12345")
        cmd = build_next_command(args, None, 1.0)
        self.assertIn("--artifacts-dir /out", cmd)
        self.assertIn("--head-sha abc12345", cmd)
        self.assertIn("--artifacts-grace-seconds", cmd)

    def test_is_ast_safe(self):
        """No variable reference, command substitution, or heredoc — those are
        the forms the tree-sitter-bash parser silently denies."""
        cmd = build_next_command(
            self._args(target="/tmp/a b/w'x.output"), "/tmp/a b/w'x.output", 1.0
        )
        for forbidden in ("$(", "`", "${", "<<"):
            self.assertNotIn(forbidden, cmd)

    def test_target_goes_last_behind_a_double_dash(self):
        """A leading-dash target would otherwise be parsed as a flag, and the
        error would reproduce on every retry since the target is echoed back."""
        cmd = build_next_command(self._args(), "-weird.output", 1.0)
        self.assertTrue(cmd.rstrip().endswith("-- -weird.output"), cmd)

    def test_a_leading_dash_target_round_trips(self):
        """The target must lead with '-' at the ARGV-TOKEN level to reproduce the
        bug. An absolute path with a dashed basename starts with '/', so argparse
        never sees an option-like token — a fixture built that way passes even
        with the fix reverted, which makes it decoration rather than a guard.
        """
        with _Workspace() as ws:
            ws.write("-dashy.output", "")
            args = self._args(target="-dashy.output")
            cmd = build_next_command(args, "-dashy.output", 1.0)
            self.assertIn("-- -dashy.output", cmd)
            # Run it exactly as emitted, from the workspace, so the target really
            # is the bare relative token `-dashy.output`.
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, cwd=ws.path
            )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        marker = json.loads(proc.stdout.strip())
        self.assertEqual(marker["attempt"], 2)
        self.assertEqual(marker["target"], "-dashy.output")

    def test_is_actually_runnable(self):
        """Round-trip it: the printed command must run and behave identically."""
        with _Workspace() as ws:
            path = ws.write("weird name'quote.output", "")
            _, out, _ = run_main(
                [path, "--timeout-seconds", "0", "--poll-interval", "0"]
            )
            cmd = sole_json_line(out)["next_command"]
            # shell=True is the property under test, not an oversight: the script
            # emits a command STRING that the orchestrator pastes into a Bash tool
            # call, so the only way to prove the quoting holds is to hand it to a
            # shell. The string is built by our own code from our own argv.
            # Run it exactly as emitted — nothing may be appended, because the
            # target rides behind a trailing `--` and a later flag would be
            # swallowed as a positional.
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, cwd=REPO_ROOT
            )
        self.assertEqual(proc.returncode, 3)
        marker = json.loads(proc.stdout.strip())
        self.assertEqual(marker["attempt"], 2)


class TestAttemptBounds(unittest.TestCase):
    def test_attempt_below_max_is_pending(self):
        code, _, _ = run_main(
            [
                "wnosuchtask000",
                "--timeout-seconds",
                "0",
                "--attempt",
                "3",
                "--max-attempts",
                "4",
            ]
        )
        self.assertEqual(code, 3)

    def test_attempt_at_max_is_exhausted(self):
        code, _, _ = run_main(
            [
                "wnosuchtask000",
                "--timeout-seconds",
                "0",
                "--attempt",
                "4",
                "--max-attempts",
                "4",
            ]
        )
        self.assertEqual(code, 4)

    def test_attempt_past_max_is_exhausted(self):
        code, _, _ = run_main(
            [
                "wnosuchtask000",
                "--timeout-seconds",
                "0",
                "--attempt",
                "9",
                "--max-attempts",
                "4",
            ]
        )
        self.assertEqual(code, 4)

    def test_zero_max_attempts_is_exhausted_immediately(self):
        code, out, _ = run_main(
            [
                "wnosuchtask000",
                "--timeout-seconds",
                "0",
                "--attempt",
                "1",
                "--max-attempts",
                "0",
            ]
        )
        self.assertEqual(code, 4)
        self.assertEqual(sole_json_line(out)["await"], "timeout")


class TestWaitLoop(unittest.TestCase):
    """The blocking behaviour itself: it must actually wait, and actually stop."""

    def test_detects_a_result_that_appears_mid_wait(self):
        with _Workspace() as ws:
            path = ws.write("w1.output", "")
            real_sleep = time.sleep
            state = {"ticks": 0}

            def fake_sleep(seconds):
                state["ticks"] += 1
                if state["ticks"] == 2:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(json.dumps(envelope(SUCCESS_RETURN)))
                real_sleep(0)

            with patch("scripts.await_workflow.time.sleep", side_effect=fake_sleep):
                code, out, _ = run_main(
                    [path, "--timeout-seconds", "5", "--poll-interval", "1"]
                )
        self.assertEqual(code, 0)
        self.assertEqual(sole_json_line(out), SUCCESS_RETURN)
        self.assertGreaterEqual(state["ticks"], 2)

    def test_returns_at_the_deadline(self):
        with _Workspace() as ws:
            path = ws.write("w1.output", "")
            started = time.time()
            code, _, _ = run_main(
                [path, "--timeout-seconds", "0.3", "--poll-interval", "0.1"]
            )
            elapsed = time.time() - started
        self.assertEqual(code, 3)
        self.assertLess(elapsed, 5)


# ---------------------------------------------------------------------------
# Lockstep and acceptance guards
# ---------------------------------------------------------------------------


class TestArtifactNamingLockstep(unittest.TestCase):
    """ARTIFACT_BASENAMES must track workflows/src/stages.js.

    The JS side is the only producer of these paths. A rename there with no change
    here would leave the artifacts fallback silently blind forever — it would never
    see a complete set again, on any run, with nothing anywhere saying so.
    """

    @staticmethod
    def _stages_js():
        with open(
            os.path.join(REPO_ROOT, "workflows", "src", "stages.js"), encoding="utf-8"
        ) as fh:
            return fh.read()

    def test_directly_built_basenames_appear_in_stages_js(self):
        """The three artifactPaths entries are literal templates over there."""
        source = self._stages_js()
        for template in ARTIFACT_BASENAMES:
            if "checkpoint" in template:
                continue  # composed via checkpointPath(); asserted below
            literal = template.replace("{sha}", "${sha}")
            self.assertIn(literal, source, f"{template} is not produced by stages.js")

    def test_checkpoint_all_is_still_how_the_combined_checkpoint_is_named(self):
        """The checkpoint name is composed, so assert both halves of it."""
        source = self._stages_js()
        self.assertRegex(
            source, r"code-gauntlet-checkpoint-\$\{phase\}-\$\{sha\}\.json"
        )
        self.assertRegex(source, r"checkpointPath\(\s*'all'")
        self.assertIn("code-gauntlet-checkpoint-all-{sha}.json", ARTIFACT_BASENAMES)


class TestWaitProtocolAcceptance(unittest.TestCase):
    """Issue #26 R3's stated acceptance form: no copy of the old loop survives."""

    def _skill_markdown(self):
        paths = []
        for root, _dirs, files in os.walk(os.path.join(REPO_ROOT, "skills")):
            for name in files:
                if name.endswith(".md"):
                    paths.append(os.path.join(root, name))
        return paths

    def test_no_sleep_poll_loop_survives_anywhere_under_skills(self):
        offenders = []
        for path in self._skill_markdown():
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            if re.search(r"\bsleep\s+\d+\b", text):
                offenders.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(offenders, [], "sleep-based poll loop still present")

    def test_both_wait_protocol_copies_reference_the_awaiter(self):
        for rel in (
            "skills/code-gauntlet/SKILL.md",
            "skills/code-gauntlet/references/phase3-dispatch.md",
        ):
            with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as fh:
                self.assertIn("await_workflow.py", fh.read(), rel)

    def test_headless_reference_points_at_the_protocol(self):
        path = os.path.join(
            REPO_ROOT, "skills", "code-gauntlet", "references", "headless-mode.md"
        )
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("await_workflow.py", text)
        self.assertIn("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", text)


if __name__ == "__main__":
    unittest.main()
