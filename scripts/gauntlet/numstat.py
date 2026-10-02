"""Count textual changes in a saved unified diff.

Use ``walk_diff`` instead of ``git apply`` (which refuses valid diffs and can
leave a piped count silently at zero) or a bare ``+``/``-`` prefix test (which
misreads content such as ``--- `` and ``+++ `` as headers). Binary files are
reported separately so zero textual changes do not imply an empty diff.
"""

import argparse

from gauntlet.cli import CliError, Command, Parser
from gauntlet.diff import diff_counts


def _execute(args: argparse.Namespace) -> int:
    try:
        with open(args.patch, encoding="utf-8", errors="replace", newline="") as fh:
            patch = fh.read()
    except OSError:
        raise CliError("cannot read patch", 2) from None

    counts = diff_counts(patch)
    print(f"changed_lines={counts.added + counts.removed}")
    print(f"binary_files={counts.binary_files}")
    return 0


parser = Parser(prog="diff_numstat", description=__doc__)
parser.add_argument("patch", help="saved unified diff patch")
CLI = Command(parser=parser, main=_execute)
