"""Resolve quoted source locations without granting authority to numeric hints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from gauntlet import proc
from gauntlet.diff import ContextMapEntry, OldSpan
from gauntlet.registry import ANCHOR_LIMITS, LOCATION_RIGHTS, LocationStatus

TargetKind = Literal["pr", "mr", "branch", "local"]
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
    "plausible_claim",
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


def source_lines(text: str) -> tuple[str, ...]:
    """Only LF and CRLF terminate source lines; other separators are content."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    return tuple(lines)


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
    """Apply claimed, map, unique-file, then unique-hunk precedence to one file."""

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
    # A distant quote alone cannot disprove a plausible source coordinate.
    if c is not None and c <= len(head_lines) and translated is None:
        return unconfirmed("plausible_claim" if starts else "not_found")
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


def _decode_source(content: bytes) -> tuple[str, ...] | None:
    if b"\0" in content:
        return None
    try:
        return source_lines(content.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _local_bytes(root: Path, parts: tuple[str, ...]) -> bytes:
    if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
        # Hold each directory open so a renamed parent cannot redirect the read.
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
                os.close(directory)
                directory = child
            fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError("nonregular source")
            return handle.read()
    current = root
    for part in parts:
        current /= part
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise OSError("source symlink")
    if not stat.S_ISREG(mode):
        raise OSError("nonregular source")
    # O_NOFOLLOW closes the final-component race where the platform supports it.
    fd = os.open(
        current,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("nonregular source")
        return handle.read()


@dataclass(frozen=True, slots=True)
class SourceAdapter:
    root: Path
    target_kind: TargetKind
    captured_head: str | None
    _cache: dict[tuple[TargetKind, str | None, str], tuple[str, ...] | None] = field(
        default_factory=dict, compare=False, repr=False
    )

    @classmethod
    def capture(
        cls, root: Path, target_kind: TargetKind, requested_sha: str | None
    ) -> SourceAdapter:
        head = None
        try:
            result = proc.run_bytes(["git", "rev-parse", "HEAD"], cwd=str(root))
            revision = result.stdout.decode("ascii").strip()
            if (
                result.returncode == 0
                and re.fullmatch(r"[0-9a-f]{40}", revision)
                and (
                    requested_sha is None
                    or (
                        re.fullmatch(r"[0-9a-f]{4,40}", requested_sha)
                        and revision.startswith(requested_sha)
                    )
                )
            ):
                head = revision
        except (OSError, UnicodeError, proc.TimeoutExpired):
            pass
        return cls(root, target_kind, head)

    def read(self, file: str) -> tuple[str, ...] | None:
        key = (self.target_kind, self.captured_head, file)
        if key not in self._cache:
            self._cache[key] = self._read(file)
        return self._cache[key]

    def _read(self, file: str) -> tuple[str, ...] | None:
        parts = tuple(file.split("/"))
        if (
            self.captured_head is None
            or not file
            or "\0" in file
            or any(part in ("", ".", "..") for part in parts)
            or os.path.isabs(file)
            or os.path.splitdrive(file)[0]
            or "\\" in file
        ):
            return None
        try:
            if self.target_kind == "local":
                return _decode_source(_local_bytes(self.root, parts))
            tree = proc.run_bytes(
                ["git", "ls-tree", "-z", self.captured_head, "--", f":(literal){file}"],
                cwd=str(self.root),
            )
            if tree.returncode or not tree.stdout.endswith(b"\0"):
                return None
            metadata, path = tree.stdout[:-1].split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ")
            if (
                mode not in (b"100644", b"100755")
                or kind != b"blob"
                or path != file.encode("utf-8")
            ):
                return None
            blob = proc.run_bytes(
                ["git", "cat-file", "blob", oid.decode("ascii")], cwd=str(self.root)
            )
            return _decode_source(blob.stdout) if blob.returncode == 0 else None
        except (OSError, ValueError, UnicodeError, proc.TimeoutExpired):
            return None
