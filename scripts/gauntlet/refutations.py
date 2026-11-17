"""Resolve refutation quotes against captured source and the saved change."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser
from gauntlet.diff import HeaderEvent, LineEvent, patch_report_policy, walk_diff
from gauntlet.git_diff import DIFF_FLAGS
from gauntlet.location import SourceAdapter
from gauntlet.paths import is_source_evidence_path
from gauntlet.registry import ANCHOR_LIMITS


class CitationWire(TypedDict):
    file: str
    line_start: int
    line_end: int
    anchor: str


class CheckWire(TypedDict):
    verified: bool
    reason: str


# Comments cannot establish executable counter-evidence. Ambiguous spans fail closed.
_COMMENT = re.compile(
    r"/\*.*?(?:\*/|\Z)|<!--.*?(?:-->|\Z)|\"\"\".*?(?:\"\"\"|\Z)|'''.*?(?:'''|\Z)", re.S
)
_LINE_COMMENT = re.compile(r"(?:^|\s)(?://|\#|--|;|%).*$")


@dataclass(frozen=True, slots=True)
class ChangeFacts:
    added: frozenset[tuple[str, int]]


def change_facts(diff: str) -> ChangeFacts:
    if not diff.startswith("diff --git "):
        raise ValueError("unavailable_diff")
    prefixed = patch_report_policy(diff) == "git-prefixed"
    added = set()
    path = ""
    for event in walk_diff(diff, strict=True):
        if isinstance(event, HeaderEvent) and event.kind == "new_path":
            path = event.value.removeprefix("b/") if prefixed else event.value
        elif (
            isinstance(event, LineEvent)
            and event.new_line is not None
            and event.old_line is None
        ):
            added.add((path, event.new_line))
    return ChangeFacts(frozenset(added))


def check_citation(
    citation: CitationWire, source: SourceAdapter, facts: ChangeFacts
) -> CheckWire:
    def reject(reason: str) -> CheckWire:
        return {"verified": False, "reason": reason}

    if not isinstance(citation, dict):
        return reject("missing_citation")
    file = citation.get("file")
    start, end = citation.get("line_start"), citation.get("line_end")
    if (
        not isinstance(file, str)
        or type(start) is not int
        or type(end) is not int
        or not 1 <= start <= end <= start + ANCHOR_LIMITS["lines"] - 1
    ):
        return reject("invalid_span")
    lines = source.read(file)
    if lines is None or end > len(lines):
        return reject("unavailable_source")
    anchor = citation.get("anchor")
    if (
        not isinstance(anchor, str)
        or not anchor.strip()
        or len(anchor) > ANCHOR_LIMITS["chars"]
    ):
        return reject("quote_mismatch")
    quoted = anchor.replace("\r\n", "\n").split("\n")
    # A citation may name surrounding context; resolve its contiguous quote within that span.
    matches = [
        line
        for line in range(start - 1, end - len(quoted) + 1)
        if [part.strip() for part in lines[line : line + len(quoted)]]
        == [part.strip() for part in quoted]
    ]
    if len(matches) != 1:
        return reject("quote_mismatch")
    start, end = matches[0] + 1, matches[0] + len(quoted)
    if not is_source_evidence_path(file):
        return reject("non_source_evidence")
    # Requiring every cited line to predate the PR prevents padding a new guard with old context.
    if any((file, line) in facts.added for line in range(start, end + 1)):
        return reject("added_evidence")
    without_blocks = _COMMENT.sub(
        lambda m: "\n" * m.group().count("\n"), "\n".join(lines)
    )
    code = "\n".join(
        _LINE_COMMENT.sub("", line)
        for line in without_blocks.split("\n")[start - 1 : end]
    )
    if not re.search(r"[\w]", code):
        return reject("comment_only")
    return {"verified": True, "reason": "resolved_preexisting_code"}


def _handle(args: argparse.Namespace) -> int:
    try:
        request = json.loads(args.request)
        if not isinstance(request, dict) or not isinstance(
            request.get("citations"), list
        ):
            raise ValueError("missing citations")
    except ValueError as exc:
        raise CliError("--request must contain a citations array") from exc
    source = SourceAdapter.capture(args.repo_root, args.target_kind, args.head_sha)
    try:
        diff = args.diff_file.read_text(encoding="utf-8")
        if args.incremental:
            if not args.base_ref or not source.captured_head:
                raise ValueError("full_pr_diff_unavailable")
            base = proc.run_bytes(
                [
                    "git",
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    args.base_ref + "^{commit}",
                ],
                cwd=str(args.repo_root),
                timeout=5,
            )
            sha = base.stdout.decode("ascii").strip()
            if base.returncode or not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ValueError("full_pr_diff_unavailable")
            full = proc.run_bytes(
                ["git", "diff", *DIFF_FLAGS, sha + "..." + source.captured_head, "--"],
                cwd=str(args.repo_root),
                timeout=30,
            )
            if full.returncode:
                raise ValueError("full_pr_diff_unavailable")
            diff = full.stdout.decode("utf-8")
        facts = change_facts(diff)
        checks = [
            dict(id=row["id"], **check_citation(row.get("refutation"), source, facts))
            for row in request["citations"]
        ]
    except (OSError, ValueError, proc.TimeoutExpired):
        checks = [
            dict(
                id=row["id"],
                verified=False,
                reason="full_pr_diff_unavailable"
                if args.incremental
                else "unavailable_diff",
            )
            for row in request["citations"]
        ]
    print(json.dumps({"request": args.request, "checks": checks}))
    return 0


parser = Parser(prog="check_refutations", description=__doc__)
parser.add_argument("--request", required=True)
parser.add_argument("--repo-root", type=Path, required=True)
parser.add_argument("--head-sha", required=True)
parser.add_argument(
    "--target-kind", choices=("pr", "mr", "branch", "local"), required=True
)
parser.add_argument("--diff-file", type=Path, required=True)
parser.add_argument("--incremental", action="store_true")
parser.add_argument("--base-ref")
CLI = Command(parser=parser, main=_handle)
