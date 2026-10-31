"""Inline slice decoding, proofs, envelopes and the verification command."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, NoReturn, TypedDict, cast

from gauntlet.cli import Command, Parser, UsageError
from gauntlet.fs import write_atomic
from gauntlet.jsjson import (
    JS_MAX_SAFE_INTEGER,
    JsSerializationError,
    fnv1a32,
    js_stringify_pretty,
)
from gauntlet.jsjson import (
    checksum_or_none as checksum_or_none,
)
from gauntlet.location import (
    SourceAdapter,
    TargetKind,
    anchor_candidates,
    canonical_range,
    valid_quote,
)
from gauntlet.registry import DELTA_VALUE_FIELDS, Delta
from gauntlet.source_capture import load_capture
from gauntlet.verify import decide
from gauntlet.verify.cite import CiteBatch, CiteResult, run_citations
from gauntlet.verify.decide import FindingWire, VerificationResult


class _SliceRequired(TypedDict):
    findings: list[object]


class SliceInput(_SliceRequired, total=False):
    base_branch: object


class RecordProof(TypedDict):
    id: str
    checksum: str | None


class DiffSource(TypedDict):
    path: str | None
    read: bool


class _ReceiptRequired(TypedDict):
    sha: str
    n_in: int
    nonce: str | None
    deltas_checksum: str | None
    inline_checksum: str
    base_branch: str
    diff_source: DiffSource
    record_proofs: list[RecordProof]


class Receipt(_ReceiptRequired, total=False):
    input_checksum: str


class ReceiptResult(VerificationResult):
    deltas: list[Delta]


class SuccessEnvelope(TypedDict):
    status: Literal["ok"]
    receipt: Receipt
    result: ReceiptResult


class FailedEnvelope(TypedDict):
    status: Literal["failed"]
    exitCode: int
    stderr: str


class CitationReceipt(Receipt):
    batch_id: str | None
    attempt: int | None


class CiteSuccessEnvelope(TypedDict):
    status: Literal["ok"]
    receipt: CitationReceipt
    result: CiteResult


Envelope = SuccessEnvelope | CiteSuccessEnvelope | FailedEnvelope
# Half-up rounding mirrors the JS side's pinNumericFields.
_NUMERIC_FIELDS = ("line_start", "line_end", "line", "end_line", "confidence")
_INT_RE = re.compile(r"[+-]?\d+")
_INLINE_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,:/_-"
)
_INLINE_STRING_RE = re.compile(r"(?:[A-Za-z0-9 .,:/_-]|%[0-9A-F]{2}|%u[0-9A-F]{4})*")


def _half_up_int(value: float) -> int | None:
    """Use finite half-up rounding so input arithmetic and delta proofs agree."""
    if not math.isfinite(value):
        return None
    # Not round(): Python's round() is half-to-even.
    return math.floor(value + 0.5)


def coerce_numeric_fields(finding: object) -> object:
    """Normalize numeric fields before arithmetic, preserving unknown keys."""
    if not isinstance(finding, dict):
        return finding
    finding = cast(dict[str, object], finding)
    for key in _NUMERIC_FIELDS:
        value = finding.get(key)
        if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
            finding[key] = int(value.strip())
            continue
        if isinstance(value, bool) or not isinstance(value, float):
            continue
        rounded = _half_up_int(value)
        if rounded is None:
            continue  # NaN/inf: decide's range guards handle uncoerced values.
        finding[key] = rounded
    return finding


def _inline_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        duplicate = next(key for key in keys if keys.count(key) > 1)
        raise ValueError(f"duplicate object key {duplicate!r}")
    return dict(pairs)


def _inline_reject(message: str) -> NoReturn:
    raise ValueError(f"inline slice-input rejected: {message}")


def _decode_inline_string(value: str, path: str) -> str:
    if not _INLINE_STRING_RE.fullmatch(value):
        for char in value:
            if char not in _INLINE_SAFE and char != "%":
                _inline_reject(f"raw U+{ord(char):04X} at {path}")
        _inline_reject(f"invalid percent escape at {path}")

    out = []
    i = 0
    while i < len(value):
        char = value[i]
        if char in _INLINE_SAFE:
            out.append(char)
            i += 1
            continue
        if value[i + 1] == "u":
            unit = int(value[i + 2 : i + 6], 16)
            if not 0xD800 <= unit <= 0xDFFF:
                _inline_reject(
                    f"invalid %u escape U+{unit:04X} at {path} "
                    "(only surrogates are allowed)"
                )
            if (
                0xD800 <= unit <= 0xDBFF
                and value[i + 6 : i + 8] == "%u"
                and 0xDC00 <= int(value[i + 8 : i + 12], 16) <= 0xDFFF
            ):
                _inline_reject(
                    f"non-canonical surrogate pair {value[i : i + 12]} at {path} "
                    "(an astral character is spelled as its UTF-8 bytes)"
                )
            out.append(chr(unit))
            i += 6
            continue

        bytes_run = bytearray()
        while i < len(value) and value[i] == "%" and value[i + 1] != "u":
            pair = value[i + 1 : i + 3]
            byte = int(pair, 16)
            if chr(byte) in _INLINE_SAFE:
                _inline_reject(
                    f"non-canonical percent escape %{pair} at {path} "
                    "(byte is SAFE ASCII)"
                )
            bytes_run.append(byte)
            i += 3
        try:
            out.append(bytes(bytes_run).decode("utf-8", "strict"))
        except UnicodeDecodeError as exc:
            _inline_reject(
                f"invalid UTF-8 at {path} "
                f"(offending bytes {bytes_run.hex().upper()}, {exc})"
            )
    return "".join(out)


def _decode_inline_node(node: object, path: str = "$") -> object:
    if isinstance(node, dict):
        # Canonical escapes are injective as JS strings; _inline_pairs already
        # rejects raw duplicates, and paired surrogate escapes are non-canonical.
        decoded = {}
        for raw_key, raw_value in cast(dict[str, object], node).items():
            key = _decode_inline_string(raw_key, f"{path}.<key>")
            decoded[key] = _decode_inline_node(raw_value, f"{path}.{key}")
        return decoded
    if isinstance(node, list):
        return [
            _decode_inline_node(value, f"{path}[{index}]")
            for index, value in enumerate(cast(list[object], node))
        ]
    if isinstance(node, str):
        return _decode_inline_string(node, path)
    return node


def _reject_non_finite_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant {value}")


def decode_inline_slice(text: str) -> dict[str, object]:
    """Strictly decode the percent-encoded JSON document carried by the executor."""
    if "\\" in text:
        _inline_reject(
            "JSON escape sequences are not canonical at $ (offending U+005C)"
        )

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_inline_pairs,
            parse_constant=_reject_non_finite_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        _inline_reject(f"invalid JSON at $ ({exc})")
    if not isinstance(parsed, dict):
        _inline_reject("root must be an object at $")
    return cast(dict[str, object], _decode_inline_node(parsed))


def validate_input_shape(data: dict[str, object]) -> SliceInput:
    if "findings" not in data:
        _inline_reject("missing required 'findings' array at $")
    if not isinstance(data["findings"], list):
        _inline_reject("'findings' must be an array at $")
    return cast(SliceInput, data)


def write_output(envelope: Envelope, output_path: str | None) -> None:
    # ensure_ascii keeps lone surrogates accepted by the inline decoder writable.
    text = json.dumps(envelope, indent=2, ensure_ascii=True)
    if output_path:
        with Path(output_path).open("w", encoding="utf-8", newline="") as stream:
            stream.write(text)
    else:
        print(text)


def _delta_confidence(value: object) -> int | None:
    """Omit confidences without a canonical safe integer spelling."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        rounded = _half_up_int(value)
        if rounded is None:
            return None
        value = rounded
    return int(value) if abs(value) <= JS_MAX_SAFE_INTEGER else None


def _record_id(finding: object) -> str | None:
    if isinstance(finding, dict):
        fid = finding.get("id")
        if isinstance(fid, str) and fid.strip():
            return fid
    return None


def build_deltas(findings: Sequence[object], verified: Sequence[object]) -> list[Delta]:
    """Echo decisions in received order, using object identity for kept membership."""
    kept = {id(f) for f in verified}
    deltas: list[Delta] = []
    for finding in findings:
        fid = _record_id(finding)
        if fid is None:
            continue
        finding = cast(dict[str, object], finding)
        span = canonical_range(finding)
        if finding.get("location_status") == "unconfirmed":
            if "line_start" in finding or "line_end" in finding:
                raise ValueError("unconfirmed delta carries coordinates")
        elif (
            span is None
            or any(q.explicit_old for q in anchor_candidates(finding.get("anchor")))
            or not any(
                valid_quote(quote)
                and not quote.explicit_old
                and len(quote.lines) == span[1] - span[0] + 1
                for quote in anchor_candidates(finding.get("anchor"))
            )
        ):
            raise ValueError("resolved delta lacks a valid anchor span")
        delta: dict[str, object] = {"id": fid, "verified": id(finding) in kept}
        for key in DELTA_VALUE_FIELDS:
            if key == "anchor" and not finding.get("derived_loss_anchor"):
                continue
            value = finding.get(key)
            if value is None:
                continue
            if key == "diff_validation":
                value = {"in_diff": cast(dict[str, object], value)["in_diff"]}
            if key == "confidence":
                value = _delta_confidence(value)
                if value is None:
                    continue
            delta[key] = value
        deltas.append(cast(Delta, delta))
    return deltas


def _receipt(
    args: argparse.Namespace, sha: str, data: SliceInput, records: list[object]
) -> Receipt:
    receipt: Receipt = {
        "sha": sha,
        "n_in": len(records),
        "nonce": args.nonce,
        "deltas_checksum": None,
        "inline_checksum": fnv1a32(args.input_inline),
        "base_branch": cast(str, data.get("base_branch") or args.base_branch),
        "diff_source": {"path": args.diff_file, "read": False},
        "record_proofs": [
            {"id": fid, "checksum": checksum_or_none(record)}
            for record in records
            if (fid := _record_id(record)) is not None
        ],
    }
    # Both runtimes omit an input proof when the received value cannot be hashed.
    if (checksum := checksum_or_none(data)) is not None:
        receipt["input_checksum"] = checksum
    return receipt


def _cite_envelope(
    args: argparse.Namespace, data: CiteBatch, root: str, receipt: Receipt
) -> CiteSuccessEnvelope:
    result = run_citations(args, data, root, receipt["sha"])
    receipt["deltas_checksum"] = checksum_or_none(result["citations"])
    receipt["diff_source"]["read"] = bool(
        args.diff_file and Path(args.diff_file).is_file()
    )
    cite_receipt = cast(CitationReceipt, receipt)
    cite_receipt["batch_id"] = args.cite_batch
    cite_receipt["attempt"] = args.cite_attempt
    return {"status": "ok", "receipt": cite_receipt, "result": result}


def run_receipt(args: argparse.Namespace) -> Envelope:
    repo_root = decide.resolve_repo_root()
    sha = args.head_sha or decide.resolve_head_sha() or ""
    try:
        decoded = decode_inline_slice(args.input_inline)
        cite_mode = args.mode == "cite"
        if cite_mode:
            if not isinstance(decoded.get("requests"), list):
                _inline_reject("cite mode requires request records")
        else:
            validate_input_shape(decoded)
        data = cast(SliceInput, decoded)
        records = (
            cast(list[object], decoded["requests"]) if cite_mode else data["findings"]
        )
        # Snapshot received values before coercion or decisions can rewrite them.
        receipt = _receipt(args, sha, data, records)
        try:
            input_text = js_stringify_pretty(data)
        except JsSerializationError:
            input_text = json.dumps(data, indent=2, ensure_ascii=True)
        write_atomic(args.input, input_text)
        if cite_mode:
            return _cite_envelope(args, cast(CiteBatch, decoded), repo_root, receipt)
        for finding in data["findings"]:
            coerce_numeric_fields(finding)
        findings = cast(list[FindingWire], data["findings"])
        context = decide.VerifyContext(
            repo_root,
            cast(str, data.get("base_branch") or args.base_branch),
            target_kind=cast(TargetKind, args.target_kind),
            requested_sha=sha,
            context_map=args.context_map,
            context_file=args.context_file,
        )
        if args.source_capture is not None:
            metadata = json.loads(args.source_capture)
            store = load_capture(
                SourceAdapter(Path(repo_root), context.target_kind, None),
                Path(args.capture_output_dir),
                metadata,
                Path(args.diff_file).read_bytes(),
                requested_sha=sha,
            )
            context.source = store.adapter(Path(repo_root))
        result = decide.run_verification(findings, context, args.diff_file)
        deltas = build_deltas(findings, result["verified"])
        receipt["deltas_checksum"] = checksum_or_none(deltas)
        receipt["diff_source"]["read"] = context.diff_read
        # Deltas precede the arrays so a capped executor Read preserves the echo.
        return {
            "status": "ok",
            "receipt": receipt,
            "result": {"deltas": deltas, **result},
        }
    except Exception as exc:  # noqa: BLE001 - honest failure is the contract
        return {"status": "failed", "exitCode": 1, "stderr": str(exc)}


def _parser() -> Parser:
    parser = Parser(
        prog="verify_findings",
        description=(
            "Deterministic finding verification for code-gauntlet Phase 4. "
            "Takes Phase 3 agent findings JSON, classifies new vs. surfaced via "
            "git blame, verifies factual accuracy against file content, validates "
            "line references against the diff."
        ),
    )
    parser.add_argument("--mode", choices=("verify", "cite"), default="verify")
    parser.add_argument("--cite-batch")
    parser.add_argument("--cite-attempt", type=int)
    parser.add_argument(
        "--base-branch",
        default="main",
        metavar="BRANCH",
        help=(
            "Base branch for blame comparison. "
            "Default: %(default)s. "
            "Override with the PR base branch name (e.g. 'develop')."
        ),
    )
    parser.add_argument(
        "--diff-file",
        metavar="PATH",
        help=(
            "Path to a pre-fetched unified diff file. "
            "If omitted, the script runs 'git diff <base-branch>...HEAD'."
        ),
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="Write output JSON to this file. If omitted, output goes to stdout.",
    )
    parser.add_argument(
        "--input",
        metavar="PATH",
        help="Receipt mode: code-written destination for the inline slice document.",
    )
    parser.add_argument(
        "--input-inline",
        metavar="TEXT",
        help=(
            "Receipt mode: the strict percent-encoded slice document "
            "to decode and write."
        ),
    )
    parser.add_argument(
        "--nonce",
        metavar="STR",
        help=(
            "Receipt mode: opaque nonce echoed back verbatim in the receipt so "
            "the workflow can confirm this output answers its dispatch."
        ),
    )
    parser.add_argument(
        "--head-sha",
        metavar="SHA",
        help=(
            "Receipt mode: head sha echoed into the receipt. "
            "Falls back to 'git rev-parse --short HEAD' when omitted."
        ),
    )
    parser.add_argument(
        "--target-kind",
        choices=("pr", "mr", "branch", "local"),
        required=True,
        help="Reviewed target selects captured HEAD blobs or local worktree source.",
    )
    parser.add_argument(
        "--context-map", metavar="PATH", help="Code-owned context line map."
    )
    parser.add_argument(
        "--source-capture", help="Complete preparation command receipt as JSON."
    )
    parser.add_argument(
        "--capture-output-dir", help="Run output area containing the source capture."
    )
    parser.add_argument(
        "--context-file",
        metavar="PATH",
        help="Framed context whose digest binds the map.",
    )
    return parser


def main(args: argparse.Namespace) -> int:
    if args.input_inline is not None and args.input is None:
        raise UsageError("--input-inline requires --input for receipt mode", 2)
    if args.input is not None and args.input_inline is None:
        raise UsageError("--input requires --input-inline for receipt mode", 2)
    if args.input is None:
        raise UsageError("--input and --input-inline are required for receipt mode", 2)
    write_output(run_receipt(args), args.output)
    return 0


CLI = Command(parser=_parser(), main=main)
