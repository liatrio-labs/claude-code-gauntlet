#!/usr/bin/env python3
"""Generate docs/style/session-context.md from the wording and cadence rule sources.

Mirrors the shape of scripts/sync_agent_rules.py: two hand-maintained sources
(docs/style/wording-rules.md, docs/style/cadence-rules.md) carry the rules for a human
maintainer, and this script extracts every `RULE: ` line verbatim into one generated
carrier a SessionStart hook can inject whole. Editing the carrier directly is the mistake
this guards against: run this script instead.

Usage:
    python3 scripts/build_style_artifacts.py           # write the carrier
    python3 scripts/build_style_artifacts.py --check   # exit 1 if the carrier is stale
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WORDING_SOURCE = os.path.join("docs", "style", "wording-rules.md")
CADENCE_SOURCE = os.path.join("docs", "style", "cadence-rules.md")
CARRIER = os.path.join("docs", "style", "session-context.md")

BANNER = (
    "<!-- GENERATED from docs/style/wording-rules.md and docs/style/cadence-rules.md by "
    "scripts/build_style_artifacts.py -- do not edit. Edit the sources, then run: "
    "python3 scripts/build_style_artifacts.py -->"
)

RULE_PREFIX = "RULE: "


def extract_rules(text, source_name):
    """Every line starting `RULE: `, verbatim, skipping fenced code blocks."""
    rules = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith(RULE_PREFIX):
            rules.append(line[len(RULE_PREFIX) :])
    if not rules:
        raise ValueError(f"{source_name} yields zero RULE: lines")
    return rules


def read_source(repo_root, relpath):
    path = os.path.join(repo_root, relpath)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"missing style rule source: {relpath}")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def carrier_text(repo_root):
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


def sync(repo_root, check_only=False):
    expected = carrier_text(repo_root)
    target = os.path.join(repo_root, CARRIER)
    current = None
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as handle:
            current = handle.read()
    if current == expected:
        return False
    if not check_only:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(expected)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=REPO_ROOT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report a stale or missing carrier without writing",
    )
    args = parser.parse_args(argv)

    try:
        stale = sync(args.repo_root, check_only=args.check)
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    if not stale:
        print("style session-context carrier is current")
        return 0
    if args.check:
        sys.stderr.write(
            f"stale generated carrier: {os.path.relpath(os.path.join(args.repo_root, CARRIER), args.repo_root)}\n"
            "run: python3 scripts/build_style_artifacts.py\n"
        )
        return 1
    print(
        f"regenerated: {os.path.relpath(os.path.join(args.repo_root, CARRIER), args.repo_root)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
