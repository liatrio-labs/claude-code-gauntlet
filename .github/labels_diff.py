#!/usr/bin/env python3
"""Compare .github/labels.json against a repository's live labels.

The manifest is the reviewable source of truth for the label taxonomy, but GitHub
applies an issue-form label only if the label already exists in the repository and
drops it silently otherwise. A manifest nothing checks against the live repo is
therefore aspirational: this helper closes that gap in both directions.

    # print the `gh label create --force` commands the repo is missing
    python3 .github/labels_diff.py --commands

    # confirm the sync landed; exit 1 on any drift
    gh api "repos/<owner>/<repo>/labels" --paginate \\
      | python3 .github/labels_diff.py --live -

`--commands` with `--live` narrows the output to the labels that actually differ.
Standard library only, like every other Python in this repo.
"""

import argparse
import json
import shlex
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent / "labels.json"


def load_manifest(path=MANIFEST):
    return json.loads(path.read_text(encoding="utf-8"))["labels"]


def load_live(source):
    """Labels from a `gh api` response.

    `gh api --paginate` concatenates one JSON array per page rather than emitting a
    single document, and `--paginate --slurp` nests the pages inside one array. Both
    shapes are accepted: pages are decoded in sequence and flattened.
    """
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    entries, offset = [], 0
    while offset < len(text):
        if text[offset].isspace():
            offset += 1
            continue
        page, offset = decoder.raw_decode(text, offset)
        entries.extend(page)
    while entries and isinstance(entries[0], list):
        entries = [entry for page in entries for entry in page]
    return [
        {"name": entry["name"],
         "color": (entry.get("color") or "").lower(),
         "description": entry.get("description") or ""}
        for entry in entries
    ]


def diff(manifest, live):
    """(missing, diverging, unmanaged) — diverging entries are (label, live_entry)."""
    by_name = {label["name"]: label for label in live}
    missing = [label for label in manifest if label["name"] not in by_name]
    diverging = [
        (label, by_name[label["name"]])
        for label in manifest
        if label["name"] in by_name
        and (by_name[label["name"]]["color"] != label["color"]
             or by_name[label["name"]]["description"] != label["description"])
    ]
    managed = {label["name"] for label in manifest}
    unmanaged = sorted(label["name"] for label in live if label["name"] not in managed)
    return missing, diverging, unmanaged


def command(label):
    # `--force` updates an existing label in place instead of failing on conflict. It
    # never deletes, so a label absent from the manifest is left alone.
    return " ".join([
        "gh", "label", "create", shlex.quote(label["name"]),
        "--color", label["color"],
        "--description", shlex.quote(label["description"]),
        "--force",
    ])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", metavar="PATH",
                        help="JSON array of the repository's labels, or - for stdin")
    parser.add_argument("--commands", action="store_true",
                        help="print gh label create commands instead of a drift report")
    args = parser.parse_args(argv)

    manifest = load_manifest()
    for label in manifest:
        # A leading dash would be read as a flag by the emitted command line.
        if label["name"].startswith("-") or label["description"].startswith("-"):
            print(f"refusing to emit a command for {label['name']!r}: leading '-'", file=sys.stderr)
            return 2

    if args.live is None:
        if not args.commands:
            parser.error("--live is required unless --commands is given")
        for label in manifest:
            print(command(label))
        return 0

    missing, diverging, unmanaged = diff(manifest, load_live(args.live))

    if args.commands:
        for label in missing:
            print(command(label))
        for label, _ in diverging:
            print(command(label))
        return 0

    for label in missing:
        print(f"missing: {label['name']}")
    for label, live in diverging:
        print(f"diverging: {label['name']}")
        print(f"    live:     color={live['color']} description={live['description']!r}")
        print(f"    manifest: color={label['color']} description={label['description']!r}")
    for name in unmanaged:
        print(f"unmanaged (in the repo, absent from the manifest, left untouched): {name}")

    if missing or diverging:
        sys.stdout.flush()
        print(f"\n{len(missing)} missing, {len(diverging)} diverging — "
              "run `python3 .github/labels_diff.py --commands --live <file>` from the repo root",
              file=sys.stderr)
        return 1
    print(f"in sync: {len(manifest)} labels match the manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
