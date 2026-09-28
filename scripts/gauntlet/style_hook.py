#!/usr/bin/env python3
"""Print the SessionStart hook payload carrying docs/style/session-context.md.

Reads the generated style carrier (scripts/build_style_artifacts.py) and prints one JSON
object on stdout in the shape a Claude Code SessionStart hook expects. A broken or missing
carrier must never block session start, so a missing file exits 0 with empty stdout rather
than raising.

Usage:
    python3 scripts/emit_style_context.py
"""

import json
import os

from gauntlet.cli import Command
from gauntlet.paths import ENTRY_ROOT

REPO_ROOT = ENTRY_ROOT
CARRIER = os.path.join(REPO_ROOT, "docs", "style", "session-context.md")


def strip_banner(text):
    """Drop the leading GENERATED banner: it instructs a maintainer, not the session."""
    lines = text.split("\n")
    if lines and lines[0].startswith("<!--"):
        del lines[0]
        if lines and lines[0] == "":
            del lines[0]
    return "\n".join(lines)


def main(argv=None):
    if not os.path.isfile(CARRIER):
        return 0
    with open(CARRIER, encoding="utf-8") as handle:
        contents = handle.read()
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": strip_banner(contents),
        }
    }
    print(json.dumps(payload))
    return 0


CLI = Command.legacy(main, prog="emit_style_context.py")
