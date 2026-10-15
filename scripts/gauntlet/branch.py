"""Resolve a branch from stdin without interpreting it as shell text."""

import argparse
import sys

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser, read_stdin_line, require_full_sha


def _execute(args: argparse.Namespace) -> int:
    name = read_stdin_line()
    try:
        result = proc.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{name}^{{commit}}",
            ],
            errors="replace",
        )
    except OSError as exc:
        raise CliError(f"cannot run git rev-parse: {exc.strerror or exc}") from exc
    if result.returncode:
        raise CliError(
            f"branch does not resolve to a commit (git exit {result.returncode})"
        )
    sha = result.stdout.rstrip("\n")
    require_full_sha(sha, "resolved branch")
    sys.stdout.write(sha + "\n")
    return 0


CLI = Command(parser=Parser(prog="resolve_branch", description=__doc__), main=_execute)
