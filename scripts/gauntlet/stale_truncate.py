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

import json
import os
import sys

from gauntlet.await_workflow import glob_under
from gauntlet.cli import CliError, Command, Parser, require_head_sha

PROG = "stale_truncate"

DEFERRED = (
    "DEFERRED: previously reviewed at the current SHA -- truncation withheld "
    "until the Skip/Review-again answer is known (a Skip must preserve these files)"
)


def _truncate(output_dir: str, head_sha: str) -> int:
    pattern = f"code-gauntlet-*-{head_sha}.*"
    paths = glob_under(output_dir, pattern)
    for done, path in enumerate(paths):
        try:
            with open(path, "wb"):
                pass
        except OSError as exc:
            raise CliError(
                f"cannot truncate {os.path.basename(path)} ({exc.strerror or exc}); "
                f"{done} of {len(paths)} matching file(s) truncated before it",
                2,
            ) from exc
    print(f"truncated {len(paths)} file(s)")
    return 0


def _execute(args):
    require_head_sha(PROG, args.head_sha)
    if not os.path.isdir(args.output_dir):
        raise CliError("--output-dir must be an existing directory", 2)

    if args.unconditional:
        return _truncate(args.output_dir, args.head_sha)

    try:
        detector = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeError):
        raise CliError("stdin must contain a JSON object", 2) from None
    if not isinstance(detector, dict):
        raise CliError("stdin must contain a JSON object", 2)

    reviewed_at_current_head = (
        detector.get("previously_reviewed")
        and detector.get("sha_resolvable")
        and detector.get("last_reviewed_sha") == detector.get("head_sha")
    )
    if reviewed_at_current_head:
        print(DEFERRED)
        return 0
    return _truncate(args.output_dir, args.head_sha)


def main(argv: list[str] | None = None) -> int:
    code = CLI.invoke(sys.argv[1:] if argv is None else argv)
    if code:
        raise SystemExit(code)
    return code


parser = Parser(prog=PROG, description=__doc__)
parser.add_argument("--output-dir", required=True)
parser.add_argument("--head-sha", required=True)
parser.add_argument("--unconditional", action="store_true")
CLI = Command(parser=parser, main=_execute)
