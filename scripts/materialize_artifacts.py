#!/usr/bin/env python3
"""
materialize_artifacts.py — put the review's artifacts on disk from the workflow's
own return value, with no model in the path.

Usage:
    python3 materialize_artifacts.py --output-dir DIR --task <task-id-or-path>
    python3 materialize_artifacts.py --output-dir DIR --nonce <args.nonce>
    python3 materialize_artifacts.py --output-dir DIR --task <id> --nonce <nonce>

Why this script exists
----------------------
The workflow sandbox has no disk, so every artifact used to reach disk through an
artifact-writer agent: a language model asked to reproduce ~50 KB of escape-dense
JSON verbatim. Measured across every recorded run (38 writer journals / 84
artifacts), 26 of 73 attempted writes — 36% — failed their own content proof and
12 artifacts were never written at all. Nothing about a document predicts which
one fails: a 47 KB findings.json with 104 backslashes came back byte-perfect, a
6.4 KB zero-backslash .md was truncated. The worst losses are silent
summarization (one checkpoint lost 29,132 chars because the writer dropped 11
fields from every finding; another lost 13,008 with its schema intact and its
prose simply rewritten shorter), which no encoding or format prevents — and both
parse cleanly. When findings.json is the casualty, assemble_artifacts.py
correctly refuses to derive anything, post-review.json is never produced, and no
PR comment can be posted.

A workflow's return value is different in kind: the HARNESS serializes it to
``tasks/<task-id>.output``, and nothing retypes it. Measured 2026-07-30 with a
zero-subagent probe, the on-disk file was byte-exact at 200,000 / 500,000 /
4,000,000 requested chars (fnv1a32 match at each, a lone surrogate included), in
18-122 ms, with no ceiling found. The largest run ever recorded carries ~66 KB of
unique content. So the pipeline now returns the primaries instead of dictating
them, and this script — run from Phase 8, which has Bash — reads them out of that
file and writes them itself.

What it does
------------
1. Finds the task output file (by id/path, by the run's ``nonce``, or both) and
   pulls the compact return's ``persistReturn`` payload out of it, reusing
   await_workflow.py's resolution and terminal-detection rather than a second
   copy of either.
2. Writes every ``{ path, text }`` entry it carries — findings.json, report.md
   and the persist plan — verbatim, atomically, and only inside --output-dir.
3. Runs assemble_artifacts.py's assembler on the plan to derive post-review.json
   and checkpoint-all.json from what actually landed, exactly as the executor
   agent does on the writer path.

THE CONTENT PROOF IS THE POINT, and it is not reimplemented here. The plan's
``expect[]`` proves the two primaries, its ``planChecksum`` proves itself, and
its ``derive[]`` proves the two projections. The first two gradings are
assemble_artifacts.py's own; the derive[] comparison is done here, in
proof_gaps(), against the assembler's own reported numbers — aimed at a
harness-written copy rather than a model-written one. Assume truncation, if it
ever happens, is SILENT: the proof is what would catch it.

Output
------
EXACTLY one line of JSON on stdout (diagnostics go to stderr), on every path
including an unexpected internal error:

    { "ok": bool, "channel": "return", "source": path|null, "scanned": N,
      "materialized": [ { path, chars, checksum } ],
      "assemble": { ...assemble_artifacts.py's receipt... }|null,
      "gaps": [ ... ], "errors": [ ... ] }

Exit codes
    0  Every artifact is on disk and every content proof matched.
    1  Something failed. ``errors`` says what, ``gaps`` says what to disclose,
       and ``materialized`` names whatever DID land — a run whose findings.json
       is proven and whose projections failed is still deliverable, so this is
       "disclose and deliver what exists", never "the review is gone".
    2  Usage error (argparse), with an empty stdout.

No external Python dependencies — stdlib only.
"""

import argparse
import glob
import json
import os
import sys

# Both halves of this script already exist elsewhere and must not be duplicated:
# the task-file resolution and terminal-object detection are await_workflow.py's,
# and the checksum, atomic write and derivation are assemble_artifacts.py's. The
# explicit path insert (rather than a try/except import) keeps both invocation
# modes working — `python3 scripts/materialize_artifacts.py` and
# `import scripts.materialize_artifacts` — without swallowing a real ImportError
# raised from inside either module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from assemble_artifacts import (
    assemble,
    escape_lone_surrogates,
    fnv1a32,
    utf16_len,
    write_text_atomic,
)
from await_workflow import (
    TASKS_DIR_ENV,
    find_terminal,
    read_text,
    resolve_target,
    task_roots,
)

#: The value `persistReturn.channel` must carry. A payload that does not name this
#: channel is not this contract and is skipped rather than guessed at.
CHANNEL = "return"

#: How many task-output files a nonce sweep will read before giving up. The sweep
#: exists for the case where no task id is in hand (a fast run returns inline),
#: and a session directory accumulates one file per background task, so it has to
#: be bounded — the same reason await_workflow.py bounds its embedded-object scan.
#: Newest-first ordering means the run that just finished is the first file read.
MAX_SCANNED_FILES = 200


# ---------------------------------------------------------------------------
# Finding the run's own task output file
# ---------------------------------------------------------------------------


def persist_return_of(path):
    """Return the ``persistReturn`` payload carried by *path*, or None.

    Never raises: an absent, half-written or unrelated file is simply not a
    source. `find_terminal` is await_workflow.py's — it already knows the
    Workflow tool's `{summary, ..., result}` envelope, the stringified-result
    variant, and the bare return, and it already refuses to accept a nested agent
    receipt as the pipeline's return.
    """
    terminal, _saw_bare_ok, _stop_reason = find_terminal(read_text(path))
    if not isinstance(terminal, dict):
        return None
    payload = terminal.get("persistReturn")
    if not isinstance(payload, dict) or payload.get("channel") != CHANNEL:
        return None
    return payload


def _sweep_paths(environ):
    """Every task-output file that may hold this session's runs, newest first.

    Bounded to MAX_SCANNED_FILES. $CODE_GAUNTLET_TASKS_DIR is honoured first and
    exactly as await_workflow.py honours it: one documented escape hatch for an
    environment whose task directory cannot be derived, never a guess.
    """
    patterns = []
    override = environ.get(TASKS_DIR_ENV)
    if override:
        patterns.append(os.path.join(override, "*.output"))
    for root in task_roots(environ):
        patterns.append(os.path.join(root, "*", "*", "tasks", "*.output"))
    hits = []
    for pattern in patterns:
        try:
            found = glob.glob(pattern)
        except OSError:
            continue
        for path in found:
            try:
                hits.append((os.path.getmtime(path), path))
            except OSError:
                continue
    hits.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _mtime, path in hits[:MAX_SCANNED_FILES]]


def select_source(task, nonce, environ):
    """Return ``(path, payload, scanned)`` for the run's task output file.

    The named target is tried first and costs one read. The nonce sweep is the
    fallback for a run whose task id was never printed (the Workflow tool returns
    inline when the pipeline finishes fast — the file is written either way), and
    it is also what makes a mis-typed id fail over instead of failing.

    When a nonce is given it is REQUIRED to match. Task ids are short and a
    session directory holds every run, so a payload from another review is a
    reachable accident — and delivering one review's findings under another's
    name is worse than not resolving at all.
    """
    scanned = 0
    candidates = []
    if task:
        resolved, _searched = resolve_target(task, environ)
        if resolved:
            candidates.append(resolved)
    if nonce:
        for path in _sweep_paths(environ):
            if path not in candidates:
                candidates.append(path)
    for path in candidates:
        scanned += 1
        payload = persist_return_of(path)
        if payload is None:
            continue
        if nonce and payload.get("nonce") != nonce:
            continue
        return path, payload, scanned
    return None, None, scanned


# ---------------------------------------------------------------------------
# Writing what it carries
# ---------------------------------------------------------------------------


def _confined(path, output_root):
    """True when *path* resolves inside *output_root*.

    The entries name their own destinations, which the pipeline built from
    `args.outputDir`. This is the check that a payload cannot write outside the
    review's own output directory — including through a symlink, since both sides
    are realpath'd — and it doubles as a typo guard: a wrong --output-dir refuses
    every entry loudly instead of scattering artifacts somewhere nothing reads.
    """
    try:
        target = os.path.realpath(path)
        return target == output_root or target.startswith(output_root + os.sep)
    except OSError:
        return False


def plan_entries(payload, output_root, errors):
    """Validate the payload and return ``(entries, plan_path)``.

    Every failure here is structural and nothing is written: the payload is the
    whole input, so a malformed one cannot be partially honoured.
    """
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
        if not _confined(path, output_root):
            errors.append(
                f"entry {index} writes outside the output directory: {path} is not "
                f"inside {output_root}"
            )
            return None, None
        checked.append((path, text))
    plan_path = payload.get("planPath")
    if not isinstance(plan_path, str) or not plan_path:
        errors.append("persistReturn names no persist plan to derive from")
        return None, None
    if plan_path not in [path for path, _text in checked]:
        errors.append(
            f"the named persist plan {plan_path} is not among the entries this "
            "payload carries"
        )
        return None, None
    return checked, plan_path


def write_entries(entries, materialized, errors):
    """Write every entry verbatim. Returns True when all of them landed.

    write_text_atomic is assemble_artifacts.py's: a sibling temp file plus
    os.replace, so a failure mid-write leaves the destination as it was rather
    than as a truncated file that a later stage would read as real.
    """
    ok = True
    for path, text in entries:
        try:
            write_text_atomic(path, text)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            errors.append(f"could not write {path} ({type(exc).__name__}: {exc})")
            ok = False
            continue
        materialized.append(
            {"path": path, "chars": utf16_len(text), "checksum": fnv1a32(text)}
        )
    return ok


# ---------------------------------------------------------------------------
# Grading what landed
# ---------------------------------------------------------------------------


def proof_gaps(receipt, plan_text):
    """The content-proof failures in *receipt*, worded for the caller's gaps.

    Two gradings, both the assembler's own numbers — nothing is re-checksummed
    here:

    * a PRIMARY whose `content_proof` came back `mismatch`: the bytes on disk are
      not the bytes the pipeline returned, so the return channel itself lost
      something. On the writer path this was an expected 36%; here it should be
      unreachable, which is exactly why it must be disclosed rather than assumed
      away.
    * a DERIVED document whose chars/checksum differ from the plan's own
      `derive[]` expectation. The plan is the pipeline's serialization of the
      document it held in memory, so a difference is a real Python-vs-JS
      serializer divergence (or a stale plan) — the canary the workflow's
      trustAssembleReceipt keeps on the writer path, kept here too now that no
      executor grades this run.
    """
    gaps = []
    for entry in receipt.get("verified") or []:
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
            item.get("path"): item
            for item in (json.loads(plan_text).get("derive") or [])
            if isinstance(item, dict)
        }
    except ValueError:
        return [*gaps, "the persist plan just written is not valid JSON"]
    for entry in receipt.get("written") or []:
        if not isinstance(entry, dict):
            continue
        want = expected.get(entry.get("path"))
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


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _receipt(ok, source, scanned, materialized, assemble_receipt, gaps, errors):
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


def _materialize(task, nonce, output_dir, environ=None):
    """Write what the run returned, derive the projections, return the receipt."""
    environ = os.environ if environ is None else environ
    errors = []
    gaps = []
    materialized = []
    output_root = os.path.realpath(output_dir)

    source, payload, scanned = select_source(task, nonce, environ)
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

    if not write_entries(entries, materialized, errors):
        # A partial write is still reported entry by entry; derivation is skipped
        # because the assembler would read a file that is not this run's.
        return _receipt(False, source, scanned, materialized, None, gaps, errors)

    receipt = assemble(plan_path)
    plan_text = dict(entries).get(plan_path, "")
    gaps.extend(proof_gaps(receipt, plan_text))
    if not receipt.get("ok"):
        errors.extend(
            receipt.get("errors") or ["the assembler refused without a reason"]
        )
        return _receipt(False, source, scanned, materialized, receipt, gaps, errors)
    return _receipt(not gaps, source, scanned, materialized, receipt, gaps, errors)


def materialize(task, nonce, output_dir, environ=None):
    """_materialize, with a last-resort guard so the caller ALWAYS gets a receipt.

    Same shape and same reason as assemble_artifacts.py's assemble(): the guard
    belongs to the function that promises a receipt, not to one caller of it, so
    every caller — main(), a test, a future importer — gets the promise. Every
    expected failure is already a receipt above; this catches the unexpected one
    and still reports it honestly as ok:false.
    """
    try:
        return _materialize(task, nonce, output_dir, environ)
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


def _minimal_receipt_line(exc):
    """A hand-built one-line receipt for when the real one will not serialize.

    Same last hop as assemble_artifacts.py's: an empty stdout is
    indistinguishable from a dead process, and this one is read by a model that
    branches on it.
    """
    try:
        return json.dumps(
            _receipt(
                False,
                None,
                0,
                [],
                None,
                [],
                [f"receipt could not be serialized: {type(exc).__name__}: {exc}"],
            )
        )
    except Exception:  # noqa: BLE001 - the never-print-nothing contract
        return (
            '{"ok": false, "channel": "return", "source": null, "scanned": 0, '
            '"materialized": [], "assemble": null, "gaps": [], '
            '"errors": ["receipt could not be serialized"]}'
        )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Write the review's artifacts from the workflow's own return value."
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
        "path (resolved exactly as await_workflow.py resolves it).",
    )
    parser.add_argument(
        "--nonce",
        metavar="NONCE",
        help="args.nonce for this run. Finds the file by content when no task id "
        "is in hand, and is REQUIRED to match when both are given.",
    )
    return parser


def main(argv=None, environ=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.task and not args.nonce:
        parser.error("give --task, --nonce, or both — there is nothing to resolve")
    receipt = materialize(args.task, args.nonce, args.output_dir, environ)
    try:
        line = escape_lone_surrogates(
            json.dumps(receipt, ensure_ascii=False, allow_nan=False)
        )
        ok = bool(receipt["ok"])
    except Exception as exc:  # noqa: BLE001 - stdout is NEVER empty
        line = _minimal_receipt_line(exc)
        ok = False
    sys.stdout.write(line + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
