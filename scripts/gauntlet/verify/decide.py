"""Repository context and deterministic finding decisions."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, cast

from gauntlet import proc
from gauntlet.cli import warn
from gauntlet.diff import DiffFacts, is_line_valid, parse_diff
from gauntlet.paths import entry
from gauntlet.registry import VerifySliceFinding

Origin = Literal["new", "surfaced"]


class BlameMetadata(TypedDict):
    classification: Origin
    author: str | None
    date: str | None
    original_severity: object


class _FactualRequired(TypedDict):
    verified: bool
    reason: str
    code_at_lines: str | None


class FactualVerification(_FactualRequired, total=False):
    original_confidence: object
    symbols_checked: int
    symbols_missing: int


class DiffValidation(TypedDict):
    in_diff: bool | None
    reason: str


class FindingWire(VerifySliceFinding, total=False):
    line: object
    end_line: object
    blame_metadata: BlameMetadata
    factual_verification: FactualVerification
    diff_validation: DiffValidation
    elimination_reason: object


class Stats(TypedDict):
    total: int
    new: int
    surfaced: int
    eliminated: int


class VerificationResult(TypedDict):
    verified: list[FindingWire]
    eliminated: list[FindingWire]
    stats: Stats


_SEVERITY_DOWNGRADE = {
    "critical": "high",
    "high": "medium",
    "medium": "low",
    "low": "low",
}


# The per-invocation lazy cache must be mutable so empty and ineligible slices query no log.
@dataclass(slots=True)
class VerifyContext:
    repo_root: str
    base_branch: object
    pr_commits: frozenset[str] | None = None
    commit_error: str | None = None


def resolve_repo_root() -> str:
    """Probe the process cwd, falling back to the entry's scripts directory."""
    stdout, _stderr, rc = proc.output(["git", "rev-parse", "--show-toplevel"])
    if rc == 0 and stdout.strip():
        return stdout.strip()
    return os.path.dirname(entry("verify_findings"))


def resolve_head_sha() -> str | None:
    stdout, _stderr, rc = proc.output(["git", "rev-parse", "--short", "HEAD"])
    return stdout.strip() if rc == 0 and stdout.strip() else None


def get_diff(context: VerifyContext, diff_file: str | None = None) -> str | None:
    """Return a supplied diff, then try merge-base and two-dot diffs."""
    base_branch = context.base_branch
    if diff_file:
        try:
            with open(diff_file, encoding="utf-8") as fh:
                content = fh.read()
            print(
                f"Diff source: --diff-file ({diff_file}), {len(content)} bytes",
                file=sys.stderr,
            )
            return content
        except OSError as e:
            warn(f"Could not read diff file '{diff_file}': {e}")
            return None

    stdout, stderr, rc = proc.output(
        ["git", "diff", "--end-of-options", f"{base_branch}...HEAD"]
    )
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

    stdout, stderr, rc = proc.output(
        ["git", "diff", "--end-of-options", cast(str, base_branch), "HEAD"]
    )
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
    # With no diff, skip validation rather than tag every finding as surfaced.
    return None


def _stamp_blame(
    finding: FindingWire,
    classification: Origin,
    original_severity: object,
    author: str | None = None,
    date: str | None = None,
) -> Origin:
    finding["blame_metadata"] = {
        "classification": classification,
        "author": author,
        "date": date,
        "original_severity": original_severity,
    }
    return classification


def classify_blame(finding: FindingWire, context: VerifyContext) -> Origin:
    """Classify blame and downgrade surfaced severity in place."""
    base_branch = context.base_branch

    filepath = finding.get("file", "")
    line_start = finding.get("line_start", 1)
    line_end = finding.get("line_end") or line_start
    original_severity = finding.get("severity", "")
    cross_file_refs = finding.get("cross_file_refs") or []

    if cross_file_refs:
        classification: Origin = "surfaced"
        if original_severity in _SEVERITY_DOWNGRADE:
            finding["severity"] = _SEVERITY_DOWNGRADE[original_severity]
        return _stamp_blame(finding, classification, original_severity)

    # File not found on disk → skip (return "new" to keep finding, conservative)
    if not os.path.exists(filepath):
        warn(
            f"classify_blame: file not found '{filepath}' — classifying as 'new' (conservative)."
        )
        return _stamp_blame(finding, "new", original_severity)

    if context.pr_commits is None and context.commit_error is None:
        pr_stdout, pr_stderr, pr_rc = proc.output(
            ["git", "log", "--format=%H", "--end-of-options", f"{base_branch}..HEAD"]
        )
        if pr_rc != 0:
            context.commit_error = pr_stderr.strip()
        else:
            context.pr_commits = frozenset(pr_stdout.strip().splitlines())
    if context.commit_error is not None:
        warn(
            f"classify_blame: git log failed for base '{base_branch}': {context.commit_error}"
            " — classifying as 'new' (conservative)."
        )
        return _stamp_blame(finding, "new", original_severity)

    pr_commits = context.pr_commits
    assert pr_commits is not None

    blame_cmd = ["git", "blame", f"-L{line_start},{line_end}", "--", filepath]
    blame_stdout, blame_stderr, blame_rc = proc.output(blame_cmd)

    if blame_rc != 0:
        err_lower = blame_stderr.lower()
        if "binary" in err_lower:
            warn(
                f"classify_blame: binary file '{filepath}' — classifying as 'new' (conservative)."
            )
        else:
            warn(
                f"classify_blame: git blame failed for '{filepath}': {blame_stderr.strip()}"
                " — classifying as 'new' (conservative)."
            )
        return _stamp_blame(finding, "new", original_severity)

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
        return _stamp_blame(finding, "new", original_severity)

    # A blamed SHA may be a short prefix; check if any blamed commit is a PR commit.
    # PR commits are full SHAs; blamed SHAs may be short (7+ chars).
    has_pr_commit = any(
        full_sha.startswith(s) for s in blamed_shas for full_sha in pr_commits
    )

    classification = "new" if has_pr_commit else "surfaced"

    if classification == "surfaced" and original_severity in _SEVERITY_DOWNGRADE:
        finding["severity"] = _SEVERITY_DOWNGRADE[original_severity]

    return _stamp_blame(
        finding, classification, original_severity, first_author, first_date
    )


def _extract_symbols(description: str, evidence: str) -> set[str]:
    """Extract definite code tokens, excluding ambiguous bare CamelCase prose."""
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
        r"\b([A-Za-z_][A-Za-z0-9_]*"  # identifier start
        r"(?:[.()\[\]#]|::|->)"  # must contain code punctuation
        r"[A-Za-z0-9_.()#\[\]:>-]*)"  # rest of token
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
    stop_words = (
        "the this that with from import class def for not and its but are was "
        "were can should would could also will has have been when then else elif "
        "True False None self return raise pass break continue lambda yield async "
        "await print isinstance len str int list dict set tuple type super object "
        "Exception ValueError TypeError KeyError AttributeError IndexError "
        "RuntimeError StopIteration OSError IOError FileNotFoundError "
        "NotImplementedError AssertionError OverflowError ZeroDivisionError"
    )
    _SKIP_SYMBOLS = frozenset(stop_words.split())

    return {s for s in raw_symbols if s not in _SKIP_SYMBOLS and len(s) > 2}


def verify_factual(finding: FindingWire, context: VerifyContext) -> bool:
    """Check file ranges and symbols, degrading plausible findings conservatively."""
    filepath = finding.get("file", "")
    line_start = finding.get("line_start")
    line_end: Any = finding.get("line_end") or line_start
    description = finding.get("description", "") or ""
    evidence = finding.get("evidence", "") or ""

    if not line_start:
        finding["factual_verification"] = {
            "verified": True,
            "reason": "no line reference — verification skipped",
            "code_at_lines": None,
        }
        return True

    if not filepath or not os.path.exists(filepath):
        finding["confidence"] = 0
        finding["factual_verification"] = {
            "verified": False,
            "reason": f"file not found: {filepath!r}",
            "code_at_lines": None,
        }
        return False

    try:
        with open(filepath, encoding="utf-8", errors="strict") as fh:
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

    # Lines are 1-indexed in findings; list is 0-indexed
    if line_start < 1 or line_start > total_lines:
        finding["confidence"] = 0
        finding["factual_verification"] = {
            "verified": False,
            "reason": (
                f"line_start {line_start} out of range (file has {total_lines} line(s))"
            ),
            "code_at_lines": None,
        }
        return False

    effective_end = min(line_end, total_lines)

    relevant_lines = all_lines[line_start - 1 : effective_end]
    code_at_lines = "".join(relevant_lines).rstrip("\n")

    symbols_to_check = _extract_symbols(description, evidence)

    if not symbols_to_check:
        finding["factual_verification"] = {
            "verified": True,
            "reason": "no extractable symbols — verification skipped",
            "code_at_lines": code_at_lines,
        }
        return True

    # We only grep for symbols not already visible in the relevant lines themselves —
    # if the symbol appears in the code at the reported lines, it's trivially confirmed.
    total_symbols = len(symbols_to_check)
    missing_symbols = []
    for symbol in sorted(symbols_to_check):
        if symbol in code_at_lines:
            continue

        try:
            stdout, grep_stderr, rc = proc.output(
                ["git", "grep", "-l", "-e", symbol], timeout=3, cwd=context.repo_root
            )
        except proc.TimeoutExpired:
            stdout, grep_stderr, rc = "", "", -1
        # rc=-1: timeout — skip symbol, Phase 5 validators will verify
        if rc == -1:
            warn(
                f"verify_factual: symbol search timed out for "
                f"'{symbol}' — skipping (Phase 5 will validate)."
            )
            continue
        # rc=2: grep I/O error; rc>=128: fatal git error — skip symbol
        if rc not in (0, 1):
            warn(
                f"verify_factual: git grep error (rc={rc}) for symbol "
                f"'{symbol}': {grep_stderr.strip()} — skipping."
            )
            continue
        if rc != 0 or not stdout.strip():
            # rc=1 means git grep ran successfully but found no matches
            missing_symbols.append(symbol)

    if missing_symbols:
        original_confidence = finding.get("confidence", 100)
        miss_ratio = len(missing_symbols) / total_symbols
        # Proportional penalty: scale reduction by fraction of symbols missing
        # e.g., 1 of 4 found → miss_ratio=0.75 → reduction of ~52
        # e.g., 3 of 4 found → miss_ratio=0.25 → reduction of ~18
        reduction = round(miss_ratio * 70)
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


def validate_diff_lines(finding: FindingWire, facts: DiffFacts | None) -> bool:
    """Keep off-diff findings as surfaced context instead of eliminating them."""
    if facts is None:
        finding["diff_validation"] = {
            "in_diff": None,
            "reason": "diff validation skipped",
        }
        return True

    filepath = finding.get("file", "")
    line_start = finding.get("line_start") or 0
    line_end = finding.get("line_end") or line_start

    if not line_start:
        finding["diff_validation"] = {
            "in_diff": True,
            "reason": "no line reference — validation skipped",
        }
        return True

    for line in range(line_start, line_end + 1):
        if is_line_valid(facts, filepath, line):
            finding["diff_validation"] = {
                "in_diff": True,
                "reason": f"line {line} found in diff",
            }
            return True

    original_origin = finding.get("origin", "new")
    finding["origin"] = "surfaced"
    finding["diff_validation"] = {
        "in_diff": False,
        "reason": (
            f"lines {line_start}-{line_end} of '{filepath}' not found in diff "
            f"— tagged as surfaced (was: {original_origin})"
        ),
    }
    blame_meta = finding.get("blame_metadata")
    # Blame downgrades when it tags surfaced; a second downgrade would drop two levels.
    if not blame_meta or blame_meta.get("classification") != "surfaced":
        original_severity = finding.get("severity", "")
        if original_severity in _SEVERITY_DOWNGRADE:
            finding["severity"] = _SEVERITY_DOWNGRADE[original_severity]

    return True


def run_verification(
    findings: list[FindingWire], context: VerifyContext, diff_file: str | None = None
) -> VerificationResult:
    """Mutate in blame, factual, then diff order, retaining dispatched object identities."""
    for finding in findings:
        finding["origin"] = classify_blame(finding, context)
    verified: list[FindingWire] = []
    eliminated: list[FindingWire] = []
    for finding in findings:
        if verify_factual(finding, context):
            verified.append(finding)
        else:
            finding["elimination_reason"] = "evidence does not match file content"
            eliminated.append(finding)
    diff_text = get_diff(context, diff_file)
    facts = (
        None
        if diff_text is None
        else parse_diff(diff_text, policy="verify-both-spellings")
    )
    for finding in verified:
        validate_diff_lines(finding, facts)
    stats: Stats = {
        "total": len(findings),
        "new": sum(1 for finding in verified if finding.get("origin") == "new"),
        "surfaced": sum(
            1 for finding in verified if finding.get("origin") == "surfaced"
        ),
        "eliminated": len(eliminated),
    }
    return {"verified": verified, "eliminated": eliminated, "stats": stats}
