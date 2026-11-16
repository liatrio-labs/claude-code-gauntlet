"""Resolved command execution for shipped Python code."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess  # noqa: TID251
import sys
import tempfile
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Literal

CompletedProcess = subprocess.CompletedProcess
TimeoutExpired = subprocess.TimeoutExpired
CalledProcessError = subprocess.CalledProcessError

_DEFAULT_PATHEXT = [".COM", ".EXE", ".BAT", ".CMD"]


def which(name: str) -> str | None:
    if sys.platform != "win32":
        return shutil.which(name)
    # Windows may search the current directory before PATH, including a reviewed repo.
    # Search explicit PATH entries only so a repository executable cannot shadow a tool.
    pathext = os.environ.get("PATHEXT", "").split(";")
    extensions = [ext for ext in pathext if ext] or _DEFAULT_PATHEXT
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
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), argv[0])
    # Anchor relative PATH entries before the child's cwd changes their base.
    return [os.path.abspath(resolved), *argv[1:]]


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
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
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


@dataclass(frozen=True, slots=True)
class BoundedBytes:
    stdout: bytes
    returncode: int
    truncated: bool


def run_bounded_bytes(
    argv: Sequence[str],
    *,
    cwd: str,
    timeout: float,
    max_output: int,
    input: bytes = b"",
) -> BoundedBytes:
    """Cap capture while the child runs; the timer also covers blocked pipe reads."""
    if type(max_output) is not int or max_output < 0:
        raise ValueError("max_output must be a nonnegative integer")
    if timeout <= 0:
        raise TimeoutExpired(list(argv), timeout)
    command = _resolve(argv)
    with tempfile.TemporaryFile() as request:
        request.write(input)
        request.seek(0)
        with subprocess.Popen(
            command,
            cwd=cwd,
            stdin=request,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as child:
            expired = threading.Event()

            def stop() -> None:
                expired.set()
                with suppress(ProcessLookupError):
                    child.kill()

            timer = threading.Timer(max(0, timeout), stop)
            timer.daemon = True
            timer.start()
            try:
                assert child.stdout is not None
                content = child.stdout.read(max_output + 1)
                truncated = len(content) > max_output
                if truncated:
                    with suppress(ProcessLookupError):
                        child.kill()
                returncode = child.wait()
                if expired.is_set():
                    raise TimeoutExpired(list(argv), timeout)
                return BoundedBytes(content[:max_output], returncode, truncated)
            finally:
                timer.cancel()
