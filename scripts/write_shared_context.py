#!/usr/bin/env python3
"""Write the shared agent context from paths and triage text supplied as data.

Paths belong in argv and artifact names are derived here so output-directory
characters cannot become Python source. The collector's rules file is always
read and an empty file is refused: its successful no-rules result is a message,
while empty means stale or failed collection.

Content bytes are rules (adding LF only if needed), a blank line and
``## Risk classification and AI-generated-code status``, CR/LF-trimmed stdin
plus LF, a blank line and ``## Diff`` with the opening untrusted-content tag,
the saved diff (adding LF only if needed), then the closing tag and LF. A single
large Read can return only part of a file without notice; the workflow has no
disk and needs the receipt's line count to plan exact Reads. It is newline-byte
count plus one only when content lacks a final LF, so an unterminated final line
counts unlike ``wc -l``. ``contextChars`` counts decoded code points with
replacement and is advisory; it only narrows the Read chunk size.
"""

from __future__ import annotations

import json
import os
import sys

from script_io import OneLineErrorParser, fail, require_head_sha

PROG = "write_shared_context"


def _read_input(path: str, label: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            content = fh.read()
    except OSError:
        fail(PROG, f"cannot read {label}")
    if not content:
        fail(PROG, f"{label} is empty")
    return content


def main(argv: list[str] | None = None) -> int:
    parser = OneLineErrorParser(prog=PROG, description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)

    require_head_sha(PROG, args.head_sha)
    if not os.path.isdir(args.output_dir):
        fail(PROG, "--output-dir must be an existing directory")

    rules_path = os.path.join(
        args.output_dir, f"code-gauntlet-project-rules-{args.head_sha}.md"
    )
    diff_path = os.path.join(
        args.output_dir, f"code-gauntlet-diff-{args.head_sha}.patch"
    )
    output_path = os.path.join(
        args.output_dir, f"code-gauntlet-context-{args.head_sha}.md"
    )
    rules = _read_input(rules_path, "project rules file")
    diff = _read_input(diff_path, "diff file")

    try:
        triage = sys.stdin.buffer.read()
    except OSError:
        fail(PROG, "cannot read stdin")
    if not triage.strip():
        fail(
            PROG,
            "stdin must contain risk classification and AI-generated-code status",
        )

    if not rules.endswith(b"\n"):
        rules += b"\n"
    if not diff.endswith(b"\n"):
        diff += b"\n"
    triage = triage.strip(b"\r\n")
    content = (
        rules
        + b"\n## Risk classification and AI-generated-code status\n\n"
        + triage
        + b"\n\n## Diff\n\n<untrusted-code-content>\n"
        + diff
        + b"</untrusted-code-content>\n"
    )

    try:
        with open(output_path, "wb") as fh:
            fh.write(content)
    except OSError as exc:
        fail(PROG, f"cannot write context file ({exc.strerror or exc})")

    lines = content.count(b"\n") + (0 if content.endswith(b"\n") else 1)
    chars = len(content.decode("utf-8", errors="replace"))
    print(json.dumps({"contextLines": lines, "contextChars": chars}))
    return 0


if __name__ == "__main__":
    from script_io import run_entrypoint

    run_entrypoint(main)
