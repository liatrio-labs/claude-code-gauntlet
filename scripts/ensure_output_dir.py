#!/usr/bin/env python3
"""
ensure_output_dir.py — Resolve, ignore-gate, and create the review output directory.

Usage:
    python3 ensure_output_dir.py [--cwd DIR]

Reads ``$CODE_GAUNTLET_OUTPUT_DIR`` (default ``.code-gauntlet``). Resolves an
absolute path under the reviewed repo (or a realpath'd override), establishes
ignore via ``.git/info/exclude`` when the path is in-repo, creates the directory
only after the ignore gate passes, and prints the absolute path on stdout.

Stdout carries the absolute path on success, or is empty on every non-zero exit.
Status, disclosures, and remedy lines go to stderr.

Exit codes:
    0  Absolute path on stdout; directory exists; in-repo path is ignored
       (pre-existing rule or exclude appended + verified) or out-of-repo skip
       disclosed.
    1  Hard stop: cannot establish ignore in-repo, or mkdir failed. Empty stdout.
       No mkdir on an ignore-gate failure (tree unchanged for that path).
    2  Usage: not a git repo, empty/whitespace env, abs == repo root,
       check-ignore error (exit 128), argparse. Empty stdout.

No external Python dependencies — stdlib only.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

DEFAULT_OUTPUT_DIR = ".code-gauntlet"
GLOB_META = set("*?[]\\!")


def git_run(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def git_repo_root(cwd: str) -> str | None:
    proc = git_run(cwd, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        return None
    return os.path.realpath(proc.stdout.strip())


def git_exclude_path(cwd: str) -> str | None:
    proc = git_run(cwd, "rev-parse", "--git-path", "info/exclude")
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    if os.path.isabs(raw):
        return os.path.realpath(raw)
    return os.path.realpath(os.path.join(cwd, raw))


def git_check_ignore(cwd: str, path: str) -> int:
    """Return check-ignore exit code: 0 ignored, 1 not ignored, 128 error.

    Directory patterns in gitignore (trailing ``/``) only match directories. A path
    that does not exist yet is not treated as a directory unless the probe itself
    ends with ``/``, so we always ask with a trailing slash — the output dir is
    always a directory by contract, and this keeps check-before-create honest.
    """
    probe = path if path.endswith(("/", os.sep)) else path + "/"
    return git_run(cwd, "check-ignore", "-q", "--", probe).returncode


def escape_gitignore_pattern_segment(segment: str) -> str:
    """Backslash-escape gitignore glob metacharacters in one path segment."""
    out: list[str] = []
    for ch in segment:
        if ch in GLOB_META:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def anchored_exclude_pattern(repo_root: str, abs_dir: str) -> str:
    """Build ``/rel/path/`` for info/exclude from repo-relative abs_dir."""
    rel = os.path.relpath(abs_dir, repo_root)
    if rel == "." or rel.startswith(".." + os.sep) or rel == "..":
        raise ValueError("path is not strictly inside the repo root")
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    escaped = "/".join(escape_gitignore_pattern_segment(p) for p in parts)
    return f"/{escaped}/"


def is_under_repo(repo_root: str, abs_path: str) -> bool:
    root = repo_root.rstrip(os.sep) + os.sep
    return abs_path == repo_root or abs_path.startswith(root)


def exclude_writable(exclude_path: str) -> bool:
    parent = os.path.dirname(exclude_path)
    if os.path.isfile(exclude_path):
        return os.access(exclude_path, os.W_OK)
    if os.path.isdir(parent):
        return os.access(parent, os.W_OK | os.X_OK)
    # Need to create info/ — probe the git dir parent.
    grand = os.path.dirname(parent)
    return os.path.isdir(grand) and os.access(grand, os.W_OK | os.X_OK)


def append_exclude_pattern(exclude_path: str, pattern: str) -> None:
    parent = os.path.dirname(exclude_path)
    os.makedirs(parent, exist_ok=True)
    existing = ""
    if os.path.isfile(exclude_path):
        with open(exclude_path, encoding="utf-8") as fh:
            existing = fh.read()
        if pattern in existing.splitlines():
            return
    with open(exclude_path, "a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(pattern + "\n")


def resolve_absolute(repo_root: str, raw: str) -> str:
    if os.path.isabs(raw):
        return os.path.realpath(raw)
    return os.path.realpath(os.path.join(repo_root, raw))


def run(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run the gate. Returns (exit_code, stdout, stderr)."""
    env = environ if environ is not None else os.environ
    parser = argparse.ArgumentParser(
        description="Resolve and create the review output directory under an ignore gate."
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for git invocations (tests). Default: process cwd.",
    )
    try:
        args = parser.parse_args([] if argv is None else argv)
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else 2
        return (code if code != 0 else 2, "", "")

    cwd = os.path.realpath(args.cwd or os.getcwd())
    stderr_parts: list[str] = []

    if "CODE_GAUNTLET_OUTPUT_DIR" in env:
        raw = env["CODE_GAUNTLET_OUTPUT_DIR"]
        if raw.strip() == "":
            msg = (
                "CODE_GAUNTLET_OUTPUT_DIR is empty or whitespace — "
                "set a non-empty path or unset the variable to use the default "
                f"({DEFAULT_OUTPUT_DIR})."
            )
            return 2, "", msg + "\n"
        raw = raw.strip()
    else:
        raw = DEFAULT_OUTPUT_DIR

    repo_root = git_repo_root(cwd)
    if repo_root is None:
        return 2, "", "not a git repository (git rev-parse --show-toplevel failed)\n"

    abs_dir = resolve_absolute(repo_root, raw)

    if abs_dir == repo_root:
        return (
            2,
            "",
            "output directory must not be the repo root — "
            "set CODE_GAUNTLET_OUTPUT_DIR to a subdirectory or an outside path\n",
        )

    if not is_under_repo(repo_root, abs_dir):
        stderr_parts.append(
            f"outside-repo: skip exclude for {abs_dir} "
            "(artifacts are outside the working tree)"
        )
        try:
            os.makedirs(abs_dir, exist_ok=True)
        except OSError as exc:
            return 1, "", f"mkdir failed for {abs_dir}: {exc}\n"
        return 0, abs_dir + "\n", "".join(line + "\n" for line in stderr_parts)

    # --- in-repo gate (before mkdir) ---
    ignore_rc = git_check_ignore(cwd, abs_dir)
    if ignore_rc == 128:
        return 2, "", f"git check-ignore failed (exit 128) for {abs_dir}\n"
    if ignore_rc not in (0, 1):
        return 2, "", f"git check-ignore returned unexpected exit {ignore_rc}\n"

    if ignore_rc == 1:
        exclude_path = git_exclude_path(cwd)
        if exclude_path is None:
            return (
                1,
                "",
                f"cannot establish gitignore for {abs_dir} "
                "(info/exclude unresolvable via `git rev-parse --git-path`, "
                "not otherwise ignored); "
                "set CODE_GAUNTLET_OUTPUT_DIR to a path outside the repo and re-run.\n",
            )
        if not exclude_writable(exclude_path):
            return (
                1,
                "",
                f"cannot establish gitignore for {abs_dir} "
                "(info/exclude unwritable, not otherwise ignored); "
                "set CODE_GAUNTLET_OUTPUT_DIR to a path outside the repo and re-run.\n",
            )
        try:
            pattern = anchored_exclude_pattern(repo_root, abs_dir)
        except ValueError as exc:
            return 2, "", f"cannot derive exclude pattern: {exc}\n"
        try:
            append_exclude_pattern(exclude_path, pattern)
        except OSError as exc:
            return (
                1,
                "",
                f"cannot establish gitignore for {abs_dir} "
                f"(failed to append to info/exclude: {exc}); "
                "set CODE_GAUNTLET_OUTPUT_DIR to a path outside the repo and re-run.\n",
            )
        verify_rc = git_check_ignore(cwd, abs_dir)
        if verify_rc == 128:
            return 2, "", f"git check-ignore failed (exit 128) after exclude append for {abs_dir}\n"
        if verify_rc != 0:
            return (
                1,
                "",
                f"cannot establish gitignore for {abs_dir} "
                f"(appended {pattern!r} but check-ignore still fails); "
                "set CODE_GAUNTLET_OUTPUT_DIR to a path outside the repo and re-run.\n",
            )
        stderr_parts.append(f"exclude: added {pattern} via {exclude_path}")
    else:
        stderr_parts.append(f"exclude: already-ignored {abs_dir}")

    try:
        os.makedirs(abs_dir, exist_ok=True)
    except OSError as exc:
        return 1, "", f"mkdir failed for {abs_dir}: {exc}\n"

    return 0, abs_dir + "\n", "".join(line + "\n" for line in stderr_parts)


def main(argv: list[str] | None = None) -> int:
    code, out, err = run(argv=sys.argv[1:] if argv is None else argv, environ=os.environ)
    if err:
        sys.stderr.write(err)
    if out:
        sys.stdout.write(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
