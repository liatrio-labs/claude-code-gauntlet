"""Resolved command execution for shipped Python code."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess  # noqa: TID251
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Literal

CompletedProcess = subprocess.CompletedProcess
TimeoutExpired = subprocess.TimeoutExpired
CalledProcessError = subprocess.CalledProcessError


class ToolError(FileNotFoundError):
    """The requested executable is unavailable on PATH."""

    def __init__(self, command: Sequence[str]) -> None:
        super().__init__(errno.ENOENT, os.strerror(errno.ENOENT), command[0])


def which(name: str) -> str | None:
    if sys.platform != "win32":
        return shutil.which(name)
    # Windows may search the current directory before PATH, including a reviewed repo.
    # Search explicit PATH entries only so a repository executable cannot shadow a tool.
    extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
    names = [name] if os.path.splitext(name)[1] else [name + ext for ext in extensions]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for candidate in names:
            path = os.path.join(directory, candidate)
            if os.path.isfile(path):
                return path
    return None


def _resolve(argv: Sequence[str]) -> list[str]:
    if any(sep and sep in argv[0] for sep in (os.sep, os.altsep, "/")):
        return list(argv)
    resolved = which(argv[0])
    if resolved is None:
        raise ToolError(argv)
    return [resolved, *argv[1:]]


@contextmanager
def _original_spelling(argv: Sequence[str], resolved: str) -> Iterator[None]:
    """Report the command as the caller spelled it, not its resolved path."""
    try:
        yield
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        exc.cmd = list(argv)
        raise
    except OSError as exc:
        if exc.filename == resolved:
            exc.filename = argv[0]
        raise


def run(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    errors: Literal["strict", "replace"] = "strict",
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = _resolve(argv)
    with _original_spelling(argv, command[0]):
        return subprocess.run(
            command,
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            check=check,
            text=True,
            encoding="utf-8",
            errors=errors,
        )


def run_bytes(
    argv: Sequence[str], *, cwd: str | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess[bytes]:
    command = _resolve(argv)
    with _original_spelling(argv, command[0]):
        return subprocess.run(command, cwd=cwd, timeout=timeout, capture_output=True)


def output(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    errors: Literal["strict", "replace"] = "strict",
) -> tuple[str, str, int]:
    result = run(argv, cwd=cwd, timeout=timeout, errors=errors)
    return result.stdout, result.stderr, result.returncode
