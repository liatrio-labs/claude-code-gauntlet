#!/usr/bin/env python3
"""Render the apply-checked suggested-fix patches beside the persisted report.

A sibling file, because the report is a checksum-proven primary that re-runs self-heal."""

import argparse
import json
import os
import re
import sys

# NEVER import gauntlet.verify.decide here: it runs git at import time.
# Only the pure gate helpers come from delivery; its main() owns the dry-run payload.
from gauntlet.cli import Command
from gauntlet.delivery.post import (
    _FIX_COUNTS,
    _FIX_REASON_COUNTS,
    _SKIP_WARNINGS,
    _fence_run,
    _fix_code_text,
    _gated_finding,
    _redact_secrets,
    reset_run_state,
)
from gauntlet.diff import parse_diff, patch_report_policy
from gauntlet.fs import JsonReadError, confined, read_json, write_atomic

_HEAD_SHA_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_EXT_RE = re.compile(r"^[A-Za-z0-9_+#-]{1,12}$")
_COMMENT_OPEN_RE = re.compile(r"<!--")

_NO_PATCHES_LINE = "No finding carried a patch this step could check."
_NO_ORACLE_LINE = (
    "The pinned diff file was missing or empty, so every candidate patch "
    "failed closed (`no_diff_oracle`)."
)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Render the apply-checked suggested_fix_code patches of the persisted "
            "findings into a read-only sibling artifact."
        )
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        required=True,
        metavar="DIR",
        help="The review's output directory. Every derived path resolves inside it.",
    )
    parser.add_argument(
        "--head-sha",
        dest="head_sha",
        required=True,
        metavar="SHORT",
        help="head_sha_short, the artifact filename discriminator.",
    )
    return parser


def _load_findings(path, errors):
    """Return the persisted findings list, or None (with *errors* populated).

    The artifact this script reads is exactly what
    ``workflows/src/stages.js``'s ``persistPrimaries`` writes: a bare JSON
    array of union-schema findings (v2 aliases ``line``/``end_line``/``body``
    alongside the canonical names). Any other shape is a hard error — there is
    no wrapped-object variant to fall back to.
    """
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


def _code_span(s):
    """Inline code span wrapping *s*: a backtick run longer than any run *s*
    contains, space-padded when *s* itself starts or ends with a backtick."""
    runs = re.findall(r"`+", s)
    ticks = "`" * (max((len(r) for r in runs), default=0) + 1)
    pad = " " if s.startswith("`") or s.endswith("`") else ""
    return f"{ticks}{pad}{s}{pad}{ticks}"


_LINE_BREAK_RUN_RE = re.compile(r"[\r\n \x0b\x0c\x85]+")


def _one_line(value):
    """Collapse *value* onto a single rendered line before it reaches a
    markdown ``##`` heading.

    A finding's ``title`` or ``file`` is caller-supplied and may embed a
    newline (or a full fenced block, per the module's threat model) — left
    alone, that would either break the heading across lines or, worse, forge
    extra markdown structure the artifact never actually verified. ``str()``
    first (a non-string field survives instead of crashing this render
    step), then every run of CR/LF/VT/FF/NEL/space collapses to exactly one
    space, then the result is stripped. The final ``encode/decode`` round
    trip through ``errors="replace"`` is defence for a LONE UTF-16 surrogate
    smuggled through the findings JSON (valid JSON, invalid Unicode): the
    strict-UTF-8 artifact write later in this script must not raise on it.
    ``str.encode(..., "replace")`` substitutes an unencodable surrogate with
    a plain ASCII ``?`` (NOT U+FFFD — that substitution is what the
    ``"replace"`` error handler uses on invalid *decode* input, not what it
    uses for an unencodable *encode* input), so that is what survives into
    the heading here, at render time, where the run still succeeds.
    """
    text = _LINE_BREAK_RUN_RE.sub(" ", str(value)).strip()
    return text.encode("utf-8", "replace").decode("utf-8")


def _neutralize(text):
    """Defuse an open HTML comment in model-/repo-derived text.

    A finding's title or file path reaches this artifact raw. Left alone, a
    stray ``<!--`` in either would open an HTML comment that swallows
    everything rendered after it for the rest of the document — the same
    class of defect ``gauntlet.delivery.post``'s ``build_skipped_section`` guards
    against for the PR/MR body. Applied to every non-fence line; a kept
    patch's fence payload bypasses it (see the fence-building loop below) —
    the payload already passed the gate's redaction check, and rewriting
    bytes inside a committable patch would make the artifact lie about what
    was actually verified.
    """
    return _COMMENT_OPEN_RE.sub("&lt;!--", text)


def _render(kept, candidates, filtered_earlier, oracle_state, sha):
    """Return the whole markdown document, deterministically, from KEPT
    findings (already gated) plus the run's own counters.

    ``_FIX_COUNTS`` (not ``candidates - len(kept)``) is the single source of
    truth for kept/downgraded: it is what ``_gated_finding`` itself
    incremented while gating this run's candidates, reset fresh by
    ``main()``'s ``reset_run_state()`` call before the first one.
    """
    downgraded = _FIX_COUNTS["downgraded"]
    parts = []

    def emit(text, *, raw=False):
        parts.append(text if raw else _neutralize(text))

    emit(f"# Apply-checked patches (against {sha})")
    emit(
        f"{_FIX_COUNTS['kept']} of {candidates} suggested patch(es) passed the read-only "
        f"apply-check against the pinned review diff "
        f"(`code-gauntlet-diff-{sha}.patch`, captured at Phase 2, not the current "
        "working tree or branch). Platform render-site constraints are not applied "
        "here, nor is delivery's set-level overlap withholding (a fence overlapping "
        "an already-kept fence in the same file, reason `overlaps_kept_fence`), so a "
        "patch kept here may still be downgraded or withheld at delivery. This covers "
        "high-confidence findings only; unverified findings carry no patch here."
    )
    if downgraded > 0:
        tally = sorted(_FIX_REASON_COUNTS.items(), key=lambda kv: (-kv[1], kv[0]))
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
        # _one_line is applied to EACH candidate before the `or` chain picks
        # one, not once at the end: a whitespace-only title is truthy as a
        # raw value but collapses to "" once rendered, and must fall through
        # to id/"finding" rather than leaving a dangling "— " with no text.
        title = (
            _one_line(finding.get("title") or "")
            or _one_line(finding.get("id") or "")
            or "finding"
        )
        emit(f"## {_code_span(file_)}:{line}-{end_line} — {title}")

        text = _fix_code_text(finding.get("suggested_fix_code"))
        text = _redact_secrets(
            text
        )  # defense in depth: the gate already proved this is a no-op
        fence = _fence_run(text)
        ext = os.path.splitext(file_)[1].lstrip(".")
        info = ext if _EXT_RE.match(ext) else ""
        emit(f"{fence}{info}\n{text}\n{fence}", raw=True)

    return "\n\n".join(parts) + "\n"


def _receipt(
    *,
    ok,
    path,
    oracle,
    candidates,
    kept,
    downgraded,
    reasons,
    filtered_earlier,
    findings,
    warnings,
    errors,
):
    """Assemble the one receipt dict this script ever emits.

    *oracle* is one of three values: ``"unattempted"`` — a pre-oracle failure
    (the confinement check, or the findings file itself could not be loaded)
    means the diff was never even opened; ``"missing"`` — the diff file
    could not be read, or was read and was empty, so every candidate failed
    closed as ``no_diff_oracle``; ``"ok"`` — the diff was read, was
    non-empty, and was parsed.

    *reasons* is sorted here, once, so every caller passes the raw
    ``_FIX_REASON_COUNTS`` (or ``{}``) without needing its own
    ``dict(sorted(...))`` — one sort site instead of one per call.
    """
    return {
        "ok": ok,
        "path": path,
        "oracle": oracle,
        "candidates": candidates,
        "kept": kept,
        "downgraded": downgraded,
        "reasons": dict(sorted(reasons.items())),
        "filtered_earlier": filtered_earlier,
        "findings": findings,
        "warnings": warnings,
        "errors": errors,
    }


def _emit_receipt(receipt):
    """Write the one JSON receipt line this script ever emits — never empty.

    ``ensure_ascii=True`` is load-bearing because input JSON may contain a
    lone surrogate, which has no valid UTF-8 encoding. Escaping non-ASCII
    codepoints keeps the machine-readable receipt valid ASCII even when a
    warning contains such a path. Ordinary non-ASCII text is supported by the
    UTF-8 CLI bootstrap; the write remains inside the same ``try`` as
    ``json.dumps`` and the fallback below is hand-verified ASCII.
    """
    try:
        line = json.dumps(receipt, ensure_ascii=True)
        sys.stdout.write(line + "\n")
        return
    except Exception:  # noqa: BLE001 - stdout is NEVER empty
        pass
    sys.stdout.write(
        '{"ok": false, "path": null, "oracle": null, "candidates": 0, '
        '"kept": 0, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, '
        '"findings": 0, "warnings": [], '
        '"errors": ["receipt could not be serialized"]}\n'
    )


def _pre_oracle_failure(out_path, errors):
    """Emit the receipt for a failure before the oracle was ever attempted and
    return the exit status. Both pre-oracle exits (path confinement, findings
    load) report the same all-zero shape; only *errors* differs."""
    _emit_receipt(
        _receipt(
            ok=False,
            path=out_path,
            oracle="unattempted",
            candidates=0,
            kept=0,
            downgraded=0,
            reasons={},
            filtered_earlier=0,
            findings=0,
            warnings=[],
            errors=errors,
        )
    )
    return 1


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not _HEAD_SHA_RE.match(args.head_sha):
        parser.error(
            f"--head-sha must match {_HEAD_SHA_RE.pattern!r}: {args.head_sha!r}"
        )

    reset_run_state()

    output_root = os.path.realpath(args.output_dir)
    findings_path = os.path.join(
        args.output_dir, f"code-gauntlet-findings-{args.head_sha}.json"
    )
    diff_path = os.path.join(
        args.output_dir, f"code-gauntlet-diff-{args.head_sha}.patch"
    )
    out_path = os.path.join(
        args.output_dir, f"code-gauntlet-patches-{args.head_sha}.md"
    )

    errors = []
    for label, path in (
        ("findings", findings_path),
        ("diff", diff_path),
        ("out", out_path),
    ):
        if not confined(path, output_root):
            errors.append(f"{label} path escapes --output-dir: {path}")
    if errors:
        return _pre_oracle_failure(out_path, errors)

    findings = _load_findings(findings_path, errors)
    if findings is None:
        return _pre_oracle_failure(out_path, errors)

    # Universal newlines (default text mode) and errors="replace" are
    # deliberate: the live apply-check oracle gauntlet.delivery.post drives is
    # subprocess text=True output, which already normalizes \r\n, and a
    # byte this repo cannot decode must not crash a read-only render step —
    # it degrades that one line's oracle, not the whole run.
    try:
        with open(diff_path, encoding="utf-8", errors="replace") as fh:
            diff_text = fh.read()
    except OSError:
        diff_text = None

    if diff_text is not None and not diff_text.strip():
        # An empty capture must take the same disclosed, fail-closed path as a
        # missing file — parsing it as an empty-but-present diff would key
        # nothing and downgrade every candidate as `range_not_in_diff`
        # instead of the honest `no_diff_oracle`.
        diff_text = None

    if diff_text is None:
        oracle_state = "missing"
        facts = None
    else:
        oracle_state = "ok"
        facts = parse_diff(diff_text, policy=patch_report_policy(diff_text))

    candidates = [
        f for f in findings if isinstance(f, dict) and "suggested_fix_code" in f
    ]
    filtered_earlier = sum(
        1
        for f in findings
        if isinstance(f, dict) and "suggested_fix_code_removed_by" in f
    )

    # Gate, render, write: wrapped as one unit because a receipt must still be
    # emitted (ok:false, no artifact) no matter which of the three raises — a
    # findings-JSON byte this repo cannot even ENCODE back out (a lone UTF-16
    # surrogate smuggled through valid JSON) must degrade to a reported error,
    # not an unhandled traceback with no receipt line at all.
    try:
        kept = []
        for finding in candidates:
            gated = _gated_finding(
                finding,
                (finding.get("line"), finding.get("end_line")),
                facts,
                warn_label="report-patch",
            )
            if "suggested_fix_code" in gated:
                kept.append(gated)

        content = _render(
            kept, len(candidates), filtered_earlier, oracle_state, args.head_sha
        )
        write_atomic(out_path, content)
    except Exception as exc:  # noqa: BLE001 - a receipt must always be emitted
        errors.append(f"{type(exc).__name__}: {exc}")
        _emit_receipt(
            _receipt(
                ok=False,
                path=out_path,
                oracle=oracle_state,
                candidates=len(candidates),
                kept=_FIX_COUNTS["kept"],
                downgraded=_FIX_COUNTS["downgraded"],
                reasons=_FIX_REASON_COUNTS,
                filtered_earlier=filtered_earlier,
                findings=len(findings),
                warnings=list(_SKIP_WARNINGS),
                errors=errors,
            )
        )
        return 1

    _emit_receipt(
        _receipt(
            ok=True,
            path=out_path,
            oracle=oracle_state,
            candidates=len(candidates),
            kept=_FIX_COUNTS["kept"],
            downgraded=_FIX_COUNTS["downgraded"],
            reasons=_FIX_REASON_COUNTS,
            filtered_earlier=filtered_earlier,
            findings=len(findings),
            warnings=list(_SKIP_WARNINGS),
            errors=[],
        )
    )
    return 0


CLI = Command.legacy(main, prog="report_patches.py")
