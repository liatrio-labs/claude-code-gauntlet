#!/usr/bin/env python3
"""
verify_findings.py — Deterministic finding verification for deep-review Phase 4.

Usage:
    python3 verify_findings.py <findings_json> [--base-branch main] [--diff-file path]

Input JSON schema:
    {
        "findings": [
            {
                "id": "bug-1",
                "dimension": "bug",
                "severity": "high",
                "confidence": 75,
                "file": "src/foo.py",
                "line_start": 42,
                "line_end": 45,
                "title": "...",
                "description": "...",
                "evidence": "...",
                "suggestion": "...",
                "suggested_fix_code": null,
                "cross_file_refs": []
            }
        ],
        "base_branch": "main",
        "head_sha": "abc123",
        "pr_number": 42,
        "owner": "org",
        "repo": "name"
    }

Output JSON schema:
    {
        "verified": [...],
        "eliminated": [...],
        "batches": [[...], ...],
        "stats": {
            "total": N,
            "new": N,
            "surfaced": N,
            "eliminated": N
        }
    }

    Each finding in "verified" has an added "origin" field:
        "new"       — line was written in the current PR/branch diff
        "surfaced"  — line predates the current diff (pre-existing issue exposed by change)

    Each finding in "eliminated" has an added "elimination_reason" field explaining
    why it was removed (e.g., "line not in diff", "evidence mismatch", etc.).

No external Python dependencies — stdlib only.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Repo root — resolved once at startup (RF-01)
# ---------------------------------------------------------------------------

def _resolve_repo_root():
    """
    Return the absolute path of the repository root.

    Uses ``git rev-parse --show-toplevel`` and falls back to the directory
    that contains this script so the module works even outside a git repo.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    # Fallback: parent directory of this script file
    return os.path.dirname(os.path.abspath(__file__))


REPO_ROOT = _resolve_repo_root()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def run(cmd, check=False, timeout=None, cwd=None):
    """Run a subprocess command. Returns (stdout, stderr, returncode).

    Args:
        cmd: Command as list of strings.
        check: If True, die() on non-zero exit.
        timeout: Seconds before TimeoutExpired. None = no limit.
        cwd: Working directory for the subprocess. None = inherit.

    Returns:
        (stdout, stderr, returncode). On timeout, returns ("", "", -1).
    """
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
    except subprocess.TimeoutExpired:
        return ("", "", -1)
    if check and result.returncode != 0:
        die(
            f"Command failed (exit {result.returncode}): {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result.stdout, result.stderr, result.returncode


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def get_diff(base_branch, diff_file=None):
    """
    Return the unified diff text between base_branch and HEAD.

    Fallback chain:
    1. --diff-file (if provided) — read from file
    2. git diff {base}...HEAD (three-dot merge-base diff)
    3. git diff {base} HEAD (two-dot — noisier but works without a merge base)
    4. Return None (skip diff validation — better than false "surfaced" tagging)

    If diff_file is provided, read from it instead of running git diff.
    Returns the diff string, or None on failure.
    """
    if diff_file:
        try:
            with open(diff_file) as fh:
                content = fh.read()
            print(
                f"Diff source: --diff-file ({diff_file}), {len(content)} bytes",
                file=sys.stderr,
            )
            return content
        except OSError as e:
            warn(f"Could not read diff file '{diff_file}': {e}")
            return None

    # Three-dot diff (merge-base): git diff {base}...HEAD
    stdout, stderr, rc = run(["git", "diff", f"{base_branch}...HEAD"])
    if rc == 0:
        print(
            f"Diff source: git diff {base_branch}...HEAD (three-dot), {len(stdout)} bytes",
            file=sys.stderr,
        )
        return stdout

    warn(
        f"git diff {base_branch}...HEAD failed (exit {rc}): {stderr.strip()}. "
        f"Falling back to git diff {base_branch} HEAD (two-dot)."
    )

    # Two-dot diff: git diff {base} HEAD
    stdout, stderr, rc = run(["git", "diff", base_branch, "HEAD"])
    if rc == 0:
        print(
            f"Diff source: git diff {base_branch} HEAD (two-dot fallback), {len(stdout)} bytes",
            file=sys.stderr,
        )
        return stdout

    warn(
        f"git diff {base_branch} HEAD also failed (exit {rc}): {stderr.strip()}. "
        "Diff validation will be skipped."
    )
    return None


def parse_diff_lines(diff_text):
    """
    Parse a unified diff and return a set of (filepath, line_number) tuples
    representing lines present in the diff (added or context lines).
    Line numbers are from the new (head) version.

    RF-04: Distinguishes two "nothing to parse" cases:
    - ``None``  → diff retrieval failed; callers should skip validation entirely.
    - ``""``    → diff retrieved successfully but is empty (e.g. no changes);
                  callers should treat every finding as "surfaced" (not in diff).
    """
    if diff_text is None:
        return None

    valid_lines = set()
    current_file = None
    new_line = 0

    for raw_line in diff_text.splitlines():
        # New file header: +++ b/path/to/file
        file_match = re.match(r"^\+\+\+ b/(.+)$", raw_line)
        if file_match:
            current_file = file_match.group(1)
            new_line = 0
            continue

        # Hunk header: @@ -old_start[,old_count] +new_start[,new_count] @@
        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
        if hunk_match:
            new_line = int(hunk_match.group(1))
            continue

        if current_file is None:
            continue

        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            valid_lines.add((current_file, new_line))
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            # Removed line — does not advance new_line
            pass
        elif not raw_line.startswith("\\"):
            # Context line
            valid_lines.add((current_file, new_line))
            new_line += 1

    return valid_lines


def is_line_in_diff(valid_lines, filepath, line):
    """Check whether (filepath, line) appears in the parsed diff."""
    if valid_lines is None:
        return True  # diff validation skipped — pass through
    if (filepath, line) in valid_lines:
        return True
    # Strip leading path component variations
    stripped = re.sub(r"^[ab]/", "", filepath)
    return (stripped, line) in valid_lines


# ---------------------------------------------------------------------------
# Stub functions — implemented in follow-on tasks
# ---------------------------------------------------------------------------

def classify_blame(finding, base_branch):
    """
    Classify a finding as "new" or "surfaced" using git blame.

    "new"       — the finding's lines were introduced by the current branch
                  (blame shows a commit reachable from HEAD but not base_branch)
    "surfaced"  — the finding's lines predate the current branch
                  (blame shows a commit also reachable from base_branch)

    Side effects:
    - Sets finding["blame_metadata"] with classification, author, date, and
      original_severity.
    - Downgrades finding["severity"] by one level for "surfaced" findings:
      critical→high, high→medium, medium→low, low stays low.

    Returns: "new" | "surfaced"
    """
    _SEVERITY_DOWNGRADE = {
        "critical": "high",
        "high": "medium",
        "medium": "low",
        "low": "low",
    }

    filepath = finding.get("file", "")
    line_start = finding.get("line_start", 1)
    line_end = finding.get("line_end") or line_start
    original_severity = finding.get("severity", "")
    cross_file_refs = finding.get("cross_file_refs") or []

    # Cross-file impact findings (about code outside the diff) → always "surfaced"
    if cross_file_refs:
        classification = "surfaced"
        finding["blame_metadata"] = {
            "classification": classification,
            "author": None,
            "date": None,
            "original_severity": original_severity,
        }
        if original_severity in _SEVERITY_DOWNGRADE:
            finding["severity"] = _SEVERITY_DOWNGRADE[original_severity]
        return classification

    # File not found on disk → skip (return "new" to keep finding, conservative)
    if not os.path.exists(filepath):
        warn(
            f"classify_blame: file not found '{filepath}' — classifying as 'new' (conservative)."
        )
        finding["blame_metadata"] = {
            "classification": "new",
            "author": None,
            "date": None,
            "original_severity": original_severity,
        }
        return "new"

    # Obtain the set of commits reachable from HEAD but not base_branch (i.e. PR commits)
    pr_stdout, pr_stderr, pr_rc = run(
        ["git", "log", "--format=%H", f"{base_branch}..HEAD"]
    )
    if pr_rc != 0:
        warn(
            f"classify_blame: git log failed for base '{base_branch}': {pr_stderr.strip()}"
            " — classifying as 'new' (conservative)."
        )
        finding["blame_metadata"] = {
            "classification": "new",
            "author": None,
            "date": None,
            "original_severity": original_severity,
        }
        return "new"

    pr_commits = set(pr_stdout.strip().splitlines())

    # Run git blame on the finding's line range
    blame_cmd = ["git", "blame", f"-L{line_start},{line_end}", "--", filepath]
    blame_stdout, blame_stderr, blame_rc = run(blame_cmd)

    if blame_rc != 0:
        err_lower = blame_stderr.lower()
        # Binary files produce a specific error from git blame
        if "binary" in err_lower:
            warn(
                f"classify_blame: binary file '{filepath}' — classifying as 'new' (conservative)."
            )
        else:
            warn(
                f"classify_blame: git blame failed for '{filepath}': {blame_stderr.strip()}"
                " — classifying as 'new' (conservative)."
            )
        finding["blame_metadata"] = {
            "classification": "new",
            "author": None,
            "date": None,
            "original_severity": original_severity,
        }
        return "new"

    # Parse blame output lines.
    # Standard porcelain format (short): "^SHA (Author Date HH:MM:SS +TZ LINE) code"
    # Short format: "SHA (Author YYYY-MM-DD HH:MM:SS +TZ LINE) code"
    blame_sha_re = re.compile(r"^\^?([0-9a-f]{7,40})\s+\((.+?)\s+(\d{4}-\d{2}-\d{2})")

    blamed_shas = set()
    first_author = None
    first_date = None

    for line in blame_stdout.splitlines():
        m = blame_sha_re.match(line)
        if not m:
            continue
        sha_prefix = m.group(1)
        author = m.group(2).strip()
        date = m.group(3)
        blamed_shas.add(sha_prefix)
        if first_author is None:
            first_author = author
            first_date = date

    if not blamed_shas:
        # Could not parse any blame output — conservative
        warn(
            f"classify_blame: could not parse blame output for '{filepath}' lines "
            f"{line_start}-{line_end} — classifying as 'new' (conservative)."
        )
        finding["blame_metadata"] = {
            "classification": "new",
            "author": None,
            "date": None,
            "original_severity": original_severity,
        }
        return "new"

    # A blamed SHA may be a short prefix; check if any blamed commit is a PR commit.
    # PR commits are full SHAs; blamed SHAs may be short (7+ chars).
    # RF-05: removed unreachable branch ``blamed_sha.startswith(full_sha)`` —
    # blamed_sha is always the shorter side, so only check full_sha.startswith(blamed_sha).
    def sha_in_pr(blamed_sha, pr_set):
        for full_sha in pr_set:
            if full_sha.startswith(blamed_sha):
                return True
        return False

    has_pr_commit = any(sha_in_pr(s, pr_commits) for s in blamed_shas)

    # "new" if any blamed commit is in the PR branch; otherwise "surfaced"
    if has_pr_commit:
        classification = "new"
    else:
        classification = "surfaced"

    finding["blame_metadata"] = {
        "classification": classification,
        "author": first_author,
        "date": first_date,
        "original_severity": original_severity,
    }

    # Downgrade severity for surfaced findings
    if classification == "surfaced" and original_severity in _SEVERITY_DOWNGRADE:
        finding["severity"] = _SEVERITY_DOWNGRADE[original_severity]

    return classification


def _extract_symbols(description, evidence):
    """
    V5-05: Tiered symbol extraction from description/evidence text.

    Returns a set of symbol strings to verify against the codebase.

    Tier 1 (definite code): backtick-delimited spans and triple-backtick
        code blocks — extract and verify.
    Tier 2 (very likely code): bare tokens containing code-punctuation
        indicators (_, (), ., ::, ->, [], #) that don't appear in English
        prose — extract and verify.
    Tier 3 (ambiguous, skip): pure CamelCase with no code-punctuation —
        do NOT extract. This eliminates false symbol extraction for English
        words like "Concrete", "Between", "However".
    """
    combined_text = (description or "") + "\n" + (evidence or "")

    # Shared identifier pattern
    _IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

    raw_symbols = set()

    # --- Tier 1: backtick-delimited symbols (definite code) ---

    # Triple-backtick code blocks: extract identifiers from content
    _CODE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
    for block_m in _CODE_BLOCK_RE.finditer(combined_text):
        block_content = block_m.group(1)
        for ident_m in _IDENT_RE.finditer(block_content):
            raw_symbols.add(ident_m.group(0))

    # Single-backtick inline code spans (split on dots for module paths)
    _BACKTICK_SPAN_RE = re.compile(r"`([^`]+)`")
    for span_m in _BACKTICK_SPAN_RE.finditer(combined_text):
        span = span_m.group(1).strip()
        parts = span.strip("._").split(".")
        for part in parts:
            clean = re.sub(r"[^A-Za-z0-9_]", "", part)
            if clean and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", clean):
                raw_symbols.add(clean)

    # --- Tier 2: bare tokens with code-punctuation indicators ---
    # Matches tokens that contain at least one code-punctuation char:
    #   _ (snake_case), (), ., ::, ->, [], #
    # These patterns don't appear in normal English prose.

    _CODE_PUNCTUATION_RE = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*"          # identifier start
        r"(?:[.()\[\]#]|::|->)"                # must contain code punctuation
        r"[A-Za-z0-9_.()#\[\]:>-]*)"           # rest of token
    )
    _SNAKE_CASE_RE = re.compile(
        r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b"  # snake_case: at least one underscore
    )
    _SPLIT_PUNCTUATION_RE = re.compile(r"[.()\[\]#:>-]+")  # Split on code punctuation

    for m in _CODE_PUNCTUATION_RE.finditer(combined_text):
        token = m.group(1)
        # Split on code punctuation to avoid concatenated identifiers
        parts = _SPLIT_PUNCTUATION_RE.split(token)
        for part in parts:
            if part and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", part) and len(part) > 2:
                raw_symbols.add(part)

    for m in _SNAKE_CASE_RE.finditer(combined_text):
        raw_symbols.add(m.group(1))

    # --- Tier 3: pure CamelCase with no code punctuation → SKIP ---
    # We intentionally do NOT extract bare CamelCase words like
    # "Concrete", "Between", "However" — these are ambiguous and
    # cause false-positive symbol misses that kill true positives.

    # Filter out very common English words, Python builtins, and short tokens
    _SKIP_SYMBOLS = {
        "the", "this", "that", "with", "from", "import", "class", "def",
        "for", "not", "and", "its", "but", "are", "was", "were", "can",
        "should", "would", "could", "also", "will", "has", "have", "been",
        "when", "then", "else", "elif", "True", "False", "None", "self",
        "return", "raise", "pass", "break", "continue", "lambda", "yield",
        "async", "await", "print", "isinstance", "len", "str", "int", "list",
        "dict", "set", "tuple", "type", "super", "object", "Exception",
        "ValueError", "TypeError", "KeyError", "AttributeError", "IndexError",
        "RuntimeError", "StopIteration", "OSError", "IOError", "FileNotFoundError",
        "NotImplementedError", "AssertionError", "OverflowError", "ZeroDivisionError",
    }

    return {s for s in raw_symbols if s not in _SKIP_SYMBOLS and len(s) > 2}


def verify_factual(finding):
    """
    Verify that the finding's evidence field matches the actual file content
    at the reported line range, and that referenced symbols exist in the codebase.

    Steps:
    1. Read finding["file"] lines [line_start, line_end] from disk.
    2. Check file exists and lines are within range.
    3. Extract referenced symbol names from description/evidence text using
       tiered extraction (V5-05).
    4. Use grep to confirm referenced symbols exist somewhere in the codebase.
    5. Apply proportional confidence reduction for missing symbols (V5-05):
       - All found → no change
       - Some missing → proportional reduction (floor 30)
       - No extractable symbols → skip symbol verification
    6. Set finding["factual_verification"] = {verified, reason, code_at_lines}.

    Returns: True if plausible (keep in verified, possibly with reduced confidence),
             False if evidence clearly does not match (eliminate finding).

    Cases that return False (eliminate):
    - File does not exist on disk
    - line_start/line_end are out of range for the file

    Cases that reduce confidence but return True (degrade, keep):
    - Referenced symbols extracted from description/evidence do not exist in codebase

    Cases that skip verification entirely (return True, no changes):
    - Finding has no line_start (no line reference to check)
    - File is a binary file
    - No extractable symbols found in description/evidence
    """
    filepath = finding.get("file", "")
    line_start = finding.get("line_start")
    line_end = finding.get("line_end") or line_start
    description = finding.get("description", "") or ""
    evidence = finding.get("evidence", "") or ""

    # No line reference → skip verification, keep as-is
    if not line_start:
        finding["factual_verification"] = {
            "verified": True,
            "reason": "no line reference — verification skipped",
            "code_at_lines": None,
        }
        return True

    # File does not exist → eliminate
    if not filepath or not os.path.exists(filepath):
        finding["confidence"] = 0
        finding["factual_verification"] = {
            "verified": False,
            "reason": f"file not found: {filepath!r}",
            "code_at_lines": None,
        }
        return False

    # Read file content, handling binary files gracefully
    try:
        with open(filepath, "r", encoding="utf-8", errors="strict") as fh:
            all_lines = fh.readlines()
    except UnicodeDecodeError:
        # Binary file → skip verification, keep as-is
        warn(f"verify_factual: binary file '{filepath}' — skipping factual check.")
        finding["factual_verification"] = {
            "verified": True,
            "reason": "binary file — verification skipped",
            "code_at_lines": None,
        }
        return True
    except OSError as e:
        finding["confidence"] = 0
        finding["factual_verification"] = {
            "verified": False,
            "reason": f"could not read file '{filepath}': {e}",
            "code_at_lines": None,
        }
        return False

    total_lines = len(all_lines)

    # line_start/line_end out of range → eliminate
    # Lines are 1-indexed in findings; list is 0-indexed
    if line_start < 1 or line_start > total_lines:
        finding["confidence"] = 0
        finding["factual_verification"] = {
            "verified": False,
            "reason": (
                f"line_start {line_start} out of range "
                f"(file has {total_lines} line(s))"
            ),
            "code_at_lines": None,
        }
        return False

    # Clamp line_end to actual file length
    effective_end = min(line_end, total_lines)

    # Extract relevant lines (convert to 0-indexed slice)
    relevant_lines = all_lines[line_start - 1 : effective_end]
    code_at_lines = "".join(relevant_lines).rstrip("\n")

    # V5-05: Tiered symbol extraction
    symbols_to_check = _extract_symbols(description, evidence)

    # No extractable symbols → skip symbol verification entirely (V5-05)
    if not symbols_to_check:
        finding["factual_verification"] = {
            "verified": True,
            "reason": "no extractable symbols — verification skipped",
            "code_at_lines": code_at_lines,
        }
        return True

    # Grep for each extracted symbol in the repository root.
    # We only grep for symbols not already visible in the relevant lines themselves —
    # if the symbol appears in the code at the reported lines, it's trivially confirmed.
    total_symbols = len(symbols_to_check)
    missing_symbols = []
    for symbol in sorted(symbols_to_check):
        # Fast path: symbol already present in the lines we read
        if symbol in code_at_lines:
            continue

        # Run grep to find the symbol anywhere in the codebase (RF-01: use REPO_ROOT)
        stdout, grep_stderr, rc = run(
            ["grep", "-rn",
             "--exclude-dir=.git", "--exclude-dir=node_modules",
             "--exclude-dir=vendor", "--exclude-dir=__pycache__",
             "--exclude-dir=dist", "--exclude-dir=build",
             "--exclude-dir=.next", "--exclude-dir=target",
             "-l", symbol, REPO_ROOT]
        )
        # RF-03: rc=2 means grep encountered an I/O error (permission denied,
        # binary file read, etc.).  Treat this as an inconclusive result and
        # skip the symbol check rather than falsely zeroing confidence.
        if rc == 2:
            warn(
                f"verify_factual: grep I/O error while searching for symbol "
                f"'{symbol}': {grep_stderr.strip()} — skipping symbol check."
            )
            continue
        if rc != 0 or not stdout.strip():
            # rc=1 means grep ran successfully but found no matches
            # Symbol not found in codebase — note it but don't eliminate
            missing_symbols.append(symbol)

    # V5-05: Proportional confidence reduction
    if missing_symbols:
        original_confidence = finding.get("confidence", 100)
        miss_ratio = len(missing_symbols) / total_symbols
        # Proportional penalty: scale reduction by fraction of symbols missing
        # e.g., 1 of 4 found → miss_ratio=0.75 → reduction of ~52
        # e.g., 3 of 4 found → miss_ratio=0.25 → reduction of ~18
        reduction = int(round(miss_ratio * 70))
        new_confidence = max(30, original_confidence - reduction)
        finding["confidence"] = new_confidence
        finding["factual_verification"] = {
            "verified": False,
            "reason": (
                f"referenced symbol(s) not found in codebase: "
                f"{', '.join(missing_symbols)}"
            ),
            "code_at_lines": code_at_lines,
            "original_confidence": original_confidence,
            "symbols_checked": total_symbols,
            "symbols_missing": len(missing_symbols),
        }
        # Degrade but keep — don't eliminate (return True)
        return True

    finding["factual_verification"] = {
        "verified": True,
        "reason": "file content and symbols verified",
        "code_at_lines": code_at_lines,
    }
    return True


def validate_diff_lines(finding, valid_lines):
    """
    Validate whether the finding's reported line range overlaps with the diff.

    Uses valid_lines set from parse_diff_lines().  Checks each line in
    [line_start, line_end] for presence in the diff.

    Per V4-10: findings entirely outside the diff are NOT eliminated — they
    are tagged as "surfaced" (cross-file context, pre-existing code exposed by
    the change).  This catches the calcom-PR10600 pattern where a finding
    targeted a line far outside the actual diff.

    Side effects:
    - If no diff line in [line_start, line_end] is present in valid_lines,
      sets finding["origin"] = "surfaced" and
      finding["diff_validation"] = {"in_diff": False, "reason": "..."}.
    - If at least one line overlaps, sets
      finding["diff_validation"] = {"in_diff": True, "reason": "..."}.
    - If diff validation is skipped (valid_lines is None), sets
      finding["diff_validation"] = {"in_diff": None, "reason": "skipped"}.

    Returns: always True (findings are kept regardless — origin is updated
             to reflect whether they are "new" or "surfaced").
    """
    if valid_lines is None:
        # Diff validation skipped — leave origin unchanged
        finding["diff_validation"] = {
            "in_diff": None,
            "reason": "diff validation skipped",
        }
        return True

    filepath = finding.get("file", "")
    line_start = finding.get("line_start") or 0
    line_end = finding.get("line_end") or line_start

    # If no line reference at all, treat as in-diff (nothing to validate)
    if not line_start:
        finding["diff_validation"] = {
            "in_diff": True,
            "reason": "no line reference — validation skipped",
        }
        return True

    # Check if any line in the range appears in the diff
    for line in range(line_start, line_end + 1):
        if is_line_in_diff(valid_lines, filepath, line):
            finding["diff_validation"] = {
                "in_diff": True,
                "reason": f"line {line} found in diff",
            }
            return True

    # No lines in range found in diff → tag as "surfaced"
    original_origin = finding.get("origin", "new")
    finding["origin"] = "surfaced"
    finding["diff_validation"] = {
        "in_diff": False,
        "reason": (
            f"lines {line_start}-{line_end} of '{filepath}' not found in diff "
            f"— tagged as surfaced (was: {original_origin})"
        ),
    }
    # Also apply severity downgrade if not already applied (blame may have
    # already downgraded; blame_metadata tracks original_severity)
    _SEVERITY_DOWNGRADE = {
        "critical": "high",
        "high": "medium",
        "medium": "low",
        "low": "low",
    }
    blame_meta = finding.get("blame_metadata") or {}
    # Only downgrade if blame did not already set surfaced (avoid double-downgrade)
    if blame_meta.get("classification") != "surfaced":
        original_severity = finding.get("severity", "")
        if original_severity in _SEVERITY_DOWNGRADE:
            finding["severity"] = _SEVERITY_DOWNGRADE[original_severity]

    return True


def batch_findings(findings, min_batch=3, max_batch=5):
    """
    Group findings into batches of 3-5 for Phase 5 agent dispatch.

    Grouping strategy (file proximity):
    1. Sort findings by file path (then by line_start within file) so that
       findings in the same file or adjacent files end up together.
    2. Fill batches greedily: keep adding findings from the current file until
       the batch would exceed max_batch, then start a new batch.
    3. If a batch would be left with fewer than min_batch items but there are
       enough findings left to form a full batch, merge remainders into the
       previous batch (up to max_batch) rather than leaving orphan singletons.

    Returns: list of lists of finding IDs, e.g.
        [["bug-1", "bug-2", "bug-3"], ["perf-1", "perf-2", ...], ...]

    The output is a list of ID-lists so the orchestrator can reference findings
    by ID without re-embedding full finding objects.
    """
    if not findings:
        return []

    # Sort by file, then line_start for stable file-proximity ordering
    def sort_key(f):
        return (f.get("file") or "", f.get("line_start") or 0)

    sorted_findings = sorted(findings, key=sort_key)

    batches = []
    current_batch = []
    current_file = None

    for idx, f in enumerate(sorted_findings):
        f_file = f.get("file") or ""
        f_id = f.get("id") or f.get("finding_id") or str(idx)

        # Start a new batch when:
        # - current batch has reached max_batch, OR
        # - we've switched to a different file AND current batch already has
        #   at least min_batch items (avoids tiny single-file batches)
        file_changed = (current_file is not None) and (f_file != current_file)
        batch_full = len(current_batch) >= max_batch
        batch_has_min = len(current_batch) >= min_batch

        if batch_full or (file_changed and batch_has_min):
            batches.append(current_batch)
            current_batch = []

        current_batch.append(f_id)
        current_file = f_file

    # Flush remaining items
    if current_batch:
        if batches and len(current_batch) < min_batch:
            # Merge tiny tail into previous batch if it still fits within max_batch
            combined = batches[-1] + current_batch
            if len(combined) <= max_batch:
                batches[-1] = combined
            else:
                # Too large to merge — keep as separate (possibly small) batch
                batches.append(current_batch)
        else:
            batches.append(current_batch)

    return batches


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def load_input(findings_json_path):
    """Load and validate the input JSON file."""
    try:
        with open(findings_json_path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        die(f"Findings file not found: {findings_json_path}")
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in findings file: {e}")

    if not isinstance(data, dict):
        die("Input JSON must be an object with a 'findings' key.")
    if "findings" not in data:
        die("Input JSON is missing required 'findings' array.")
    if not isinstance(data["findings"], list):
        die("'findings' must be an array.")

    return data


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic finding verification for deep-review Phase 4. "
            "Takes Phase 3 agent findings JSON, classifies new vs. surfaced via "
            "git blame, verifies factual accuracy against file content, validates "
            "line references against the diff, and batches results for Phase 5."
        )
    )
    parser.add_argument(
        "findings_json",
        help="Path to input findings JSON (Phase 3 agent outputs merged).",
    )
    parser.add_argument(
        "--base-branch",
        default="main",
        metavar="BRANCH",
        help=(
            "Base branch for blame comparison. "
            "Default: %(default)s. "
            "Override with the PR base branch name (e.g. 'develop')."
        ),
    )
    parser.add_argument(
        "--diff-file",
        default=None,
        metavar="PATH",
        help=(
            "Path to a pre-fetched unified diff file. "
            "If omitted, the script runs 'git diff <base-branch>...HEAD'."
        ),
    )
    args = parser.parse_args()

    # Phase 1: Load
    data = load_input(args.findings_json)
    findings = data["findings"]
    base_branch = data.get("base_branch") or args.base_branch
    total = len(findings)
    print(f"Loaded {total} finding(s) from {args.findings_json}", file=sys.stderr)

    # Phase 2: Classify (blame)
    print(f"Classifying findings against base branch '{base_branch}'...", file=sys.stderr)
    for f in findings:
        f["origin"] = classify_blame(f, base_branch)

    # Phase 3: Verify (factual)
    print("Verifying factual accuracy...", file=sys.stderr)
    verified = []
    eliminated = []
    for f in findings:
        if verify_factual(f):
            verified.append(f)
        else:
            f["elimination_reason"] = "evidence does not match file content"
            eliminated.append(f)

    # Phase 4: Validate diff lines (V4-10)
    # Findings outside the diff are tagged as "surfaced" (not eliminated) so
    # that cross-file context findings are preserved for Phase 5 reporting.
    print("Validating finding line numbers against diff...", file=sys.stderr)
    diff_text = get_diff(base_branch, args.diff_file)
    valid_lines = parse_diff_lines(diff_text)
    if valid_lines is None:
        warn("Diff validation skipped — all findings passed through.")

    diff_surfaced_count = 0
    for f in verified:
        origin_before = f.get("origin", "new")
        validate_diff_lines(f, valid_lines)
        if f.get("origin") == "surfaced" and origin_before != "surfaced":
            diff_surfaced_count += 1

    if diff_surfaced_count:
        print(
            f"  Tagged {diff_surfaced_count} finding(s) as surfaced "
            "(outside diff range).",
            file=sys.stderr,
        )

    # Phase 5: Batch (groups of 3-5 by file proximity)
    print(f"Batching {len(verified)} verified finding(s)...", file=sys.stderr)
    batches = batch_findings(verified)

    # Build stats
    new_count = sum(1 for f in verified if f.get("origin") == "new")
    surfaced_count = sum(1 for f in verified if f.get("origin") == "surfaced")
    stats = {
        "total": total,
        "new": new_count,
        "surfaced": surfaced_count,
        "eliminated": len(eliminated),
    }

    output = {
        "verified": verified,
        "eliminated": eliminated,
        "batches": batches,
        "stats": stats,
    }

    print(json.dumps(output, indent=2, ensure_ascii=False))

    # Summary to stderr
    print(
        f"Done: {len(verified)} verified ({new_count} new, {surfaced_count} surfaced), "
        f"{len(eliminated)} eliminated, {len(batches)} batch(es).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
