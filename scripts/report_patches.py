#!/usr/bin/env python3
"""
report_patches.py — read-only Phase 8 gate: render the apply-checked
``suggested_fix_code`` patches of the persisted high-confidence findings into a
sibling artifact.

What it does
------------
Runs the diff-only subset of delivery's deterministic apply-check
(``scripts/post_review.py``'s ``_gated_finding`` / ``_suggested_fix_gate``)
against the PINNED review diff captured at Phase 2, and renders every patch
that passes into ``{output_dir}/code-gauntlet-patches-{head_sha_short}.md`` —
a heading and fenced code block per kept patch, preceded by a summary of how
many candidates passed, were downgraded (with a reason tally), or had no
diff oracle to check against. It writes nothing else and reads
``findings.json``/the diff read-only; nothing it does can fail delivery or
change what a PR/MR comment posts.

Why a SIBLING artifact, not an edit to the report
--------------------------------------------------
``code-gauntlet-report-{sha}.md`` is a persist-plan ``expect[]`` primary: its
bytes are checksum-proven by ``assemble_artifacts.py`` on every re-run, and a
``materialize_artifacts.py`` re-run self-heals the file straight from
``persistReturn``, silently reverting any in-place edit made after the fact.
Writing a NEW file sidesteps both problems — it has no plan entry to disagree
with and nothing to self-heal away.

Why a SEPARATE script, not a `post_review.py` mode
----------------------------------------------------
``post_review.py``'s ``main()`` owns ``DRY_RUN``/``CODE_GAUNTLET_POST_MODE``
and writes ``post-review-payload.json`` next to the findings file — the file
bench scores as the delivery candidate set. A sub-mode squeezed into that
``main()`` risks either mode leaking into the other's write path. This script
never calls ``post_review.main()``; it imports only the pure gate helpers.

Producer detection (read this before touching the oracle)
------------------------------------------------------------
The pinned diff has three producers: ``gh pr diff`` (full or incremental) and
plain ``git diff`` (branch/local targets, and the incremental path on either
platform) run git's own diff machinery, and plain ``glab mr diff``
reconstructs headers from the MR versions API with paths verbatim and writes
no ``diff --git`` line at all (see ``tests/fixtures/glab_diff/README.md``).
Git's diff machinery does NOT always write ``a/``/``b/`` prefixes — that is
only its default. ``diff.noprefix=true`` drops them entirely (the first line
reads ``diff --git foo.py foo.py``), and ``diff.mnemonicPrefix`` swaps them
for ``i/``/``w/`` instead. So the check anchors on the one shape every git
producer's default config writes — a first line matching ``diff --git "?a/``
(the optional quote covers a C-quoted first file) — and only THAT shape is
keyed by stripping ``a/``/``b/``, via ``post_review.parse_diff_text``, the
same parser ``post_review.py`` runs live, with no alias keys and no second
keying implementation. Neither non-default config matches the anchor, so both
fall to verbatim keying: under ``diff.noprefix`` that is exactly right (every
path keys as itself), and under ``diff.mnemonicPrefix`` it fails closed on
every finding instead of stripping the wrong prefix — the safer of the two
wrong answers. The check reads the first line only, so no body content can
masquerade as the header, and an empty diff keys nothing under either
reading. Name the one residual: a git-shaped incremental diff on a GitLab run
is keyed git-style while live delivery keys glab-style — the finding's path
spelling is the same real path under both, so a kept patch here may still be
downgraded live for render-site reasons or withheld for the delivery-side
set-level overlap reason (``overlaps_kept_fence`` — see
``post_review._overlap_losers``); both are already disclosed in the
artifact.

Usage:
    python3 report_patches.py --output-dir DIR --head-sha SHORT
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# NEVER import verify_findings here — it resolves the repo root via
# `git rev-parse --show-toplevel` at import time, which this script has no
# business triggering for a read-only render step.
from assemble_artifacts import write_text_atomic
from post_review import (
    _FIX_COUNTS,
    _FIX_REASON_COUNTS,
    _SKIP_WARNINGS,
    _fence_run,
    _fix_code_text,
    _gated_finding,
    _redact_secrets,
    parse_diff_text,
    reset_run_state,
)

_HEAD_SHA_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Anchored to the FIRST line, and to git's default `a/` prefix specifically —
# not a bare `diff --git ` — because that default prefix is exactly what
# `parse_diff_text("github", …)` strips. Under `diff.noprefix=true` the first
# line is `diff --git foo.py foo.py`: it does not match, so parsing falls to
# verbatim keying, which is exactly right (every path keys as itself). Under
# `diff.mnemonicPrefix` the first line is `diff --git i/… w/…`: it also does
# not match, so parsing falls to verbatim keying too — this fails closed on
# every finding rather than stripping the wrong prefix, the safer of the two
# wrong answers. The optional `"` keeps a C-quoted first file git-shaped
# (`diff --git "a/café.py" "b/café.py"`). Searching the whole text instead of
# anchoring to the first line would let a marker-less body line (a bare
# zero-prefixed context line whose content begins `diff --git a/`) flip a
# verbatim diff into git-shaped keying.
_GIT_SHAPED_RE = re.compile(r'\Adiff --git "?a/')
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


def _confined(path, output_root):
    """True when *path* resolves inside *output_root*.

    Every path this script touches is DERIVED from --output-dir and a
    regex-validated --head-sha (no `/` can appear in the sha, so no filename
    built from it can smuggle a path separator) — so this can only ever fire
    on a pathological --output-dir. Kept anyway as the same typo/symlink guard
    materialize_artifacts.py's own ``_confined`` applies: a wrong flag refuses
    loudly instead of writing somewhere nothing reads.
    """
    try:
        target = os.path.realpath(path)
        return target == output_root or target.startswith(output_root + os.sep)
    except OSError:
        return False


def _load_findings(path, errors):
    """Return the persisted findings list, or None (with *errors* populated).

    The artifact this script reads is exactly what
    ``workflows/src/stages.js``'s ``persistPrimaries`` writes: a bare JSON
    array of union-schema findings (v2 aliases ``line``/``end_line``/``body``
    alongside the canonical names). Any other shape is a hard error — there is
    no wrapped-object variant to fall back to.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as exc:
        errors.append(f"could not read findings file {path}: {exc}")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        errors.append(f"invalid JSON in findings file {path}: {exc}")
        return None
    if not isinstance(data, list):
        errors.append(
            f"findings file {path} must be a JSON array of findings, got "
            f"{type(data).__name__}"
        )
        return None
    return data


def _diff_oracle(diff_text):
    """Return ``(valid_lines, line_texts)`` from *diff_text*, keyed by
    ``post_review.parse_diff_text`` after detecting the producer from the bytes —
    see the "Producer detection" module docstring section."""
    platform = "github" if _GIT_SHAPED_RE.search(diff_text) else "gitlab"
    valid_lines, _new_files, _old_paths, line_texts = parse_diff_text(
        platform, diff_text
    )
    return valid_lines, line_texts


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
    class of defect ``post_review.py``'s ``build_skipped_section`` guards
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

    ``ensure_ascii=True`` is load-bearing, not cosmetic: the receipt is
    machine-read, so escaping every non-ASCII codepoint as ``\\uXXXX`` costs
    nothing a reader needs, and it guarantees *line* is pure ASCII before it
    ever reaches ``sys.stdout``. That guarantee is what stops a host whose
    stdout is opened with a narrow codec (``PYTHONIOENCODING=ascii``, the
    default *stderr* error handler is ``backslashreplace`` but *stdout*'s is
    not) from raising ``UnicodeEncodeError`` on a warning line that embeds a
    repo path outside ASCII — which previously left stdout completely empty,
    indistinguishable from a dead executor. The write itself stays inside the
    same ``try`` as ``json.dumps`` — belt and suspenders against any other
    codec surprise — and the fallback string below is hand-verified ASCII so
    it can never trip the same failure it exists to recover from.
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
        if not _confined(path, output_root):
            errors.append(f"{label} path escapes --output-dir: {path}")
    if errors:
        return _pre_oracle_failure(out_path, errors)

    findings = _load_findings(findings_path, errors)
    if findings is None:
        return _pre_oracle_failure(out_path, errors)

    # Universal newlines (default text mode) and errors="replace" are
    # deliberate: the live apply-check oracle post_review.py drives is
    # subprocess text=True output, which already normalizes \r\n, and a
    # byte this repo cannot decode must not crash a read-only render step —
    # it degrades that one line's oracle, not the whole run.
    try:
        with open(diff_path, encoding="utf-8", errors="replace") as fh:
            diff_text = fh.read()
    except OSError:
        diff_text = None

    if diff_text is not None and not diff_text.strip():
        # A 0-byte capture is Phase 2's documented diff-producer failure
        # mode. It must take the same disclosed, fail-closed path as a
        # missing file — parsing it as an empty-but-present diff would key
        # nothing and downgrade every candidate as `range_not_in_diff`
        # instead of the honest `no_diff_oracle`.
        diff_text = None

    if diff_text is None:
        oracle_state = "missing"
        valid_lines, line_texts = None, None
    else:
        oracle_state = "ok"
        valid_lines, line_texts = _diff_oracle(diff_text)

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
                valid_lines,
                line_texts,
                warn_label="report-patch",
            )
            if "suggested_fix_code" in gated:
                kept.append(gated)

        content = _render(
            kept, len(candidates), filtered_earlier, oracle_state, args.head_sha
        )
        write_text_atomic(out_path, content)
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


if __name__ == "__main__":
    sys.exit(main())
