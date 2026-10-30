"""Bounded, durable citation checks against captured repository evidence."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any, TypedDict, cast

from gauntlet.fs import write_atomic
from gauntlet.location import Quote, valid_quote
from gauntlet.registry import (
    ANCHOR_LIMITS,
    CITATION_REQUEST_SCHEMA,
    CITE_VERIFIER_VERSION,
    STAGE_COSTS,
    CitationRequest,
    CitationStatus,
    CiteResultsItem,
    CiteResultsItemRequest,
    CiteResultsItemSource,
    SkepticalStage,
)
from gauntlet.source_capture import (
    SourceAdapter,
    SourceCapture,
    load_capture,
    source_lines,
)


class CitationCheck(TypedDict):
    id: str
    finding_id: str
    stage: SkepticalStage
    request: object


class CiteBatch(TypedDict):
    requests: list[object]
    base_branch: str


class CiteRow(CiteResultsItem):
    id: str


class CiteResult(TypedDict):
    citations: list[CiteRow]


class CiteAttempt(TypedDict):
    nonce: str
    results: list[CiteRow] | None


class CiteLedger(TypedDict):
    identity: object
    attempts: list[CiteAttempt]


def citation_source(metadata: dict[str, object]) -> CiteResultsItemSource:
    identity = {"snapshot_id": metadata.get("manifest_sha256", "")}
    identity.update(
        (key, metadata.get(key, ""))
        for key in ("target_kind", "head", "base", "diff_sha256")
    )
    identity["verifier_version"] = CITE_VERIFIER_VERSION
    return cast(CiteResultsItemSource, identity)


def _request_error(
    record: CitationCheck,
    source: SourceAdapter,
    store: SourceCapture | None,
    snapshot: object,
) -> str:
    q = record.get("request")
    schema = cast(dict[str, Any], CITATION_REQUEST_SCHEMA)
    if not isinstance(q, dict) or set(q) != set(schema["required"]):
        return "malformed_request"
    for key, field in schema["properties"].items():
        value = q[key]
        if field["type"] == "integer":
            if type(value) is not int or not 1 <= value <= 2**53 - 1:
                return "unsafe_range"
        elif not isinstance(value, str) or not value.strip():
            return "malformed_request"
        if "enum" in field and value not in field["enum"]:
            return "malformed_request"
    if q["finding_id"] != record.get("finding_id") or q["stage"] != record.get("stage"):
        return "foreign_identity"
    if q["snapshot_id"] != snapshot:
        return "snapshot_mismatch"
    if store is not None and not store.admits(source, q["file"]):
        return "rejected_origin"
    lines = source_lines(q["quote"])
    if (
        len(q["quote"].encode("utf-16-le", errors="surrogatepass")) // 2
        > ANCHOR_LIMITS["chars"]
        or not valid_quote(Quote(lines))
        or q["line_end"] - q["line_start"] + 1 != len(lines)
    ):
        return "invalid_quote_or_range"
    return ""


def _identity_key(request: dict[str, object]) -> str:
    return json.dumps([request.get(k) for k in ("finding_id", "stage", "request_id")])


def _load_ledger(path: Path, identity: object, count: int) -> CiteLedger:
    ledger = json.loads(path.read_text(encoding="utf-8"))
    attempts = ledger["attempts"]
    if (
        ledger["identity"] != identity
        or not isinstance(attempts, list)
        or len(attempts) > STAGE_COSTS["cite"]["attempts"]
        or any(
            not isinstance(a, dict)
            or set(a) != {"nonce", "results"}
            or not isinstance(a["nonce"], str)
            or (
                a["results"] is not None
                and (not isinstance(a["results"], list) or len(a["results"]) != count)
            )
            for a in attempts
        )
    ):
        raise ValueError("corrupt_attempt_identity")
    return cast(CiteLedger, ledger)


def _classify_record(
    record: CitationCheck,
    source: SourceAdapter,
    store: SourceCapture | None,
    metadata: dict[str, object],
    ids: Counter[str],
    identities: Counter[str],
    cache: dict[tuple[object, ...], CitationStatus],
) -> tuple[CitationStatus, str]:
    error = _request_error(record, source, store, metadata.get("manifest_sha256"))
    q = record.get("request")
    if ids[record.get("id")] != 1 or (
        isinstance(q, dict) and identities[_identity_key(q)] != 1
    ):
        error = "duplicate_identity"
    if error:
        return "rejected", error
    if store is None:
        return "unavailable", "source_capture_unavailable"
    q = cast(CitationRequest, q)
    key = tuple(
        q[k] for k in ("snapshot_id", "file", "side", "line_start", "line_end", "quote")
    )
    if key not in cache:
        lines = source.read_captured_source(
            q["file"],
            side=q["side"],
            start=q["line_start"],
            end=q["line_end"],
            base=cast("str | None", metadata["base"]) if q["side"] == "old" else None,
            diff_sha256=cast("str | None", metadata["diff_sha256"]),
        )
        if lines is None:
            cache[key] = "unavailable"
        elif lines == source_lines(q["quote"]):
            cache[key] = "matched"
        else:
            cache[key] = "unmatched"
    return cache[key], "explicit_source_range"


def run_citations(
    args: argparse.Namespace, data: CiteBatch, root: str, sha: str
) -> CiteResult:
    """Reserve before reads; an interrupted command cannot renew its allowance."""
    try:
        metadata = json.loads(args.source_capture or "{}")
    except ValueError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    source_identity = citation_source(metadata)
    records: list[CitationCheck] = [
        cast(CitationCheck, r)
        if isinstance(r, dict)
        else {"id": "", "finding_id": "", "stage": "validator", "request": r}
        for r in data["requests"]
    ]

    def result(record: CitationCheck, status: CitationStatus, detail: str) -> CiteRow:
        return {
            "id": record.get("id", ""),
            "request": cast(CiteResultsItemRequest, record.get("request")),
            "status": status,
            "detail": detail,
            "source": source_identity,
        }

    def unavailable(
        detail: str, proven: dict[str, CiteRow] | None = None
    ) -> CiteResult:
        return {
            "citations": [
                (proven or {}).get(r.get("id", "")) or result(r, "unavailable", detail)
                for r in records
            ]
        }

    nonce = metadata.get("nonce")
    if (
        not isinstance(nonce, str)
        or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", nonce)
        or not args.capture_output_dir
        or not re.fullmatch(r"[0-9a-f]{8}", args.cite_batch or "")
        or args.cite_attempt not in range(1, STAGE_COSTS["cite"]["attempts"] + 1)
        or not args.nonce
    ):
        return unavailable("missing_attempt_identity")
    parent = Path(args.capture_output_dir) / f"code-gauntlet-inputs-{nonce}"
    if parent.is_symlink() or not parent.is_dir():
        return unavailable("missing_attempt_identity")
    directory = parent / ("cite-" + args.cite_batch)
    ledger_path = directory / "ledger.json"
    identity = {"batch": data, "source": metadata, "version": CITE_VERIFIER_VERSION}
    lock = directory / "lock"
    stale_lock = False
    try:
        if not directory.exists():
            directory.mkdir()
            write_atomic(
                ledger_path, json.dumps({"identity": identity, "attempts": []})
            )
        if directory.is_symlink() or ledger_path.is_symlink():
            return unavailable("corrupt_attempt_identity")
        with lock.open("x", encoding="utf-8", newline=""):
            pass
    except FileExistsError:
        stale_lock = True
    except (OSError, ValueError):
        return unavailable("uncertain_attempt_identity")
    try:
        try:
            ledger = _load_ledger(ledger_path, identity, len(records))
            terminal = {
                r["id"]: r
                for a in ledger["attempts"]
                for r in (a["results"] or [])
                if r["status"] != "unavailable"
            }
            ids = Counter(r.get("id") for r in records)
            identities = Counter(
                _identity_key(q)
                for r in records
                if isinstance(q := r.get("request"), dict)
            )
        except (OSError, ValueError, KeyError, TypeError):
            return unavailable("corrupt_attempt_identity")
        attempts = ledger["attempts"]
        for prior in attempts:
            if prior["nonce"] != args.nonce:
                continue
            if prior["results"] is None:
                return unavailable("uncertain_attempt_identity")
            return {"citations": prior["results"]}
        if stale_lock and all(r.get("id") in terminal for r in records):
            return {"citations": [terminal[r["id"]] for r in records]}
        # Without persisted attempts a leftover lock is the only crash evidence.
        if stale_lock and not attempts:
            attempts.append({"nonce": "stale_lock", "results": None})
        if len(attempts) >= STAGE_COSTS["cite"]["attempts"]:
            return unavailable("exhausted_attempts", terminal if stale_lock else None)
        attempts.append({"nonce": args.nonce, "results": None})
        try:
            write_atomic(ledger_path, json.dumps(ledger, ensure_ascii=True))
        except OSError:
            return unavailable("corrupt_attempt_identity")
        source = SourceAdapter(Path(root), args.target_kind, None)
        store = None
        with suppress(OSError, ValueError, TypeError):
            store = load_capture(
                source,
                Path(args.capture_output_dir),
                metadata,
                Path(args.diff_file).read_bytes(),
                requested_sha=sha,
            )
            source = store.adapter(Path(root))
        cache: dict[tuple[object, ...], CitationStatus] = {}
        rows: list[CiteRow] = []
        for record in records:
            if record.get("id") in terminal:
                rows.append(terminal[record["id"]])
                continue
            status, detail = _classify_record(
                record, source, store, metadata, ids, identities, cache
            )
            rows.append(result(record, status, detail))
        attempts[-1]["results"] = rows
        try:
            write_atomic(ledger_path, json.dumps(ledger, ensure_ascii=True))
        except OSError:
            return unavailable("corrupt_attempt_identity")
        return {"citations": rows}
    finally:
        with suppress(OSError):
            lock.unlink()
