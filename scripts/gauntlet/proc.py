"""Resolved command execution for shipped Python code."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Literal

CompletedProcess = subprocess.CompletedProcess
TimeoutExpired = subprocess.TimeoutExpired
CalledProcessError = subprocess.CalledProcessError


class ToolError(FileNotFoundError):
    """The requested executable is unavailable on PATH."""

    def __init__(self, command: Sequence[str]) -> None:
        self.command = tuple(command)
        self.executable = command[0]
        self.reason = "missing_tool"
        super().__init__(errno.ENOENT, os.strerror(errno.ENOENT), self.executable)


def _resolve(argv: Sequence[str]) -> list[str]:
    resolved = shutil.which(argv[0])
    if resolved is None:
        raise ToolError(argv)
    return [resolved, *argv[1:]]


@contextmanager
def _original_spelling(argv: Sequence[str]) -> Iterator[None]:
    """Report the command as the caller spelled it, not its resolved path."""
    try:
        yield
    except subprocess.CalledProcessError as exc:
        exc.cmd = list(argv)
        raise
    except OSError as exc:
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
    with _original_spelling(argv):
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
    with _original_spelling(argv):
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
