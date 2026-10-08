"""Verify persist plans and derive artifacts from the primaries on disk."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Collection, Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

from gauntlet.cli import Command, Parser
from gauntlet.fs import write_atomic
from gauntlet.jsjson import fnv1a32, js_stringify_pretty, normalize_content, utf16_len

PLAN_VERSION = 2
PLAN_CHECKSUM_KEY = "planChecksum"
ProofState = Literal["match", "mismatch"]


@dataclass(frozen=True, slots=True)
class PlanEntry:
    path: str
    text: str


class WrittenArtifact(TypedDict):
    path: object
    chars: int
    checksum: str


class VerifiedArtifact(TypedDict):
    path: object
    chars: int
    expected_chars: object
    checksum: str
    expected_checksum: object
    content_proof: ProofState


class AssembleReceipt(TypedDict):
    ok: bool
    planVersion: object
    planChecksum: str | None
    verified: list[VerifiedArtifact]
    written: list[WrittenArtifact]
    errors: list[str]


def _read_content(path: object) -> str:
    # Preserve internal CRLF and normalize only the BOM and one trailing newline.
    with open(cast("str | os.PathLike[str]", path), encoding="utf-8", newline="") as fh:
        return normalize_content(fh.read())


def _receipt(
    ok: bool,
    plan_version: object,
    plan_checksum: str | None,
    verified: list[VerifiedArtifact],
    written: list[WrittenArtifact],
    errors: list[str],
) -> AssembleReceipt:
    return {
        "ok": ok,
        "planVersion": plan_version,
        "planChecksum": plan_checksum,
        "verified": verified,
        "written": written,
        "errors": errors,
    }


# The plan selects delivered ids. Prove its entire instruction set, dropping only its proof key.
# Explicit id lists preserve the workflow's ranked, capped selection without re-deriving ranking here.
def plan_checksum(plan: Mapping[str, object]) -> str:
    body = dict((k, v) for (k, v) in plan.items() if k != PLAN_CHECKSUM_KEY)
    return fnv1a32(js_stringify_pretty(body))


# Cache failures too, so a shared invalid source contributes its error once.
def _load_source(
    path: object,
    cache: dict[Hashable, Mapping[str, Mapping[str, object]] | None],
    errors: list[str],
) -> Mapping[str, Mapping[str, object]] | None:
    if path in cache:
        return cache[path]
    cache[path] = None
    try:
        raw = _read_content(path)
    except Exception as exc:  # noqa: BLE001 - includes UnicodeDecodeError (a ValueError)
        errors.append(f"source not found or unreadable: {path} ({exc})")
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        errors.append(f"source is not valid JSON: {path} ({exc})")
        return None
    if not isinstance(data, list):
        errors.append(f"source must be a JSON array of findings: {path}")
        return None
    by_id: dict[str, Mapping[str, object]] = {}
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"source entry {index} is not an object: {path}")
            return None
        fid = item.get("id")
        if not isinstance(fid, str) or not fid:
            errors.append(f"source entry {index} has no usable string id: {path}")
            return None
        if fid in by_id:
            errors.append(f"duplicate id {fid!r} in source: {path}")
            return None
        by_id[fid] = item
    cache[path] = by_id
    return by_id


# Missing ids are structural disagreement between the plan and on-disk source.
def _project(
    by_id: Mapping[str, Mapping[str, object]],
    ids: Sequence[object],
    source_path: object,
    label: str,
    errors: list[str],
) -> list[Mapping[str, object]]:
    out = []
    for fid in ids:
        if cast(str, fid) not in by_id:
            errors.append(f"{label} id {fid!r} not present in source {source_path}")
            continue
        out.append(by_id[cast(str, fid)])
    return out


# Removing appended aliases preserves canonical key order.
def _strip_aliases(
    finding: Mapping[str, object], alias_fields: Collection[object]
) -> dict[str, object]:
    return dict((k, v) for (k, v) in finding.items() if k not in alias_fields)


def _serialize(document: object, label: str, errors: list[str]) -> str | None:
    try:
        return js_stringify_pretty(document)
    except Exception as exc:  # noqa: BLE001 - converted to a structural error
        errors.append(
            f"could not serialize the {label} artifact: {type(exc).__name__}: {exc}"
        )
        return None


def _assemble(plan_path: str | os.PathLike[str]) -> AssembleReceipt:
    errors: list[str] = []
    verified: list[VerifiedArtifact] = []

    try:
        plan_raw = _read_content(plan_path)
    except Exception as exc:  # noqa: BLE001 - converted to a structural error
        errors.append(f"plan not found or unreadable: {plan_path} ({exc})")
        return _receipt(False, None, None, verified, [], errors)
    try:
        plan = json.loads(plan_raw)
    except (ValueError, RecursionError) as exc:
        errors.append(f"plan is not valid JSON: {plan_path} ({exc})")
        return _receipt(False, None, None, verified, [], errors)
    if not isinstance(plan, dict):
        errors.append(f"plan must be a JSON object: {plan_path}")
        return _receipt(False, None, None, verified, [], errors)

    plan_version = plan.get("planVersion")
    if plan_version != PLAN_VERSION:
        errors.append(
            f"unsupported planVersion {plan_version!r} (expected {PLAN_VERSION})"
        )
        return _receipt(False, plan_version, None, verified, [], errors)

    # Instructions cannot execute without proof, unlike nonfatal primary differences.
    declared = plan.get(PLAN_CHECKSUM_KEY)
    if not isinstance(declared, str) or not declared:
        errors.append(
            f"plan carries no {PLAN_CHECKSUM_KEY} — an unproven instruction set is "
            f"not executed: {plan_path}"
        )
        return _receipt(False, plan_version, None, verified, [], errors)
    try:
        actual = plan_checksum(plan)
    except Exception as exc:  # noqa: BLE001 - converted to a structural error
        errors.append(
            f"plan checksum could not be recomputed: {type(exc).__name__}: {exc}"
        )
        return _receipt(False, plan_version, None, verified, [], errors)
    if actual != declared:
        errors.append(
            f"plan checksum mismatch: declared {declared}, recomputed {actual} — "
            "the persist plan "
            "changed in transit; it is the instruction set for which findings reach "
            "the post-review artifact, so it is NOT executed"
        )
        sys.stderr.write(
            f"plan checksum mismatch: declared {declared}, recomputed {actual}\n"
        )
        return _receipt(False, plan_version, actual, verified, [], errors)

    # Primary mismatch is nonfatal: derive from on-disk truth and disclose the difference.
    for entry in cast(Sequence[Mapping[str, object]], plan.get("expect", []) or []):
        path = entry.get("path")
        try:
            content = _read_content(path)
        except Exception as exc:  # noqa: BLE001 - converted to a structural error
            errors.append(f"expected artifact not found or unreadable: {path} ({exc})")
            continue
        if isinstance(path, str) and path.endswith(".json"):
            try:
                json.loads(content)
            except (ValueError, RecursionError) as exc:
                errors.append(f"expected artifact is not valid JSON: {path} ({exc})")
                continue
        chars = utf16_len(content)
        checksum = fnv1a32(content)
        matched = chars == entry.get("chars") and checksum == entry.get("checksum")
        verified.append(
            {
                "path": path,
                "chars": chars,
                "expected_chars": entry.get("chars"),
                "checksum": checksum,
                "expected_checksum": entry.get("checksum"),
                "content_proof": "match" if matched else "mismatch",
            }
        )
        if not matched:
            sys.stderr.write(
                f"content-proof mismatch: {path} (expected {entry.get('chars')} chars "
                f"/ {entry.get('checksum')}, got {chars} / {checksum})\n"
            )

    if errors:
        return _receipt(False, plan_version, actual, verified, [], errors)

    cache: dict[Hashable, Mapping[str, Mapping[str, object]] | None] = {}
    pending: list[PlanEntry] = []

    # Build both documents before any write, so structural failures leave outputs untouched.
    post = cast(Mapping[str, object], plan.get("postReview") or {})
    post_source = post.get("source")
    by_id = _load_source(post_source, cache, errors)
    if by_id is not None:
        projected = _project(
            by_id,
            cast(Sequence[object], post.get("ids") or []),
            post_source,
            "postReview",
            errors,
        )
        wrapper = post.get("wrapper")
        if wrapper is None:
            document: object = projected
        else:
            document = dict(cast(Mapping[str, object], wrapper))
            document["findings"] = projected
        text = _serialize(document, "post-review", errors)
        if text is not None:
            pending.append(PlanEntry(cast(str, post.get("path")), text))

    cp = cast(Mapping[str, object], plan.get("checkpoint") or {})
    cp_source = cp.get("source")
    cp_by_id = _load_source(cp_source, cache, errors)
    if cp_by_id is not None:
        alias_fields = set(cast(Sequence[Hashable], cp.get("stripAliasFields") or []))
        cp_ids = cast(Sequence[object], cp.get("challengeFindingIds") or [])
        cp_projected = [
            _strip_aliases(f, alias_fields)
            for f in _project(cp_by_id, cp_ids, cp_source, "challenge", errors)
        ]
        # Keep the JSON round-trip: deepcopy changes accepted NaN/depth behavior.
        skeleton: Mapping[str, object] = json.loads(
            json.dumps(cp.get("skeleton") or {})
        )
        challenge = cast(Mapping[str, object], skeleton.get("phases") or {}).get(
            "challenge"
        )
        # Mirror JS persistPlan: only replace an existing array, never fabricate findings.
        if isinstance(challenge, dict) and isinstance(challenge.get("findings"), list):
            # Assignment retains the existing key position for byte parity.
            challenge["findings"] = cp_projected
        elif cp_ids:
            errors.append(
                "checkpoint skeleton has no phases.challenge.findings array to receive "
                f"{len(cp_ids)} challenge finding(s)"
            )
        text = _serialize(skeleton, "checkpoint", errors)
        if text is not None:
            pending.append(PlanEntry(cast(str, cp.get("path")), text))

    if errors:
        return _receipt(False, plan_version, actual, verified, [], errors)

    written: list[WrittenArtifact] = []
    # Atomic replacements prevent truncation; individual failures retain partial accounting.
    for planned in pending:
        path, text = planned.path, planned.text
        try:
            write_atomic(path, text)
        except Exception as exc:  # noqa: BLE001 - converted to a structural error
            errors.append(f"could not write {path} ({type(exc).__name__}: {exc})")
            continue
        written.append(
            {
                "path": path,
                "chars": utf16_len(text),
                "checksum": fnv1a32(text),
            }
        )

    if errors:
        return _receipt(False, plan_version, actual, verified, written, errors)
    return _receipt(True, plan_version, actual, verified, written, errors)


# The library itself promises a receipt, including for unexpected failures.
def assemble(plan_path: str | os.PathLike[str]) -> AssembleReceipt:
    try:
        return _assemble(plan_path)
    except Exception as exc:  # noqa: BLE001 - the one-line-receipt contract
        return _receipt(
            False,
            None,
            None,
            [],
            [],
            [f"assembler failed unexpectedly: {type(exc).__name__}: {exc}"],
        )


def _fallback_receipt(exc: Exception) -> AssembleReceipt:
    return _receipt(
        False,
        None,
        None,
        [],
        [],
        [f"receipt could not be serialized: {type(exc).__name__}: {exc}"],
    )


def build_parser() -> Parser:
    parser = Parser(
        prog="assemble_artifacts",
        description="Derive the projected code-gauntlet artifacts from a persist plan.",
    )
    parser.add_argument("--plan", required=True, help="path to the persist plan JSON")
    return parser


def main(args: argparse.Namespace) -> tuple[AssembleReceipt, int]:
    receipt = assemble(args.plan)
    return receipt, 0 if receipt["ok"] else 1


CLI = Command(
    parser=build_parser(),
    main=main,
    ascii=False,
    fallback_receipt=_fallback_receipt,
    fallback_line=(
        '{"ok": false, "planVersion": null, "planChecksum": null, '
        '"verified": [], "written": [], '
        '"errors": ["receipt could not be serialized"]}'
    ),
)
