"""Persist repository evidence independently of later worktree and HEAD changes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict, cast

from gauntlet import proc
from gauntlet.diff import OldSpan, annotate_diff, parse_diff, patch_report_policy
from gauntlet.paths import NON_SOURCE_DIRS

TargetKind = Literal["pr", "mr", "branch", "local"]

# Bound transient memory and run storage even in repositories with large local data.
MAX_COPY_FILE_BYTES = 2 * 1024 * 1024
MAX_COPY_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FAILURE_SAMPLE = 10
# Whole-repository enumeration can be slow on large worktrees and network filesystems.
LIST_TIMEOUT_SECONDS = 180


def source_lines(text: str) -> tuple[str, ...]:
    """Only LF and CRLF terminate source lines; other separators are content."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    return tuple(lines)


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
    if stat.S_ISLNK(mode):
        raise OSError("source symlink")
    for part in parts:
        current /= part
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise OSError("source symlink")
    return mode


def _local_open(root: Path, parts: tuple[str, ...]) -> int:
    if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
        # Hold each directory open so a renamed parent cannot redirect the read.
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
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


def _local_bytes(
    root: Path, parts: tuple[str, ...], *, copy_budget: int | None = None
) -> bytes:
    with os.fdopen(_local_open(root, parts), "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("nonregular source")
        if copy_budget is None:
            return handle.read()
        limit = min(MAX_COPY_FILE_BYTES, copy_budget)
        if info.st_size > limit:
            raise ValueError("source copy size limit")
        # Probe for NULs before allocating the full file; cap reads if it grows later.
        probe = handle.read(min(8192, limit + 1))
        if b"\0" in probe:
            raise ValueError("non-text source")
        content = probe + handle.read(limit + 1 - len(probe))
        if len(content) > limit:
            raise ValueError("source copy size limit")
        return content


@dataclass(frozen=True, slots=True)
class SourceAdapter:
    root: Path
    target_kind: TargetKind
    captured_head: str | None
    source_capture: SourceCapture | None = field(default=None, repr=False)
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
        if self.source_capture is not None:
            return self.read_captured_source(file)
        key = (self.target_kind, self.captured_head, file)
        if key not in self._cache:
            self._cache[key] = self._read(file)
        return self._cache[key]

    def capture_sources(
        self, output: Path, nonce: str, diff: bytes, base: str | None = None
    ) -> SourceCapture:
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", nonce):
            raise ValueError("invalid source capture nonce")
        parts = (f"code-gauntlet-inputs-{nonce}", "source-capture")
        directory = output.joinpath(*parts)
        if directory.exists() or (directory.parent / "source-capture-started").exists():
            if not (directory / "manifest.json").exists():
                raise ValueError(
                    "source capture interrupted; start over with a fresh nonce"
                )
            loaded = load_capture(self, output, {"nonce": nonce}, diff)
            if (
                base is not None
                and _git(self, "merge-base", loaded.manifest["head"], base)
                .decode("ascii")
                .strip()
                != loaded.manifest["base"]
            ):
                raise ValueError("source capture base mismatch")
            return loaded
        if self.captured_head is None:
            raise ValueError("source capture head unavailable")
        base = base or self.captured_head
        if not re.fullmatch(r"[0-9a-f]{40}", base):
            raise ValueError("source capture requires a full base commit")
        base = (
            _git(self, "merge-base", self.captured_head, base).decode("ascii").strip()
        )
        dirty = b""
        if self.target_kind == "local":
            dirty = _git(
                self,
                "diff",
                "--name-only",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "-z",
                self.captured_head,
                "--",
                timeout=LIST_TIMEOUT_SECONDS,
            )
            dirty += _git(
                self,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                timeout=LIST_TIMEOUT_SECONDS,
            )
        output = output.resolve()
        excluded = (
            output.relative_to(self.root.resolve()).as_posix()
            if output.is_relative_to(self.root.resolve())
            else None
        )
        m: CaptureManifest = {
            "version": 1,
            "nonce": nonce,
            "target_kind": self.target_kind,
            "head": self.captured_head,
            "base": base,
            "diff_sha256": _digest(diff),
            "repo_root": str(self.root.resolve()),
            "excluded": excluded,
            "copies": {},
            "copied_bytes": 0,
            "failures": [],
        }
        # Reserve before copying: an interrupted capture must never use newer source bytes.
        directory.parent.mkdir(exist_ok=True)
        if directory.parent.is_symlink():
            raise ValueError("source capture symlink")
        with (directory.parent / "source-capture-started").open("xb"):
            pass
        directory.mkdir()
        if self.target_kind == "local":
            for file in sorted(
                set(dirty.decode("utf-8", "surrogateescape").split("\0"))
            ):
                if not _readable(self, file, excluded) or any(
                    part.lower() in NON_SOURCE_DIRS for part in file.split("/")[:-1]
                ):
                    continue
                m["copies"][file] = None
                try:
                    content = _local_bytes(
                        self.root,
                        cast(tuple[str, ...], self._parts(file)),
                        copy_budget=MAX_COPY_TOTAL_BYTES - m["copied_bytes"],
                    )
                    if _decode_source(content) is None:
                        raise ValueError("non-text source")
                    digest = _digest(content)
                    if not (directory / digest).exists():
                        (directory / digest).write_bytes(content)
                    m["copies"][file] = digest
                    m["copied_bytes"] += len(content)
                except (OSError, ValueError):
                    m["failures"].append(file)
        (directory / "diff").write_bytes(diff)
        with (directory / "manifest.json").open(
            "x", encoding="utf-8", newline=""
        ) as stream:
            json.dump(m, stream, ensure_ascii=True, sort_keys=True)
            stream.write("\n")
        return load_capture(self, output, {"nonce": nonce}, diff)

    def read_captured_source(
        self,
        file: str,
        *,
        side: Literal["head", "old"] = "head",
        start: int | None = None,
        end: int | None = None,
        base: str | None = None,
        diff_sha256: str | None = None,
    ) -> tuple[str, ...] | None:
        if self.source_capture is None:
            return None
        return self.source_capture.read(self, file, side, start, end, base, diff_sha256)

    def entries(self, directory: str = "") -> tuple[str, ...] | None:
        """List immediate names from the same source identity used by reads."""
        if self.source_capture is not None:
            return self.source_capture.entries(self, directory)
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
            or re.match(r"^[A-Za-z]:", file)
        ):
            return None
        return parts

    def path_kind(self, file: str) -> Literal["file", "directory", "unknown"]:
        if self.read(file) is not None:
            return "file"
        parts = self._parts(file)
        if parts is None:
            return "unknown"
        if self.source_capture is not None:
            return "directory" if self.entries(file) is not None else "unknown"
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


class _CaptureIdentity(TypedDict):
    version: Literal[1]
    nonce: str
    target_kind: TargetKind
    head: str
    base: str
    diff_sha256: str
    copied_bytes: int


class CaptureReceipt(_CaptureIdentity):
    manifest_sha256: str
    copied_files: int
    failure_count: int
    failure_sample: list[str]


class CaptureManifest(_CaptureIdentity):
    repo_root: str
    excluded: str | None
    copies: dict[str, str | None]
    failures: list[str]


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _readable(source: SourceAdapter, file: str, excluded: str | None) -> bool:
    parts = source._parts(file)
    return bool(
        parts
        and not any(part.lower().startswith("code-gauntlet-") for part in parts)
        and parts[-1] not in {"pr-description.json", "source-capture.json"}
        and (
            excluded is None
            or not (file == excluded or file.startswith(excluded + "/"))
        )
    )


def _git(source: SourceAdapter, *args: str, timeout: int = 3) -> bytes:
    try:
        result = proc.run_bytes(["git", *args], cwd=str(source.root), timeout=timeout)
    except (OSError, proc.TimeoutExpired) as exc:
        raise ValueError("source capture git input unavailable") from exc
    if result.returncode:
        raise ValueError("source capture git input unavailable")
    return result.stdout


@dataclass(frozen=True, slots=True)
class SourceCapture:
    directory: Path
    manifest: CaptureManifest
    receipt: CaptureReceipt
    _source: SourceAdapter = field(repr=False, compare=False)
    _old_spans: dict[str, tuple[OldSpan, ...]] = field(repr=False, compare=False)

    def adapter(self, root: Path) -> SourceAdapter:
        return SourceAdapter(
            root,
            self.manifest["target_kind"],
            self.manifest["head"],
            self,
        )

    def entries(self, source: SourceAdapter, directory: str) -> tuple[str, ...] | None:
        if directory and not _readable(source, directory, self.manifest["excluded"]):
            return None
        original = self._source.entries(directory)
        names = set(original or ())
        prefix = directory + "/" if directory else ""
        for file, digest in self.manifest["copies"].items():
            if not file.startswith(prefix):
                continue
            child, separator, _ = file[len(prefix) :].partition("/")
            name = prefix + child + ("/" if separator else "")
            if digest is not None:
                names.add(name)
            elif not separator:
                names.discard(name)
        return tuple(sorted(names)) if names or original is not None else None

    def read(
        self,
        source: SourceAdapter,
        file: str,
        side: Literal["head", "old"],
        start: int | None,
        end: int | None,
        base: str | None,
        diff_sha256: str | None,
    ) -> tuple[str, ...] | None:
        m = self.manifest
        if not _readable(source, file, m["excluded"]):
            return None
        try:
            if side == "old":
                if base != m["base"] or diff_sha256 != m["diff_sha256"]:
                    return None
                diff = _local_bytes(self.directory, ("diff",))
                if _digest(diff) != m["diff_sha256"]:
                    return None
                spans = self._old_spans.get(file, ())
                return next(
                    (
                        span.lines[start - span.start : end - span.start + 1]
                        for span in spans
                        if type(start) is int
                        and type(end) is int
                        and span.start <= start <= end < span.start + len(span.lines)
                    ),
                    None,
                )
            if (
                side != "head"
                or base is not None
                or diff_sha256 not in (None, m["diff_sha256"])
            ):
                return None
            if file in m["copies"]:
                digest = m["copies"][file]
                if digest is None:
                    return None
                content = _local_bytes(self.directory, (digest,))
                if _digest(content) != digest:
                    return None
                lines = _decode_source(content)
            else:
                lines = self._source.read(file)
            if start is None and end is None:
                return lines
            if (
                lines is not None
                and type(start) is int
                and type(end) is int
                and 1 <= start <= end <= len(lines)
            ):
                return lines[start - 1 : end]
        except (OSError, ValueError):
            pass
        return None


def load_capture(
    source: SourceAdapter,
    output: Path,
    metadata: dict[str, object],
    diff: bytes,
    requested_sha: str | None = None,
) -> SourceCapture:
    if not isinstance(metadata, dict) or (
        requested_sha is not None
        and set(metadata) != set(CaptureReceipt.__annotations__)
    ):
        raise ValueError("source capture requires its complete command receipt")
    nonce = metadata.get("nonce")
    if not isinstance(nonce, str) or not re.fullmatch(
        r"[A-Za-z0-9_-][A-Za-z0-9._-]*", nonce
    ):
        raise ValueError("invalid source capture nonce")
    parts = (f"code-gauntlet-inputs-{nonce}", "source-capture")
    raw = _local_bytes(output, (*parts, "manifest.json"))
    m = json.loads(raw)
    if (
        not isinstance(m, dict)
        or type(m.get("version")) is not int
        or m["version"] != 1
        or m.get("nonce") != nonce
        or m.get("target_kind") != source.target_kind
        or not isinstance(m.get("head"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", m["head"])
        or not isinstance(m.get("base"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", m["base"])
        or (source.captured_head is not None and source.captured_head != m["head"])
        or m.get("repo_root") != str(source.root.resolve())
        or m.get("diff_sha256") != _digest(diff)
        or not isinstance(m.get("copies"), dict)
        or not all(
            isinstance(k, str)
            and (v is None or (isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)))
            for k, v in m["copies"].items()
        )
        or not isinstance(m.get("failures"), list)
        or not all(isinstance(f, str) for f in m["failures"])
        or type(m.get("copied_bytes")) is not int
        or m["copied_bytes"] < 0
        or not (m.get("excluded") is None or isinstance(m["excluded"], str))
    ):
        raise ValueError("source capture identity or manifest mismatch")
    if requested_sha is not None and (
        not re.fullmatch(r"[0-9a-f]{4,40}", requested_sha)
        or not m["head"].startswith(requested_sha)
    ):
        raise ValueError("source capture requested head mismatch")
    receipt = cast(
        CaptureReceipt, {key: m[key] for key in _CaptureIdentity.__annotations__}
    )
    receipt["manifest_sha256"] = _digest(raw)
    receipt["copied_files"] = sum(v is not None for v in m["copies"].values())
    receipt["failure_count"] = len(m["failures"])
    receipt["failure_sample"] = m["failures"][:MAX_FAILURE_SAMPLE]
    if any(receipt.get(key) != value for key, value in metadata.items()):
        raise ValueError("source capture receipt mismatch")
    text = diff.decode("utf-8", "surrogateescape")
    spans: dict[str, tuple[OldSpan, ...]] = {}
    try:
        old_paths = parse_diff(text, policy=patch_report_policy(text)).old_paths
        for path, entries in annotate_diff(diff).old_spans.items():
            file = old_paths.get(path, path)
            spans[file] = (*spans.get(file, ()), *entries)
    except ValueError:
        # Malformed old-side evidence must not prevent independent head reads.
        spans.clear()
    return SourceCapture(
        output.joinpath(*parts),
        cast(CaptureManifest, m),
        receipt,
        SourceAdapter(source.root, "branch", m["head"]),
        spans,
    )
