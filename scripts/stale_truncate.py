#!/usr/bin/env python3
"""Truncate stale artifacts for a SHA without embedding paths in code.

The gate is conditional because a Skip at the already-reviewed current SHA must
preserve its artifacts. ``head_advanced`` cannot decide this: it is also false
for an unresolvable recorded SHA and rewritten history, which must truncate.
The four outcomes are: no prior review, unresolvable prior SHA, and a different
resolvable SHA all truncate; a resolvable prior review at this SHA defers until
the Skip/Review-again answer is known.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import NoReturn

from await_workflow import glob_under

DEFERRED = (
    "DEFERRED: previously reviewed at the current SHA -- truncation withheld "
    "until the Skip/Review-again answer is known (a Skip must preserve these files)"
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        detail = " ".join(message.splitlines())
        print(f"stale_truncate: {detail}", file=sys.stderr)
        raise SystemExit(2)


def _fail(message: str) -> int:
    print(f"stale_truncate: {message}", file=sys.stderr)
    return 2


def _truncate(output_dir: str, head_sha: str) -> int:
    pattern = f"code-gauntlet-*-{head_sha}.*"
    paths = glob_under(output_dir, pattern)
    try:
        for path in paths:
            with open(path, "wb"):
                pass
    except OSError:
        return _fail("cannot truncate a matching artifact")
    print(f"truncated {len(paths)} file(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--unconditional", action="store_true")
    args = parser.parse_args(argv)

    if re.fullmatch(r"[0-9a-f]{4,40}", args.head_sha) is None:
        return _fail("--head-sha must be 4 to 40 lowercase hexadecimal characters")
    if not os.path.isdir(args.output_dir):
        return _fail("--output-dir must be an existing directory")

    if args.unconditional:
        return _truncate(args.output_dir, args.head_sha)

    try:
        detector = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeError):
        return _fail("stdin must contain a JSON object")
    if not isinstance(detector, dict):
        return _fail("stdin must contain a JSON object")

    reviewed_at_current_head = (
        detector.get("previously_reviewed")
        and detector.get("sha_resolvable")
        and detector.get("last_reviewed_sha") == detector.get("head_sha")
    )
    if reviewed_at_current_head:
        print(DEFERRED)
        return 0
    return _truncate(args.output_dir, args.head_sha)


if __name__ == "__main__":
    from script_io import run_entrypoint

    run_entrypoint(main)
