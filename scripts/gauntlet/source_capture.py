"""Persist repository evidence independently of later worktree and HEAD changes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

from gauntlet import proc
from gauntlet.diff import annotate_diff, parse_diff, patch_report_policy
from gauntlet.location import SourceAdapter, TargetKind, _decode_source, _local_bytes
from gauntlet.paths import NON_SOURCE_DIRS


class _CaptureIdentity(TypedDict):
    version: Literal[1]
    nonce: str
    target_kind: TargetKind
    head: str
    base: str
    diff_sha256: str
    copied_bytes: int
    failures: list[str]


class CaptureReceipt(_CaptureIdentity):
    manifest_sha256: str
    copied_files: int


class CaptureManifest(_CaptureIdentity):
    repo_root: str
    excluded: str | None
    copies: dict[str, str | None]


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _eligible(source: SourceAdapter, file: str, excluded: str | None) -> bool:
    parts = source._parts(file)
    return bool(
        parts
        and not any(
            part.lower() in NON_SOURCE_DIRS | {".code-gauntlet", ".codex", "pairing"}
            or part.lower().startswith(("code-gauntlet-", ".env"))
            for part in parts
        )
        and parts[-1] not in {"pr-description.json", "source-capture.json"}
        and not (parts[0] == "backups" and file.endswith(".gpg"))
        and (
            excluded is None
            or not (file == excluded or file.startswith(excluded + "/"))
        )
    )


def _git(source: SourceAdapter, *args: str) -> bytes:
    try:
        result = proc.run_bytes(["git", *args], cwd=str(source.root), timeout=3)
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

    def adapter(self, root: Path) -> SourceAdapter:
        return SourceAdapter(
            root,
            self.manifest["target_kind"],
            self.manifest["head"],
            self,
        )

    def entries(self, source: SourceAdapter, directory: str) -> tuple[str, ...] | None:
        if directory and not _eligible(source, directory, self.manifest["excluded"]):
            return None
        original = SourceAdapter(source.root, "branch", self.manifest["head"]).entries(
            directory
        )
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
        if not _eligible(source, file, m["excluded"]):
            return None
        try:
            if side == "old":
                if base != m["base"] or diff_sha256 != m["diff_sha256"]:
                    return None
                diff = _local_bytes(self.directory, ("diff",))
                if _digest(diff) != m["diff_sha256"]:
                    return None
                text = diff.decode("utf-8", "surrogateescape")
                old_paths = parse_diff(text, policy=patch_report_policy(text)).old_paths
                spans = tuple(
                    span
                    for path, spans in annotate_diff(diff).old_spans.items()
                    if old_paths.get(path, path) == file
                    for span in spans
                )
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
                lines = SourceAdapter(source.root, "branch", m["head"]).read(file)
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
    if any(receipt.get(key) != value for key, value in metadata.items()):
        raise ValueError("source capture receipt mismatch")
    return SourceCapture(output.joinpath(*parts), cast(CaptureManifest, m), receipt)


def capture_sources(
    source: SourceAdapter, output: Path, nonce: str, diff: bytes, base: str | None
) -> SourceCapture:
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", nonce):
        raise ValueError("invalid source capture nonce")
    parts = (f"code-gauntlet-inputs-{nonce}", "source-capture")
    directory = output.joinpath(*parts)
    if directory.exists() or (directory.parent / "source-capture-started").exists():
        loaded = load_capture(source, output, {"nonce": nonce}, diff)
        if base is not None and base != loaded.manifest["base"]:
            raise ValueError("source capture base mismatch")
        return loaded
    if source.captured_head is None:
        raise ValueError("source capture head unavailable")
    base = base or source.captured_head
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("source capture requires a full base commit")
    _git(source, "cat-file", "-e", base + "^{commit}")
    output = output.resolve()
    excluded = (
        output.relative_to(source.root.resolve()).as_posix()
        if output.is_relative_to(source.root.resolve())
        else None
    )
    m: CaptureManifest = {
        "version": 1,
        "nonce": nonce,
        "target_kind": source.target_kind,
        "head": source.captured_head,
        "base": base,
        "diff_sha256": _digest(diff),
        "repo_root": str(source.root.resolve()),
        "excluded": excluded,
        "copies": {},
        "copied_bytes": 0,
        "failures": [],
    }
    # Reserve before reading: an interrupted capture must never be rebuilt from newer bytes.
    directory.parent.mkdir(exist_ok=True)
    if directory.parent.is_symlink():
        raise ValueError("source capture symlink")
    with (directory.parent / "source-capture-started").open("xb"):
        pass
    directory.mkdir()
    if source.target_kind == "local":
        dirty = _git(
            source,
            "diff",
            "--name-only",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "-z",
            source.captured_head,
            "--",
        )
        dirty += _git(source, "ls-files", "--others", "--exclude-standard", "-z")
        for file in sorted(set(dirty.decode("utf-8", "surrogateescape").split("\0"))):
            if not _eligible(source, file, excluded):
                continue
            m["copies"][file] = None
            try:
                content = _local_bytes(
                    source.root, cast(tuple[str, ...], source._parts(file))
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
    return load_capture(source, output, {"nonce": nonce}, diff)
