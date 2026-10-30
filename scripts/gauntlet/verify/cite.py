"""Bounded, durable citation checks against captured repository evidence."""

from __future__ import annotations

import argparse
import hashlib
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
    _local_bytes,
    _readable,
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
    record: CitationCheck, source: SourceAdapter, snapshot: object
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
    if source._parts(file) is None or "\\" in file or not _readable(source, file, None):
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
                not isinstance(a, dict) or set(a) != {"nonce", "results"}
                for a in attempts
            )
            or any(
                a["results"] is not None
                and (
                    not isinstance(a["results"], list)
                    or len(a["results"]) != len(records)
                    or any(
                        not isinstance(row, dict)
                        or row.get("id") != record.get("id")
                        or row.get("request") != record.get("request")
                        or row.get("source") != source_identity
                        or row.get("status")
                        not in ("matched", "unmatched", "rejected", "unavailable")
                        or not isinstance(row.get("detail"), str)
                        for row, record in zip(a["results"], records, strict=True)
                    )
                )
                for a in attempts
            )
        ):
            return unavailable("corrupt_attempt_identity")
        index = args.cite_attempt - 1
        if index < len(attempts):
            prior = attempts[index]
            if prior["nonce"] != args.nonce or prior["results"] is None:
                return unavailable("uncertain_attempt_identity")
            return {"citations": prior["results"]}
        if index != len(attempts):
            return unavailable("missing_attempt_identity")
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
        terminal = (
            {
                r["id"]: r
                for r in (attempts[0]["results"] or [])
                if r["status"] != "unavailable"
            }
            if index
            else {}
        )
        for record in records:
            if record.get("id") in terminal:
                rows.append(terminal[record["id"]])
                continue
            error = _request_error(record, source, metadata.get("manifest_sha256"))
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
                check_identity = {"source": source_identity, "check": key}
                check_text = json.dumps(check_identity, ensure_ascii=True)
                cache_name = (
                    "cite-check-"
                    + hashlib.sha256(check_text.encode()).hexdigest()
                    + ".json"
                )
                status: CitationStatus = "unavailable"
                try:
                    saved = json.loads(_local_bytes(parent, (cache_name,)))
                    if saved["identity"] == check_text and saved["status"] in (
                        "matched",
                        "unmatched",
                    ):
                        status = saved["status"]
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                if status == "unavailable":
                    lines = source.read_captured_source(
                        q["file"],
                        side=q["side"],
                        start=q["line_start"],
                        end=q["line_end"],
                        base=metadata["base"] if q["side"] == "old" else None,
                        diff_sha256=metadata["diff_sha256"],
                    )
                    status = (
                        "unavailable"
                        if lines is None
                        else "matched"
                        if lines == source_lines(q["quote"])
                        else "unmatched"
                    )
                    if status != "unavailable":
                        write_atomic(
                            parent / cache_name,
                            json.dumps({"identity": check_text, "status": status}),
                        )
                cache[key] = status
            rows.append(result(record, cache[key], "explicit_source_range"))
        attempts[index]["results"] = rows
        write_atomic(ledger_path, json.dumps(ledger, ensure_ascii=True))
        return {"citations": rows}
    except (OSError, ValueError, KeyError, TypeError):
        return unavailable("corrupt_attempt_identity")
    finally:
        lock.unlink()
