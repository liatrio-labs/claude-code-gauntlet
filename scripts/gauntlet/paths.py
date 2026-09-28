"""Trusted paths for script entries and package implementations."""

import os

PLUGIN_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))


def entry(name: str) -> str:
    return os.path.join(PLUGIN_ROOT, "scripts", f"{name}.py")
