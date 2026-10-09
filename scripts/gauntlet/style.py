#!/usr/bin/env python3
"""Generate the style carrier; edit the rule sources instead of the carrier."""

from __future__ import annotations

import argparse
import os

from gauntlet.cli import CliError, Command, Parser
from gauntlet.fs import read_text
from gauntlet.generate import finish, sync_targets
from gauntlet.paths import ENTRY_ROOT

REPO_ROOT = ENTRY_ROOT

WORDING_SOURCE = os.path.join("docs", "style", "wording-rules.md")
CADENCE_SOURCE = os.path.join("docs", "style", "cadence-rules.md")
CARRIER = os.path.join("docs", "style", "session-context.md")

BANNER = (
    "<!-- GENERATED from docs/style/wording-rules.md and docs/style/cadence-rules.md by "
    "scripts/build_style_artifacts.py -- do not edit. Edit the sources, then run: "
    "python3 scripts/build_style_artifacts.py -->"
)

RULE_PREFIX = "RULE: "


def extract_rules(text: str, source_name: str) -> list[str]:
    """Every line starting `RULE: `, verbatim, skipping fenced code blocks.

    Each rule lives in its own `##` section, so a stray or missing fence delimiter that
    would silently swallow later rules is caught by per-section attribution: every
    section outside a fence must carry exactly one RULE: line, and a violation names both
    the source and the offending section heading. An aggregate count comparison would pass
    when one section has zero RULE: lines and another has two; this does not. Preamble
    text before the first `## ` heading is exempt and may carry zero rules. An unclosed
    fence at end of file is a separate hard error.
    """
    rules = []
    current_heading = None
    section_count = 0
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("## "):
            if current_heading is not None and section_count != 1:
                raise CliError(
                    f"{source_name} section {current_heading!r} has {section_count} "
                    "RULE: lines; all sections must carry exactly one"
                )
            current_heading = line
            section_count = 0
            continue
        if line.startswith(RULE_PREFIX):
            rules.append(line[len(RULE_PREFIX) :])
            section_count += 1
    if in_fence:
        raise CliError(f"unbalanced code fence in {source_name}")
    if current_heading is not None and section_count != 1:
        raise CliError(
            f"{source_name} section {current_heading!r} has {section_count} "
            "RULE: lines; all sections must carry exactly one"
        )
    if not rules:
        raise CliError(f"{source_name} yields zero RULE: lines")
    return rules


def read_source(repo_root: str, relpath: str) -> str:
    path = os.path.join(repo_root, relpath)
    if not os.path.isfile(path):
        raise CliError(f"missing style rule source: {relpath}")
    return read_text(path)


def carrier_text(repo_root: str) -> str:
    wording_rules = extract_rules(
        read_source(repo_root, WORDING_SOURCE), WORDING_SOURCE
    )
    cadence_rules = extract_rules(
        read_source(repo_root, CADENCE_SOURCE), CADENCE_SOURCE
    )

    lines = [
        BANNER,
        "",
        "# Session output style",
        "",
        "These rules govern Claude's session output in this repository.",
        "",
        "## Wording",
        "",
    ]
    lines.extend(f"- {rule}" for rule in wording_rules)
    lines.append("")
    lines.append("## Cadence")
    lines.append("")
    lines.extend(f"- {rule}" for rule in cadence_rules)
    lines.append("")
    return "\n".join(lines)


def main(args: argparse.Namespace) -> int:
    try:
        stale = sync_targets(
            args.repo_root, {CARRIER: carrier_text(args.repo_root)}, args.check
        )
    except (OSError, UnicodeError) as exc:
        raise CliError(str(exc)) from exc
    return finish(
        stale,
        args.check,
        current_message="style session-context carrier is current",
        stale_description="stale generated carrier",
        command="python3 scripts/build_style_artifacts.py",
    )


parser = Parser(
    prog="build_style_artifacts",
    description="Generate the style carrier from wording and cadence rule sources.",
)
parser.add_argument("--repo-root", default=REPO_ROOT)
parser.add_argument(
    "--check",
    action="store_true",
    help="report a stale or missing carrier without writing",
)
CLI = Command(parser=parser, main=main)
