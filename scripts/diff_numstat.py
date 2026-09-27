#!/usr/bin/env python3
"""Count textual changes in a saved unified diff.

Use ``walk_diff`` instead of ``git apply`` (which refuses valid diffs and can
leave a piped count silently at zero) or a bare ``+``/``-`` prefix test (which
misreads content such as ``--- `` and ``+++ `` as headers). Binary files are
reported separately so zero textual changes do not imply an empty diff.
"""

from __future__ import annotations

import argparse
import sys
from typing import NoReturn

from diff_lines import walk_diff


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        detail = " ".join(message.splitlines())
        print(f"diff_numstat: {detail}", file=sys.stderr)
        raise SystemExit(2)


def _fail(message: str) -> int:
    print(f"diff_numstat: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("patch", help="saved unified diff patch")
    args = parser.parse_args(argv)

    try:
        with open(args.patch, encoding="utf-8", errors="replace", newline="") as fh:
            patch = fh.read()
    except OSError:
        return _fail("cannot read patch")

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
        line.startswith("Binary files ") and line.endswith(" differ")
        for line in patch.split("\n")
    )
    print(f"changed_lines={added + removed}")
    print(f"binary_files={binary_files}")
    return 0


if __name__ == "__main__":
    from script_io import run_entrypoint

    run_entrypoint(main)
