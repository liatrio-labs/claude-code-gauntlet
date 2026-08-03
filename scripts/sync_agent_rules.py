#!/usr/bin/env python3
"""Generate each directory's CLAUDE.md from its AGENTS.md.

WHY THIS EXISTS — measured 2026-07-30, twice.

Claude Code loads a subdirectory's CLAUDE.md on demand when it reads a file in that
directory, which is exactly the path-scoped delivery we want. But the on-demand loader
injects the file's CONTENTS VERBATIM: it does not expand `@path` imports. A one-line
`workflows/CLAUDE.md` containing `@AGENTS.md` was observed loading as the literal string
`@AGENTS.md`, delivering no rules at all. The same was observed for `.claude/rules/*.md`.
Imports are a launch-time root-CLAUDE.md feature only.

So the directory rules have to be physically present in the file that gets injected, and
the only honest way to keep one source is to generate the copy and prove it is current.
That is the pattern this repo already runs for `workflows/pipeline.js` (`build.js` plus
`tests/test_bundle_fresh.py`); this is the same shape for prose.

AGENTS.md is canonical because it is the file Codex and Cursor read natively. CLAUDE.md is
the generated twin. Editing the twin is the mistake this guards: run this script instead.

The banner is an HTML comment deliberately — Claude Code strips block-level HTML comments
before injecting a memory file, so the marker costs zero context while still being visible
to a human opening the file, and to git.

Usage:
    python3 scripts/sync_agent_rules.py           # write the twins
    python3 scripts/sync_agent_rules.py --check   # exit 1 if any twin is stale
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BANNER = (
    "<!-- GENERATED from AGENTS.md by scripts/sync_agent_rules.py — do not edit.\n"
    "     Claude Code's on-demand loader injects this file verbatim and does NOT expand\n"
    "     @imports, so the rules must be physically present here. Edit AGENTS.md. -->\n"
)


def twin_text(agents_text):
    """The exact bytes the generated CLAUDE.md must contain."""
    return BANNER + "\n" + agents_text


def directories_with_rules(repo_root):
    """Every directory holding an AGENTS.md, excluding the repo root.

    The root pair is deliberately NOT generated: root CLAUDE.md is a real file with a
    Claude-only tail plus an `@AGENTS.md` import, which DOES expand at launch.
    """
    out = []
    for name in sorted(os.listdir(repo_root)):
        directory = os.path.join(repo_root, name)
        if not os.path.isdir(directory) or name.startswith("."):
            continue
        if os.path.isfile(os.path.join(directory, "AGENTS.md")):
            out.append(directory)
    return out


def sync(repo_root, check_only=False):
    stale = []
    for directory in directories_with_rules(repo_root):
        source = os.path.join(directory, "AGENTS.md")
        target = os.path.join(directory, "CLAUDE.md")
        with open(source, "r", encoding="utf-8") as handle:
            expected = twin_text(handle.read())
        current = None
        if os.path.isfile(target):
            with open(target, "r", encoding="utf-8") as handle:
                current = handle.read()
        if current == expected:
            continue
        stale.append(os.path.relpath(target, repo_root))
        if not check_only:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(expected)
    return stale


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=REPO_ROOT)
    parser.add_argument(
        "--check", action="store_true", help="report stale twins without writing"
    )
    args = parser.parse_args(argv)

    stale = sync(args.repo_root, check_only=args.check)
    if not stale:
        print("agent rule twins are current")
        return 0
    if args.check:
        sys.stderr.write(
            "stale generated twins: %s\nrun: python3 scripts/sync_agent_rules.py\n"
            % ", ".join(stale)
        )
        return 1
    print("regenerated: %s" % ", ".join(stale))
    return 0


if __name__ == "__main__":
    sys.exit(main())
