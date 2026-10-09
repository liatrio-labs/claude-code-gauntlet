"""Task output discovery, whole-file reading, and bounded terminal extraction."""

from __future__ import annotations

import glob
import json
import os
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from gauntlet.fs import glob_under

TASKS_DIR_ENV = "CODE_GAUNTLET_TASKS_DIR"
TASK_ROOTS_ENV = "CODE_GAUNTLET_TASK_ROOTS"
TASK_OUTPUT_DIR_GLOB = os.path.join("*", "*", "tasks")
# Output files accumulate per session directory, so the cap bounds reads.
MAX_SCANNED_FILES = 200

# Bound attempted decodes as well as successes, so malformed input cannot hang a wait.
SCAN_MAX_CANDIDATES = 500
SCAN_MAX_PROBES = 2000
SCAN_MAX_CHARS = 8_000_000
# One deep document must not hide a later return; repeated stack exhaustion stops the scan.
SCAN_MAX_DEEP_CANDIDATES = 8

# Receipts also carry ok; only pipeline fields corroborate a terminal return.
# error is deliberately excluded as too generic to corroborate a return.
COMPACT_RETURN_KEYS = (
    "phaseReached",
    "stats",
    "artifactPaths",
    "checkpoints",
    "resolvedPolicy",
    "gaps",
    "failingPhase",
)


@dataclass(frozen=True, slots=True)
class TaskRoots:
    directories: tuple[str, ...]
    override: str | None


@dataclass(frozen=True, slots=True)
class TaskObservation:
    resolved_path: str | None
    searched: tuple[str, ...]
    file_bytes: int | None
    terminal: Mapping[str, object] | None
    saw_bare_ok: bool
    scan_stop_reason: str | None


def _root_candidates(
    bases: Sequence[str], tmpdir: str | None, uid: int | None, system_temp: str | None
) -> tuple[str, ...]:
    candidates = list(bases)
    if tmpdir:
        candidates.append(tmpdir.rstrip(os.sep) or os.sep)
    if uid is None:
        root_name = "claude"
        if system_temp is not None:
            candidates.append(system_temp)
    else:
        root_name = f"claude-{uid}"
    return tuple(os.path.join(base, root_name) for base in candidates)


def _deduplicate(directories: Sequence[str]) -> tuple[str, ...]:
    roots = []
    seen = set()
    for candidate in directories:
        real = os.path.realpath(candidate)
        if real not in seen:
            seen.add(real)
            roots.append(candidate)
    return tuple(roots)


# /tmp can resolve to /private/tmp on macOS, so both spellings are searched and realpath-deduplicated.
def _default_roots(tmpdir: str | None) -> tuple[str, ...]:
    getuid = getattr(os, "getuid", None)
    uid = getuid() if callable(getuid) else None
    candidates = _root_candidates(
        ("/tmp", "/private/tmp"),
        tmpdir,
        uid,
        tempfile.gettempdir() if uid is None else None,
    )
    # Retain nonexistent roots so unresolved markers still disclose where they searched.
    return _deduplicate(candidates)


def roots_from_environment(environ: Mapping[str, str]) -> TaskRoots:
    raw = environ.get(TASK_ROOTS_ENV, "")
    items = tuple(item for item in raw.split(os.pathsep) if item)
    directories = (
        _deduplicate(items) if items else _default_roots(environ.get("TMPDIR"))
    )
    return TaskRoots(directories, environ.get(TASKS_DIR_ENV))


def looks_like_path(target: str) -> bool:
    if not target:
        return False
    return (
        os.sep in target
        or (os.altsep is not None and os.altsep in target)
        or target.endswith(".output")
    )


def roots_for_request(target: str | None, nonce: str | None = None) -> TaskRoots:
    if target and looks_like_path(target) and not nonce:
        return TaskRoots((), None)
    return roots_from_environment(os.environ)


# An mtime floor could reject a fast failure written before the first await call.
# Short ids can collide across sessions, so newest favours the current run over another session's finished file; lexicographic order would not.
def _newest(paths: Sequence[str]) -> str | None:
    best, best_mtime = None, None
    for path in paths:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if best_mtime is None or mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def resolve_target(target: str, roots: TaskRoots) -> tuple[str | None, list[str]]:
    if looks_like_path(target):
        return target, []

    searched = []
    override = roots.override
    if override:
        direct = os.path.join(override, target + ".output")
        searched.append(direct)
        if os.path.exists(direct):
            return direct, searched

    pattern = os.path.join(TASK_OUTPUT_DIR_GLOB, glob.escape(target) + ".output")
    for root in roots.directories:
        searched.append(os.path.join(root, pattern))
        if not os.path.isdir(root):
            continue
        hits = glob_under(root, pattern)
        if hits:
            return _newest(hits) or sorted(hits)[0], searched
    return None, searched


# A FIFO open can block forever. Missing, torn, or undecodable output is pending.
# Strip a BOM and use universal newlines so both whole and appended JSON can be found.
def read_task(path: str | None) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with Path(path).open(encoding="utf-8-sig", errors="replace") as fh:
            return fh.read()
    except (OSError, ValueError):
        return ""


def _file_size(path: str | None) -> int | None:
    if not path:
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def is_terminal_return(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    if not isinstance(obj.get("ok"), bool):
        return False
    return any(key in obj for key in COMPACT_RETURN_KEYS)


def _has_bare_ok(obj: object) -> bool:
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("ok"), bool)
        and not any(key in obj for key in COMPACT_RETURN_KEYS)
    )


# Check result-object, result-string, then self. Recursive hunts promote agent receipts.
def terminal_from(value: object) -> tuple[Mapping[str, object] | None, bool]:
    saw_bare_ok = False
    if isinstance(value, dict):
        result = value.get("result")
        if is_terminal_return(result):
            return cast(Mapping[str, object], result), saw_bare_ok
        saw_bare_ok = saw_bare_ok or _has_bare_ok(result)
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except (ValueError, RecursionError):
                parsed = None
            if is_terminal_return(parsed):
                return cast(Mapping[str, object], parsed), saw_bare_ok
            saw_bare_ok = saw_bare_ok or _has_bare_ok(parsed)
    if is_terminal_return(value):
        return cast(Mapping[str, object], value), saw_bare_ok
    saw_bare_ok = saw_bare_ok or _has_bare_ok(value)
    return None, saw_bare_ok


# Column zero excludes nested fragments in torn pretty JSON and bounds decode cost.
def _document_starts(text: str) -> Iterator[int]:
    for line_start in _line_starts(text):
        if line_start < len(text) and text[line_start] == "{":
            yield line_start


def _line_starts(text: str) -> Iterator[int]:
    yield 0
    idx = text.find("\n")
    while idx != -1:
        yield idx + 1
        idx = text.find("\n", idx + 1)


def _iter_json_objects(
    text: str,
    max_candidates: int = SCAN_MAX_CANDIDATES,
    max_probes: int = SCAN_MAX_PROBES,
    max_deep: int = SCAN_MAX_DEEP_CANDIDATES,
) -> Iterator[tuple[Mapping[str, object] | None, str | None]]:
    decoder = json.JSONDecoder()
    yielded = 0
    probes = 0
    deep = 0
    consumed_to = -1
    for idx in _document_starts(text):
        if yielded >= max_candidates:
            yield None, "max_candidates"
            return
        if probes >= max_probes:
            yield None, "max_probes"
            return
        if idx < consumed_to:
            continue  # already inside an object this scan decoded
        probes += 1
        try:
            obj, end = decoder.raw_decode(text, idx)
        except RecursionError:
            deep += 1
            if deep >= max_deep:
                yield None, "max_deep_candidates"
                return
            continue
        except ValueError:
            # The error position may be past a later valid document; do not skip to it.
            continue
        # A successful span contains nested values, not sibling documents.
        consumed_to = end
        if isinstance(obj, dict):
            yielded += 1
            yield obj, None


def find_terminal(text: str) -> tuple[Mapping[str, object] | None, bool, str | None]:
    if not text or not text.strip():
        return None, False, None

    saw_bare_ok = False
    # Whole JSON may exceed the embedded scan bound; the last appended return wins.
    try:
        whole = json.loads(text)
    except (ValueError, RecursionError):
        pass
    else:
        found, bare = terminal_from(whole)
        saw_bare_ok = saw_bare_ok or bare
        if found is not None:
            return found, saw_bare_ok, None

    # Check before allocating scan state, while preserving the whole-document exception.
    if len(text) > SCAN_MAX_CHARS:
        return None, saw_bare_ok, "max_chars"

    stop_reason = None
    found = None
    for candidate, reason in _iter_json_objects(text):
        if candidate is None:
            stop_reason = reason
            break
        hit, bare = terminal_from(candidate)
        saw_bare_ok = saw_bare_ok or bare
        if hit is not None:
            found = hit
    # A later bound cannot invalidate a terminal already found.
    return found, saw_bare_ok, (None if found is not None else stop_reason)


def observe(target: str, roots: TaskRoots) -> TaskObservation:
    path, searched = resolve_target(target, roots)
    # Size is independent of the regular-file guard, including directories and FIFOs.
    file_bytes = _file_size(path)
    terminal, bare, stop_reason = find_terminal(read_task(path))
    return TaskObservation(
        path, tuple(searched), file_bytes, terminal, bare, stop_reason
    )


# A nonce sweep is globally newest-first and capped; named candidates are separate.
def sweep_paths(roots: TaskRoots, limit: int = MAX_SCANNED_FILES) -> tuple[str, ...]:
    roots_and_patterns = []
    override = roots.override
    if override:
        roots_and_patterns.append((override, "*.output"))
    for root in roots.directories:
        roots_and_patterns.append(
            (root, os.path.join(TASK_OUTPUT_DIR_GLOB, "*.output"))
        )
    hits = []
    for root, pattern in roots_and_patterns:
        found = glob_under(root, pattern)
        for path in found:
            try:
                hits.append((os.path.getmtime(path), path))
            except OSError:
                continue
    hits.sort(key=lambda pair: pair[0], reverse=True)
    return tuple(path for _mtime, path in hits[:limit])
