"""Persist returned primaries verbatim and grade their derived artifacts."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict, cast

from gauntlet import tasks
from gauntlet.artifacts import AssembleReceipt, PlanEntry, WrittenArtifact, assemble
from gauntlet.cli import Command, Parser, UsageError
from gauntlet.fs import confined, write_atomic
from gauntlet.jsjson import fnv1a32, utf16_len

# Only this persist channel carries primary text rather than a writer plan.
CHANNEL = "return"


@dataclass(frozen=True, slots=True)
class SourceSelection:
    path: str | None
    payload: Mapping[str, object] | None
    scanned: int


class MaterializeReceipt(TypedDict):
    ok: bool
    channel: str
    source: str | None
    scanned: int
    materialized: list[WrittenArtifact]
    assemble: AssembleReceipt | None
    gaps: list[str]
    errors: list[str]


def persist_return_of(path: str) -> Mapping[str, object] | None:
    terminal, _saw_bare_ok, _stop_reason = tasks.find_terminal(tasks.read_task(path))
    if not isinstance(terminal, dict):
        return None
    payload = terminal.get("persistReturn")
    if not isinstance(payload, dict) or payload.get("channel") != CHANNEL:
        return None
    return payload


# Named paths cost one candidate; unresolved ids cost none. A supplied nonce must match.
def select_source(
    task: str | None, nonce: str | None, roots: tasks.TaskRoots
) -> SourceSelection:
    scanned = 0
    candidates = []
    if task:
        resolved, _searched = tasks.resolve_target(task, roots)
        if resolved:
            candidates.append(resolved)
    if nonce:
        for path in tasks.sweep_paths(roots):
            if path not in candidates:
                candidates.append(path)
    for path in candidates:
        scanned += 1
        payload = persist_return_of(path)
        if payload is None:
            continue
        if nonce and payload.get("nonce") != nonce:
            continue
        return SourceSelection(path, payload, scanned)
    return SourceSelection(None, None, scanned)


# Validate the whole payload before writing anything; malformed later entries cannot be ignored.
def plan_entries(
    payload: Mapping[str, object], output_root: str, errors: list[str]
) -> tuple[list[PlanEntry] | None, str | None]:
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        errors.append("persistReturn carries no entries to write")
        return None, None
    checked = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"entry {index} is not an object")
            return None, None
        path = entry.get("path")
        text = entry.get("text")
        if not isinstance(path, str) or not path:
            errors.append(f"entry {index} has no usable string path")
            return None, None
        if not isinstance(text, str):
            errors.append(f"entry {index} ({path}) carries no string text")
            return None, None
        if not confined(path, output_root):
            errors.append(
                f"entry {index} writes outside the output directory: {path} is not "
                f"inside {output_root}"
            )
            return None, None
        checked.append(PlanEntry(path, text))
    plan_path = payload.get("planPath")
    if not isinstance(plan_path, str) or not plan_path:
        errors.append("persistReturn names no persist plan to derive from")
        return None, None
    if plan_path not in [entry.path for entry in checked]:
        errors.append(
            f"the named persist plan {plan_path} is not among the entries this "
            "payload carries"
        )
        return None, None
    return checked, plan_path


def write_entries(
    entries: Sequence[PlanEntry], materialized: list[WrittenArtifact], errors: list[str]
) -> bool:
    ok = True
    for entry in entries:
        path, text = cast(str, entry.path), entry.text
        try:
            write_atomic(path, text)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            errors.append(f"could not write {path} ({type(exc).__name__}: {exc})")
            ok = False
            continue
        materialized.append(
            {"path": path, "chars": utf16_len(text), "checksum": fnv1a32(text)}
        )
    return ok


# Use the assembler numbers; derive expectations detect serializer divergence or stale plans.
def proof_gaps(receipt: Mapping[str, object], plan_text: str) -> list[str]:
    gaps = []
    for entry in cast(Sequence[object], receipt.get("verified") or []):
        if not isinstance(entry, dict) or entry.get("content_proof") == "match":
            continue
        gaps.append(
            f"artifact-content-proof: {entry.get('path')} on disk differs from the "
            "bytes the workflow returned "
            f"(expected {entry.get('expected_chars')} chars/"
            f"{entry.get('expected_checksum')}, got {entry.get('chars')}/"
            f"{entry.get('checksum')})"
        )
    try:
        expected = {
            cast(Hashable, item.get("path")): item
            for item in (json.loads(plan_text).get("derive") or [])
            if isinstance(item, dict)
        }
    except ValueError:
        return [*gaps, "the persist plan just written is not valid JSON"]
    for entry in cast(Sequence[object], receipt.get("written") or []):
        if not isinstance(entry, dict):
            continue
        want = expected.get(cast(Hashable, entry.get("path")))
        if want is None:
            gaps.append(
                "artifact-content-proof: the persist plan carries no derived-content "
                f"expectation for {entry.get('path')} (no content proof)"
            )
            continue
        if entry.get("chars") == want.get("chars") and entry.get(
            "checksum"
        ) == want.get("checksum"):
            continue
        gaps.append(
            f"artifact-content-proof: derived document {entry.get('path')} does not "
            "match the pipeline's own derivation "
            f"(wrote {entry.get('chars')} chars/{entry.get('checksum')}, the pipeline "
            f"derived {want.get('chars')}/{want.get('checksum')})"
        )
    return gaps


def _receipt(
    ok: bool,
    source: str | None,
    scanned: int,
    materialized: list[WrittenArtifact],
    assemble_receipt: AssembleReceipt | None,
    gaps: list[str],
    errors: list[str],
) -> MaterializeReceipt:
    return {
        "ok": ok,
        "channel": CHANNEL,
        "source": source,
        "scanned": scanned,
        "materialized": materialized,
        "assemble": assemble_receipt,
        "gaps": gaps,
        "errors": errors,
    }


def _materialize(
    task: str | None, nonce: str | None, output_dir: str, roots: tasks.TaskRoots
) -> MaterializeReceipt:
    errors: list[str] = []
    gaps: list[str] = []
    materialized: list[WrittenArtifact] = []
    output_root = os.path.realpath(output_dir)

    selection = select_source(task, nonce, roots)
    source, payload, scanned = selection.path, selection.payload, selection.scanned
    if payload is None:
        errors.append(
            "no task output file carrying this run's returned artifacts was found "
            f"(looked at {scanned} candidate file(s) for target {task!r} / nonce "
            f"{nonce!r})"
        )
        return _receipt(False, source, scanned, materialized, None, gaps, errors)

    entries, plan_path = plan_entries(payload, output_root, errors)
    if entries is None:
        return _receipt(False, source, scanned, materialized, None, gaps, errors)

    # Skip derivation after a partial write: its source could belong to an earlier run.
    if not write_entries(entries, materialized, errors):
        return _receipt(False, source, scanned, materialized, None, gaps, errors)

    # plan_entries proved that plan_path is a string present among the validated entries.
    receipt = assemble(cast(str, plan_path))
    plan_text = {entry.path: entry.text for entry in entries}.get(plan_path, "")
    gaps.extend(proof_gaps(receipt, plan_text))
    if not receipt.get("ok"):
        errors.extend(
            receipt.get("errors") or ["the assembler refused without a reason"]
        )
        return _receipt(False, source, scanned, materialized, receipt, gaps, errors)
    return _receipt(not gaps, source, scanned, materialized, receipt, gaps, errors)


# Keep the receipt guarantee at the library boundary for every caller.
def materialize(
    task: str | None, nonce: str | None, output_dir: str, roots: tasks.TaskRoots
) -> MaterializeReceipt:
    try:
        return _materialize(task, nonce, output_dir, roots)
    except Exception as exc:  # noqa: BLE001 - the one-line-receipt contract
        return _receipt(
            False,
            None,
            0,
            [],
            None,
            [],
            [f"materializer failed unexpectedly: {type(exc).__name__}: {exc}"],
        )


def _fallback_receipt(exc: Exception) -> MaterializeReceipt:
    return _receipt(
        False,
        None,
        0,
        [],
        None,
        [],
        [f"receipt could not be serialized: {type(exc).__name__}: {exc}"],
    )


def build_parser() -> Parser:
    parser = Parser(
        prog="materialize_artifacts",
        description="Write the review's artifacts from the workflow's own return value.",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        required=True,
        metavar="DIR",
        help="The review's output directory. Every entry must resolve inside it.",
    )
    parser.add_argument(
        "--task",
        metavar="TASK_ID_OR_PATH",
        help="The Task ID printed by the Workflow tool, or the task output file's "
        "path (resolved exactly as gauntlet.tasks resolves it).",
    )
    parser.add_argument(
        "--nonce",
        metavar="NONCE",
        help="args.nonce for this run. Finds the file by content when no task id "
        "is in hand, and is REQUIRED to match when both are given.",
    )
    return parser


def main(args: argparse.Namespace) -> tuple[MaterializeReceipt, int]:
    if not args.task and not args.nonce:
        raise UsageError(
            "give --task, --nonce, or both \u2014 there is nothing to resolve", 2
        )
    roots = tasks.roots_from_environment(os.environ)
    receipt = materialize(args.task, args.nonce, args.output_dir, roots)
    return receipt, 0 if receipt["ok"] else 1


CLI = Command(
    parser=build_parser(),
    main=main,
    ascii=False,
    fallback_receipt=_fallback_receipt,
    fallback_line=(
        '{"ok": false, "channel": "return", "source": null, "scanned": 0, '
        '"materialized": [], "assemble": null, "gaps": [], '
        '"errors": ["receipt could not be serialized"]}'
    ),
)
