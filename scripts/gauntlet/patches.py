"""Render patches beside the checksum-proven primary report, which reruns self-heal."""

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict, cast

from gauntlet.cli import Command, Parser, UsageError
from gauntlet.delivery import gate
from gauntlet.delivery.gate import FixReason, fix_code_text
from gauntlet.diff import parse_diff, patch_report_policy
from gauntlet.fs import JsonReadError, confined, read_json, write_atomic
from gauntlet.markdown import code_span, fence_run
from gauntlet.text import neutralize_comment_openers, redact_secrets

# The pure gate avoids verify.decide's import-time git and delivery's dry-run state.
__all__ = ["CLI", "OracleState", "PatchReceipt"]

OracleState = Literal["unattempted", "missing", "ok"]


class PatchReceipt(TypedDict):
    ok: bool
    path: str | None
    oracle: OracleState | None
    candidates: int
    kept: int
    downgraded: int
    reasons: dict[FixReason, int]
    filtered_earlier: int
    findings: int
    warnings: list[str]
    errors: list[str]


_HEAD_SHA_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_EXT_RE = re.compile(r"^[A-Za-z0-9_+#-]{1,12}$")
_NO_PATCHES_LINE = "No finding carried a patch this step could check."
_NO_ORACLE_LINE = (
    "The pinned diff file was missing or empty, so every candidate patch "
    "failed closed (`no_diff_oracle`)."
)
# Input JSON may carry lone surrogates; the final fallback must stay valid ASCII.
_FALLBACK_LINE = (
    '{"ok": false, "path": null, "oracle": null, "candidates": 0, '
    '"kept": 0, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, '
    '"findings": 0, "warnings": [], "errors": ["receipt could not be serialized"]}'
)


def _parser() -> Parser:
    parser = Parser(
        prog="report_patches",
        description=(
            "Render the apply-checked suggested_fix_code patches of the persisted "
            "findings into a read-only sibling artifact."
        ),
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        metavar="DIR",
        help="The review's output directory. Every derived path resolves inside it.",
    )
    parser.add_argument(
        "--head-sha",
        required=True,
        metavar="SHORT",
        help="head_sha_short, the artifact filename discriminator.",
    )
    return parser


def _load_findings(path: str, errors: list[str]) -> list[object] | None:
    # persistPrimaries writes a bare array with canonical and v2 alias fields;
    # a wrapped-object fallback would hide a wrong artifact.
    try:
        data = read_json(path, errors="replace")
    except JsonReadError as exc:
        if exc.kind == "read":
            errors.append(f"could not read findings file {path}: {exc.cause}")
        else:
            errors.append(f"invalid JSON in findings file {path}: {exc.cause}")
        return None
    if not isinstance(data, list):
        errors.append(
            f"findings file {path} must be a JSON array of findings, got "
            f"{type(data).__name__}"
        )
        return None
    return data


_LINE_BREAK_RUN_RE = re.compile(r"[\r\n \x0b\x0c\x85]+")


def _one_line(value: object) -> str:
    # Heading fields cannot create blocks; unpaired surrogates must render as '?'.
    text = _LINE_BREAK_RUN_RE.sub(" ", str(value)).strip()
    return text.encode("utf-8", "replace").decode("utf-8")


def _render(
    kept: Sequence[Mapping[str, object]],
    candidates: int,
    filtered_earlier: int,
    oracle_state: OracleState,
    sha: str,
    *,
    reasons: Mapping[FixReason, int],
) -> str:
    parts: list[str] = []

    def emit(text: str, *, raw: bool = False) -> None:
        # Kept patch bytes bypass prose neutralization; they passed the gate.
        parts.append(text if raw else neutralize_comment_openers(text))

    emit(f"# Apply-checked patches (against {sha})")
    emit(
        f"{len(kept)} of {candidates} suggested patch(es) passed the read-only "
        f"apply-check against the pinned review diff "
        f"(`code-gauntlet-diff-{sha}.patch`, captured at Phase 2, not the current "
        "working tree or branch). Platform render-site constraints are not applied "
        "here, nor is delivery's set-level overlap withholding (a fence overlapping "
        "an already-kept fence in the same file, reason `overlaps_kept_fence`), so a "
        "patch kept here may still be downgraded or withheld at delivery. This covers "
        "high-confidence findings only; unverified findings carry no patch here."
    )
    downgraded = sum(reasons.values())
    if downgraded > 0:
        tally = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        tally_str = ", ".join(f"{reason} ({n})" for reason, n in tally)
        emit(f"Downgraded: {downgraded} — reason tally: {tally_str}")
    if oracle_state == "missing":
        emit(_NO_ORACLE_LINE)
    if filtered_earlier > 0:
        emit(
            f"{filtered_earlier} patch(es) were removed earlier by the pipeline's "
            "content filter and are not candidates here."
        )
    if candidates == 0:
        emit(_NO_PATCHES_LINE)

    for finding in kept:
        file_ = _one_line(finding.get("file", "?"))
        line = finding.get("line")
        end_line = finding.get("end_line")
        # A truthy whitespace title must collapse before fallback selection.
        title = (
            _one_line(finding.get("title") or "")
            or _one_line(finding.get("id") or "")
            or "finding"
        )
        emit(f"## {code_span(file_)}:{line}-{end_line} — {title}")

        text = fix_code_text(finding.get("suggested_fix_code"))
        # Defense in depth: the gate already proved redaction is a no-op.
        text = redact_secrets(cast(str, text))
        fence = fence_run(text)
        ext = os.path.splitext(file_)[1].lstrip(".")
        info = ext if _EXT_RE.match(ext) else ""
        emit(f"{fence}{info}\n{text}\n{fence}", raw=True)

    return "\n\n".join(parts) + "\n"


def _failure_receipt(message: str | None = None) -> PatchReceipt:
    return {
        "ok": False,
        "path": None,
        "oracle": "unattempted",
        "candidates": 0,
        "kept": 0,
        "downgraded": 0,
        "reasons": {},
        "filtered_earlier": 0,
        "findings": 0,
        "warnings": [],
        "errors": [] if message is None else [message],
    }


def _execute(args: argparse.Namespace) -> tuple[PatchReceipt, int]:
    if not _HEAD_SHA_RE.match(args.head_sha):
        raise UsageError(
            f"--head-sha must match {_HEAD_SHA_RE.pattern!r}: {args.head_sha!r}", 2
        )
    output_root = os.path.realpath(args.output_dir)
    prefix = os.path.join(args.output_dir, "code-gauntlet-")
    findings_path = f"{prefix}findings-{args.head_sha}.json"
    diff_path = f"{prefix}diff-{args.head_sha}.patch"
    out_path = f"{prefix}patches-{args.head_sha}.md"
    receipt = _failure_receipt()
    receipt["path"] = out_path
    for label, path in (
        ("findings", findings_path),
        ("diff", diff_path),
        ("out", out_path),
    ):
        if not confined(path, output_root):
            receipt["errors"].append(f"{label} path escapes --output-dir: {path}")
    if receipt["errors"]:
        return receipt, 1
    findings = _load_findings(findings_path, receipt["errors"])
    if findings is None:
        return receipt, 1
    # A receipt must survive every post-load failure, including valid JSON lone
    # surrogates that fail only when the artifact is encoded back out as UTF-8.
    try:
        receipt["findings"] = len(findings)
        # Universal newlines match the live subprocess text=True oracle.
        # Replacement decoding degrades only the undecodable line, never the run.
        try:
            with open(diff_path, encoding="utf-8", errors="replace") as fh:
                diff_text = fh.read()
        except OSError:
            diff_text = None
        if diff_text is not None and not diff_text.strip():
            # Phase 2 treats empty capture as producer failure: disclose missing
            # oracle/no_diff_oracle, rather than an empty diff/range_not_in_diff.
            diff_text = None
        oracle_state: OracleState = "missing" if diff_text is None else "ok"
        receipt["oracle"] = oracle_state
        facts = (
            None
            if diff_text is None
            else parse_diff(diff_text, policy=patch_report_policy(diff_text))
        )
        candidates = [
            cast(Mapping[str, object], f)
            for f in findings
            if isinstance(f, dict) and "suggested_fix_code" in f
        ]
        receipt["candidates"] = len(candidates)
        receipt["filtered_earlier"] = sum(
            1
            for f in findings
            if isinstance(f, dict) and "suggested_fix_code_removed_by" in f
        )
        kept = []
        for finding in candidates:
            verdict = gate.evaluate_fix(
                finding,
                apply_range=(finding.get("line"), finding.get("end_line")),
                facts=facts,
            )
            if verdict.keep:
                receipt["kept"] += 1
                kept.append(finding)
            else:
                reason = cast(FixReason, verdict.reason)
                receipt["downgraded"] += 1
                receipt["reasons"][reason] = receipt["reasons"].get(reason, 0) + 1
                message = gate.format_fix_warning(finding, reason, label="report-patch")
                receipt["warnings"].append(message)
                print(
                    "report_patches: " + " ".join(message.splitlines()), file=sys.stderr
                )
        content = _render(
            kept,
            receipt["candidates"],
            receipt["filtered_earlier"],
            oracle_state,
            args.head_sha,
            reasons=receipt["reasons"],
        )
        write_atomic(out_path, content)
        receipt["ok"] = True
    except Exception as exc:  # noqa: BLE001 - a receipt must always be emitted
        receipt["errors"].append(f"{type(exc).__name__}: {exc}")
    receipt["reasons"] = dict(sorted(receipt["reasons"].items()))
    return receipt, 0 if receipt["ok"] else 1


CLI = Command(
    parser=_parser(),
    main=_execute,
    failure_receipt=_failure_receipt,
    fallback_line=_FALLBACK_LINE,
)
