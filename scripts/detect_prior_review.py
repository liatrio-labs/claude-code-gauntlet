#!/usr/bin/env python3
"""
detect_prior_review.py — Has code-gauntlet reviewed this PR/MR before, and up to which commit?

Usage:
    python3 detect_prior_review.py --platform {github|gitlab} --number N
                                   [--owner O] [--repo R] [--head-sha SHA]
                                   [--bodies-file PATH]

Reads the prior-review signal that ``post_review.py`` leaves on a PR/MR summary —
both halves are parsed by ``review_marker.py``, which is the single source of truth
for the format. Nothing here branches on the marker's ``version`` field.

    --platform     REQUIRED. The orchestrator has already resolved the PR with
                   gh/glab, so the platform is known; guessing it for a
                   self-hosted host would be a coin flip.
    --owner/--repo Optional — parsed from the `origin` remote when omitted, so
                   the usual call needs only --platform and --number.
    --head-sha     Use this instead of `git rev-parse HEAD` for the comparison.
    --bodies-file  Offline/test hook: a JSON array of
                   {"body","timestamp","source","id"} entries used INSTEAD of any
                   network fetch. Makes the CLI end-to-end testable with no network
                   and gives self-hosted users an escape hatch.

Surfaces scanned (read-only; these are exactly the surfaces post_review.py writes to):
    github — repos/{owner}/{repo}/pulls/{n}/reviews      (source "review")
    gitlab — projects/{id}/merge_requests/{n}/notes      (source "note")

Only the surfaces post_review.py actually writes to are scanned. A surface we
never write to can yield no true positive, but anyone with read access can post
to it — and since the newest signal wins, that is a way to aim a rerun at an
attacker-chosen SHA. Note the residual risk: both scanned surfaces are still
user-writable, so a forged signal can at worst cause a rerun to offer/take an
incremental scope. The interactive gate surfaces this to a human; headless
`CODE_GAUNTLET_REVIEWED_POLICY=skip` is the configuration to think twice about.

Output — exactly one JSON object on stdout:
    {
        "previously_reviewed": true,
        "signal": "marker",              # or "footer" / null
        "source": "review",              # which surface carried it / null
        "legacy": false,                 # pre-rename token or product name
        "last_reviewed_sha": "<full>",   # expanded when resolvable, else as recorded
        "last_reviewed_sha_short": "<8>",
        "sha_resolvable": true,          # the object exists in this clone
        "sha_is_ancestor": true,         # ...and is an ancestor of head_sha
        "head_sha": "<full>",
        "head_advanced": true,
        "new_commit_count": 3,           # null when the SHA is unresolvable
        "incremental_safe": true,        # sha_resolvable and head_advanced
        "marker": {...},                 # full parsed payload / null
        "scanned": {"review": 4},
        "errors": []
    }

Exit codes:
    0 for EVERY outcome — "found nothing", "all fetches failed", a missing
    --number, an unparseable remote. The caller reads "errors". Detection is an
    optimization; a review must never fail because a comment fetch 404'd, and a
    non-zero exit with empty stdout would leave the caller nothing to degrade on.
    argparse still rejects a malformed flag (unknown option, bad --platform).

No external Python dependencies — stdlib only.
"""

import argparse
import json
import os
import re
import subprocess
import sys

# Same explicit bootstrap as post_review.py: works when run directly
# (`python3 scripts/detect_prior_review.py`) and when imported as
# `scripts.detect_prior_review`, without swallowing real ImportErrors.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# select_latest drives the scan; detect_signal is re-exported so callers and tests
# can reach the single-body parser without importing review_marker separately.
from review_marker import detect_signal, select_latest  # noqa: E402,F401


FETCH_TIMEOUT_SECONDS = 30
GIT_TIMEOUT_SECONDS = 10

# The surfaces each platform exposes, in scan order. Used to seed "scanned" so the
# key set is stable even when a fetch fails or returns nothing.
PLATFORM_SOURCES = {
    "github": ("review",),
    "gitlab": ("note",),
}


# ---------------------------------------------------------------------------
# Subprocess wrappers — the only impure surface in this module
# ---------------------------------------------------------------------------

def run(cmd, timeout=None):
    """Run *cmd*. Returns ``(stdout, stderr, returncode)``. Never raises.

    A missing CLI tool (OSError) and a timeout both come back as returncode -1
    with the reason in stderr, so callers degrade instead of blowing up.
    """
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            # text=True decodes STRICT utf-8 by default, and UnicodeDecodeError is
            # a ValueError, not an OSError — a single undecodable byte from gh/glab
            # would escape this wrapper and exit 1 with empty stdout, breaking the
            # always-exit-0 contract the caller degrades on.
            encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "", f"timed out after {timeout}s", -1
    except OSError as exc:
        return "", str(exc), -1
    return result.stdout, result.stderr, result.returncode


def _parse_json_array(text):
    """Parse *text* as a JSON array of objects. Returns a list, or None on failure.

    ``gh api --paginate`` merges pages into a single array, but a client that
    emits one array per page must not defeat detection, so concatenated documents
    are tolerated and flattened.
    """
    text = text.strip()
    if not text:
        return []
    decoder = json.JSONDecoder()
    items = []
    idx = 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \t\r\n":
            idx += 1
        if idx >= len(text):
            break
        try:
            doc, end = decoder.raw_decode(text, idx)
        except (ValueError, RecursionError):
            return items if items else None
        if isinstance(doc, list):
            items.extend(doc)
        else:
            items.append(doc)
        idx = end
    return items


def fetch_json(cmd, label):
    """Run *cmd* and parse its stdout as a JSON array.

    Returns ``(items, error)`` — exactly one of which is meaningful. Never raises;
    a failure is a string for ``errors[]``, not an exception.
    """
    stdout, stderr, rc = run(cmd, timeout=FETCH_TIMEOUT_SECONDS)
    if rc != 0:
        detail = (stderr.strip() or stdout.strip())[:300]
        return [], f"{label}: fetch failed (exit {rc}): {detail}"
    items = _parse_json_array(stdout)
    if items is None:
        return [], f"{label}: response was not JSON: {stdout.strip()[:120]}"
    return items, None


def git_rev_parse(rev):
    """Return the full object id for *rev*, or None."""
    stdout, _, rc = run(["git", "rev-parse", rev], timeout=GIT_TIMEOUT_SECONDS)
    value = stdout.strip()
    return value if rc == 0 and value else None


# ---------------------------------------------------------------------------
# Fetch — one call per surface; each failure is independent
# ---------------------------------------------------------------------------

def gitlab_project_id(owner, repo):
    """Return the URL-encoded project path (mirrors post_review.gitlab_project_id)."""
    return f"{owner}/{repo}".replace("/", "%2F")


def remote_slug():
    """Return ``(owner, repo)`` parsed from ``origin``, or ``(None, None)``.

    Lets the caller pass only ``--platform`` and ``--number``: composing an
    owner/repo lookup was one more CLI incantation for the orchestrator to get
    wrong, and this is the same remote parse ``post_review.detect_platform``
    performs (SSH ``git@host:path`` and http(s) ``host/path``, ``.git`` stripped).
    A namespaced GitLab path keeps its subgroups in *repo*, which is correct —
    ``gitlab_project_id`` re-joins and encodes the whole path.
    """
    stdout, _, rc = run(
        ["git", "remote", "get-url", "origin"], timeout=GIT_TIMEOUT_SECONDS
    )
    if rc != 0:
        return None, None
    url = stdout.strip()
    match = (
        # scp-style: git@host:owner/repo(.git)
        re.match(r"[^@/]+@[^:/]+:(.+?)(?:\.git)?/?$", url)
        # any scheme, with optional user@ and :port —
        # https://, http://, ssh://, git://, git+ssh://
        or re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]+@)?[^/]+/(.+?)(?:\.git)?/?$", url)
    )
    if not match:
        return None, None
    owner, sep, repo = match.group(1).strip("/").partition("/")
    if not sep or not owner or not repo:
        return None, None
    return owner, repo


def fetch_entries_github(owner, repo, number):
    """Return ``(entries, errors)`` for the GitHub PR-reviews surface.

    Only ``pulls/{n}/reviews`` is scanned — the exact endpoint ``post_review.py``
    POSTs to. ``issues/{n}/comments`` was deliberately dropped: nothing has ever
    written the signal there, so it could contribute no true positive, while any
    user with read access can post an issue comment carrying a forged marker.
    Since the newest signal wins, that surface was a way to point a rerun at an
    attacker-chosen SHA and skip review of the commits after it.
    """
    reviews, err = fetch_json(
        ["gh", "api", "--paginate", f"repos/{owner}/{repo}/pulls/{number}/reviews"],
        "github reviews",
    )
    return collect_entries_github(reviews), ([err] if err else [])


def fetch_entries_gitlab(owner, repo, number):
    """Return ``(entries, errors)`` for the GitLab MR notes surface.

    ``--paginate`` is required, not optional: GitLab returns 20 notes per page,
    and ``post_gitlab`` posts the marker-bearing summary note FIRST and then one
    inline discussion note per finding. On any MR with more than 20 notes the
    summary is off page 1, so an unpaginated fetch would make every GitLab rerun
    look fresh — the very bug this script exists to fix.
    """
    project_id = gitlab_project_id(owner, repo)
    notes, err = fetch_json(
        ["glab", "api", "--paginate",
         f"projects/{project_id}/merge_requests/{number}/notes"],
        "gitlab notes",
    )
    return collect_entries_gitlab(notes), ([err] if err else [])


# ---------------------------------------------------------------------------
# Pure collectors
# ---------------------------------------------------------------------------

def _entries_from(payload, source, timestamp_key):
    """Map an API array into the entry shape review_marker.select_latest consumes."""
    entries = []
    if not isinstance(payload, list):
        return entries
    for item in payload:
        if not isinstance(item, dict):
            continue
        body = item.get("body")
        if not isinstance(body, str):
            continue
        timestamp = item.get(timestamp_key)
        entries.append({
            "body": body,
            "timestamp": timestamp if isinstance(timestamp, str) else None,
            "source": source,
            "id": item.get("id"),
        })
    return entries


def collect_entries_github(payload_reviews):
    """PR reviews, keyed on ``submitted_at`` — the only surface we write to."""
    return _entries_from(payload_reviews, "review", "submitted_at")


def collect_entries_gitlab(payload_notes):
    """MR notes (``created_at``)."""
    return _entries_from(payload_notes, "note", "created_at")


def collect_entries_file(payload):
    """Map a ``--bodies-file`` array into the entry shape. Unknown sources pass through."""
    entries = []
    if not isinstance(payload, list):
        return entries
    for item in payload:
        if not isinstance(item, dict):
            continue
        body = item.get("body")
        if not isinstance(body, str):
            continue
        timestamp = item.get("timestamp")
        source = item.get("source")
        entries.append({
            "body": body,
            "timestamp": timestamp if isinstance(timestamp, str) else None,
            "source": source if isinstance(source, str) else "bodies_file",
            "id": item.get("id"),
        })
    return entries


def count_by_source(entries):
    """Return ``{source: count}`` over *entries*."""
    counts = {}
    for entry in entries:
        source = entry.get("source")
        counts[source] = counts.get(source, 0) + 1
    return counts


def load_bodies_file(path):
    """Return ``(entries, errors)`` from the offline hook file. Never raises."""
    try:
        with open(path) as fh:
            payload = json.load(fh)
    except (OSError, ValueError, RecursionError) as exc:
        return [], [f"bodies-file: could not read {path} ({exc})"]
    if not isinstance(payload, list):
        return [], [f"bodies-file: expected a JSON array in {path}"]
    return collect_entries_file(payload), []


# ---------------------------------------------------------------------------
# Git facts + result assembly
# ---------------------------------------------------------------------------

def resolve_git_facts(sha, head_sha=None, errors=None):
    """Return the git-derived facts about *sha* relative to the head. Never raises.

    ``sha_resolvable`` is False when the recorded object is not present in this
    clone (force-push, shallow clone, unfetched object); the raw value is kept and
    ``new_commit_count`` stays None.

    An explicit *head_sha* is expanded through ``git rev-parse`` so an abbreviated
    value is never compared against a full one (which would read as "advanced"
    every time), and the commit count is taken against that same head rather than
    whatever HEAD happens to be.
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
        # Without a head there is nothing to compare against; say why, so the
        # caller's "detection unavailable" disclosure names the real reason
        # instead of reporting a bare "no prior review".
        errors.append(
            "git: could not resolve the head commit "
            "(not a git repository, an unborn branch, or git is unavailable)"
        )
    facts = {
        "head_sha": head or "unknown",
        "last_reviewed_sha": sha if isinstance(sha, str) and sha else None,
        "last_reviewed_sha_short": None,
        "sha_resolvable": False,
        "sha_is_ancestor": False,
        "new_commit_count": None,
    }
    if not facts["last_reviewed_sha"]:
        return facts
    facts["last_reviewed_sha_short"] = facts["last_reviewed_sha"][:8]

    _, cat_err, rc = run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"], timeout=GIT_TIMEOUT_SECONDS
    )
    if rc != 0:
        errors.append(
            f"git: the last-reviewed commit {facts['last_reviewed_sha_short']} is not "
            f"present in this clone{': ' + cat_err.strip() if cat_err.strip() else ''}"
        )
        return facts
    facts["sha_resolvable"] = True

    full = git_rev_parse(sha) or sha
    facts["last_reviewed_sha"] = full
    facts["last_reviewed_sha_short"] = full[:8]

    # The reviewed commit must be an ANCESTOR of the head, not merely a different
    # object. After a branch is force-pushed backwards the old commit still exists
    # in the object DB, so an inequality test alone reports "advanced" while
    # `rev-list --count` correctly says 0 — which would render as "0 new commits
    # have been pushed since" and hand `git diff <sha>...HEAD` an empty diff.
    if head:
        _, _, anc_rc = run(
            ["git", "merge-base", "--is-ancestor", sha, head],
            timeout=GIT_TIMEOUT_SECONDS,
        )
        facts["sha_is_ancestor"] = anc_rc == 0

    stdout, _, rc = run(
        ["git", "rev-list", "--count", f"{sha}..{head or 'HEAD'}"],
        timeout=GIT_TIMEOUT_SECONDS,
    )
    count = stdout.strip()
    if rc == 0 and count.isdigit():
        facts["new_commit_count"] = int(count)
    return facts


#: Keys echoed back from a parsed marker. The payload is attacker-controllable —
#: anyone with read access can post a comment carrying a marker — and the
#: orchestrator is told to consume the `marker` object, so an unbounded verbatim
#: echo would pipe arbitrary text straight into a model's context. Forward
#: compatibility is preserved by `unknown_keys` (names only, capped), which lets a
#: future producer's fields be noticed without their values being replayed.
_MARKER_ECHO_KEYS = ("version", "findings_count", "sha", "findings", "_token", "_legacy")
_MARKER_ECHO_MAX_CHARS = 4096


def _bounded(value, limit=512):
    """Return *value* with any string/collection clipped to a printable bound."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "...[truncated]"
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        # Numbers are attacker-chosen too: a 4200-digit integer sails past a cap
        # that only inspects strings.
        text = repr(value)
        return value if len(text) <= 64 else f"[number truncated, {len(text)} digits]"
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError):
        return "[unrepresentable]"
    if len(encoded) <= limit:
        return value
    return f"[{type(value).__name__} truncated, {len(encoded)} chars]"


def sanitize_marker(marker):
    """Return a size-bounded, allow-listed view of a parsed marker payload.

    Every echoed VALUE is bounded too, not just the key set: an allow-listed key
    is still attacker-controlled, so an unbounded `version` string was a way to
    pipe arbitrary text into the orchestrator's context through a key that
    passed the allow-list.
    """
    if not isinstance(marker, dict):
        return None
    out = {k: _bounded(marker[k]) for k in _MARKER_ECHO_KEYS if k in marker}
    # Key NAMES are attacker-authored strings too — capping their count alone
    # still let kilobytes of free text through the "names only" guarantee.
    extra = sorted(
        (k if isinstance(k, str) and len(k) <= 64 else str(k)[:64] + "...")
        for k in marker if k not in _MARKER_ECHO_KEYS
    )
    if extra:
        out["unknown_keys"] = extra[:32]
    try:
        encoded = json.dumps(out)
    except (TypeError, ValueError):
        return {"sha": marker.get("sha"), "unrepresentable": True}
    if len(encoded) > _MARKER_ECHO_MAX_CHARS:
        return {
            "version": _bounded(out.get("version"), 64),
            "findings_count": out.get("findings_count")
            if isinstance(out.get("findings_count"), int) else None,
            "sha": out.get("sha"),
            "_token": out.get("_token"),
            "_legacy": out.get("_legacy"),
            "truncated": True,
        }
    return out


def build_result(signal, git_facts, scanned=None, errors=None):
    """Assemble the output object. Pure — no subprocess, no I/O.

    ``incremental_safe`` is exactly ``sha_resolvable and head_advanced``, and
    ``head_advanced`` additionally requires the reviewed commit to be an ancestor
    of the head — so a backwards force-push degrades to a full review instead of
    promising an incremental diff that would be empty. It is the one boolean the
    orchestrator gates the incremental path on.
    """
    scanned = dict(scanned or {})
    errors = list(errors or [])
    git_facts = git_facts or {}
    head_sha = git_facts.get("head_sha")

    if not signal:
        return {
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
        }

    sha_resolvable = bool(git_facts.get("sha_resolvable"))
    last_reviewed_sha = git_facts.get("last_reviewed_sha") or signal.get("sha")
    # An unusable head ("unknown", i.e. `git rev-parse HEAD` failed) must never
    # read as "advanced" — that would offer an incremental diff against nothing.
    head_known = bool(head_sha) and head_sha != "unknown"
    is_ancestor = bool(git_facts.get("sha_is_ancestor"))
    head_advanced = bool(
        sha_resolvable and head_known and is_ancestor and last_reviewed_sha != head_sha
    )
    return {
        "previously_reviewed": True,
        "signal": signal.get("signal"),
        "source": signal.get("source"),
        "legacy": bool(signal.get("legacy")),
        "last_reviewed_sha": last_reviewed_sha,
        "last_reviewed_sha_short": git_facts.get("last_reviewed_sha_short"),
        "sha_resolvable": sha_resolvable,
        "sha_is_ancestor": is_ancestor,
        "head_sha": head_sha,
        "head_advanced": head_advanced,
        "new_commit_count": git_facts.get("new_commit_count"),
        "incremental_safe": bool(sha_resolvable and head_advanced),
        "marker": sanitize_marker(signal.get("marker")),
        "scanned": scanned,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def gather_entries(args):
    """Return ``(entries, errors, scanned)`` for the requested source of bodies."""
    if args.bodies_file:
        entries, errors = load_bodies_file(args.bodies_file)
        return entries, errors, count_by_source(entries)

    # Anything recoverable past this point is reported as an `errors[]` entry on a
    # normal exit-0 result, never as an argparse exit-2 with empty stdout: the
    # caller parses our stdout to decide how to degrade, and a run that prints no
    # JSON gives it nothing to read.
    if not args.number:
        return [], ["usage: --number is required unless --bodies-file is given"], {}

    owner, repo = args.owner, args.repo
    if not owner or not repo:
        derived_owner, derived_repo = remote_slug()
        owner = owner or derived_owner
        repo = repo or derived_repo
    if not owner or not repo:
        return [], [
            "could not determine owner/repo: the 'origin' remote is missing or "
            "its URL is not in a recognized form — pass --owner and --repo"
        ], {}

    if args.platform == "github":
        entries, errors = fetch_entries_github(owner, repo, args.number)
    else:
        entries, errors = fetch_entries_gitlab(owner, repo, args.number)

    scanned = {source: 0 for source in PLATFORM_SOURCES[args.platform]}
    scanned.update(count_by_source(entries))
    return entries, errors, scanned


def main():
    parser = argparse.ArgumentParser(
        description="Detect whether code-gauntlet has already reviewed this PR/MR."
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
    args = parser.parse_args()

    entries, errors, scanned = gather_entries(args)

    try:
        signal = select_latest(entries)
    except Exception as exc:  # pragma: no cover — defensive: detection never blocks
        signal = None
        errors = errors + [f"detection failed: {exc}"]

    git_facts = resolve_git_facts(
        signal.get("sha") if signal else None, args.head_sha, errors
    )
    result = build_result(signal, git_facts, scanned, errors)
    # ensure_ascii=True: `marker` echoes unknown keys verbatim and `errors`
    # carries raw gh/glab stderr, so the payload can hold text outside the
    # terminal encoding. Escaping it keeps stdout printable under an ASCII
    # locale instead of dying with UnicodeEncodeError and no JSON at all.
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
