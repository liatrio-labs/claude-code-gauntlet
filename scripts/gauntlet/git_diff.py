"""Save a standard unified patch independent of user diff configuration."""

import argparse
import re
from pathlib import Path

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser

DIFF_FLAGS = ("--no-ext-diff", "--no-color", "--src-prefix=a/", "--dst-prefix=b/")


def require_git_range(revision_range: str) -> None:
    if re.fullmatch(r"HEAD|[0-9a-f]{40}\.\.\.[0-9a-f]{40}", revision_range) is None:
        raise CliError("--git-range must be HEAD or <base_sha>...<target_sha>", 2)


def _handle(args: argparse.Namespace) -> int:
    require_git_range(args.git_range)
    try:
        result = proc.run_bytes(
            ["git", "diff", *DIFF_FLAGS, args.git_range], timeout=180
        )
    except (OSError, proc.TimeoutExpired) as exc:
        raise CliError(f"cannot run git diff: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CliError(f"git diff failed (exit {result.returncode}): {detail}")
    if not result.stdout.startswith(b"diff --git"):
        raise CliError("patch is empty or does not start with diff --git")
    try:
        Path(args.output).write_bytes(result.stdout)
    except OSError as exc:
        raise CliError(f"cannot write patch: {exc}") from exc
    return 0


parser = Parser(prog="save_git_diff", description=__doc__)
parser.add_argument("--git-range", required=True)
parser.add_argument("--output", required=True)
CLI = Command(parser=parser, main=_handle)
