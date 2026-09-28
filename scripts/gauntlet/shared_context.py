"""Write the shared agent context from paths and triage text supplied as data."""

import argparse
import os
import sys

from gauntlet.cli import CliError, Command, Parser, UsageError, require_head_sha


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
    try:
        require_head_sha(args.head_sha)
    except CliError as exc:
        raise UsageError(str(exc), exc.code) from exc
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
