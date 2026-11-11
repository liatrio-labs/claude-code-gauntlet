"""Resolve quoted source locations without granting authority to numeric hints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from gauntlet import proc
from gauntlet.diff import ContextMapEntry, OldSpan
from gauntlet.registry import (
    ANCHOR_LIMITS,
    DISPOSITION_RIGHTS,
    LOCATION_RIGHTS,
    Admission,
    Disposition,
    LocationStatus,
)

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


def parse_admission(finding: Mapping[str, object]) -> Admission | None:
    """Check the persisted verdict shape before granting delivery rights."""
    stamp = finding.get("admission")
    if not isinstance(stamp, dict):
        return None
    proof = stamp.get("input_proof")
    fields = stamp.get("input_fields")
    reasons = stamp.get("reasons")
    if (
        type(stamp.get("version")) is not int
        or stamp["version"] != 1
        or stamp.get("status") not in ("admitted", "report_only", "held_back")
        or not isinstance(proof, str)
        or not proof.strip()
        or not isinstance(fields, list)
        or not all(isinstance(field, str) for field in fields)
        or not isinstance(reasons, list)
        or any(
            not isinstance(reason, dict)
            or not isinstance(reason.get("predicate"), str)
            or reason.get("status") not in ("report_only", "held_back")
            for reason in reasons
        )
        or not isinstance(stamp.get("kind"), str)
        or type(stamp.get("core")) is not bool
        or stamp.get("tier") not in ("main", "suggestion")
        or type(stamp.get("inline_eligible")) is not bool
    ):
        return None
    return cast(Admission, stamp)


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
    admission = parse_admission(finding)
    if admission is None or admission["status"] != "admitted":
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


def _decode_source(content: bytes) -> tuple[str, ...] | None:
    if b"\0" in content:
        return None
    try:
        return source_lines(content.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _local_mode(root: Path, parts: tuple[str, ...]) -> int:
    current = root
    mode = current.lstat().st_mode
    for part in parts:
        current /= part
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise OSError("source symlink")
    return mode


def _local_open(root: Path, parts: tuple[str, ...]) -> int:
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
        return fd
    if not stat.S_ISREG(_local_mode(root, parts)):
        raise OSError("nonregular source")
    # O_NOFOLLOW closes the final-component race where the platform supports it.
    return os.open(
        root.joinpath(*parts),
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )


def _local_bytes(root: Path, parts: tuple[str, ...]) -> bytes:
    with os.fdopen(_local_open(root, parts), "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("nonregular source")
        return handle.read()


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    file: str
    oid: str
    size: int


@dataclass(frozen=True, slots=True)
class SnapshotInventory:
    files: tuple[SnapshotFile, ...]
    available: bool
    truncated: bool
    excluded: int = 0
    unreadable: int = 0


@dataclass(frozen=True, slots=True)
class SnapshotRead:
    files: Mapping[str, tuple[str, ...]]
    unreadable: int
    deadline_reached: bool


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
            result = proc.run_bytes(
                ["git", "rev-parse", "HEAD"], cwd=str(root), timeout=3
            )
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

    def entries(self, directory: str = "") -> tuple[str, ...] | None:
        """List immediate names from the same source identity used by reads."""
        parts = self._parts(directory) if directory else ()
        if parts is None or (
            self.target_kind != "local" and self.captured_head is None
        ):
            return None
        prefix = directory + "/" if directory else ""
        try:
            if self.target_kind == "local":
                fd = None
                try:
                    if parts and os.scandir in os.supports_fd:
                        fd = _local_open(self.root, parts)
                        source: int | Path = fd
                    else:
                        if parts and not stat.S_ISDIR(_local_mode(self.root, parts)):
                            return None
                        source = self.root.joinpath(*parts)
                    with os.scandir(source) as entries:
                        return tuple(
                            sorted(
                                prefix
                                + entry.name
                                + ("/" if entry.is_dir(follow_symlinks=False) else "")
                                for entry in entries
                                if entry.is_file(follow_symlinks=False)
                                or entry.is_dir(follow_symlinks=False)
                            )
                        )
                finally:
                    if fd is not None:
                        os.close(fd)
            result = proc.run_bytes(
                ["git", "ls-tree", "-z", f"{self.captured_head}:{directory}"],
                cwd=str(self.root),
                timeout=3,
            )
            if result.returncode:
                return None
            names = []
            for entry in result.stdout.split(b"\0"):
                if not entry:
                    continue
                metadata, name = entry.split(b"\t", 1)
                mode, kind, _ = metadata.split(b" ")
                if (mode, kind) in (
                    (b"040000", b"tree"),
                    (b"100644", b"blob"),
                    (b"100755", b"blob"),
                ):
                    names.append(
                        prefix
                        + name.decode("utf-8", "surrogateescape")
                        + ("/" if kind == b"tree" else "")
                    )
            return tuple(sorted(names))
        except (OSError, ValueError, UnicodeError, proc.TimeoutExpired):
            return None

    def tracked_files(
        self,
        include: Callable[[str], bool],
        *,
        deadline: float,
        max_listing_bytes: int,
        priority_directories: Sequence[str] = (),
    ) -> SnapshotInventory:
        """Use the captured tree, or the local index, as the membership authority."""
        if time.monotonic() >= deadline:
            return SnapshotInventory((), False, True)
        if self.target_kind != "local" and self.captured_head is None:
            return SnapshotInventory((), False, False)
        command = (
            ["git", "ls-files", "--stage", "-z"]
            if self.target_kind == "local"
            else [
                "git",
                "ls-tree",
                "-r",
                "-l",
                "-z",
                "--full-tree",
                cast(str, self.captured_head),
            ]
        )
        try:
            directories = [
                d for d in priority_directories if self._parts(d) is not None
            ]
            commands = []
            if directories:
                commands.append(
                    [*command, "--", *(f":(literal){d}" for d in directories)]
                )
            commands.append(command)
            remaining = max_listing_bytes
            records: list[bytes] = []
            limited = False
            available = False
            for index, query in enumerate(commands):
                if remaining <= 0:
                    limited = True
                    break
                cap = max(1, remaining // 2) if index < len(commands) - 1 else remaining
                result = proc.run_bounded_bytes(
                    query,
                    cwd=str(self.root),
                    timeout=max(0, deadline - time.monotonic()),
                    max_output=cap,
                )
                remaining -= len(result.stdout)
                if result.returncode and not result.truncated:
                    limited = True
                    continue
                available = True
                limited |= result.truncated
                records.extend(result.stdout.split(b"\0")[:-1])
            if not available:
                return SnapshotInventory((), False, limited)
            files = []
            excluded = 0
            unreadable = 0
            seen: set[str] = set()
            for record in records:
                if time.monotonic() >= deadline:
                    limited = True
                    break
                metadata, raw_path = record.split(b"\t", 1)
                path = raw_path.decode("utf-8", "surrogateescape")
                if path in seen:
                    continue
                seen.add(path)
                parts = self._parts(path)
                values = metadata.split()
                if self.target_kind == "local":
                    mode, oid, stage = values
                    if stage != b"0":
                        unreadable += 1
                        continue
                else:
                    mode, kind, oid, raw_size = values
                    if kind != b"blob":
                        excluded += 1
                        continue
                if (
                    mode not in (b"100644", b"100755")
                    or parts is None
                    or not include(path)
                ):
                    excluded += 1
                    continue
                if re.fullmatch(rb"[0-9a-f]{40,64}", oid) is None:
                    continue
                if self.target_kind == "local":
                    try:
                        if not stat.S_ISREG(_local_mode(self.root, parts)):
                            unreadable += 1
                            continue
                        size = (
                            self.root.joinpath(*parts)
                            .stat(follow_symlinks=False)
                            .st_size
                        )
                    except OSError:
                        unreadable += 1
                        continue
                else:
                    size = int(raw_size)
                files.append(SnapshotFile(path, oid.decode("ascii"), size))
            return SnapshotInventory(tuple(files), True, limited, excluded, unreadable)
        except (OSError, ValueError, UnicodeError, proc.TimeoutExpired):
            return SnapshotInventory((), False, time.monotonic() >= deadline)

    def unignored_files(
        self, inventory: SnapshotInventory, *, deadline: float, max_listing_bytes: int
    ) -> SnapshotInventory:
        """Intersect snapshot membership with Git's current and captured ignore rules."""
        if not inventory.available:
            return SnapshotInventory(
                (), False, inventory.truncated, inventory.excluded, inventory.unreadable
            )
        files = inventory.files
        if not files:
            return inventory

        def ignored(worktree: str | None = None) -> set[str]:
            command = [
                "git",
                *(["--work-tree=" + worktree] if worktree else []),
                "check-ignore",
                "--no-index",
                "-z",
                "--stdin",
            ]
            result = proc.run_bounded_bytes(
                command,
                cwd=str(self.root),
                timeout=max(0, deadline - time.monotonic()),
                max_output=max_listing_bytes,
                input=b"\0".join(
                    f.file.encode("utf-8", "surrogateescape") for f in files
                )
                + b"\0",
            )
            if result.returncode not in (0, 1) or result.truncated:
                raise ValueError("ignore check unavailable")
            return {
                p.decode("utf-8", "surrogateescape")
                for p in result.stdout.split(b"\0")
                if p
            }

        try:
            excluded = ignored()
            if self.target_kind != "local":
                rules = [f for f in files if f.file.rsplit("/", 1)[-1] == ".gitignore"]
                if inventory.truncated:
                    ancestors = {".gitignore"}
                    for item in files:
                        parts = item.file.split("/")[:-1]
                        ancestors.update(
                            "/".join([*parts[:n], ".gitignore"])
                            for n in range(1, len(parts) + 1)
                        )
                    records = []
                    remaining = max_listing_bytes
                    pending: list[str] = []
                    cost = 0
                    queries: list[list[str]] = []
                    for name in sorted(ancestors):
                        pathspec = ":(literal)" + name
                        size = len(pathspec.encode("utf-8", "surrogateescape")) + 1
                        if pending and cost + size > 8192:
                            queries.append(pending)
                            pending, cost = [], 0
                        pending.append(pathspec)
                        cost += size
                    if pending:
                        queries.append(pending)
                    for query in queries:
                        result = proc.run_bounded_bytes(
                            [
                                "git",
                                "ls-tree",
                                "-l",
                                "-z",
                                "--full-tree",
                                cast(str, self.captured_head),
                                "--",
                                *query,
                            ],
                            cwd=str(self.root),
                            timeout=max(0, deadline - time.monotonic()),
                            max_output=remaining,
                        )
                        remaining -= len(result.stdout)
                        if result.returncode or result.truncated:
                            raise ValueError("ignore listing unavailable")
                        records.extend(result.stdout.split(b"\0")[:-1])
                    rules = []
                    for record in records:
                        metadata, raw_path = record.split(b"\t", 1)
                        mode, kind, oid, raw_size = metadata.split()
                        if mode in (b"100644", b"100755") and kind == b"blob":
                            rules.append(
                                SnapshotFile(
                                    raw_path.decode("utf-8", "surrogateescape"),
                                    oid.decode("ascii"),
                                    int(raw_size),
                                )
                            )

                if sum(f.size for f in rules) > 256 * 1024:
                    raise ValueError("ignore rules exceed budget")
                read = self.read_snapshot(rules, deadline=deadline)
                if (
                    read.unreadable
                    or read.deadline_reached
                    or len(read.files) != len(rules)
                ):
                    raise ValueError("ignore snapshot unavailable")
                with tempfile.TemporaryDirectory(
                    prefix="gauntlet-ignore-"
                ) as directory:
                    for file, lines in read.files.items():
                        if any("\ufffd" in line for line in lines):
                            raise ValueError("unrepresentable ignore rule")
                        path = Path(directory).joinpath(*file.split("/"))
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(
                            "\n".join(lines) + "\n", encoding="utf-8", newline=""
                        )
                    excluded |= ignored(directory)
            return SnapshotInventory(
                tuple(f for f in files if f.file not in excluded),
                True,
                inventory.truncated,
                inventory.excluded + len(excluded),
                inventory.unreadable,
            )
        except (OSError, ValueError, proc.TimeoutExpired):
            return SnapshotInventory(
                (),
                False,
                time.monotonic() >= deadline,
                inventory.excluded,
                inventory.unreadable,
            )

    def read_snapshot(
        self, files: Sequence[SnapshotFile], *, deadline: float
    ) -> SnapshotRead:
        """Read only pre-budgeted tracked files, with one Git batch for a pinned tree."""
        loaded: dict[str, tuple[str, ...]] = {}
        if not files:
            return SnapshotRead(loaded, 0, time.monotonic() >= deadline)
        try:
            if self.target_kind == "local":
                for item in files:
                    if time.monotonic() >= deadline:
                        break
                    parts = self._parts(item.file)
                    if parts is None:
                        continue
                    try:
                        with os.fdopen(_local_open(self.root, parts), "rb") as handle:
                            before = os.fstat(handle.fileno())
                            if (
                                not stat.S_ISREG(before.st_mode)
                                or before.st_size != item.size
                            ):
                                continue
                            content = handle.read(item.size)
                            after = os.fstat(handle.fileno())
                            if (
                                after.st_size != item.size
                                or after.st_mtime_ns != before.st_mtime_ns
                            ):
                                continue
                        decoded = _decode_source(content)
                        if decoded is not None:
                            loaded[item.file] = decoded
                    except OSError:
                        continue
            else:
                request = b"".join(item.oid.encode("ascii") + b"\n" for item in files)
                result = proc.run_bounded_bytes(
                    ["git", "cat-file", "--batch"],
                    cwd=str(self.root),
                    timeout=max(0, deadline - time.monotonic()),
                    input=request,
                    max_output=sum(item.size + 100 for item in files),
                )
                if result.returncode or result.truncated:
                    return SnapshotRead({}, len(files), time.monotonic() >= deadline)
                offset = 0
                for item in files:
                    end = result.stdout.index(b"\n", offset)
                    header = result.stdout[offset:end].split()
                    offset = end + 1
                    if header == [item.oid.encode("ascii"), b"missing"]:
                        continue
                    # Consume the declared payload even when this object's identity is wrong,
                    # so a bad entry cannot shift subsequent files onto another blob's bytes.
                    if len(header) != 3 or not header[2].isdigit():
                        break
                    size = int(header[2])
                    content = result.stdout[offset : offset + size]
                    if (
                        len(content) != size
                        or result.stdout[offset + size : offset + size + 1] != b"\n"
                    ):
                        break
                    offset += size + 1
                    if header != [
                        item.oid.encode("ascii"),
                        b"blob",
                        str(item.size).encode("ascii"),
                    ]:
                        continue
                    decoded = _decode_source(content)
                    if decoded is not None:
                        loaded[item.file] = decoded
            return SnapshotRead(
                loaded, len(files) - len(loaded), time.monotonic() >= deadline
            )
        except (OSError, ValueError, UnicodeError, proc.TimeoutExpired):
            return SnapshotRead(
                loaded, len(files) - len(loaded), time.monotonic() >= deadline
            )

    def valid_path(self, file: str) -> bool:
        return self._parts(file) is not None

    def _parts(self, file: str) -> tuple[str, ...] | None:
        parts = tuple(file.split("/"))
        if (
            (self.target_kind != "local" and self.captured_head is None)
            or not file
            or "\0" in file
            or any(part in ("", ".", "..") for part in parts)
            or os.path.isabs(file)
            or os.path.splitdrive(file)[0]
            or "\\" in file
        ):
            return None
        return parts

    def path_kind(self, file: str) -> Literal["file", "directory", "unknown"]:
        if self.read(file) is not None:
            return "file"
        parts = self._parts(file)
        if parts is None:
            return "unknown"
        try:
            if self.target_kind == "local":
                # Some platforms cannot open directories; the checked lstat walk suffices.
                if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
                    return (
                        "directory"
                        if stat.S_ISDIR(_local_mode(self.root, parts))
                        else "unknown"
                    )
                fd = _local_open(self.root, parts)
                try:
                    return (
                        "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "unknown"
                    )
                finally:
                    os.close(fd)
            tree = proc.run_bytes(
                [
                    "git",
                    "ls-tree",
                    "-z",
                    cast(str, self.captured_head),
                    "--",
                    f":(literal){file}",
                ],
                cwd=str(self.root),
                timeout=3,
            )
            return (
                "directory"
                if tree.returncode == 0
                and tree.stdout.startswith(b"040000 tree ")
                and tree.stdout.endswith(b"\t" + file.encode("utf-8") + b"\0")
                else "unknown"
            )
        except (OSError, ValueError, UnicodeError, proc.TimeoutExpired):
            return "unknown"

    def _read(self, file: str) -> tuple[str, ...] | None:
        parts = self._parts(file)
        if parts is None:
            return None
        try:
            if self.target_kind == "local":
                return _decode_source(_local_bytes(self.root, parts))
            if self.captured_head is None:
                return None
            tree = proc.run_bytes(
                ["git", "ls-tree", "-z", self.captured_head, "--", f":(literal){file}"],
                cwd=str(self.root),
                timeout=3,
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
                ["git", "cat-file", "blob", oid.decode("ascii")],
                cwd=str(self.root),
                timeout=3,
            )
            return _decode_source(blob.stdout) if blob.returncode == 0 else None
        except (OSError, ValueError, UnicodeError, proc.TimeoutExpired):
            return None
