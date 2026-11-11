"""Map changed hunk packets to measured reads without copying source into argv."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal, TypedDict

from gauntlet.diff import AnnotatedDiff, split_diff_lines
from gauntlet.registry import READ_PLAN_LINE_PREFIX_BYTES


class ReadRange(TypedDict):
    """Absolute shared-context rows and their charged Read bytes."""

    offset: int
    limit: int
    bytes: int


class Region(TypedDict):
    id: str
    file: str
    line_start: int
    line_end: int
    reads: list[ReadRange]


class FactPacket(TypedDict):
    related_files: list[str]
    reads: list[ReadRange]


class DiscoveryPlan(TypedDict):
    version: Literal[2]
    status: Literal["available", "unavailable"]
    regions: list[Region]
    fact_packets: list[FactPacket]


def read_line_bytes(lines: Sequence[str]) -> int:
    return sum(
        len(line.encode("utf-8")) + 1 + READ_PLAN_LINE_PREFIX_BYTES for line in lines
    )


def read_ranges(
    content: str, offset: int, pack: Callable[[str], list[int]]
) -> list[ReadRange]:
    reads: list[ReadRange] = []
    rows, _ = split_diff_lines(content)
    consumed = 0
    for count in pack(content):
        size = read_line_bytes(rows[consumed : consumed + count])
        reads.append({"offset": offset, "limit": count, "bytes": size})
        consumed += count
        offset += count
    return reads


class RegionPlanError(ValueError):
    """A changed packet exceeds the measured-read allocation ceiling."""


@dataclass(frozen=True, slots=True)
class DiffPacket:
    """Coordinates in annotated diff rows, before context framing or placement."""

    id: str
    file: str
    line_start: int
    line_end: int
    annotated_offset: int
    line_count: int


def changed_packets(
    annotated: AnnotatedDiff, pack: Callable[[str], list[int]]
) -> list[DiffPacket]:
    lines, _ = split_diff_lines(annotated.content.decode("utf-8", "replace"))
    starts = [
        i for i, line in enumerate(lines) if line.startswith(("@@ ", "diff --git "))
    ]
    starts.append(len(lines))
    packets: list[DiffPacket] = []
    for start, end in pairwise(starts):
        if not lines[start].startswith("@@ "):
            continue
        packet = "\n".join(lines[start:end]) + "\n"
        counts = pack(packet)
        if not counts:
            raise RegionPlanError("changed packet cannot be measured")
        consumed = 0
        for count in counts:
            annotated_offset = start + 1 + consumed
            consumed += count
            entries = [
                annotated.entries[n]
                for n in range(annotated_offset, annotated_offset + count)
                if n in annotated.entries
            ]
            if not entries:
                continue
            head = [e["line"] for e in entries if e["side"] == "head"]
            # Keep deletion-only packets in the ledger; the hunter cannot anchor in old coordinates.
            lo, hi = (min(head), max(head)) if head else (0, 0)
            file = entries[0]["file"]
            identity = hashlib.sha256(
                f"{file}:{annotated_offset}:{count}".encode()
            ).hexdigest()[:16]
            packets.append(DiffPacket(identity, file, lo, hi, annotated_offset, count))
    return packets


class CoveredRegion(TypedDict):
    id: str
    file: str
    line_start: int
    line_end: int
    cited_by: list[str]


class OmittedRegion(TypedDict):
    id: str
    reason: Literal["budget", "no_head_anchor", "scope", "primary_failure"]


class HunterDispatch(TypedDict):
    agent: str
    assigned: list[str]
    status: Literal["complete", "incomplete", "failed"]


class HunterReceipt(HunterDispatch, total=False):
    outside_assignment: list[str]


class CoverageLedger(TypedDict):
    version: Literal[2]
    status: Literal["available", "unavailable"]
    regions: list[CoveredRegion]
    uncovered: list[str]
    remaining_uncovered: list[str]
    omitted: list[OmittedRegion]
    hunters: list[HunterReceipt]
    budget_bytes: int
    assigned_bytes: int
    omitted_fact_packets: int


class TrimmedCoverageLedger(TypedDict):
    version: Literal[2]
    status: Literal["trimmed"]
    region_count: int
    uncovered_count: int
    omitted_count: int
    hunter_count: int
    hunters: list[HunterReceipt]
    omitted: list[OmittedRegion]
    budget_bytes: int
    assigned_bytes: int
    omitted_fact_packets: int
