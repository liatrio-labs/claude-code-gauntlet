"""Resolve quoted source locations without granting authority to numeric hints."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from gauntlet.diff import ContextMapEntry, OldSpan
from gauntlet.registry import (
    ANCHOR_LIMITS,
    DISPOSITION_RIGHTS,
    LOCATION_RIGHTS,
    Disposition,
    LocationStatus,
)
from gauntlet.source_capture import (
    SourceAdapter as SourceAdapter,
)
from gauntlet.source_capture import (
    TargetKind as TargetKind,
)
from gauntlet.source_capture import (
    source_lines as source_lines,
)

ResolutionReason = Literal[
    "claimed",
    "context_map",
    "unique",
    "unique_hunk",
    "invalid_anchor",
    "unreadable_source",
    "deleted_side",
    "ambiguous",
    "not_found",
    "ambiguous_presentation",
]
_SAFE_INTEGER = 2**53 - 1
_READ_LABEL = re.compile(r"^[ \t]*[0-9]+(?:\t|\u2192)")
_GUTTER = re.compile(r"^(?:N[1-9][0-9]* [+ ]\||O[1-9][0-9]* -\|)")


@dataclass(frozen=True, slots=True)
class Resolution:
    start: int | None
    end: int | None
    status: LocationStatus
    reason: ResolutionReason


@dataclass(frozen=True, slots=True)
class Quote:
    lines: tuple[str, ...]
    explicit_old: bool = False


def positive_safe_integer(value: object) -> bool:
    return type(value) is int and 1 <= value <= _SAFE_INTEGER


def presentation_lines(lines: Sequence[str]) -> Quote:
    peeled = []
    explicit_old = False
    for line in lines:
        line = _READ_LABEL.sub("", line, count=1)
        if gutter := _GUTTER.match(line):
            explicit_old |= line.startswith("O")
            line = line[gutter.end() :]
        peeled.append(line)
    return Quote(tuple(peeled), explicit_old)


def anchor_candidates(anchor: object) -> tuple[Quote, ...]:
    if not isinstance(anchor, str):
        return ()
    literal = Quote(source_lines(anchor))
    peeled = presentation_lines(literal.lines)
    return (literal,) if peeled == literal else (literal, peeled)


def valid_quote(quote: Quote) -> bool:
    lines = quote.lines
    return (
        1 <= len(lines) <= ANCHOR_LIMITS["lines"]
        and len("\n".join(lines)) <= ANCHOR_LIMITS["chars"]
        and any(line.strip() for line in lines)
        and (len(lines) > 1 or sum(not ch.isspace() for ch in lines[0]) >= 12)
    )


def _matches(quote: Quote, lines: Sequence[str], start: int) -> bool:
    end = start + len(quote.lines)
    if start < 0 or end > len(lines):
        return False
    normalized = tuple(line.strip() for line in quote.lines)
    if len(normalized) == 1:
        return normalized[0] in lines[start].strip()
    return normalized == tuple(line.strip() for line in lines[start:end])


def _starts(quote: Quote, lines: Sequence[str]) -> set[int]:
    return {
        start + 1
        for start in range(len(lines) - len(quote.lines) + 1)
        if _matches(quote, lines, start)
    }


def resolve_location(
    request: Mapping[str, object],
    head_lines: Sequence[str] | None,
    map_entry: Mapping[int, ContextMapEntry],
    hunks: Sequence[tuple[int, int]],
    old_spans: Sequence[OldSpan],
) -> Resolution:
    """Apply claimed, map, claimed-among-matches, unique-file, then unique-hunk precedence."""

    def unconfirmed(reason: ResolutionReason) -> Resolution:
        return Resolution(None, None, "unconfirmed", reason)

    quotes = anchor_candidates(request.get("anchor"))
    if any(q.explicit_old for q in quotes):
        return unconfirmed("deleted_side")
    candidates = tuple(q for q in quotes if valid_quote(q))
    if not candidates:
        return unconfirmed("invalid_anchor")
    if head_lines is None:
        return unconfirmed("unreadable_source")
    matches = [(q, _starts(q, head_lines)) for q in candidates]
    found = [(q, starts) for q, starts in matches if starts]
    if len(found) == 2 and found[0][1] != found[1][1]:
        return unconfirmed("ambiguous_presentation")
    quote, starts = found[-1] if found else matches[-1]
    claimed = request.get("line_start")
    c = cast(int, claimed) if positive_safe_integer(claimed) else None
    translated = map_entry.get(c) if c is not None else None
    if translated is not None and translated["file"] != request.get("file"):
        translated = None
    if (
        translated is not None
        and translated["side"] == "old"
        and any(
            _matches(quote, span.lines, translated["line"] - span.start)
            for span in old_spans
        )
    ):
        return unconfirmed("deleted_side")
    k = len(quote.lines)

    def resolved(
        start: int, status: LocationStatus, reason: ResolutionReason
    ) -> Resolution:
        return Resolution(start, start + k - 1, status, reason)

    if c in starts and (len(starts) == 1 or translated is None):
        assert c is not None
        return resolved(c, "confirmed", "claimed")
    if (
        translated is not None
        and translated["side"] == "head"
        and translated["line"] in starts
    ):
        return resolved(translated["line"], "relocated", "context_map")
    # The map reading missed every match, so only the source reading of the number holds.
    if c in starts:
        assert c is not None
        return resolved(c, "confirmed", "claimed")
    if len(starts) == 1:
        return resolved(next(iter(starts)), "relocated", "unique")
    in_hunks = {
        start
        for start in starts
        if any(begin <= start and start + k - 1 <= end for begin, end in hunks)
    }
    if len(in_hunks) == 1:
        return resolved(next(iter(in_hunks)), "relocated", "unique_hunk")
    return unconfirmed("ambiguous" if starts else "not_found")


def canonical_range(finding: Mapping[str, object]) -> tuple[int, int] | None:
    if finding.get("location_status") not in ("confirmed", "relocated"):
        return None
    start, end = finding.get("line_start"), finding.get("line_end")
    if not positive_safe_integer(start) or not positive_safe_integer(end):
        return None
    start, end = cast(int, start), cast(int, end)
    return (start, end) if 1 <= end - start + 1 <= ANCHOR_LIMITS["lines"] else None


def location_allows(finding: Mapping[str, object], right: str) -> bool:
    status = finding.get("location_status")
    if not isinstance(status, str) or status not in LOCATION_RIGHTS:
        status = "unconfirmed"
    rights = LOCATION_RIGHTS[cast(LocationStatus, status)]
    return rights.get(right, False) and (
        right == "body" or canonical_range(finding) is not None
    )


def normalize_location(finding: Mapping[str, object]) -> dict[str, object]:
    """Derive delivery aliases only from coherent pipeline-owned coordinates."""
    out = dict(finding)
    span = canonical_range(finding)
    if span is None:
        out["location_status"] = "unconfirmed"
        for key in ("line_start", "line_end", "line", "end_line"):
            out.pop(key, None)
    else:
        out["line"], out["end_line"] = span
    return out


def delivery_allows(finding: Mapping[str, object], right: str) -> bool:
    stamp = finding.get("disposition")
    reasons = finding.get("disposition_reasons", [])

    if (
        not isinstance(stamp, str)
        or stamp not in DISPOSITION_RIGHTS
        or not (isinstance(reasons, list) and all(isinstance(r, str) for r in reasons))
    ):
        stamp = "held_back"
    rights = DISPOSITION_RIGHTS[cast(Disposition, stamp)]
    if right == "report":
        return rights["report"] == "finding"
    if right in ("unverified", "eliminated"):
        return rights["report"] == right
    admission = finding.get("admission")
    if not (
        isinstance(admission, dict)
        and admission.get("version") == 1
        and admission.get("status") == "admitted"
        and isinstance(admission.get("input_proof"), str)
    ):
        return False
    if (
        stamp == "body"
        and right == "fixTask"
        and finding.get("body_fix_task_eligible") is not True
    ):
        return False
    return rights.get(right) is True and location_allows(finding, right)


def anchor_matches_span(
    finding: Mapping[str, object], lines: Sequence[str], *, whole: bool = False
) -> bool:
    """Recheck a held quote at its canonical site without searching or relocating."""
    span = canonical_range(finding)
    if span is None or len(lines) != span[1] - span[0] + 1:
        return False
    quotes = anchor_candidates(finding.get("anchor"))
    if any(quote.explicit_old for quote in quotes):
        return False
    return any(
        valid_quote(quote)
        and len(quote.lines) == len(lines)
        and (
            tuple(line.strip() for line in quote.lines)
            == tuple(line.strip() for line in lines)
            if whole
            else _matches(quote, lines, 0)
        )
        for quote in quotes
    )


@dataclass(frozen=True, slots=True)
class LoadedContextMap:
    entries: Mapping[int, ContextMapEntry]
    diagnostic: str | None = None


def load_context_map(
    map_path: str | Path,
    context_path: str | Path,
    diff: bytes,
    sha: str,
) -> LoadedContextMap:
    try:
        raw = json.loads(Path(map_path).read_bytes())
        context = Path(context_path).read_bytes()
    except (OSError, ValueError, UnicodeError) as exc:
        return LoadedContextMap({}, f"context map unavailable: {exc}")
    if (
        not isinstance(raw, dict)
        or type(raw.get("version")) is not int
        or raw["version"] != 1
        or raw.get("sha") != sha
        or raw.get("context_sha256") != hashlib.sha256(context).hexdigest()
        or raw.get("diff_sha256") != hashlib.sha256(diff).hexdigest()
        or raw.get("status") != "available"
        or not isinstance(raw.get("entries"), dict)
    ):
        return LoadedContextMap(
            {}, "context map unavailable: identity or status mismatch"
        )
    entries: dict[int, ContextMapEntry] = {}
    context_lines = context.count(b"\n")
    for key, entry in raw["entries"].items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[1-9][0-9]*", key)
            or len(key) > 16
            or int(key) > context_lines
            or not isinstance(entry, dict)
            or set(entry) != {"file", "side", "line"}
            or not isinstance(entry["file"], str)
            or not entry["file"]
            or entry["side"] not in ("head", "old")
            or not positive_safe_integer(entry["line"])
        ):
            return LoadedContextMap({}, "context map unavailable: malformed entry")
        entries[int(key)] = cast(ContextMapEntry, entry)
    return LoadedContextMap(entries)
