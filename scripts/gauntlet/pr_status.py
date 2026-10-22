"""Resolve validated forge head and URL fields without shell JSON tooling."""

import argparse
import json
import os
from typing import TypedDict, cast

from gauntlet.cli import CliError, Command, Parser
from gauntlet.forge import Platform
from gauntlet.fs import confined
from gauntlet.pr_files import fetch_pr_view
from gauntlet.pr_identity import PrIdentityWire, resolve


class DescriptionSnapshot(TypedDict):
    head_sha_full: str
    target: PrIdentityWire
    body: str


def snapshot_url(target: PrIdentityWire) -> str:
    route = "pull" if target["platform"] == "github" else "-/merge_requests"
    return f"{target['web_origin']}/{target['owner']}/{target['repo']}/{route}/{target['pr_number']}"


def load_snapshot(path: str, root: str) -> DescriptionSnapshot:
    try:
        if not confined(path, root):
            raise ValueError("unconfined snapshot")
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or set(data) != {
            "head_sha_full",
            "target",
            "body",
        }:
            raise ValueError("invalid snapshot shape")
        target = data["target"]
        if not isinstance(target, dict) or target.get("platform") not in (
            "github",
            "gitlab",
        ):
            raise ValueError("invalid target")
        canonical = resolve(
            target["platform"],
            snapshot_url(cast(PrIdentityWire, target)),
            data["head_sha_full"],
        )
        if (
            target != canonical
            or type(target["pr_number"]) is not int
            or not isinstance(data["body"], str)
        ):
            raise ValueError("invalid snapshot fields")
        data["body"].encode("utf-8")
        return cast(DescriptionSnapshot, data)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        CliError,
    ):
        raise CliError(
            "missing, malformed or unconfined description snapshot", 2
        ) from None


def _fetch(
    platform: Platform, number: int, capture: bool
) -> tuple[str, str, PrIdentityWire, str]:
    try:
        view = (
            fetch_pr_view(platform, number, capture_description=True)
            if capture
            else fetch_pr_view(platform, number)
        )
        sha = view.get("headRefOid" if platform == "github" else "sha")
        url = view.get("url" if platform == "github" else "web_url")
        if not isinstance(sha, str) or not isinstance(url, str):
            raise CliError("forge view must contain a head SHA and URL", 2)
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
            raise CliError("forge URL must not contain whitespace or controls", 2)
        target = resolve(platform, url, sha)
        body = ""
        if capture:
            if target["pr_number"] != number:
                raise CliError("description target does not match selected PR/MR", 2)
            raw_body = view["body" if platform == "github" else "description"]
            if raw_body is not None and not isinstance(raw_body, str):
                raise ValueError("invalid description")
            body = "" if raw_body is None else raw_body
            body.encode("utf-8")
        return sha, url, target, body
    except (CliError, KeyError, ValueError, UnicodeError):
        if capture:
            raise CliError("cannot capture validated PR/MR description", 2) from None
        raise


def _handle(args: argparse.Namespace) -> int:
    if args.pr <= 0:
        raise CliError("--pr must be positive", 2)
    path = args.snapshot_destination
    if path:
        path = os.path.join(args.output_dir, path)
        if not confined(path, args.output_dir):
            raise CliError("description snapshot must stay beneath output directory", 2)
        if os.path.lexists(path):
            data = load_snapshot(path, args.output_dir)
        else:
            sha, _, target, body = _fetch(args.platform, args.pr, True)
            data = DescriptionSnapshot(head_sha_full=sha, target=target, body=body)
            serialized = json.dumps(data, ensure_ascii=True) + "\n"
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "x", encoding="utf-8", newline="") as stream:
                    stream.write(serialized)
            except FileExistsError:
                data = load_snapshot(path, args.output_dir)
            except OSError:
                raise CliError("cannot create description snapshot", 2) from None
        if (
            data["target"]["platform"] != args.platform
            or data["target"]["pr_number"] != args.pr
        ):
            raise CliError("description snapshot target mismatch", 2)
        sha, url = data["head_sha_full"], snapshot_url(data["target"])
    else:
        sha, url, _, _ = _fetch(args.platform, args.pr, False)
    print(f"head_sha={sha}\npr_url={url}")
    return 0


parser = Parser(prog="pr_status", description=__doc__)
parser.add_argument("--platform", choices=("github", "gitlab"), required=True)
parser.add_argument("--pr", type=int, required=True)
parser.add_argument("--snapshot-destination")
parser.add_argument("--output-dir", default=".")
CLI = Command(parser=parser, main=_handle)
