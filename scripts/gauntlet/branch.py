"""Resolve a branch or revision read from stdin to a commit SHA, optionally checking it out."""

import argparse
import sys

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser, read_stdin_line, require_full_sha


def _git(*argv: str) -> proc.CompletedProcess[str]:
    try:
        return proc.run(["git", *argv], errors="replace")
    except OSError as exc:
        raise CliError(f"cannot run git: {exc.strerror or exc}") from exc


def _rev_parse(rev: str) -> str | None:
    result = _git("rev-parse", "--verify", "--quiet", "--end-of-options", rev)
    if result.returncode == 0:
        return result.stdout.rstrip("\n")
    if result.returncode != 1:
        raise CliError(f"git rev-parse failed (exit {result.returncode})")
    return None


def _resolve(name: str) -> tuple[str, str]:
    # Branches are looked up by full ref first so a same-named tag cannot shadow one;
    # any other revision (a tag, a SHA, HEAD~1) goes through git's own lookup.
    found = []
    if not _git("check-ref-format", f"refs/heads/{name}").returncode:
        for ref in (f"refs/heads/{name}", f"refs/remotes/{name}"):
            sha = _rev_parse(ref)
            if sha is not None:
                found.append((ref, sha))
    if len(found) > 1:
        raise CliError("name matches both a local and a remote-tracking branch")
    if found:
        return found[0]
    sha = _rev_parse(f"{name}^{{commit}}")
    if sha is None:
        raise CliError("name does not resolve to a commit")
    return "", sha


def _execute(args: argparse.Namespace) -> int:
    name = read_stdin_line()
    ref, sha = _resolve(name)
    require_full_sha(sha, "resolved branch")
    if args.checkout:
        # A local branch is checked out by name so new commits stay on it; one held
        # by another worktree cannot be, so it falls back to a detached HEAD.
        result = None
        if ref.startswith("refs/heads/"):
            result = _git("checkout", "--quiet", "--end-of-options", name, "--")
        if result is None or result.returncode:
            result = _git("checkout", "--quiet", "--detach", sha)
        if result.returncode:
            reason = (result.stderr.strip().splitlines() or [""])[0]
            raise CliError(f"git checkout failed (exit {result.returncode}): {reason}")
    sys.stdout.write(sha + "\n")
    return 0


_PARSER = Parser(prog="resolve_branch", description=__doc__)
_PARSER.add_argument(
    "--checkout", action="store_true", help="also check the branch out"
)
CLI = Command(parser=_PARSER, main=_execute)
