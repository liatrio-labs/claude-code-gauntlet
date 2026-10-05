#!/usr/bin/env python3
"""Prepare a marketplace PR, check its entry, or remind maintainers weekly.

Usage:
    python3 .github/marketplace_bump.py open-pr --tag vX.Y.Z
    python3 .github/marketplace_bump.py check --tag vX.Y.Z
    python3 .github/marketplace_bump.py remind --repo <owner>/<repo>

The org marketplace is an internal repository. GitHub lets this public repository call
reusable workflows only from public ones, and its Actions token cannot read an internal
repository, so no workflow here can publish a release or compare versions. A maintainer
runs `open-pr` with their own `gh` login and a local release tag; `remind` is the only
part Actions can run.
"""

import argparse
import copy
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MARKETPLACE_REPO = "liatrio-labs/claude-plugins"
DEFAULT_SOURCE_REPO = "liatrio-labs/claude-code-gauntlet"
SOURCE_URL = f"https://github.com/{DEFAULT_SOURCE_REPO}.git"
MARKETPLACE_PATH = ".claude-plugin/marketplace.json"
MANIFEST_PATH = ".claude-plugin/plugin.json"
TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
ISSUE_TITLE_PATTERN = re.compile(r"^Marketplace bump due: (v[0-9]+\.[0-9]+\.[0-9]+)$")
REMINDER_SEARCH = 'in:title "Marketplace bump due" sort:created-desc'

CommandRunner = Callable[[list[str], str | None], str]
IssueState = Literal["OPEN", "CLOSED"]


class PluginManifest(TypedDict):
    version: str
    description: str
    keywords: list[str]


class MarketplaceSource(TypedDict, total=False):
    url: str
    sha: str


class MarketplaceEntry(TypedDict, total=False):
    source: MarketplaceSource
    version: str
    description: str
    keywords: list[str]


class MarketplaceDocument(TypedDict, total=False):
    plugins: list[MarketplaceEntry]


class ReminderIssue(TypedDict):
    number: int
    title: str
    state: IssueState


@dataclass(frozen=True, slots=True)
class CommandFailure(Exception):
    argv: tuple[str, ...]
    returncode: int
    stderr: str


class InputError(Exception):
    """An input or external response cannot safely describe a marketplace update."""


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError(message)


def run_command(argv: list[str], cwd: str | None = None) -> str:
    """Run the only subprocess boundary and return stdout as text."""
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except UnicodeDecodeError:
        raise InputError("command output is not valid UTF-8") from None
    except OSError as error:
        raise CommandFailure(tuple(argv), 127, str(error)) from None
    if result.returncode:
        raise CommandFailure(tuple(argv), result.returncode, result.stderr.strip())
    return result.stdout


def _json_object(text: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise InputError(f"{label} is not valid JSON: {error.msg}") from None
    if not isinstance(value, dict):
        raise InputError(f"{label} must be a JSON object")
    return value


def _load_manifest(text: str) -> PluginManifest:
    document = _json_object(text, "release manifest")
    missing = [
        field
        for field in ("version", "description", "keywords")
        if field not in document
    ]
    if missing:
        raise InputError(f"release manifest is missing fields: {', '.join(missing)}")
    if not isinstance(document["version"], str):
        raise InputError("release manifest version must be a string")
    if not isinstance(document["description"], str):
        raise InputError("release manifest description must be a string")
    if not isinstance(document["keywords"], list) or not all(
        isinstance(word, str) for word in document["keywords"]
    ):
        raise InputError("release manifest keywords must be an array of strings")
    return cast(PluginManifest, document)


def _validate_tag(tag: str) -> str:
    if not TAG_PATTERN.fullmatch(tag):
        raise InputError(f"malformed release tag: {tag!r}; expected vX.Y.Z")
    return tag


def _validate_repo(repo: str, label: str) -> str:
    if not REPO_PATTERN.fullmatch(repo):
        raise InputError(f"malformed {label}: {repo!r}; expected owner/repo")
    return repo


def _read_release(tag: str, run: CommandRunner) -> tuple[PluginManifest, str]:
    _validate_tag(tag)
    sha_text = run(["git", "rev-parse", f"{tag}^{{commit}}"], str(ROOT)).strip()
    if not SHA_PATTERN.fullmatch(sha_text):
        raise InputError(f"git returned a malformed commit sha for {tag}")
    published_sha = run(
        ["gh", "api", f"repos/{DEFAULT_SOURCE_REPO}/commits/{tag}", "--jq", ".sha"],
        None,
    ).strip()
    if sha_text.lower() != published_sha.lower():
        raise InputError(
            f"release {tag} sha mismatch: local {sha_text}, published {published_sha}"
        )
    manifest_text = run(["git", "show", f"{tag}:{MANIFEST_PATH}"], str(ROOT))
    manifest = _load_manifest(manifest_text)
    expected_version = tag[1:]
    if manifest["version"] != expected_version:
        raise InputError(
            f"release manifest version {manifest['version']!r} does not match tag {tag!r}"
        )
    return manifest, sha_text.lower()


def rewrite_entry(
    marketplace: MarketplaceDocument,
    manifest: PluginManifest,
    sha: str,
) -> MarketplaceDocument:
    """Copy the document and update its sole matching entry."""
    updated = copy.deepcopy(marketplace)
    entry = _find_entry(updated, SOURCE_URL)
    for field in ("version", "description", "keywords"):
        entry[field] = copy.deepcopy(manifest[field])
    entry["source"]["sha"] = sha
    return updated


def _find_entry(marketplace: MarketplaceDocument, source_url: str) -> MarketplaceEntry:
    plugins = marketplace["plugins"]
    matches = [
        entry
        for entry in plugins
        if isinstance(entry, dict)
        and isinstance(entry.get("source"), dict)
        and entry["source"].get("url") == source_url
    ]
    if len(matches) != 1:
        cardinality = "no" if not matches else "several"
        raise InputError(f"marketplace has {cardinality} entries for {source_url}")

    return matches[0]


def _require_document(text: str, label: str) -> MarketplaceDocument:
    document = _json_object(text, label)
    if not isinstance(document.get("plugins"), list):
        raise InputError(f"{label} must contain a plugins array")
    return cast(MarketplaceDocument, document)


def open_pr(tag: str, run: CommandRunner) -> int:
    manifest, sha = _read_release(tag, run)
    branch = f"bump-marketplace/claude-code-gauntlet-{tag}"
    source_repo = DEFAULT_SOURCE_REPO
    marketplace_repo = DEFAULT_MARKETPLACE_REPO
    title = f"fix(plugins): update {source_repo} to {tag}"
    release_url = f"https://github.com/{source_repo}/releases/tag/{tag}"
    body = f"Update the marketplace entry to upstream release [{tag}]({release_url})."

    with tempfile.TemporaryDirectory(prefix="marketplace-bump-") as temporary:
        run(["gh", "repo", "clone", marketplace_repo], temporary)
        clone = Path(temporary) / marketplace_repo.split("/", 1)[1]
        marketplace_path = clone / MARKETPLACE_PATH
        try:
            original = _require_document(
                marketplace_path.read_text(encoding="utf-8"), "marketplace file"
            )
        except (OSError, UnicodeDecodeError) as error:
            raise InputError(f"cannot read cloned marketplace file: {error}") from None
        updated = rewrite_entry(original, manifest, sha)
        if updated == original:
            print("The marketplace entry is already current.")
            return 0

        marketplace_path.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="",
        )
        cwd = str(clone)
        run(["git", "switch", "--create", branch], cwd)
        run(["git", "add", "--", MARKETPLACE_PATH], cwd)
        run(["git", "commit", "-m", title], cwd)
        run(
            ["git", "push", "--force-with-lease", "--set-upstream", "origin", branch],
            cwd,
        )
        pr_text = run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                marketplace_repo,
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "url",
            ],
            None,
        )
        try:
            prs = json.loads(pr_text)
        except json.JSONDecodeError:
            raise InputError("pull request list is not valid JSON") from None
        if not isinstance(prs, list) or any(
            not isinstance(pr, dict) or not isinstance(pr.get("url"), str) for pr in prs
        ):
            raise InputError("pull request list must contain objects with URLs")
        if prs:
            print(prs[0]["url"])
            return 0
        url = run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                marketplace_repo,
                "--base",
                "main",
                "--title",
                title,
                "--body",
                body,
            ],
            cwd,
        ).strip()
        if url:
            print(url)
    return 0


def check(tag: str, run: CommandRunner) -> int:
    manifest, sha = _read_release(tag, run)
    text = run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{DEFAULT_MARKETPLACE_REPO}/contents/{MARKETPLACE_PATH}",
        ],
        None,
    )
    marketplace = _require_document(text, "marketplace response")
    entry = _find_entry(marketplace, SOURCE_URL)
    expected = {
        "version": manifest["version"],
        "source.sha": sha,
        "description": manifest["description"],
        "keywords": manifest["keywords"],
    }
    actual = {
        "version": entry.get("version"),
        "source.sha": entry["source"].get("sha"),
        "description": entry.get("description"),
        "keywords": entry.get("keywords"),
    }
    drift = [field for field, value in expected.items() if actual[field] != value]
    if drift:
        for field in drift:
            print(f"drift: {field}")
        return 1
    print(f"in sync: marketplace entry matches {tag}")
    return 0


def _valid_issue(issue: object) -> ReminderIssue:
    if not isinstance(issue, dict):
        raise InputError("issue list must contain issue objects")
    number = issue.get("number")
    title = issue.get("title")
    state = issue.get("state")
    if isinstance(number, bool) or not isinstance(number, int):
        raise InputError("issue number must be an integer")
    if not isinstance(title, str) or not isinstance(state, str):
        raise InputError("issue title and state must be strings")
    if state.upper() not in ("OPEN", "CLOSED"):
        raise InputError("issue state must be OPEN or CLOSED")
    normalized = dict(issue)
    normalized["state"] = state.upper()
    return cast(ReminderIssue, normalized)


def remind(repo: str, run: CommandRunner) -> int:
    repo = _validate_repo(repo, "repository")
    try:
        release_text = run(
            ["gh", "release", "view", "--repo", repo, "--json", "tagName"], None
        )
    except CommandFailure as error:
        if re.search(r"no releases?|release not found", error.stderr, re.IGNORECASE):
            return 0
        raise
    release = _json_object(release_text, "latest release response")
    tag = release.get("tagName")
    if not isinstance(tag, str):
        raise InputError("latest release response is missing tagName")
    _validate_tag(tag)

    issue_text = run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--author",
            "app/github-actions",
            # Newest first with an explicit limit: gh ranks a search by best match
            # and returns 30, which would hide the newest reminder behind older ones.
            "--search",
            REMINDER_SEARCH,
            "--limit",
            "100",
            "--json",
            "number,title,state",
        ],
        None,
    )
    try:
        raw_issues = json.loads(issue_text)
    except json.JSONDecodeError as error:
        raise InputError(f"issue list is not valid JSON: {error.msg}") from None
    if not isinstance(raw_issues, list):
        raise InputError("issue list must be a JSON array")
    issues = [_valid_issue(issue) for issue in raw_issues]
    newest = max(
        (issue for issue in issues if ISSUE_TITLE_PATTERN.fullmatch(issue["title"])),
        key=lambda issue: issue["number"],
        default=None,
    )
    title = f"Marketplace bump due: {tag}"
    if newest is not None and newest["title"] == title:
        print(f"Marketplace reminder for {tag} is already current.")
        return 0

    body = (
        f"A release of claude-code-gauntlet is ready for the marketplace: `{tag}`.\n\n"
        "Open the marketplace PR:\n\n"
        f"`python3 .github/marketplace_bump.py open-pr --tag {tag}`\n\n"
        "Check the marketplace entry after it merges:\n\n"
        f"`python3 .github/marketplace_bump.py check --tag {tag}`\n\n"
        "Close this issue once the marketplace PR merges."
    )
    if newest is not None and newest["state"] == "OPEN":
        argv = ["gh", "issue", "edit", str(newest["number"])]
    else:
        argv = ["gh", "issue", "create"]
    output = run([*argv, "--repo", repo, "--title", title, "--body", body], None)
    if output.strip():
        print(output.strip())
    return 0


def _parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("open-pr", "check"):
        command = commands.add_parser(name)
        command.add_argument("--tag", required=True)
    reminder = commands.add_parser("remind")
    reminder.add_argument("--repo", required=True)
    return parser


def main(argv: list[str] | None = None, *, run: CommandRunner = run_command) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "remind":
            return remind(args.repo, run)
        operation = open_pr if args.command == "open-pr" else check
        return operation(args.tag, run)
    except InputError as error:
        print(str(error).splitlines()[0], file=sys.stderr)
        return 2
    except CommandFailure as error:
        command = " ".join(error.argv)
        lines = [line.strip() for line in error.stderr.splitlines() if line.strip()]
        detail = f": {lines[-1]}" if lines else ""
        print(
            f"command failed ({error.returncode}): {command}{detail}", file=sys.stderr
        )
        return 2
    except OSError as error:
        print(f"file operation failed: {str(error).splitlines()[0]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from gauntlet.cli import Command

    Command.legacy(main, prog="marketplace_bump.py").run()
