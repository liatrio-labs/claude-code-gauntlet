"""Resolve validated forge head and URL fields without shell JSON tooling."""

import argparse

from gauntlet.cli import CliError, Command, Parser
from gauntlet.pr_files import fetch_pr_view
from gauntlet.pr_identity import resolve


def _handle(args: argparse.Namespace) -> int:
    if args.pr <= 0:
        raise CliError("--pr must be positive", 2)
    view = fetch_pr_view(args.platform, args.pr)
    sha = view.get("headRefOid" if args.platform == "github" else "sha")
    url = view.get("url" if args.platform == "github" else "web_url")
    if not isinstance(sha, str) or not isinstance(url, str):
        raise CliError("forge view must contain a head SHA and URL", 2)
    # Line framing must survive shell parameter expansion without eval.
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise CliError("forge URL must not contain whitespace or controls", 2)
    resolve(args.platform, url, sha)
    print(f"head_sha={sha}\npr_url={url}")
    return 0


parser = Parser(prog="pr_status", description=__doc__)
parser.add_argument("--platform", choices=("github", "gitlab"), required=True)
parser.add_argument("--pr", type=int, required=True)
CLI = Command(parser=parser, main=_handle)
