#!/usr/bin/env python3
"""
post_review.py — Deterministic PR/MR comment delivery for deep-review.

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
                "suggested_fix_code": "..."  # optional — renders as suggestion block
            }
        ],
        "platform": "github",            # optional — auto-detected from git remote
        "owner": "myorg",
        "repo": "myrepo",
        "pr_number": 7
    }

Platform detection:
    Parses git remote URL to detect github.com vs gitlab.com vs self-hosted.
    Override with "platform" field: "github" or "gitlab".

GitHub path:
    Single POST /repos/{owner}/{repo}/pulls/{n}/reviews with comments array,
    event: "COMMENT", via gh api --input.

GitLab path:
    Fetches MR version SHAs (GET /projects/{id}/merge_requests/{iid}/versions).
    Posts per-finding discussion with position object, via glab api --input.

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
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Dry-run capture
# ---------------------------------------------------------------------------
# When --dry-run is passed, main() sets DRY_RUN=True and post_json() captures
# the would-be API calls into _CAPTURED instead of sending them. Skip warnings
# are accumulated into _SKIP_WARNINGS (in addition to being printed) so they can
# be written into the payload file. main() resets all three at startup.

DRY_RUN = False
_CAPTURED = []
_SKIP_WARNINGS = []


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
    result = subprocess.run(
        ["which", name], capture_output=True, text=True
    )
    if result.returncode != 0:
        die(
            f"'{name}' CLI tool not found. "
            f"Install it and ensure it is authenticated before running this script."
        )


def run_api(cmd):
    """Run a CLI API command. Returns (stdout, stderr, returncode)."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout, result.stderr, result.returncode


def post_json(cmd_prefix, payload):
    """Write payload to a temp file and pass via --input. Returns parsed response.

    In dry-run mode the call is captured instead of sent: the intended command
    prefix and payload are appended to ``_CAPTURED`` and an empty dict is
    returned so callers proceed exactly as they would after a successful post.
    """
    if DRY_RUN:
        _CAPTURED.append({"cmd_prefix": cmd_prefix, "payload": payload})
        return {}
    fd, tmppath = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        cmd = cmd_prefix + ["--input", tmppath]
        stdout, stderr, rc = run_api(cmd)
        if rc != 0:
            die(
                f"API call failed (exit {rc}).\n"
                f"Command: {' '.join(cmd)}\n"
                f"stderr: {stderr.strip()}"
            )
        if not stdout.strip():
            return {}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            warn(f"Could not parse API response as JSON: {stdout[:200]}")
            return {"raw": stdout}
    finally:
        if os.path.exists(tmppath):
            os.unlink(tmppath)


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
        path = ssh_match.group(2)
    else:
        # https://host/path or http://host/path
        https_match = re.match(r"https?://([^/]+)/(.+?)(?:\.git)?$", url)
        if not https_match:
            return None, None
        host = https_match.group(1)
        path = https_match.group(2)

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
    Return ``(valid_lines, new_files)``:

    * ``valid_lines`` — set of ``(filepath, line_number)`` tuples for lines present
      in the diff. Line numbers are relative to the new (head) version of each file.
    * ``new_files`` — set of filepaths that are *newly added* in this diff (the
      old-side header is ``--- /dev/null``). GitLab's discussions API rejects
      ``old_path`` for these, so the GitLab poster needs to know.

    Returns ``(None, None)`` when validation should be skipped (unknown platform
    or CLI failure). Callers must handle the ``None`` case.

    Accepts both ``a/`` / ``b/`` prefixed headers (``gh pr diff``) and unprefixed
    headers (``glab mr diff``).
    """
    if platform == "github":
        stdout, stderr, rc = run_api(
            ["gh", "pr", "diff", str(pr_number), "--repo", f"{owner}/{repo}"]
        )
    elif platform == "gitlab":
        # For GitLab, use glab mr diff
        stdout, stderr, rc = run_api(
            ["glab", "mr", "diff", str(pr_number)]
        )
    else:
        warn("Unknown platform — skipping diff validation. All findings will be posted.")
        return None, None

    if rc != 0:
        warn(
            f"Could not fetch diff (exit {rc}): {stderr.strip()}. "
            "Skipping line validation — all findings will be posted."
        )
        return None, None

    valid_lines = set()
    new_files = set()
    current_file = None
    new_line = 0
    current_file_is_new = False

    for raw_line in stdout.splitlines():
        # Old-side header: `--- a/path`, `--- path`, or `--- /dev/null`.
        # `glab mr diff` may omit the `a/` prefix that `gh pr diff` emits.
        old_match = re.match(r"^--- (?:[ab]/)?(.+)$", raw_line)
        if old_match:
            current_file_is_new = old_match.group(1) == "/dev/null"
            continue

        # New-side header: `+++ b/path`, `+++ path`, or `+++ /dev/null`.
        file_match = re.match(r"^\+\+\+ (?:[ab]/)?(.+)$", raw_line)
        if file_match:
            path = file_match.group(1)
            if path == "/dev/null":
                current_file = None  # deleted file — no new path to track
            else:
                current_file = path
                if current_file_is_new:
                    new_files.add(current_file)
            new_line = 0
            current_file_is_new = False
            continue

        # Hunk header: @@ -old_start[,old_count] +new_start[,new_count] @@
        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
        if hunk_match:
            new_line = int(hunk_match.group(1))
            continue

        if current_file is None:
            continue

        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            # Added line — valid for inline comment
            valid_lines.add((current_file, new_line))
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            # Removed line — does not advance new_line
            pass
        elif not raw_line.startswith("\\"):
            # Context line (no prefix or space prefix)
            valid_lines.add((current_file, new_line))
            new_line += 1

    return valid_lines, new_files


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


def valid_lines_for_file(valid_lines, filepath):
    """Return sorted list of up to 10 valid line numbers for *filepath* in the diff.

    Returns None when *valid_lines* is None (validation was skipped).
    """
    if valid_lines is None:
        return None
    stripped = re.sub(r"^[ab]/", "", filepath)
    lines = sorted(
        {l for fp, l in valid_lines if fp == filepath or fp == stripped}
    )
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
    suggested_fix = finding.get("suggested_fix_code", "")

    parts = [f"**{emoji} [{severity.upper()}] {title}**", "", body]

    if suggested_fix:
        parts += [
            "",
            "```suggestion",
            suggested_fix.rstrip("\n"),
            "```",
        ]

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Metadata footer
# ---------------------------------------------------------------------------

def build_footer(findings_count, sha):
    metadata = {
        "version": "3.0",
        "findings_count": findings_count,
        "sha": sha,
    }
    return f"\n\n<!-- deep-review-findings: {json.dumps(metadata, separators=(',', ':'))} -->"


def get_head_sha():
    stdout, _, rc = run_api(["git", "rev-parse", "HEAD"])
    return stdout.strip() if rc == 0 else "unknown"


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

    sha = get_head_sha()
    review_body = data.get("review_body", "")
    review_body += build_footer(len(findings), sha)

    payload = {
        "body": review_body,
        "event": "COMMENT",
        "comments": comments,
    }

    cmd_prefix = [
        "gh", "api",
        "--method", "POST",
        "-H", "Accept: application/vnd.github+json",
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


def post_gitlab(data, valid_lines, new_files=None):
    owner = data["owner"]
    repo = data["repo"]
    mr_iid = data["pr_number"]
    findings = data.get("findings", [])

    check_tool("glab")

    project_id = gitlab_project_id(owner, repo)
    base_sha, head_sha, start_sha = fetch_gitlab_shas(project_id, mr_iid)

    sha = get_head_sha()
    review_body = data.get("review_body", "")
    review_body += build_footer(len(findings), sha)

    # Post the review summary as a top-level MR note first
    summary_payload = {"body": review_body}
    cmd_prefix = [
        "glab", "api",
        "--method", "POST",
        "--header", "Content-Type: application/json",
        f"projects/{project_id}/merge_requests/{mr_iid}/notes",
    ]
    post_json(cmd_prefix, summary_payload)
    print("MR summary note captured (dry-run)." if DRY_RUN else "MR summary note posted.")

    # Post each finding as an inline discussion
    posted = 0
    skipped = 0
    for f in findings:
        filepath = f["file"]
        line = f.get("line")
        if line is None:
            warn_skip(f"Finding '{f.get('title', '?')}' has no line number — skipping.")
            skipped += 1
            continue

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
        # Newly-added files have no old version. GitLab's discussions API
        # returns HTTP 500 (after silently creating the discussion) when
        # ``old_path`` is set on a position pointing into a new file. Omit
        # ``old_path`` for added files; include it for modified files so the
        # position stays anchored to the diff.
        if not is_new_file(new_files, filepath):
            position["old_path"] = filepath

        payload = {
            "body": render_comment_body(f),
            "position": position,
        }

        cmd_prefix = [
            "glab", "api",
            "--method", "POST",
            "--header", "Content-Type: application/json",
            f"projects/{project_id}/merge_requests/{mr_iid}/discussions",
        ]
        resp = post_json(cmd_prefix, payload)
        posted += 1

    if DRY_RUN:
        print(f"  {posted} inline discussion(s) captured.")
    else:
        print(f"  {posted} inline discussion(s) posted.")
    if skipped:
        print(f"  {skipped} finding(s) skipped.")


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
        description="Post deep-review findings as PR/MR comments."
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

    DRY_RUN = args.dry_run
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
    valid_lines, new_files = parse_diff_lines(
        platform, data["owner"], data["repo"], data["pr_number"]
    )

    # Deliver
    if platform == "github":
        post_github(data, valid_lines)
    else:
        post_gitlab(data, valid_lines, new_files)

    if DRY_RUN:
        out_path = write_dry_run_payload(platform, args.findings_json)
        print(f"Dry run — no comments posted. Payload written to: {out_path}")


if __name__ == "__main__":
    main()
