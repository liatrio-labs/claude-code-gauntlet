"""Bounded lexical evidence from the same source snapshot as the review."""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from gauntlet.diff import AnnotatedDiff, split_diff_lines
from gauntlet.location import SnapshotFile, SourceAdapter
from gauntlet.paths import NON_SOURCE_DIRS, is_test_path
from gauntlet.registry import READ_PLAN_LINE_PREFIX_BYTES, READ_PLAN_MAX_BYTES

MAX_FILES = 4000
MAX_CALLEE_CALLS = 32
MAX_CALL_OCCURRENCES = 64
MAX_LISTING_BYTES = 1024 * 1024
MAX_SCAN_SECONDS = 5.0
MAX_CALL_LINE_BYTES = 4096
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
_CALL = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(")


@dataclass(frozen=True, slots=True)
class CalleeCandidate:
    file: str
    line: int
    prefix: str
    name: str


@dataclass(frozen=True, slots=True)
class RankedCall:
    key: tuple[int, bool, int]
    call: str
    matches: tuple[CalleeCandidate, ...]


class FactCard(TypedDict):
    kind: Literal["neighbor", "callee_candidate", "sibling_contrast"]
    file: str
    line_start: int
    line_end: int
    text: str
    related_files: list[str]
    detail: str


class ScanStatus(TypedDict):
    available: bool
    listing_truncated: bool
    deadline_reached: bool
    excluded: int
    oversize_files: int
    oversize_lines: int
    file_limit: int
    byte_limit: int
    unreadable: int
    selected_files: int
    selected_bytes: int
    diff_limited: bool


class FactCards(TypedDict):
    cards: list[FactCard]
    budget_bytes: int
    used_bytes: int
    omitted: int
    scan_limited: bool
    scan: ScanStatus


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


def _body_after_parameters(lines: tuple[str, ...], start: int, opening: int) -> bool:
    # An argument object is not a definition body; the parameter list must end first.
    depth = 1
    indent = len(lines[start]) - len(lines[start].lstrip())
    for row in range(start, min(len(lines), start + 10)):
        text = lines[row]
        if len(text) > MAX_CALL_LINE_BYTES:
            return False
        for column in range(opening + 1 if row == start else 0, len(text)):
            if text[column] == "(":
                depth += 1
            elif text[column] == ")":
                depth -= 1
                if depth == 0:
                    suffix = text[column + 1 :]
                    header = suffix.split("{", 1)[0]
                    if any(ch in header for ch in ";=()"):
                        return False
                    following = lines[row + 1] if row + 1 < len(lines) else ""
                    nested = len(following) - len(following.lstrip()) > indent
                    return "{" in suffix or nested
    return False


def build_fact_cards(
    source: SourceAdapter, annotated: AnnotatedDiff, diff_bytes: int
) -> FactCards:
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    scan: ScanStatus = {
        "available": False,
        "listing_truncated": False,
        "deadline_reached": False,
        "excluded": 0,
        "oversize_files": 0,
        "oversize_lines": 0,
        "file_limit": 0,
        "byte_limit": 0,
        "unreadable": 0,
        "selected_files": 0,
        "selected_bytes": 0,
        "diff_limited": False,
    }
    budget = min(
        READ_PLAN_MAX_BYTES - READ_PLAN_LINE_PREFIX_BYTES - 1,
        max(4096, diff_bytes // 4),
    )
    result: FactCards = {
        "cards": [],
        "budget_bytes": budget,
        "used_bytes": 512,
        "omitted": 0,
        "scan_limited": False,
        "scan": scan,
    }
    changed = set(annotated.files)
    call_files: dict[str, set[str]] = {}
    old_calls: set[str] = set()
    for index, line in enumerate(
        split_diff_lines(annotated.content.decode("utf-8", "replace"))[0], 1
    ):
        if time.monotonic() >= deadline:
            scan["deadline_reached"] = scan["diff_limited"] = True
            break
        if len(line.encode("utf-8")) > MAX_CALL_LINE_BYTES:
            scan["diff_limited"] = True
            continue
        if re.match(r"N\d+ \+\|", line):
            text = line.split("|", 1)[1]
            for call in _CALL.findall(text):
                call_files.setdefault(call, set()).add(annotated.entries[index]["file"])
        elif re.match(r"O\d+ -\|", line):
            old_calls.update(_CALL.findall(line.split("|", 1)[1]))
    calls = [call for call in call_files if call not in old_calls]
    receivers = {call.split(".")[-2].lower() for call in calls if "." in call}
    candidates: dict[str, list[CalleeCandidate]] = {}
    neighbors: list[FactCard] = []
    callees: list[FactCard] = []
    siblings: list[FactCard] = []
    inventory = source.tracked_files(
        source_path,
        deadline=deadline,
        max_listing_bytes=MAX_LISTING_BYTES,
        priority_directories=sorted(
            {f.rpartition("/")[0] for f in changed if "/" in f}
        ),
    )
    inventory = source.unignored_files(
        inventory, deadline=deadline, max_listing_bytes=MAX_LISTING_BYTES
    )
    scan["available"] = inventory.available
    scan["listing_truncated"] = inventory.truncated
    scan["excluded"] = inventory.excluded
    directories = {f.rpartition("/")[0] for f in changed}
    paths = sorted(
        inventory.files,
        key=lambda item: (
            item.file not in changed,
            Path(item.file).stem.lower() not in receivers,
            item.file.rpartition("/")[0] not in directories,
            is_test_path(item.file),
            item.file,
        ),
    )
    selected: list[SnapshotFile] = []
    for index, entry in enumerate(paths):
        if time.monotonic() >= deadline:
            scan["deadline_reached"] = True
            break
        if entry.size > MAX_FILE_BYTES:
            scan["oversize_files"] += 1
        elif len(selected) >= MAX_FILES:
            scan["file_limit"] += len(paths) - index
            break
        elif scan["selected_bytes"] + entry.size > MAX_SCAN_BYTES:
            scan["byte_limit"] += 1
        else:
            selected.append(entry)
            scan["selected_bytes"] += entry.size
    scan["selected_files"] = len(selected)
    read = source.read_snapshot(selected, deadline=deadline)
    scan["unreadable"] = inventory.unreadable + read.unreadable
    scan["deadline_reached"] |= read.deadline_reached
    loaded = dict(read.files)

    def card(
        kind: Literal["neighbor", "callee_candidate", "sibling_contrast"],
        file: str,
        name: str,
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
            "text": name + "(…)",
            "related_files": related,
            "detail": detail,
        }

    shown_head = {
        (e["file"], e["line"])
        for e in annotated.entries.values()
        if e["side"] == "head"
    }
    names = {call.rsplit(".", 1)[-1] for call in calls}
    occurrences: Counter[str] = Counter()
    prefix_uses: Counter[str] = Counter()
    prefix_names: dict[str, set[str]] = {}
    declarations: dict[str, list[CalleeCandidate]] = {}
    for file, lines in loaded.items() if names else ():
        if time.monotonic() >= deadline:
            scan["deadline_reached"] = True
            break
        for n, line in enumerate(lines):
            if time.monotonic() >= deadline:
                scan["deadline_reached"] = True
                break
            # Bound regex work before encoding or scanning generated/minified lines.
            if (
                len(line) > MAX_CALL_LINE_BYTES
                or len(line.encode("utf-8")) > MAX_CALL_LINE_BYTES
            ):
                scan["oversize_lines"] += 1
                continue
            for match in _CALL.finditer(line):
                token = match[1]
                name = token.rsplit(".", 1)[-1]
                words = line[: match.start()].split()
                prefix = " ".join(words)
                introducing = bool(words) and all(
                    re.fullmatch(r"[A-Za-z_]\w*", word) for word in words
                )
                if len(words) > 2:
                    prefix = " ".join(
                        (words[0], *("_" for _ in words[1:-1]), words[-1])
                    )
                if introducing:
                    prefix_uses[prefix] += 1
                    prefix_names.setdefault(prefix, set()).add(name)
                if (
                    introducing
                    and "." not in token
                    and _body_after_parameters(lines, n, match.end() - 1)
                ):
                    declarations.setdefault(file, []).append(
                        CalleeCandidate(file, n + 1, prefix, name)
                    )
                if name not in names:
                    continue
                occurrences[name] += 1
                if occurrences[name] > MAX_CALL_OCCURRENCES or "." in token:
                    continue
                if introducing and _body_after_parameters(lines, n, match.end() - 1):
                    # Changed lines are already in the diff, not an outside fact.
                    if (file, n + 1) in shown_head:
                        continue
                    candidates.setdefault(name, []).append(
                        CalleeCandidate(file, n + 1, prefix, name)
                    )

    def introduced(candidate: CalleeCandidate) -> bool:
        return prefix_uses[candidate.prefix] <= 2 * len(prefix_names[candidate.prefix])

    declarations = {
        file: [d for d in items if introduced(d)]
        for file, items in declarations.items()
    }

    def distance(file: str, related: set[str]) -> int:
        parts = file.split("/")[:-1]
        best = len(parts) + 1000
        for other in related:
            peer = other.split("/")[:-1]
            shared = 0
            for left, right in zip(parts, peer, strict=False):
                if left != right:
                    break
                shared += 1
            best = min(best, len(parts) + len(peer) - 2 * shared)
        return best

    ranked: list[RankedCall] = []
    for order, call in enumerate(calls):
        name = call.rsplit(".", 1)[-1]
        all_matches = candidates.get(name, [])
        # Introduction-shaped prefixes normally bind distinct names. Repeated uses
        # of the same names reveal invocation syntax without language keyword lists.
        matches = [m for m in all_matches if introduced(m)]
        result["omitted"] += len(all_matches) - len(matches)
        if occurrences[name] > MAX_CALL_OCCURRENCES:
            result["omitted"] += len(matches)
            continue
        receiver = call.split(".")[-2].lower() if "." in call else ""
        preferred = [m for m in matches if Path(m.file).stem.lower() == receiver]
        matches = sorted(
            preferred or matches,
            key=lambda m: (
                distance(m.file, call_files[call]),
                is_test_path(m.file),
                m.file,
                m.line,
            ),
        )
        if matches:
            ranked.append(
                RankedCall(
                    (
                        distance(matches[0].file, call_files[call]),
                        not bool(preferred),
                        order,
                    ),
                    call,
                    tuple(matches),
                )
            )
    ranked.sort(key=lambda ranked_call: ranked_call.key)
    for ranked_call in ranked[MAX_CALLEE_CALLS:]:
        result["omitted"] += min(3, len(ranked_call.matches))
    for ranked_call in ranked[:MAX_CALLEE_CALLS]:
        result["omitted"] += max(0, len(ranked_call.matches) - 3)
        for definition in ranked_call.matches[:3]:
            callees.append(
                card(
                    "callee_candidate",
                    definition.file,
                    definition.name,
                    definition.line,
                    definition.line,
                    sorted(call_files[ranked_call.call]),
                    f"Lexical candidate for {ranked_call.call}; declaration outline only; parameters and body omitted, resolution unproven.",
                )
            )

    for file in sorted(changed, key=lambda p: (is_test_path(p), p)):
        if time.monotonic() >= deadline:
            scan["deadline_reached"] = True
            break
        for declaration in declarations.get(file, []):
            if (file, declaration.line) not in shown_head and any(
                max(1, a - 20) <= declaration.line <= b + 20
                for a, b in annotated.hunks.get(file, ())
            ):
                neighbors.append(
                    card(
                        "neighbor",
                        file,
                        declaration.name,
                        declaration.line,
                        declaration.line,
                        [file],
                        "Unchanged nearby declaration; parameters and body omitted.",
                    )
                )
        directory = file.rpartition("/")[0]
        peers = [
            p
            for p in loaded
            if p not in changed
            and p.rpartition("/")[0] == directory
            and is_test_path(p) == is_test_path(file)
        ]
        if len(peers) < 3:
            continue
        counts: Counter[str] = Counter()
        for peer in peers:
            counts.update({d.name for d in declarations.get(peer, [])})
        present = {d.name for d in declarations.get(file, [])}
        signals = sorted(
            (
                name
                for name, count in counts.items()
                if count >= math.ceil(len(peers) * 0.6) and name not in present
            ),
            key=lambda name: (-counts[name], name),
        )[:6]
        for name in signals:
            declaration = next(
                d
                for peer in peers
                for d in declarations.get(peer, [])
                if d.name == name
            )
            siblings.append(
                card(
                    "sibling_contrast",
                    declaration.file,
                    name,
                    declaration.line,
                    declaration.line,
                    [file],
                    f"Declaration absent from changed file; present in {counts[name]}/{len(peers)} unchanged peers. Parameters and bodies omitted; no correctness claim.",
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
    result["scan_limited"] = (
        not scan["available"]
        or scan["diff_limited"]
        or scan["listing_truncated"]
        or scan["deadline_reached"]
        or any(
            scan[k]
            for k in (
                "oversize_files",
                "oversize_lines",
                "file_limit",
                "byte_limit",
                "unreadable",
            )
        )
    )
    return result
