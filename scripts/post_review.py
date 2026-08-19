#!/usr/bin/env python3
"""
post_review.py — Deterministic PR/MR comment delivery for code-gauntlet.

Usage:
    python3 post_review.py <findings_json_path> [--dry-run]

    --dry-run captures the would-be GitHub/GitLab API payloads to
    post-review-payload.json (written next to the findings file) instead of
    posting. Line validation and read-only fetches (diff, MR versions) still run.
    One capture is deliberately NOT the live bytes: a GitLab discussion body is
    captured as the rendered comment alone, without the per-finding delivery
    marker the live post appends (see the marker note in post_gitlab).

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
                "suggestion": "...",         # optional — **Suggested fix:**; sanitized + redacted; uncapped
                "claude_md_rule": "...",     # optional — **Cited rule:** (wins over spec_text); sanitized, redacted, capped at 500, blockquoted
                "spec_text": "...",          # optional — **Cited rule:** when no claude_md_rule; same treatment
                "suggested_fix_code": "..."  # optional — the ```suggestion fence: a COMMITTABLE patch
                                             #            replacing exactly lines [line, end_line]. Rendered
                                             #            only when the deterministic apply-check passes
                                             #            (see _suggested_fix_gate); a failing patch is
                                             #            downgraded to the prose `suggestion` and the
                                             #            reason recorded. Secret-redacted; outer fence
                                             #            lengthened; payload otherwise byte-exact
                                             #            (structural sanitize off).
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
    Asks detect_prior_review.py — the only reader — what this SHA's review already left on
    the MR, so a rerun after a partial delivery duplicates neither the summary note nor the
    inline discussions that did land. Posts per-finding discussions with a position object,
    via glab api --input; a rejected position warns and skips that finding rather than
    aborting the batch. Each posted discussion carries a delivery marker keyed on its own
    rendered content, which is what makes the retry recognizable.

    Every position is checked against the diff facts before it is sent OR captured — see
    validate_position for what that does and does not cover. Any malformed position fails
    a --dry-run; live, it is a per-finding loss like a rejection, so it exits non-zero
    only when nothing NEW was posted inline.

Line validation:
    Parses diff to validate each finding line is in the diff. A finding whose line
    cannot be anchored inline (line not in the diff, or no line at all) is not
    dropped: it degrades into a trailing "could not be anchored inline" section on
    the review body / summary note (see build_skipped_section), so every finding
    still reaches the PR/MR even when it cannot land as an inline comment. A
    warning is still emitted per skipped finding.

No external Python dependencies — stdlib only.
"""

import argparse
import hashlib
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

# The idempotency checks READ the markers, and detect_prior_review.py is the only reader
# (review_marker.py's contract). Importing its one state helper keeps this module
# write-only: it must never grow a second parse of the signals it writes.
from detect_prior_review import gitlab_prior_delivery_state
from review_marker import SHA_RE, build_finding_marker, build_footer, is_sha_shaped

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
    Return ``(valid_lines, new_files, old_paths, line_texts)``:

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
    * ``line_texts`` — a PARALLEL mapping over the SAME keys as ``valid_lines``, holding
      each line's NEW-SIDE TEXT with the diff's marker column removed. Parallel rather
      than folded into ``valid_lines``'s value so every existing consumer of that mapping
      reads exactly what it read before. The parser already read this text off each
      ``+``/context line and discarded it; it is the content oracle the
      ``suggested_fix_code`` apply-check needs, and by construction it is the content the
      platform's anchor points at, at the same head SHA the position carries. ``git show``
      is not an alternative: the local HEAD is usually the base branch, a shallow clone has
      no object to show, and it desyncs from GitLab's versions-API head_sha.

    Returns ``(None, None, None, None)`` when validation should be skipped (unknown
    platform or CLI failure). Callers must handle the ``None`` case.

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
        # PLAIN `glab mr diff` — never `--raw`. Plain output is glab's OWN reconstruction
        # from the MR versions API: `--- <old_path>` / `+++ <new_path>` with the path
        # verbatim, and nothing else between hunks. `--raw` streams git's diff instead,
        # which reintroduces `a/` / `b/` prefixes and `/dev/null` — the gitlab branch
        # below keys on paths exactly as printed and reads the old-side header as a real
        # path, both premised on their absence. tests/fixtures/glab_diff/ records the
        # shape and where it comes from.
        stdout, stderr, rc = run_api(["glab", "mr", "diff", str(pr_number)])
    else:
        warn(
            "Unknown platform — skipping diff validation. All findings will be posted."
        )
        return None, None, None, None

    if rc != 0:
        warn(
            f"Could not fetch diff (exit {rc}): {stderr.strip()}. "
            "Skipping line validation — all findings will be posted."
        )
        return None, None, None, None

    # File-header regexes, chosen ONCE by platform — the platform cannot change inside
    # the loop. `gh pr diff` emits git's synthetic prefixes (`a/` on the old side, `b/`
    # on the new side): those are diff syntax and must come off. `glab mr diff` emits
    # paths VERBATIM and never writes a synthetic prefix, so a leading `a/` there is a
    # REAL top-level directory; stripping it truncated `a/`-rooted paths (`a/foo.py` ->
    # `foo.py`) into keys and positions GitLab does not know, and every finding in such
    # a repo was rejected. The catch-all group carries `/dev/null`, which only the GitHub
    # shape ever writes — glab repeats the real path on both sides for an added or
    # deleted file (tests/fixtures/glab_diff/ pins both shapes).
    if platform == "github":
        old_header_re = re.compile(r"^--- (?:a/)?(.+)$")
        new_header_re = re.compile(r"^\+\+\+ (?:b/)?(.+)$")
    else:
        old_header_re = re.compile(r"^--- (.+)$")
        new_header_re = re.compile(r"^\+\+\+ (.+)$")

    valid_lines = {}
    line_texts = {}
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
                line_texts[(current_file, new_line)] = raw_line[1:]
            new_line += 1
        elif raw_line.startswith("-"):
            # Removed line — advances the OLD side only; not addressable by new_line.
            old_rem -= 1
            old_line += 1
        else:
            # Context line (space- or zero-prefixed) — present on BOTH sides. The
            # marker column comes off only when it is really there: a blank context
            # line is a lone space (content ""), but a zero-prefixed one is already
            # bare and slicing it would eat its first character.
            old_rem -= 1
            new_rem -= 1
            if current_file is not None:
                valid_lines[(current_file, new_line)] = old_line
                line_texts[(current_file, new_line)] = (
                    raw_line[1:] if raw_line.startswith(" ") else raw_line
                )
            new_line += 1
            old_line += 1

    return valid_lines, new_files, old_paths, line_texts


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


def _range_is_valid(valid_lines, filepath, start, end):
    """True when every line in [start, end] is a valid diff line for *filepath*.

    A contiguous run of valid lines implies a single hunk, which is what GitHub
    requires for a multi-line comment. Short-circuits on the first miss, so a
    bogus huge *end* (e.g. an ``end_line`` copied from the wrong file) costs at
    most one failing lookup rather than iterating the whole span.
    """
    if valid_lines is None:
        return True  # validation skipped — pass the range through unchanged
    return all(is_line_valid(valid_lines, filepath, n) for n in range(start, end + 1))


def is_new_file(new_files, filepath):
    """Return True when *filepath* was newly added in the diff.

    *filepath* must already be resolved to the diff's own key spelling (see
    :func:`diff_path_spelling`) — its sole caller resolves before calling. `new_files`
    and the resolved keys come from the SAME parse of the SAME headers, so an exact
    match is authoritative. A second, independent ``a/``/``b/``-stripped lookup here
    would let a real `a/`-rooted MODIFIED file collide with an unrelated NEW file that
    happens to share its stripped basename (e.g. modified ``a/foo.py`` vs. added
    ``foo.py``) whenever GitLab preserves a genuine top-level ``a/`` directory, wrongly
    reporting the modified file as new and dropping ``old_path`` from its position.
    Returns False when *new_files* is None or empty.
    """
    if not new_files:
        return False
    return filepath in new_files


def validate_position(
    position, shas, valid_lines, new_files, old_paths, filepath, line
):
    """Return the reasons *position* is malformed for GitLab; empty when it is sound.

    This is what makes a capture mean something. ``try_post_json`` short-circuits into
    ``_CAPTURED`` before a payload reaches the network, so a pre-flight that only counts
    captures reports twelve discussions "captured" immediately before the live run
    answers 400 on all twelve. Its caller runs this UNCONDITIONALLY — one gate for both
    modes by construction, because a check that runs only under --dry-run cannot be the
    thing that makes --dry-run trustworthy.

    The gate is an EXACT-SHAPE comparison against a full expected position, in both
    directions: a missing key, an unexpected key and a wrong value are the same kind of
    400, and only a whole-shape check catches the ones nobody thought to enumerate.
    ``line_code`` is the known case — GitLab derives it server-side — but its sibling
    ``line_range`` reproduces the identical 400, and a key-by-key gate stays silent on
    every field added to the assembly after it was written. Two-directional presence
    matters for the same reason: an if-present-check-equality test passes every fixture
    while saying nothing about the omission it exists to catch, which is precisely how a
    dropped conditional attach reaches the wire.

    *shas* is the ``fetch_gitlab_shas`` triple. Whether the fetched values are usable at
    all is a loop-invariant question answered once at the fetch; whether each position
    CARRIES them is per-position structure, and only this gate sees that.

    Every expectation is recomputed here from the SAME facts the assembly consumed,
    rather than shared with it: a gate that derives its answer through the code under
    test moves with the bug and passes it.

    SCOPE, stated plainly: this catches a regression in the assembly below, or a
    malformed finding. It cannot catch a parser defect — ``valid_lines``, ``new_files``
    and ``old_paths`` are the ground truth BOTH sides are derived from, so a wrong answer
    there is compared against itself and passes.
    """
    base_sha, head_sha, start_sha = shas
    expected = {
        "position_type": "text",
        "base_sha": base_sha,
        "head_sha": head_sha,
        "start_sha": start_sha,
        "new_path": filepath,
        "new_line": line,
    }
    expected_old_line = old_line_for(valid_lines, filepath, line)
    if expected_old_line is not None:
        expected["old_line"] = expected_old_line
    if not is_new_file(new_files, filepath):
        expected["old_path"] = (old_paths or {}).get(filepath, filepath)

    problems = []

    # `True` and `61.0` both hash equal to the integer key, so a bool or a float line
    # number passes line validation AND the equality check below, reaching the wire in
    # its own spelling. Type is the only thing that separates them from the integer.
    new_line = position.get("new_line")
    if "new_line" in position and (
        isinstance(new_line, bool) or not isinstance(new_line, int)
    ):
        problems.append(f"new_line must be an integer, got {new_line!r}")

    for key in sorted(set(expected) - set(position)):
        problems.append(f"{key} is missing, expected {expected[key]!r}")
    for key in sorted(set(position) - set(expected)):
        problems.append(f"{key} must not be sent for this position")
    for key in sorted(set(position) & set(expected)):
        if position[key] != expected[key]:
            problems.append(f"{key} is {position[key]!r}, expected {expected[key]!r}")

    return problems


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
    are stripped, never spaces.

    PROSE fields only. ``suggested_fix_code`` has its own normalizer
    (:func:`_fix_code_text`) because stripping edge newlines off a PATCH silently
    changes what it replaces the span with.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if not value.strip():
        return None
    return value.strip("\n")


def _fix_code_text(value):
    """Normalize ``suggested_fix_code`` — the ONE normalizer for the patch.

    The gate measures this text and the fence carries this text, so stated ==
    checked == applied. Exactly ONE trailing ``"\\n"`` comes off — that is the
    file's line terminator, which the fence supplies itself — and nothing else
    does: a replacement stating a leading or a trailing BLANK line means it, and
    a fence that silently dropped one would commit different bytes than the gate
    approved.

    Whitespace-only input is still absent (``None``), the #47 semantics: a patch
    made of nothing but blanks is not representable and is not shipped. A
    non-string value is coerced via ``str()`` so the renderer cannot crash on a
    hand-assembled payload — the gate rejects it as ``non_string`` first.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if not value.strip():
        return None
    return value[:-1] if value.endswith("\n") else value


_RULE_TEXT_CAP = 500
_TRUNCATION_MARKER = "…[truncated]"

_GH_TOKEN_RE = re.compile(r"(?:ghp_|gho_|ghs_|ghr_|ghu_|github_pat_)[A-Za-z0-9_]{20,}")
_GL_TOKEN_RE = re.compile(r"(?:glpat-|glrt-)[A-Za-z0-9_\-]{20,}")

_ENTITY_DEC_RE = re.compile(r"&#(\d+);")
_ENTITY_HEX_RE = re.compile(r"&#x([0-9a-fA-F]+);", re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_HTML_COMMENT_UNTERMINATED_RE = re.compile(r"<!--[\s\S]*\Z")
_BACKTICK_RUN_RE = re.compile(r"`{3,}")

# Invisible / control code points stripped from outbound prose. Built as an
# explicit frozenset (not a regex character-class range) so CR (U+000D) is
# included while TAB/LF stay, and so CodeQL does not flag C0/C1 ranges as
# "overly permissive" (py/overly-large-range). Design: C0 minus \\t\\n, DEL,
# C1, soft hyphen, zero-width, bidi controls.
_INVISIBLE_ORDS = frozenset(
    (
        *range(0x00, 0x09),  # C0 through BS (excludes TAB)
        0x0B,  # VT
        0x0C,  # FF
        0x0D,  # CR — must strip: _blockquote splits only on \\n
        *range(0x0E, 0x20),  # rest of C0 (excludes LF, already skipped)
        0x7F,  # DEL
        *range(0x80, 0xA0),  # C1
        0xAD,  # soft hyphen
        0x200B,
        0x200C,
        0x200D,
        0xFEFF,
        0x2060,  # zero-width
        *range(0x202A, 0x202F),  # bidi embeddings/overrides
        *range(0x2066, 0x206A),  # bidi isolates
    )
)


def _strip_invisibles(text):
    """Remove C0/C1/zero-width/bidi controls; keep TAB and LF."""
    return "".join(ch for ch in text if ord(ch) not in _INVISIBLE_ORDS)


def _decode_numeric_entities(text):
    """Decode printable-ASCII numeric entities; drop all others.

    Named entities are left untouched. Non-ASCII numeric entities (e.g.
    ``&#8212;``) are dropped deliberately — decoding the full Unicode range
    would reintroduce smuggleable invisibles if pass order ever drifts.
    Markdown parses fences before HTML entity decode, so a surviving literal
    ``&#96;`` cannot form a fence.
    """

    def _dec(match):
        num = int(match.group(1), 10)
        if 32 <= num <= 126:
            return chr(num)
        return ""

    def _hex(match):
        num = int(match.group(1), 16)
        if 32 <= num <= 126:
            return chr(num)
        return ""

    text = _ENTITY_DEC_RE.sub(_dec, text)
    text = _ENTITY_HEX_RE.sub(_hex, text)
    return text


def _sanitize_outbound_prose(text):
    """Sanitize repo-derived / quoted prose before it enters a PR/MR comment.

    Order is load-bearing: decode entities first (so ``&#60;!--`` becomes a
    real comment), then strip terminated HTML comments, then unterminated
    ``<!--`` through EOS, then invisibles, then collapse backtick runs of
    length ≥3 to exactly two.
    """
    text = _decode_numeric_entities(text)
    text = _HTML_COMMENT_RE.sub("", text)
    text = _HTML_COMMENT_UNTERMINATED_RE.sub("", text)
    text = _strip_invisibles(text)
    text = _BACKTICK_RUN_RE.sub("``", text)
    return text


def _redact_secrets(text):
    """Replace prefixed credential-shaped tokens with ``[REDACTED]``.

    Prefixed formats only (GitHub + GitLab); no entropy heuristics. A prefix
    with no credential-shaped body (e.g. ``glpat-`` followed by space or a
    short token) survives; a prefix immediately followed by ≥20 hyphenated
    word chars is redacted.
    """
    text = _GH_TOKEN_RE.sub("[REDACTED]", text)
    text = _GL_TOKEN_RE.sub("[REDACTED]", text)
    return text


def _cap_rule_text(text, limit=_RULE_TEXT_CAP):
    """Hard-cap cited-rule text; marker is appended outside ``limit``."""
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_MARKER


def _prepared_prose(text, *, cap=False):
    """Sanitize and redact repo-derived prose; optionally cap cited-rule text.

    Returns ``None`` when the field is absent before or after processing.
    """
    text = _rendered_text(text)
    if not text:
        return None
    text = _redact_secrets(_sanitize_outbound_prose(text))
    if cap:
        text = _cap_rule_text(text)
    return _rendered_text(text)


def _blockquote(text):
    """Prefix every line for a markdown blockquote; bare ``>`` on blanks.

    Normalizes ``\\r\\n`` / lone ``\\r`` to ``\\n`` before splitting so a
    surviving CR cannot end a CommonMark line after a single ``>`` prefix
    (defense in depth on top of ``_strip_invisibles`` removing CR).
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    out = []
    for line in lines:
        if line:
            out.append(f"> {line}")
        else:
            out.append(">")
    return "\n".join(out)


def _suggestion_fence(payload):
    """Return ``(open, close)`` fence lines that contain ``payload``.

    Length is ``max(3, longest_backtick_run + 1)`` so CommonMark cannot close
    early. Platforms keep Apply for 4+ (GitHub confirmed; GitLab documents
    nesting with four backticks).
    """
    runs = re.findall(r"`+", payload)
    n = max(3, max((len(r) for r in runs), default=0) + 1)
    fence = "`" * n
    return f"{fence}suggestion", fence


# ---------------------------------------------------------------------------
# suggested_fix_code — the deterministic apply-check (issue #63)
# ---------------------------------------------------------------------------
# A ```suggestion fence is a COMMITTABLE patch: one click replaces the comment's
# apply range with its bytes, unreviewed. So it renders if and only if that range
# is EXACTLY the range the fence-owning finding states, at the specific render
# site, and every content check passes. Everything else downgrades to the prose
# `suggestion`, which a human reads before acting on — no finding is lost, only
# its one-click affordance.

_FIX_NON_STRING = "non_string"
_FIX_EMPTY = "empty"
_FIX_REDACTED = "redacted"
_FIX_MISSING_END_LINE = "missing_end_line"
_FIX_INVALID_RANGE = "invalid_range"
_FIX_NO_ORACLE = "no_diff_oracle"
_FIX_RANGE_NOT_IN_DIFF = "range_not_in_diff"
_FIX_ANCHOR_MISMATCH = "anchor_mismatch"
_FIX_NO_OP = "no_op_replacement"
_FIX_INDENTATION = "indentation_mismatch"
_FIX_TOO_LARGE = "replacement_too_large"
_FIX_CARRIAGE_RETURN = "carriage_return"

# The vocabulary is CLOSED: every downgrade names exactly one of these, in the
# stable warning `suggested-fix downgraded: {file}:{line} ({reason})`. Adding a
# reason is a deliberate act — a free-text reason would make the record
# unreadable in aggregate.
_FIX_REASONS = frozenset(
    {
        _FIX_NON_STRING,
        _FIX_EMPTY,
        _FIX_REDACTED,
        _FIX_MISSING_END_LINE,
        _FIX_INVALID_RANGE,
        _FIX_NO_ORACLE,
        _FIX_RANGE_NOT_IN_DIFF,
        _FIX_ANCHOR_MISMATCH,
        _FIX_NO_OP,
        _FIX_INDENTATION,
        _FIX_TOO_LARGE,
        _FIX_CARRIAGE_RETURN,
    }
)

# Delivery bound on fence content. `suggestion` prose is deliberately uncapped —
# a human reads it — but a fence is committed by one click, so it is bounded here
# unconditionally. The SAME two numbers bound the field upstream in the filter
# twins (scripts/filter_findings.py and workflows/src/filterFindings.js); change
# all three together.
#
# ONE definition of both measures, in all three homes: they are taken on the
# NORMALIZED text (`_fix_code_text` — the single terminating newline removed).
# Lines are the elements of that text's `split("\n")`; chars are its length in
# CODE POINTS. Measuring the raw string instead makes a 100-line patch count 101.
_FIX_MAX_LINES = 100
_FIX_MAX_CHARS = 8000

# Per-run patch-acceptance counters, reset by main() alongside _CAPTURED and
# _SKIP_WARNINGS. n/(n+m) over these two is the acceptance rate, deterministic
# and readable from any run's stdout at no cost.
_FIX_COUNTS = {"kept": 0, "downgraded": 0}


def _leading_whitespace_charset(lines):
    """Return the characters used in the LEADING whitespace of *lines*.

    A line with no leading whitespace contributes nothing: it says nothing about
    how the surrounding code indents.
    """
    charset = set()
    for line in lines:
        charset.update(line[: len(line) - len(line.lstrip(" \t"))])
    return charset


def _span_texts(line_texts, path_lookup, start, end):
    """Return the diff's new-side text for every line in ``[start, end]``.

    ``None`` when there is no complete answer — some line of the span is not a
    diff line. The content checks that consume this treat ``None`` as "no
    oracle", never as "no difference". (A missing ``line_texts`` mapping never
    reaches here: the gate fails the whole patch closed on it first.)
    """
    texts = []
    for n in range(start, end + 1):
        if (path_lookup, n) not in line_texts:
            return None
        texts.append(line_texts[(path_lookup, n)])
    return texts


def _suggested_fix_gate(finding, *, apply_range, line_texts, valid_lines, path_lookup):
    """Return ``(ok, reason)`` for *finding*'s ``suggested_fix_code`` at ONE render site.

    Pure — no I/O, no subprocess — so it runs identically under --dry-run and live.
    That is ``validate_position``'s precedent: a check that runs only under
    --dry-run cannot be the thing that makes --dry-run trustworthy, and a caller's
    hand-assembled JSON goes through this same one path, not a lenient fork.

    *apply_range* is the ``(start, end)`` this site's one-click apply really
    replaces, or ``None`` where a fence can never apply at all (a position-less
    note, the degraded body section). *path_lookup* is the finding's path in the
    DIFF's own spelling (``diff_path_spelling``), so this gate and the anchor
    validation consult the same keys and cannot disagree.

    First failure wins, in the order written below; ``reason`` is always a member
    of ``_FIX_REASONS``.

    When diff validation was skipped (``valid_lines`` / ``line_texts`` are
    ``None`` — an unknown platform, or a failed diff fetch) this FAILS CLOSED
    with ``no_diff_oracle``, deliberately unlike the anchor. The anchor fails
    open there because a wrong anchor costs a misplaced comment a human reads
    and ignores; a patch cannot, because a wrong patch is committed by one click
    and corrupts the file. The fallback is free: the prose ``suggestion`` still
    carries the same content, so nothing is lost but the affordance.
    """
    if "suggested_fix_code" not in finding:
        return True, None

    raw = finding["suggested_fix_code"]
    if not isinstance(raw, str):
        return False, _FIX_NON_STRING
    text = _fix_code_text(raw)
    if text is None:
        return False, _FIX_EMPTY
    # CommonMark treats a lone \r (not just \r\n) as a line ending, so
    # "foo\rbar\rbaz" renders as THREE fence lines and applies as three lines —
    # but it is ONE element to this gate's `split("\n")`, which never checks for
    # \r. That let a CR-joined patch dodge the no-op, indentation, and
    # _FIX_MAX_LINES measurements entirely: none of them saw the document a
    # one-click apply actually commits. Fail closed on any interior CR before
    # those measurements run; the prose `suggestion` still carries the fix, and
    # a genuinely CRLF-terminated patch is exactly the ambiguous case a
    # one-click apply must not ship.
    if "\r" in text:
        return False, _FIX_CARRIAGE_RETURN
    # Gate on the ORIGINAL bytes: a fence carrying a literal `[REDACTED]` would be
    # committed by one click. Passing here means the render-time redaction is a
    # guaranteed no-op, so the posted fence is byte-identical to what was checked.
    if _redact_secrets(text) != text:
        return False, _FIX_REDACTED

    line = finding.get("line")
    end_line = finding.get("end_line")
    if end_line is None:
        # A patch's stated range must be explicit. An absent end_line — including
        # one #205 DELETED for exceeding maxLineSpan — is exactly how a multi-line
        # replacement lands on a single-line anchor and corrupts the file.
        return False, _FIX_MISSING_END_LINE
    if (
        isinstance(line, bool)
        or not isinstance(line, int)
        or isinstance(end_line, bool)
        or not isinstance(end_line, int)
        or line < 1
        or end_line < line
    ):
        # `True` and `2.0` both hash equal to the integer key, so they survive every
        # lookup — the trap validate_position documents.
        return False, _FIX_INVALID_RANGE
    if not isinstance(valid_lines, dict) or not isinstance(line_texts, dict):
        # No diff was parsed, so every check below has nothing to consult. Both
        # mappings are required: one alone answers only half of "is this range
        # real" / "does this patch change anything".
        return False, _FIX_NO_ORACLE
    if not _range_is_valid(valid_lines, path_lookup, line, end_line):
        return False, _FIX_RANGE_NOT_IN_DIFF
    if apply_range != (line, end_line):
        return False, _FIX_ANCHOR_MISMATCH

    replacement = text.split("\n")
    span = _span_texts(line_texts, path_lookup, line, end_line)
    if span is None:
        # _range_is_valid/is_line_valid tolerate a per-line mix of path-spelling
        # variants (the exact diff key, or its a/ b/ -stripped form), but a span
        # needs ONE spelling to hold for every line in it — so a mixed-spelling
        # range makes this None even though every line individually validated.
        # That is still "no oracle", not "no difference": treating it as the
        # latter would silently skip the no-op/indentation checks below instead
        # of failing the patch closed.
        return False, _FIX_NO_ORACLE
    # A trailing CR is transport (a CRLF diff carries one on every line), not
    # content, and `_fix_code_text` already took the replacement's terminating
    # newline off — so neither side's line terminators decide this. An EDGE
    # BLANK LINE survives that normalization and is compared as content: a
    # patch that only adds one is a change, not a no-op.
    if [ln.rstrip("\r") for ln in replacement] == [ln.rstrip("\r") for ln in span]:
        return False, _FIX_NO_OP
    span_indent = _leading_whitespace_charset(span)
    fix_indent = _leading_whitespace_charset(replacement)
    # Deliberately weak, and language-agnostic: a legitimate re-indentation
    # passes, and only a tab/space charset conflict — the one that silently
    # corrupts a file whichever language it is written in — is caught.
    if (span_indent == {" "} and "\t" in fix_indent) or (
        span_indent == {"\t"} and " " in fix_indent
    ):
        return False, _FIX_INDENTATION
    if len(replacement) > _FIX_MAX_LINES or len(text) > _FIX_MAX_CHARS:
        return False, _FIX_TOO_LARGE
    return True, None


def _gated_finding(finding, apply_range, valid_lines, line_texts):
    """Return the finding to RENDER at one site, gating its ``suggested_fix_code``.

    A failure strips the field from a SHALLOW COPY — the copy is what gets
    rendered, so ``render_comment_body`` itself is untouched (the benchmark calls
    it directly and pins its bytes) and the prose ``suggestion`` carries the fix
    instead. Each downgrade is recorded through ``warn_skip``, which both prints
    and lands in the dry-run payload's existing ``skipped`` list, and counted for
    the run's patch-acceptance readout.

    Called at every site where a fence can actually render. A corroborator inside
    a group body is not such a site — ``_render_corroboration`` renders no fence
    (nor the prose suggestion) at all — so it is neither gated nor counted here.
    """
    if not isinstance(finding, dict) or "suggested_fix_code" not in finding:
        return finding
    ok, reason = _suggested_fix_gate(
        finding,
        apply_range=apply_range,
        line_texts=line_texts,
        valid_lines=valid_lines,
        path_lookup=diff_path_spelling(
            valid_lines, finding.get("file", "?"), finding.get("line")
        ),
    )
    if ok:
        _FIX_COUNTS["kept"] += 1
        return finding
    if reason not in _FIX_REASONS:
        # A typo'd reason string in a future gate edit must fail loudly at the
        # first downgrade, not silently record garbage in the stable warning
        # line (whose readers rely on the vocabulary being closed).
        raise ValueError(f"_suggested_fix_gate returned an unknown reason: {reason!r}")
    _FIX_COUNTS["downgraded"] += 1
    warn_skip(
        f"suggested-fix downgraded: {finding.get('file', '?')}:"
        f"{finding.get('line')} ({reason})"
    )
    stripped = dict(finding)
    del stripped["suggested_fix_code"]
    return stripped


def _degraded_entry(filepath, line, finding, valid_lines, line_texts):
    """One entry for the body section, which has no anchor to apply against.

    Shared by both posters: a body-section entry never has an apply range, so
    ``_gated_finding`` is always called with ``None`` here regardless of
    platform.
    """
    return filepath, line, _gated_finding(finding, None, valid_lines, line_texts)


def _key_material_finding(finding):
    """Return the copy whose render seeds a DELIVERY KEY.

    ``suggested_fix_code`` comes off UNCONDITIONALLY — not gated — so a key does
    not depend on the field at all: it is the same whether the finding ships
    grouped or individually, the same whichever way the apply-check went, and
    byte-equal to the key a pre-#63 run computed for the same finding.
    Prior-delivery dedup (#132/#208) is retry-safe only while keys are stable
    across runs and across delivery shapes; making the GATE deterministic would
    not be enough, because the gate's inputs (the diff, the render site) are not.
    """
    if not isinstance(finding, dict) or "suggested_fix_code" not in finding:
        return finding
    stripped = dict(finding)
    del stripped["suggested_fix_code"]
    return stripped


def _print_fix_summary():
    """Print the run's patch-acceptance readout, or nothing.

    These two lines are the APPLY-CHECK's verdicts at this run's render sites,
    and nothing else — they carry no delivery verb, and read identically live
    and under --dry-run. Delivery is the per-platform count lines' business: a
    GitLab rerun whose discussions are all already on the MR renders (and so
    gates, and so counts) every fence while posting nothing, and "N fences
    posted" was a false claim there. A run that renders nothing counts nothing
    and prints nothing.

    Both halves print together whenever ANY finding carried the field, so
    n/(n+m) is readable from any run's stdout.
    """
    kept = _FIX_COUNTS["kept"]
    downgraded = _FIX_COUNTS["downgraded"]
    if not (kept or downgraded):
        return
    print(f"  {kept} suggested fix(es) passed the apply-check.")
    print(f"  {downgraded} suggested fix(es) downgraded to prose.")


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
    suggested_fix = _fix_code_text(finding.get("suggested_fix_code"))

    parts = [f"**{emoji} [{severity.upper()}] {title}**", "", body]

    # Prose fix suggestion (issue #47 / #122). Agent-authored: sanitize +
    # redact, uncapped. Structural sanitize only — not the cited-rule cap.
    suggestion_text = _prepared_prose(finding.get("suggestion"))
    if suggestion_text:
        parts += ["", "**Suggested fix:**", suggestion_text]

    # Cited rule (issue #47 / #122). Repo-derived: sanitize → redact → cap →
    # blockquote. Each candidate is prepared independently; `claude_md_rule`
    # wins only when it survives sanitize (comment-only rules fall through).
    rule_text = _prepared_prose(finding.get("claude_md_rule"), cap=True)
    if not rule_text:
        rule_text = _prepared_prose(finding.get("spec_text"), cap=True)
    if rule_text:
        parts += ["", "**Cited rule:**", _blockquote(rule_text)]

    # `criticality`, `failure_scenario`, `evidence`, `confidence`, and
    # `dimension` are deliberately NOT rendered into posted PR comments
    # (issue #47) — they are scoped to the artifact/report consumers, not
    # this deterministic comment renderer. Do not "helpfully" add them here.

    # suggested_fix_code: secret-redacted; outer fence lengthened. Structural
    # sanitize OFF so one-click apply stays byte-exact aside from credential
    # redaction (deliberate exception). This renderer does NOT decide whether the
    # patch may ship — `_suggested_fix_gate` does, at each render site, and hands
    # this function a copy with the field already removed when it may not. Keeping
    # the decision out of here is what lets one finding render with a fence inline
    # and without one in the degraded body section of the same review.
    #
    # The fence carries `_fix_code_text`'s output — the SAME normalization the
    # gate measured, applied ONCE. Re-normalizing after redaction would take a
    # second trailing newline off, so the posted bytes would differ from the
    # checked ones; redaction only ever substitutes a token, never empties.
    if suggested_fix:
        suggested_fix = _redact_secrets(suggested_fix)
        open_f, close_f = _suggestion_fence(suggested_fix)
        parts += ["", open_f, suggested_fix, close_f]

    return "\n".join(parts)


def consolidate_delivery(findings):
    """Group *findings* for the posted delivery payload (#22 D2).

    Findings stay distinct in the caller's array — this only groups them for
    rendering. A finding carrying a truthy ``consolidation_key`` joins the group
    for that key; ``consolidation_primary: true`` marks which member anchors the
    group's single posted comment. A finding with no (or falsy) key becomes its
    own single-member group — this is what keeps output byte-identical to today
    for findings without stamps (older artifacts, degraded pipelines).

    Returns a list of ``{"primary": finding, "corroborators": [finding, ...]}``
    dicts, one per group, in the order each group's FIRST member appears in
    *findings* — deterministic regardless of which member within a group is
    the primary.
    """
    groups = []
    key_to_group = {}
    for f in findings:
        key = f.get("consolidation_key") if isinstance(f, dict) else None
        if not key:
            groups.append({"primary": f, "corroborators": []})
            continue
        group = key_to_group.get(key)
        if group is None:
            group = {"primary": None, "corroborators": []}
            key_to_group[key] = group
            groups.append(group)
        if f.get("consolidation_primary"):
            if group["primary"] is None:
                group["primary"] = f
            else:
                # A second consolidation_primary in the same group must not
                # overwrite (and drop) the first — demote it to corroborator.
                group["corroborators"].append(f)
        else:
            group["corroborators"].append(f)
    # Reachable only for hand-assembled payloads: filter_findings.py /
    # filterFindings.js always stamp exactly one consolidation_primary per
    # group. If a caller's data has none, don't drop the group's first-seen
    # member — treat it as the primary rather than surface `None`.
    for group in groups:
        if group["primary"] is None and group["corroborators"]:
            group["primary"] = group["corroborators"].pop(0)
    return groups


def _render_corroboration(finding):
    """Render one non-primary group member as a corroborating section."""
    agent = finding.get("agent", "unknown")
    dimension = finding.get("dimension", "unknown")
    confidence = finding.get("confidence")
    conf_text = str(confidence) if confidence is not None else "?"
    title = finding.get("title", "Finding")
    body = finding.get("body", "")
    parts = [
        f"**Corroborating finding — {agent} ({dimension}, confidence {conf_text}):**",
        "",
        f"**{title}**",
    ]
    if body:
        parts += ["", body]
    return "\n".join(parts)


def render_group_body(primary, corroborators):
    """Build the markdown comment body for one consolidation group.

    Renders *primary* exactly as ``render_comment_body`` always has — with no
    *corroborators* this is byte-identical to today, which is what keeps
    unstamped findings (older artifacts, degraded pipelines) unaffected.
    Each corroborator is appended as its own section; that appended text is
    finding-controlled, so it is run through the same ``<!--`` neutralization
    ``build_skipped_section`` applies (see its docstring) — the primary's own
    render is deliberately left alone, matching its existing raw-on-the-wire
    behavior.
    """
    body = render_comment_body(primary)
    if not corroborators:
        return body
    section = "\n\n".join(_render_corroboration(c) for c in corroborators)
    section = section.replace("<!--", "&lt;!--")
    return f"{body}\n\n---\n\n{section}"


def build_skipped_section(skipped, inline_count=None):
    """Render the trailing section for findings that could not be anchored inline.

    *skipped* is a list of ``(filepath, line, finding)`` tuples — the finding plus the
    ``filepath``/``line`` it was to be anchored at (``line`` is ``None`` for a finding
    that carried no line at all). A member is not always here for its OWN reason: a
    consolidation group whose primary could not be anchored (no line, or a line the
    diff doesn't touch) degrades as a whole (#22 D2) — a corroborator can appear here
    with a perfectly valid line of its own, because its group's primary is what
    failed. *inline_count*, when given, is how many comments
    landed — GitHub passes ``len(comments)`` because its one POST is atomic, so the
    number is exact by the time this runs. GitLab passes nothing: its summary note
    posts BEFORE the per-finding loop, so how many will actually land (some may still
    fail, dedupe, or turn out malformed) is not yet known, and claiming a number here
    would be a promise the loop below has not kept yet. Returns ``""`` for an empty
    *skipped* list (issue #192).

    Every finding's title/body reaches the wire RAW (only ``suggestion`` /
    ``claude_md_rule`` / ``spec_text`` go through ``_sanitize_outbound_prose``), and
    that applies to every field a finding can control here — not just the rendered
    comment body but also its ``file``/``line``, which are interpolated raw into each
    entry's ``#### `path:line` `` heading. So a finding can carry
    ``<!-- code-gauntlet-finding-key: ... -->`` verbatim in ANY of those fields.
    Unlike an inline discussion body — where the mechanical marker is APPENDED after
    the comment and so always shadows a forged one (see review_marker.py) — nothing
    mechanical follows a finding's rendered text here, so a forged marker would parse
    as a real, currently-undelivered "already posted" signal on the next run. The
    section legitimately contains no ``<!--`` anywhere of its own, so every ``<!--``
    in the WHOLE composed section — headings included, not just each finding's
    rendered body — is neutralized to ``&lt;!--`` in one pass at the end, closing
    every field (present or future) by construction rather than per-field.
    """
    if not skipped:
        return ""
    n = len(skipped)
    group_note = (
        " A finding listed here may not have an anchoring problem of its own — a "
        "consolidation group whose primary could not be anchored inline is listed "
        "here in full, corroborators included."
    )
    if inline_count is None:
        intro = (
            f"The following {n} finding(s) reference lines outside this diff and are "
            f"included here instead of as inline comments:{group_note}"
        )
    else:
        intro = (
            f"{inline_count} inline comment(s) were posted; the following {n} "
            "finding(s) reference lines outside this diff and are included here "
            f"instead:{group_note}"
        )
    lines = [
        "",
        "---",
        "",
        f"### ⚠️ {n} finding(s) could not be anchored inline",
        "",
        intro,
    ]
    for filepath, line, finding in skipped:
        location = f"{filepath}:{line}" if line is not None else str(filepath or "?")
        lines += ["", f"#### `{location}`", "", render_comment_body(finding)]
    return "\n".join(lines).replace("<!--", "&lt;!--")


def finding_key(filepath, line, title, body):
    """Return the 16-hex delivery key recorded in a posted inline discussion's marker.

    Derived from what a reader can see on the wire — the path and line the comment is
    anchored to, the title, and the RENDERED body — never from an upstream finding
    ``id``: the input schema documented at the top of this file does not require one, so
    keying on it would make dedup depend on a field a caller need not supply. Content
    derivation also gives the right answer when it changes: an edited finding is a
    different comment and gets posted, rather than being suppressed as "already there".
    """
    material = "\x00".join((str(filepath), str(line), str(title), body))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


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


def post_github(data, valid_lines, line_texts):
    # Both oracles are REQUIRED arguments, no defaults: `parse_diff_lines` returns
    # them together or not at all, and a caller that omitted `line_texts` used to
    # silently disable half the apply-check (every fence then downgrading for a
    # reason the diff would have answered). Pass what the parser returned.
    owner = data["owner"]
    repo = data["repo"]
    pr_number = data["pr_number"]
    findings = data.get("findings", [])

    check_tool("gh")

    comments = []
    skipped_entries = []  # (filepath, line, finding) — line is None for a no-line skip
    # One posted comment per consolidation group (#22 D2): findings without a stamp
    # are each their own single-member group, so this loop is unchanged for them.
    for group in consolidate_delivery(findings):
        primary = group["primary"]
        corroborators = group["corroborators"]
        line = primary.get("line")
        if line is None:
            warn_skip(
                f"Finding '{primary.get('title', '?')}' has no line number — skipping."
            )
            skipped_entries.append(
                _degraded_entry(
                    primary.get("file", "?"), None, primary, valid_lines, line_texts
                )
            )
            # The primary can't anchor, so the whole group degrades into the
            # skipped section as individual entries — the corroborators never
            # merged into a comment that itself never gets posted.
            for c in corroborators:
                skipped_entries.append(
                    _degraded_entry(
                        c.get("file", "?"), c.get("line"), c, valid_lines, line_texts
                    )
                )
            continue

        filepath = primary["file"]
        if not is_line_valid(valid_lines, filepath, line):
            diag = ""
            vl = valid_lines_for_file(valid_lines, filepath)
            if vl is not None:
                diag = f" Valid lines for this file: {vl}"
            warn_skip(
                f"Skipping finding '{primary.get('title', '?')}' at {filepath}:{line} "
                f"— line not found in diff.{diag}"
            )
            skipped_entries.append(
                _degraded_entry(filepath, line, primary, valid_lines, line_texts)
            )
            for c in corroborators:
                skipped_entries.append(
                    _degraded_entry(
                        c.get("file", "?"), c.get("line"), c, valid_lines, line_texts
                    )
                )
            continue

        # The multi-line anchor decision is made HERE, ABOVE the body render, and
        # the assembly below consumes these same locals — the decision moved, it is
        # not duplicated. The apply-check has to see the range the comment will
        # REALLY apply at, and that range is only known once this has run (#63 D2).
        #
        # start_line is added for multi-line comments, but only when the whole range
        # sits inside one hunk — GitHub rejects the ENTIRE review POST (losing every
        # finding, not just this one) with a 422 "Line could not be resolved" if
        # end_line falls outside every hunk, even though `line` alone was valid.
        end_line = primary.get("end_line")
        multiline = (
            isinstance(end_line, int)
            and end_line >= line
            and end_line != line
            and _range_is_valid(valid_lines, filepath, line, end_line)
        )
        # A group comment anchors on the PRIMARY's range and only the primary's
        # fence exists in the body (`_render_corroboration` emits none), so this is
        # the apply range of every fence the comment can carry.
        apply_range = (line, end_line) if multiline else (line, line)
        comment = {
            "path": filepath,
            "line": line,
            "side": "RIGHT",
            "body": render_group_body(
                _gated_finding(primary, apply_range, valid_lines, line_texts),
                corroborators,
            ),
        }
        if multiline:
            comment["start_line"] = line
            comment["start_side"] = "RIGHT"
            comment["line"] = end_line

        comments.append(comment)

    # The partition (comments vs skipped) is complete above — compose the section
    # BEFORE the footer, which must stay last (it is the machine-parsed marker). The
    # footer is computed against the ORIGINAL body, not the body plus the section: a
    # skipped finding's raw title/body can plant text that looks like the mechanical
    # prose footer or the summary marker (only suggestion/claude_md_rule/spec_text are
    # sanitized), and build_footer's own-signal dedup would read that forgery as
    # already-present and omit the real one.
    skipped_section = build_skipped_section(skipped_entries, len(comments))
    sha = resolve_marker_sha(data)
    review_body = data.get("review_body", "")
    footer = build_footer(len(findings), sha, body=review_body)
    review_body += skipped_section + footer

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
    if skipped_entries:
        print(
            f"  {len(skipped_entries)} finding(s) skipped inline (lines not in diff) — "
            "appended to review body."
        )
    _print_fix_summary()
    return 0


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


def gitlab_prior_delivery(owner, repo, mr_iid, sha):
    """Return ``(summary_posted, finding_keys, legacy_group_keys)`` — what THIS sha's
    review already left.

    Makes a rerun after a partial delivery retry-safe in three ways: the summary note is
    not stacked a second time (issue #127 D4), the inline discussions that did land are
    not reposted (issue #132), and a pre-#208 group body that rendered a corroborator's
    content without ever keying it is recognized as already carrying that member too (see
    :func:`detect_prior_review.legacy_group_keys_for_sha`) rather than posted a second
    time. ONE fetch serves all three, in detect_prior_review.py — the only reader of the
    signals — so this module stays write-only.

    Never blocks delivery. Dry-run does not fetch AT ALL: dry-run's invariant is that it
    issues no WRITE calls (reads do happen under dry-run — `glab mr diff` and the
    versions fetch both run), and the DRY_RUN guard here exists so dry-run adds no READ
    either. That keeps `_CAPTURED[0]` the summary, which build_dry_run_payload's "the
    first capture is the summary" shape depends on, and keeps every finding captured
    rather than deduped away. A marker sha that is not SHA-shaped (get_head_sha's
    "unknown" fallback) is not a usable dedup key. A failed fetch warns and delivers
    everything — a possible duplicate beats a silently dropped review.
    """
    if DRY_RUN or not is_sha_shaped(sha):
        return False, frozenset(), frozenset()
    summary_posted, keys, legacy_group_keys, error = gitlab_prior_delivery_state(
        owner, repo, mr_iid, sha
    )
    if error:
        warn(
            f"could not check for an existing summary note or already-delivered inline "
            f"discussions ({error}); posting them."
        )
        return False, frozenset(), frozenset()
    return summary_posted, keys, legacy_group_keys


def post_gitlab(data, valid_lines, new_files, old_paths, line_texts):
    # Every parsed-diff argument is REQUIRED — see post_github's note: defaults
    # let a caller disable half the apply-check by omission.
    owner = data["owner"]
    repo = data["repo"]
    mr_iid = data["pr_number"]
    findings = data.get("findings", [])

    check_tool("glab")

    project_id = gitlab_project_id(owner, repo)
    shas = fetch_gitlab_shas(project_id, mr_iid)
    base_sha, head_sha, start_sha = shas

    # fetch_gitlab_shas dies when the FETCH fails but never inspects the field values. An
    # empty sha is a loop-invariant configuration failure — every position built below
    # carries the same three — so it is reported ONCE, here, before the summary note
    # lands on the MR, rather than as N per-finding rejections after it.
    for name, value in (
        ("base_sha", base_sha),
        ("head_sha", head_sha),
        ("start_sha", start_sha),
    ):
        if not isinstance(value, str) or not value.strip():
            die(
                f"MR version {name} is {value!r} — every inline position would be "
                f"rejected. Check that the MR has a version carrying all three SHAs."
            )

    def anchored(finding, line):
        """Gate a finding for a GitLab inline body.

        GitLab anchors are ALWAYS single-line and this script never emits the
        ``suggestion:-m+n`` multi-line syntax (#63 D9), so one anchored line is the
        whole apply range — a finding stating any wider span is downgraded.
        """
        return _gated_finding(finding, (line, line), valid_lines, line_texts)

    # Pre-partition the deterministic skips (no line number, or a line the diff never
    # touched) BEFORE the summary note is composed — the note is posted first, so the
    # skipped section must already be known. Both checks are pure functions of facts
    # already fetched above (valid_lines, the finding's own file/line), so this is
    # exactly the decision the inline loop below would make; it is just made early for
    # the findings that will never reach that loop. `remaining` carries each finding's
    # resolved filepath through to the loop so it is not re-derived.
    skipped_entries = []  # (filepath, line, finding) — line is None for a no-line skip
    remaining = []  # (filepath, group) — groups that reach the inline loop
    skipped = 0
    # One posted discussion per consolidation group (#22 D2): findings without a
    # stamp are each their own single-member group, so this loop is unchanged
    # for them.
    for group in consolidate_delivery(findings):
        primary = group["primary"]
        corroborators = group["corroborators"]
        line = primary.get("line")
        if line is None:
            warn_skip(
                f"Finding '{primary.get('title', '?')}' has no line number — skipping."
            )
            skipped_entries.append(
                _degraded_entry(
                    primary.get("file", "?"), None, primary, valid_lines, line_texts
                )
            )
            skipped += 1
            # The primary can't anchor, so the whole group degrades into the
            # skipped section as individual entries.
            for c in corroborators:
                skipped_entries.append(
                    _degraded_entry(
                        c.get("file", "?"), c.get("line"), c, valid_lines, line_texts
                    )
                )
                skipped += 1
            continue

        # Same spelling resolution the loop below applies — see its comment.
        filepath = diff_path_spelling(valid_lines, primary["file"], line)
        if not is_line_valid(valid_lines, filepath, line):
            diag = ""
            vl = valid_lines_for_file(valid_lines, filepath)
            if vl is not None:
                diag = f" Valid lines for this file: {vl}"
            warn_skip(
                f"Skipping finding '{primary.get('title', '?')}' at {filepath}:{line} "
                f"— line not found in diff.{diag}"
            )
            skipped_entries.append(
                _degraded_entry(filepath, line, primary, valid_lines, line_texts)
            )
            skipped += 1
            for c in corroborators:
                skipped_entries.append(
                    _degraded_entry(
                        c.get("file", "?"), c.get("line"), c, valid_lines, line_texts
                    )
                )
                skipped += 1
            continue

        remaining.append((filepath, group))

    sha = resolve_marker_sha(data)
    review_body = data.get("review_body", "")
    # Footer computed against the ORIGINAL body — see the matching comment in
    # post_github for why a skipped finding's raw text must not be able to suppress it.
    footer = build_footer(len(findings), sha, body=review_body)
    review_body += build_skipped_section(skipped_entries) + footer

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
    summary_posted, delivered_keys, legacy_group_keys = gitlab_prior_delivery(
        owner, repo, mr_iid, sha
    )
    # Same predicate that makes gitlab_prior_delivery skip the fetch: a marker built
    # from a non-SHA-shaped sha (get_head_sha's "unknown" fallback) is one
    # find_finding_marker is guaranteed to reject, so appending it would leave an
    # unreadable comment on every discussion and dedup nothing.
    sha_is_markable = is_sha_shaped(sha)
    if summary_posted:
        print(f"MR summary note for {sha} already on the MR — skipping.")
    else:
        post_json(cmd_prefix, summary_payload)
        print(
            "MR summary note captured (dry-run)."
            if DRY_RUN
            else "MR summary note posted."
        )

    # Post each finding as an inline discussion. Every finding lands in exactly one of
    # the five counters below, so the outcome reported at the end is a partition of
    # `findings` — a run cannot both under-report and claim success. `skipped` was
    # already counted in the pre-partition above (its decisions are identical to what
    # this loop would make, made early so the skipped section can precede the note);
    # `remaining` carries only what that pre-partition let through, filepath already
    # resolved.
    def member_key(m, filepath, line):
        """Return the delivery key for ONE finding, independent of what posts it.

        Always derived from the member's own anchor and its SINGLE-finding render,
        never from the group body it may happen to ride in. That is what makes the
        two delivery shapes interchangeable for dedup: a corroborator posted on its
        own by one run is recognized by the group discussion of the next, and vice
        versa (issue #132).

        The key render also drops ``suggested_fix_code`` UNCONDITIONALLY (see
        :func:`_key_material_finding`), so a key is fence-independent: the apply-check
        can strip a fence here and keep it there without ever moving a delivery key.
        """
        return finding_key(
            filepath,
            line,
            m.get("title", ""),
            render_comment_body(_key_material_finding(m)),
        )

    def deliver(f, filepath, line, comment_body, keys):
        """Post ONE discussion and return its outcome counter name.

        The group's single discussion and each fallback per-corroborator
        discussion go through this same validate → POST → dedup → count path,
        so a corroborator that is posted on its own is accounted for exactly
        like any never-grouped finding. *keys* is the delivery key of every
        finding this one discussion carries — the caller owns the mapping,
        because only it knows which findings share a body.
        """
        if keys and all(k in delivered_keys for k in keys):
            # An earlier run already delivered every finding in this discussion for
            # this sha. Reposting it is the duplication issue #132 reports, not a
            # failure. A PARTIAL match never reaches here: post_gitlab splits such a
            # group into its missing members before calling.
            return "already_present"

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

        problems = validate_position(
            position, shas, valid_lines, new_files, old_paths, filepath, line
        )
        if problems:
            warn_skip(
                f"Skipping finding '{f.get('title', '?')}' at {filepath}:{line} "
                f"— malformed GitLab position: {'; '.join(problems)}."
            )
            return "invalid"

        # The delivery marker goes on the LIVE wire only. The benchmark harness pins
        # dry-run and scores the captured bodies as candidate text, so a marker in a
        # capture would change what is scored; the live path is the only place a rerun
        # has to recognize what it already posted.
        # ONE marker per finding carried, so a rerun's key scan registers every
        # member of a group as delivered — not just the finding that anchored it.
        markers = "\n".join(build_finding_marker(sha, k) for k in keys)
        payload = {
            "body": comment_body
            if DRY_RUN or not sha_is_markable
            else f"{comment_body}\n\n{markers}",
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
            return "failed"
        return "posted"

    def member_key_for(m, anchor):
        """Return the delivery key for ONE group member, anchored or not.

        An anchorable member's key is `member_key` at its own resolved
        position — the same key its individual fallback discussion would
        carry. A member with no anchor (no line, or a line outside the diff)
        can only ever be delivered inside its group's body, so it has no
        resolved position to key on; it is keyed on its own raw, unresolved
        `file`/`line` instead. That is deterministic and is exactly what the
        group body's marker for this member must also use — the round-trip
        `deliver()` relies on for every member key.
        """
        if anchor:
            return member_key(m, *anchor)
        return member_key(m, m.get("file", "?"), m.get("line"))

    def deliver_unanchored(c, key):
        """Post an unanchorable corroborator's content as a position-less MR note.

        Reached only when *c* has no line to anchor an inline discussion on
        (or its line is outside the diff) AND the group's single discussion
        already delivered its anchored siblings on an earlier run — the group
        body cannot be reposted without duplicating those siblings, so this is
        the only delivery vehicle left. Mirrors the summary note's own
        position-less POST to the same `/notes` endpoint, with the same
        per-finding marker `deliver()` appends to an inline discussion, so a
        later rerun recognizes this exactly as it would an inline one.

        A position-less note has no anchor at all, so no fence it carried could
        ever be applied — the gate below strips one unconditionally here.
        """
        body = render_comment_body(_gated_finding(c, None, valid_lines, line_texts))
        payload = {
            "body": body
            if DRY_RUN or not sha_is_markable
            else f"{body}\n\n{build_finding_marker(sha, key)}"
        }
        cmd_prefix = [
            "glab",
            "api",
            "--method",
            "POST",
            "--header",
            "Content-Type: application/json",
            f"projects/{project_id}/merge_requests/{mr_iid}/notes",
        ]
        _response, error = try_post_json(cmd_prefix, payload)
        if error is not None:
            warn_skip(
                f"Skipping corroborating finding '{c.get('title', '?')}' — GitLab "
                f"rejected the position-less note.\n{error}"
            )
            return "failed"
        return "posted"

    def corroborator_anchor(c):
        """Return ``(filepath, line)`` a corroborator can be anchored at on its own.

        ``None`` when it cannot be — no line, or a line the diff does not touch.
        Silent: this is the predicate, and :func:`deliver_corroborator` is the
        delivery that reports the same losses to the operator.
        """
        line = c.get("line")
        if line is None:
            return None
        filepath = diff_path_spelling(valid_lines, c.get("file", "?"), line)
        if not is_line_valid(valid_lines, filepath, line):
            return None
        return filepath, line

    def deliver_corroborator(c):
        """Fall back to a corroborator's OWN individual discussion.

        Reached when the group's discussion is lost late (malformed primary
        position, or a rejected POST), and when a partially-delivered group is
        split into the members an earlier run did not deliver. Its file/line are
        resolved and gated here exactly as the pre-partition gates a primary's.
        """
        line = c.get("line")
        title = c.get("title", "?")
        if line is None:
            warn_skip(
                f"Skipping corroborating finding '{title}' — no line number to "
                f"anchor its own discussion on."
            )
            return "invalid"
        filepath = diff_path_spelling(valid_lines, c.get("file", "?"), line)
        if not is_line_valid(valid_lines, filepath, line):
            warn_skip(
                f"Skipping corroborating finding '{title}' at {filepath}:{line} "
                f"— line not found in diff."
            )
            return "invalid"
        return deliver(
            c,
            filepath,
            line,
            render_comment_body(anchored(c, line)),
            [member_key(c, filepath, line)],
        )

    counters = {
        "posted": 0,
        "already_present": 0,
        "invalid": 0,
        "failed": 0,
    }
    for filepath, group in remaining:
        f = group["primary"]
        corroborators = group["corroborators"]
        primary_key = member_key(f, filepath, f["line"])
        # Every member gets a key — even one with no anchor of its own, which can
        # only ever be delivered by its group's body (see member_key_for). Without
        # this, an unanchorable corroborator's key was simply absent, so the
        # all-keys-present check below could never see it as missing: the group
        # was declared already_present while that member's content had never
        # landed anywhere (unanchored corroborators lost on rerun).
        anchors = {id(c): corroborator_anchor(c) for c in corroborators}
        corroborator_keys = {
            id(c): member_key_for(c, anchors[id(c)]) for c in corroborators
        }
        member_keys = [primary_key] + [corroborator_keys[id(c)] for c in corroborators]
        if primary_key in legacy_group_keys:
            # This group's primary key was found on a pre-#208 group body that
            # rendered a corroborator's content without ever giving it a key of
            # its own (see legacy_group_keys_for_sha). That body IS this group's
            # whole delivery — every member it renders is provably already on
            # the MR, missing keys included — so treat the whole group as
            # already_present rather than let the "some but not all" branch
            # below post the missing member a second time.
            counters["already_present"] += len(member_keys)
            continue
        if any(k in delivered_keys for k in member_keys) and not all(
            k in delivered_keys for k in member_keys
        ):
            # An earlier run delivered SOME of this group — its fallback posted
            # individual discussions for part of it. Posting the group now would put
            # that content on the MR twice (issue #132), so deliver only what is
            # missing, each on its own. A missing member without an anchor cannot
            # get its own inline discussion (deliver_corroborator requires a line),
            # so it is posted as a position-less note instead — the group body it
            # would otherwise ride in was already delivered by the earlier run.
            counters[
                "already_present"
                if primary_key in delivered_keys
                else deliver(
                    f,
                    filepath,
                    f["line"],
                    render_comment_body(anchored(f, f["line"])),
                    [primary_key],
                )
            ] += 1
            for c in corroborators:
                anchor = anchors[id(c)]
                key = corroborator_keys[id(c)]
                if key in delivered_keys:
                    counters["already_present"] += 1
                elif anchor:
                    counters[deliver_corroborator(c)] += 1
                else:
                    counters[deliver_unanchored(c, key)] += 1
            continue
        outcome = deliver(
            f,
            filepath,
            f["line"],
            render_group_body(anchored(f, f["line"]), corroborators),
            member_keys,
        )
        if outcome in ("invalid", "failed"):
            # The group's single discussion is lost, but its corroborators were
            # never given a chance of their own — post each as its own
            # discussion rather than dropping validated findings. The primary
            # still counts 1 toward its own loss; each corroborator is counted
            # on its own merits. (Unlike the partial-prior-delivery branch
            # above, nothing from this group has landed on the MR yet, so
            # `deliver_corroborator`'s own no-line/off-diff diagnostics are
            # the right report here — not the position-less note fallback,
            # which exists only to reach a member whose group body already
            # went out without it.)
            counters[outcome] += 1
            for c in corroborators:
                counters[deliver_corroborator(c)] += 1
        else:
            # One discussion carries the whole group, so every member shares
            # its outcome.
            counters[outcome] += 1 + len(corroborators)

    posted = counters["posted"]
    already_present = counters["already_present"]
    invalid = counters["invalid"]
    failed = counters["failed"]

    if DRY_RUN:
        print(f"  {posted} inline discussion(s) captured.")
    else:
        print(f"  {posted} inline discussion(s) posted.")
    if skipped:
        print(f"  {skipped} finding(s) skipped.")
    if already_present:
        print(
            f"  {already_present} inline discussion(s) already on the MR from an "
            f"earlier run — left alone."
        )
    if invalid:
        print(f"  {invalid} finding(s) had a malformed position (see warnings above).")
    _print_fix_summary()

    # Both "nothing new landed" exits below report the same outcome for two different
    # losses, so both owe the operator the same true statement about what is already
    # there: on a rerun, "nothing was posted inline" is a lie whenever this review's
    # discussions are standing on the MR from an earlier run (issue #132).
    standing = (
        f" {already_present} from an earlier run remain on the MR."
        if already_present
        else ""
    )

    if failed:
        print(
            f"  {failed} inline discussion(s) rejected by GitLab (see warnings above)."
        )
        if posted == 0:
            # Every attempt was made first — this exit reports the outcome, it does not
            # abandon the batch. A partial delivery is a success with warnings, but a
            # run that delivered nothing NEW and had a rejection is a failure, and the
            # message counts only what THIS run attempted: discussions an earlier run
            # already placed are neither successes of this one nor part of the total,
            # and a malformed position never reached the wire to be "attempted".
            die(
                f"all {failed} finding(s) attempted this run were rejected by "
                f"GitLab — nothing new was posted inline.{standing} The MR summary note "
                f"is on the MR; rerunning retries the inline comments without "
                f"duplicating what is already there."
            )

    # A malformed position is a payload defect, not a delivery outcome, and catching one
    # before the live post is the whole point of the pre-flight: ANY of them fails the
    # dry run, however many other findings captured cleanly. Returned rather than exited
    # on, so main() still writes the dry-run payload that shows what was wrong.
    if DRY_RUN:
        return 1 if invalid else 0

    # Live, the rule the rejection branch above already follows applies whichever way a
    # finding was lost: a partial delivery is a success with warnings. Inline discussions
    # carry no idempotency key — only the summary note is deduplicated — so a non-zero
    # exit here invites the rerun that double-posts every discussion that landed.
    if invalid and posted == 0:
        die(
            f"{invalid} finding(s) had a malformed position — nothing new was posted "
            f"inline.{standing} The MR summary note is on the MR; rerunning retries the "
            f"inline comments without duplicating what is already there."
        )
    return 0


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
    become ``discussions``. A ``discussions`` body is the rendered comment alone —
    the per-finding delivery marker is appended on the live wire only.
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
        "and read-only fetches still run. A captured GitLab discussion body "
        "omits the per-finding delivery marker the live post appends.",
    )
    args = parser.parse_args()

    # Defense-in-depth: CODE_GAUNTLET_POST_MODE=dry-run self-enforces dry-run so a
    # headless Phase 8 invocation that omits --dry-run cannot live-post. The flag wins
    # when present; env "live" or unset changes nothing without the flag.
    DRY_RUN = args.dry_run or os.environ.get("CODE_GAUNTLET_POST_MODE") == "dry-run"
    _CAPTURED.clear()
    _SKIP_WARNINGS.clear()
    _FIX_COUNTS.update(kept=0, downgraded=0)

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
    valid_lines, new_files, old_paths, line_texts = parse_diff_lines(
        platform, data["owner"], data["repo"], data["pr_number"]
    )

    # Deliver. A poster RETURNS its exit status instead of exiting, so a payload defect
    # it found cannot pre-empt the dry-run payload write below — that file is the artifact
    # an operator reads to see what the run would have sent.
    if platform == "github":
        status = post_github(data, valid_lines, line_texts)
    else:
        status = post_gitlab(data, valid_lines, new_files, old_paths, line_texts)

    if DRY_RUN:
        out_path = write_dry_run_payload(platform, args.findings_json)
        print(f"Dry run — no comments posted. Payload written to: {out_path}")

    if status:
        sys.exit(status)


if __name__ == "__main__":
    main()
