"""Bounded, durable citation checks against captured repository evidence."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
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
    return cast(
        CiteResultsItemSource,
        {
            "snapshot_id": metadata.get("manifest_sha256", ""),
            **{
                key: metadata.get(key, "")
                for key in ("target_kind", "head", "base", "diff_sha256")
            },
            "verifier_version": CITE_VERIFIER_VERSION,
        },
    )


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
    file = q["file"]
    if store is not None and not store.admits(source, file):
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
        return cast(
            CiteRow,
            {
                "id": record.get("id", ""),
                "request": record.get("request"),
                "status": status,
                "detail": detail,
                "source": source_identity,
            },
        )

    def unavailable(detail: str) -> CiteResult:
        return {"citations": [result(r, "unavailable", detail) for r in records]}

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
        ledger: CiteLedger = json.loads(ledger_path.read_text(encoding="utf-8"))
        attempts = ledger["attempts"]
        if (
            ledger["identity"] != identity
            or not isinstance(attempts, list)
            or len(attempts) > STAGE_COSTS["cite"]["attempts"]
            or any(
                not isinstance(a, dict)
                or set(a) != {"nonce", "results"}
                or not isinstance(a["nonce"], str)
                for a in attempts
            )
            or any(
                a["results"] is not None
                and (
                    not isinstance(a["results"], list)
                    or len(a["results"]) != len(records)
                )
                for a in attempts
            )
        ):
            return unavailable("corrupt_attempt_identity")
        for prior in attempts:
            if prior["nonce"] != args.nonce:
                continue
            if prior["results"] is None:
                return unavailable("uncertain_attempt_identity")
            return {"citations": prior["results"]}
        # A lock left before reservation still consumes the interrupted command.
        if (
            stale_lock
            and not any(a["results"] is None for a in attempts)
            and len(attempts) < STAGE_COSTS["cite"]["attempts"]
        ):
            attempts.append({"nonce": "stale_lock", "results": None})
            write_atomic(ledger_path, json.dumps(ledger, ensure_ascii=True))
        if len(attempts) >= STAGE_COSTS["cite"]["attempts"]:
            return unavailable("exhausted_attempts")
        attempts.append({"nonce": args.nonce, "results": None})
        write_atomic(ledger_path, json.dumps(ledger, ensure_ascii=True))
        source = SourceAdapter(Path(root), "local", None)
        store = None
        try:
            store = load_capture(
                SourceAdapter(Path(root), args.target_kind, None),
                Path(args.capture_output_dir),
                metadata,
                Path(args.diff_file).read_bytes(),
                requested_sha=sha,
            )
            source = store.adapter(Path(root))
        except (OSError, ValueError, TypeError):
            pass
        ids = Counter(r.get("id") for r in records)
        identities = Counter(
            json.dumps([q.get("finding_id"), q.get("stage"), q.get("request_id")])
            for r in records
            if isinstance(q := r.get("request"), dict)
        )
        cache: dict[tuple[object, ...], CitationStatus] = {}
        rows: list[CiteRow] = []
        terminal = {
            r["id"]: r
            for a in attempts
            for r in (a["results"] or [])
            if r["status"] != "unavailable"
        }
        for record in records:
            if record.get("id") in terminal:
                rows.append(terminal[record["id"]])
                continue
            error = _request_error(
                record, source, store, metadata.get("manifest_sha256")
            )
            q = record.get("request")
            if ids[record.get("id")] != 1 or (
                isinstance(q, dict)
                and identities[
                    json.dumps(
                        [q.get("finding_id"), q.get("stage"), q.get("request_id")]
                    )
                ]
                != 1
            ):
                error = "duplicate_identity"
            if error:
                rows.append(result(record, "rejected", error))
                continue
            if store is None:
                rows.append(result(record, "unavailable", "source_capture_unavailable"))
                continue
            q = cast(CitationRequest, q)
            key = (
                q["snapshot_id"],
                q["file"],
                q["side"],
                q["line_start"],
                q["line_end"],
                q["quote"],
            )
            if key not in cache:
                lines = source.read_captured_source(
                    q["file"],
                    side=q["side"],
                    start=q["line_start"],
                    end=q["line_end"],
                    base=metadata["base"] if q["side"] == "old" else None,
                    diff_sha256=metadata["diff_sha256"],
                )
                cache[key] = (
                    "unavailable"
                    if lines is None
                    else "matched"
                    if lines == source_lines(q["quote"])
                    else "unmatched"
                )
            rows.append(result(record, cache[key], "explicit_source_range"))
        attempts[-1]["results"] = rows
        write_atomic(ledger_path, json.dumps(ledger, ensure_ascii=True))
        return {"citations": rows}
    except (OSError, ValueError, KeyError, TypeError):
        return unavailable("corrupt_attempt_identity")
    finally:
        lock.unlink()
