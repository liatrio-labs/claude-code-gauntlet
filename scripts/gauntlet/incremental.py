"""Keep author-controlled changed paths out of shell text."""

import argparse
import json
import sys

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser, require_full_sha
from gauntlet.git_diff import DIFF_FLAGS


def _execute(args: argparse.Namespace) -> int:
    require_full_sha(args.since, "--since")
    try:
        with open(args.files, encoding="utf-8") as source:
            paths = json.load(source)
    except OSError:
        raise CliError("cannot read --files", 2) from None
    except (ValueError, UnicodeError, RecursionError):
        raise CliError("--files must contain a JSON array of path strings", 2) from None
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise CliError("--files must contain a JSON array of path strings", 2)
    if any(not p or "\0" in p for p in paths):
        raise CliError("--files paths must be non-empty and contain no NUL", 2)
    # No pathspecs would make Git include files outside the server list.
    if not paths:
        return 0
    try:
        result = proc.run_bytes(
            [
                "git",
                "--literal-pathspecs",
                "diff",
                *DIFF_FLAGS,
                f"{args.since}..HEAD",
                "--",
                *paths,
            ]
        )
    except OSError as exc:
        raise CliError(f"cannot run git diff: {exc.strerror or exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CliError(f"git diff failed (exit {result.returncode}): {detail}")
    sys.stdout.buffer.write(result.stdout)
    return 0


parser = Parser(prog="incremental_diff", description=__doc__)
parser.add_argument("--files", required=True)
parser.add_argument("--since", required=True)
CLI = Command(parser=parser, main=_execute)
