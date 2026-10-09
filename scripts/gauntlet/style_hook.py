#!/usr/bin/env python3
"""Emit SessionStart style context; an absent carrier leaves startup unblocked."""

import argparse
import json
import os

from gauntlet.cli import CliError, Command, Parser
from gauntlet.fs import read_text
from gauntlet.paths import ENTRY_ROOT

CARRIER = os.path.join(ENTRY_ROOT, "docs", "style", "session-context.md")


def strip_banner(text: str) -> str:
    """Drop the leading GENERATED banner: it instructs a maintainer, not the session."""
    lines = text.split("\n")
    if lines[0].startswith("<!--"):
        del lines[0]
        if lines and lines[0] == "":
            del lines[0]
    return "\n".join(lines)


def main(args: argparse.Namespace) -> int:
    if not os.path.isfile(CARRIER):
        return 0
    try:
        contents = read_text(CARRIER)
    except (OSError, UnicodeError) as exc:
        raise CliError(str(exc)) from exc
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": strip_banner(contents),
        }
    }
    print(json.dumps(payload))
    return 0


# A SessionStart hook must not fail on its argv: NUL cannot occur in an argument, so no
# token parses as an option and every one lands in the ignored positional.
parser = Parser(prog="emit_style_context", add_help=False, prefix_chars="\x00")
parser.add_argument("ignored", nargs="*")
CLI = Command(parser=parser, main=main)
