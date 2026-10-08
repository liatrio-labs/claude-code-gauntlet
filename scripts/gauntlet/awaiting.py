"""Wait policy, retry markers, and delivery of background workflow results."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from collections.abc import Mapping
from typing import Literal, TypedDict

from gauntlet import tasks
from gauntlet.cli import Command, Parser, UsageError
from gauntlet.paths import entry
from gauntlet.registry import ARTIFACT_PATH_TEMPLATES

# Return before the Bash ceiling kills the process and prevents its receipt.
DEFAULT_TIMEOUT_SECONDS = 540
TIMEOUT_HEADROOM_SECONDS = 60
# Smaller waits are not useful even when the exported ceiling is tiny.
MIN_TIMEOUT_SECONDS = 30
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_POLL_INTERVAL = 2
# Persist is last, so an unresolved target can use fresh artifacts after this grace.
DEFAULT_ARTIFACTS_GRACE_SECONDS = 120

AwaitOutcome = Literal["pending", "timeout", "artifacts_only", "error"]


class ArtifactState(TypedDict):
    checked: bool
    complete: bool
    present: list[str]
    missing: list[str]


_MarkerFields = TypedDict(
    "_MarkerFields",
    {
        "await": AwaitOutcome,
        "attempt": int,
        "max_attempts": int,
        "waited_seconds": float,
        "target": str,
        "resolved_path": str | None,
        "file_bytes": int | None,
        "since_epoch": float,
        "artifacts": ArtifactState,
        "saw_ok_without_corroborator": bool,
        "scan_skipped": bool,
        "scan_stop_reason": str | None,
    },
)


class AwaitMarker(_MarkerFields, total=False):
    searched: list[str]
    next_command: str
    gap: str
    detail: str
    artifactPaths: dict[str, str]


_ErrorFields = TypedDict(
    "_ErrorFields",
    {
        "await": Literal["error"],
        "gap": str,
        "message": str,
    },
)


class AwaitError(_ErrorFields, total=False):
    target: str | None
    attempt: int
    max_attempts: int


# A malformed ceiling must not shorten the wait. Resolve it at each invocation.
def default_timeout_seconds(environ: Mapping[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    raw = environ.get("BASH_MAX_TIMEOUT_MS")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        ceiling = int(float(raw)) // 1000
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if ceiling <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return max(
        MIN_TIMEOUT_SECONDS,
        min(DEFAULT_TIMEOUT_SECONDS, ceiling - TIMEOUT_HEADROOM_SECONDS),
    )


# Replace entries with a different key so a byte-consuming caller cannot write hollow files.
# Materialize reads the original task file; every other terminal key stays untouched.
def elide_persist_return(
    terminal: Mapping[str, object], resolved_path: str | None
) -> Mapping[str, object]:
    if not isinstance(terminal, dict):
        return terminal
    payload = terminal.get("persistReturn")
    if not isinstance(payload, dict):
        return terminal
    entries = payload.get("entries")
    entries = entries if isinstance(entries, list) else []
    slim = dict((k, v) for (k, v) in payload.items() if k != "entries")
    slim["elided"] = True
    slim["resolvedPath"] = resolved_path
    slim["paths"] = [e.get("path") for e in entries if isinstance(e, dict)]
    out = dict(terminal)
    out["persistReturn"] = slim
    return out


# The freshness floor excludes earlier reviews; directories cannot count as artifacts.
# Artifacts are an independent signal when the harness output layout changes.
def artifacts_state(
    artifacts_dir: str | None, head_sha: str | None, since_epoch: float
) -> ArtifactState:
    state: ArtifactState = {
        "checked": False,
        "complete": False,
        "present": [],
        "missing": [],
    }
    if not artifacts_dir or not head_sha:
        return state
    state["checked"] = True
    paths = _artifact_paths(artifacts_dir, head_sha)
    for path in paths.values():
        try:
            stat = os.stat(path)
            fresh = (
                os.path.isfile(path)
                and stat.st_size > 0
                and stat.st_mtime >= since_epoch
            )
        except OSError:
            fresh = False
        name = os.path.basename(path)
        if fresh:
            state["present"].append(name)
        else:
            state["missing"].append(name)
    state["complete"] = not state["missing"]
    return state


def _artifact_paths(artifacts_dir: str, head_sha: str) -> dict[str, str]:
    return {
        key: os.path.join(artifacts_dir, template.format(sha=head_sha))
        for key, template in ARTIFACT_PATH_TEMPLATES.items()
    }


# Carry the original floor and resolved path; quote a literal Bash retry with target last behind -- to preserve leading dashes.
def build_next_command(
    args: argparse.Namespace, resolved_path: str | None, since_epoch: float
) -> str:
    parts = [
        "python3",
        shlex.quote(entry("await_workflow")),
        "--attempt",
        str(args.attempt + 1),
        "--max-attempts",
        str(args.max_attempts),
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--poll-interval",
        str(args.poll_interval),
        "--since-epoch",
        repr(float(since_epoch)),
    ]
    if args.artifacts_dir:
        parts += ["--artifacts-dir", shlex.quote(args.artifacts_dir)]
    if args.head_sha:
        parts += ["--head-sha", shlex.quote(args.head_sha)]
    if args.artifacts_dir or args.head_sha:
        parts += ["--artifacts-grace-seconds", str(args.artifacts_grace_seconds)]
    parts += ["--", shlex.quote(resolved_path or args.target)]
    return " ".join(parts)


# Failure can precede observation, so these receipts deliberately have fewer fields.
def _wait_error_payload(args: argparse.Namespace, message: str) -> AwaitError:
    return {
        "await": "error",
        "gap": "workflow-timeout",
        "message": message,
        "target": args.target,
        "attempt": args.attempt,
        "max_attempts": args.max_attempts,
    }


# Compact ASCII JSON retains NaN spelling and flushes before returning an outcome code.
def _emit(payload: Mapping[str, object]) -> None:
    try:
        line = json.dumps(payload, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        line = json.dumps(
            {
                "await": "error",
                "gap": "workflow-timeout",
                "message": f"result would not serialize: {exc}",
            },
            separators=(",", ":"),
        )
    try:
        print(line)
        sys.stdout.flush()
    except OSError:
        # Prevent a second broken-pipe error at interpreter shutdown; caller returns 4.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, sys.stdout.fileno())
            finally:
                # dup2 duplicates the descriptor; the original must still be closed.
                os.close(devnull)
        except OSError:
            # Best effort: preserve the original delivery error.
            pass
        raise


def build_marker(
    kind: AwaitOutcome,
    args: argparse.Namespace,
    observation: tasks.TaskObservation,
    artifacts: ArtifactState,
    since_epoch: float,
    started_at: float,
    saw_bare_ok: bool,
) -> AwaitMarker:
    marker: AwaitMarker = {
        "await": kind,
        "attempt": args.attempt,
        "max_attempts": args.max_attempts,
        "waited_seconds": round(time.time() - started_at, 1),
        "target": args.target,
        "resolved_path": observation.resolved_path,
        "file_bytes": observation.file_bytes,
        "since_epoch": since_epoch,
        "artifacts": artifacts,
        "saw_ok_without_corroborator": saw_bare_ok,
        # Disclose both whether scanning stopped and the bound that stopped it.
        "scan_skipped": observation.scan_stop_reason is not None,
        "scan_stop_reason": observation.scan_stop_reason,
    }
    if observation.resolved_path is None:
        marker["searched"] = list(observation.searched)
    return marker


def await_terminal(
    args: argparse.Namespace, roots: tasks.TaskRoots
) -> tuple[Mapping[str, object], int]:
    started_at = time.time()
    since_epoch = args.since_epoch if args.since_epoch is not None else started_at
    deadline = started_at + max(0, args.timeout_seconds)
    artifacts_complete_at = None
    saw_bare_ok = False

    while True:
        observation = tasks.observe(args.target, roots)
        saw_bare_ok = saw_bare_ok or observation.saw_bare_ok
        if observation.terminal is not None:
            # The Bash caller merges streams, so terminal delivery must stay silent on stderr.
            return elide_persist_return(
                observation.terminal, observation.resolved_path
            ), 0

        artifacts = artifacts_state(args.artifacts_dir, args.head_sha, since_epoch)
        now = time.time()
        if artifacts["complete"]:
            if artifacts_complete_at is None:
                artifacts_complete_at = now
            grace_elapsed = now - artifacts_complete_at >= args.artifacts_grace_seconds
            # Fresh artifacts are the only fallback for an unresolved target; a resolved file may still receive its terminal return, so it waits to the deadline.
            if (grace_elapsed and observation.resolved_path is None) or (
                args.attempt >= args.max_attempts and now >= deadline
            ):
                marker = build_marker(
                    "artifacts_only",
                    args,
                    observation,
                    artifacts,
                    since_epoch,
                    started_at,
                    saw_bare_ok,
                )
                marker["gap"] = "workflow-timeout"
                marker["detail"] = (
                    "every persisted artifact is present and fresh, but the "
                    "workflow's compact return was never observed; deliver from "
                    "the marker's artifactPaths and disclose the gap"
                )
                marker["artifactPaths"] = _artifact_paths(
                    args.artifacts_dir, args.head_sha
                )
                return marker, 5
        else:
            artifacts_complete_at = None

        if now >= deadline:
            break
        time.sleep(max(0, min(args.poll_interval, deadline - now)))

    if args.attempt < args.max_attempts:
        marker = build_marker(
            "pending",
            args,
            observation,
            artifacts,
            since_epoch,
            started_at,
            saw_bare_ok,
        )
        marker["next_command"] = build_next_command(
            args, observation.resolved_path, since_epoch
        )
        return marker, 3

    marker = build_marker(
        "timeout", args, observation, artifacts, since_epoch, started_at, saw_bare_ok
    )
    marker["gap"] = "workflow-timeout"
    marker["detail"] = (
        f"no terminal workflow result after {args.max_attempts} attempts; declare "
        "the gap and deliver whatever partial artifacts exist"
    )
    return marker, 4


def build_parser() -> Parser:
    parser = Parser(
        prog="await_workflow",
        description="Block until a backgrounded Workflow task has a terminal result.",
    )
    parser.add_argument(
        "target",
        metavar="TASK_ID_OR_PATH",
        help="The Task ID printed by the Workflow tool, or the task output file's "
        "path. Anything containing a path separator or ending in .output is "
        "used verbatim; anything else is resolved as a task id.",
    )
    parser.add_argument(
        "--attempt",
        type=int,
        default=1,
        metavar="N",
        help="Which attempt this is (default 1). Carried forward by next_command.",
    )
    parser.add_argument(
        "--max-attempts",
        dest="max_attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        metavar="M",
        help="Total attempts allowed before declaring the workflow-timeout gap "
        f"(default {DEFAULT_MAX_ATTEMPTS}).",
    )
    parser.add_argument(
        "--timeout-seconds",
        dest="timeout_seconds",
        type=float,
        default=None,
        metavar="S",
        help=f"Per-invocation wait. Defaults to {DEFAULT_TIMEOUT_SECONDS}, lowered "
        "automatically when BASH_MAX_TIMEOUT_MS is exported.",
    )
    parser.add_argument(
        "--poll-interval",
        dest="poll_interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        metavar="P",
        help=f"Seconds between checks (default {DEFAULT_POLL_INTERVAL}).",
    )
    parser.add_argument(
        "--artifacts-dir",
        dest="artifacts_dir",
        metavar="DIR",
        help="The review's output directory. With --head-sha, enables the "
        "secondary signal: the four persisted artifacts.",
    )
    parser.add_argument(
        "--head-sha",
        dest="head_sha",
        metavar="SHORT",
        help="head_sha_short, the artifact filename discriminator.",
    )
    parser.add_argument(
        "--artifacts-grace-seconds",
        dest="artifacts_grace_seconds",
        type=float,
        default=DEFAULT_ARTIFACTS_GRACE_SECONDS,
        metavar="G",
        help="How long to keep waiting for the compact return once every artifact "
        f"is present (default {DEFAULT_ARTIFACTS_GRACE_SECONDS}).",
    )
    parser.add_argument(
        "--since-epoch",
        dest="since_epoch",
        type=float,
        default=None,
        metavar="T",
        help="Artifact freshness floor, as a unix timestamp. Defaults to now; "
        "next_command carries the ORIGINAL value forward so an artifact "
        "written during attempt 1 still counts as fresh during attempt 4.",
    )
    return parser


def main(args: argparse.Namespace) -> int:
    if bool(args.artifacts_dir) != bool(args.head_sha):
        # One half silently disables the secondary signal; reject that typo.
        raise UsageError("--artifacts-dir and --head-sha must be given together", 2)
    if args.timeout_seconds is None:
        args.timeout_seconds = default_timeout_seconds()
    try:
        roots = tasks.roots_for_request(args.target)
        payload, code = await_terminal(args, roots)
    except KeyboardInterrupt:
        payload, code = _wait_error_payload(args, "interrupted"), 4
    except Exception as exc:  # noqa: BLE001 - every wait outcome needs a receipt
        payload, code = _wait_error_payload(args, f"{type(exc).__name__}: {exc}"), 4
    # One delivery site gives interrupted and successful waits the same pipe handling.
    try:
        _emit(payload)
    except OSError:
        return 4
    return code


CLI = Command(parser=build_parser(), main=main)
