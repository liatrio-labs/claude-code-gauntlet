"""Allowlisted interpreter commands for pinned Bash invocations."""

from typing import Literal

PythonCommand = Literal["python3", "python", "py -3"]
PYTHON_ARGV: dict[PythonCommand, tuple[str, ...]] = {
    "python3": ("python3",),
    "python": ("python",),
    "py -3": ("py", "-3"),
}
