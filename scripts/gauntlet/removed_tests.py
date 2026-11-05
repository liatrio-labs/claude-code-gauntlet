"""Inventory removed test text without depending on a source language."""

import hashlib
from dataclasses import dataclass
from typing import Literal, TypedDict

from gauntlet.diff import (
    DiffStructureError,
    HeaderEvent,
    HunkEvent,
    patch_report_policy,
    walk_diff,
)
from gauntlet.jsjson import fnv1a32, js_stringify_pretty
from gauntlet.paths import is_test_path


class RemovedTestLocation(TypedDict):
    id: str
    path: str
    old_start: int
    old_end: int


class RemovedTestItem(RemovedTestLocation):
    kind: Literal["deleted_file", "hunk"]


class RemovedTestsBody(TypedDict):
    status: Literal["available", "unavailable"]
    items: list[RemovedTestItem]
    omitted: int


class RemovedTestsReceipt(RemovedTestsBody):
    checksum: str


@dataclass(frozen=True, slots=True)
class _Removal:
    path: str
    start: int
    end: int
    deleted: bool


def removed_tests(diff_text: str) -> RemovedTestsReceipt:
    removals: list[_Removal] = []
    old_path = new_path = ""
    removed: list[int] = []
    git_prefixed = patch_report_policy(diff_text) == "git-prefixed"

    def flush() -> None:
        if removed and is_test_path(old_path):
            removals.append(
                _Removal(old_path, min(removed), max(removed), new_path == "/dev/null")
            )
        removed.clear()

    try:
        for event in walk_diff(diff_text, strict=True):
            if isinstance(event, HeaderEvent):
                if event.kind == "git_header":
                    flush()
                    old_path = new_path = ""
                elif event.kind == "old_path":
                    flush()
                    old_path = (
                        event.value.removeprefix("a/") if git_prefixed else event.value
                    )
                    new_path = ""
                else:
                    new_path = event.value
            elif isinstance(event, HunkEvent):
                flush()
                if not old_path or not new_path:
                    raise DiffStructureError("hunk without paired paths")
            elif event.old_line is not None and event.new_line is None:
                removed.append(event.old_line)
        flush()
    except DiffStructureError:
        body: RemovedTestsBody = {"status": "unavailable", "items": [], "omitted": 0}
    else:
        grouped: dict[tuple[str, int], _Removal] = {}
        for removal in removals:
            key = (removal.path, -1 if removal.deleted else removal.start)
            previous = grouped.get(key)
            grouped[key] = (
                _Removal(
                    removal.path,
                    min(previous.start, removal.start),
                    max(previous.end, removal.end),
                    True,
                )
                if previous is not None and removal.deleted
                else removal
            )
        items: list[RemovedTestItem] = []
        for removal in list(grouped.values())[:60]:
            identity = js_stringify_pretty([removal.path, removal.start, removal.end])
            items.append(
                {
                    "id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
                    "path": removal.path,
                    "old_start": removal.start,
                    "old_end": removal.end,
                    "kind": "deleted_file" if removal.deleted else "hunk",
                }
            )
        body = {
            "status": "available",
            "items": items,
            "omitted": max(0, len(grouped) - 60),
        }
    return {**body, "checksum": fnv1a32(js_stringify_pretty(body))}
