"""Repository context and deterministic finding decisions."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict, cast

from gauntlet import proc
from gauntlet.cli import warn
from gauntlet.diff import (
    ContextMapEntry,
    DiffFacts,
    DiffStructureError,
    annotate_diff,
    is_line_valid,
    parse_diff,
)
from gauntlet.git_diff import DIFF_FLAGS
from gauntlet.location import (
    SourceAdapter,
    TargetKind,
    canonical_range,
    load_context_map,
    location_allows,
    resolve_location,
)
from gauntlet.paths import entry
from gauntlet.registry import SEVERITY_ORDER, LocationStatus, VerifySliceFinding

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
    origin: Origin
    blame_metadata: BlameMetadata
    factual_verification: FactualVerification
    diff_validation: DiffValidation
    elimination_reason: object
    location_status: LocationStatus
    claimed_line_start: object
    claimed_line_end: object


class Stats(TypedDict):
    total: int
    new: int
    surfaced: int
    eliminated: int


class VerificationResult(TypedDict):
    verified: list[FindingWire]
    eliminated: list[FindingWire]
    stats: Stats


_SEVERITY_DOWNGRADE = dict(
    zip(SEVERITY_ORDER, (*SEVERITY_ORDER[1:], SEVERITY_ORDER[-1]), strict=True)
)


def _downgrade_severity(finding: FindingWire) -> None:
    severity = finding.get("severity", "")
    if severity in _SEVERITY_DOWNGRADE:
        finding["severity"] = _SEVERITY_DOWNGRADE[severity]


# Lazy caching keeps empty and ineligible slices from querying git log.
@dataclass(slots=True)
class VerifyContext:
    repo_root: str
    base_branch: str
    _pr_commits: frozenset[str] | None = None
    commit_error: str | None = None
    diff_read: bool = False
    target_kind: TargetKind = "branch"
    requested_sha: str | None = None
    context_map: str | None = None
    context_file: str | None = None
    source: SourceAdapter | None = None
    map_entries: Mapping[int, ContextMapEntry] = field(default_factory=dict)
    facts: DiffFacts | None = None

    def sources(self) -> SourceAdapter:
        if self.source is None:
            self.source = SourceAdapter.capture(
                Path(self.repo_root), self.target_kind, self.requested_sha
            )
        return self.source

    def commits(self) -> frozenset[str] | None:
        if self._pr_commits is None and self.commit_error is None:
            stdout, stderr, rc = proc.output(
                [
                    "git",
                    "log",
                    "--format=%H",
                    "--end-of-options",
                    f"{self.base_branch}..{self.sources().captured_head or 'HEAD'}",
                ]
            )
            if rc != 0:
                self.commit_error = stderr.strip()
            else:
                self._pr_commits = frozenset(stdout.strip().splitlines())
        return self._pr_commits


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
            with open(
                diff_file, encoding="utf-8", errors="surrogateescape", newline=""
            ) as fh:
                content = fh.read()
            print(
                f"Diff source: --diff-file ({diff_file}), {len(content)} bytes",
                file=sys.stderr,
            )
            return content
        except OSError as e:
            warn(f"Could not read diff file '{diff_file}': {e}")
            return None

    if context.target_kind == "local":
        revision = context.sources().captured_head or "HEAD"
        stdout, stderr, rc = proc.output(
            ["git", "diff", *DIFF_FLAGS, "--end-of-options", revision]
        )
        if rc == 0:
            print(
                f"Diff source: git diff {revision} (local), {len(stdout)} bytes",
                file=sys.stderr,
            )
            return stdout
        warn(f"local git diff failed: {stderr.strip()}; diff validation skipped")
        return None

    stdout, stderr, rc = proc.output(
        ["git", "diff", *DIFF_FLAGS, "--end-of-options", f"{base_branch}...HEAD"]
    )
    if rc == 0:
        print(
            f"Diff source: git diff {base_branch}...HEAD (three-dot), "
            f"{len(stdout)} bytes",
            file=sys.stderr,
        )
        return stdout

    warn(
        f"git diff {base_branch}...HEAD failed (exit {rc}): {stderr.strip()}. "
        f"Falling back to git diff {base_branch} HEAD (two-dot)."
    )

    stdout, stderr, rc = proc.output(
        ["git", "diff", *DIFF_FLAGS, "--end-of-options", base_branch, "HEAD"]
    )
    if rc == 0:
        print(
            f"Diff source: git diff {base_branch} HEAD (two-dot fallback), "
            f"{len(stdout)} bytes",
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
    if finding.get("has_removed_test_location", False):
        return _stamp_blame(finding, "new", finding.get("severity", ""))
    base_branch = context.base_branch

    filepath = finding.get("file", "")
    line_start = finding.get("line_start", 1)
    line_end = finding.get("line_end") or line_start
    original_severity = finding.get("severity", "")
    if finding.get("has_cross_file_refs", False):
        _downgrade_severity(finding)
        return _stamp_blame(finding, "surfaced", original_severity)

    span = canonical_range(finding)
    if span is None:
        return _stamp_blame(finding, "new", original_severity)
    if (
        context.target_kind == "local"
        and context.facts is not None
        and any(
            (filepath, line) in context.facts.valid_lines
            and context.facts.valid_lines[(filepath, line)] is None
            for line in range(span[0], span[1] + 1)
        )
    ):
        return _stamp_blame(finding, "new", original_severity)

    pr_commits = context.commits()
    if pr_commits is None:
        warn(
            f"classify_blame: git log failed for base '{base_branch}': "
            f"{context.commit_error}"
            " — classifying as 'new' (conservative)."
        )
        return _stamp_blame(finding, "new", original_severity)

    blame_cmd = ["git", "blame", f"-L{line_start},{line_end}"]
    if context.target_kind != "local":
        blame_cmd.append(cast(str, context.sources().captured_head))
    blame_cmd.extend(["--", filepath])
    blame_stdout, blame_stderr, blame_rc = proc.output(blame_cmd)

    if blame_rc != 0:
        err_lower = blame_stderr.lower()
        if "binary" in err_lower:
            warn(
                f"classify_blame: binary file '{filepath}' "
                "— classifying as 'new' (conservative)."
            )
        else:
            warn(
                f"classify_blame: git blame failed for '{filepath}': "
                f"{blame_stderr.strip()}"
                " — classifying as 'new' (conservative)."
            )
        return _stamp_blame(finding, "new", original_severity)

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
        warn(
            f"classify_blame: could not parse blame output for '{filepath}' lines "
            f"{line_start}-{line_end} — classifying as 'new' (conservative)."
        )
        return _stamp_blame(finding, "new", original_severity)

    # Git blame abbreviates the full commit hashes returned by git log.
    has_pr_commit = any(
        full_sha.startswith(s) for s in blamed_shas for full_sha in pr_commits
    )

    classification: Origin = "new" if has_pr_commit else "surfaced"

    if classification == "surfaced":
        _downgrade_severity(finding)

    return _stamp_blame(
        finding, classification, original_severity, first_author, first_date
    )


def verify_factual(finding: FindingWire, context: VerifyContext) -> bool:
    has_removed_test_location = finding.get("has_removed_test_location", False)
    filepath = finding.get("file", "")
    span = canonical_range(finding)
    all_lines = context.sources().read(filepath) if span else None
    code_at_lines = None
    if all_lines is not None and span is not None:
        code_at_lines = "\n".join(all_lines[span[0] - 1 : span[1]])

    # Removed test identifiers need not exist at HEAD; the anchor still must.
    symbols_to_check = (
        set()
        if has_removed_test_location
        else set((finding.get("symbols") or "").split())
    )

    if not symbols_to_check:
        finding["factual_verification"] = {
            "verified": True,
            "reason": "no extractable symbols - verification skipped"
            if span
            else "location unconfirmed - positional checks skipped",
            "code_at_lines": code_at_lines,
        }
        return True

    total_symbols = len(symbols_to_check)
    missing_symbols = []
    for symbol in sorted(symbols_to_check):
        if code_at_lines is not None and symbol in code_at_lines:
            continue

        try:
            stdout, grep_stderr, rc = proc.output(
                ["git", "grep", "-l", "-e", symbol], timeout=3, cwd=context.repo_root
            )
        except proc.TimeoutExpired:
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
            missing_symbols.append(symbol)

    if missing_symbols:
        original_confidence = finding.get("confidence", 100)
        miss_ratio = len(missing_symbols) / total_symbols
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
        # Validators can still confirm missing symbols, so retain the finding.
        return True

    finding["factual_verification"] = {
        "verified": True,
        "reason": "file content and symbols verified"
        if span
        else "symbols verified; location unconfirmed - positional checks skipped",
        "code_at_lines": code_at_lines,
    }
    return True


def validate_diff_lines(finding: FindingWire, facts: DiffFacts | None) -> bool:
    """Keep off-diff findings as surfaced context instead of eliminating them."""
    if finding.get("has_removed_test_location", False):
        finding["diff_validation"] = {
            "in_diff": True,
            "reason": "removed test inventory anchors a new coverage loss",
        }
        return True
    if facts is None:
        finding["diff_validation"] = {
            "in_diff": None,
            "reason": "diff validation skipped",
        }
        return True

    filepath = finding.get("file", "")
    line_start = finding.get("line_start") or 0
    line_end = finding.get("line_end") or line_start

    if not location_allows(finding, "sourceLink"):
        finding["diff_validation"] = {
            "in_diff": None,
            "reason": "location unconfirmed - diff validation skipped",
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
        _downgrade_severity(finding)

    return True


def run_verification(
    findings: list[FindingWire], context: VerifyContext, diff_file: str | None = None
) -> VerificationResult:
    """Mutate in phase order, retaining dispatched object identities."""
    diff_text = get_diff(context, diff_file)
    context.diff_read = diff_text is not None
    raw_diff = (diff_text or "").encode("utf-8", "surrogateescape")
    facts = (
        None
        if diff_text is None
        else parse_diff(diff_text, policy="verify-both-spellings")
    )
    context.facts = facts
    try:
        indexed = annotate_diff(raw_diff)
    except DiffStructureError:
        indexed = None
    if context.context_map and context.context_file:
        loaded = load_context_map(
            context.context_map,
            context.context_file,
            raw_diff,
            context.requested_sha or "",
        )
        context.map_entries = loaded.entries
        if loaded.diagnostic:
            warn(loaded.diagnostic)
    elif findings:
        warn("context map unavailable: context/map paths not supplied")
    sources = context.sources()
    for finding in findings:
        file = finding.get("file", "")
        resolution = resolve_location(
            finding,
            sources.read(file),
            context.map_entries,
            indexed.hunks.get(file, ()) if indexed else (),
            indexed.old_spans.get(file, ()) if indexed else (),
        )
        for key, claimed in (
            ("line_start", "claimed_line_start"),
            ("line_end", "claimed_line_end"),
        ):
            if key in finding and claimed not in finding:
                cast(dict[str, object], finding)[claimed] = cast(
                    dict[str, object], finding
                )[key]
            cast(dict[str, object], finding).pop(key, None)
        finding.pop("line", None)
        finding.pop("end_line", None)
        finding["location_status"] = resolution.status
        if resolution.start is not None:
            finding["line_start"], finding["line_end"] = (
                resolution.start,
                resolution.end,
            )
        finding["origin"] = classify_blame(finding, context)
    verified: list[FindingWire] = []
    eliminated: list[FindingWire] = []
    for finding in findings:
        verify_factual(finding, context)
        verified.append(finding)
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
