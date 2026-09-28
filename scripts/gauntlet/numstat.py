"""Count textual changes in a saved unified diff.

Binary files count separately so zero textual changes do not imply an empty diff.
"""

import argparse

from gauntlet.cli import CliError, Command, Parser
from gauntlet.diff import walk_diff


def _execute(args: argparse.Namespace) -> int:
    try:
        with open(args.patch, encoding="utf-8", errors="replace", newline="") as fh:
            patch = fh.read()
    except OSError:
        raise CliError("cannot read patch", 2) from None

    added = 0
    removed = 0
    for event in walk_diff(patch):
        if event.kind != "line":
            continue
        if event.new_line is not None and event.old_line is None:
            added += 1
        elif event.old_line is not None and event.new_line is None:
            removed += 1

    binary_files = sum(
        line.startswith("Binary files ") and line.rstrip("\r").endswith(" differ")
        for line in patch.split("\n")
    )
    print(f"changed_lines={added + removed}")
    print(f"binary_files={binary_files}")
    return 0


parser = Parser(prog="diff_numstat", description=__doc__)
parser.add_argument("patch", help="saved unified diff patch")
CLI = Command(parser=parser, main=_execute)
