"""Fetch changed paths with forge-specific evidence of completeness."""

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser
from gauntlet.forge import GitHub, GitLab, Platform, ReviewTarget, parse_pr_url


@dataclass(frozen=True, slots=True)
class FileListing:
    paths: tuple[str, ...]
    reason: str | None = None
    failed: bool = False

    @property
    def complete(self) -> bool:
        return self.reason is None


def fetch_pr_view(platform: Platform, number: int) -> dict[str, object]:
    try:
        adapter = GitHub() if platform == "github" else GitLab()
        stdout, stderr, status = adapter.pr_view(number)
        if status:
            raise ValueError(stderr.strip() or f"forge exited {status}")
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            raise ValueError("view response must be an object")
        return payload
    except (OSError, proc.TimeoutExpired, ValueError) as exc:
        raise CliError(f"FAILED: {exc}") from exc


def _records(stdout: str) -> list[str]:
    # Both CLIs leave U+2028 raw inside a JSON string; splitlines() would cut there.
    return [line for line in stdout.split("\n") if line.strip()]


def list_pr_files(platform: Platform, view: Mapping[str, object]) -> FileListing:
    paths: list[str] = []
    try:
        url = view.get("url" if platform == "github" else "web_url")
        parsed = parse_pr_url(platform, url if isinstance(url, str) else "")
        target = ReviewTarget(parsed.owner, parsed.repo, parsed.number)
        if platform == "github":
            stdout, stderr, status = GitHub().file_names(
                target, urlsplit(parsed.web_origin).netloc
            )
        else:
            stdout, stderr, status = GitLab().file_names(target)
        if status:
            raise ValueError(stderr.strip() or f"forge exited {status}")
        if platform == "github":
            for line in _records(stdout):
                path = json.loads(line)
                if not isinstance(path, str) or not path:
                    raise ValueError("filename must be a non-empty string")
                paths.append(path)
            if len(paths) >= 3000:
                return FileListing(
                    tuple(paths), "file listing reached the 3000-file cap"
                )
        else:
            for line in _records(stdout):
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    raise ValueError("diff entry must be an object")
                path = entry.get(
                    "old_path" if entry.get("deleted_file") is True else "new_path"
                )
                if not isinstance(path, str) or not path:
                    raise ValueError("diff entry must have a path")
                paths.append(path)
            count = view.get("changes_count")
            if (
                not isinstance(count, str)
                or re.fullmatch(r"[0-9]+", count) is None
                or int(count) != len(paths)
            ):
                return FileListing(
                    tuple(paths),
                    "file listing incomplete: changes_count does not match paths",
                )
        return FileListing(tuple(paths))
    except (OSError, proc.TimeoutExpired, ValueError) as exc:
        return FileListing(tuple(paths), f"file listing failed: {exc}", failed=True)


def _parser() -> Parser:
    parser = Parser(prog="list_pr_files")
    parser.add_argument("--platform", choices=("github", "gitlab"), required=True)
    parser.add_argument("--pr", type=int, required=True)
    return parser


def _handle(args: argparse.Namespace) -> int:
    if args.pr <= 0:
        raise CliError("--pr must be positive", 2)
    listing = list_pr_files(args.platform, fetch_pr_view(args.platform, args.pr))
    if listing.failed:
        raise CliError(f"FAILED: {listing.reason}")
    print(json.dumps(listing.paths))
    if listing.reason:
        print(f"list_pr_files: {listing.reason}", file=sys.stderr)
    return 0


CLI = Command(parser=_parser(), main=_handle)
