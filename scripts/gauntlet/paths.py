"""Trusted paths for script entries and package implementations."""

import os

PLUGIN_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENTRY_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def entry(name: str) -> str:
    return os.path.join(ENTRY_ROOT, "scripts", f"{name}.py")
