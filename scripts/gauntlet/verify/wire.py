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
from gauntlet.registry import DELTA_VALUE_FIELDS, Delta
from gauntlet.verify import decide
from gauntlet.verify.decide import FindingWire, VerificationResult


class _SliceRequired(TypedDict):
    findings: list[object]


class SliceInput(_SliceRequired, total=False):
    base_branch: object


class _ReceiptRequired(TypedDict):
    sha: str
    n_in: int
    nonce: str | None
    deltas_checksum: str | None
    inline_checksum: str


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


Envelope = SuccessEnvelope | FailedEnvelope
# A quoted number would raise TypeError and degrade the whole slice.
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
    # Explicit half-up, not round(): Python's round() is half-to-even, and the
    # spelling of this decision should not depend on which runtime reads the code.
    return math.floor(value + 0.5)


def coerce_numeric_fields(finding: object) -> object:
    """Normalize numeric fields before arithmetic, preserving unknown keys and identity."""
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
                    f"invalid %u escape U+{unit:04X} at {path} (only surrogates are allowed)"
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
                    f"non-canonical percent escape %{pair} at {path} (byte is SAFE ASCII)"
                )
            bytes_run.append(byte)
            i += 3
        try:
            out.append(bytes(bytes_run).decode("utf-8", "strict"))
        except UnicodeDecodeError as exc:
            _inline_reject(
                f"invalid UTF-8 at {path} (offending bytes {bytes_run.hex().upper()}, {exc})"
            )
    return "".join(out)


def _decode_inline_node(node: object, path: str = "$") -> object:
    if isinstance(node, dict):
        # No decoded-duplicate check here: the accepted spelling alphabet is injective as JS
        # values, so two distinct raw keys cannot decode to the same JS string. A safe byte is
        # spelled only as itself (a %XX escape of one is rejected as non-canonical), every
        # other UTF-8 byte only as canonical uppercase %XX, a lone surrogate only as %uXXXX,
        # and an astral character only as its UTF-8 bytes. Adjacent escaped high/low surrogate
        # pairs are rejected by name. Raw duplicates are rejected earlier, by _inline_pairs.
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
    if abs(value) > JS_MAX_SAFE_INTEGER:
        return None
    return int(value)


def build_deltas(findings: Sequence[object], verified: Sequence[object]) -> list[Delta]:
    """Echo decisions in dispatch order, using object identity for kept membership."""
    kept = {id(f) for f in verified}
    deltas: list[Delta] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        finding = cast(dict[str, object], finding)
        fid = finding.get("id")
        if not isinstance(fid, str) or not fid.strip():
            continue
        delta: dict[str, object] = {"id": fid, "verified": id(finding) in kept}
        for key in DELTA_VALUE_FIELDS:
            value = finding.get(key)
            if value is None:
                continue
            if key == "confidence":
                value = _delta_confidence(value)
                if value is None:
                    continue
            delta[key] = value
        deltas.append(cast(Delta, delta))
    return deltas


def run_receipt(args: argparse.Namespace) -> Envelope:
    repo_root = decide.resolve_repo_root()
    sha = args.head_sha or decide.resolve_head_sha() or ""
    try:
        # Proofs precede decoding and coercion: the token covers received bytes and the
        # value proof covers the dispatched document before numeric values are rewritten.
        inline_checksum = fnv1a32(args.input_inline)
        data = validate_input_shape(decode_inline_slice(args.input_inline))
        input_checksum = checksum_or_none(data)
        try:
            input_text = js_stringify_pretty(data)
        except JsSerializationError:
            input_text = json.dumps(data, indent=2, ensure_ascii=True)
        write_atomic(args.input, input_text)
        for finding in data["findings"]:
            coerce_numeric_fields(finding)
        findings = cast(list[FindingWire], data["findings"])
        context = decide.VerifyContext(
            repo_root, cast(str, data.get("base_branch") or args.base_branch)
        )
        result = decide.run_verification(findings, context, args.diff_file)
        deltas = build_deltas(findings, result["verified"])
        receipt: Receipt = {
            "sha": sha,
            "n_in": len(findings),
            "nonce": args.nonce,
            "deltas_checksum": checksum_or_none(deltas),
            "inline_checksum": inline_checksum,
        }
        # Omit unavailable input proof rather than null: the executor schema only accepts
        # strings, and trustSlice skips the check when dispatch cannot compute it either.
        if input_checksum is not None:
            receipt["input_checksum"] = input_checksum
        # Deltas must precede the full arrays so a capped executor Read preserves the echo.
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
        default=None,
        metavar="PATH",
        help=(
            "Path to a pre-fetched unified diff file. "
            "If omitted, the script runs 'git diff <base-branch>...HEAD'."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help=("Write output JSON to this file. If omitted, output goes to stdout."),
    )
    parser.add_argument(
        "--input",
        default=None,
        metavar="PATH",
        help=("Receipt mode: code-written destination for the inline slice document."),
    )
    parser.add_argument(
        "--input-inline",
        default=None,
        metavar="TEXT",
        help="Receipt mode: the strict percent-encoded slice document to decode and write.",
    )
    parser.add_argument(
        "--nonce",
        default=None,
        metavar="STR",
        help=(
            "Receipt mode: opaque nonce echoed back verbatim in the receipt so "
            "the workflow can confirm this output answers its dispatch."
        ),
    )
    parser.add_argument(
        "--head-sha",
        default=None,
        metavar="SHA",
        help=(
            "Receipt mode: head sha echoed into the receipt. "
            "Falls back to 'git rev-parse --short HEAD' when omitted."
        ),
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
