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
disk and needs the receipt's line count to plan exact Reads. The content always
ends with LF, so the newline count equals the Read tool's line numbering.
``contextChars`` counts decoded code points with replacement and is advisory;
it only narrows the Read chunk size.
"""

import argparse
import os
import sys

from gauntlet.cli import Command, Parser, UsageError, require_head_sha


def _read_input(path: str, label: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            content = fh.read()
    except OSError:
        raise UsageError(f"cannot read {label}", 2) from None
    if not content:
        raise UsageError(f"{label} is empty", 2)
    return content


def _execute(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    require_head_sha(args.head_sha)
    if not os.path.isdir(args.output_dir):
        raise UsageError("--output-dir must be an existing directory", 2)

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

    if sys.stdin is None:
        raise UsageError("cannot read stdin", 2)
    try:
        triage = sys.stdin.buffer.read()
    except OSError:
        raise UsageError("cannot read stdin", 2) from None
    if not triage.strip():
        raise UsageError(
            "stdin must contain risk classification and AI-generated-code status",
            2,
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
        raise UsageError(
            f"cannot write context file ({exc.strerror or exc})", 2
        ) from exc

    # The content ends with LF, so the newline count is the Read tool's last line
    # number; the workflow has no disk and plans exact Reads from it.
    lines = content.count(b"\n")
    chars = len(content.decode("utf-8", errors="replace"))
    return {"contextLines": lines, "contextChars": chars}, 0


parser = Parser(prog="write_shared_context", description=__doc__)
parser.add_argument("--output-dir", required=True)
parser.add_argument("--head-sha", required=True)
CLI = Command(
    parser=parser,
    main=_execute,
    failure_receipt=lambda message: {"error": message},
)
