"""Trusted paths and checks for paths substituted into shell templates."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gauntlet.registry import PathRole

NON_SOURCE_DIRS = frozenset(
    {
        "vendor",
        "node_modules",
        "third_party",
        "third-party",
        "deps",
        "generated",
        "__generated__",
        "dist",
        "build",
        "out",
        "target",
        ".git",
        ".venv",
        "venv",
        ".tox",
        ".cache",
        "coverage",
    }
)
_TEST_DIRS = frozenset({"test", "tests", "spec", "specs", "__tests__"})
_BASENAME_RE = re.compile(r"^(?:test_.+|.+_(?:test|spec))$", re.IGNORECASE)
_SUFFIX_RE = re.compile(r"(?:Tests?|Spec)$")


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    if any(part.lower() in NON_SOURCE_DIRS for part in parts[:-1]):
        return False
    basename = parts[-1]
    stem = basename.split(".", 1)[0]
    return (
        any(part.lower() in _TEST_DIRS for part in parts[:-1])
        or bool(_BASENAME_RE.fullmatch(stem))
        or bool(re.search(r"\.(?:test|spec)\.", basename, re.IGNORECASE))
        or bool(_SUFFIX_RE.search(stem))
    )


def path_role(path: str, *, readable: bool) -> PathRole:
    if not readable:
        return "unknown"
    parts = path.split("/")
    if any(part.lower() in NON_SOURCE_DIRS for part in parts[:-1]):
        return "non_source"
    if is_test_path(path):
        return "test"
    # Resolved, validator-confirmed evidence proves a production element; paths cannot.
    return "source"


# Backslash is Windows' path separator; elsewhere it can escape inside double quotes.
SHELL_PATH_UNSAFE = frozenset(
    '$`"' + ("" if os.sep == "\\" else "\\") + "".join(map(chr, range(32))) + "\x7f"
)

PLUGIN_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENTRY_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def is_shell_safe_path(path: str) -> bool:
    """Double quotes preserve spaces and apostrophes, but not these characters."""
    return SHELL_PATH_UNSAFE.isdisjoint(path)


def entry(name: str) -> str:
    return os.path.join(ENTRY_ROOT, "scripts", f"{name}.py")


def wire_path(path: str, *, separator: str = os.sep) -> str:
    """Keep resolved host roots unambiguous at the workflow boundary."""
    if separator == "\\":
        if path.startswith(("\\\\", "//")):
            raise ValueError("UNC roots are unsupported; use a mapped drive")
        return path.replace(separator, "/")
    return path
