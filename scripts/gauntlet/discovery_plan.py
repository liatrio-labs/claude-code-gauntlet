"""Map changed hunk packets to measured reads without copying source into argv."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from itertools import pairwise
from typing import Literal, TypedDict

from gauntlet.diff import AnnotatedDiff, split_diff_lines
from gauntlet.registry import READ_PLAN_LINE_PREFIX_BYTES


class ReadRange(TypedDict):
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
    fact_reads: list[ReadRange]
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


def changed_regions(
    annotated: AnnotatedDiff, pack: Callable[[str], list[int]]
) -> list[Region]:
    lines, _ = split_diff_lines(annotated.content.decode("utf-8", "replace"))
    starts = [
        i for i, line in enumerate(lines) if line.startswith(("@@ ", "diff --git "))
    ]
    starts.append(len(lines))
    regions: list[Region] = []
    for start, end in pairwise(starts):
        if not lines[start].startswith("@@ "):
            continue
        packet = "\n".join(lines[start:end]) + "\n"
        reads = read_ranges(packet, start + 1, pack)
        if not reads:
            raise RegionPlanError("changed packet cannot be measured")
        for read in reads:
            entries = [
                annotated.entries[n]
                for n in range(read["offset"], read["offset"] + read["limit"])
                if n in annotated.entries
            ]
            if not entries:
                continue
            head = [e["line"] for e in entries if e["side"] == "head"]
            # Deletion-only packets still need review, but old coordinates cannot claim head coverage.
            lo, hi = (min(head), max(head)) if head else (0, 0)
            file = entries[0]["file"]
            identity = hashlib.sha256(
                f"{file}:{read['offset']}:{read['limit']}".encode()
            ).hexdigest()[:16]
            regions.append(
                {
                    "id": identity,
                    "file": file,
                    "line_start": lo,
                    "line_end": hi,
                    "reads": [read],
                }
            )
    return regions


class CoveredRegion(TypedDict):
    id: str
    file: str
    line_start: int
    line_end: int
    cited_by: list[str]


class OmittedRegion(TypedDict):
    id: str
    reason: Literal["budget", "no_head_anchor", "scope"]


class HunterReceipt(TypedDict):
    agent: str
    assigned: list[str]
    status: Literal["complete", "incomplete", "failed"]


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
