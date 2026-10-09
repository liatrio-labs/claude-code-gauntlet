"""Shared file writes, JSON reads, and path selection."""

from __future__ import annotations

import glob
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from typing import Literal


class JsonReadError(Exception):
    """A file read or JSON parse failure with its original cause."""

    def __init__(self, kind: Literal["read", "parse"], cause: Exception):
        self.kind = kind
        self.cause = cause
        super().__init__(str(cause))


def write_atomic(
    path: str | os.PathLike[str], text: str, *, create_parents: bool = False
) -> None:
    """Replace a text file only after its complete UTF-8 content is encoded."""
    directory = os.path.dirname(os.path.abspath(path))
    if create_parents:
        os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".code-gauntlet-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        # A 0600 replacement may be unreadable by a later step running as another user.
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(temporary, 0o666 & ~mask)
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def read_text(
    path: str | os.PathLike[str],
    *,
    errors: Literal["strict", "replace"] = "strict",
) -> str:
    with open(path, encoding="utf-8", errors=errors) as handle:
        return handle.read()


def read_json(
    path: str | os.PathLike[str],
    *,
    errors: Literal["strict", "replace"] = "strict",
    parse_constant: Callable[[str], object] | None = None,
) -> object:
    """Read a JSON file and distinguish I/O from invalid content."""
    try:
        content = read_text(path, errors=errors)
    except (OSError, UnicodeError) as exc:
        raise JsonReadError("read", exc) from exc
    try:
        return json.loads(content, parse_constant=parse_constant)
    except (ValueError, RecursionError) as exc:
        raise JsonReadError("parse", exc) from exc


def confined(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """Include the resolved root and descendants, excluding symlink escapes."""
    try:
        target = os.path.realpath(path)
        base = os.path.realpath(root)
        return target == base or target.startswith(base.rstrip(os.sep) + os.sep)
    except (OSError, ValueError):
        return False


def glob_under(root: str | os.PathLike[str], pattern: str) -> list[str]:
    """Keep the root literal; callers escape literal names inside the pattern."""
    try:
        return [os.path.join(root, name) for name in glob.glob(pattern, root_dir=root)]
    except OSError:
        return []
