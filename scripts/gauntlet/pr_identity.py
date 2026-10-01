"""Resolve a PR/MR URL into the registry-owned delivery.prIdentity wire shape.
Field order and consumer checks are defined by PR_IDENTITY_FIELDS in registry.js.
"""

import argparse
import re
import sys
from typing import TypedDict

from gauntlet.cli import CliError, Command, Parser
from gauntlet.forge import Platform, parse_pr_url

SHA_FULL_RE = re.compile(r"^[0-9a-f]{40}$")


class _PrIdentityRequired(TypedDict):
    owner: str
    repo: str
    pr_number: int
    sha_full: str
    platform: Platform
    web_origin: str


class PrIdentityWire(_PrIdentityRequired, total=False):
    title: str


def resolve(
    platform: Platform, url: str, sha: str, title: str | None = None
) -> PrIdentityWire:
    if not SHA_FULL_RE.fullmatch(sha):
        raise CliError("sha must be a 40-character lowercase hex commit id", 2)
    try:
        parsed = parse_pr_url(platform, url)
    except ValueError as exc:
        raise CliError(str(exc), 2) from exc
    identity: PrIdentityWire = {
        "owner": parsed.owner,
        "repo": parsed.repo,
        "pr_number": parsed.number,
        "sha_full": sha,
        "platform": platform,
        "web_origin": parsed.web_origin,
    }
    if title is not None and title.strip():
        identity["title"] = title
    return identity


def _parser() -> Parser:
    parser = Parser(prog="resolve_pr_identity")
    parser.add_argument("--platform", choices=("github", "gitlab"), required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--title")
    return parser


def _handle(args: argparse.Namespace) -> tuple[PrIdentityWire, int]:
    return resolve(args.platform, args.url, args.sha, args.title), 0


def main(argv: list[str] | None = None) -> int:
    return CLI.invoke(sys.argv[1:] if argv is None else argv)


CLI = Command(parser=_parser(), main=_handle)
