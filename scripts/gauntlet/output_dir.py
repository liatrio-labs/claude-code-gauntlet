#!/usr/bin/env python3
"""Create the output directory only after establishing its ignore gate."""

from __future__ import annotations

import argparse
import os
import sys

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser
from gauntlet.fs import confined, read_text

DEFAULT_OUTPUT_DIR = ".code-gauntlet"
GLOB_META = set("*?[]\\!")


def git_run(cwd: str, *args: str) -> proc.CompletedProcess[str]:
    return proc.run(["git", *args], cwd=cwd)


def git_repo_root(cwd: str) -> str | None:
    result = git_run(cwd, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    return os.path.realpath(result.stdout.strip())


def git_exclude_path(cwd: str) -> str | None:
    result = git_run(cwd, "rev-parse", "--git-path", "info/exclude")
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    return os.path.realpath(os.path.join(cwd, raw))


def git_check_ignore(cwd: str, path: str) -> int:
    """Directory patterns only match directories; probe with a slash before mkdir."""
    probe = path if path.endswith(("/", os.sep)) else path + "/"
    return git_run(cwd, "check-ignore", "-q", "--", probe).returncode


def escape_gitignore_pattern_segment(segment: str) -> str:
    return "".join("\\" + ch if ch in GLOB_META else ch for ch in segment)


def anchored_exclude_pattern(repo_root: str, abs_dir: str) -> str:
    rel = os.path.relpath(abs_dir, repo_root)
    if rel == "." or not confined(abs_dir, repo_root):
        raise ValueError("path is not strictly inside the repo root")
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    escaped = "/".join(escape_gitignore_pattern_segment(p) for p in parts)
    return f"/{escaped}/"


def exclude_writable(exclude_path: str) -> bool:
    parent = os.path.dirname(exclude_path)
    if os.path.isfile(exclude_path):
        return os.access(exclude_path, os.W_OK)
    if os.path.isdir(parent):
        return os.access(parent, os.W_OK | os.X_OK)
    # Probe the git dir parent when info/ needs creation.
    grand = os.path.dirname(parent)
    return os.path.isdir(grand) and os.access(grand, os.W_OK | os.X_OK)


def append_exclude_pattern(exclude_path: str, pattern: str) -> None:
    parent = os.path.dirname(exclude_path)
    os.makedirs(parent, exist_ok=True)
    existing = ""
    if os.path.isfile(exclude_path):
        existing = read_text(exclude_path)
        if pattern in existing.splitlines():
            return
    with open(exclude_path, "a", encoding="utf-8", newline="") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(pattern + "\n")


def resolve_absolute(repo_root: str, raw: str) -> str:
    return os.path.realpath(os.path.join(repo_root, raw))


def build_parser() -> Parser:
    parser = Parser(
        prog="ensure_output_dir",
        description="Resolve and create the review output directory under an ignore gate.",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for git invocations (tests). Default: process cwd.",
    )
    return parser


def _gitignore_failure(abs_dir: str, detail: str) -> CliError:
    return CliError(
        f"cannot establish gitignore for {abs_dir} ({detail}); "
        "set CODE_GAUNTLET_OUTPUT_DIR to a path outside the repo and re-run."
    )


def _handle(args: argparse.Namespace) -> int:
    try:
        return _create(args)
    except OSError as exc:
        raise CliError(str(exc)) from exc


def _create(args: argparse.Namespace) -> int:
    cwd = os.path.realpath(args.cwd or os.getcwd())
    if "CODE_GAUNTLET_OUTPUT_DIR" in os.environ:
        raw = os.environ["CODE_GAUNTLET_OUTPUT_DIR"]
        if raw.strip() == "":
            raise CliError(
                "CODE_GAUNTLET_OUTPUT_DIR is empty or whitespace — "
                "set a non-empty path or unset the variable to use the default "
                f"({DEFAULT_OUTPUT_DIR}).",
                2,
            )
        raw = raw.strip()
    else:
        raw = DEFAULT_OUTPUT_DIR

    repo_root = git_repo_root(cwd)
    if repo_root is None:
        raise CliError("not a git repository (git rev-parse --show-toplevel failed)", 2)
    abs_dir = resolve_absolute(repo_root, raw)
    if abs_dir == repo_root:
        raise CliError(
            "output directory must not be the repo root — "
            "set CODE_GAUNTLET_OUTPUT_DIR to a subdirectory or an outside path",
            2,
        )

    if not confined(abs_dir, repo_root):
        disclosure = (
            f"outside-repo: skip exclude for {abs_dir} "
            "(artifacts are outside the working tree)"
        )
    else:
        # The ignore gate must finish before creating the output directory.
        ignore_rc = git_check_ignore(cwd, abs_dir)
        if ignore_rc == 128:
            raise CliError(f"git check-ignore failed (exit 128) for {abs_dir}", 2)
        if ignore_rc not in (0, 1):
            raise CliError(f"git check-ignore returned unexpected exit {ignore_rc}", 2)
        if ignore_rc == 1:
            exclude_path = git_exclude_path(cwd)
            if exclude_path is None:
                raise _gitignore_failure(
                    abs_dir,
                    "info/exclude unresolvable via `git rev-parse --git-path`, not otherwise ignored",
                )
            if not exclude_writable(exclude_path):
                raise _gitignore_failure(
                    abs_dir, "info/exclude unwritable, not otherwise ignored"
                )
            try:
                pattern = anchored_exclude_pattern(repo_root, abs_dir)
            except ValueError as exc:
                raise CliError(f"cannot derive exclude pattern: {exc}", 2) from exc
            try:
                append_exclude_pattern(exclude_path, pattern)
            except OSError as exc:
                raise _gitignore_failure(
                    abs_dir, f"failed to append to info/exclude: {exc}"
                ) from exc
            verify_rc = git_check_ignore(cwd, abs_dir)
            if verify_rc == 128:
                raise CliError(
                    f"git check-ignore failed (exit 128) after exclude append for {abs_dir}",
                    2,
                )
            if verify_rc != 0:
                raise _gitignore_failure(
                    abs_dir, f"appended {pattern!r} but check-ignore still fails"
                )
            disclosure = f"exclude: added {pattern} via {exclude_path}"
        else:
            disclosure = f"exclude: already-ignored {abs_dir}"

    try:
        os.makedirs(abs_dir, exist_ok=True)
    except OSError as exc:
        raise CliError(f"mkdir failed for {abs_dir}: {exc}") from exc
    sys.stderr.write(disclosure + "\n")
    sys.stdout.write(abs_dir + "\n")
    return 0


CLI = Command(parser=build_parser(), main=_handle)
