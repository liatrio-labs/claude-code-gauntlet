#!/usr/bin/env python3
"""Compare .github/labels.json against a repository's live labels.

The manifest is the reviewable source of truth for the label taxonomy, but GitHub
applies an issue-form label only if the label already exists in the repository and
drops it silently otherwise. A manifest nothing checks against the live repo is
therefore aspirational: this helper closes that gap in both directions.

    # print the `gh label create --force` commands the repo is missing
    python3 .github/labels_diff.py --commands --repo <owner>/<repo>

    # confirm the sync landed; exit 1 on any drift
    gh api "repos/<owner>/<repo>/labels" --paginate \\
      | python3 .github/labels_diff.py --live -

`--commands` with `--live` narrows the output to the labels that actually differ.
Standard library only, like every other Python in this repo.
"""

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent / "labels.json"
HEX_COLOR = re.compile(r"^[0-9a-f]{6}$")

# Exit codes: 0 in sync, 1 drift, 2 bad input or unusable manifest.
EXIT_BAD_INPUT = 2


def _die(message):
    print(message, file=sys.stderr)
    raise SystemExit(EXIT_BAD_INPUT)


def load_manifest(path=MANIFEST):
    """Labels from the checked-in taxonomy.

    A corrupt manifest must not be reported as drift: the caller distinguishes 1
    (drift) from 2 (bad input), and the workflow reads any non-zero from the drift step
    as drift.
    """
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        _die(f"cannot read the manifest: {error}")
    except json.JSONDecodeError as error:
        _die(f"{path} is not valid JSON: {error}")

    labels = document.get("labels") if isinstance(document, dict) else None
    if not isinstance(labels, list) or not labels:
        _die(f"{path} must be an object with a non-empty `labels` array")
    for entry in labels:
        if not isinstance(entry, dict):
            _die(f"{path}: every label must be an object, got {json.dumps(entry)[:200]}")
        missing = [key for key in ("name", "color", "description")
                   if not isinstance(entry.get(key), str)]
        if missing:
            _die(f"{path}: label {json.dumps(entry)[:120]} is missing string {missing}")
    return labels


def load_live(source):
    """Labels from a `gh api` response.

    `gh api --paginate` concatenates one JSON array per page rather than emitting a
    single document, and `--paginate --slurp` nests the pages inside one array. Both
    shapes are accepted, and nesting is flattened to any depth rather than the single
    level `--slurp` happens to produce today. Anything else — an error payload, a single object, a list of
    strings from `--jq` — is refused by name rather than crashing three frames later,
    because a failed API read is what a maintainer will actually hit.
    """
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as error:
        _die(f"cannot read the label response: {error}")
    if not text.strip():
        _die(f"no JSON in {'stdin' if source == '-' else source}: "
             "the `gh api` read produced nothing")

    decoder = json.JSONDecoder()
    entries, offset = [], 0
    while offset < len(text):
        if text[offset].isspace():
            offset += 1
            continue
        try:
            page, offset = decoder.raw_decode(text, offset)
        except json.JSONDecodeError as error:
            _die(f"could not parse the label response as JSON: {error}")
        if not isinstance(page, list):
            _die("expected a JSON array of labels from `gh api`, got "
                 f"{json.dumps(page)[:200]}")
        entries.extend(page)

    while entries and all(isinstance(entry, list) for entry in entries):
        entries = [entry for page in entries for entry in page]

    labels = []
    for entry in entries:
        if not isinstance(entry, dict) or "name" not in entry:
            _die(f"expected label objects with a name, got {json.dumps(entry)[:200]}")
        labels.append({
            "name": entry["name"],
            "color": (entry.get("color") or "").lower(),
            "description": entry.get("description") or "",
        })
    return labels


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


def command(label, repo=None):
    # `--force` updates an existing label in place instead of failing on conflict. It
    # never deletes, so a label absent from the manifest is left alone.
    argv = ["gh", "label", "create", shlex.quote(label["name"]),
            "--color", shlex.quote(label["color"]),
            "--description", shlex.quote(label["description"])]
    if repo:
        # Without this, `gh` infers the target from the working directory's git remote.
        argv += ["--repo", shlex.quote(repo)]
    return " ".join(argv + ["--force"])


def check_emittable(manifest):
    """Refuse to build a command line out of a value that would not survive one."""
    for label in manifest:
        for field in ("name", "description"):
            if label[field].startswith("-"):
                _die(f"{label['name']!r}: {field} starts with '-', which `gh` would read "
                     "as a flag")
        if not HEX_COLOR.match(label["color"]):
            _die(f"{label['name']!r}: color {label['color']!r} is not 6-digit lowercase hex")


def emit(labels, manifest, repo):
    """Print the sync commands for `labels`, refusing first if any value is unusable."""
    check_emittable(manifest)
    for label in labels:
        print(command(label, repo))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Compare .github/labels.json against a repository's live labels.")
    parser.add_argument("--live", metavar="PATH",
                        help="JSON array of the repository's labels, or - for stdin")
    parser.add_argument("--commands", action="store_true",
                        help="print gh label create commands instead of a drift report")
    parser.add_argument("--repo", metavar="OWNER/REPO",
                        help="pass --repo to the emitted gh commands")
    parser.add_argument("--manifest", metavar="PATH", default=MANIFEST,
                        help="taxonomy manifest to read (default: .github/labels.json)")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)

    if args.live is None:
        if not args.commands:
            parser.error("--live is required unless --commands is given")
        return emit(manifest, manifest, args.repo)

    missing, diverging, unmanaged = diff(manifest, load_live(args.live))

    if args.commands:
        return emit(missing + [label for label, _ in diverging], manifest, args.repo)

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
        print(f"\n{len(missing)} missing, {len(diverging)} diverging — run "
              "`python3 .github/labels_diff.py --commands --live <file> --repo <owner>/<repo>` "
              "from the repo root", file=sys.stderr)
        return 1
    print(f"in sync: {len(manifest)} labels match the manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
