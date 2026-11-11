"""Bounded lexical evidence from the same source snapshot as the review."""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, TypedDict

from gauntlet import proc
from gauntlet.diff import AnnotatedDiff
from gauntlet.location import SourceAdapter
from gauntlet.paths import NON_SOURCE_DIRS, is_test_path
from gauntlet.registry import READ_PLAN_LINE_PREFIX_BYTES, READ_PLAN_MAX_BYTES

MAX_FILES = 4000
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
_CALL = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(")


class FactCard(TypedDict):
    kind: Literal["neighbor", "callee_candidate", "sibling_contrast"]
    file: str
    line_start: int
    line_end: int
    text: str
    related_files: list[str]
    detail: str


class FactCards(TypedDict):
    cards: list[FactCard]
    budget_bytes: int
    used_bytes: int
    omitted: int
    scan_limited: bool


def source_path(file: str) -> bool:
    parts = file.rstrip("/").split("/")
    # Evidence harvesting must not turn an ordinary review into secret collection.
    return not any(
        p.lower() in NON_SOURCE_DIRS
        or p.lower() in {"pairing", "backups", ".codex", "credentials", "secrets"}
        or p.lower().startswith(".env")
        or p.lower()
        in {
            "auth.json",
            "credentials.json",
            "credentials.yaml",
            "credentials.yml",
            "secrets.json",
            "secrets.yaml",
            "secrets.yml",
            ".aws",
            ".ssh",
            ".netrc",
            ".npmrc",
            ".pypirc",
            ".git-credentials",
            "id_rsa",
            "id_ed25519",
        }
        or p.lower().endswith((".gpg", ".pem", ".key"))
        for p in parts
    )


def _files(source: SourceAdapter) -> Iterable[str]:
    if source.target_kind != "local":
        if source.captured_head is None:
            return
        try:
            result = proc.run_bytes(
                ["git", "ls-tree", "-rz", "--name-only", source.captured_head],
                cwd=str(source.root),
                timeout=5,
            )
        except (OSError, proc.TimeoutExpired):
            return
        if result.returncode == 0:
            for raw in result.stdout.split(b"\0"):
                file = raw.decode("utf-8", "surrogateescape")
                if file and source_path(file):
                    yield file
        return
    for directory, dirs, files in os.walk(source.root, followlinks=False):
        relative = Path(directory).relative_to(source.root)
        dirs[:] = sorted(
            d
            for d in dirs
            if source_path(str(relative / d)) and not (Path(directory) / d).is_symlink()
        )
        for name in sorted(files):
            file = (relative / name).as_posix()
            if source_path(file) and not (Path(directory) / name).is_symlink():
                yield file


def build_fact_cards(
    source: SourceAdapter, annotated: AnnotatedDiff, diff_bytes: int
) -> FactCards:
    budget = min(
        READ_PLAN_MAX_BYTES - READ_PLAN_LINE_PREFIX_BYTES - 1,
        max(4096, diff_bytes // 4),
    )
    result: FactCards = {
        "cards": [],
        "budget_bytes": budget,
        "used_bytes": 256,
        "omitted": 0,
        "scan_limited": False,
    }
    changed = set(annotated.files)
    call_files: dict[str, set[str]] = {}
    added: list[str] = []
    removed: list[str] = []
    for index, line in enumerate(
        annotated.content.decode("utf-8", "replace").splitlines(), 1
    ):
        if re.match(r"N\d+ \+\|", line):
            text = line.split("|", 1)[1]
            added.append(text)
            for call in _CALL.findall(text):
                call_files.setdefault(call, set()).add(annotated.entries[index]["file"])
        elif re.match(r"O\d+ -\|", line):
            removed.append(line.split("|", 1)[1])
    old_calls = set(_CALL.findall("\n".join(removed)))
    calls = sorted(
        set(_CALL.findall("\n".join(added))) - old_calls,
        key=lambda call: ("." not in call, call),
    )[:32]
    receivers = {call.split(".")[-2].lower() for call in calls if "." in call}
    candidates: dict[str, list[tuple[str, int, tuple[str, ...]]]] = {}
    neighbors: list[FactCard] = []
    callees: list[FactCard] = []
    siblings: list[FactCard] = []
    loaded: dict[str, tuple[str, ...]] = {}

    def card(
        kind: Literal["neighbor", "callee_candidate", "sibling_contrast"],
        file: str,
        lines: tuple[str, ...],
        start: int,
        end: int,
        related: list[str],
        detail: str,
    ) -> FactCard:
        return {
            "kind": kind,
            "file": file,
            "line_start": start,
            "line_end": end,
            "text": "\n".join(lines[start - 1 : end]),
            "related_files": related,
            "detail": detail,
        }

    for file in sorted(changed, key=lambda p: (is_test_path(p), p)):
        if not source_path(file):
            continue
        lines = source.read(file)
        if lines is None:
            continue
        loaded[file] = lines
        shown = {
            entry["line"]
            for entry in annotated.entries.values()
            if entry["file"] == file and entry["side"] == "head"
        }
        wanted = sorted(
            {
                n
                for start, end in annotated.hunks.get(file, ())
                for n in range(max(1, start - 20), min(len(lines), end + 20) + 1)
                if n not in shown
            }
        )
        spans: list[tuple[int, int]] = []
        for n in wanted:
            if spans and n == spans[-1][1] + 1 and n - spans[-1][0] < 20:
                spans[-1] = (spans[-1][0], n)
            else:
                spans.append((n, n))
        for lo, hi in spans:
            neighbors.append(
                card(
                    "neighbor",
                    file,
                    lines,
                    lo,
                    hi,
                    [file],
                    "Head neighborhood outside diff; unchanged code.",
                )
            )

    paths = sorted(
        _files(source),
        key=lambda file: (
            Path(file).stem.lower() not in receivers,
            file.rpartition("/")[0] not in {f.rpartition("/")[0] for f in changed},
            file,
        ),
    )
    scanned = 0
    for index, file in enumerate(paths):
        if index >= MAX_FILES or scanned >= MAX_SCAN_BYTES:
            result["scan_limited"] = True
            break
        if file in changed:
            continue
        lines = source.read(file)
        if lines is None:
            continue
        size = sum(len(line.encode("utf-8")) + 1 for line in lines)
        scanned += size
        if size > MAX_FILE_BYTES:
            continue
        loaded[file] = lines
        for call in calls:
            name = call.rsplit(".", 1)[-1]
            pattern = re.compile(r"(?<![\w.])" + re.escape(name) + r"\s*\(")
            for n, line in enumerate(lines):
                match = pattern.search(line)
                if not match:
                    continue
                prefix = line[: match.start()].strip()
                # This is lexical evidence, not a language-specific declaration parser.
                following = lines[n + 1] if n + 1 < len(lines) else ""
                indent = len(line) - len(line.lstrip())
                nested = len(following) - len(following.lstrip()) > indent
                if (prefix and not any(c in prefix for c in "=();{}")) and (
                    "{" in line[match.end() :] or nested
                ):
                    candidates.setdefault(call, []).append((file, n + 1, lines))

    for call in sorted(
        calls,
        key=lambda c: (
            not any(
                Path(m[0]).stem.lower()
                == (c.split(".")[-2].lower() if "." in c else "")
                for m in candidates.get(c, [])
            ),
            c,
        ),
    ):
        matches = candidates.get(call, [])
        receiver = call.split(".")[-2].lower() if "." in call else ""
        preferred = [m for m in matches if Path(m[0]).stem.lower() == receiver]
        matches = preferred or matches
        if len(matches) > 3:
            result["omitted"] += len(matches)
            continue
        for file, definition_line, lines in matches:
            callees.append(
                card(
                    "callee_candidate",
                    file,
                    lines,
                    definition_line,
                    min(len(lines), definition_line + 9),
                    sorted(call_files[call]),
                    f"Lexical candidate for {call}; bounded excerpt, resolution unproven.",
                )
            )

    for file in sorted(changed, key=lambda p: (is_test_path(p), p)):
        lines = loaded.get(file)
        if lines is None:
            continue
        directory = file.rpartition("/")[0]
        peers = [
            (p, text)
            for p, text in loaded.items()
            if p not in changed
            and p.rpartition("/")[0] == directory
            and is_test_path(p) == is_test_path(file)
        ]
        if len(peers) < 3:
            continue
        counts = Counter(
            line
            for _, text in peers
            for line in {
                s.strip()
                for s in text
                if s[:1].isspace() and len(re.findall(r"\w+", s)) >= 3
            }
        )
        present = {line.strip() for line in lines}
        signals = sorted(
            (
                line
                for line, count in counts.items()
                if count >= math.ceil(len(peers) * 0.6) and line not in present
            ),
            key=lambda line: (-counts[line], line),
        )[:6]
        for signal in signals:
            peer, peer_lines = next(
                (p, t) for p, t in peers if signal in {s.strip() for s in t}
            )
            signal_line = next(
                i + 1 for i, s in enumerate(peer_lines) if s.strip() == signal
            )
            siblings.append(
                card(
                    "sibling_contrast",
                    peer,
                    peer_lines,
                    max(1, signal_line - 1),
                    min(len(peer_lines), signal_line + 2),
                    [file],
                    f"Exact line absent from changed file; present in {counts[signal]}/{len(peers)} unchanged peers. No correctness claim.",
                )
            )

    neighbors.sort(
        key=lambda c: (
            is_test_path(c["file"]),
            not any(
                a <= c["line_start"] and c["line_end"] <= b
                for (_, a), (b, _) in zip(
                    annotated.hunks.get(c["file"], ()),
                    annotated.hunks.get(c["file"], ())[1:],
                    strict=False,
                )
            ),
            c["file"],
            c["line_start"],
        )
    )
    callees.sort(key=lambda c: is_test_path(c["file"]))
    siblings.sort(key=lambda c: is_test_path(c["related_files"][0]))

    # Round-robin kinds prevent large neighborhoods from starving relational evidence.
    def spread(items: list[FactCard]) -> list[FactCard]:
        groups: dict[str, list[FactCard]] = {}
        for item in items:
            groups.setdefault(item["related_files"][0], []).append(item)
        output = []
        while any(groups.values()):
            for group in groups.values():
                if group:
                    output.append(group.pop(0))
        return output

    queues = [callees, spread(siblings), spread(neighbors)]
    while any(queues):
        for queue in queues:
            if not queue:
                continue
            item = queue.pop(0)
            size = (
                len(
                    json.dumps(item, ensure_ascii=True)
                    .replace("<", "\\u003c")
                    .encode("utf-8")
                )
                + 2
            )
            if result["used_bytes"] + size <= budget:
                result["cards"].append(item)
                result["used_bytes"] += size
            else:
                result["omitted"] += 1
    return result
