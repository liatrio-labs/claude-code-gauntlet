"""Trusted paths and checks for paths substituted into shell templates."""

import os

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
