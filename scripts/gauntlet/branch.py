"""Resolve a branch read from stdin to a commit SHA, optionally checking it out."""

import argparse
import sys

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser, read_stdin_line, require_full_sha


def _git(*argv: str) -> proc.CompletedProcess[str]:
    try:
        return proc.run(["git", *argv], errors="replace")
    except OSError as exc:
        raise CliError(f"cannot run git: {exc.strerror or exc}") from exc


def _resolve(name: str) -> tuple[str, str]:
    # Full ref names after check-ref-format refuses revision syntax, so a same-named
    # tag or a reflog suffix cannot redirect the lookup.
    if _git("check-ref-format", f"refs/heads/{name}").returncode:
        raise CliError("not a valid branch name")
    found = []
    for ref in (f"refs/heads/{name}", f"refs/remotes/{name}"):
        result = _git("rev-parse", "--verify", "--quiet", "--end-of-options", ref)
        if result.returncode == 0:
            found.append((ref, result.stdout.rstrip("\n")))
        elif result.returncode != 1:
            raise CliError(f"git rev-parse failed (exit {result.returncode})")
    if not found:
        raise CliError("no local or remote-tracking branch has that name")
    if len(found) > 1:
        raise CliError("name matches both a local and a remote-tracking branch")
    return found[0]


def _execute(args: argparse.Namespace) -> int:
    name = read_stdin_line()
    ref, sha = _resolve(name)
    require_full_sha(sha, "resolved branch")
    if args.checkout:
        # A local branch is checked out by name so new commits stay on it.
        argv = (
            ["checkout", "--quiet", "--end-of-options", name, "--"]
            if ref.startswith("refs/heads/")
            else ["checkout", "--quiet", "--detach", sha]
        )
        result = _git(*argv)
        if result.returncode:
            raise CliError(f"git checkout failed (exit {result.returncode})")
    sys.stdout.write(sha + "\n")
    return 0


_PARSER = Parser(prog="resolve_branch", description=__doc__)
_PARSER.add_argument(
    "--checkout", action="store_true", help="also check the branch out"
)
CLI = Command(parser=_PARSER, main=_execute)
