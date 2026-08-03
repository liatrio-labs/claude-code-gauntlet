#!/usr/bin/env python3
"""
validate_ndjson.py — Validate that a findings file is well-formed NDJSON.

Each non-empty line must parse as a complete JSON object on its own.
Reports any lines that fail to parse, with line numbers, the JSON parser's
error message, and a truncated snippet of the offending content.

Exit codes:
    0 — every non-empty line parses (or the file is missing / empty)
    1 — at least one line is invalid
    2 — usage error

Why this script exists
----------------------
Under v2, review agents emitted findings via ``printf '%s\\n' '<json>'``
because ``python3 -c`` and other dynamic-quoting forms are silently denied
by the subagent sandbox AST parser. That put the burden of escaping ``\\n``,
``\\r``, ``\\t``, apostrophes, etc. on the agent. When an agent embedded a
raw newline (or other control character) inside a JSON string value, the
single ``printf`` call wrote two physical lines and the merge pipeline saw
two malformed records instead of one valid finding. Agents ran this script
as their final action so the bug was caught while the agent was still in
scope and could re-emit the bad finding(s).

**v3 discovery agents no longer emit NDJSON** — they return findings by
value through their dispatch schema, and ``tests/test_agent_contracts.py``
scrubs the emission token from all seven contracts. This script is retained
as v2-compatibility and benchmark surface; nothing in the v3 review pipeline
invokes it.

Usage:
    python3 validate_ndjson.py <findings_file>
"""

import json
import os
import sys


# How much of an invalid line to echo back. Long enough to identify the
# finding, short enough to keep stderr readable when many lines fail.
_SNIPPET_MAX = 160


def _truncate(text):
    if len(text) <= _SNIPPET_MAX:
        return text
    return text[:_SNIPPET_MAX] + "..."


def validate(path):
    """Validate ``path`` as NDJSON. Print a report to stderr; return exit code."""
    if not os.path.exists(path):
        # No findings file means the agent emitted nothing — that's a valid
        # outcome (the agent is allowed to find no real issues).
        print(
            f"[validate_ndjson] {path}: file not found — 0 findings, OK.",
            file=sys.stderr,
        )
        return 0

    with open(path, "rb") as fh:
        raw = fh.read()

    if not raw.strip():
        print(
            f"[validate_ndjson] {path}: empty — 0 findings, OK.",
            file=sys.stderr,
        )
        return 0

    # Split on b"\n" specifically. Do NOT use splitlines() — it treats
    # b"\r" and other Unicode line separators as breaks too, which would
    # mask the very bug this script exists to detect (an embedded \r
    # inside a JSON string would look like a clean line split to
    # splitlines() but is invalid NDJSON).
    raw_lines = raw.split(b"\n")

    invalid = []
    valid_count = 0
    for line_no, raw_line in enumerate(raw_lines, start=1):
        # Trailing newline produces an empty final element. Treat any blank
        # line as a no-op rather than an error — agents may produce them
        # accidentally and they don't break the merge pipeline.
        if not raw_line.strip():
            continue

        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as e:
            invalid.append((line_no, "<non-UTF-8 bytes>", f"UnicodeDecodeError: {e}"))
            continue

        try:
            json.loads(line)
            valid_count += 1
        except json.JSONDecodeError as e:
            invalid.append(
                (line_no, _truncate(line), f"JSONDecodeError: {e.msg} at col {e.colno}")
            )

    if invalid:
        print(
            f"[validate_ndjson] {path}: {valid_count} valid, "
            f"{len(invalid)} invalid line(s).",
            file=sys.stderr,
        )
        for line_no, snippet, err in invalid:
            print(f"  line {line_no}: {err}", file=sys.stderr)
            print(f"    > {snippet}", file=sys.stderr)
        print(
            "Most common cause: a literal newline, tab, or carriage return inside "
            "a JSON string value. Replace each with the two-character escape "
            "sequence \\n / \\t / \\r so the finding stays on one line. "
            'Also escape: apostrophe -> \\u0027, double-quote -> \\", backslash '
            "-> \\\\. Re-emit the affected finding(s) and re-run this script.",
            file=sys.stderr,
        )
        return 1

    print(
        f"[validate_ndjson] {path}: {valid_count} valid finding(s).",
        file=sys.stderr,
    )
    return 0


def main(argv):
    if len(argv) != 2:
        print("Usage: validate_ndjson.py <findings_file>", file=sys.stderr)
        return 2
    return validate(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
