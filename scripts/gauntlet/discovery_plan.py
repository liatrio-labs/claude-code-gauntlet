"""Map changed hunk packets to measured reads without copying source into argv."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from itertools import pairwise
from typing import Literal, TypedDict

from gauntlet.diff import AnnotatedDiff


class ReadRange(TypedDict):
    offset: int
    limit: int


class Region(TypedDict):
    id: str
    file: str
    line_start: int
    line_end: int
    reads: list[ReadRange]


class DiscoveryPlan(TypedDict):
    version: Literal[1]
    status: Literal["available", "unavailable"]
    regions: list[Region]
    fact_reads: list[ReadRange]


def read_ranges(
    content: str, offset: int, pack: Callable[[str], list[int]]
) -> list[ReadRange]:
    reads: list[ReadRange] = []
    for count in pack(content):
        reads.append({"offset": offset, "limit": count})
        offset += count
    return reads


class RegionPlanError(ValueError):
    """A changed packet exceeds the measured-read allocation ceiling."""


def changed_regions(
    annotated: AnnotatedDiff, pack: Callable[[str], list[int]]
) -> list[Region]:
    lines = annotated.content.decode("utf-8", "replace").split("\n")
    starts = [
        i for i, line in enumerate(lines) if line.startswith(("@@ ", "diff --git "))
    ]
    starts.append(len(lines) - 1)
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
    read_by: list[str]
    cited_by: list[str]


class InvalidReceipt(TypedDict):
    agent: str
    ids: list[str]


class HunterReceipt(TypedDict):
    agent: str
    assigned: list[str]
    status: Literal["complete", "incomplete", "failed"]
    read_regions: list[str]


class CoverageLedger(TypedDict):
    version: Literal[1]
    status: Literal["available", "unavailable"]
    regions: list[CoveredRegion]
    uncovered: list[str]
    remaining_uncovered: list[str]
    invalid_receipts: list[InvalidReceipt]
    hunters: list[HunterReceipt]
