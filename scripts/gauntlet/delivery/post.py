#!/usr/bin/env python3
"""
post_review.py — Deterministic PR/MR comment delivery for code-gauntlet.

Usage:
    python3 post_review.py <findings_json_path> [--dry-run] [--report PATH]

    --dry-run captures the would-be GitHub/GitLab API payloads to
    post-review-payload.json (written next to the findings file) instead of
    posting. Line validation and read-only fetches (diff, MR versions) still run.
    One capture is deliberately NOT the live bytes: a GitLab discussion body is
    captured as the rendered comment alone, without the per-finding delivery
    marker the live post appends (see the marker note in post_gitlab).
    --report reads the rendered Summary body when the input wrapper does not
    already carry review_body. The owner/repo/pr-number/platform/sha flags fill
    or override the corresponding wrapper fields, including for a bare findings
    array.

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
                "rule_source": "documented_rule", # optional — label key when claude_md_rule is rendered
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
    Recognizes validated public github.com/gitlab.com hosts and dot subdomains.
    Override with "platform" field: "github" or "gitlab".

GitHub path:
    Single POST /repos/{owner}/{repo}/pulls/{n}/reviews with comments array,
    event: "COMMENT", via gh api --input.

GitLab path:
    Fetches MR version SHAs (GET /projects/{id}/merge_requests/{iid}/versions).
    Asks gauntlet.prior_review — the only reader — what this SHA's review already left on
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
    still reaches the PR/MR unless the complete composed body exceeds its platform
    byte budget. One warning is emitted per skipped group, naming every member.

No external Python dependencies — stdlib only.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any, NamedTuple

from gauntlet import proc
from gauntlet.cli import Command
from gauntlet.diff import walk_diff
from gauntlet.forge import (
    Forge,
    ForgeUnavailable,
    GitLab,
    PostRequest,
    ReviewTarget,
    detect_platform,
    github_review_request,
    gitlab_discussion_request,
    gitlab_note_request,
    make_forge,
    origin_remote,
)
from gauntlet.fs import JsonReadError, read_json
from gauntlet.marker import (
    FINDING_MARKER_TOKEN,
    LEGACY_PRODUCT,
    MARKER_TOKENS,
    SHA_RE,
    build_finding_marker,
    build_footer,
    build_prose_footer,
    is_sha_shaped,
)
from gauntlet.prior_review import PriorDelivery, gitlab_prior_delivery_state
from gauntlet.registry import (
    BRAND_MARK,
    BRAND_NAME,
    CODE_OWNED_HEADINGS,
    RULE_SOURCE_LABEL_FALLBACK,
    RULE_SOURCE_LABELS,
    SEVERITY_EMOJI,
    SEVERITY_EMOJI_FALLBACK,
)

# Delivery bound on fence content: `suggestion` prose is uncapped because a human reads it,
# but a fence is committed by one click. Both runtimes measure the normalized text
# (`_fix_code_text` removes one final newline): lines are its `split("\n")` elements and
# chars its length in code points, so a 100-line patch never counts 101.
from gauntlet.registry import (
    FIX_MAX_CHARS as _FIX_MAX_CHARS,
)
from gauntlet.registry import (
    FIX_MAX_LINES as _FIX_MAX_LINES,
)
from gauntlet.text import normalize_report_severity

# ---------------------------------------------------------------------------
# Dry-run capture
# ---------------------------------------------------------------------------
DRY_RUN = False
_CAPTURED: list[PostRequest] = []
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


def ensure_available(forge: Forge) -> None:
    try:
        forge.ensure_available()
    except ForgeUnavailable as exc:
        die(str(exc))


def try_post_json(request: PostRequest, *, forge: Forge):
    """Return response/error without stranding siblings after a rejected position."""
    if DRY_RUN:
        _CAPTURED.append(request)
        return {}, None
    result = forge.submit(request)
    if result.warning is not None:
        warn(result.warning)
    return result.response, result.error


def post_json(request: PostRequest, *, forge: Forge):
    """Fail the whole delivery when the single review or first summary is rejected."""
    response, error = try_post_json(request, forge=forge)
    if error is not None:
        die(error)
    return response


# ---------------------------------------------------------------------------
# Diff parsing — line validation
# ---------------------------------------------------------------------------


def parse_diff_lines(target: ReviewTarget, *, forge: Forge):
    """Fetch the diff, returning four absent oracles on a nonzero status."""
    stdout, stderr, rc = forge.diff(target)
    if rc != 0:
        warn(
            f"Could not fetch diff (exit {rc}): {stderr.strip()}. "
            "Skipping line validation — all findings will be posted."
        )
        return None, None, None, None

    return parse_diff_text(forge.platform, stdout)


def _git_header_agrees(git_header, old_side, new_side):
    """True when *git_header* is exactly the ``diff --git`` text a producer writing
    ``a/``/``b/`` prefixes composes for this ``---``/``+++`` pair.

    The header is never split into two paths: a producer may write a path holding a
    space unquoted, so the boundary between them is undecidable on its own. It is
    rebuilt from the pair and compared whole instead, a ``/dev/null`` old side
    borrowing the new side's name. The pair's paths are the walk's decoded spellings, which
    for the raw paths glab composes are its own text; a name the decoder cut at a
    literal TAB no longer rebuilds the header, so that block stays unproven.
    """
    old_name = old_side[2:] if old_side.startswith("a/") else None
    new_name = new_side[2:] if new_side.startswith("b/") else None
    if old_side == "/dev/null":
        old_name = new_name
    return (
        old_name is not None
        and new_name is not None
        and git_header == f"a/{old_name} b/{new_name}"
    )


def parse_diff_text(platform, diff_text):
    """Parse a GitHub or GitLab diff into line and file metadata.

    * ``valid_lines`` — ``(path, new_line)`` -> the same line's OLD-side number, or
      ``None`` for an added line. A key is present exactly when the line can carry an
      inline comment. GitLab addresses a context line only when the position carries
      both ``old_line`` and ``new_line``, so the old-side number must survive parsing.
    * ``new_files`` — paths ADDED by this diff. GitLab answers HTTP 500 to an
      ``old_path`` on a file that has none.
    * ``old_paths`` — new-side path -> the path its ``---`` header named: the
      pre-rename path GitLab requires in ``position.old_path``. Absent for added files.
    * ``line_texts`` — the same keys as ``valid_lines`` -> the line's new-side text,
      the content oracle for the suggested-fix apply-check.

    ``a/`` and ``b/`` are diff syntax on GitHub. ``glab mr diff`` has two shapes, told
    apart per file block. In its PLAIN shape a block is a bare ``---``/``+++`` pair
    with paths verbatim, so a leading ``a/`` or ``b/`` is a real directory, there is no
    ``/dev/null``, and ``@@ -0,0`` is the only added-file signal. In its GIT-STYLE
    shape the prefixes are syntax and ``/dev/null`` marks an absent side; a block is
    read that way only when its ``diff --git`` line proves it (see
    :func:`_git_header_agrees`), because a plain block under a real ``a/`` directory
    is otherwise indistinguishable. One ``diff --git`` line proves at most the one
    pair that follows it.
    """
    valid_lines = {}
    line_texts = {}
    new_files = set()
    old_paths = {}
    git_header = None
    pending_old_side = None
    current_file = None
    # Whether `@@ -0,0` in the current file's hunks means "added".
    zero_old_hunk_means_added = True

    for event in walk_diff(diff_text):
        if event.kind == "git_header":
            git_header = event.text
            # A `---` left unpaired by this header belongs to no block.
            pending_old_side = None
            continue

        if event.kind == "old_path":
            pending_old_side = event.path
            continue

        if event.kind == "new_path":
            old_side, new_side = pending_old_side, event.path
            pending_old_side = None
            git_style = (
                platform == "gitlab"
                and old_side is not None
                and _git_header_agrees(git_header, old_side, new_side)
            )
            git_header = None
            if platform == "github" or git_style:
                old_side = None if old_side is None else old_side.removeprefix("a/")
                new_side = new_side.removeprefix("b/")
            # An empty old side is either an added file or an edit of a file that was
            # already empty. A git-style block says which with `/dev/null`; a plain
            # one cannot, and guesses "added": omitting `old_path` for a pre-existing
            # empty file is harmless, sending it for a new file is the HTTP 500.
            zero_old_hunk_means_added = not git_style
            if new_side == "/dev/null":
                current_file = None  # deleted file — no new path to track
                continue
            current_file = new_side
            if old_side == "/dev/null":
                new_files.add(current_file)
            elif old_side is not None:
                old_paths[current_file] = old_side
            continue

        if event.kind == "hunk":
            if (
                zero_old_hunk_means_added
                and event.old_line == 0
                and event.old_count == 0
                and current_file is not None
            ):
                new_files.add(current_file)
            continue

        # event.kind == "line". A removed line carries no `new_line` — not
        # addressable by the new side, so it records nothing here.
        if event.new_line is not None and current_file is not None:
            valid_lines[(current_file, event.new_line)] = event.old_line
            line_texts[(current_file, event.new_line)] = event.text

    return valid_lines, new_files, old_paths, line_texts


def _strip_ab_prefix(filepath):
    """Return *filepath* without a leading Git diff prefix."""
    return re.sub(r"^[ab]/", "", filepath)


def _resolve_spelling(valid_lines, filepath, line):
    """Return the diff-key spelling of *filepath* at *line* — exact, else its
    ``a/``/``b/``-stripped form — or ``None`` when neither is a diff key.

    The single resolution order shared by :func:`is_line_valid`,
    :func:`diff_path_spelling` and :func:`old_line_for`: prefer the finding's own
    spelling (a real top-level ``a/``/``b/`` directory on GitLab must not be
    stripped), fall back to the diff-prefix-stripped one (a GitHub finding may spell
    its path with git's synthetic prefix). Assumes *valid_lines* is already known to
    behave like a mapping — every caller guards its own None / non-dict case first,
    since their "validation skipped" defaults differ (see each docstring).
    """
    if (filepath, line) in valid_lines:
        return filepath
    stripped = _strip_ab_prefix(filepath)
    if (stripped, line) in valid_lines:
        return stripped
    return None


def is_line_valid(valid_lines, filepath, line):
    """Check whether (filepath, line) appears in the diff."""
    if valid_lines is None:
        return True  # validation skipped
    return _resolve_spelling(valid_lines, filepath, line) is not None


def diff_path_spelling(valid_lines, filepath, line):
    """Return the spelling of *filepath* recorded in the diff, or *filepath* unchanged.

    A finding may spell its path with a synthetic diff prefix (``b/src/app.py``) while
    the parsed keys are unprefixed — or, on GitLab, the repo may contain a REAL top-level
    ``a/``/``b/`` directory that must not be stripped. Trust the diff: prefer the exact
    key, fall back to the stripped one, and when validation was skipped (*valid_lines*
    is None) pass the finding's own spelling through untouched.

    Residual: when the diff contains a real file under the
    STRIPPED spelling but nothing ADDRESSABLE under the exact one, this resolves to the
    stripped sibling — cross-file, and undecidable from the diff text alone. That is an
    accepted, ANCHOR-level limitation (a wrong anchor costs a misplaced comment a human
    reads and ignores). The higher-harm case — a patch silently applying to the wrong
    one of two REAL files under both spellings — is caught one layer up, at the
    ``suggested_fix_code`` fence (:func:`_suggested_fix_gate`'s ambiguity check), but
    ONLY when both files contribute at least one addressable new-side line to
    *valid_lines*: that check reads path components off *valid_lines*' own keys, which
    hold nothing for a file present in the diff solely as a deletion, or as the
    pre-rename side of a rename (no ``new_line`` to record — see ``parse_diff_text``).
    The finding's own file being absent from the diff entirely, or contributing only
    such non-addressable lines, is exactly the residual above, not the caught case: the
    fence cross-resolves to the sibling and validates against it same as the anchor.
    """
    if not isinstance(valid_lines, dict):
        return filepath
    spelling = _resolve_spelling(valid_lines, filepath, line)
    return filepath if spelling is None else spelling


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
    spelling = _resolve_spelling(valid_lines, filepath, line)
    return None if spelling is None else valid_lines[(spelling, line)]


def valid_lines_for_file(valid_lines, filepath):
    """Return sorted list of up to 10 valid line numbers for *filepath* in the diff.

    Returns None when *valid_lines* is None (validation was skipped). No *line* to
    resolve against here, so this uses the shared strip helper directly rather than
    :func:`_resolve_spelling` — both spellings' lines are wanted, not one.
    """
    if valid_lines is None:
        return None
    stripped = _strip_ab_prefix(filepath)
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


def _is_plain_int(value):
    """True only for a real ``int`` — ``True`` and ``2.0`` both hash equal to the
    integer key, so they survive every dict lookup and equality check; type is the
    only thing that separates them from the integer they impersonate."""
    return isinstance(value, int) and not isinstance(value, bool)


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

    # A bool or a float line number passes line validation AND the equality check
    # below, reaching the wire in its own spelling.
    new_line = position.get("new_line")
    if "new_line" in position and not _is_plain_int(new_line):
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

    Whitespace-only input is still absent (``None``), a patch
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

_ENTITY_DEC_RE = re.compile(r"&#([0-9]+);")
_ENTITY_HEX_RE = re.compile(r"&#x([0-9a-fA-F]+);", re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
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

    ``&commat;`` is decoded too; other named entities are left untouched.
    Non-ASCII numeric entities (e.g.
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
    return text.replace("&commat;", "@")


_DANGEROUS_LT_RE = re.compile(r"<(?=[A-Za-z/!?])")
_MARKER_OPEN_RE = re.compile(
    r"<!--\s*(?:"
    + "|".join(re.escape(token) for token in (*MARKER_TOKENS, FINDING_MARKER_TOKEN))
    + r")\s*:"
)
_FENCE_SHAPE_RE = re.compile(r"^(?:[ \t>]|[-+*][ \t]|[0-9]{1,9}[.)][ \t])*([`~])\1{2,}")
_MULTILINE_QUOTE_RE = re.compile(r"^(?:[ \t>]|[-+*][ \t]|[0-9]{1,9}[.)][ \t])*?(>{3,})")


def _remove_comments(text):
    while True:
        cleaned = _HTML_COMMENT_RE.sub("", text)
        if cleaned == text:
            return text
        text = cleaned


def _normalize_outbound(text):
    while True:
        normalized = _strip_invisibles(_remove_comments(_decode_numeric_entities(text)))
        if normalized == text:
            return text
        text = normalized


def _break_marker_openers(text):
    return _MARKER_OPEN_RE.sub(lambda match: "&lt;" + match.group()[1:], text)


def _escape_visible(text, *, code=False):
    text = _DANGEROUS_LT_RE.sub("\uff1c" if code else "&lt;", text)
    return re.sub(
        r"@",
        lambda match: (
            "\uff20"
            if not match.start()
            or not re.match(r"[A-Za-z0-9]", text[match.start() - 1])
            else "@"
        ),
        text,
    )


def _escaped_tick(text, index):
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return bool(backslashes % 2)


def _contain_line(line):
    line = re.sub(r"<(?=`+[A-Za-z/!?])", "\uff1c", line)
    out = []
    index = 0
    while index < len(line):
        if line[index] == "`":
            if _escaped_tick(line, index):
                out.append("`")
                index += 1
                continue
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            width = end - index
            # Only a complete run of exactly this width closes; backslashes
            # inside the span have no escape meaning.
            cursor = end
            close = -1
            while cursor < len(line):
                tick = line.find("`", cursor)
                if tick < 0:
                    break
                after = tick
                while after < len(line) and line[after] == "`":
                    after += 1
                if after - tick == width:
                    close = tick
                    break
                cursor = after
            if close >= 0:
                out.append(
                    line[index : close + width].replace(
                        line[end:close],
                        _escape_visible(line[end:close], code=True),
                        1,
                    )
                )
                index = close + width
                continue
            out.append("\\`")
            index += 1
            continue
        next_tick = line.find("`", index)
        if next_tick < 0:
            next_tick = len(line)
        out.append(_escape_visible(line[index:next_tick]))
        index = next_tick
    return "".join(out)


def _prepare_text(
    text, *, single_line=False, collapse_ticks=False, cap=None, trust_fences=True
):
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text.strip():
        return ""
    text = _normalize_outbound(text)
    text = _redact_secrets(text)
    if collapse_ticks:
        text = _BACKTICK_RUN_RE.sub("``", text)
    if single_line:
        text = re.sub(r"[\r\n]+", " ", text)
    if cap is not None:
        text = _cap_rule_text(text, cap)
    if not text.strip():
        return ""
    intervals = []
    fence = (
        _open_fence(text, strict=True, intervals=intervals)
        if not single_line and trust_fences
        else None
    )
    lines = text.split("\n")
    prepared = []
    protected_lines = []
    offset = 0
    for line in lines:
        protected = any(start <= offset < end for start, end in intervals)
        original_length = len(line)
        shape = _FENCE_SHAPE_RE.match(line) if not single_line else None
        if shape and not protected:
            tick = shape.start(1)
            line = line[:tick] + "\\" + line[tick:]
        if not protected and not single_line:
            if line.startswith("/"):
                line = "\\" + line
            quote = _MULTILINE_QUOTE_RE.match(line)
            if quote:
                index = quote.start(1)
                line = line[:index] + "\\" + line[index:]
        prepared.append(line if protected else _contain_line(line))
        protected_lines.append(protected)
        offset += original_length + 1
    start = None
    for index in range(len(prepared) + 1):
        if index < len(prepared) and protected_lines[index]:
            if start is None:
                start = index
        elif start is not None:
            prepared[start:index] = _break_marker_openers(
                "\n".join(prepared[start:index])
            ).split("\n")
            start = None
    result = "\n".join(prepared)
    if fence is not None:
        result += "\n" + fence[0] * fence[1]
    return result if result.strip() else ""


def prepare_prose(text):
    """Prepare multiline, untrusted text for a posted Markdown field."""
    return _prepare_text(text)


def prepare_line(text):
    """Prepare a single-line, untrusted display field."""
    return _prepare_text(text, single_line=True)


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
    prepared = _prepare_text(
        text,
        collapse_ticks=True,
        cap=_RULE_TEXT_CAP if cap else None,
        trust_fences=not cap,
    )
    return _rendered_text(prepared)


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


def _fence_run(payload):
    """Return the backtick fence long enough to contain *payload* unbroken.

    ``max(3, longest_backtick_run + 1)`` so CommonMark cannot close early. Per
    clause: the longest-run+1, min-3 rule matches GitLab's own suggestion UI
    (gitlab-org MR !172981); GitHub keeps Apply at 4+ backticks (confirmed);
    GitLab documents four-backtick suggestion nesting.

    Factored out of ``_suggestion_fence`` so a second renderer
    (``gauntlet.patches``, the report-side read-only apply-check) computes
    the identical length from the identical rule rather than a second copy of it.
    """
    runs = re.findall(r"`+", payload)
    n = max(3, max((len(r) for r in runs), default=0) + 1)
    return "`" * n


def _suggestion_fence(payload, *, offsets=None):
    """Return ``(open, close)`` fence lines that contain ``payload``.

    Length is ``_fence_run(payload)`` — see its docstring for the rule.

    *offsets* is GitLab's ``(above, below)`` pair: it makes the header
    ``suggestion:-m+n``, which widens what one click replaces to
    ``[anchor - m, anchor + n]``. The parser is fence-length blind, so
    the header composes with any length. ``None`` and ``(0, 0)`` both render
    the plain header — ``suggestion:-0+0`` is its exact synonym, and the plain
    spelling is the one every platform understands. This renderer is
    platform-blind: whether offsets are expressible at all is the poster's
    decision, made where the anchor is known.
    """
    fence = _fence_run(payload)
    header = "suggestion"
    if offsets not in (None, (0, 0)):
        header = f"suggestion:-{offsets[0]}+{offsets[1]}"
    return f"{fence}{header}", fence


# ---------------------------------------------------------------------------
# suggested_fix_code — the deterministic apply-check
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
_FIX_MARKER_SHAPED = "marker_shaped"
_FIX_MISSING_END_LINE = "missing_end_line"
_FIX_INVALID_RANGE = "invalid_range"
_FIX_NO_ORACLE = "no_diff_oracle"
_FIX_RANGE_NOT_IN_DIFF = "range_not_in_diff"
_FIX_ANCHOR_MISMATCH = "anchor_mismatch"
_FIX_SPAN_EXCEEDS_CAP = "span_exceeds_platform_cap"
_FIX_NO_OP = "no_op_replacement"
_FIX_INDENTATION = "indentation_mismatch"
_FIX_TOO_LARGE = "replacement_too_large"
_FIX_CARRIAGE_RETURN = "carriage_return"
_FIX_OVERLAPS_KEPT_FENCE = "overlaps_kept_fence"

# The vocabulary is CLOSED: every downgrade names exactly one of these, in the
# stable warning `{warn_label} downgraded: {file}:{line} ({reason})` — the label
# is the caller's (`suggested-fix` for delivery, `report-patch` for the report
# path's read-only gate in gauntlet.patches); everything after it never
# changes shape. Adding a reason is a deliberate act — a free-text reason would
# make the record unreadable in aggregate.
#
# `overlaps_kept_fence` is the one member that is not a
# `_suggested_fix_gate` outcome: it names a SET-LEVEL decision (this finding's
# fence would collide, on the platform's own overlap semantic, with another
# kept fence in the same file) rather than a property of the finding alone —
# see `_overlap_losers` and `_gated_finding`'s `demote_reason` parameter.
_FIX_REASONS = frozenset(
    {
        _FIX_NON_STRING,
        _FIX_EMPTY,
        _FIX_REDACTED,
        _FIX_MARKER_SHAPED,
        _FIX_MISSING_END_LINE,
        _FIX_INVALID_RANGE,
        _FIX_NO_ORACLE,
        _FIX_RANGE_NOT_IN_DIFF,
        _FIX_ANCHOR_MISMATCH,
        _FIX_SPAN_EXCEEDS_CAP,
        _FIX_NO_OP,
        _FIX_INDENTATION,
        _FIX_TOO_LARGE,
        _FIX_CARRIAGE_RETURN,
        _FIX_OVERLAPS_KEPT_FENCE,
    }
)

# GitLab's own platform limit on a ```suggestion:-m+n offset
# (`Suggestible::MAX_LINES_CONTEXT`). An offset above it is silently CLAMPED
# server-side, not rejected — the header would show Apply and then replace a
# range it does not state — so an emitted header must never carry one. Unrelated
# to `_FIX_MAX_LINES`, which bounds the fence's PAYLOAD, and to the pipeline's
# tunable maxLineSpan intake bound (default 100), which is what keeps a span
# this wide from reaching delivery at all unless a caller raises it.
_GITLAB_SUGGESTION_OFFSET_CAP = 100

# Per-run patch-acceptance counters, reset by reset_run_state() alongside
# _CAPTURED and _SKIP_WARNINGS. n/(n+m) over these two is the acceptance rate,
# deterministic and readable from any run's stdout at no cost.
_FIX_COUNTS = {"kept": 0, "downgraded": 0}
# Per-reason downgrade tally, reset alongside _FIX_COUNTS. Delivery's own
# stdout readout (_print_fix_summary) does not consult this — it exists for a
# second gate caller (gauntlet.patches, the report-side apply-check)
# that renders a reason breakdown from it.
_FIX_REASON_COUNTS: dict[str, int] = {}


def reset_run_state():
    """Clear every module-level counter/log a run accumulates.

    One entry point for both gate callers: main() (delivery) calls this in
    place of its old inline three-statement reset, and gauntlet.patches
    (the report-side apply-check, which never calls main()) calls it too —
    so a second caller of _gated_finding cannot start from state a prior
    caller in the same process left behind.
    """
    _CAPTURED.clear()
    _SKIP_WARNINGS.clear()
    _FIX_COUNTS.update(kept=0, downgraded=0)
    _FIX_REASON_COUNTS.clear()


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


def _fence_path_is_ambiguous(valid_lines, raw_file):
    """True when *raw_file* — the finding's OWN spelling, unresolved — cannot say
    which of two distinct real files in the diff a ``suggested_fix_code`` fence
    targets.

    Fires only when *raw_file* carries a synthetic ``a/``/``b/`` diff prefix AND
    BOTH it and its stripped form name a real path in *valid_lines* — path level,
    any line, so this is a property of the diff's file set, not of which line the
    finding happens to state. When that holds, the diff itself contains two
    distinct files one strip apart (most commonly a real top-level ``a/``/``b/``
    directory alongside its unprefixed sibling), and neither the exact spelling
    nor :func:`diff_path_spelling`'s stripped fallback can disambiguate which one
    a patch is meant to replace — one directly (the raw spelling IS a real,
    different file's key), the other by silent cross-resolution. Both directions
    are closed by the same check: it does not matter which one the finding's
    stated line happens to validate against.

    Reads only ``valid_lines`` KEYS — no platform, no alias map, no second keying
    implementation — so delivery and ``gauntlet.patches``' payload
    mirror (both routing through :func:`_suggested_fix_gate`) get identical
    semantics for free. *valid_lines* must already be known to be a dict; the
    caller checks that first.
    """
    stripped = _strip_ab_prefix(raw_file)
    if stripped == raw_file:
        return False
    diff_paths = {fp for fp, _line in valid_lines}
    return raw_file in diff_paths and stripped in diff_paths


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

    That same harm asymmetry is why the fence-path AMBIGUITY check
    (:func:`_fence_path_is_ambiguous`) fails closed here, before span
    validation, on the finding's RAW (unresolved) spelling — even though
    :func:`diff_path_spelling` (used for *path_lookup*, and separately for the
    posted anchor) tolerates and cross-resolves the exact same collision. An
    anchor pointed at the wrong one of two colliding files costs a misplaced
    comment; a fence pointed at the wrong one commits a patch to it.
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
    if _MARKER_OPEN_RE.search(text):
        return False, _FIX_MARKER_SHAPED

    line = finding.get("line")
    end_line = finding.get("end_line")
    if end_line is None:
        # A patch's stated range must be explicit. An absent end_line — including
        # one deleted for exceeding maxLineSpan — is exactly how a multi-line
        # replacement lands on a single-line anchor and corrupts the file.
        return False, _FIX_MISSING_END_LINE
    if (
        not _is_plain_int(line)
        or not _is_plain_int(end_line)
        or line < 1
        or end_line < line
    ):
        return False, _FIX_INVALID_RANGE
    if not isinstance(valid_lines, dict) or not isinstance(line_texts, dict):
        # No diff was parsed, so every check below has nothing to consult. Both
        # mappings are required: one alone answers only half of "is this range
        # real" / "does this patch change anything".
        return False, _FIX_NO_ORACLE
    if _fence_path_is_ambiguous(valid_lines, finding.get("file", "?")):
        # The diff names two distinct real files one strip apart: this finding's
        # own spelling cannot say which one the patch targets, so the fence fails
        # closed even though the ANCHOR still resolves (ratified, comment-level
        # harm only — see diff_path_spelling's docstring).
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
        # needs ONE spelling to hold for every line in it. A mixed-spelling range
        # PARSED from a real diff is no longer reachable here — the ambiguity
        # check above already fails closed on it, since a mix implies both
        # spellings name real diff paths. What still reaches this branch is a
        # PARTIAL line_texts oracle: a hand-built or truncated mapping (e.g. a
        # caller-supplied dict missing some span lines) that answers is_line_valid
        # for every line but has no text for one of them. That is still "no
        # oracle", not "no difference": treating it as the latter would silently
        # skip the no-op/indentation checks below instead of failing the patch
        # closed.
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


def _fence_verdict(finding, apply_range, valid_lines, line_texts):
    """Return ``(ok, reason)`` for *finding*'s ``suggested_fix_code`` at *apply_range*.

    Owns the ``path_lookup`` derivation (:func:`diff_path_spelling`, resolved
    against the finding's OWN ``line`` — never the render site's anchor) and the
    call into :func:`_suggested_fix_gate`. Every caller that needs to know
    whether a fence would render — :func:`_gated_finding` (the render sites) and
    a poster's overlap pre-pass (the candidate check) — goes through this ONE
    function, so "is this finding a candidate" and "would this finding's fence
    actually be kept" are the same computation, not two that could drift apart.
    """
    return _suggested_fix_gate(
        finding,
        apply_range=apply_range,
        line_texts=line_texts,
        valid_lines=valid_lines,
        path_lookup=diff_path_spelling(
            valid_lines, finding.get("file", "?"), finding.get("line")
        ),
    )


def _gated_finding(
    finding,
    apply_range,
    valid_lines,
    line_texts,
    *,
    mismatch_reason=_FIX_ANCHOR_MISMATCH,
    warn_label="suggested-fix",
    demote_reason=None,
):
    """Return the finding to RENDER at one site, gating its ``suggested_fix_code``.

    A failure strips the field from a SHALLOW COPY — the copy is what gets
    rendered, so ``render_comment_body`` itself is untouched. Benchmark mirrors
    call ``compose_inline_body`` over ``_render_group_sections(...)`` to construct
    the body they score, and the prose ``suggestion`` carries the fix instead.
    Each downgrade is recorded through ``warn_skip``, which both prints
    and lands in the dry-run payload's existing ``skipped`` list, and counted for
    the run's patch-acceptance readout (``_FIX_COUNTS``) and per-reason tally
    (``_FIX_REASON_COUNTS``).

    Called at every site where a fence can actually render. A corroborator inside
    a group body is not such a site — ``_render_corroboration`` renders no fence
    (nor the prose suggestion) at all — so it is neither gated nor counted here.

    *mismatch_reason* renames the anchor-equality failure for a caller that knows
    WHY no anchor could cover the stated range: GitLab's cap on ``-m+n`` offsets
    is the one such caller. It renames one outcome, it does not add a
    check, and it cannot widen the vocabulary — an unknown name raises below
    exactly like a typo'd gate reason.

    *demote_reason* forces a fence that PASSED the per-finding gate
    to downgrade anyway, through this same funnel — tallied, warned, stripped
    exactly like an ordinary gate failure. It is consulted ONLY when the gate
    says ``ok``: a gate FAILURE keeps its own reason regardless of
    *demote_reason* (per-fence reasons always win — a set-level demotion is
    chosen only among findings the gate already approved, so the two paths can
    never disagree about the same finding). ``None`` (the default) is a no-op —
    every existing caller is unaffected. The ``elif`` below that applies
    *mismatch_reason* is reached only on that gate-FAILURE branch — a
    set-level *demote_reason* is never renamed by *mismatch_reason*, because
    the ``ok`` branch above already returned or reassigned ``reason`` before
    this ``elif`` is ever evaluated.

    *warn_label* names the CALLER in the downgrade warning line (default
    ``"suggested-fix"``, delivery's own spelling — unchanged bytes for every
    existing reader). ``gauntlet.patches``, the report-side read-only
    apply-check, passes ``"report-patch"`` so its downgrades stay distinguishable
    from delivery's in a run's combined stderr.
    """
    if not isinstance(finding, dict) or "suggested_fix_code" not in finding:
        return finding
    ok, reason = _fence_verdict(finding, apply_range, valid_lines, line_texts)
    if ok:
        if demote_reason is None:
            _FIX_COUNTS["kept"] += 1
            return finding
        reason = demote_reason
    elif reason == _FIX_ANCHOR_MISMATCH:
        reason = mismatch_reason
    if reason not in _FIX_REASONS:
        # A typo'd reason string in a future gate edit — or a typo'd
        # demote_reason from a future overlap-demotion caller — must fail
        # loudly at the first downgrade, not silently record garbage in the
        # stable warning line (whose readers rely on the vocabulary being
        # closed).
        raise ValueError(
            f"unknown downgrade reason: {reason!r} (from the gate, a "
            f"mismatch_reason rename, or a caller's demote_reason)"
        )
    _FIX_COUNTS["downgraded"] += 1
    _FIX_REASON_COUNTS[reason] = _FIX_REASON_COUNTS.get(reason, 0) + 1
    warn_skip(
        f"{warn_label} downgraded: {finding.get('file', '?')}:"
        f"{finding.get('line')} ({reason})"
    )
    stripped = dict(finding)
    del stripped["suggested_fix_code"]
    return stripped


def _gitlab_fence_offsets(anchor, line, end_line):
    """Return ``(offsets, cap_exceeded)`` for a GitLab fence posted at *anchor*.

    *offsets* is the ``(above, below)`` pair a ```suggestion:-m+n header must
    state for one click to replace ``[line, end_line]``, or ``None`` when no
    header expresses that range from this anchor. GitLab resolves the header
    against ``position.new_line``, so the pair is a function of the ANCHOR and
    the stated range — never of the finding alone.

    Pure and total, so both the poster and the benchmark's payload mirror can
    consume it: a non-integer or absent bound answers ``None`` and leaves the
    gate's own ``missing_end_line`` / ``invalid_range`` rules to name what is
    wrong. *cap_exceeded* is True only when the range was otherwise realizable
    and the platform cap alone forbade the header — which is what separates
    ``span_exceeds_platform_cap`` from ``anchor_mismatch``.
    """
    if not all(_is_plain_int(v) for v in (anchor, line, end_line)):
        return None, False
    above, below = anchor - line, end_line - anchor
    if above < 0 or below < 0:
        # The anchor lies outside the stated range: offsets extend outward from
        # it in both directions, so no pair can reach a range it is not inside.
        return None, False
    if above > _GITLAB_SUGGESTION_OFFSET_CAP or below > _GITLAB_SUGGESTION_OFFSET_CAP:
        return None, True
    return (above, below), False


def _gitlab_apply_range(finding, anchor):
    """Return ``(apply_range, offsets, cap_exceeded)`` for *finding* anchored at *anchor*.

    The render site and overlap pre-pass share the same decision.
    """
    offsets, cap_exceeded = _gitlab_fence_offsets(
        anchor, finding.get("line"), finding.get("end_line")
    )
    apply_range = (
        (anchor, anchor)
        if offsets is None
        else (anchor - offsets[0], anchor + offsets[1])
    )
    return apply_range, offsets, cap_exceeded


def _gitlab_anchored(finding, anchor, valid_lines, line_texts, *, demote_reason=None):
    """Return ``(finding_to_render, fence_offsets)`` for ONE GitLab inline body.

    A GitLab position is always single-line, but the fence header widens what one
    click replaces to ``[anchor - m, anchor + n]`` — so the apply range
    the gate judges is the one those offsets realize, and a span no header can
    express is judged against the single anchored line instead. The gate's
    equality check then makes a kept fence's offsets provably realize the range
    the finding states.

    The offsets are returned OUT OF BAND and never written onto the finding: the
    findings JSON is caller-supplied and flows in unfiltered, so an in-band key
    would be a key that widens an apply range the gate approved as narrower. This
    is the whole GitLab render-site decision, in one place, so the benchmark's
    payload mirror can make it by calling rather than by copying.

    *demote_reason* passes straight through to :func:`_gated_finding` —
    a caller with a set-level overlap decision for this anchor states it here,
    exactly as it would at a GitHub render site.
    """
    apply_range, offsets, cap_exceeded = _gitlab_apply_range(finding, anchor)
    gated = _gated_finding(
        finding,
        apply_range,
        valid_lines,
        line_texts,
        mismatch_reason=_FIX_SPAN_EXCEEDS_CAP if cap_exceeded else _FIX_ANCHOR_MISMATCH,
        demote_reason=demote_reason,
    )
    return gated, offsets


def _degraded_entry(filepath, line, finding, valid_lines, line_texts):
    """One entry for the body section, which has no anchor to apply against.

    Shared by both posters: a body-section entry never has an apply range, so
    ``_gated_finding`` is always called with ``None`` here regardless of
    platform.
    """
    return filepath, line, _gated_finding(finding, None, valid_lines, line_texts)


def _warn_group_skipped(group, valid_lines, filepath=None):
    """Record the one skip warning a group that cannot anchor inline gets.

    *filepath* is the primary's resolved diff spelling when its line is missing from
    the diff, and ``None`` when the primary has no line at all. The warning stands for
    the whole group, so a group of several names every member: the title alone would
    leave the corroborators untraceable.
    """
    primary = group["primary"]
    title = primary.get("title", "?")
    if filepath is None:
        message = f"Finding '{title}' has no line number — skipping."
    else:
        diag = ""
        vl = valid_lines_for_file(valid_lines, filepath)
        if vl is not None:
            diag = f" Valid lines for this file: {vl}"
        message = (
            f"Skipping finding '{title}' at {filepath}:{primary['line']} "
            f"— line not found in diff.{diag}"
        )
    members = [primary, *group["corroborators"]]
    if len(members) > 1:
        labels = [
            str(m["id"] if m.get("id") is not None else m.get("title", "?"))
            for m in members
        ]
        message += f" [group members: {', '.join(labels)}]"
    warn_skip(message)


def _key_material_finding(finding):
    """Return the copy whose render seeds a DELIVERY KEY.

    ``suggested_fix_code`` and ``rule_source`` come off UNCONDITIONALLY — not gated —
    so a key does not depend on either field at all: it is the same whether the finding ships
    grouped or individually, and the same whichever way the apply-check went. This preserves
    the fence/rule-source exclusion guarantee for keys across delivery shapes.
    Prior-delivery dedup is retry-safe only while keys are stable
    across runs and across delivery shapes; making the GATE deterministic would
    not be enough, because the gate's inputs (the diff, the render site) are not.

    The brand trailer is excluded the same way and for the same reason — it is not a
    field, so it is excluded structurally by :func:`key_material_body` calling
    :func:`_finding_sections`, never by stripping a suffix.
    """
    if not isinstance(finding, dict) or not any(
        field in finding for field in ("suggested_fix_code", "rule_source")
    ):
        return finding
    stripped = dict(finding)
    stripped.pop("suggested_fix_code", None)
    stripped.pop("rule_source", None)
    return stripped


def _github_apply_range(valid_lines, filepath, line, end_line):
    """Return ``(multiline, apply_range)`` for a GitHub comment anchored at *line*.

    Shared with ``post_github``'s render loop
    so the loop, a pre-render overlap pass, and the benchmark's payload mirror
    all make this ONE decision by calling it — never by duplicating the
    formula. A group comment anchors only on the primary's range (a
    corroborator never contributes a fence), so this is the apply range of
    every fence the comment can carry.
    """
    multiline = (
        isinstance(end_line, int)
        and end_line >= line
        and end_line != line
        and _range_is_valid(valid_lines, filepath, line, end_line)
    )
    apply_range = (line, end_line) if multiline else (line, line)
    return multiline, apply_range


def _ranges_overlap(a, b):
    """True when closed intervals *a* and *b* (each an ``(start, end)`` pair)
    share at least one integer.

    ``max(l1, l2) <= min(e1, e2)`` — GitLab's own ``Range#overlaps?`` exactly
    (``lib/gitlab/suggestions/file_suggestion.rb``): two identical single-line
    ranges collide (same line, same value both sides of the comparison);
    touching disjoint ranges like ``[1, 3]``/``[4, 6]`` do not (3 <= 4 is the
    ``<=`` that would need to run the other way to fire).
    """
    return max(a[0], b[0]) <= min(a[1], b[1])


def _github_overlap_records(groups, valid_lines, line_texts):
    """Return the CANDIDATE ``(index, path_lookup, apply_range)`` records for
    post_github's overlap pre-pass.

    *groups* is ``consolidate_delivery``'s own output (or the benchmark
    mirror's single-member-group equivalent — it models no consolidation) —
    the returned ``index`` is ``enumerate(groups)``'s index into THAT list,
    the same index post_github's render loop later checks with
    ``index in losers``. A candidate is a group whose primary carries
    ``suggested_fix_code``, anchors on a line the diff has, and whose fence
    passes the SAME pure gate (:func:`_fence_verdict`) at the SAME apply
    range (:func:`_github_apply_range`) the render loop itself will apply —
    "candidate" and "would render a kept fence" are one computation, not two
    that could disagree. Called by post_github's pre-pass and by the
    benchmark's payload mirror — never duplicated.
    """
    records = []
    for index, group in enumerate(groups):
        primary = group["primary"]
        if not isinstance(primary, dict) or "suggested_fix_code" not in primary:
            continue
        line = primary.get("line")
        if line is None:
            continue
        filepath = diff_path_spelling(valid_lines, primary.get("file", "?"), line)
        if not is_line_valid(valid_lines, filepath, line):
            continue
        _, apply_range = _github_apply_range(
            valid_lines, filepath, line, primary.get("end_line")
        )
        ok, _ = _fence_verdict(primary, apply_range, valid_lines, line_texts)
        if ok:
            records.append((index, filepath, apply_range))
    return records


def _gitlab_overlap_records(remaining, valid_lines, line_texts):
    """Return the CANDIDATE ``(index, path_lookup, apply_range)`` records for
    post_gitlab's overlap pre-pass.

    *remaining* is post_gitlab's own pre-partitioned list of ``(filepath,
    group)`` pairs — every skip decision already made — so the returned
    ``index`` is ``enumerate(remaining)``'s index into THAT list, the same
    index post_gitlab's render loop later checks with ``index in losers``.
    Feeding this the unfiltered ``findings``/``consolidate_delivery`` list
    instead would silently re-key every record against the wrong basis — a
    skipped (e.g. off-diff) finding shifts ``remaining``'s positions but
    never occupies one itself. A candidate is a primary carrying
    ``suggested_fix_code`` whose fence passes the SAME pure gate the render
    loop applies, anchored at its own ``line``. Called by post_gitlab's
    pre-pass and by the benchmark's payload mirror.
    """
    records = []
    for index, (filepath, group) in enumerate(remaining):
        primary = group["primary"]
        if not isinstance(primary, dict) or "suggested_fix_code" not in primary:
            continue
        apply_range, _offsets, _cap_exceeded = _gitlab_apply_range(
            primary, primary["line"]
        )
        ok, _ = _fence_verdict(primary, apply_range, valid_lines, line_texts)
        if ok:
            records.append((index, filepath, apply_range))
    return records


def _overlap_losers(records):
    """Return the set of *record* indexes to DEMOTE.

    *records* is an iterable of ``(index, path_lookup, apply_range)`` —
    candidates only: every record's ``apply_range`` is a real ``(start, end)``
    interval, already known to pass the per-finding gate at that range (a
    fence-less or gate-failing finding is never a record at all — see each
    poster's candidate predicate — so it can never claim an interval and can
    never block anyone).

    Pure, total, and mirror-callable. Scans *records* in the given order (each
    poster's own delivery order) and keeps a running per-path list of claimed
    intervals: a record whose interval intersects (closed, :func:`_ranges_overlap`)
    ANY already-kept interval on the SAME ``path_lookup`` is demoted; a demoted
    record claims nothing, so it can never block a later record either — "loser
    occupies nothing".

    First-wins is greedy, not maximum-cardinality: given ``A=[1,10]``,
    ``B=[5,6]``, ``C=[8,20]`` in that order, only ``A`` survives (``B`` and
    ``C`` both collide with it) even though keeping ``B`` and ``C`` instead
    would keep two fences rather than one. Priority (delivery order) beats
    count, deliberately — record order is the poster's GROUP order (each
    group positioned at its first member's array index, per
    ``consolidate_delivery``), so a group's fence inherits its best-ranked
    member's priority, not necessarily its primary's own rank, and demoting
    the first group to keep more lower-priority fences would still be the
    wrong trade.
    """
    losers = set()
    kept_by_path = {}
    for index, path_lookup, apply_range in records:
        kept = kept_by_path.setdefault(path_lookup, [])
        if any(_ranges_overlap(apply_range, k) for k in kept):
            losers.add(index)
        else:
            kept.append(apply_range)
    return losers


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


# One mark per delivered SURFACE, never per element: an inline comment/discussion body
# carries the trailer once at the end; the summary body carries the header instead, and the
# skipped-findings entries inside it are rendered unbranded.
BRAND_TRAILER = f"{BRAND_MARK} *{BRAND_NAME}*"
BRAND_SUMMARY_HEADER = f"### {BRAND_MARK} {BRAND_NAME}"
PLATFORM_BODY_LIMITS = {
    "github": {
        "label": "GitHub",
        "surfaces": {
            "summary": {"surface": "review body", "bytes": 65536},
            "inline": {"surface": "inline review comment", "bytes": 65536},
        },
    },
    "gitlab": {
        "label": "GitLab",
        "surfaces": {
            "summary": {"surface": "summary note", "bytes": 1000000},
            "discussion": {"surface": "inline discussion", "bytes": 1000000},
            "note": {"surface": "corroborator note", "bytes": 1000000},
        },
    },
}


class ComposedBody(NamedTuple):
    body: str
    shown: int
    omitted: int
    folded_bytes: int
    omitted_entries: tuple[tuple[str, str], ...]


class InlineBody(NamedTuple):
    body: str
    folded_bytes: int


def _utf8_len(text):
    return len(text.encode("utf-8"))


def _body_limit(platform, surface="summary"):
    limits = PLATFORM_BODY_LIMITS[platform]
    row = limits["surfaces"][surface]
    return {"label": limits["label"], **row}


def _normalize_report_severity(raw):
    """Normalize severity with the current generated or contract-sample label map."""
    return normalize_report_severity(raw, SEVERITY_EMOJI)


def _finding_sections(finding, *, fence_offsets=None):
    """The finding's rendered sections — historically byte-identical to what
    ``render_comment_body`` returned before the brand trailer existed for existing canonical
    labels and plain case variants. This is the KEY MATERIAL: a delivery key must not move
    when the product's identity does (see :func:`key_material_body`). Severity normalization
    supplies both the emoji and the heading label.

    *fence_offsets* is passed through to the suggestion fence and is meaningful
    only where a platform reads one (GitLab). It is a parameter rather
    than a finding field because the value depends on the anchor the body will
    be posted at, which only the caller knows — and because a finding's own JSON
    is caller-supplied.
    """
    severity = _normalize_report_severity(finding.get("severity"))
    emoji = SEVERITY_EMOJI.get(severity, SEVERITY_EMOJI_FALLBACK)

    raw_title = finding.get("title")
    title = prepare_line(raw_title) if isinstance(raw_title, str) else ""
    body = prepare_prose(finding.get("body", ""))
    suggested_fix = _fix_code_text(finding.get("suggested_fix_code"))

    parts = [f"**{emoji} [{severity.upper()}] {title or 'Finding'}**", "", body]

    # Agent-authored prose fix suggestion: sanitize and
    # redact, uncapped. Structural sanitize only — not the cited-rule cap.
    suggestion_text = _prepared_prose(finding.get("suggestion"))
    if suggestion_text:
        parts += ["", "**Suggested fix:**", suggestion_text]

    # Cited rule: normalize, redact, collapse long backtick runs, cap the source
    # at 500 characters, contain every line, then blockquote. Each candidate
    # is prepared independently; `claude_md_rule`
    # wins only when it survives sanitize (comment-only rules fall through).
    rule_text = _prepared_prose(finding.get("claude_md_rule"), cap=True)
    if not rule_text:
        rule_text = _prepared_prose(finding.get("spec_text"), cap=True)
    rule_label = RULE_SOURCE_LABEL_FALLBACK
    if rule_text and _prepared_prose(finding.get("claude_md_rule"), cap=True):
        source = finding.get("rule_source")
        if isinstance(source, str):
            rule_label = RULE_SOURCE_LABELS.get(source, RULE_SOURCE_LABEL_FALLBACK)
    if rule_text:
        parts += ["", f"**{rule_label}:**", _blockquote(rule_text)]

    # `criticality`, `failure_scenario`, `evidence`, `confidence`, and
    # `dimension` are deliberately NOT rendered into posted PR comments
    # — they are scoped to the artifact/report consumers, not
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
        open_f, close_f = _suggestion_fence(suggested_fix, offsets=fence_offsets)
        parts += ["", open_f, suggested_fix, close_f]

    return "\n".join(parts)


def render_comment_body(finding, *, fence_offsets=None):
    """Return the unfolded, marker-free rendering used by tests and delivery-key logic.

    It is byte-identical to a posted body only when no fold and no live marker
    applies; delivery composes the sections with ``compose_inline_body`` and
    appends live markers separately.
    """
    return (
        _render_group_sections(finding, [], fence_offsets=fence_offsets)
        + f"\n\n{BRAND_TRAILER}"
    )


def key_material_body(finding):
    """The bytes ``finding_key`` hashes: sections only, no trailer, ``suggested_fix_code``
    stripped (:func:`_key_material_finding`).

    Changing this rendering re-keys each affected finding on every open PR/MR.
    Rendering normalizes severity labels, so off-enum, blank or missing labels
    key as their normalized form. Canonical severities and case variants keep
    their keys; posted title, body, suggestion, rule and absent-title values key
    from their rendered forms.
    """
    return _finding_sections(_key_material_finding(finding))


def consolidate_delivery(findings):
    """Group *findings* for the posted delivery payload.

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
    # Reachable only for hand-assembled payloads: filterFindings.js stamps
    # exactly one consolidation_primary per
    # group. If a caller's data has none, don't drop the group's first-seen
    # member — treat it as the primary rather than surface `None`.
    for group in groups:
        if group["primary"] is None and group["corroborators"]:
            group["primary"] = group["corroborators"].pop(0)
    return groups


def _render_corroboration(finding):
    """Render one non-primary group member as a corroborating section."""
    agent = prepare_line(finding.get("agent", "unknown"))
    dimension = prepare_line(finding.get("dimension", "unknown"))
    confidence = finding.get("confidence")
    conf_text = prepare_line(confidence) if confidence is not None else "?"
    raw_title = finding.get("title")
    title = prepare_line(raw_title) if isinstance(raw_title, str) else ""
    body = prepare_prose(finding.get("body", ""))
    parts = [
        f"**Corroborating finding — {agent} ({dimension}, confidence {conf_text}):**",
        "",
        f"**{title or 'Finding'}**",
    ]
    if body:
        parts += ["", body]
    return "\n".join(parts)


def render_group_body(primary, corroborators, *, fence_offsets=None):
    """Build the unfolded, marker-free markdown body for one consolidation group.

    This is the marker-free rendering used by tests and delivery-key logic. It is
    byte-identical to a posted body only when no fold and no live marker applies.
    Delivery composes the sections with ``compose_inline_body`` and appends live
    markers separately. The identity trailer is appended ONCE, after the
    corroborations — one mark per delivered SURFACE, never one per element. With
    no *corroborators* the result is byte-identical to ``render_comment_body``,
    which keeps unstamped findings and degraded pipelines unaffected.
    *fence_offsets* reaches the primary's fence only: a corroborator never
    renders one (see :func:`_render_corroboration`), so a group body carries at
    most the one header, measured from the anchor the group is posted at.
    Each corroborator is appended as its own section; that appended text is
    finding-controlled, so it is run through the same ``<!--`` neutralization
    ``build_skipped_section`` applies (see its docstring). The primary's fields
    are prepared before this group is assembled.
    """
    return _render_group_sections(
        primary, corroborators, fence_offsets=fence_offsets
    ) + (f"\n\n{BRAND_TRAILER}")


def _render_group_sections(primary, corroborators, *, fence_offsets=None):
    """Render one group's sections without the identity trailer."""
    body = _finding_sections(primary, fence_offsets=fence_offsets)
    if not corroborators:
        return body
    section = "\n\n".join(_render_corroboration(c) for c in corroborators)
    section = section.replace("<!--", "&lt;!--")
    return f"{body}\n\n---\n\n{section}"


def _skipped_location(filepath, line):
    return f"{filepath}:{line}" if line is not None else str(filepath or "?")


def _quoted_location(value):
    """Keep display location text inside a delimiter longer than its tick runs."""
    value = _redact_secrets(_normalize_outbound(str(value)))
    value = re.sub(r"<(?=`+[A-Za-z/!?])", "\uff1c", value)
    value = _escape_visible(value, code=True).replace("\r", " ").replace("\n", " ")
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    delimiter = "`" * (longest + 1)
    if value.startswith(("`", " ")) or value.endswith(("`", " ")):
        value = f" {value} "
    return f"{delimiter}{value}{delimiter}"


def _plural(count, singular, plural=None):
    return singular if count == 1 else (plural or singular + "s")


def _skipped_frame(n, shown, inline_count):
    """Render the fixed part of the skipped section without any entries."""
    group_note = (
        " A finding listed here may not have an anchoring problem of its own — a "
        "consolidation group whose primary could not be anchored inline is listed "
        "here in full, corroborators included."
    )
    if inline_count is None:
        intro = (
            f"The following {shown} {_plural(shown, 'finding')} "
            f"{_plural(shown, 'references', 'reference')} lines outside this diff and "
            f"{_plural(shown, 'is', 'are')} included here instead of as inline comments:{group_note}"
        )
    else:
        intro = (
            f"{inline_count} inline {_plural(inline_count, 'comment')} "
            f"{_plural(inline_count, 'was', 'were')} posted; the following {shown} "
            f"{_plural(shown, 'finding')} {_plural(shown, 'references', 'reference')} "
            f"lines outside this diff and {_plural(shown, 'is', 'are')} included here "
            f"instead:{group_note}"
        )
    return "\n".join(
        [
            "---",
            "",
            f"### ⚠️ {n} {_plural(n, 'finding')} could not be anchored inline",
            "",
            intro,
        ]
    )


def _skipped_piece(filepath, line, finding):
    """Render and neutralize one whole skipped entry, including its heading.

    The finding fields are prepared by their renderers. The whole piece also
    neutralizes any remaining marker opener, including one in its heading.
    """
    location = _skipped_location(filepath, line)
    piece = f"\n\n#### {_quoted_location(location)}\n\n{_finding_sections(finding)}"
    return piece.replace("<!--", "&lt;!--")


def _closing_line(m, n, platform):
    limits = _body_limit(platform)
    return (
        f"_{m} of these {n} {_plural(n, 'finding')} {_plural(m, 'is', 'are')} not shown: this {limits['surface']} "
        f"reached the {limits['bytes']}-byte {limits['label']} body limit._"
    )


def build_skipped_section(skipped, inline_count=None):
    """Render the trailing section for findings that could not be anchored inline.

    *skipped* is a list of ``(filepath, line, finding)`` tuples. A member is not always
    here for its own reason: a consolidation group whose primary could not be anchored
    degrades as a whole, so corroborators are listed here too. *inline_count*, when
    given, is how many comments landed. GitLab passes no inline count because its
    summary note posts before the per-finding loop, so the landed count is not yet
    known. Entries are unbranded and use the shared frame and piece renderers. Each
    piece neutralizes its own ``<!--`` markers before fitting; that neutralization
    lives in ``_skipped_piece``. ``gauntlet.patches`` and
    ``gauntlet.marker`` cite ``build_skipped_section`` as the precedent.
    Returns ``""`` for an empty *skipped* list.
    """
    if not skipped:
        return ""
    n = len(skipped)
    return _skipped_frame(n, n, inline_count) + "".join(
        _skipped_piece(filepath, line, finding) for filepath, line, finding in skipped
    )


def _codepoint_prefix(text, allowance):
    """Return the longest prefix whose UTF-8 encoding fits *allowance*."""
    if allowance <= 0:
        return ""
    pieces = []
    used = 0
    for character in text:
        size = _utf8_len(character)
        if used + size > allowance:
            break
        pieces.append(character)
        used += size
    return "".join(pieces)


# Twin of ``foldProse`` in ``workflows/src/renderReport.js``.
def _open_fence(text, *, strict=False, intervals=None):
    """Return the final open prose fence as ``(char, length, offset)``."""

    state = None
    opened_at = None
    line_start = 0
    index = 0

    def line_run(line):
        indent = 0
        while indent < 3 and indent < len(line) and line[indent] == " ":
            indent += 1
        if indent >= len(line) or line[indent] not in "`~":
            return None
        char = line[indent]
        end = indent
        while end < len(line) and line[end] == char:
            end += 1
        return char, end - indent, end, indent

    def visit(line, offset, line_end):
        nonlocal state, opened_at
        run = line_run(line)
        if state is not None:
            if (
                run is not None
                and run[0] == state[0]
                and run[1] >= state[1]
                and all(character in " \t" for character in line[run[2] :])
            ):
                state = None
                if intervals is not None:
                    intervals.append((opened_at, line_end))
            return
        if (
            run is not None
            and run[1] >= 3
            and (not strict or run[3] == 0)
            and not (run[0] == "`" and "`" in line[run[2] :])
        ):
            state = (run[0], run[1], offset + run[3])
            opened_at = offset

    while index < len(text):
        if text[index] in "\r\n":
            visit(text[line_start:index], line_start, index + 1)
            if (
                text[index] == "\r"
                and index + 1 < len(text)
                and text[index + 1] == "\n"
            ):
                index += 2
            else:
                index += 1
            line_start = index
        else:
            index += 1
    visit(text[line_start:], line_start, len(text))
    if intervals is not None and state is not None:
        intervals.append((opened_at, len(text)))
    return state


def _fence_closer(prefix):
    state = _open_fence(prefix)
    return "" if state is None else state[0] * state[1]


def _code_intervals(text):
    """Return paired single-line spans and trusted fences as separate intervals."""
    fences = []
    intervals = []
    _open_fence(text, intervals=fences)
    offset = 0
    for line in text.split("\n"):
        if any(start <= offset < end for start, end in fences):
            offset += len(line) + 1
            continue
        cursor = 0
        while cursor < len(line):
            start = line.find("`", cursor)
            if start < 0:
                break
            end = start
            while end < len(line) and line[end] == "`":
                end += 1
            if _escaped_tick(line, start):
                cursor = start + 1
                continue
            width = end - start
            close = end
            while close < len(line):
                close = line.find("`", close)
                if close < 0:
                    break
                after = close
                while after < len(line) and line[after] == "`":
                    after += 1
                if after - close == width:
                    intervals.append((offset + start, offset + after))
                    cursor = after
                    break
                close = after
            else:
                cursor = end
            if close < 0:
                cursor = end
        offset += len(line) + 1
    return intervals, fences


def _retreat_inside_span(text, cut, intervals):
    for start, end in intervals:
        if start < cut < end:
            return text[:start]
    return text[:cut]


def _cut_unclosed_comment(prefix, intervals=None):
    """Remove the first unclosed HTML comment and everything after it."""

    if intervals is None:
        spans, fences = _code_intervals(prefix)
        intervals = spans + fences

    position = 0
    while True:
        opener = prefix.find("<!--", position)
        if opener < 0:
            return prefix
        if any(start <= opener < end for start, end in intervals):
            position = opener + 4
            continue
        closer = prefix.find("-->", opener + 4)
        if closer < 0:
            return prefix[:opener]
        position = closer + 3


def _drop_last_line(prefix):
    """Drop the final logical line, accepting LF, CRLF, and lone CR."""

    ended_with_line_ending = prefix.endswith(("\r", "\n"))
    end = len(prefix)
    if prefix.endswith("\r\n"):
        end -= 2
    elif prefix.endswith(("\r", "\n")):
        end -= 1
    separators = [prefix.rfind("\n", 0, end), prefix.rfind("\r", 0, end)]
    line_start = max(separators)
    if line_start < 0:
        return ""
    if ended_with_line_ending:
        return prefix[: line_start + 1]
    if prefix[line_start] == "\n" and line_start and prefix[line_start - 1] == "\r":
        line_start -= 1
    return prefix[:line_start]


def _retreat_fold_prefix(
    prefix,
    *,
    cut_inside_line,
    suggestion_start=None,
    intervals=(),
    comment_intervals=None,
):
    """Retreat a fold prefix and re-cut any HTML comment it exposes."""
    if suggestion_start is not None:
        prefix = prefix[:suggestion_start]
        cut_inside_line = False
    elif cut_inside_line and not prefix.endswith(("\n", "\r")):
        prefix = prefix[:-1]
    else:
        prefix = _drop_last_line(prefix)
        cut_inside_line = False
    prefix = _retreat_inside_span(prefix, len(prefix), intervals)
    return _cut_unclosed_comment(prefix, comment_intervals), cut_inside_line


def _fold_review_body(text, allowance, platform):
    """Fold *text* into *allowance* bytes while preserving lines and code points."""
    limits = _body_limit(platform)
    total = _utf8_len(text)
    intervals, fences = _code_intervals(text)
    comment_intervals = intervals + fences

    def fold_line_for(byte_count):
        return (
            f"_[folded: {byte_count} more bytes; this {limits['surface']} reached the "
            f"{limits['bytes']}-byte {limits['label']} body limit]_"
        )

    # This is only the shortest provisional fence reserve. The final assembly
    # below measures the actual opener character and length.
    reserve = _utf8_len(f"\n\n{fold_line_for(total)}\n{_fence_closer('```')}")
    if allowance < reserve:
        prefix = ""
        cut_inside_line = False
    else:
        prefix_allowance = allowance - reserve
        prefix = ""
        cut_inside_line = False
        for index, line in enumerate(text.split("\n")):
            next_part = line if index == 0 else f"\n{line}"
            remaining = prefix_allowance - _utf8_len(prefix)
            if _utf8_len(next_part) <= remaining:
                prefix += next_part
                continue
            if _utf8_len(line) > prefix_allowance:
                partial = _codepoint_prefix(next_part, remaining)
                prefix += partial
                cut_inside_line = bool(partial) and not partial.endswith(("\n", "\r"))
            break

    prefix = _retreat_inside_span(text, len(prefix), intervals)
    prefix = _cut_unclosed_comment(prefix, comment_intervals)

    while True:
        closer = _fence_closer(prefix)
        dropped_bytes = total - _utf8_len(prefix)
        fold_line = fold_line_for(dropped_bytes)
        separator = "" if not closer or prefix.endswith(("\n", "\r")) else "\n"
        folded = f"{prefix}{separator}{closer}\n\n{fold_line}"
        if _utf8_len(folded) <= allowance or not prefix:
            return folded, dropped_bytes
        # After the overlong-line cut the prefix ends mid-line, so retreat one code
        # point at a time: dropping the line would discard the partial that a cut
        # inside an opener run keeps (the PARTIAL fixture row).
        prefix, cut_inside_line = _retreat_fold_prefix(
            prefix,
            cut_inside_line=cut_inside_line,
            intervals=intervals,
            comment_intervals=comment_intervals,
        )


def _open_suggestion_line(prefix, state):
    """Return the opener line start when *state* is a committable suggestion."""
    if state is None:
        return None
    _char, length, offset = state
    line_start = max(prefix.rfind("\n", 0, offset), prefix.rfind("\r", 0, offset)) + 1
    line_end = len(prefix)
    for separator in ("\n", "\r"):
        candidate = prefix.find(separator, offset)
        if candidate >= 0:
            line_end = min(line_end, candidate)
    line = prefix[line_start:line_end]
    delimiter = offset - line_start
    if line[delimiter + length :].startswith("suggestion"):
        return line_start
    return None


def _fold_inline_body(sections, allowance, platform, surface):
    """Fold inline *sections* into *allowance* bytes without breaking markdown."""
    limits = _body_limit(platform, surface)
    total = _utf8_len(sections)
    intervals, fences = _code_intervals(sections)
    comment_intervals = intervals + fences

    def fold_line_for(byte_count):
        return (
            f"_[folded: {byte_count} more bytes; this {limits['surface']} reached the "
            f"{limits['bytes']}-byte {limits['label']} body limit]_"
        )

    # Reserve the fold line at the maximum digit count and the shortest possible
    # synthetic closer. The final assembly measures the actual fence and retreats
    # whole lines when the closer is longer.
    reserve = _utf8_len(f"\n\n{fold_line_for(total)}\n{_fence_closer('```')}")
    if allowance < reserve:
        prefix = ""
        cut_inside_line = False
    else:
        prefix_allowance = allowance - reserve
        prefix = ""
        cut_inside_line = False
        for index, line in enumerate(sections.split("\n")):
            next_part = line if index == 0 else f"\n{line}"
            remaining = prefix_allowance - _utf8_len(prefix)
            if _utf8_len(next_part) <= remaining:
                prefix += next_part
                continue
            if _utf8_len(line) > prefix_allowance:
                partial = _codepoint_prefix(next_part, remaining)
                prefix += partial
                cut_inside_line = bool(partial) and not partial.endswith(("\n", "\r"))
            break

    prefix = _retreat_inside_span(sections, len(prefix), intervals)
    prefix = _cut_unclosed_comment(prefix, comment_intervals)

    while True:
        state = _open_fence(prefix)
        suggestion_start = _open_suggestion_line(prefix, state)
        if suggestion_start is not None:
            # A partial committable suggestion is worse than omitting its patch:
            # closing it would turn an incomplete patch into a valid wrong patch.
            prefix, cut_inside_line = _retreat_fold_prefix(
                prefix,
                cut_inside_line=cut_inside_line,
                suggestion_start=suggestion_start,
                intervals=intervals,
                comment_intervals=comment_intervals,
            )
            continue

        closer = "" if state is None else state[0] * state[1]
        dropped_bytes = total - _utf8_len(prefix)
        fold_line = fold_line_for(dropped_bytes)
        separator = "" if not closer or prefix.endswith(("\n", "\r")) else "\n"
        folded = f"{prefix}{separator}{closer}\n\n{fold_line}"
        if _utf8_len(folded) <= allowance or not prefix:
            return folded, dropped_bytes
        # After an overlong-line cut the prefix ends mid-line, so retreat one code
        # point at a time. This preserves the shared summary fold's opener behavior.
        prefix, cut_inside_line = _retreat_fold_prefix(
            prefix,
            cut_inside_line=cut_inside_line,
            intervals=intervals,
            comment_intervals=comment_intervals,
        )


def _delivery_marker_suffix(sha, keys):
    """Return the live-only suffix for the findings carried by one delivery."""
    if not is_sha_shaped(sha) or not keys:
        return ""
    return "\n\n" + "\n".join(build_finding_marker(sha, key) for key in keys)


def compose_inline_body(sections, *, platform, surface, marker_suffix=""):
    """Compose and budget one complete inline body, reserving its live markers."""
    limits = _body_limit(platform, surface)
    trailer = f"\n\n{BRAND_TRAILER}"
    allowance = limits["bytes"] - _utf8_len(trailer) - _utf8_len(marker_suffix)
    if _utf8_len(sections) <= allowance:
        return InlineBody(sections + trailer, 0)
    folded, folded_bytes = _fold_inline_body(sections, allowance, platform, surface)
    return InlineBody(folded + trailer, folded_bytes)


def _inline_body_over_limit(composed, marker_suffix, platform, surface):
    """Handle an inline envelope too small even for its synthetic fold."""
    limits = _body_limit(platform, surface)
    actual = _utf8_len(composed.body + marker_suffix)
    if actual <= limits["bytes"]:
        return False
    message = (
        f"The composed {limits['surface']} is {actual} bytes, over the "
        f"{limits['bytes']}-byte {limits['label']} body limit"
    )
    if platform == "github":
        die(message + "; nothing was posted.")
    warn(message + "; skipping this delivery.")
    return True


def _report_inline_budget(composed, platform, surface, filepath, line):
    """Report an inline fold without changing stdout or dry-run capture."""
    if not composed.folded_bytes:
        return
    limits = _body_limit(platform, surface)
    path = filepath or "?"
    warn(
        f"Inline body folded by {composed.folded_bytes} bytes at {path}:{line}: "
        f"this {limits['surface']} reached the {limits['bytes']}-byte "
        f"{limits['label']} body limit."
    )


def _bounded_section(n, shown_entries, inline_count, platform):
    section = _skipped_frame(n, len(shown_entries), inline_count)
    section += "".join(
        _skipped_piece(filepath, line, finding)
        for filepath, line, finding in shown_entries
    )
    omitted = n - len(shown_entries)
    if omitted:
        section += f"\n\n{_closing_line(omitted, n, platform)}"
    return section


def _compose_fragments(review_body, section, footer):
    fragments = [BRAND_SUMMARY_HEADER]
    if review_body:
        fragments.append(review_body)
    if section:
        fragments.append(section)
    return "\n\n".join(fragments) + footer


def _standalone_footer_lines(review_body, sha):
    if not isinstance(review_body, str):
        return ""
    sha_text = str(sha)
    if not sha_text:
        return ""
    footer_prefix = build_prose_footer(sha).split(sha_text, 1)[0]
    prefixes = (footer_prefix, f"Generated by {LEGACY_PRODUCT} | Reviewed up to: ")
    return "\n".join(
        line.lstrip(" \t")
        for line in review_body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.lstrip(" \t").startswith(prefixes)
    )


def _skipped_reserve(n, inline_count, platform):
    # The widest count and both grammar forms bound every possible shown count.
    frame = max(_utf8_len(_skipped_frame(n, shown, inline_count)) for shown in (0, n))
    closing = _utf8_len(_closing_line(n, n, platform))
    return frame + closing


def compose_review_body(
    review_body, skipped_groups, *, platform, findings_count, sha, inline_count=None
):
    """Compose and budget the complete summary comment for one platform.

    The fast path preserves the prepared body and any existing footer halves.
    The bounded path reserves the header, the skipped-section frame and closing line,
    and builds the canonical footer before folding the supplied prose or fitting whole
    skipped groups in list order. No output is printed. This is deliberately not
    idempotent by regex: a hand-typed heading in ``review_body`` yields two headings
    once and self-heals, whereas a phrase-sniffing stripper would make identity depend
    on counting prose. Preparation removes terminated comments before footer
    deduplication; prose-footer deduplication sees only standalone footer lines
    with the same head SHA. The bounded path always appends the full canonical
    footer, and skipped-finding text never reaches the dedup.
    """
    review_body = prepare_prose(review_body)
    limits = _body_limit(platform)
    skipped = [entry for group in skipped_groups for entry in group]
    n = len(skipped)
    section = build_skipped_section(skipped, inline_count)
    footer = build_footer(
        findings_count,
        sha,
        body=review_body,
        prose_body=_standalone_footer_lines(review_body, sha),
    )
    body = _compose_fragments(review_body, section, footer)
    if _utf8_len(body) <= limits["bytes"]:
        return ComposedBody(body, n, 0, 0, ())

    footer = build_footer(findings_count, sha, body="")
    fixed = _utf8_len(BRAND_SUMMARY_HEADER) + _utf8_len(footer)
    if review_body:
        fixed += _utf8_len("\n\n")
    if n:
        fixed += _utf8_len("\n\n\n\n") + _skipped_reserve(n, inline_count, platform)
    allowance = limits["bytes"] - fixed

    if review_body and _utf8_len(review_body) > allowance:
        effective_body, folded_bytes = _fold_review_body(
            review_body, allowance, platform
        )
        section = _bounded_section(n, [], inline_count, platform) if n else ""
        body = _compose_fragments(effective_body, section, footer)
        return ComposedBody(
            body,
            0,
            n,
            folded_bytes,
            tuple(
                (_skipped_location(filepath, line), finding.get("title", "Finding"))
                for filepath, line, finding in skipped
            ),
        )

    remaining = allowance - (_utf8_len(review_body) if review_body else 0)
    shown_entries = []
    omitted_entries = []
    for group in skipped_groups:
        pieces = [_skipped_piece(*entry) for entry in group]
        group_size = sum(_utf8_len(piece) for piece in pieces)
        if group_size <= remaining:
            shown_entries.extend(group)
            remaining -= group_size
        else:
            omitted_entries.extend(group)
    section = _bounded_section(n, shown_entries, inline_count, platform) if n else ""
    body = _compose_fragments(review_body, section, footer)
    return ComposedBody(
        body,
        len(shown_entries),
        len(omitted_entries),
        0,
        tuple(
            (_skipped_location(filepath, line), finding.get("title", "Finding"))
            for filepath, line, finding in omitted_entries
        ),
    )


def _refuse_over_limit(body, platform):
    """Refuse a summary body that still exceeds its platform limit."""
    limits = _body_limit(platform)
    actual = _utf8_len(body)
    if actual > limits["bytes"]:
        die(
            f"The composed {limits['surface']} is {actual} bytes, over the "
            f"{limits['bytes']}-byte {limits['label']} body limit; nothing was posted."
        )


def _report_summary_budget(composed, platform):
    """Report omitted entries and prose folding without changing dry-run capture."""
    limits = _body_limit(platform)
    if composed.omitted:
        print(
            f"  {composed.omitted} skipped finding(s) not shown: the "
            f"{limits['surface']} reached the {limits['bytes']}-byte "
            f"{limits['label']} body limit."
        )
        for location, title in composed.omitted_entries:
            warn(
                f"Skipped finding '{title}' at {location} not shown: the "
                f"{limits['surface']} reached the {limits['bytes']}-byte "
                f"{limits['label']} body limit."
            )
    if composed.folded_bytes:
        print(
            f"  review_body folded by {composed.folded_bytes} bytes: the "
            f"{limits['surface']} reached the {limits['bytes']}-byte "
            f"{limits['label']} body limit."
        )


def summary_body_from_report(report):
    """Return the body between the rendered Summary and next code-owned heading."""
    marker = "## Summary\n\n"
    start = report.find(marker)
    if start < 0:
        die("Report does not contain a rendered Summary section.")
    body_start = start + len(marker)
    cursor = body_start
    while cursor <= len(report):
        end = report.find("\n", cursor)
        if end < 0:
            line = report[cursor:]
            next_cursor = len(report) + 1
        else:
            line = report[cursor:end]
            next_cursor = end + 1
        if line.endswith("\r"):
            line = line[:-1]
        if line in CODE_OWNED_HEADINGS:
            body = report[body_start:cursor]
            return body[:-2] if body.endswith("\n\n") else body
        cursor = next_cursor
    die("Report does not contain a following code-owned heading after Summary.")


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
# ``build_footer`` is imported from gauntlet.marker and re-exported here,
# so this module has no second definition of the
# signal it writes.


def get_head_sha():
    stdout, _, rc = proc.output(["git", "rev-parse", "HEAD"])
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


def post_github(data, valid_lines, line_texts, *, forge: Forge):
    # Require both diff oracles so omission cannot silently disable apply checks.
    owner = data["owner"]
    repo = data["repo"]
    pr_number = data["pr_number"]
    findings = data.get("findings", [])

    ensure_available(forge)

    # consolidate_delivery(findings) is materialized ONCE: the pre-pass
    # below and the render loop that follows it walk the SAME list of groups by
    # index, so a demotion decided by the pre-pass lands on the exact group the
    # render loop later renders.
    groups = consolidate_delivery(findings)

    # Pure, SILENT pre-pass: decide which kept fences would collide, on
    # GitLab's own closed-interval overlap semantic, with another kept fence in
    # the same file. No warn_skip, no tally, no _gated_finding call here — every
    # existing warning still fires exactly once, from its existing render-loop
    # site below, in loop order (byte-stability constraint: a findings set with
    # no overlapping kept fences produces zero losers and is untouched by this
    # pass). The candidate predicate and index basis are `_github_overlap_records`'s
    # own docstring — this poster and the benchmark mirror both call it rather
    # than each keeping their own copy.
    overlap_records = _github_overlap_records(groups, valid_lines, line_texts)
    losers = _overlap_losers(overlap_records)

    comments = []
    skipped_groups = []  # one list of (filepath, line, finding) per degraded group
    # One posted comment per consolidation group: findings without a stamp
    # are each their own single-member group, so this loop is unchanged for them.
    for index, group in enumerate(groups):
        primary = group["primary"]
        corroborators = group["corroborators"]
        line = primary.get("line")
        if line is None:
            _warn_group_skipped(group, valid_lines)
            skipped_groups.append(
                [
                    _degraded_entry(
                        primary.get("file", "?"), None, primary, valid_lines, line_texts
                    )
                ]
            )
            # The primary can't anchor, so the whole group degrades into the
            # skipped section as individual entries — the corroborators never
            # merged into a comment that itself never gets posted.
            for c in corroborators:
                skipped_groups[-1].append(
                    _degraded_entry(
                        c.get("file", "?"), c.get("line"), c, valid_lines, line_texts
                    )
                )
            continue

        # Resolve to the diff's own spelling BEFORE validating: a prefixed
        # finding that only the STRIPPED form validates must ship that
        # stripped path as `comment["path"]`, or GitHub 422s the whole review
        # on a path the PR does not have.
        filepath = diff_path_spelling(valid_lines, primary["file"], line)
        if not is_line_valid(valid_lines, filepath, line):
            _warn_group_skipped(group, valid_lines, filepath)
            skipped_groups.append(
                [_degraded_entry(filepath, line, primary, valid_lines, line_texts)]
            )
            for c in corroborators:
                skipped_groups[-1].append(
                    _degraded_entry(
                        c.get("file", "?"), c.get("line"), c, valid_lines, line_texts
                    )
                )
            continue

        # The multi-line anchor decision is made HERE, ABOVE the body render, and
        # the assembly below consumes these same locals — the decision moved, it is
        # not duplicated. The apply-check has to see the range the comment will
        # REALLY apply at, and that range is only known once this has run.
        # `_github_apply_range` is the one function that makes it, so
        # this loop, the overlap pre-pass above, and the benchmark's payload
        # mirror all call it rather than each computing their own copy.
        #
        # start_line is added for multi-line comments, but only when the whole range
        # sits inside one hunk — GitHub rejects the ENTIRE review POST (losing every
        # finding, not just this one) with a 422 "Line could not be resolved" if
        # end_line falls outside every hunk, even though `line` alone was valid.
        end_line = primary.get("end_line")
        multiline, apply_range = _github_apply_range(
            valid_lines, filepath, line, end_line
        )
        gated = _gated_finding(
            primary,
            apply_range,
            valid_lines,
            line_texts,
            demote_reason=(_FIX_OVERLAPS_KEPT_FENCE if index in losers else None),
        )
        composed = compose_inline_body(
            _render_group_sections(gated, corroborators),
            platform="github",
            surface="inline",
        )
        _inline_body_over_limit(composed, "", "github", "inline")
        _report_inline_budget(composed, "github", "inline", filepath, line)
        comment = {
            "path": filepath,
            "line": line,
            "side": "RIGHT",
            "body": composed.body,
        }
        if multiline:
            comment["start_line"] = line
            comment["start_side"] = "RIGHT"
            comment["line"] = end_line

        comments.append(comment)

    # The partition (comments vs skipped groups) is complete above. The fast path
    # checks the marker against the full review_body and prose against standalone
    # lines carrying the head SHA; the bounded path always appends the full canonical
    # footer, and skipped-finding text never reaches the dedup.
    sha = resolve_marker_sha(data)
    review_body = data.get("review_body", "")
    composed = compose_review_body(
        review_body,
        skipped_groups,
        platform="github",
        findings_count=len(findings),
        sha=sha,
        inline_count=len(comments),
    )

    payload = {
        "body": composed.body,
        "event": "COMMENT",
        "comments": comments,
    }

    _refuse_over_limit(composed.body, "github")
    resp = post_json(
        github_review_request(ReviewTarget(owner, repo, pr_number), payload),
        forge=forge,
    )
    if DRY_RUN:
        print("Review captured (dry-run).")
        print(f"  {len(comments)} inline comment(s) captured.")
    else:
        url = resp.get("html_url", resp.get("id", "posted"))
        print(f"Review posted: {url}")
        print(f"  {len(comments)} inline comment(s) posted.")
    flat_skipped = [entry for group in skipped_groups for entry in group]
    if flat_skipped:
        if composed.omitted:
            skipped_line = (
                f"  {composed.shown} of {len(flat_skipped)} finding(s) skipped inline "
                "(lines not in diff) — appended to review body."
            )
        else:
            skipped_line = (
                f"  {len(flat_skipped)} finding(s) skipped inline (lines not in diff) — "
                "appended to review body."
            )
        print(skipped_line)
    _report_summary_budget(composed, "github")
    _print_fix_summary()
    return 0


# ---------------------------------------------------------------------------
# GitLab delivery
# ---------------------------------------------------------------------------


def fetch_gitlab_shas(target: ReviewTarget, *, forge: GitLab):
    """Fetch latest MR version SHAs from GitLab."""
    ensure_available(forge)
    result = forge.diff_refs(target)
    if result.error is not None:
        die(result.error)
    versions: Any = result.payload

    if not versions:
        die("MR versions endpoint returned an empty list.")

    latest = versions[0]
    return (
        latest["base_commit_sha"],
        latest["head_commit_sha"],
        latest["start_commit_sha"],
    )


def gitlab_prior_delivery(
    owner: str, repo: str, mr_iid: int | str, sha: object, *, forge: Forge
) -> PriorDelivery:
    """Read this SHA's summary, finding keys, and legacy group keys from one snapshot.

    Dry-run skips the read so every finding is captured and the summary stays first.
    An invalid SHA cannot key deduplication. Fetch failure warns and delivers everything
    because a possible duplicate beats a silently dropped review.
    """
    if DRY_RUN or not is_sha_shaped(sha):
        return PriorDelivery(False, frozenset(), frozenset(), None)
    state = gitlab_prior_delivery_state(owner, repo, mr_iid, sha, forge=forge)
    if state.error:
        warn(
            f"could not check for an existing summary note or already-delivered inline "
            f"discussions ({state.error}); posting them."
        )
    return state


def post_gitlab(data, valid_lines, new_files, old_paths, line_texts, *, forge: GitLab):
    # Require every diff oracle so omission cannot silently disable apply checks.
    owner = data["owner"]
    repo = data["repo"]
    mr_iid = data["pr_number"]
    findings = data.get("findings", [])

    ensure_available(forge)

    shas = fetch_gitlab_shas(ReviewTarget(owner, repo, mr_iid), forge=forge)
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

    def body_factory(finding, corroborators=(), *, demote_reason=None):
        """Return the body renderer ``deliver`` calls with the anchor it posts at.

        A GitLab position is always single-line; the ```suggestion:-m+n header is
        what widens the apply range to ``[anchor - m, anchor + n]``. Those
        offsets are therefore a property of the ANCHOR, not of the finding — and
        handing ``deliver`` a renderer instead of rendered bytes is what stops a
        body from being built for one anchor and posted at another.

        *demote_reason* passes straight through to ``_gitlab_anchored`` —
        the caller's set-level overlap decision for THIS finding, made once by
        the pre-pass below and threaded here rather than recomputed per anchor.
        """

        def make_body(anchor):
            gated, offsets = _gitlab_anchored(
                finding, anchor, valid_lines, line_texts, demote_reason=demote_reason
            )
            return _render_group_sections(gated, corroborators, fence_offsets=offsets)

        return make_body

    # Pre-partition the deterministic skips (no line number, or a line the diff never
    # touched) BEFORE the summary note is composed — the note is posted first, so the
    # skipped section must already be known. Both checks are pure functions of facts
    # already fetched above (valid_lines, the finding's own file/line), so this is
    # exactly the decision the inline loop below would make; it is just made early for
    # the findings that will never reach that loop. `remaining` carries each finding's
    # resolved filepath through to the loop so it is not re-derived.
    skipped_groups = []  # one list of (filepath, line, finding) per degraded group
    remaining = []  # (filepath, group) — groups that reach the inline loop
    groups = consolidate_delivery(findings)
    # One posted discussion per consolidation group: findings without a
    # stamp are each their own single-member group, so this loop is unchanged
    # for them.
    for group in groups:
        primary = group["primary"]
        corroborators = group["corroborators"]
        line = primary.get("line")
        if line is None:
            _warn_group_skipped(group, valid_lines)
            skipped_groups.append(
                [
                    _degraded_entry(
                        primary.get("file", "?"), None, primary, valid_lines, line_texts
                    )
                ]
            )
            # The primary can't anchor, so the whole group degrades into the
            # skipped section as individual entries.
            for c in corroborators:
                skipped_groups[-1].append(
                    _degraded_entry(
                        c.get("file", "?"), c.get("line"), c, valid_lines, line_texts
                    )
                )
            continue

        # Same spelling resolution the loop below applies — see its comment.
        filepath = diff_path_spelling(valid_lines, primary["file"], line)
        if not is_line_valid(valid_lines, filepath, line):
            _warn_group_skipped(group, valid_lines, filepath)
            skipped_groups.append(
                [_degraded_entry(filepath, line, primary, valid_lines, line_texts)]
            )
            for c in corroborators:
                skipped_groups[-1].append(
                    _degraded_entry(
                        c.get("file", "?"), c.get("line"), c, valid_lines, line_texts
                    )
                )
            continue

        remaining.append((filepath, group))

    # Pure, SILENT pre-pass, same shape and same discipline as GitHub's —
    # BEFORE the summary note or any discussion posts, so the decision is made
    # once, statically, and never depends on what has or hasn't gone out live
    # yet (the demoted set is a pure function of findings + diff,
    # rerun-stable, dry-run == live). The candidate predicate and index basis
    # are `_gitlab_overlap_records`'s own docstring — this poster and the
    # benchmark mirror both call it rather than each keeping their own copy.
    # `kept_intervals` is the read-only map `deliver_corroborator`'s reactive
    # fence sites consult — it names every WINNING candidate's apply
    # range per path, never a loser's (the `if index not in losers:` guard
    # below is load-bearing: a loser's own range must never occupy anything).
    overlap_records = _gitlab_overlap_records(remaining, valid_lines, line_texts)
    losers = _overlap_losers(overlap_records)
    kept_intervals: dict[Any, list[Any]] = {}
    for index, filepath, apply_range in overlap_records:
        if index not in losers:
            kept_intervals.setdefault(filepath, []).append(apply_range)

    sha = resolve_marker_sha(data)
    review_body = data.get("review_body", "")
    # The pre-partition above is complete before the summary note. The fast path
    # checks the marker against the full review_body and prose against standalone
    # lines carrying the head SHA; the bounded path always appends the full canonical
    # footer, and skipped-finding text never reaches the dedup.
    composed = compose_review_body(
        review_body,
        skipped_groups,
        platform="gitlab",
        findings_count=len(findings),
        sha=sha,
    )

    summary_payload = {"body": composed.body}
    prior = gitlab_prior_delivery(owner, repo, mr_iid, sha, forge=forge)
    delivered_keys = prior.finding_keys
    legacy_group_keys = prior.legacy_group_keys
    # Same predicate that makes gitlab_prior_delivery skip the fetch: a marker built
    # from a non-SHA-shaped sha (get_head_sha's "unknown" fallback) is one
    # find_finding_marker is guaranteed to reject, so appending it would leave an
    # unreadable comment on every discussion and dedup nothing.
    if prior.summary_posted:
        print(f"MR summary note for {sha} already on the MR — skipping.")
    else:
        _refuse_over_limit(composed.body, "gitlab")
        post_json(
            gitlab_note_request(ReviewTarget(owner, repo, mr_iid), summary_payload),
            forge=forge,
        )
        print(
            "MR summary note captured (dry-run)."
            if DRY_RUN
            else "MR summary note posted."
        )
        _report_summary_budget(composed, "gitlab")

    # Post each finding as an inline discussion. Every finding lands in exactly one of
    # the five counters below, so the outcome reported at the end is a partition of
    # `findings` — a run cannot both under-report and claim success. The flattened
    # skipped groups above are reported after the summary-note decision; `remaining`
    # carries only what that pre-partition let through, filepath already resolved.
    def member_key(m, filepath, line):
        """Return the delivery key for ONE finding, independent of what posts it.

        Always derived from the member's own anchor and its SINGLE-finding render,
        never from the group body it may happen to ride in. That is what makes the
        two delivery shapes interchangeable for dedup: a corroborator posted on its
        own by one run is recognized by the group discussion of the next, and vice
        versa.

        The key render also drops ``suggested_fix_code`` UNCONDITIONALLY (see
        :func:`_key_material_finding`), so a key is fence-independent: the apply-check
        can strip a fence here and keep it there without ever moving a delivery key.
        """
        raw_title = m.get("title")
        title = prepare_line(raw_title) if isinstance(raw_title, str) else ""
        return finding_key(
            filepath,
            line,
            title,
            key_material_body(m),
        )

    def deliver(f, filepath, line, make_body, keys):
        """Post ONE discussion and return its outcome counter name.

        The group's single discussion and each fallback per-corroborator
        discussion go through this same validate → POST → dedup → count path,
        so a corroborator that is posted on its own is accounted for exactly
        like any never-grouped finding. *keys* is the delivery key of every
        finding this one discussion carries — the caller owns the mapping,
        because only it knows which findings share a body.

        *make_body* is called with the SAME *line* written into
        ``position.new_line`` below, so a fence's offsets cannot be measured
        from an anchor the discussion is not posted at.
        """
        # Render before the dedup check: the apply-check runs at render sites, so
        # the body is still gated even when a rerun posts nothing. The fold notice
        # belongs after dedup because it describes a delivery that was attempted.
        sections = make_body(line)
        marker_suffix = _delivery_marker_suffix(sha, keys)
        composed = compose_inline_body(
            sections,
            platform="gitlab",
            surface="discussion",
            marker_suffix=marker_suffix,
        )
        if _inline_body_over_limit(composed, marker_suffix, "gitlab", "discussion"):
            return "failed"
        if keys and all(k in delivered_keys for k in keys):
            # An earlier run already delivered every finding in this discussion for
            # this sha. Reposting it is duplication, not a
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
        # both sides; new_line alone is rejected with 400 `line_code can't be blank`.
        # An added line has no old side — omit the key rather than
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
            # A RENAMED file must anchor `old_path` to its PRE-RENAME path — the
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
        _report_inline_budget(composed, "gitlab", "discussion", filepath, line)

        payload = {
            "body": composed.body if DRY_RUN else composed.body + marker_suffix,
            "position": position,
        }

        _response, error = try_post_json(
            gitlab_discussion_request(ReviewTarget(owner, repo, mr_iid), payload),
            forge=forge,
        )
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
        gated = _gated_finding(c, None, valid_lines, line_texts)
        marker_suffix = _delivery_marker_suffix(sha, [key])
        composed = compose_inline_body(
            _finding_sections(gated),
            platform="gitlab",
            surface="note",
            marker_suffix=marker_suffix,
        )
        if _inline_body_over_limit(composed, marker_suffix, "gitlab", "note"):
            return "failed"
        _report_inline_budget(composed, "gitlab", "note", c.get("file"), c.get("line"))
        payload = {"body": composed.body if DRY_RUN else composed.body + marker_suffix}
        _response, error = try_post_json(
            gitlab_note_request(ReviewTarget(owner, repo, mr_iid), payload), forge=forge
        )
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

        This is a REACTIVE fence site: its own anchor was never a
        candidate in the pre-pass above (the pre-pass only ever sees a group's
        primary), so it QUERIES `kept_intervals` read-only — never claims an
        interval of its own — and demotes when its stated closed interval
        intersects an already-KEPT one on the same path. Two residuals remain:
        a corroborator that collides only with another reactive
        corroborator (this call site cannot see a sibling it has not been
        called for yet), and a corroborator that collides with a WINNING
        primary whose own discussion is lost late (malformed position, or a
        rejected POST) — that primary still occupies its interval in
        `kept_intervals`, so the corroborator can lose its affordance to a
        fence that never actually landed. Both are accepted: the map is built
        once by the pure pre-pass and deliberately never depends on live
        delivery outcomes.
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
        demote_reason = None
        if "suggested_fix_code" in c:
            apply_range, _offsets, _cap_exceeded = _gitlab_apply_range(c, line)
            if any(
                _ranges_overlap(apply_range, kept)
                for kept in kept_intervals.get(filepath, ())
            ):
                demote_reason = _FIX_OVERLAPS_KEPT_FENCE
        return deliver(
            c,
            filepath,
            line,
            body_factory(c, demote_reason=demote_reason),
            [member_key(c, filepath, line)],
        )

    counters = {
        "posted": 0,
        "already_present": 0,
        "invalid": 0,
        "failed": 0,
    }
    for index, (filepath, group) in enumerate(remaining):
        f = group["primary"]
        corroborators = group["corroborators"]
        # Decided once by the pure pre-pass above — independent of
        # everything below (prior-delivery state, live-POST outcomes), so a
        # rerun always reaches the same verdict for this same index.
        demote_reason = _FIX_OVERLAPS_KEPT_FENCE if index in losers else None
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
            # This group's primary key was found on an older group body that
            # rendered a corroborator's content without ever giving it a key of
            # its own (see prior_delivery_from_entries). That body IS this group's
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
            # that content on the MR twice, so deliver only what is
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
                    body_factory(f, demote_reason=demote_reason),
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
            body_factory(f, corroborators, demote_reason=demote_reason),
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
    flat_skipped = [entry for group in skipped_groups for entry in group]
    if flat_skipped:
        print(f"  {len(flat_skipped)} finding(s) skipped.")
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
    # discussions are standing on the MR from an earlier run.
    standing = (
        f" {already_present} from an earlier run remain on the MR."
        if already_present
        else ""
    )

    if failed:
        print(f"  {failed} inline discussion(s) not delivered (see warnings above).")
        if posted == 0:
            # Every attempt was made first — this exit reports the outcome, it does not
            # abandon the batch. A partial delivery is a success with warnings, but a
            # run that delivered nothing NEW and had a rejection is a failure, and the
            # message counts only what THIS run attempted: discussions an earlier run
            # already placed are neither successes of this one nor part of the total,
            # and a malformed position never reached the wire to be "attempted".
            die(
                f"all {failed} finding(s) attempted this run were not delivered "
                f"— nothing new was posted inline.{standing} The MR summary note "
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


def build_dry_run_payload(platform):
    """Transform the captured API calls + skip warnings into the payload shape.

    GitHub posts a single review, so the payload exposes ``endpoint`` / ``method``
    / ``payload`` for that one call. GitLab posts a summary note followed by one
    discussion per finding, so the first capture becomes ``summary`` and the rest
    become ``discussions``. A ``discussions`` body is the rendered comment alone —
    the per-finding delivery marker is appended on the live wire only.
    """
    if platform == "github":
        cap = _CAPTURED[0] if _CAPTURED else None
        return {
            "platform": "github",
            "endpoint": cap.endpoint if cap is not None else "",
            "method": cap.method if cap is not None else "POST",
            "payload": cap.payload if cap is not None else {},
            "skipped": list(_SKIP_WARNINGS),
        }

    summary = _CAPTURED[0].payload if _CAPTURED else {}
    discussions = [cap.payload for cap in _CAPTURED[1:]]
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
    with open(out_path, "w", encoding="utf-8", newline="") as f:
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
    parser.add_argument(
        "--report",
        metavar="PATH",
        help="Read the rendered report Summary section as review_body when absent.",
    )
    parser.add_argument("--owner", help="Override the wrapper owner field.")
    parser.add_argument("--repo", help="Override the wrapper repo field.")
    parser.add_argument(
        "--pr-number", type=int, help="Override the wrapper PR/MR number."
    )
    parser.add_argument("--platform", help="Override the wrapper platform field.")
    parser.add_argument("--sha", help="Override the wrapper reviewed commit SHA.")
    args = parser.parse_args()

    # Defense-in-depth: CODE_GAUNTLET_POST_MODE=dry-run self-enforces dry-run so a
    # headless Phase 8 invocation that omits --dry-run cannot live-post. The flag wins
    # when present; env "live" or unset changes nothing without the flag.
    DRY_RUN = args.dry_run or os.environ.get("CODE_GAUNTLET_POST_MODE") == "dry-run"
    reset_run_state()

    try:
        loaded = read_json(args.findings_json)
    except JsonReadError as exc:
        if isinstance(exc.cause, FileNotFoundError):
            die(f"Findings file not found: {args.findings_json}")
        if exc.kind == "parse":
            die(f"Invalid JSON in findings file: {exc.cause}")
        raise exc.cause from exc

    if isinstance(loaded, list):
        data = {}
        for name, value in (
            ("owner", args.owner),
            ("repo", args.repo),
            ("pr_number", args.pr_number),
            ("sha", args.sha),
            ("platform", args.platform),
        ):
            if value is not None:
                data[name] = value
        data["review_body"] = ""
        data["findings"] = loaded
    elif isinstance(loaded, dict):
        data = loaded
    else:
        die("Findings JSON must be an object or an array.")

    for name, value in (
        ("owner", args.owner),
        ("repo", args.repo),
        ("pr_number", args.pr_number),
        ("platform", args.platform),
        ("sha", args.sha),
    ):
        if value is not None:
            data[name] = value

    if args.report and not data.get("review_body"):
        try:
            with open(args.report, encoding="utf-8") as fh:
                data["review_body"] = summary_body_from_report(fh.read())
        except FileNotFoundError:
            die(f"Report file not found: {args.report}")

    for field in ("owner", "repo", "pr_number"):
        if field not in data:
            die(f"Missing required field in findings JSON: '{field}'")

    platform = data.get("platform")
    if platform:
        platform = platform.lower()
    else:
        detection = detect_platform(origin_remote())
        if detection.platform:
            platform = detection.platform
            print(f"Detected platform: {platform} (from git remote: {detection.host})")
        else:
            die(
                "Could not detect platform from git remote. "
                "Set 'platform' field in findings JSON to 'github' or 'gitlab'."
            )

    if platform not in ("github", "gitlab"):
        die(f"Unsupported platform: '{platform}'. Use 'github' or 'gitlab'.")

    forge = make_forge(platform)
    target = ReviewTarget(data["owner"], data["repo"], data["pr_number"])
    valid_lines, new_files, old_paths, line_texts = parse_diff_lines(
        target, forge=forge
    )

    # Deliver. A poster RETURNS its exit status instead of exiting, so a payload defect
    # it found cannot pre-empt the dry-run payload write below — that file is the artifact
    # an operator reads to see what the run would have sent.
    if platform == "github":
        status = post_github(data, valid_lines, line_texts, forge=forge)
    else:
        assert isinstance(forge, GitLab)
        status = post_gitlab(
            data, valid_lines, new_files, old_paths, line_texts, forge=forge
        )

    if DRY_RUN:
        out_path = write_dry_run_payload(platform, args.findings_json)
        print(f"Dry run — no comments posted. Payload written to: {out_path}")

    if status:
        sys.exit(status)


CLI = Command.legacy(main, prog="post_review.py")
