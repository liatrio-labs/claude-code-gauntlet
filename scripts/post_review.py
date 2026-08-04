#!/usr/bin/env python3
"""
post_review.py — Deterministic PR/MR comment delivery for code-gauntlet.

Usage:
    python3 post_review.py <findings_json_path> [--dry-run]

    --dry-run captures the would-be GitHub/GitLab API payloads to
    post-review-payload.json (written next to the findings file) instead of
    posting. Line validation and read-only fetches (diff, MR versions) still run.

Input JSON schema:
    {
        "review_body": "...",
        "findings": [
            {
                "file": "src/foo.py",
                "line": 42,
                "end_line": 45,          # optional
                "severity": "high",
                "title": "SQL injection risk",
                "body": "...",
                "suggestion": "...",         # optional — renders as a "Suggested fix:" prose block
                "claude_md_rule": "...",     # optional — renders as "Cited rule:"; wins over spec_text
                "spec_text": "...",          # optional — renders as "Cited rule:" when there is
                                             #            no claude_md_rule (intent findings)
                "suggested_fix_code": "..."  # optional — renders as suggestion block. Caller-supplied
                                             #            only: no review-pipeline agent emits it.
            }
        ],
        "platform": "github",            # optional — auto-detected from git remote
        "owner": "myorg",
        "repo": "myrepo",
        "pr_number": 7,
        "sha": "0f1e2d3..."              # optional — the commit the review ran
                                         # against. Recorded in the review marker
                                         # when SHA-shaped; falls back to
                                         # `git rev-parse HEAD` when absent, so a
                                         # HEAD that moved between the review and
                                         # the post cannot mislabel the marker.
    }

Platform detection:
    Parses git remote URL to detect github.com vs gitlab.com vs self-hosted.
    Override with "platform" field: "github" or "gitlab".

GitHub path:
    Single POST /repos/{owner}/{repo}/pulls/{n}/reviews with comments array,
    event: "COMMENT", via gh api --input.

GitLab path:
    Fetches MR version SHAs (GET /projects/{id}/merge_requests/{iid}/versions).
    Skips the summary note when this SHA's review marker is already on the MR (asked of
    detect_prior_review.py, the only reader), so a rerun after a partial delivery does not
    duplicate it. Posts per-finding discussions with a position object, via glab api --input;
    a rejected position warns and skips that finding rather than aborting the batch.

Line validation:
    Parses diff to validate each finding line is in the diff.
    Skips findings with invalid lines with a warning.

No external Python dependencies — stdlib only.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

# The prior-review signal (prose footer + marker) is owned end-to-end by
# review_marker.py — this script is only its writer. The explicit path insert
# (rather than a try/except import) keeps both invocation modes working —
# `python3 scripts/post_review.py` and `import scripts.post_review` — without
# swallowing a real ImportError raised from inside review_marker.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The summary-note idempotency check READS the marker, and detect_prior_review.py is the
# only reader (review_marker.py's contract). Importing its one yes/no helper keeps this
# module write-only: it must never grow a second parse of the signal it writes.
from detect_prior_review import gitlab_note_exists_for_sha
from review_marker import SHA_RE, build_footer, is_sha_shaped

# ---------------------------------------------------------------------------
# Dry-run capture
# ---------------------------------------------------------------------------
# When --dry-run is passed, main() sets DRY_RUN=True and post_json() captures
# the would-be API calls into _CAPTURED instead of sending them. Skip warnings
# are accumulated into _SKIP_WARNINGS (in addition to being printed) so they can
# be written into the payload file. main() resets all three at startup.

DRY_RUN = False
_CAPTURED: list[dict] = []
_SKIP_WARNINGS: list[str] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def warn_skip(msg):
    """Emit a skip warning and record it for dry-run payload capture."""
    _SKIP_WARNINGS.append(msg)
    warn(msg)


def check_tool(name):
    """Exit with clear error if CLI tool is not available."""
    result = subprocess.run(["which", name], capture_output=True, text=True)
    if result.returncode != 0:
        die(
            f"'{name}' CLI tool not found. "
            f"Install it and ensure it is authenticated before running this script."
        )


def run_api(cmd):
    """Run a CLI API command. Returns (stdout, stderr, returncode)."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout, result.stderr, result.returncode


def try_post_json(cmd_prefix, payload):
    """Post *payload* and return ``(response, error)`` — exactly one is meaningful.

    The non-fatal core of :func:`post_json`, for the one caller that must survive a
    single rejected item: post_gitlab's per-finding loop posts the summary note FIRST,
    so exiting on the first rejected position stranded every finding behind it behind
    non-idempotent state (issue #127 D3).

    In dry-run the call is captured into ``_CAPTURED`` and ``({}, None)`` is returned,
    so callers proceed exactly as after a successful post.
    """
    if DRY_RUN:
        _CAPTURED.append({"cmd_prefix": cmd_prefix, "payload": payload})
        return {}, None
    fd, tmppath = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        cmd = [*cmd_prefix, "--input", tmppath]
        stdout, stderr, rc = run_api(cmd)
        if rc != 0:
            return None, (
                f"API call failed (exit {rc}).\n"
                f"Command: {' '.join(cmd)}\n"
                f"stderr: {stderr.strip()}"
            )
        if not stdout.strip():
            return {}, None
        try:
            return json.loads(stdout), None
        except json.JSONDecodeError:
            warn(f"Could not parse API response as JSON: {stdout[:200]}")
            return {"raw": stdout}, None
    finally:
        if os.path.exists(tmppath):
            os.unlink(tmppath)


def post_json(cmd_prefix, payload):
    """Post *payload*; die on failure. Returns the parsed response.

    Unchanged contract for every caller whose failure is total — the GitHub review
    (one POST delivers everything) and the GitLab summary note (a failure there means
    auth/MR is wrong and the discussion posts behind it are doomed too).
    """
    response, error = try_post_json(cmd_prefix, payload)
    if error is not None:
        die(error)
    return response


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def detect_platform():
    """Parse git remote URL to detect github.com vs gitlab.com vs self-hosted."""
    stdout, _, rc = run_api(["git", "remote", "get-url", "origin"])
    if rc != 0:
        return None, None
    url = stdout.strip()

    # Normalize SSH git@host:path to https-style for parsing
    # git@github.com:owner/repo.git  ->  github.com/owner/repo
    ssh_match = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)
    if ssh_match:
        host = ssh_match.group(1)
    else:
        # https://host/path or http://host/path
        https_match = re.match(r"https?://([^/]+)/(.+?)(?:\.git)?$", url)
        if not https_match:
            return None, None
        host = https_match.group(1)

    if "github.com" in host:
        return "github", host
    if "gitlab.com" in host or "gitlab" in host:
        return "gitlab", host
    # Unknown host — return host so caller can decide
    return None, host


# ---------------------------------------------------------------------------
# Diff parsing — line validation
# ---------------------------------------------------------------------------


def parse_diff_lines(platform, owner, repo, pr_number):
    """
    Return ``(valid_lines, new_files, old_paths)``:

    * ``valid_lines`` — mapping of ``(filepath, new_line)`` -> the SAME line's number on
      the OLD side, or ``None`` when the line exists only on the new side (an added
      line). Membership is unchanged (a key is present exactly when the line can carry
      an inline comment); the value is what GitLab needs. GitLab addresses an
      UNCHANGED/context line only when the position carries BOTH ``old_line`` and
      ``new_line`` — with new_line alone it answers 400 ``line_code can't be blank``
      (issue #127) — so the old-side number has to survive parsing. GitHub never needs
      it (``path``/``line``/``side`` address the new side).
    * ``new_files`` — set of filepaths newly ADDED in this diff. TWO signals, both
      required: ``gh pr diff`` writes ``--- /dev/null``; ``glab mr diff`` writes the
      SAME path on both sides and betrays the addition ONLY through an
      ``@@ -0,0 +N,M @@`` hunk header. Matching /dev/null alone made this set
      permanently empty on GitLab, so ``old_path`` was always sent and the HTTP 500 the
      GitLab poster documents was never actually avoided (#127 D2).
    * ``old_paths`` — mapping of new-side path -> the path its ``---`` header named. For
      a RENAMED file that is the pre-rename path, which is what GitLab requires in
      ``position.old_path`` (#130); for an unrenamed modified file the two coincide
      (harmless — the poster's fallback is the new path anyway). Absent for added files,
      whose old side is ``/dev/null``.

    Returns ``(None, None, None)`` when validation should be skipped (unknown platform
    or CLI failure). Callers must handle the ``None`` case.

    Header syntax is read PER PLATFORM (see the compiled pair below): ``gh pr diff``
    writes git's synthetic ``a/`` / ``b/`` prefixes, ``glab mr diff`` writes paths
    verbatim. Every key returned therefore carries the platform's true spelling.

    The parser tracks each hunk's DECLARED old/new line budgets (a unified diff hunk
    header states exactly how many lines of each side the hunk body contains) and only
    matches file/hunk headers BETWEEN hunks. Well-formed git/gh/glab diffs always declare
    correct counts, so the counts are trusted. Header matching is suspended inside a hunk
    body because diff body lines collide with the header syntax: removing the SQL comment
    ``-- deprecated: drop me`` renders as ``--- deprecated: drop me``, which the old-side
    header regex swallows — silently desyncing ``old_line`` for every later line of the
    hunk. Symmetrically an added ``++ x`` renders ``+++ x`` and would reset
    ``current_file``. Suspending header matching also means non-header noise between
    hunks (``diff --git …``, ``index …``, ``Binary files … differ``) is ignored instead of
    being admitted as a fake context line.
    """
    if platform == "github":
        stdout, stderr, rc = run_api(
            ["gh", "pr", "diff", str(pr_number), "--repo", f"{owner}/{repo}"]
        )
    elif platform == "gitlab":
        # For GitLab, use glab mr diff
        stdout, stderr, rc = run_api(["glab", "mr", "diff", str(pr_number)])
    else:
        warn(
            "Unknown platform — skipping diff validation. All findings will be posted."
        )
        return None, None, None

    if rc != 0:
        warn(
            f"Could not fetch diff (exit {rc}): {stderr.strip()}. "
            "Skipping line validation — all findings will be posted."
        )
        return None, None, None

    # File-header regexes, chosen ONCE by platform — the platform cannot change inside
    # the loop. `gh pr diff` emits git's synthetic prefixes (`a/` on the old side, `b/`
    # on the new side): those are diff syntax and must come off. `glab mr diff` emits
    # paths VERBATIM and never writes a synthetic prefix, so a leading `a/` there is a
    # REAL top-level directory; stripping it truncated `a/`-rooted paths (`a/foo.py` ->
    # `foo.py`) into keys and positions GitLab does not know, and every finding in such
    # a repo was rejected. The catch-all group carries `/dev/null` on both platforms.
    if platform == "github":
        old_header_re = re.compile(r"^--- (?:a/)?(.+)$")
        new_header_re = re.compile(r"^\+\+\+ (?:b/)?(.+)$")
    else:
        old_header_re = re.compile(r"^--- (.+)$")
        new_header_re = re.compile(r"^\+\+\+ (.+)$")

    valid_lines = {}
    new_files = set()
    old_paths = {}
    pending_old_path = None
    current_file = None
    new_line = 0
    old_line = 0
    # Lines of each side still owed by the hunk currently being read. Both at 0 means
    # "between hunks" — the only zone where a line may be read as a header.
    old_rem = 0
    new_rem = 0
    current_file_is_new = False

    # Split on "\n" ONLY, never str.splitlines(): that also breaks on \x0c, \x0b, \x85 and
    # U+2028/U+2029, which git treats as ordinary line CONTENT. A form feed inside a hunk
    # body would become two parsed lines, draining the declared budgets one line early —
    # flipping the header/body zone boundary and shipping a wrong old_line thereafter. The
    # trailing "" a newline-terminated stream yields lands in the header zone and matches
    # nothing.
    for raw_line in stdout.split("\n"):
        if old_rem <= 0 and new_rem <= 0:
            # -- header zone -------------------------------------------------
            # Old-side header: `--- a/path` (gh), `--- path` (glab), or `--- /dev/null`.
            old_match = old_header_re.match(raw_line)
            if old_match:
                old_side = old_match.group(1)
                current_file_is_new = old_side == "/dev/null"
                # Held until the `+++` header names the new-side path this belongs to;
                # for a rename the two differ and only this one is GitLab's `old_path`.
                pending_old_path = None if current_file_is_new else old_side
                continue

            # New-side header: `+++ b/path` (gh), `+++ path` (glab), or `+++ /dev/null`.
            file_match = new_header_re.match(raw_line)
            if file_match:
                path = file_match.group(1)
                if path == "/dev/null":
                    current_file = None  # deleted file — no new path to track
                else:
                    current_file = path
                    if current_file_is_new:
                        new_files.add(current_file)
                    if pending_old_path is not None:
                        old_paths[current_file] = pending_old_path
                new_line = 0
                old_line = 0
                current_file_is_new = False
                continue

            # Hunk header: @@ -old_start[,old_count] +new_start[,new_count] @@
            # A count is omitted only for a one-line side, so it defaults to 1.
            hunk_match = re.match(
                r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw_line
            )
            if hunk_match:
                old_start, old_count, new_start, new_count = hunk_match.groups()
                old_line = int(old_start)
                new_line = int(new_start)
                old_rem = 1 if old_count is None else int(old_count)
                new_rem = 1 if new_count is None else int(new_count)
                # `@@ -0,0` means the old side of this file is empty: either the file is
                # ADDED, or it pre-existed and was empty. Plain `glab mr diff` offers no
                # discriminator between the two (it repeats the path on both
                # `---`/`+++` lines and never writes /dev/null, so this is its ONLY
                # added-file signal). We prefer the added-file reading: sending
                # `old_path` into a genuinely new file is the documented HTTP 500,
                # whereas omitting it for a pre-existing empty file is not.
                if old_line == 0 and old_rem == 0 and current_file is not None:
                    new_files.add(current_file)
                continue

            # Anything else between hunks (`diff --git …`, `index …`,
            # `Binary files … differ`, mode lines) is noise — never a commentable line.
            continue

        # -- hunk-body zone --------------------------------------------------
        # Headers are NOT matched here: `--- <text>` / `+++ <text>` are body content.
        # Budgets are consumed even when `current_file` is None (a deleted file's body
        # must still drain, or its lines would be read as the next file's headers).
        if raw_line.startswith("\\"):
            # `\ No newline at end of file` belongs to neither side.
            continue

        if raw_line.startswith("+"):
            # Added line — new side only, so there is no old_line to record.
            new_rem -= 1
            if current_file is not None:
                valid_lines[(current_file, new_line)] = None
            new_line += 1
        elif raw_line.startswith("-"):
            # Removed line — advances the OLD side only; not addressable by new_line.
            old_rem -= 1
            old_line += 1
        else:
            # Context line (space- or zero-prefixed) — present on BOTH sides.
            old_rem -= 1
            new_rem -= 1
            if current_file is not None:
                valid_lines[(current_file, new_line)] = old_line
            new_line += 1
            old_line += 1

    return valid_lines, new_files, old_paths


def is_line_valid(valid_lines, filepath, line):
    """Check whether (filepath, line) appears in the diff."""
    if valid_lines is None:
        return True  # validation skipped
    # Try exact path and also path without leading component
    if (filepath, line) in valid_lines:
        return True
    # Strip leading "a/" or "b/" if present
    stripped = re.sub(r"^[ab]/", "", filepath)
    return (stripped, line) in valid_lines


def diff_path_spelling(valid_lines, filepath, line):
    """Return the spelling of *filepath* recorded in the diff, or *filepath* unchanged.

    A finding may spell its path with a synthetic diff prefix (``b/src/app.py``) while
    the parsed keys are unprefixed — or, on GitLab, the repo may contain a REAL top-level
    ``a/``/``b/`` directory that must not be stripped. Trust the diff: prefer the exact
    key, fall back to the stripped one, and when validation was skipped (*valid_lines*
    is None) pass the finding's own spelling through untouched.
    """
    if not isinstance(valid_lines, dict):
        return filepath
    if (filepath, line) in valid_lines:
        return filepath
    stripped = re.sub(r"^[ab]/", "", filepath)
    if (stripped, line) in valid_lines:
        return stripped
    return filepath


def old_line_for(valid_lines, filepath, line):
    """Return the OLD-side line number for ``(filepath, line)``, or None.

    None means "send no old_line": validation was skipped (``valid_lines`` is None or
    not a mapping), the line is add-only, or the path is unknown. Applies the SAME
    ``a/``/``b/`` normalization as :func:`is_line_valid` — a raw-key-only lookup would
    return None for every finding that passed validation only through the stripped form,
    silently re-arming the 400 this function exists to prevent.
    """
    if not isinstance(valid_lines, dict):
        return None
    if (filepath, line) in valid_lines:
        return valid_lines[(filepath, line)]
    stripped = re.sub(r"^[ab]/", "", filepath)
    return valid_lines.get((stripped, line))


def valid_lines_for_file(valid_lines, filepath):
    """Return sorted list of up to 10 valid line numbers for *filepath* in the diff.

    Returns None when *valid_lines* is None (validation was skipped).
    """
    if valid_lines is None:
        return None
    stripped = re.sub(r"^[ab]/", "", filepath)
    lines = sorted({line for fp, line in valid_lines if fp in (filepath, stripped)})
    return lines[:10]


def is_new_file(new_files, filepath):
    """Return True when *filepath* was newly added in the diff.

    Strips any leading ``a/`` / ``b/`` prefix on *filepath* before lookup so
    finding paths match diff-captured paths regardless of which side emitted
    the prefix. Returns False when *new_files* is None or empty.
    """
    if not new_files:
        return False
    if filepath in new_files:
        return True
    stripped = re.sub(r"^[ab]/", "", filepath)
    return stripped in new_files


# ---------------------------------------------------------------------------
# Comment body rendering
# ---------------------------------------------------------------------------


def _rendered_text(value):
    """Normalize a finding field for optional rendering.

    Returns ``None`` for ``None``, ``""``, and whitespace-only strings — all
    treated as absent, mirroring the established ``suggested_fix_code``
    semantics. A non-string value (e.g. a number) is coerced via ``str()``
    rather than crashing the renderer. LEADING and trailing newlines are both
    stripped: the sections below are joined with their own blank lines, so a value
    padded on either side puts a stray blank line into the comment. Only NEWLINES
    are stripped, never spaces — ``suggested_fix_code`` runs through here too and
    its first line's indentation is part of the replacement.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if not value.strip():
        return None
    return value.strip("\n")


def render_comment_body(finding):
    """Build the markdown comment body for a finding."""
    severity = finding.get("severity", "medium").lower()
    emoji_map = {
        "critical": "🔴",
        "high": "🟠",
        "medium": "🟡",
        "low": "💡",
    }
    emoji = emoji_map.get(severity, "💡")

    title = finding.get("title", "Finding")
    body = finding.get("body", "")
    # Through the same normalizer as the fields below: a non-string here used to reach
    # .rstrip() and raise, and a whitespace-only value used to render a ```suggestion fence
    # whose one-click apply would BLANK the cited lines. Both are now treated as absent.
    suggested_fix = _rendered_text(finding.get("suggested_fix_code"))

    parts = [f"**{emoji} [{severity.upper()}] {title}**", "", body]

    # Prose fix suggestion (issue #47). Sourced from `suggestion`; rendered
    # only when non-empty, ahead of the ```suggestion fence below.
    suggestion_text = _rendered_text(finding.get("suggestion"))
    if suggestion_text:
        parts += ["", "**Suggested fix:**", suggestion_text]

    # Cited rule (issue #47). `claude_md_rule` wins when both it and
    # `spec_text` are present.
    rule_text = _rendered_text(finding.get("claude_md_rule"))
    if not rule_text:
        rule_text = _rendered_text(finding.get("spec_text"))
    if rule_text:
        parts += ["", f"**Cited rule:** {rule_text}"]

    # `criticality`, `failure_scenario`, `evidence`, `confidence`, and
    # `dimension` are deliberately NOT rendered into posted PR comments
    # (issue #47) — they are scoped to the artifact/report consumers, not
    # this deterministic comment renderer. Do not "helpfully" add them here.

    if suggested_fix:
        parts += [
            "",
            "```suggestion",
            suggested_fix,
            "```",
        ]

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Metadata footer
# ---------------------------------------------------------------------------
# ``build_footer`` is imported from review_marker (see the bootstrap at the top of
# this file) and re-exported here, so this module has no second definition of the
# signal it writes.


def get_head_sha():
    stdout, _, rc = run_api(["git", "rev-parse", "HEAD"])
    return stdout.strip() if rc == 0 else "unknown"


def resolve_marker_sha(data):
    """Return the SHA to record in the review marker.

    Prefers the ``sha`` pinned in the payload — the commit the review actually ran
    against — so a HEAD that moved between the workflow run and the post cannot
    record a SHA no review ever examined (which would scope a later incremental
    diff wrongly). Falls back to ``git rev-parse HEAD`` when the field is absent
    or not SHA-shaped.
    """
    sha = data.get("sha")
    if isinstance(sha, str) and SHA_RE.fullmatch(sha.strip()):
        return sha.strip()
    head = get_head_sha()
    if not SHA_RE.fullmatch(head):
        # get_head_sha() yields "unknown" when git fails. Writing that produces a
        # marker the reader is guaranteed to reject, so the review posts but the
        # next run cannot detect it. Say so rather than failing silently.
        warn(
            f"could not resolve a commit SHA for the review marker (git returned "
            f"{head!r}); the posted review will not be detectable as a prior review. "
            f"Set the 'sha' field in the findings JSON to avoid this."
        )
    return head


# ---------------------------------------------------------------------------
# GitHub delivery
# ---------------------------------------------------------------------------


def post_github(data, valid_lines):
    owner = data["owner"]
    repo = data["repo"]
    pr_number = data["pr_number"]
    findings = data.get("findings", [])

    check_tool("gh")

    comments = []
    skipped = []
    for f in findings:
        filepath = f["file"]
        line = f["line"]
        if not is_line_valid(valid_lines, filepath, line):
            diag = ""
            vl = valid_lines_for_file(valid_lines, filepath)
            if vl is not None:
                diag = f" Valid lines for this file: {vl}"
            warn_skip(
                f"Skipping finding '{f.get('title', '?')}' at {filepath}:{line} "
                f"— line not found in diff.{diag}"
            )
            skipped.append(f)
            continue

        comment = {
            "path": filepath,
            "line": line,
            "side": "RIGHT",
            "body": render_comment_body(f),
        }
        # Add start_line for multi-line comments
        end_line = f.get("end_line")
        if end_line and end_line != line:
            comment["start_line"] = line
            comment["start_side"] = "RIGHT"
            comment["line"] = end_line

        comments.append(comment)

    sha = resolve_marker_sha(data)
    review_body = data.get("review_body", "")
    review_body += build_footer(len(findings), sha, body=review_body)

    payload = {
        "body": review_body,
        "event": "COMMENT",
        "comments": comments,
    }

    cmd_prefix = [
        "gh",
        "api",
        "--method",
        "POST",
        "-H",
        "Accept: application/vnd.github+json",
        f"repos/{owner}/{repo}/pulls/{pr_number}/reviews",
    ]

    resp = post_json(cmd_prefix, payload)
    if DRY_RUN:
        print("Review captured (dry-run).")
        print(f"  {len(comments)} inline comment(s) captured.")
    else:
        url = resp.get("html_url", resp.get("id", "posted"))
        print(f"Review posted: {url}")
        print(f"  {len(comments)} inline comment(s) posted.")
    if skipped:
        print(f"  {len(skipped)} finding(s) skipped (lines not in diff).")


# ---------------------------------------------------------------------------
# GitLab delivery
# ---------------------------------------------------------------------------


def gitlab_project_id(owner, repo):
    """Return URL-encoded project path for use in GitLab API."""
    path = f"{owner}/{repo}"
    return path.replace("/", "%2F")


def fetch_gitlab_shas(project_id, mr_iid):
    """Fetch latest MR version SHAs from GitLab."""
    check_tool("glab")
    stdout, stderr, rc = run_api(
        ["glab", "api", f"projects/{project_id}/merge_requests/{mr_iid}/versions"]
    )
    if rc != 0:
        die(
            f"Failed to fetch MR versions (exit {rc}): {stderr.strip()}\n"
            "Ensure glab is authenticated and the MR IID is correct."
        )
    try:
        versions = json.loads(stdout)
    except json.JSONDecodeError:
        die(f"Could not parse MR versions response: {stdout[:200]}")

    if not versions:
        die("MR versions endpoint returned an empty list.")

    latest = versions[0]
    return (
        latest["base_commit_sha"],
        latest["head_commit_sha"],
        latest["start_commit_sha"],
    )


def summary_already_posted(owner, repo, mr_iid, sha):
    """True when a note on this MR already carries THIS sha's review marker.

    Makes a rerun after a partial delivery retry-safe: the per-finding loop can be
    re-attempted without stacking a second summary note (issue #127 D4). The read itself
    lives in detect_prior_review.py — the only reader of the signal — so this module
    stays write-only.

    Never blocks the post. Dry-run does not fetch AT ALL: dry-run's invariant is that it
    issues no WRITE calls (reads do happen under dry-run — `glab mr diff` and the
    versions fetch both run), and the DRY_RUN guard here exists so dry-run adds no READ
    either and `_CAPTURED[0]` stays the summary, which build_dry_run_payload's "the
    first capture is the summary" shape depends on. A marker sha that is not SHA-shaped
    (get_head_sha's "unknown" fallback) is not a usable dedup key. A failed fetch warns
    and posts — a possible duplicate note beats a silently dropped review.
    """
    if DRY_RUN:
        return False
    if not is_sha_shaped(sha):
        return False
    exists, error = gitlab_note_exists_for_sha(owner, repo, mr_iid, sha)
    if error:
        warn(f"could not check for an existing summary note ({error}); posting it.")
        return False
    return exists


def post_gitlab(data, valid_lines, new_files=None, old_paths=None):
    owner = data["owner"]
    repo = data["repo"]
    mr_iid = data["pr_number"]
    findings = data.get("findings", [])

    check_tool("glab")

    project_id = gitlab_project_id(owner, repo)
    base_sha, head_sha, start_sha = fetch_gitlab_shas(project_id, mr_iid)

    sha = resolve_marker_sha(data)
    review_body = data.get("review_body", "")
    review_body += build_footer(len(findings), sha, body=review_body)

    # Post the review summary as a top-level MR note first
    summary_payload = {"body": review_body}
    cmd_prefix = [
        "glab",
        "api",
        "--method",
        "POST",
        "--header",
        "Content-Type: application/json",
        f"projects/{project_id}/merge_requests/{mr_iid}/notes",
    ]
    if summary_already_posted(owner, repo, mr_iid, sha):
        print(f"MR summary note for {sha} already on the MR — skipping.")
    else:
        post_json(cmd_prefix, summary_payload)
        print(
            "MR summary note captured (dry-run)."
            if DRY_RUN
            else "MR summary note posted."
        )

    # Post each finding as an inline discussion
    posted = 0
    skipped = 0
    failed = 0
    for f in findings:
        line = f.get("line")
        if line is None:
            warn_skip(f"Finding '{f.get('title', '?')}' has no line number — skipping.")
            skipped += 1
            continue

        # The position must ship the spelling GitLab knows, which is the spelling the
        # diff recorded: a `b/`-prefixed finding path against unprefixed keys resolves
        # to the stripped form, while a real `a/`-rooted path that IS a diff key stays
        # whole. Resolve once, here, and use it everywhere below. When validation was
        # skipped there is no diff to consult and the finding's raw path travels as-is.
        filepath = diff_path_spelling(valid_lines, f["file"], line)

        if not is_line_valid(valid_lines, filepath, line):
            diag = ""
            vl = valid_lines_for_file(valid_lines, filepath)
            if vl is not None:
                diag = f" Valid lines for this file: {vl}"
            warn_skip(
                f"Skipping finding '{f.get('title', '?')}' at {filepath}:{line} "
                f"— line not found in diff.{diag}"
            )
            skipped += 1
            continue

        position = {
            "position_type": "text",
            "base_sha": base_sha,
            "head_sha": head_sha,
            "start_sha": start_sha,
            "new_path": filepath,
            "new_line": line,
        }
        # An UNCHANGED (context) line is addressable only when the position carries
        # both sides; new_line alone is rejected with 400 `line_code can't be blank`
        # (issue #127). An added line has no old side — omit the key rather than
        # sending null. NEVER synthesize `line_code`: it is derived server-side, and
        # both documented attempts to compute it client-side (position sibling, and
        # inside line_range) reproduced the identical 400.
        old_line = old_line_for(valid_lines, filepath, line)
        if old_line is not None:
            position["old_line"] = old_line
        # Newly-added files have no old version. GitLab's discussions API
        # returns HTTP 500 (after silently creating the discussion) when
        # ``old_path`` is set on a position pointing into a new file. Omit
        # ``old_path`` for added files; include it for modified files so the
        # position stays anchored to the diff.
        if not is_new_file(new_files, filepath):
            # A RENAMED file must anchor `old_path` to its PRE-RENAME path (#130) — the
            # new path does not exist on the old side. `filepath` was resolved against
            # the parsed keys above and `old_paths` is keyed by those same keys, so the
            # two are the same spelling. The fallback to the new path covers skipped
            # validation (`old_paths` is None) and unrenamed files, where the two paths
            # coincide anyway.
            position["old_path"] = (old_paths or {}).get(filepath, filepath)

        payload = {
            "body": render_comment_body(f),
            "position": position,
        }

        cmd_prefix = [
            "glab",
            "api",
            "--method",
            "POST",
            "--header",
            "Content-Type: application/json",
            f"projects/{project_id}/merge_requests/{mr_iid}/discussions",
        ]
        _response, error = try_post_json(cmd_prefix, payload)
        if error is not None:
            # One rejected position must not strand the findings behind it: the summary
            # note is already on the MR, so exiting here leaves partial, non-retryable
            # state.
            warn_skip(
                f"Skipping finding '{f.get('title', '?')}' at {filepath}:{line} "
                f"— GitLab rejected the inline discussion.\n{error}"
            )
            failed += 1
            continue
        posted += 1

    if DRY_RUN:
        print(f"  {posted} inline discussion(s) captured.")
    else:
        print(f"  {posted} inline discussion(s) posted.")
    if skipped:
        print(f"  {skipped} finding(s) skipped.")
    if failed:
        print(
            f"  {failed} inline discussion(s) rejected by GitLab (see warnings above)."
        )
        if posted == 0:
            # Every attempt was made first — this exit reports the outcome, it does not
            # abandon the batch. A partial delivery is a success with warnings.
            die(
                f"all {failed} inline discussion(s) were rejected by GitLab — nothing "
                f"was posted inline. The MR summary note is on the MR; rerunning retries "
                f"the inline comments without duplicating it."
            )


# ---------------------------------------------------------------------------
# Dry-run payload assembly
# ---------------------------------------------------------------------------


def _method_from_cmd(cmd_prefix):
    """Return the HTTP method following ``--method`` in *cmd_prefix* (default POST)."""
    for i, tok in enumerate(cmd_prefix):
        if tok == "--method" and i + 1 < len(cmd_prefix):
            return cmd_prefix[i + 1]
    return "POST"


def build_dry_run_payload(platform):
    """Transform the captured API calls + skip warnings into the payload shape.

    GitHub posts a single review, so the payload exposes ``endpoint`` / ``method``
    / ``payload`` for that one call. GitLab posts a summary note followed by one
    discussion per finding, so the first capture becomes ``summary`` and the rest
    become ``discussions``.
    """
    if platform == "github":
        cap = _CAPTURED[0] if _CAPTURED else {"cmd_prefix": [], "payload": {}}
        cmd_prefix = cap["cmd_prefix"]
        return {
            "platform": "github",
            "endpoint": cmd_prefix[-1] if cmd_prefix else "",
            "method": _method_from_cmd(cmd_prefix),
            "payload": cap["payload"],
            "skipped": list(_SKIP_WARNINGS),
        }

    summary = _CAPTURED[0]["payload"] if _CAPTURED else {}
    discussions = [cap["payload"] for cap in _CAPTURED[1:]]
    return {
        "platform": "gitlab",
        "summary": summary,
        "discussions": discussions,
        "skipped": list(_SKIP_WARNINGS),
    }


def write_dry_run_payload(platform, findings_path):
    """Write the dry-run payload JSON next to *findings_path*. Returns its path."""
    payload = build_dry_run_payload(platform)
    out_dir = os.path.dirname(os.path.abspath(findings_path))
    out_path = os.path.join(out_dir, "post-review-payload.json")
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    global DRY_RUN

    parser = argparse.ArgumentParser(
        description="Post code-gauntlet findings as PR/MR comments."
    )
    parser.add_argument(
        "findings_json",
        help="Path to the findings JSON file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Capture the would-be API payloads to post-review-payload.json "
        "(next to the findings file) instead of posting. Line validation "
        "and read-only fetches still run.",
    )
    args = parser.parse_args()

    # Defense-in-depth: CODE_GAUNTLET_POST_MODE=dry-run self-enforces dry-run so a
    # headless Phase 8 invocation that omits --dry-run cannot live-post. The flag wins
    # when present; env "live" or unset changes nothing without the flag.
    DRY_RUN = args.dry_run or os.environ.get("CODE_GAUNTLET_POST_MODE") == "dry-run"
    _CAPTURED.clear()
    _SKIP_WARNINGS.clear()

    # Load input
    try:
        with open(args.findings_json) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        die(f"Findings file not found: {args.findings_json}")
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in findings file: {e}")

    # Validate required fields
    for field in ("owner", "repo", "pr_number"):
        if field not in data:
            die(f"Missing required field in findings JSON: '{field}'")

    # Determine platform
    platform = data.get("platform")
    if platform:
        platform = platform.lower()
    else:
        detected, host = detect_platform()
        if detected:
            platform = detected
            print(f"Detected platform: {platform} (from git remote: {host})")
        else:
            die(
                "Could not detect platform from git remote. "
                "Set 'platform' field in findings JSON to 'github' or 'gitlab'."
            )

    if platform not in ("github", "gitlab"):
        die(f"Unsupported platform: '{platform}'. Use 'github' or 'gitlab'.")

    # Validate diff lines
    valid_lines, new_files, old_paths = parse_diff_lines(
        platform, data["owner"], data["repo"], data["pr_number"]
    )

    # Deliver
    if platform == "github":
        post_github(data, valid_lines)
    else:
        post_gitlab(data, valid_lines, new_files, old_paths)

    if DRY_RUN:
        out_path = write_dry_run_payload(platform, args.findings_json)
        print(f"Dry run — no comments posted. Payload written to: {out_path}")


if __name__ == "__main__":
    main()
