"""Resolve validated forge head and URL fields without shell JSON tooling."""

import argparse
import json
import os
from typing import TypedDict, cast

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser, require_full_sha
from gauntlet.forge import Platform, parse_remote
from gauntlet.fs import confined
from gauntlet.pr_files import fetch_pr_view
from gauntlet.pr_identity import PrIdentityWire, resolve


class DescriptionSnapshot(TypedDict):
    head_sha_full: str
    base_sha_full: str
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
        required = DescriptionSnapshot.__required_keys__
        if not isinstance(data, dict) or set(data) != required:
            raise ValueError("invalid snapshot shape")
        target = data["target"]
        require_full_sha(data["base_sha_full"], "snapshot base")
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
    except (OSError, ValueError, TypeError, KeyError, RecursionError, CliError):
        raise CliError(
            "missing, malformed or unconfined description snapshot", 2
        ) from None


def _fetch(
    platform: Platform, number: int, capture: bool
) -> tuple[str, str, PrIdentityWire, str, str]:
    step = "validate forge view"
    try:
        view = fetch_pr_view(platform, number, capture_description=capture)
        sha = view.get("headRefOid" if platform == "github" else "sha")
        url = view.get("url" if platform == "github" else "web_url")
        if not isinstance(sha, str) or not isinstance(url, str):
            raise CliError("forge view must contain a head SHA and URL", 2)
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
            raise CliError("forge URL must not contain whitespace or controls", 2)
        target = resolve(platform, url, sha)
        if not capture:
            return sha, url, target, "", ""
        if target["pr_number"] != number:
            raise CliError("description target does not match selected PR/MR", 2)
        raw_body = view["body" if platform == "github" else "description"]
        if raw_body is not None and not isinstance(raw_body, str):
            raise ValueError("invalid description")
        body = "" if raw_body is None else raw_body
        body.encode("utf-8")
        step = "validate base ref"
        ref = view["baseRefName" if platform == "github" else "target_branch"]
        if not isinstance(ref, str):
            raise ValueError("invalid base ref")
        repository = f"{target['web_origin']}/{target['owner']}/{target['repo']}.git"
        namespace = "pull" if platform == "github" else "merge-requests"
        head_ref = f"refs/{namespace}/{number}/head"
        _git("check-ref-format", f"refs/heads/{ref}")
        step = "fetch head"
        _fetch_ref(repository, head_ref)
        if _git("rev-parse", "FETCH_HEAD") != sha:
            step = "head moved"
            raise ValueError(step)
        step = "fetch base"
        _fetch_ref(repository, f"refs/heads/{ref}")
        step = "merge-base"
        base = _git("merge-base", sha, "FETCH_HEAD")
        require_full_sha(base, "comparison base")
        return sha, url, target, body, base
    except (CliError, KeyError, ValueError, OSError, proc.TimeoutExpired):
        if capture:
            raise CliError(
                f"cannot capture validated PR/MR description: {step}", 2
            ) from None
        raise


def _fetch_ref(repository: str, ref: str) -> str:
    expected = parse_remote(repository)
    try:
        for row in _git("config", "--get-regexp", r"^remote\..*\.url$").splitlines():
            key, url = row.split(None, 1)
            remote = parse_remote(url)
            if not remote or not expected:
                continue
            if (remote.hostname, remote.path) == (expected.hostname, expected.path):
                return _git("fetch", "--no-tags", "--", key[7:-4], ref)
    except (ValueError, OSError, proc.TimeoutExpired):
        pass
    return _git("fetch", "--no-tags", "--", repository, ref)


def _git(*args: str) -> str:
    result = proc.run_bytes(["git", *args], timeout=180)
    if result.returncode:
        raise ValueError("comparison base unavailable")
    return result.stdout.decode("ascii").strip()


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
            sha, _, target, body, base = _fetch(args.platform, args.pr, True)
            data = DescriptionSnapshot(
                head_sha_full=sha, base_sha_full=base, target=target, body=body
            )
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "x", encoding="utf-8", newline="") as stream:
                    stream.write(json.dumps(data, ensure_ascii=True) + "\n")
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
        print(f"base_sha_full={data['base_sha_full']}")
    else:
        sha, url, _, _, _ = _fetch(args.platform, args.pr, False)
    print(f"head_sha={sha}\npr_url={url}")
    return 0


parser = Parser(prog="pr_status", description=__doc__)
parser.add_argument("--platform", choices=("github", "gitlab"), required=True)
parser.add_argument("--pr", type=int, required=True)
parser.add_argument("--snapshot-destination")
parser.add_argument("--output-dir", default=".")
CLI = Command(parser=parser, main=_handle)
