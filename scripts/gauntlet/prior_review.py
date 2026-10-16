#!/usr/bin/env python3
"""Detect prior summary reviews; marker.version never selects a reader.
Network entries must belong to the posting identity; offline files are trusted.
Recoverable identity/fetch/Git errors stay in one ASCII receipt with exit zero.
"""

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

from gauntlet import forge as forge_api
from gauntlet import proc
from gauntlet.cli import Command, Parser
from gauntlet.forge import Forge, Platform, ReviewTarget, make_forge
from gauntlet.fs import JsonReadError, read_json
from gauntlet.marker import detect_signal, find_finding_markers, select_latest


class ReviewEntryWire(TypedDict):
    body: str
    timestamp: str | None
    source: str
    id: object


class PriorReviewWire(TypedDict):
    previously_reviewed: bool
    signal: Literal["marker", "footer"] | None
    source: str | None
    legacy: bool
    last_reviewed_sha: str | None
    last_reviewed_sha_short: str | None
    sha_resolvable: bool
    sha_is_ancestor: bool
    head_sha: str | None
    head_advanced: bool
    new_commit_count: int | None
    incremental_safe: bool
    marker: Mapping[str, object] | None
    scanned: dict[str, int]
    errors: list[str]
    ignored_foreign: int


@dataclass(frozen=True, slots=True)
class PriorDelivery:
    summary_posted: bool
    finding_keys: frozenset[str]
    legacy_group_keys: frozenset[str]
    error: str | None


@dataclass(frozen=True, slots=True)
class GitFacts:
    head_sha: str | None
    last_reviewed_sha: str | None
    last_reviewed_sha_short: str | None
    sha_resolvable: bool
    sha_is_ancestor: bool
    new_commit_count: int | None


_NO_GIT_FACTS = GitFacts(None, None, None, False, False, None)

GIT_TIMEOUT_SECONDS = 10

# Keep receipt keys stable when a fetch fails or returns nothing.
PLATFORM_SOURCES: dict[Platform, tuple[str, ...]] = {
    "github": ("review",),
    "gitlab": ("note",),
}


def run(cmd: Sequence[str], timeout: float | None = None) -> tuple[str, str, int]:
    """Return a failure sentinel when a fetch cannot run."""
    try:
        # Strict decoding raises UnicodeDecodeError outside the OSError exit-zero path.
        return proc.output(cmd, errors="replace", timeout=timeout)
    except proc.TimeoutExpired:
        return "", f"timed out after {timeout}s", -1
    except OSError as exc:
        return "", str(exc), -1


def git_rev_parse(rev: str) -> str | None:
    """Return the full object id for *rev*, or None."""
    stdout, _, rc = run(["git", "rev-parse", rev], timeout=GIT_TIMEOUT_SECONDS)
    value = stdout.strip()
    return value if rc == 0 and value else None


def remote_slug() -> tuple[str | None, str | None]:
    """Keep replacement decoding and exit-zero degradation for origin lookup."""
    try:
        remote = forge_api.origin_remote(timeout=GIT_TIMEOUT_SECONDS, errors="replace")
    except (OSError, proc.TimeoutExpired):
        return None, None
    slug = forge_api.remote_slug(remote)
    return (slug.owner, slug.repo) if slug else (None, None)


def fetch_entries(
    owner: str, repo: str, number: int | str, *, forge: Forge
) -> tuple[list[ReviewEntryWire], list[str], int]:
    """Trust only the posting account on the surfaces the tool writes."""
    fetched = forge.review_entries(ReviewTarget(owner, repo, number))
    errors = [fetched.error] if fetched.error else []
    poster = forge.poster()
    if poster.error:
        errors.append(poster.error)
    payload = fetched.payload if isinstance(fetched.payload, list) else []
    own: list[object] = []
    ignored = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        author = item.get("user" if forge.platform == "github" else "author")
        login = (
            author.get("login" if forge.platform == "github" else "username")
            if isinstance(author, dict)
            else None
        )
        trusted = False
        if isinstance(login, str) and poster.login is not None:
            trusted = (
                login.casefold() == poster.login.casefold()
                if forge.platform == "github"
                else login == poster.login
            )
        if not trusted or (forge.platform == "gitlab" and item.get("system") is True):
            ignored += 1
            continue
        own.append(item)
    collector = (
        collect_entries_github if forge.platform == "github" else collect_entries_gitlab
    )
    return collector(own), errors, ignored


def gitlab_prior_delivery_state(
    owner: str, repo: str, number: int | str, sha: object, *, forge: Forge
) -> PriorDelivery:
    """Read summary, finding keys and group coverage from one flat-note snapshot.

    A second fetch can see a different MR. Nested discussion objects have no
    top-level body. Fetch failure must remain distinct from an empty success.
    """
    entries, errors, _ = fetch_entries(owner, repo, number, forge=forge)
    if errors:
        return PriorDelivery(False, frozenset(), frozenset(), "; ".join(errors))
    return prior_delivery_from_entries(entries, sha)


def _entries_from(
    payload: object, source: str | None, timestamp_key: str
) -> list[ReviewEntryWire]:
    """Network sources are fixed; offline unknown sources pass through."""
    entries: list[ReviewEntryWire] = []
    if not isinstance(payload, list):
        return entries
    for item in payload:
        if not isinstance(item, dict):
            continue
        body = item.get("body")
        if not isinstance(body, str):
            continue
        timestamp = item.get(timestamp_key)
        item_source = source if source is not None else item.get("source")
        entries.append(
            {
                "body": body,
                "timestamp": timestamp if isinstance(timestamp, str) else None,
                "source": item_source
                if isinstance(item_source, str)
                else "bodies_file",
                "id": item.get("id"),
            }
        )
    return entries


def collect_entries_github(payload_reviews: object) -> list[ReviewEntryWire]:
    """PR reviews, keyed on ``submitted_at`` — the only surface we write to."""
    return _entries_from(payload_reviews, "review", "submitted_at")


def collect_entries_gitlab(payload_notes: object) -> list[ReviewEntryWire]:
    return _entries_from(payload_notes, "note", "created_at")


def collect_entries_file(payload: object) -> list[ReviewEntryWire]:
    return _entries_from(payload, None, "timestamp")


# Each corroborator renders this header in delivery.compose._render_corroboration;
# its count lets old groups suppress delivery without all member markers.
_CORROBORATION_HEADER = "Corroborating finding — "


def _is_legacy_undermarked_group_body(body: object, matched_marker_count: int) -> bool:
    """A rendered group member was delivered even when its own key is absent.

    Count markers, including duplicates, against rendered members. Individual
    fallback bodies have no corroboration header and cannot cover a group.
    """
    if not isinstance(body, str):
        return False
    section_count = body.count(_CORROBORATION_HEADER)
    return section_count > 0 and matched_marker_count < 1 + section_count


def prior_delivery_from_entries(
    entries: Iterable[object] | None, sha: object
) -> PriorDelivery:
    """Extract state with exact SHA equality; a prefix could suppress another commit.

    Network callers filter authors before passing bodies here.

    Marker counts include duplicates; unique keys alone would misclassify a
    fully marked group as legacy and suppress an undelivered member.
    """
    summary_posted = False
    keys: set[str] = set()
    legacy_keys: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        body = entry.get("body")
        signal = detect_signal(body)
        if signal and signal.get("sha") == sha:
            summary_posted = True
        matched: list[str] = [
            m["key"] for m in find_finding_markers(body) if m["sha"] == sha
        ]
        keys.update(matched)
        if matched and _is_legacy_undermarked_group_body(body, len(matched)):
            legacy_keys.update(matched)
    return PriorDelivery(summary_posted, frozenset(keys), frozenset(legacy_keys), None)


def count_by_source(entries: Iterable[ReviewEntryWire]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        source = entry.get("source")
        counts[source] = counts.get(source, 0) + 1
    return counts


def load_bodies_file(path: str) -> tuple[list[ReviewEntryWire], list[str]]:
    """Return ``(entries, errors)`` from the offline hook file. Never raises."""
    try:
        payload = read_json(path)
    except JsonReadError as exc:
        return [], [f"bodies-file: could not read {path} ({exc.cause})"]
    if not isinstance(payload, list):
        return [], [f"bodies-file: expected a JSON array in {path}"]
    return collect_entries_file(payload), []


def resolve_git_facts(
    sha: object, head_sha: str | None = None, errors: list[str] | None = None
) -> GitFacts:
    """Resolve reviewed/head commits, preserving unavailable objects in the receipt.

    Expand explicit head abbreviations so they compare against the same full SHA
    and commit range as HEAD. Force-pushed or shallow history may be unavailable.
    """
    errors = errors if errors is not None else []
    if head_sha:
        head = git_rev_parse(head_sha)
        if not head:
            errors.append(
                f"git: --head-sha {head_sha} could not be resolved in this clone; "
                "comparisons against it are unreliable"
            )
            head = head_sha
    else:
        head = git_rev_parse("HEAD")
    if not head:
        # The receipt must disclose unavailable Git instead of a false negative.
        errors.append(
            "git: could not resolve the head commit "
            "(not a git repository, an unborn branch, or git is unavailable)"
        )
    reviewed_sha = sha if isinstance(sha, str) and sha else None
    if not reviewed_sha:
        return GitFacts(head or "unknown", None, None, False, False, None)

    _, cat_err, rc = run(
        ["git", "cat-file", "-e", f"{reviewed_sha}^{{commit}}"],
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if rc != 0:
        errors.append(
            f"git: the last-reviewed commit {reviewed_sha[:8]} is not "
            f"present in this clone{': ' + cat_err.strip() if cat_err.strip() else ''}"
        )
        return GitFacts(
            head or "unknown", reviewed_sha, reviewed_sha[:8], False, False, None
        )

    full = git_rev_parse(reviewed_sha) or reviewed_sha
    # Object existence alone cannot make a backwards force-push incremental-safe.
    is_ancestor = False
    if head:
        _, _, anc_rc = run(
            ["git", "merge-base", "--is-ancestor", reviewed_sha, head],
            timeout=GIT_TIMEOUT_SECONDS,
        )
        is_ancestor = anc_rc == 0

    stdout, _, rc = run(
        ["git", "rev-list", "--count", f"{reviewed_sha}..{head or 'HEAD'}"],
        timeout=GIT_TIMEOUT_SECONDS,
    )
    count = stdout.strip()
    return GitFacts(
        head or "unknown",
        full,
        full[:8],
        True,
        is_ancestor,
        int(count) if rc == 0 and count.isdigit() else None,
    )


# Markers are public input to the skill: echo bounded known fields and unknown
# names, never arbitrary unknown values that could inject model instructions.
_MARKER_ECHO_KEYS = (
    "version",
    "findings_count",
    "sha",
    "_token",
    "_legacy",
)
_MARKER_ECHO_MAX_CHARS = 4096


def _bounded(value: object, limit: int = 512) -> object:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "...[truncated]"
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        # Numbers are attacker-chosen too: a 4200-digit integer sails past a cap
        # that only inspects strings.
        text = repr(value)
        return value if len(text) <= 64 else f"[number truncated, {len(text)} digits]"
    encoded = json.dumps(value)
    if len(encoded) <= limit:
        return value
    return f"[{type(value).__name__} truncated, {len(encoded)} chars]"


def sanitize_marker(marker: object) -> dict[str, object] | None:
    """Bound attacker-controlled values before the skill consumes the receipt."""
    if not isinstance(marker, dict):
        return None
    out: dict[str, object] = {
        k: _bounded(marker[k]) for k in _MARKER_ECHO_KEYS if k in marker
    }
    # Key names also carry public text, so bound their length as well as count.
    extra = sorted(
        (k if len(k) <= 64 else k[:64] + "...")
        for k in marker
        if k not in _MARKER_ECHO_KEYS
    )
    if extra:
        out["unknown_keys"] = extra[:32]
    encoded = json.dumps(out)
    if len(encoded) > _MARKER_ECHO_MAX_CHARS:
        return {
            "version": _bounded(out.get("version"), 64),
            "findings_count": out.get("findings_count")
            if isinstance(out.get("findings_count"), int)
            else None,
            "sha": out.get("sha"),
            "_token": out.get("_token"),
            "_legacy": out.get("_legacy"),
            "truncated": True,
        }
    return out


def build_result(
    signal: Mapping[str, object] | None,
    git_facts: GitFacts | None,
    scanned: Mapping[str, int] | None = None,
    errors: Iterable[str] | None = None,
    ignored_foreign: int = 0,
) -> PriorReviewWire:
    """Assemble the receipt; rewritten history must never offer incremental review."""
    git_facts = git_facts if git_facts is not None else _NO_GIT_FACTS
    scanned = dict(scanned or {})
    errors = list(errors or [])
    head_sha = git_facts.head_sha

    result: PriorReviewWire = {
        "previously_reviewed": False,
        "signal": None,
        "source": None,
        "legacy": False,
        "last_reviewed_sha": None,
        "last_reviewed_sha_short": None,
        "sha_resolvable": False,
        "sha_is_ancestor": False,
        "head_sha": head_sha,
        "head_advanced": False,
        "new_commit_count": None,
        "incremental_safe": False,
        "marker": None,
        "scanned": scanned,
        "errors": errors,
        "ignored_foreign": ignored_foreign,
    }
    if not signal:
        return result

    sha_resolvable = git_facts.sha_resolvable
    last_reviewed_sha = cast(
        str | None,
        git_facts.last_reviewed_sha or signal.get("sha"),
    )
    # An unusable head ("unknown", i.e. `git rev-parse HEAD` failed) must never
    # read as "advanced" — that would offer an incremental diff against nothing.
    head_known = bool(head_sha) and head_sha != "unknown"
    is_ancestor = git_facts.sha_is_ancestor
    head_advanced = bool(
        sha_resolvable and head_known and is_ancestor and last_reviewed_sha != head_sha
    )
    result.update(
        {
            "previously_reviewed": True,
            "signal": cast(Literal["marker", "footer"] | None, signal.get("signal")),
            "source": cast(str | None, signal.get("source")),
            "legacy": bool(signal.get("legacy")),
            "last_reviewed_sha": last_reviewed_sha,
            "last_reviewed_sha_short": git_facts.last_reviewed_sha_short,
            "sha_resolvable": sha_resolvable,
            "sha_is_ancestor": is_ancestor,
            "head_advanced": head_advanced,
            "new_commit_count": git_facts.new_commit_count,
            "incremental_safe": bool(sha_resolvable and head_advanced),
            "marker": sanitize_marker(signal.get("marker")),
        }
    )
    return result


def gather_entries(
    args: argparse.Namespace, *, forge: Forge
) -> tuple[list[ReviewEntryWire], list[str], dict[str, int], int]:
    if args.bodies_file:
        entries, errors = load_bodies_file(args.bodies_file)
        return entries, errors, count_by_source(entries), 0

    # Anything recoverable past this point is reported as an `errors[]` entry on a
    # normal exit-0 result, never as an argparse exit-2 with empty stdout: the
    # caller parses our stdout to decide how to degrade, and a run that prints no
    # JSON gives it nothing to read.
    if not args.number:
        return [], ["usage: --number is required unless --bodies-file is given"], {}, 0

    owner, repo = args.owner, args.repo
    if not owner or not repo:
        derived_owner, derived_repo = remote_slug()
        owner = owner or derived_owner
        repo = repo or derived_repo
    if not owner or not repo:
        return (
            [],
            [
                "could not determine owner/repo: the 'origin' remote is missing or "
                "its URL is not in a recognized form — pass --owner and --repo"
            ],
            {},
            0,
        )

    entries, errors, ignored = fetch_entries(owner, repo, args.number, forge=forge)

    scanned = {source: 0 for source in PLATFORM_SOURCES[args.platform]}
    scanned.update(count_by_source(entries))
    return entries, errors, scanned, ignored


def _parser() -> Parser:
    parser = Parser(
        prog="detect_prior_review",
        description="Detect whether code-gauntlet has already reviewed this PR/MR.",
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=("github", "gitlab"),
        help="Forge hosting the PR/MR. Required — the caller already knows it.",
    )
    parser.add_argument(
        "--owner",
        help="Repository owner / GitLab namespace. Defaults to the 'origin' remote.",
    )
    parser.add_argument(
        "--repo",
        help="Repository name. Defaults to the 'origin' remote.",
    )
    parser.add_argument("--number", help="PR number / MR IID.")
    parser.add_argument(
        "--head-sha",
        dest="head_sha",
        help="Compare against this SHA instead of `git rev-parse HEAD`.",
    )
    parser.add_argument(
        "--bodies-file",
        dest="bodies_file",
        help="JSON array of {body,timestamp,source,id} entries to scan INSTEAD of "
        "fetching. Offline/test hook.",
    )
    return parser


def _handle(args: argparse.Namespace) -> tuple[PriorReviewWire, int]:
    forge = make_forge(args.platform)
    entries, errors, scanned, ignored = gather_entries(args, forge=forge)

    signal = select_latest(entries)

    git_facts = resolve_git_facts(
        signal.get("sha") if signal else None, args.head_sha, errors
    )
    result = build_result(signal, git_facts, scanned, errors, ignored)
    return result, 0


def main() -> int:
    return CLI.invoke(sys.argv[1:])


# ASCII escaping keeps public marker names printable under any terminal encoding.
CLI = Command(parser=_parser(), main=_handle, indent=2)
