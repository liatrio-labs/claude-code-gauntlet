"""Resolve PR/MR eligibility before checkout or interactive questions."""

import argparse
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, TypedDict, cast

from gauntlet.cli import CliError, Command, Parser, read_stdin_json_object
from gauntlet.forge import Platform
from gauntlet.pr_files import FileListing, fetch_pr_view, list_pr_files

Verdict = Literal["proceed", "stop", "ask_draft"]
State = Literal["open", "closed", "merged"]
Mode = Literal["interactive", "headless"]
DraftPolicy = Literal["review", "skip"]
LOCKFILES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "Gemfile.lock",
        "poetry.lock",
        "composer.lock",
        "go.sum",
        "Pipfile.lock",
        "uv.lock",
        "bun.lockb",
        "bun.lock",
        "mix.lock",
        "pubspec.lock",
        "Podfile.lock",
        "packages.lock.json",
        "gradle.lockfile",
        "flake.lock",
        "npm-shrinkwrap.json",
    }
)


class EligibilityWire(TypedDict):
    verdict: Verdict
    reason: str | None
    state: State
    draft: bool


@dataclass(frozen=True, slots=True)
class Facts:
    state: State
    draft: bool
    view: dict[str, object]


def _facts(platform: Platform, number: int) -> Facts:
    payload = fetch_pr_view(platform, number)
    states: dict[str, State] = (
        {"OPEN": "open", "CLOSED": "closed", "MERGED": "merged"}
        if platform == "github"
        else {
            "opened": "open",
            "closed": "closed",
            "merged": "merged",
            "locked": "open",
        }
    )
    raw_state = payload.get("state")
    if not isinstance(raw_state, str) or raw_state not in states:
        raise CliError("FAILED: unrecognized PR/MR state")
    draft = (
        payload.get("isDraft")
        if platform == "github"
        else payload.get("draft", payload.get("work_in_progress"))
    )
    if not isinstance(draft, bool):
        raise CliError("FAILED: draft must be a boolean")
    return Facts(states[raw_state], draft, payload)


def _verdict(
    facts: Facts, files: FileListing, mode: Mode, policy: DraftPolicy
) -> tuple[Verdict, str | None]:
    if facts.state != "open" and mode == "interactive":
        return "stop", "closed"
    if files.complete:
        if not files.paths:
            return "stop", "no changes"
        if all(PurePosixPath(path).name in LOCKFILES for path in files.paths):
            return "stop", "lockfile-only"
    if facts.draft:
        if mode == "interactive":
            return "ask_draft", None
        if policy == "skip":
            return "stop", "draft_policy=skip"
    return "proceed", files.reason


def _parser() -> Parser:
    parser = Parser(prog="check_eligibility")
    parser.add_argument("--platform", choices=("github", "gitlab"), required=True)
    parser.add_argument("--pr", type=int, required=True)
    return parser


def _handle(args: argparse.Namespace) -> tuple[EligibilityWire, int]:
    payload = read_stdin_json_object()
    mode = payload.get("mode")
    if mode not in ("interactive", "headless"):
        raise CliError("stdin mode must be interactive or headless", 2)
    resolved = payload.get("resolved")
    if not isinstance(resolved, dict):
        raise CliError("stdin resolved must be an object", 2)
    policy = resolved.get("draft_policy", "review")
    if policy not in ("review", "skip") or (
        mode == "headless" and "draft_policy" not in resolved
    ):
        raise CliError("stdin resolved.draft_policy must be review or skip", 2)
    if args.pr <= 0:
        raise CliError("--pr must be positive", 2)
    facts = _facts(args.platform, args.pr)
    files = list_pr_files(args.platform, facts.view)
    verdict, reason = _verdict(facts, files, mode, cast(DraftPolicy, policy))
    return {
        "verdict": verdict,
        "reason": reason,
        "state": facts.state,
        "draft": facts.draft,
    }, 0


def main(argv: list[str] | None = None) -> int:
    return CLI.invoke(sys.argv[1:] if argv is None else argv)


CLI = Command(parser=_parser(), main=_handle)
