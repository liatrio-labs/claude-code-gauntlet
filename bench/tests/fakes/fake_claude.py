#!/usr/bin/env python3
"""Fake ``claude`` binary for invoke.py tests. Placed on PATH as ``claude``.

Behavior is selected by env ``FAKE_CLAUDE_MODE``:
  ok             -> canned "Headless config:" echo (8 bench knobs) + a success result
                    envelope (total_cost_usd 1.23, modelUsage, usage, empty
                    permission_denials), and a fake post-review-payload.json under
                    $DEEP_REVIEW_OUTPUT_DIR.
  hang           -> record our process-group id, then sleep well past any test timeout.
  asks           -> echo + envelope whose permission_denials names AskUserQuestion.
  badecho        -> partial echo (missing trivial_scope) in BOTH stdout and the envelope
                    .result -> a partial receipt is never accepted. + normal envelope + payload.
  echo_in_result -> NO echo in stdout; the full block lives only in the envelope .result
                    (models a ``-p --output-format json`` run). + payload.
  echo_in_report -> NO echo in stdout or .result; the full block lives only in a collected
                    report .md under $DEEP_REVIEW_OUTPUT_DIR. + payload.
  mutate_repo    -> write to FAKE_CLAUDE_MUTATE_PATH (inside the plugin repo), then behave
                    like ``ok`` — models a child self-healing the plugin mid-run.

All CLI args are ignored for behavior selection. If FAKE_CLAUDE_PIDFILE is set, the
process-group id is written there at startup so the watchdog test can prove the group was
killed. If FAKE_CLAUDE_ARGV_FILE is set, the review invocation's argv is recorded there
(one arg per line) so a test can assert the exact ``-p`` command the runner built -- the
``--version`` preflight probe does not record (it returns before reaching main's body).
"""

import json
import os
import sys
import time

# The exact echo the real skill prints under DEEP_REVIEW_HEADLESS=1 (Task 3 format:
# two-space indent, key=value (source), 8 knob lines under the header).
ECHO_LINES = [
    "Headless config:",
    "  model_tier=optimized (env)",
    "  delivery=pr_comments,markdown (env)",
    "  post_mode=dry-run (env)",
    "  pr_comment_cap=25 (env)",
    "  draft_policy=review (env)",
    "  reviewed_policy=full (env)",
    "  pr_not_found_policy=error (env)",
    "  trivial_scope=full (env)",
]


def _envelope(permission_denials, result_text="Review complete. 3 findings posted (dry-run)."):
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 7,
        "result": result_text,
        "total_cost_usd": 1.23,
        "modelUsage": {
            "claude-opus-4-8": {
                "inputTokens": 1200,
                "outputTokens": 800,
                "cacheReadInputTokens": 400,
                "cacheCreationInputTokens": 100,
                "costUSD": 1.23,
            }
        },
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 800,
            "cache_read_input_tokens": 400,
            "cache_creation_input_tokens": 100,
        },
        "permission_denials": permission_denials,
        "duration_ms": 41230,
        "session_id": "fake-session-0001",
    }


def _write_payload():
    output_dir = os.environ.get("DEEP_REVIEW_OUTPUT_DIR")
    if not output_dir:
        return
    os.makedirs(output_dir, exist_ok=True)
    payload = {
        "platform": "github",
        "endpoint": "repos/o/r/pulls/5/reviews",
        "method": "POST",
        "payload": {
            "event": "COMMENT",
            "body": "deep-review (dry-run)",
            "comments": [
                {"path": "a.py", "line": 10, "body": "Possible null deref."},
                {"path": "a.py", "line": 22, "body": "Unbounded loop."},
                {"path": "b.py", "line": 5, "body": "Missing error check."},
            ],
        },
        "skipped": [],
    }
    with open(os.path.join(output_dir, "post-review-payload.json"), "w") as fh:
        json.dump(payload, fh)


def _record_pgid():
    pidfile = os.environ.get("FAKE_CLAUDE_PIDFILE")
    if pidfile:
        with open(pidfile, "w") as fh:
            fh.write(str(os.getpgrp()))


def _mutate_repo():
    """Write to a path inside the plugin repo — models a child self-healing the plugin
    mid-run (the contamination the invoke.py integrity guard must catch and reset).
    The target path is given by FAKE_CLAUDE_MUTATE_PATH."""
    path = os.environ.get("FAKE_CLAUDE_MUTATE_PATH")
    if not path:
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        fh.write("// self-healed by child mid-run\n")


def _record_argv():
    argv_file = os.environ.get("FAKE_CLAUDE_ARGV_FILE")
    if argv_file:
        with open(argv_file, "w") as fh:
            fh.write("\n".join(sys.argv[1:]))


def _write_report(lines):
    """Write a report .md carrying the echo block in its methodology section."""
    output_dir = os.environ.get("DEEP_REVIEW_OUTPUT_DIR")
    if not output_dir:
        return
    os.makedirs(output_dir, exist_ok=True)
    body = ["# Deep Review Report", "", "## Methodology", "", "```"] + lines + ["```", ""]
    with open(os.path.join(output_dir, "deep-review-report.md"), "w") as fh:
        fh.write("\n".join(body) + "\n")


def main():
    # A --version probe (the v3 preflight) prints only the version and exits, before any
    # mode handling -- so it never records a pgid, hangs, or emits a review envelope. The
    # default clears V3_MIN_CLAUDE_VERSION; FAKE_CLAUDE_VERSION can drive an older CLI.
    if "--version" in sys.argv:
        version = os.environ.get("FAKE_CLAUDE_VERSION", "2.1.154")
        sys.stdout.write("{} (Claude Code)\n".format(version))
        return

    _record_pgid()
    _record_argv()
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")

    if mode == "mutate_repo":
        # Write into the plugin repo, then otherwise behave like a normal 'ok' run so the
        # only anomaly is the dirty repo — exactly the self-healing-child contamination.
        _mutate_repo()
        mode = "ok"

    if mode == "hang":
        time.sleep(60)
        return

    partial_lines = [ln for ln in ECHO_LINES if "trivial_scope" not in ln]

    # Where the receipt lands: stdout lines, the envelope .result text, a report .md.
    stdout_lines = ECHO_LINES
    result_text = "Review complete. 3 findings posted (dry-run)."
    write_report = False

    if mode == "badecho":
        # A partial block (missing trivial_scope) in BOTH stdout and .result.
        stdout_lines = partial_lines
        result_text = "\n".join(partial_lines)
    elif mode == "echo_in_result":
        # Receipt only in the final message (.result), never in intermediate stdout.
        stdout_lines = []
        result_text = "\n".join(ECHO_LINES)
    elif mode == "echo_in_report":
        # Receipt only in the collected report markdown.
        stdout_lines = []
        write_report = True

    denials = []
    if mode == "asks":
        denials = [
            {
                "tool_name": "AskUserQuestion",
                "tool_use_id": "toolu_fake_ask",
                "reason": "interactive prompt blocked in headless run",
            }
        ]

    if stdout_lines:
        sys.stdout.write("\n".join(stdout_lines) + "\n")
    if mode != "asks":
        _write_payload()
    if write_report:
        _write_report(ECHO_LINES)
    sys.stdout.write(json.dumps(_envelope(denials, result_text)) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
