#!/usr/bin/env python3
"""
assemble_artifacts.py — derive the projected code-gauntlet artifacts on disk.

Usage:
    python3 assemble_artifacts.py --plan <path>

A single invocation whose tokens are AST-safe (CLAUDE.md AST-safe emission: no
command substitution, heredocs, env prefixes, or shell operators), each
single-quoted only as the token needs it, so the executor agent can run it
inside a sandbox-auto-approved Bash call.

Why this script exists (issue #38, D3)
--------------------------------------
The workflow runtime has no disk access, so an artifact-writer agent persists the
pipeline's artifacts by value. Measured on a real run: of the 88,389 B that
crossed the writer prompt, the post-review artifact's findings array was
canonically byte-identical to findings.json, the checkpoint's
`phases.challenge.findings` was the alias-stripped twin of the same array, and
the genuine residual was 383 B. So the writer now persists only the UNIQUE
content (findings.json, report.md, this plan) and this script DERIVES the two
projections from what actually landed on disk.

The plan file
-------------
    {
      "planVersion": 2,
      "expect": [ { "path": ..., "chars": N, "checksum": "fnv1a32:0x........" } ],
      "derive": [ { "path": ..., "chars": N, "checksum": "fnv1a32:0x........" } ],
      "postReview": {
        "path": ..., "source": ...,
        "ids": [ ...ordered finding ids... ],
        "wrapper": { "owner", "repo", "pr_number", "sha", "review_body" } | null
      },
      "checkpoint": {
        "path": ..., "source": ...,
        "challengeFindingIds": [ ...ordered... ],
        "stripAliasFields": ["line", "end_line", "body"],
        "skeleton": { ...the checkpoint artifact minus phases.challenge.findings... }
      },
      "planChecksum": "fnv1a32:0x........"   <- LAST key; see below
    }

`wrapper: null` writes the post-review artifact as a bare array (the local-diff
shape); a present wrapper writes the post_review.py-ready envelope with the
projected findings appended.

`expect` describes the PRIMARIES this script reads; `derive` describes the two
DOCUMENTS THIS SCRIPT WRITES — the pipeline's own chars/checksum for the
post-review and checkpoint artifacts it holds in memory. This script does not
read `derive` (it derives from on-disk truth either way and reports what it
wrote, with chars/checksum, in `written[]`); the workflow compares the two and
treats a difference as a STRUCTURAL failure, because unlike a primary mismatch
there is no second copy of a derived document to fall back to. That comparison is
a consistency check against a stale or confused executor and against a
Python-vs-JS serializer divergence — NOT authentication: the plan is on disk, so
the numbers it names are readable by anything that can run this script.

The plan's self-proof (`planChecksum`)
--------------------------------------
The plan is transcribed to disk by the artifact-writer agent exactly like the two
primaries — but unlike them it is not DATA to be checked, it is the INSTRUCTION
SET. `postReview.ids` is the sole authority for which finding ids reach the
post-review artifact, so a writer that elides two entries from that list yields a
silently smaller delivered set, a self-consistent `ok:true` receipt, and no gap.
The primaries have `expect[].chars/checksum` to prove them; the plan had nothing.

So the plan carries a checksum of itself. The construction has to be unambiguous
in BOTH runtimes (the workflow sandbox computes it, this script recomputes it):

  1. Take the plan object exactly as parsed. Key order is the wire order — JS
     object literals and Python's `json.loads` dicts are both insertion-ordered,
     and neither runtime reorders on re-serialization.
  2. Remove the single key `planChecksum`. The pipeline appends it LAST (it is
     computed before the key exists), so removing it restores the exact
     pre-checksum object on both sides.
  3. Serialize with the shared pretty printer — `JSON.stringify(x, null, 2)` on
     the JS side, `js_stringify_pretty` here — and take `fnv1a32` of the result.

Because step 3 reuses the same serializer that produces the derived artifacts,
the plan checksum doubles as a canary for serializer divergence: if this file's
`js_stringify_pretty` ever stopped agreeing with `JSON.stringify` byte-for-byte
over the plan's own content, the plan checksum would fail before a single
divergent artifact was written.

WHY THE ID LISTS STAY. The obvious reaction to "the id list is the weak point" is
to replace it with a more robust encoding. Nothing removes the failure mode:
the plan has to cross an LLM agent's transcription to reach disk (the sandbox has
no disk access at all), and no encoding stops an agent from writing fewer bytes
than it was given. The two alternatives to an explicit list are both worse:
deriving the delivery set here would mean reimplementing selectDelivery's ranking
and cap in a second language — an order-sensitive stage whose divergence would
silently change the delivered findings, which is exactly the class of bug this
guard exists to prevent; and "all findings, in file order" is simply false, the
delivery set is a ranked, capped subset. So the list stays explicit and gains a
proof, and a plan that fails its proof is not executed.

Failure contract
----------------
* STRUCTURAL failures are HARD failures — exit non-zero, `ok:false`, and NOTHING
  is written: a missing file, unparseable JSON, a requested id absent from the
  source, duplicate ids in the source, a numeric value this script cannot
  reproduce byte-identically (see below), or a plan whose checksum does not
  recompute. This is the class that caught the #25 incident (tool-call markup
  appended after the JSON document).
* A CHECKSUM MISMATCH on one of the `expect` PRIMARIES is NOT a hard failure. The
  source of truth is what is actually on disk, and the derived artifacts are
  self-consistent with it, so derivation proceeds and the entry is stamped
  `content_proof: "mismatch"` with the expected/actual chars and checksum so the
  caller can surface it loudly. Refusing here would invent a new way to lose a
  run's artifacts, which is the opposite of the never-fabricate contract.
  A PLAN checksum mismatch is the opposite call: the plan is not data to derive
  from, it is the instruction set, and an untrustworthy instruction set must not
  be executed.
* NOTHING is ever left truncated at a planned path. Every derived document is
  serialized and encoded in memory first, then written to a sibling temp file and
  `os.replace()`d into place, so a failure at any layer leaves the destination as
  it was rather than as an empty file.

Output
------
EXACTLY one line of JSON on stdout (diagnostics go to stderr), on EVERY path
including an unexpected internal error:

    { "ok": bool, "planVersion": 2, "planChecksum": "fnv1a32:0x........"|null,
      "verified": [ { path, chars, expected_chars, checksum, expected_checksum,
                      content_proof: "match"|"mismatch" } ],
      "written":  [ { path, chars, checksum } ],
      "errors":   [ ... ] }

No external Python dependencies — stdlib only. Language-agnostic: nothing here
inspects the reviewed codebase.
"""

import argparse
import json
import os
import struct
import sys
import tempfile
from contextlib import suppress

PLAN_VERSION = 2
PLAN_CHECKSUM_KEY = "planChecksum"

# ---------------------------------------------------------------------------
# Checksum — fnv1a32 over UTF-16 code units
# ---------------------------------------------------------------------------
#
# The JS twin runs inside the workflow sandbox, which has NO TextEncoder and NO
# Buffer, so the only cheap byte-source available there is String#charCodeAt —
# i.e. UTF-16 code units. This implementation reproduces it exactly, including
# surrogate pairs (an emoji contributes TWO units on both sides).
#
#   JS: let h = 0x811c9dc5;
#       for (let i = 0; i < s.length; i++) {
#         h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0;
#       }
#       return 'fnv1a32:0x' + h.toString(16).padStart(8, '0');
#
# JS's `^=` coerces through ToInt32 (so h may read as negative there) but the
# 32-bit pattern is identical to the masked arithmetic below, and Math.imul is a
# language builtin — not a host global — so it IS available in the sandbox.

FNV_OFFSET_BASIS = 0x811C9DC5
FNV_PRIME = 0x01000193


def utf16_code_units(s):
    """The UTF-16 code units of `s`, exactly what JS charCodeAt() walks."""
    raw = s.encode("utf-16-le", "surrogatepass")
    return struct.unpack(f"<{len(raw) // 2}H", raw)


def utf16_len(s):
    """The UTF-16 code-unit count — JS's `s.length`, not len(s)."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def fnv1a32(s):
    """FNV-1a (32-bit) over UTF-16 code units, formatted as `fnv1a32:0x........`."""
    h = FNV_OFFSET_BASIS
    for unit in utf16_code_units(s):
        h ^= unit
        h = (h * FNV_PRIME) & 0xFFFFFFFF
    return f"fnv1a32:0x{h:08x}"


def normalize_content(s):
    """Strip a UTF-8 BOM and AT MOST ONE trailing newline (\\n or \\r\\n).

    The Write tool may normalise a trailing newline or prepend a BOM; a false
    content-proof degrade must not cost a run its artifacts. Applied to BOTH
    sides — the workflow computes the expected chars/checksum over the same
    normalisation — so the tolerance is symmetric. Two trailing newlines is a
    REAL difference and still reports as a mismatch.
    """
    if s.startswith("﻿"):
        s = s[1:]
    if s.endswith("\r\n"):
        return s[:-2]
    if s.endswith("\n"):
        return s[:-1]
    return s


# ---------------------------------------------------------------------------
# Serialization — byte-equivalent to JS JSON.stringify(obj, null, 2)
# ---------------------------------------------------------------------------


class JsSerializationError(ValueError):
    """A value this script cannot render byte-identically to JSON.stringify.

    Raised instead of writing a document that would diverge from what the
    pipeline holds in memory. Callers turn it into a STRUCTURAL failure.
    """


# JS numbers are IEEE-754 doubles and Number#toString has its own spelling rules;
# Python's repr(float) does not share them. The five live divergences:
#
#     value        JSON.stringify   json.dumps
#     1e-7         1e-7             1e-07      (exponent zero-padding)
#     0.000001     0.000001         1e-06      (Python switches to exponent at 1e-4,
#                                               JS only below 1e-6)
#     90.0         90               90.0       (JS never prints a trailing .0)
#     -0.0         0                -0.0
#     NaN          null             NaN        (and json.loads ACCEPTS bare NaN)
#
# DECISION: enforce integers as a PRECONDITION rather than reimplement
# Number#toString. Every number the pipeline puts in a finding or a checkpoint is
# a count, a line number, or a confidence — all integers — and every number in
# these documents originated as a JS `JSON.stringify` output, where an integral
# double is always spelled without a dot or exponent (so it parses back as a
# Python *int*, which round-trips exactly). A float or a NaN reaching here
# therefore means the input is not what the pipeline produced, and reproducing it
# faithfully would require a full Number#toString port whose own bugs would be
# invisible. Refusing is honest and, on the JS side, persistDerivable applies the
# same rule BEFORE anything is written and falls back to the legacy by-value
# writer — so this precondition costs a run nothing in practice.
#
# Integers outside JS's safe range are rejected for the same reason: JS would have
# parsed them lossily, so the two runtimes no longer hold the same value.
JS_MAX_SAFE_INTEGER = 2**53 - 1


def assert_js_reproducible(obj, path="$"):
    """Raise JsSerializationError for any value JSON.stringify would spell
    differently than json.dumps. Iterative — findings nest shallowly, but a
    hand-edited plan must not be able to blow the recursion limit."""
    stack = [(obj, path)]
    while stack:
        node, where = stack.pop()
        if node is None or isinstance(node, (bool, str)):
            continue
        if isinstance(node, int):
            if not (-JS_MAX_SAFE_INTEGER <= node <= JS_MAX_SAFE_INTEGER):
                raise JsSerializationError(
                    f"integer at {where} is outside JS's safe integer range ({node!r})"
                )
            continue
        if isinstance(node, float):
            raise JsSerializationError(
                f"non-integer number at {where} ({node!r}): JS and Python spell "
                "such numbers differently, so the derived artifact would diverge"
            )
        if isinstance(node, list):
            for i, item in enumerate(node):
                stack.append((item, f"{where}[{i}]"))
            continue
        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(key, str):
                    raise JsSerializationError(
                        f"non-string object key at {where} ({key!r})"
                    )
                stack.append((value, f"{where}.{key}"))
            continue
        raise JsSerializationError(
            f"value at {where} has no JSON representation ({type(node).__name__})"
        )


def escape_lone_surrogates(s):
    """Spell surrogate code points the way a well-formed JSON.stringify does.

    ES2019 made JSON.stringify "well-formed": a lone surrogate is emitted as a
    `\\uXXXX` escape rather than a raw code unit. `json.dumps(ensure_ascii=False)`
    emits it raw instead, which then (a) diverges from JS and (b) makes the
    result *unencodable* as UTF-8 — the crash that used to leave a zero-byte file
    at a planned path.

    Python's JSON decoder combines a well-formed pair into one astral character,
    so a surrogate reaching here is normally already lone; the pair branch below
    exists so the function is faithful to JS for any input, not just decoder
    output (JS sees the two code units as one astral character and emits it raw).
    """
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in s):
        return s
    out = []
    i = 0
    n = len(s)
    while i < n:
        cp = ord(s[i])
        if 0xD800 <= cp <= 0xDBFF and i + 1 < n and 0xDC00 <= ord(s[i + 1]) <= 0xDFFF:
            low = ord(s[i + 1])
            out.append(chr(0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00)))
            i += 2
            continue
        if 0xD800 <= cp <= 0xDFFF:
            out.append(f"\\u{cp:04x}")
            i += 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def js_stringify_pretty(obj):
    """JSON.stringify(obj, null, 2), byte for byte.

    ensure_ascii=False because JS never escapes non-ASCII (U+2028/U+2029 included
    — they are legal raw inside a JSON string and JSON.stringify leaves them
    alone), Python's indent mode already emits JS's separators, allow_nan=False
    so a non-finite number can never be spelled `NaN`/`Infinity` (JS emits
    `null`), and the surrogate pass restores JS's well-formed escaping.
    """
    assert_js_reproducible(obj)
    return escape_lone_surrogates(
        json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False)
    )


# ---------------------------------------------------------------------------
# Disk helpers
# ---------------------------------------------------------------------------


def read_text(path):
    """Return the normalized file content, or raise (IOError/OSError/ValueError —
    a non-UTF-8 byte on disk raises UnicodeDecodeError, which is a ValueError)."""
    with open(path, encoding="utf-8", newline="") as fh:
        return normalize_content(fh.read())


def write_text_atomic(path, text):
    """Write `text` verbatim (no trailing newline, so the bytes on disk are
    exactly the string whose checksum the receipt reports) via a sibling temp
    file + os.replace().

    Opening the destination directly would truncate it BEFORE the encode, so any
    failure mid-write leaves a zero-byte file at a planned path — a truncated
    artifact that later stages would read as real. os.replace() is atomic within
    a filesystem, so the destination is either its old content or the complete
    new content, never a prefix.
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(
        prefix=".code-gauntlet-assemble-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        # mkstemp creates 0600; os.replace would carry that onto the artifact, which
        # a later CI step running as another user could no longer read. Restore the
        # mode a plain open() would have produced.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise
    try:
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# The assembler
# ---------------------------------------------------------------------------


def _receipt(ok, plan_version, plan_checksum, verified, written, errors):
    return {
        "ok": ok,
        "planVersion": plan_version,
        "planChecksum": plan_checksum,
        "verified": verified,
        "written": written,
        "errors": errors,
    }


def plan_checksum(plan):
    """The plan's self-proof, recomputed from the plan actually read.

    Construction (documented at module level): the plan object as parsed, MINUS
    the single `planChecksum` key (the pipeline appends it last, so deleting it
    restores the exact object the pipeline checksummed), serialized with the
    shared pretty printer and hashed with fnv1a32.
    """
    body = dict((k, v) for (k, v) in plan.items() if k != PLAN_CHECKSUM_KEY)
    return fnv1a32(js_stringify_pretty(body))


def _load_source(path, cache, errors):
    """Load and index one findings source file. Returns the id -> finding index, or None.

    Every failure here is STRUCTURAL: unreadable, unparseable, not an array, an
    entry without a usable id, or duplicate ids.
    """
    if path in cache:
        return cache[path]
    cache[path] = None
    try:
        raw = read_text(path)
    except Exception as exc:  # noqa: BLE001 - includes UnicodeDecodeError (a ValueError)
        errors.append(f"source not found or unreadable: {path} ({exc})")
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        errors.append(f"source is not valid JSON: {path} ({exc})")
        return None
    if not isinstance(data, list):
        errors.append(f"source must be a JSON array of findings: {path}")
        return None
    by_id = {}
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


def _project(by_id, ids, source_path, label, errors):
    """Project `ids` (in order) out of the indexed source. A missing id is a
    structural failure — the plan and the file on disk disagree."""
    out = []
    for fid in ids:
        if fid not in by_id:
            errors.append(f"{label} id {fid!r} not present in source {source_path}")
            continue
        out.append(by_id[fid])
    return out


def _strip_aliases(finding, alias_fields):
    """Drop the v2 alias fields the persist boundary added, preserving the key
    ORDER of the remaining fields (the aliases were appended last, so removing
    them restores the canonical shape byte-for-byte)."""
    return dict((k, v) for (k, v) in finding.items() if k not in alias_fields)


def _serialize(document, label, errors):
    """js_stringify_pretty with every failure turned into a structural error."""
    try:
        return js_stringify_pretty(document)
    except Exception as exc:  # noqa: BLE001 - converted to a structural error
        errors.append(
            f"could not serialize the {label} artifact: {type(exc).__name__}: {exc}"
        )
        return None


def _assemble(plan_path):
    """Verify the primaries, derive the projections, return the receipt dict."""
    errors = []
    verified = []

    try:
        with open(plan_path, encoding="utf-8", newline="") as fh:
            plan_raw = fh.read()
    except Exception as exc:  # noqa: BLE001 - converted to a structural error
        errors.append(f"plan not found or unreadable: {plan_path} ({exc})")
        return _receipt(False, None, None, verified, [], errors)
    try:
        plan = json.loads(normalize_content(plan_raw))
    except ValueError as exc:
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

    # 0. The plan's self-proof. It is the INSTRUCTION SET, not data to derive
    #    from — `postReview.ids` alone decides which findings reach the delivered
    #    artifact — so an unproven or altered plan is a STRUCTURAL failure and is
    #    never executed. (Contrast the `expect` primaries below, where a mismatch
    #    is loud but non-fatal because on-disk truth is still derivable.)
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

    # 1. Verify the primaries the writer agent persisted. A structural problem
    #    here (missing file, unparseable JSON) is fatal; a content difference is
    #    recorded and derivation continues from the on-disk truth.
    for entry in plan.get("expect", []) or []:
        path = entry.get("path")
        try:
            content = read_text(path)
        except Exception as exc:  # noqa: BLE001 - converted to a structural error
            errors.append(f"expected artifact not found or unreadable: {path} ({exc})")
            continue
        if isinstance(path, str) and path.endswith(".json"):
            try:
                json.loads(content)
            except ValueError as exc:
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

    # 2. Build both derived documents IN MEMORY before writing anything, so a
    #    structural failure leaves the output directory untouched.
    cache = {}
    pending = []

    post = plan.get("postReview") or {}
    post_source = post.get("source")
    by_id = _load_source(post_source, cache, errors)
    if by_id is not None:
        projected = _project(
            by_id, post.get("ids") or [], post_source, "postReview", errors
        )
        wrapper = post.get("wrapper")
        if wrapper is None:
            document = projected
        else:
            document = dict(wrapper)
            document["findings"] = projected
        text = _serialize(document, "post-review", errors)
        if text is not None:
            pending.append((post.get("path"), text))

    cp = plan.get("checkpoint") or {}
    cp_source = cp.get("source")
    cp_by_id = _load_source(cp_source, cache, errors)
    if cp_by_id is not None:
        alias_fields = set(cp.get("stripAliasFields") or [])
        cp_ids = cp.get("challengeFindingIds") or []
        cp_projected = [
            _strip_aliases(f, alias_fields)
            for f in _project(cp_by_id, cp_ids, cp_source, "challenge", errors)
        ]
        skeleton = json.loads(json.dumps(cp.get("skeleton") or {}))
        challenge = (skeleton.get("phases") or {}).get("challenge")
        # MIRROR THE JS GUARD EXACTLY (persistPlan in workflows/src/stages.js):
        # `challenge && Array.isArray(challenge.findings)`. The pipeline empties that
        # ARRAY into the skeleton only when it held one; where it held a non-array (or
        # no `findings` key at all) the skeleton carries that verbatim and the derived
        # checkpoint must too. A looser predicate here FABRICATES a
        # `phases.challenge.findings: []` the pipeline never had — the two runtimes
        # would then disagree about a document neither can cross-check.
        has_findings_array = isinstance(challenge, dict) and isinstance(
            challenge.get("findings"), list
        )
        if has_findings_array:
            # Assigning an EXISTING key preserves its position, so the derived
            # checkpoint is key-order-identical to the in-memory one.
            challenge["findings"] = cp_projected
        elif cp_ids:
            errors.append(
                "checkpoint skeleton has no phases.challenge.findings array to receive "
                f"{len(cp_ids)} challenge finding(s)"
            )
        text = _serialize(skeleton, "checkpoint", errors)
        if text is not None:
            pending.append((cp.get("path"), text))

    if errors:
        return _receipt(False, plan_version, actual, verified, [], errors)

    # 3. Write. Every derived document is already serialized, so this loop cannot
    #    fail halfway on a data problem — and each write is temp-file + replace,
    #    so it cannot leave a truncated file at a planned path either.
    written = []
    for path, text in pending:
        try:
            write_text_atomic(path, text)
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


def assemble(plan_path):
    """_assemble, with a last-resort guard so the caller ALWAYS gets a receipt.

    The one-line-receipt contract is what the executor agent returns to the
    workflow; a traceback on stdout/stderr with an empty stdout is indistinguishable
    from a dead executor and costs the run its artifacts silently. Every expected
    failure is already handled above — this catches the unexpected one and still
    reports it honestly as ok:false.
    """
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


def _minimal_receipt_line(exc):
    """A hand-built one-line receipt for when the real one will not serialize.

    The last hop of the one-line-receipt contract. `assemble()` guarantees a
    receipt DICT on every path, but a dict is not yet a line: values copied out of
    a hand-edited plan reach the receipt as-is (`expected_chars` is whatever the
    plan said), and `json.loads` accepts bare `NaN`/`Infinity`, which
    `allow_nan=False` then refuses to spell. That raised through main() as a
    traceback with EMPTY stdout — indistinguishable to the executor from a dead
    script, which costs the run its artifacts silently.

    Built with ensure_ascii=True over a single interpolated string so nothing from
    the failed receipt can reproduce the failure; the literal is the last resort if
    even that does not hold.
    """
    try:
        return json.dumps(
            {
                "ok": False,
                "planVersion": None,
                "planChecksum": None,
                "verified": [],
                "written": [],
                "errors": [
                    f"receipt could not be serialized: {type(exc).__name__}: {exc}"
                ],
            }
        )
    except Exception:  # noqa: BLE001 - the one-line-receipt contract
        return (
            '{"ok": false, "planVersion": null, "planChecksum": null, '
            '"verified": [], "written": [], '
            '"errors": ["receipt could not be serialized"]}'
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Derive the projected code-gauntlet artifacts from a persist plan."
    )
    parser.add_argument("--plan", required=True, help="path to the persist plan JSON")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    receipt = assemble(args.plan)
    # EXACTLY one line of JSON on stdout — the executor returns it verbatim. The
    # receipt itself is plain ASCII-safe data, but errors can quote arbitrary
    # source content, so it goes through the same surrogate-safe serializer. If even
    # that fails, a minimal hand-built line goes out instead: stdout is NEVER empty.
    try:
        line = escape_lone_surrogates(
            json.dumps(receipt, ensure_ascii=False, allow_nan=False)
        )
        ok = bool(receipt["ok"])
    except Exception as exc:  # noqa: BLE001 - stdout is never empty
        line = _minimal_receipt_line(exc)
        ok = False
    sys.stdout.write(line + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
