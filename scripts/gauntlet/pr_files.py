"""Fetch changed paths from Git or a forge with evidence of completeness."""

import argparse
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser
from gauntlet.forge import (
    Platform,
    ReviewTarget,
    gh_hostname,
    make_forge,
    parse_pr_url,
)
from gauntlet.git_diff import require_git_range


@dataclass(frozen=True, slots=True)
class FileListing:
    paths: tuple[str, ...]
    reason: str | None = None
    failed: bool = False

    @property
    def complete(self) -> bool:
        return self.reason is None


def fetch_pr_view(
    platform: Platform, number: int, *, capture_description: bool = False
) -> dict[str, object]:
    try:
        adapter = make_forge(platform)
        stdout, stderr, status = adapter.pr_view(
            number, capture_description=capture_description
        )
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
        stdout, stderr, status = make_forge(
            platform, gh_hostname(parsed.web_origin)
        ).file_names(target)
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


def list_git_files(revision_range: str) -> FileListing:
    try:
        result = proc.run_bytes(
            ["git", "diff", "--name-only", "-z", revision_range], timeout=180
        )
        if result.returncode:
            reason = (
                result.stderr.decode("utf-8", errors="replace").strip()
                or f"git exited {result.returncode}"
            )
            return FileListing((), f"file listing failed: {reason}", failed=True)
        # NUL framing preserves newlines and avoids Git's quoted filename format.
        paths = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        return FileListing(tuple(paths[:-1]))
    except (OSError, proc.TimeoutExpired) as exc:
        return FileListing((), f"file listing failed: {exc}", failed=True)


def _parser() -> Parser:
    parser = Parser(prog="list_pr_files")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--platform", choices=("github", "gitlab"))
    mode.add_argument("--git-range")
    parser.add_argument("--pr", type=int)
    return parser


def _handle(args: argparse.Namespace) -> int:
    if args.git_range is not None:
        if args.pr is not None:
            raise CliError("--pr cannot be used with --git-range", 2)
        require_git_range(args.git_range)
        listing = list_git_files(args.git_range)
    else:
        if args.pr is None:
            raise CliError("--pr is required with --platform", 2)
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
