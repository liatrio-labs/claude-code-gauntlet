#!/usr/bin/env python3
"""Count textual changes in a saved unified diff.

Use ``walk_diff`` instead of ``git apply`` (which refuses valid diffs and can
leave a piped count silently at zero) or a bare ``+``/``-`` prefix test (which
misreads content such as ``--- `` and ``+++ `` as headers). Binary files are
reported separately so zero textual changes do not imply an empty diff.
"""

from __future__ import annotations

from diff_lines import walk_diff
from script_io import OneLineErrorParser, fail

PROG = "diff_numstat"


def main(argv: list[str] | None = None) -> int:
    parser = OneLineErrorParser(prog=PROG, description=__doc__)
    parser.add_argument("patch", help="saved unified diff patch")
    args = parser.parse_args(argv)

    try:
        with open(args.patch, encoding="utf-8", errors="replace", newline="") as fh:
            patch = fh.read()
    except OSError:
        fail(PROG, "cannot read patch")

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


if __name__ == "__main__":
    from script_io import run_entrypoint

    run_entrypoint(main)
