#!/usr/bin/env python3
"""
filter_findings.py — Deterministic Phase 6 filtering for code-gauntlet.

Usage:
    python3 filter_findings.py <findings_json> [--review-md path] [--exclusions-md path]

Arguments:
    findings_json     Path to verified findings JSON (from verify_findings.py or Phase 5 output).
    --review-md       Path to REVIEW.md for custom thresholds and ignore patterns.
                      When omitted, built-in defaults are used.
    --exclusions-md   Path to false-positive-exclusions.md.
                      When omitted, no exclusion filtering is applied.

Input JSON schema:
    A JSON object or array of verified findings. When an object is given, the
    "findings" key is read. Each finding must have at minimum:
        {
            "id":          "unique string",
            "file":        "src/foo.py",
            "line_start":  42,
            "line_end":    45,             # optional
            "severity":    "critical|high|medium|low",
            "confidence":  85,             # 0-100 integer
            "title":       "...",
            "description": "...",
            "origin":      "new|surfaced", # optional, set by verify_findings.py
            "dimension":   "security",     # optional, single string
            "agent":       "security-reviewer"  # optional, used for disagreement detection
        }

Output JSON schema:
    {
        "filtered": [...],    # findings that passed all filters, tagged for output
        "eliminated": [...],  # findings removed by any filter, with "eliminated_by" field
        "stats": {
            "total":                   N,   # total input findings
            "passed_threshold":        N,   # passed confidence + severity threshold
            "contested_count":         N,   # findings that bypassed threshold via validator contestation
            "exclusions_removed":      N,   # removed by exclusion filter (REVIEW.md ignore / false-positive-exclusions.md)
            "injections_removed":      N,   # removed by injection filter
            "suggestions_removed":     N,   # kept findings whose suggestion field was stripped by injection scan
            "claude_md_rules_removed": N,   # kept findings whose claude_md_rule field was stripped by injection scan
            "spec_texts_removed":      N,   # kept findings whose spec_text field was stripped by injection scan
            "suggested_fix_codes_removed": N, # kept findings whose suggested_fix_code field was stripped by injection scan
            "consensus_boosted":       N,   # confidence boosted for co-location (same file + 10-line bucket, any agent)
            "singleton_penalized":     N,   # singleton findings penalized -15 confidence (non-core dims)
            "dimension_routed":        N,   # findings routed to suggestion by dimension (BF-15a)
            "reachability_demoted":    N,   # findings stamped by reachability demotion
            "cross_agent_consolidated": N,   # cross-agent findings stamped (never dropped) with consolidation_key
            "test_analyzer_promoted":  N,   # test-analyzer findings promoted to main report
            "tagged_main":             N,   # tagged for main report
            "tagged_suggestion":       N    # tagged as improvement suggestions
        }
    }

Each filtered finding includes:
    "report_destination":  "main" | "suggestion"  # routing destination for Phase 8
    "report_tag":          "main" | "suggestion"  # backward-compatible alias for report_destination
    "suggestion_removed_by":     "injection"  # present only when the suggestion field was stripped
    "suggestion_removal_reason": "..."        # present only when suggestion_removed_by is present
    # "suggestion" itself may be absent after filtering -- it is deleted, not
    # nulled, whenever suggestion_removed_by is present.
    "claude_md_rule_removed_by":     "injection"  # present only when claude_md_rule was stripped
    "claude_md_rule_removal_reason": "..."        # present only when claude_md_rule_removed_by is present
    "spec_text_removed_by":     "injection"  # present only when spec_text was stripped
    "spec_text_removal_reason": "..."        # present only when spec_text_removed_by is present
    # "claude_md_rule"/"spec_text" follow the same delete-not-null contract as
    # "suggestion" above (#213 extends the #62 strip mechanism to these two
    # repo-derived citation fields).
    "suggested_fix_code_removed_by":     "injection"  # present only when suggested_fix_code was stripped
    "suggested_fix_code_removal_reason": "..."        # present only when suggested_fix_code_removed_by is present
    # "suggested_fix_code" itself may be absent after filtering -- it is
    # deleted, not nulled, whenever suggested_fix_code_removed_by is present
    # (#63/D8, mirrors the suggestion contract above).
    "original_severity": <severity before a reachability demotion, when changed>
    "demoted_by": "reachability"  # present for future_change_only validator findings
    "demotion_reason": "validator found no code path that reaches this issue without a future change"

REVIEW.md parsing:
    Looks for a fenced code block or YAML-style section containing:
        confidence_threshold: 70
        security_min_confidence: 70
        severity_threshold: medium
        ignore:
          - pattern to ignore

No external Python dependencies -- stdlib only.
"""

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from script_io import write_result

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Typed-field coercion (#266)
# ---------------------------------------------------------------------------
#
# A scanned finding field must contribute a value of its expected type or
# that type's default -- never a stringified null, never a crash. Applied at
# every scan/concat/sort-key/threshold site that reads title/description/
# file/severity (string-typed) or confidence (numeric-typed) from a finding
# whose provenance is not schema-validated: the retained Python CLI's
# unvalidated --input/checkpoint resume, and a replayed checkpoint from an
# earlier pipeline version. Before this, `finding.get(field, "")` only
# substitutes the default when the KEY IS ABSENT -- an explicit `null` (the
# common shape) passes the raw None through, which raises TypeError the
# moment it reaches `re.search`, a `+` string concatenation, a `.lower()`
# call, or an ordering comparison (`<`), or renders as the literal text
# "None" in an f-string. `severity` additionally keeps its historical
# "default to low" fallback: `_as_text(value) or "low"`, not a bare
# `_as_text(value)`, so an empty or non-string severity still becomes "low"
# rather than "". Mirrors the JS twin's `asText`/`asConfidence`
# (workflows/src/filterFindings.js).


def _as_text(value):
    """Return value unchanged if it is a str, else "" -- the shared string-
    typed coercion for a scanned title/description/file field."""
    return value if isinstance(value, str) else ""


def _as_confidence(value):
    """Return value unchanged if it is a real numeric type (int/float,
    excluding bool -- Python's bool is an int subclass but is never a
    legitimate confidence score), else 0 -- the shared numeric-typed
    coercion for a scanned confidence field. NaN is also mapped to 0 (a
    NaN passes the isinstance(float) check but fails every ordering
    comparison, which would otherwise let a "malformed" confidence dodge
    both the injection heuristics and the threshold filter silently) --
    mirrors the JS twin's `asConfidence` (workflows/src/filterFindings.js),
    whose `!Number.isNaN(value)` guard this brings Python to parity with."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return 0
        return value
    return 0


# Gate for the string branch of `_py_int_or_none` (#244 (b)). Python's own
# `int(str)` strips a wider whitespace set than JS `parseInt` and accepts
# non-ASCII decimal digits and PEP-515 `_` separators, so the two twins bucketed
# the same `line_start` differently. This pins the string form to the ONE union
# whitespace class + ASCII `[0-9]`, and `int()` runs on the CAPTURE (never the
# raw string), matching the JS twin's `parseInt(m[1], 10)`. Registry-sourced (an
# INLINE_SITES row); INERT (no `\d`/`\w`/`\s`), so no re.ASCII is required.
_INT_COERCE_RE = re.compile(
    r"^[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*([+-]?[0-9]+)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*$"
)

# Leading/trailing trim of the union whitespace class (#244 (a)). ONE constant
# shared by four call sites: the dedup-signature title strip AND the three
# review-line strips (load_exclusions' fenced block + bullet fallback, and
# parse_review_md's ignore item), all of which had a per-line `str.strip()` whose
# JS twin `trim()` disagreed on the same six codepoints -- silently zeroing a
# user exclusion/ignore pattern that carried one. `.sub` is inherently global;
# the JS twin uses `.replace(/.../g, '')`. Registry-sourced (an INLINE_SITES row).
_WS_TRIM_RE = re.compile(
    r"^[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+|[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+$"
)


def _py_int_or_none(value):
    """Python int() truncation semantics for the value types plausibly found on
    ``line_start``, mirroring the JS twin ``pyIntOrNull`` branch-for-branch:
    bool -> 0/1 (checked BEFORE int, since bool is an int subclass), int -> the
    value, float -> trunc toward zero when finite else None, str -> the
    ``_INT_COERCE_RE`` gate then ``int()`` of the CAPTURE, anything else -> None
    (a ``TypeError`` from Python's own ``int()``). Returns None on "would raise"
    so ``_line_bucket`` falls back to 0 exactly like the old
    ``except (TypeError, ValueError): return 0``. Also type-guards a non-str
    ``line_start`` (float/None/bool) that a bare ``.strip()`` would crash on."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return math.trunc(value) if math.isfinite(value) else None
    if isinstance(value, str):
        m = _INT_COERCE_RE.match(value)
        return int(m.group(1)) if m else None
    return None


def _line_bucket(line, proximity):
    """Round ``line`` to the nearest multiple of ``proximity`` to group nearby
    findings, sharing ONE coercion path across ``detect_disagreement``
    (proximity 10) and ``group_by_proximity`` / ``consolidate_cross_agent``
    (proximity 5). The JS twin ``lineBucket`` already had a single path; this
    converges Python's two former nested closures (``_line_bucket`` and
    ``_bucket``) onto it, so no bucket site can drift again. ``round()`` is
    Python's native banker's rounding, matching the JS ``pyRound`` half-to-even
    port."""
    n = _py_int_or_none(line)
    if n is None:
        return 0
    return round(n / proximity) * proximity


# ---------------------------------------------------------------------------
# Input normalization (BF-14)
# ---------------------------------------------------------------------------

# Legacy field names mapped to their canonical equivalents.
# The pipeline uses "description", "line_start", and "origin" internally.
# Agents or orchestrators occasionally emit the legacy names.
_FIELD_RENAMES = {
    "body": "description",
    "line": "line_start",
    "blame_tag": "origin",
}


def normalize_field_names(findings):
    """
    Normalize legacy field names to the canonical pipeline schema.

    For each finding:
      - ``body`` -> ``description`` (only when ``description`` is absent)
      - ``line`` -> ``line_start`` (only when ``line_start`` is absent)
      - ``blame_tag`` -> ``origin`` (only when ``origin`` is absent)

    When a rename is applied, the legacy key is removed and a WARNING is
    logged to stderr.  If both the legacy and canonical key exist, the
    canonical value is preserved and the legacy key is left untouched.

    Returns the number of findings that had at least one field renamed.
    """
    normalized_count = 0

    for finding in findings:
        renamed_fields = []
        for legacy, canonical in _FIELD_RENAMES.items():
            if legacy in finding and canonical not in finding:
                finding[canonical] = finding.pop(legacy)
                renamed_fields.append(f"{legacy}->{canonical}")
        if renamed_fields:
            normalized_count += 1
            fid = finding.get("id", "?")
            warn(
                f"[normalize] Finding {fid!r}: renamed legacy fields: "
                + ", ".join(renamed_fields)
            )

    if normalized_count:
        warn(
            f"[normalize] Normalized legacy field names on "
            f"{normalized_count}/{len(findings)} finding(s)."
        )

    return normalized_count


# ---------------------------------------------------------------------------
# REVIEW.md parser
# ---------------------------------------------------------------------------

# Severity ordering for threshold comparisons (lower index = higher severity)
SEVERITY_ORDER = ["critical", "high", "medium", "low"]

# Default thresholds used when REVIEW.md is absent or does not specify them.
# As of issue #94 F7, parse_review_md() does NOT pre-fill these into its returned
# dict -- it only sets confidence_threshold/security_min_confidence/severity_threshold
# when the corresponding key is actually found (see parse_review_md's docstring and
# the missing_defaults / commented_keys_ignored parity fixtures, both of which now
# return a config with those keys absent). These constants back the *fallback*
# apply_threshold_filter applies via config.get(key, DEFAULT) when the key is
# missing: DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD (55) for non-security
# dimensions, DEFAULT_CONFIDENCE_THRESHOLD (70) for the security branch.
DEFAULT_CONFIDENCE_THRESHOLD = 70
DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD = 55
DEFAULT_SECURITY_MIN_CONFIDENCE = 70
DEFAULT_SEVERITY_THRESHOLD = "low"  # pass all severities by default
REVIEW_SETTING_KEYS = (
    "confidence_threshold",
    "security_min_confidence",
    "severity_threshold",
)

# Contestation: if the validator dropped confidence by more than this amount,
# the finding is marked as contested and bypasses the threshold check.
_CONTESTATION_DROP_THRESHOLD = 25


def _strip_matching_quotes(item):
    """
    Ignore entries are raw substrings matched against a finding's title+description
    (apply_exclusions, below). Written REVIEW.md examples wrap the pattern in quotes
    for readability (`- "console.log in dev mode"`) -- strip ONE matching pair of
    surrounding quotes (single or double) so the stored pattern is the bare
    substring, not a string that includes the quote characters (which would then
    never appear in an unquoted finding title/description and silently never
    match; issue #94 adversarial review F2). Only a single matching pair strips --
    an entry that is not quote-wrapped, or whose quotes don't match, passes
    through untouched.
    """
    if len(item) >= 2 and item[0] == item[-1] and item[0] in ("'", '"'):
        return item[1:-1]
    return item


def _split_review_lines(text):
    """Split ``text`` on the universal-newline alternation ``\r\n | \r | \n``.

    The converged twin line splitter (issue #243). Replaces Python's
    ``str.splitlines()`` (which also breaks on U+000B/U+000C/U+001C-1E/U+0085/
    U+2028/U+2029) and the JS twin's ``split('\n')`` / ``split(/\r?\n/)`` -- the
    two twins now agree byte-for-byte on every line boundary. The alternation
    order (``\r\n`` first) reproduces Python ``open()``'s universal-newline
    translation exactly, so a lone ``\r``, a ``\r\n``, and a ``\n`` all break the
    line identically in both engines (``re.split`` here and JS
    ``text.split(/\r\n|\r|\n/)``), while a U+2028/U+000B/U+0085 stays inside the
    line. This subsumes the old "strip one trailing ``\r``" step: ``\r\n`` still
    collapses to one break, and a lone ``\r`` -- which the Python file-read path
    never delivers (``open()`` already translated it) but the JS twin sees raw --
    now converges instead of surviving inside the line.
    """
    return re.split(r"\r\n|\r|\n", text)


# The config-parser pattern declarations (issue #243) are GENERATED from
# scripts/filter_patterns_registry.py -- edit the registry, then run
# scripts/generate_filter_patterns.py. ``.`` under DOTALL / ``[\s\S]`` became
# ``[^\x00]`` (cross-twin symmetric, NOT behavior-preserving against a NUL in the
# block body); re.ASCII rides on the case-folding markers so a homoglyph key like
# a homoglyph key (dotless-i U+0131 in "deep-review") no longer matches
# (converged with the JS ``/i`` twin).
# generated-from-filter-pattern-registry:_REVIEW_BLOCK_PATTERNS do not edit; run scripts/generate_filter_patterns.py
_REVIEW_BLOCK_PATTERNS = [
    r"```(?:yaml|)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*#?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*code-gauntlet(?:[^\n]*)?\n([^\x00]*?)```",
    r"<!--[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*code-gauntlet-config[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\n([^\x00]*?)-->",
    r"```(?:yaml|)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*#?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*deep-review(?:[^\n]*)?\n([^\x00]*?)```",
    r"<!--[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*deep-review-config[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\n([^\x00]*?)-->",
]
# /generated-from-filter-pattern-registry:_REVIEW_BLOCK_PATTERNS
# generated-from-filter-pattern-registry:_REVIEW_CONFIDENCE_RE do not edit; run scripts/generate_filter_patterns.py
_REVIEW_CONFIDENCE_RE = re.compile(
    r"(?:^|\n)[ \t]*confidence_threshold[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[:=][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*([0-9]{1,3})(?![0-9])",
    re.IGNORECASE | re.ASCII,
)
# /generated-from-filter-pattern-registry:_REVIEW_CONFIDENCE_RE
# generated-from-filter-pattern-registry:_REVIEW_SECURITY_RE do not edit; run scripts/generate_filter_patterns.py
_REVIEW_SECURITY_RE = re.compile(
    r"(?:^|\n)[ \t]*security_min_confidence[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[:=][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*([0-9]{1,3})(?![0-9])",
    re.IGNORECASE | re.ASCII,
)
# /generated-from-filter-pattern-registry:_REVIEW_SECURITY_RE
# generated-from-filter-pattern-registry:_REVIEW_SEVERITY_RE do not edit; run scripts/generate_filter_patterns.py
_REVIEW_SEVERITY_RE = re.compile(
    r"(?:^|\n)[ \t]*severity_threshold[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[:=][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*(critical|high|medium|low)",
    re.IGNORECASE | re.ASCII,
)
# /generated-from-filter-pattern-registry:_REVIEW_SEVERITY_RE
# generated-from-filter-pattern-registry:_REVIEW_IGNORE_RE do not edit; run scripts/generate_filter_patterns.py
_REVIEW_IGNORE_RE = re.compile(
    r"(?:^|\n)[ \t]*ignore[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*:[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\n((?:[ \t]*-[^\n]*\n?)+)",
    re.IGNORECASE | re.ASCII,
)
# /generated-from-filter-pattern-registry:_REVIEW_IGNORE_RE
# generated-from-filter-pattern-registry:_REVIEW_IGNORE_ITEM_RE do not edit; run scripts/generate_filter_patterns.py
_REVIEW_IGNORE_ITEM_RE = re.compile(
    r"^[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*-[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*",
    re.ASCII,
)
# /generated-from-filter-pattern-registry:_REVIEW_IGNORE_ITEM_RE
# generated-from-filter-pattern-registry:_REVIEW_EXCL_BLOCK_RE do not edit; run scripts/generate_filter_patterns.py
_REVIEW_EXCL_BLOCK_RE = re.compile(
    r"```[^\n]*\n([^\x00]*?)```",
    re.ASCII,
)
# /generated-from-filter-pattern-registry:_REVIEW_EXCL_BLOCK_RE
# generated-from-filter-pattern-registry:_REVIEW_EXCL_BULLET_RE do not edit; run scripts/generate_filter_patterns.py
_REVIEW_EXCL_BULLET_RE = re.compile(
    r"^[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[-*][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+([^\n]+)$",
    re.ASCII,
)
# /generated-from-filter-pattern-registry:_REVIEW_EXCL_BULLET_RE


def parse_review_md_text(text, warning_path=None):
    """Parse REVIEW.md text without file I/O or warnings.

    Threshold keys are present only when the text sets them, and ``ignore`` is
    always present. This is the pure parser used by the scoped config builder.
    """
    config: dict[str, Any] = {"ignore": []}

    # Match a YAML-style code-gauntlet config block (patterns in
    # _REVIEW_BLOCK_PATTERNS). Markers are matched case-insensitively but
    # ASCII-folded (re.ASCII), so a homoglyph marker (dotless-i U+0131 in
    # "deep-review") no longer matches -- converged with the JS `/i` twin.
    block_text = ""
    for pattern in _REVIEW_BLOCK_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE | re.ASCII)
        if m:
            block_text = m.group(1)
            break

    # Also scan the whole file for bare key: value lines if no block found
    if not block_text:
        if warning_path is not None:
            warn(
                f"REVIEW.md at {warning_path!r}: no code-gauntlet config block found; falling back to whole-file scan."
            )
        block_text = text

    # Every key regex is anchored to a line start via `(?:^|\n)` (converged with
    # the JS twin: `/m` broke a line after \r/U+2028/U+2029, this only breaks
    # after \n or string start). A `#` before the key -- a commented-out example
    # line, e.g. `# confidence_threshold: 70` in a scaffolding template -- is not
    # in the `[ \t]*` leading-whitespace class, so it breaks the anchor and the
    # line is correctly ignored (issue #94 adversarial review F1).
    #
    # confidence_threshold / security_min_confidence are bounded to a 1-3 digit
    # ASCII run and accepted only when <= 100 (review-md-spec.md `<0-100>`): a
    # value above 100 is ignored (defaults apply). This closes the measured
    # int()-vs-parseInt() divergence on out-of-range values (>2^53 spelled
    # `1e+21` in JS, an exact int in Python) and the unicode-\d divergence
    # (Arabic-Indic digits U+0667 U+0665 matched Python's `\d` but never JS's
    # ASCII `\d`).
    m = _REVIEW_CONFIDENCE_RE.search(block_text)
    if m:
        value = int(m.group(1))
        if value <= 100:
            config["confidence_threshold"] = value

    m = _REVIEW_SECURITY_RE.search(block_text)
    if m:
        value = int(m.group(1))
        if value <= 100:
            config["security_min_confidence"] = value

    m = _REVIEW_SEVERITY_RE.search(block_text)
    if m:
        config["severity_threshold"] = m.group(1).lower()

    # ignore list -- lines after "ignore:" that start with "  -" or "- ". The
    # `ignore:` anchor itself: same rationale, `[ \t]*` before it, never `#`.
    ignore_section = _REVIEW_IGNORE_RE.search(block_text)
    if ignore_section:
        for line in _split_review_lines(ignore_section.group(1)):
            item = _WS_TRIM_RE.sub("", _REVIEW_IGNORE_ITEM_RE.sub("", line))
            if item:
                config["ignore"].append(_strip_matching_quotes(item))

    return config


def parse_review_md(path):
    """Read and parse one REVIEW.md file, preserving file warnings."""
    try:
        with open(path) as fh:
            text = fh.read()
    except FileNotFoundError:
        warn(f"REVIEW.md not found at {path!r}; using default thresholds.")
        return {"ignore": []}
    except OSError as e:
        warn(f"Could not read REVIEW.md: {e}; using default thresholds.")
        return {"ignore": []}

    return parse_review_md_text(text, warning_path=path)


def _merge_review_layer(layer, parsed):
    for key in REVIEW_SETTING_KEYS:
        if key in parsed:
            layer[key] = parsed[key]
    layer["ignore"].extend(parsed["ignore"])


def build_review_config(entries):
    """Build root and subtree REVIEW.md layers from raw path/text entries."""
    root = {"ignore": []}
    scopes = []
    for index, entry in enumerate(entries):
        path = entry["path"]
        slash = path.rfind("/")
        directory = "" if slash < 0 else path[:slash]
        parsed = parse_review_md_text(entry["text"])
        if not directory:
            _merge_review_layer(root, parsed)
            continue
        scope = next((item for item in scopes if item["dir"] == directory), None)
        if scope is None:
            scope = {"dir": directory, "ignore": [], "_index": index}
            scopes.append(scope)
        _merge_review_layer(scope, parsed)
    scopes.sort(key=lambda item: (item["dir"].count("/") + 1, item["_index"]))
    for scope in scopes:
        del scope["_index"]
    if scopes:
        root["scopes"] = scopes
    return root


def _scope_matches_file(scope, file):
    return (
        isinstance(file, str)
        and isinstance(scope, dict)
        and isinstance(scope.get("dir"), str)
        and scope["dir"] != ""
        and file.startswith(scope["dir"] + "/")
    )


def config_for_file(config, file):
    """Return a fresh flat view from root plus matching subtree layers."""
    source = config or {}
    # parse_review_md_text initializes config = {"ignore": []}; the JS args waist
    # validates this shape on the pipeline path.
    view = {"ignore": list(source.get("ignore", []))}
    for key in REVIEW_SETTING_KEYS:
        if key in source:
            view[key] = source[key]
    scopes = source.get("scopes")
    if not isinstance(scopes, list) or not isinstance(file, str):
        return view
    matching = [
        (index, scope)
        for index, scope in enumerate(scopes)
        if _scope_matches_file(scope, file)
    ]
    matching.sort(key=lambda item: (item[1]["dir"].count("/") + 1, item[0]))
    for _, scope in matching:
        for key in REVIEW_SETTING_KEYS:
            if key in scope:
                view[key] = scope[key]
        view["ignore"].extend(scope.get("ignore", []))
    return view


# ---------------------------------------------------------------------------
# Filter: confidence / severity threshold (with validator contestation)
# ---------------------------------------------------------------------------


def apply_threshold_filter(findings, config):
    """
    Remove findings that fall below the thresholds selected for each finding file.

    A finding passes if:
      - confidence >= config["confidence_threshold"]
        (security dimensions use min(confidence_threshold, security_min_confidence) —
        i.e. security_min_confidence can only LOWER the bar, never raise it)
      - severity is at or above config["severity_threshold"] in SEVERITY_ORDER

    Validator contestation (V5-09C):
      If a finding has ``original_confidence`` (set before Phase 5 validation)
      and the validator dropped confidence by more than 25 points
      (original_confidence - confidence > 25), the finding is marked as
      **contested** and bypasses the confidence threshold check. This prevents
      an overly aggressive validator from silently killing legitimate findings.

      Contested findings gain:
        - contested: True
        - contestation_drop: N  (how many points the validator removed)
        - contestation_reason: human-readable explanation

    Returns (passed, eliminated, contested_count) where contested_count is
    the number of findings that bypassed the threshold via contestation.
    """
    passed = []
    eliminated = []
    contested_count = 0

    for finding in findings:
        file_config = config_for_file(config, finding.get("file"))
        confidence = _as_confidence(finding.get("confidence"))
        severity = (_as_text(finding.get("severity")) or "low").lower()
        dimensions = (
            [finding.get("dimension", "").lower()] if finding.get("dimension") else []
        )

        # Determine effective confidence threshold
        is_security = "security" in dimensions
        if is_security:
            min_conf = file_config.get(
                "security_min_confidence", DEFAULT_SECURITY_MIN_CONFIDENCE
            )
            effective_threshold = min(
                file_config.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD),
                min_conf,
            )
        else:
            effective_threshold = file_config.get(
                "confidence_threshold", DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD
            )

        # -----------------------------------------------------------------
        # Validator contestation check (V5-09C)
        # -----------------------------------------------------------------
        is_contested = False
        original_confidence = finding.get("original_confidence")
        if original_confidence is not None:
            drop = original_confidence - confidence
            if drop > _CONTESTATION_DROP_THRESHOLD:
                is_contested = True
                contested_count += 1
                finding["contested"] = True
                finding["contestation_drop"] = drop
                finding["contestation_reason"] = (
                    f"validator dropped confidence by {drop} points "
                    f"(original: {original_confidence}, current: {confidence})"
                )

        # Check confidence (contested findings bypass this check)
        if not is_contested and confidence < effective_threshold:
            elim = dict(finding)
            elim["eliminated_by"] = "threshold"
            elim["elimination_reason"] = (
                f"confidence {confidence} < threshold {effective_threshold}"
            )
            eliminated.append(elim)
            continue

        # Check severity (contested findings also bypass severity threshold)
        if not is_contested:
            sev_threshold_idx = SEVERITY_ORDER.index(
                file_config.get("severity_threshold", DEFAULT_SEVERITY_THRESHOLD)
            )
            if severity not in SEVERITY_ORDER:
                warn(
                    f"Unknown severity {severity!r} on finding {finding.get('id', '?')}; treating as low."
                )
                severity = "low"
            sev_idx = SEVERITY_ORDER.index(severity)
            if sev_idx > sev_threshold_idx:
                elim = dict(finding)
                elim["eliminated_by"] = "threshold"
                elim["elimination_reason"] = (
                    f"severity '{severity}' is below threshold '{SEVERITY_ORDER[sev_threshold_idx]}'"
                )
                eliminated.append(elim)
                continue

        passed.append(finding)

    return passed, eliminated, contested_count


# ---------------------------------------------------------------------------
# Filter: validator reachability demotion
# ---------------------------------------------------------------------------


_REACHABILITY_DEMOTION_REASON = (
    "validator found no code path that reaches this issue without a future change"
)


def apply_reachability_demotion(findings):
    """Demote future-change-only findings to low severity without eliminating them.

    Every matching finding receives the reachability stamps, including a finding
    already at low severity. ``original_severity`` is recorded only when the
    severity value changes.

    Returns (findings, demoted_count); the input list and its findings are
    mutated in place.
    """
    demoted_count = 0
    for finding in findings:
        if finding.get("reachability") != "future_change_only":
            continue
        if finding.get("severity") != "low":
            if "severity" in finding:
                finding["original_severity"] = finding["severity"]
            finding["severity"] = "low"
        finding["demoted_by"] = "reachability"
        finding["demotion_reason"] = _REACHABILITY_DEMOTION_REASON
        demoted_count += 1
    return findings, demoted_count


# ---------------------------------------------------------------------------
# Filter: injection artifact detection
# ---------------------------------------------------------------------------

# Patterns that suggest a finding was injected by a prompt artifact or
# hallucinated without grounding in actual code.
# #254 (F13): the four (now five) "<word> finding" entries picked up the
# union whitespace class between the word and "finding" (previously a
# literal space) -- see the #254 record.
# #260: the bare-word TODO/FIXME/Placeholder entries were dropped -- a real
# finding legitimately reports TODO/FIXME/placeholder residue about the code
# it reviews (measured: 5/727 real corpus titles, 100% false positive, 0
# true positives across 30 recorded runs). Detection now keys on the stub
# vocabulary "<word> finding" itself -- the phrase an injected scaffold
# title tends to spell and a real finding about residue essentially never
# does -- so the standalone `Placeholder` entry was replaced by a
# `Placeholder finding` entry alongside its four siblings.
# generated-from-filter-pattern-registry:_INJECTION_TITLE_PATTERNS do not edit; run scripts/generate_filter_patterns.py
_INJECTION_TITLE_PATTERNS = [
    r"\bExample[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b",
    r"\bSample[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b",
    r"\btest[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b",
    r"\bdemo[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b",
    r"\bPlaceholder[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b",
]
# /generated-from-filter-pattern-registry:_INJECTION_TITLE_PATTERNS

# #254: <finding>/<example> widened to tolerate attributes (unbounded
# [^>]*, terminated by the required ">" so it stays linear and parity-safe
# across twins -- Python counts code points, JS counts UTF-16 units, so a
# bounded {0,N} window here would diverge on astral input; </finding> was
# considered and declined -- an injected block always opens, so a closing
# tag adds false-fire surface with zero catch). The bracketed placeholder
# entry gained a second, appended form gated on a placeholder noun
# (FINDING/TITLE/TEXT/PLACEHOLDER/HERE): a bare `[INSERT ...]` widened past
# ~40 interior chars collides with real SQL privilege-list findings
# (`[INSERT, UPDATE, DELETE]`), so the noun gate is the discriminator
# instead of a length bound. Appended after the original bracket entry so
# `_first_match`'s reason for a bare `[INSERT]` payload is unchanged.
# "lorem ipsum" picked up the union whitespace class (previously a literal
# space).
# generated-from-filter-pattern-registry:_INJECTION_BODY_PATTERNS do not edit; run scripts/generate_filter_patterns.py
_INJECTION_BODY_PATTERNS = [
    r"<finding(?:[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff][^>]*)?>",
    r"<example(?:[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff][^>]*)?>",
    r"\[[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*INSERT[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\]",
    r"\[[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*INSERT\b[^\]]*\b(?:FINDING|TITLE|TEXT|PLACEHOLDER|HERE)\b[^\]]*\]",
    r"lorem[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+ipsum",
]
# /generated-from-filter-pattern-registry:_INJECTION_BODY_PATTERNS

# Shell command patterns — presence in a finding description/title indicates the agent
# was manipulated by adversarial content embedded in the code under review.
# These match the patterns documented in false-positive-exclusions.md
# generated-from-filter-pattern-registry:_INJECTION_SHELL_PATTERNS do not edit; run scripts/generate_filter_patterns.py
_INJECTION_SHELL_PATTERNS = [
    r"\brm[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+-[rf]",
    r"\bcurl[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+https?://",
    r"\bwget[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+https?://",
    r"\bgit[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+push\b",
    r"\bgh[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+api\b",
]
# /generated-from-filter-pattern-registry:_INJECTION_SHELL_PATTERNS

# URL patterns — findings should reference code locations, not external URLs to
# visit/fetch. Only "visit"/"download from" ship: they are imperatives a
# legitimate finding never states about itself. A prior pass (#252) tried
# adding two directive-gated long-bare-URL entries -- a reader-imperative
# verb immediately before the URL, and an exfiltration-verb + secret-object
# phrase ahead of it -- but round-2 review measured both false-firing on
# realistic LEGITIMATE security findings that quote the same vocabulary a
# real vulnerability description needs ("the router should navigate to
# <url>" for a routing bug, "an attacker can send the session cookie to
# <url>" for a real exfil finding): a legit finding and an injected
# instruction both read as "<verb> to/from <url>" in English, so this shape
# cannot be narrowed further to tell them apart. Reverted; see #255 review.
# #254: the scheme was widened from a bare `https?` to any scheme-shaped
# token (ftp, sftp, scp, ...) -- the imperative is the discriminator, not
# the scheme, so enumerating individual schemes is whack-a-mole and every
# scheme closes in one edit. "download from" also picked up the union
# whitespace class between "download" and "from" (previously a literal
# space) -- see F13 in the #254 record.
# generated-from-filter-pattern-registry:_INJECTION_URL_PATTERNS do not edit; run scripts/generate_filter_patterns.py
_INJECTION_URL_PATTERNS = [
    r"\bvisit[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+[a-z][a-z0-9+.\-]{1,15}://",
    r"\bdownload[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+from[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+[a-z][a-z0-9+.\-]{1,15}://",
]
# /generated-from-filter-pattern-registry:_INJECTION_URL_PATTERNS

# Encoded payload patterns — base64 or hex blobs in findings are injection
# artifacts. Each shape is now two directive-gated entries: a before-branch
# requiring a decode-family verb ahead of the blob, an after-branch requiring
# decode/execute sink syntax after it. A bare encoded-looking run with no
# decode directive nearby (a commit SHA, an opaque config token, a padded
# identifier) no longer matches either branch -- both measured a false-fire
# on ordinary review/DevOps prose where a generic verb (run/curl/wget)
# happened to sit near an unrelated hash-shaped token.
# generated-from-filter-pattern-registry:_INJECTION_ENCODED_PATTERNS do not edit; run scripts/generate_filter_patterns.py
_INJECTION_ENCODED_PATTERNS = [
    r"\b(?:decode|base64|atob|b64decode)\b[^\x00]{0,40}[A-Za-z0-9+/]{40,}={0,2}\b",
    r"\b[A-Za-z0-9+/]{40,}={0,2}\b[^\x00]{0,40}(?:\|[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*(?:sh|bash|zsh)\b|base64[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+-d\b|(?:then|and)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:run|execute|eval)\b)",
    r"\b(?:decode|unhex|xxd|fromhex|unhexlify)\b[^\x00]{0,40}(?<!\w)(?:0x)?[0-9a-fA-F]{32,}(?!\w)",
    r"(?<!\w)(?:0x)?[0-9a-fA-F]{32,}(?!\w)[^\x00]{0,40}(?:\|[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*(?:xxd|sh|bash)\b|(?:then|and)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:run|execute|eval)\b|-r[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+-p\b)",
]
# /generated-from-filter-pattern-registry:_INJECTION_ENCODED_PATTERNS

# Bypass / auto-approve instruction patterns. auto-approve is now two
# grammatically-gated entries (a determiner + PR/MR/commit object, or an
# "and <verb>" continuation) instead of a bare phrase match -- the bare
# phrase false-fired on third-person policy prose ("auto-approve changes to
# lockfiles should be gated behind review") where "auto-approve" is the
# grammatical subject, not an imperative.
# generated-from-filter-pattern-registry:_INJECTION_BYPASS_PATTERNS do not edit; run scripts/generate_filter_patterns.py
_INJECTION_BYPASS_PATTERNS = [
    r"\bskip[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+review\b",
    r"\bauto[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]?approve[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:this|these|the|it|my|your)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:pr|pull[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+request|mr|merge[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+request|changes?|commit)\b",
    r"\bauto[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]?approve[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+and[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:merge|skip|bypass|push|deploy|proceed|continue)\b",
    r"\bbypass[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:security[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?controls?\b",
    r"\bbypass[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:the[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?(?:auth|authentication|authorization)\b",
    r"\bdisable[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:auth|authentication|authorization)\b",
    r"\bmark[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:this[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?(?:finding[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?as[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+safe\b",
    r"\bapprove[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:this|the)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:PR|pull[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+request|change)\b",
]
# /generated-from-filter-pattern-registry:_INJECTION_BYPASS_PATTERNS

# Instructional tone patterns — analytical findings do not issue commands to the user
# generated-from-filter-pattern-registry:_INJECTION_INSTRUCTIONAL_PATTERNS do not edit; run scripts/generate_filter_patterns.py
_INJECTION_INSTRUCTIONAL_PATTERNS = [
    r"\byou[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+should[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+run\b",
    r"\bexecute[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+the[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+following\b",
    r"\brun[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+this[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+command\b",
    r"\bplease[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+run\b",
    r"\bpaste[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:this|the[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+following)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+into[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:your[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?terminal",
    r"\bcopy[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+and[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+paste[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+the[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+following\b",
]
# /generated-from-filter-pattern-registry:_INJECTION_INSTRUCTIONAL_PATTERNS

# Patterns that recommend introducing vulnerabilities or disabling security features
# generated-from-filter-pattern-registry:_INJECTION_VULN_INTRO_PATTERNS do not edit; run scripts/generate_filter_patterns.py
_INJECTION_VULN_INTRO_PATTERNS = [
    r"\badd[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+eval[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\(",
    r"\buse[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+eval[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\(",
    r"\bdisable[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:CORS|CSP|content[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]security[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]policy)\b",
    r"\bdisable[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:CSRF|csrf)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:protection|check|token)\b",
    r"\ballow[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+all[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+origins\b",
    r"\bset[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+secure[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+to[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+false\b",
    r"\bdisable[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:TLS|SSL|HTTPS)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:verification|validation)\b",
    r"\bskip[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:certificate|cert)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:verification|validation)\b",
    r"\bdisable[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+security[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:check|feature|control)\b",
]
# /generated-from-filter-pattern-registry:_INJECTION_VULN_INTRO_PATTERNS

# Minimum word count for a valid finding description; fewer words + high confidence = suspicious
_MIN_BODY_WORDS = 10
_HIGH_CONFIDENCE_THRESHOLD = 85

# Delivery bound on suggested_fix_code content (#63/D8) -- the SAME two numbers
# bound the field at render time in scripts/post_review.py (`_FIX_MAX_LINES` /
# `_FIX_MAX_CHARS`) and in the JS twin (workflows/src/filterFindings.js);
# change all three together. tests/test_filter_findings.py's lockstep test
# (#63 round-1 F8) regex-parses all three assignments and asserts they agree.
#
# Both bound checks below measure the SAME normalized text the render-time gate
# does (post_review.py's dedicated fence normalizer, #63 round-1 F2/F5-B): strip
# exactly ONE trailing "\n" (the terminator) and nothing else, then lines =
# split("\n") elements, chars = len() of that normalized text. Python's len()
# already counts code points (not UTF-16 units), so no extra measure is needed
# here -- that distinction only bites the JS twin (F6), which must use
# [...code].length rather than .length for the same reason.
_FIX_MAX_LINES = 100
_FIX_MAX_CHARS = 8000


def _normalize_fix_code_for_bound(code):
    """Strip exactly one trailing "\\n" (the terminator) before measuring the
    delivery bound -- mirrors scripts/post_review.py's dedicated fence
    normalizer and workflows/src/filterFindings.js's JS twin (#63 round-1
    F2/F5-B). An edge blank line stated by the replacement (a second, non-
    terminator trailing "\\n") is content and must count toward the bound,
    same as it counts toward the fence the gate later renders.
    """
    return code[:-1] if code.endswith("\n") else code


# Matches the union whitespace class respelled into the injection/routing
# patterns above (item 2 of the #211 decision) so a word-count boundary and
# a pattern-match boundary agree on what separates words. re.ASCII is NOT
# applied here -- the class is already fully explicit, so the flag would be
# a no-op (verified: tests/test_filter_findings.py pins this).
# generated-from-filter-pattern-registry:_WORD_SPLIT_RE do not edit; run scripts/generate_filter_patterns.py
_WORD_SPLIT_RE = re.compile(
    r"[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)
# /generated-from-filter-pattern-registry:_WORD_SPLIT_RE


def _count_words(text):
    """Return the number of words in text (split on the union whitespace class)."""
    return sum(1 for w in _WORD_SPLIT_RE.split(text or "") if w)


# Prose fields scanned by the seven _SUGGESTION_SETS pattern sets and stripped
# (never eliminated) on a match -- #62 introduced this for `suggestion`; #213
# extends it to `claude_md_rule`/`spec_text`, the two repo-derived citation
# fields the conventions-and-intent agent quotes verbatim into posted
# comments (a higher-risk injection source than agent-authored suggestion).
# One shared list, mirrored by workflows/src/filterFindings.js's
# INJECTION_STRIPPED_PROSE_FIELDS -- a lockstep test (TestFixBoundConstantsLockstep-
# adjacent, tests/test_filter_findings.py) asserts the two lists agree element-wise
# so adding a field to only one twin goes red. Order is scan/strip order:
# `suggestion` first, so its bytes are reproduced exactly when it is the field
# that matches.
_INJECTION_STRIPPED_PROSE_FIELDS = ("suggestion", "claude_md_rule", "spec_text")

# Module-level table of the seven content-set (phrase, raw-pattern-tuple)
# pairs, in _SUGGESTION_SETS order -- apply_injection_filter compiles this
# ONCE per call into its `_SUGGESTION_SETS` (#215 round-1 parity-F4/F5) and
# also uses it, unchanged, as the source for the field-strip scan
# (_strip_injected_prose_fields). Reordering, adding, or removing a content
# set is a single edit here.
# generated-from-filter-pattern-registry:_CONTENT_PATTERN_SETS do not edit; run scripts/generate_filter_patterns.py
_CONTENT_PATTERN_SETS = (
    ("contains shell command pattern", tuple(_INJECTION_SHELL_PATTERNS)),
    ("contains visit-URL pattern", tuple(_INJECTION_URL_PATTERNS)),
    ("contains encoded payload pattern", tuple(_INJECTION_ENCODED_PATTERNS)),
    ("contains bypass/auto-approve instruction", tuple(_INJECTION_BYPASS_PATTERNS)),
    ("uses instructional tone", tuple(_INJECTION_INSTRUCTIONAL_PATTERNS)),
    ("recommends introducing vulnerability", tuple(_INJECTION_VULN_INTRO_PATTERNS)),
    ("matches injection marker", tuple(_INJECTION_BODY_PATTERNS)),
)
# /generated-from-filter-pattern-registry:_CONTENT_PATTERN_SETS


# Confusable-fold + invisible-strip tables (#272): a single non-ASCII codepoint folds
# to one ASCII letter, and 599 zero-width/joiner/bidi/variation-selector/combining
# codepoints are deleted, so a homoglyph- or invisible-disguised injection phrase
# reduces to the plain ASCII the heuristics scan for. GENERATED from
# scripts/filter_patterns_registry.py (CONFUSABLE_FOLD_PACKED / INVISIBLE_STRIP_PACKED)
# by scripts/generate_confusable_tables.py -- do not hand-edit inside the fences -- and
# pinned back to the registry by tests/test_filter_findings.py. An NFKC/normalize
# pre-pass at runtime would diverge the twins (CPython UCD vs Node ICU ship different
# Unicode versions), so the tables are hand-pinned, never re-derived. Precedence
# casefold(4) > NFKC(898) > confusables(585) is baked into the data: U+017F LONG S
# folds to s, not the confusables f. The scan folds RAW-first then the transformed copy
# (see _first_match), so a HEAD detection is never lost -- the transform only ADDS reach.
# generated-from-confusable-registry:_CONFUSABLE_FOLD_PACKED do not edit; run scripts/generate_confusable_tables.py
_CONFUSABLE_FOLD_PACKED = (
    "\u00aaa\u00bao\u00d7x\u00fep\u0130i\u0131i\u017fs\u0184b\u018dg\u0192f\u0196l\u01a6R"
    "\u01bds\u01bfp\u01c0l\u0251a\u0261g\u0263y\u0269i\u026ai\u026fw\u028bu\u028fy\u02b0h"
    "\u02b2j\u02b3r\u02b7w\u02b8y\u02dbi\u02e1l\u02e2s\u02e3x\u037ai\u037fJ\u0391A\u0392B"
    "\u0395E\u0396Z\u0397H\u0399l\u039aK\u039cM\u039dN\u039fO\u03a1P\u03a4T\u03a5Y\u03a7X"
    "\u03b1a\u03b3y\u03b9i\u03bdv\u03bfo\u03c1p\u03c3o\u03c5u\u03d2Y\u03dcF\u03edo\u03f1p"
    "\u03f2c\u03f3j\u03f8p\u03f9C\u03faM\u0405S\u0406l\u0408J\u0410A\u0412B\u0415E\u041aK"
    "\u041cM\u041dH\u041eO\u0420P\u0421C\u0422T\u0423Y\u0425X\u042cb\u0430a\u0433r\u0435e"
    "\u043eo\u0440p\u0441c\u0443y\u0445x\u0448w\u0455s\u0456i\u0458j\u0461w\u0474V\u0475v"
    "\u04aeY\u04afy\u04bbh\u04bde\u04c0l\u04cfl\u0501d\u050cG\u051bq\u051cW\u051dw\u054dU"
    "\u054fS\u0555O\u0561w\u0563q\u0566q\u0570h\u0578n\u057cn\u057du\u0581g\u0582i\u0584f"
    "\u0585o\u05c0l\u05d5l\u05d8v\u05dfl\u05e1o\u0627l\u0647o\u0661l\u0665o\u0667V\u06beo"
    "\u06c1o\u06d5o\u06f1l\u06f5o\u06f7V\u07c0O\u07cal\u0966o\u09e6o\u0a66o\u0ae6o\u0b20O"
    "\u0b66o\u0be6o\u0c02o\u0c66o\u0c82o\u0ce6O\u0d02o\u0d1fs\u0d20o\u0d66o\u0d82o\u0e50o"
    "\u0ed0o\u1004c\u101do\u1040o\u105ac\u10e7y\u10ffo\u1200U\u12d0O\u13a0D\u13a1R\u13a2T"
    "\u13a5i\u13a9Y\u13aaA\u13abJ\u13acE\u13b3W\u13b7M\u13bbH\u13bdY\u13c0G\u13c2h\u13c3Z"
    "\u13cfb\u13d2R\u13d4W\u13d5S\u13d9V\u13daS\u13deL\u13dfC\u13e2P\u13e6K\u13e7d\u13f3G"
    "\u13f4B\u142fV\u144cU\u146dP\u146fd\u1472b\u148dJ\u14aaL\u1541x\u157cH\u157dx\u1587R"
    "\u15afb\u15b4F\u15c5A\u15deD\u15eaD\u15f0M\u15f7B\u166dX\u166ex\u16b7X\u16c1l\u16d5K"
    "\u16d6M\u17e0o\u1d04c\u1d0fo\u1d11o\u1d1cu\u1d20v\u1d21w\u1d22z\u1d26r\u1d2cA\u1d2eB"
    "\u1d30D\u1d31E\u1d33G\u1d34H\u1d35I\u1d36J\u1d37K\u1d38L\u1d39M\u1d3aN\u1d3cO\u1d3eP"
    "\u1d3fR\u1d40T\u1d41U\u1d42W\u1d43a\u1d47b\u1d48d\u1d49e\u1d4dg\u1d4fk\u1d50m\u1d52o"
    "\u1d56p\u1d57t\u1d58u\u1d5bv\u1d62i\u1d63r\u1d64u\u1d65v\u1d83g\u1d8cy\u1d9cc\u1da0f"
    "\u1dbbz\u1e9df\u1effy\u1fbei\u2071i\u207fn\u2090a\u2091e\u2092o\u2093x\u2095h\u2096k"
    "\u2097l\u2098m\u2099n\u209ap\u209bs\u209ct\u2102C\u210ag\u210bH\u210cH\u210dH\u210eh"
    "\u2110I\u2111I\u2112L\u2113l\u2115N\u2119P\u211aQ\u211bR\u211cR\u211dR\u2124Z\u2128Z"
    "\u212ak\u212cB\u212dC\u212ee\u212fe\u2130E\u2131F\u2133M\u2134o\u2139i\u213dy\u2145D"
    "\u2146d\u2147e\u2148i\u2149j\u2160I\u2164V\u2169X\u216cL\u216dC\u216eD\u216fM\u2170i"
    "\u2174v\u2179x\u217cl\u217dc\u217ed\u217fm\u2223l\u2228v\u222aU\u22a4T\u22c1v\u22c3U"
    "\u22ffE\u2373i\u2374p\u237aa\u23fdl\u24b6A\u24b7B\u24b8C\u24b9D\u24baE\u24bbF\u24bcG"
    "\u24bdH\u24beI\u24bfJ\u24c0K\u24c1L\u24c2M\u24c3N\u24c4O\u24c5P\u24c6Q\u24c7R\u24c8S"
    "\u24c9T\u24caU\u24cbV\u24ccW\u24cdX\u24ceY\u24cfZ\u24d0a\u24d1b\u24d2c\u24d3d\u24d4e"
    "\u24d5f\u24d6g\u24d7h\u24d8i\u24d9j\u24dak\u24dbl\u24dcm\u24ddn\u24deo\u24dfp\u24e0q"
    "\u24e1r\u24e2s\u24e3t\u24e4u\u24e5v\u24e6w\u24e7x\u24e8y\u24e9z\u2573X\u27d9T\u292bx"
    "\u292cx\u2a2fx\u2c7cj\u2c7dV\u2c82B\u2c85r\u2c8eH\u2c92l\u2c93i\u2c94K\u2c98M\u2c9aN"
    "\u2c9eO\u2c9fo\u2ca2P\u2ca3p\u2ca4C\u2ca5c\u2ca6T\u2ca8Y\u2ca9y\u2cacX\u2cbdw\u2cceP"
    "\u2ccfp\u2cd0L\u2d38V\u2d39E\u2d4fl\u2d54O\u2d55Q\u2d5dX\u3007O\ua4d0B\ua4d1P\ua4d2d"
    "\ua4d3D\ua4d4T\ua4d6G\ua4d7K\ua4d9J\ua4daC\ua4dcZ\ua4ddF\ua4dfM\ua4e0N\ua4e1L\ua4e2S"
    "\ua4e3R\ua4e6V\ua4e7H\ua4eaW\ua4ebX\ua4ecY\ua4eeA\ua4f0E\ua4f2l\ua4f3O\ua4f4U\ua647i"
    "\ua6dfV\ua731s\ua798F\ua799f\ua79fu\ua7b2J\ua7b3X\ua7b4B\ua7f2C\ua7f3F\ua7f4Q\uab32e"
    "\uab35f\uab3do\uab47r\uab48r\uab4eu\uab52u\uab5ay\uab75i\uab81r\uab83w\uab93z\uaba9v"
    "\uabaas\uabafc\ufba6o\ufba7o\ufba8o\ufba9o\ufbaao\ufbabo\ufbaco\ufbado\ufe8dl\ufe8el"
    "\ufee9o\ufeeao\ufeebo\ufeeco\uff21A\uff22B\uff23C\uff24D\uff25E\uff26F\uff27G\uff28H"
    "\uff29I\uff2aJ\uff2bK\uff2cL\uff2dM\uff2eN\uff2fO\uff30P\uff31Q\uff32R\uff33S\uff34T"
    "\uff35U\uff36V\uff37W\uff38X\uff39Y\uff3aZ\uff41a\uff42b\uff43c\uff44d\uff45e\uff46f"
    "\uff47g\uff48h\uff49i\uff4aj\uff4bk\uff4cl\uff4dm\uff4en\uff4fo\uff50p\uff51q\uff52r"
    "\uff53s\uff54t\uff55u\uff56v\uff57w\uff58x\uff59y\uff5az\uffe8l\U00010282B\U00010286E\U00010287F"
    "\U0001028al\U00010290X\U00010292O\U00010295P\U00010296S\U00010297T\U000102a0A\U000102a1B\U000102a2C\U000102a5F\U000102abO\U000102b0M"
    "\U000102b1T\U000102b2Y\U000102b4X\U000102cfH\U000102f5Z\U00010301B\U00010302C\U00010309l\U00010311M\U00010315T\U00010317X\U00010320l"
    "\U00010322X\U00010404O\U00010415C\U0001041bL\U00010420S\U0001042co\U0001043dc\U00010448s\U000104b4R\U000104c2O\U000104ceU\U000104eao"
    "\U000104f6u\U00010513N\U00010516O\U00010518K\U0001051cC\U0001051dV\U00010525F\U00010526L\U00010527X\U000107a5q\U000114d0o\U00011706v"
    "\U0001170aw\U0001170ew\U0001170fw\U000118a0V\U000118a2F\U000118a3L\U000118a4Y\U000118a6E\U000118a9Z\U000118aeE\U000118b2L\U000118b5O"
    "\U000118b8U\U000118bcT\U000118c0v\U000118c1s\U000118c2F\U000118c3i\U000118c4z\U000118c8o\U000118d7o\U000118d8u\U000118dcy\U000118e0O"
    "\U000118e5Z\U000118e6W\U000118e9C\U000118ecX\U000118efW\U000118f2C\U00011ddal\U00011de0O\U00011de1l\U00016eaal\U00016eb6b\U00016f08V"
    "\U00016f0aT\U00016f16L\U00016f28l\U00016f35R\U00016f3aS\U00016f40A\U00016f42U\U00016f43Y\U0001ccdel\U0001ccf0O\U0001ccf1l\U0001d20dV"
    "\U0001d213F\U0001d216R\U0001d22aL\U0001d400A\U0001d401B\U0001d402C\U0001d403D\U0001d404E\U0001d405F\U0001d406G\U0001d407H\U0001d408I"
    "\U0001d409J\U0001d40aK\U0001d40bL\U0001d40cM\U0001d40dN\U0001d40eO\U0001d40fP\U0001d410Q\U0001d411R\U0001d412S\U0001d413T\U0001d414U"
    "\U0001d415V\U0001d416W\U0001d417X\U0001d418Y\U0001d419Z\U0001d41aa\U0001d41bb\U0001d41cc\U0001d41dd\U0001d41ee\U0001d41ff\U0001d420g"
    "\U0001d421h\U0001d422i\U0001d423j\U0001d424k\U0001d425l\U0001d426m\U0001d427n\U0001d428o\U0001d429p\U0001d42aq\U0001d42br\U0001d42cs"
    "\U0001d42dt\U0001d42eu\U0001d42fv\U0001d430w\U0001d431x\U0001d432y\U0001d433z\U0001d434A\U0001d435B\U0001d436C\U0001d437D\U0001d438E"
    "\U0001d439F\U0001d43aG\U0001d43bH\U0001d43cI\U0001d43dJ\U0001d43eK\U0001d43fL\U0001d440M\U0001d441N\U0001d442O\U0001d443P\U0001d444Q"
    "\U0001d445R\U0001d446S\U0001d447T\U0001d448U\U0001d449V\U0001d44aW\U0001d44bX\U0001d44cY\U0001d44dZ\U0001d44ea\U0001d44fb\U0001d450c"
    "\U0001d451d\U0001d452e\U0001d453f\U0001d454g\U0001d456i\U0001d457j\U0001d458k\U0001d459l\U0001d45am\U0001d45bn\U0001d45co\U0001d45dp"
    "\U0001d45eq\U0001d45fr\U0001d460s\U0001d461t\U0001d462u\U0001d463v\U0001d464w\U0001d465x\U0001d466y\U0001d467z\U0001d468A\U0001d469B"
    "\U0001d46aC\U0001d46bD\U0001d46cE\U0001d46dF\U0001d46eG\U0001d46fH\U0001d470I\U0001d471J\U0001d472K\U0001d473L\U0001d474M\U0001d475N"
    "\U0001d476O\U0001d477P\U0001d478Q\U0001d479R\U0001d47aS\U0001d47bT\U0001d47cU\U0001d47dV\U0001d47eW\U0001d47fX\U0001d480Y\U0001d481Z"
    "\U0001d482a\U0001d483b\U0001d484c\U0001d485d\U0001d486e\U0001d487f\U0001d488g\U0001d489h\U0001d48ai\U0001d48bj\U0001d48ck\U0001d48dl"
    "\U0001d48em\U0001d48fn\U0001d490o\U0001d491p\U0001d492q\U0001d493r\U0001d494s\U0001d495t\U0001d496u\U0001d497v\U0001d498w\U0001d499x"
    "\U0001d49ay\U0001d49bz\U0001d49cA\U0001d49eC\U0001d49fD\U0001d4a2G\U0001d4a5J\U0001d4a6K\U0001d4a9N\U0001d4aaO\U0001d4abP\U0001d4acQ"
    "\U0001d4aeS\U0001d4afT\U0001d4b0U\U0001d4b1V\U0001d4b2W\U0001d4b3X\U0001d4b4Y\U0001d4b5Z\U0001d4b6a\U0001d4b7b\U0001d4b8c\U0001d4b9d"
    "\U0001d4bbf\U0001d4bdh\U0001d4bei\U0001d4bfj\U0001d4c0k\U0001d4c1l\U0001d4c2m\U0001d4c3n\U0001d4c5p\U0001d4c6q\U0001d4c7r\U0001d4c8s"
    "\U0001d4c9t\U0001d4cau\U0001d4cbv\U0001d4ccw\U0001d4cdx\U0001d4cey\U0001d4cfz\U0001d4d0A\U0001d4d1B\U0001d4d2C\U0001d4d3D\U0001d4d4E"
    "\U0001d4d5F\U0001d4d6G\U0001d4d7H\U0001d4d8I\U0001d4d9J\U0001d4daK\U0001d4dbL\U0001d4dcM\U0001d4ddN\U0001d4deO\U0001d4dfP\U0001d4e0Q"
    "\U0001d4e1R\U0001d4e2S\U0001d4e3T\U0001d4e4U\U0001d4e5V\U0001d4e6W\U0001d4e7X\U0001d4e8Y\U0001d4e9Z\U0001d4eaa\U0001d4ebb\U0001d4ecc"
    "\U0001d4edd\U0001d4eee\U0001d4eff\U0001d4f0g\U0001d4f1h\U0001d4f2i\U0001d4f3j\U0001d4f4k\U0001d4f5l\U0001d4f6m\U0001d4f7n\U0001d4f8o"
    "\U0001d4f9p\U0001d4faq\U0001d4fbr\U0001d4fcs\U0001d4fdt\U0001d4feu\U0001d4ffv\U0001d500w\U0001d501x\U0001d502y\U0001d503z\U0001d504A"
    "\U0001d505B\U0001d507D\U0001d508E\U0001d509F\U0001d50aG\U0001d50dJ\U0001d50eK\U0001d50fL\U0001d510M\U0001d511N\U0001d512O\U0001d513P"
    "\U0001d514Q\U0001d516S\U0001d517T\U0001d518U\U0001d519V\U0001d51aW\U0001d51bX\U0001d51cY\U0001d51ea\U0001d51fb\U0001d520c\U0001d521d"
    "\U0001d522e\U0001d523f\U0001d524g\U0001d525h\U0001d526i\U0001d527j\U0001d528k\U0001d529l\U0001d52am\U0001d52bn\U0001d52co\U0001d52dp"
    "\U0001d52eq\U0001d52fr\U0001d530s\U0001d531t\U0001d532u\U0001d533v\U0001d534w\U0001d535x\U0001d536y\U0001d537z\U0001d538A\U0001d539B"
    "\U0001d53bD\U0001d53cE\U0001d53dF\U0001d53eG\U0001d540I\U0001d541J\U0001d542K\U0001d543L\U0001d544M\U0001d546O\U0001d54aS\U0001d54bT"
    "\U0001d54cU\U0001d54dV\U0001d54eW\U0001d54fX\U0001d550Y\U0001d552a\U0001d553b\U0001d554c\U0001d555d\U0001d556e\U0001d557f\U0001d558g"
    "\U0001d559h\U0001d55ai\U0001d55bj\U0001d55ck\U0001d55dl\U0001d55em\U0001d55fn\U0001d560o\U0001d561p\U0001d562q\U0001d563r\U0001d564s"
    "\U0001d565t\U0001d566u\U0001d567v\U0001d568w\U0001d569x\U0001d56ay\U0001d56bz\U0001d56cA\U0001d56dB\U0001d56eC\U0001d56fD\U0001d570E"
    "\U0001d571F\U0001d572G\U0001d573H\U0001d574I\U0001d575J\U0001d576K\U0001d577L\U0001d578M\U0001d579N\U0001d57aO\U0001d57bP\U0001d57cQ"
    "\U0001d57dR\U0001d57eS\U0001d57fT\U0001d580U\U0001d581V\U0001d582W\U0001d583X\U0001d584Y\U0001d585Z\U0001d586a\U0001d587b\U0001d588c"
    "\U0001d589d\U0001d58ae\U0001d58bf\U0001d58cg\U0001d58dh\U0001d58ei\U0001d58fj\U0001d590k\U0001d591l\U0001d592m\U0001d593n\U0001d594o"
    "\U0001d595p\U0001d596q\U0001d597r\U0001d598s\U0001d599t\U0001d59au\U0001d59bv\U0001d59cw\U0001d59dx\U0001d59ey\U0001d59fz\U0001d5a0A"
    "\U0001d5a1B\U0001d5a2C\U0001d5a3D\U0001d5a4E\U0001d5a5F\U0001d5a6G\U0001d5a7H\U0001d5a8I\U0001d5a9J\U0001d5aaK\U0001d5abL\U0001d5acM"
    "\U0001d5adN\U0001d5aeO\U0001d5afP\U0001d5b0Q\U0001d5b1R\U0001d5b2S\U0001d5b3T\U0001d5b4U\U0001d5b5V\U0001d5b6W\U0001d5b7X\U0001d5b8Y"
    "\U0001d5b9Z\U0001d5baa\U0001d5bbb\U0001d5bcc\U0001d5bdd\U0001d5bee\U0001d5bff\U0001d5c0g\U0001d5c1h\U0001d5c2i\U0001d5c3j\U0001d5c4k"
    "\U0001d5c5l\U0001d5c6m\U0001d5c7n\U0001d5c8o\U0001d5c9p\U0001d5caq\U0001d5cbr\U0001d5ccs\U0001d5cdt\U0001d5ceu\U0001d5cfv\U0001d5d0w"
    "\U0001d5d1x\U0001d5d2y\U0001d5d3z\U0001d5d4A\U0001d5d5B\U0001d5d6C\U0001d5d7D\U0001d5d8E\U0001d5d9F\U0001d5daG\U0001d5dbH\U0001d5dcI"
    "\U0001d5ddJ\U0001d5deK\U0001d5dfL\U0001d5e0M\U0001d5e1N\U0001d5e2O\U0001d5e3P\U0001d5e4Q\U0001d5e5R\U0001d5e6S\U0001d5e7T\U0001d5e8U"
    "\U0001d5e9V\U0001d5eaW\U0001d5ebX\U0001d5ecY\U0001d5edZ\U0001d5eea\U0001d5efb\U0001d5f0c\U0001d5f1d\U0001d5f2e\U0001d5f3f\U0001d5f4g"
    "\U0001d5f5h\U0001d5f6i\U0001d5f7j\U0001d5f8k\U0001d5f9l\U0001d5fam\U0001d5fbn\U0001d5fco\U0001d5fdp\U0001d5feq\U0001d5ffr\U0001d600s"
    "\U0001d601t\U0001d602u\U0001d603v\U0001d604w\U0001d605x\U0001d606y\U0001d607z\U0001d608A\U0001d609B\U0001d60aC\U0001d60bD\U0001d60cE"
    "\U0001d60dF\U0001d60eG\U0001d60fH\U0001d610I\U0001d611J\U0001d612K\U0001d613L\U0001d614M\U0001d615N\U0001d616O\U0001d617P\U0001d618Q"
    "\U0001d619R\U0001d61aS\U0001d61bT\U0001d61cU\U0001d61dV\U0001d61eW\U0001d61fX\U0001d620Y\U0001d621Z\U0001d622a\U0001d623b\U0001d624c"
    "\U0001d625d\U0001d626e\U0001d627f\U0001d628g\U0001d629h\U0001d62ai\U0001d62bj\U0001d62ck\U0001d62dl\U0001d62em\U0001d62fn\U0001d630o"
    "\U0001d631p\U0001d632q\U0001d633r\U0001d634s\U0001d635t\U0001d636u\U0001d637v\U0001d638w\U0001d639x\U0001d63ay\U0001d63bz\U0001d63cA"
    "\U0001d63dB\U0001d63eC\U0001d63fD\U0001d640E\U0001d641F\U0001d642G\U0001d643H\U0001d644I\U0001d645J\U0001d646K\U0001d647L\U0001d648M"
    "\U0001d649N\U0001d64aO\U0001d64bP\U0001d64cQ\U0001d64dR\U0001d64eS\U0001d64fT\U0001d650U\U0001d651V\U0001d652W\U0001d653X\U0001d654Y"
    "\U0001d655Z\U0001d656a\U0001d657b\U0001d658c\U0001d659d\U0001d65ae\U0001d65bf\U0001d65cg\U0001d65dh\U0001d65ei\U0001d65fj\U0001d660k"
    "\U0001d661l\U0001d662m\U0001d663n\U0001d664o\U0001d665p\U0001d666q\U0001d667r\U0001d668s\U0001d669t\U0001d66au\U0001d66bv\U0001d66cw"
    "\U0001d66dx\U0001d66ey\U0001d66fz\U0001d670A\U0001d671B\U0001d672C\U0001d673D\U0001d674E\U0001d675F\U0001d676G\U0001d677H\U0001d678I"
    "\U0001d679J\U0001d67aK\U0001d67bL\U0001d67cM\U0001d67dN\U0001d67eO\U0001d67fP\U0001d680Q\U0001d681R\U0001d682S\U0001d683T\U0001d684U"
    "\U0001d685V\U0001d686W\U0001d687X\U0001d688Y\U0001d689Z\U0001d68aa\U0001d68bb\U0001d68cc\U0001d68dd\U0001d68ee\U0001d68ff\U0001d690g"
    "\U0001d691h\U0001d692i\U0001d693j\U0001d694k\U0001d695l\U0001d696m\U0001d697n\U0001d698o\U0001d699p\U0001d69aq\U0001d69br\U0001d69cs"
    "\U0001d69dt\U0001d69eu\U0001d69fv\U0001d6a0w\U0001d6a1x\U0001d6a2y\U0001d6a3z\U0001d6a4i\U0001d6a8A\U0001d6a9B\U0001d6acE\U0001d6adZ"
    "\U0001d6aeH\U0001d6b0l\U0001d6b1K\U0001d6b3M\U0001d6b4N\U0001d6b6O\U0001d6b8P\U0001d6bbT\U0001d6bcY\U0001d6beX\U0001d6c2a\U0001d6c4y"
    "\U0001d6cai\U0001d6cev\U0001d6d0o\U0001d6d2p\U0001d6d4o\U0001d6d6u\U0001d6e0p\U0001d6e2A\U0001d6e3B\U0001d6e6E\U0001d6e7Z\U0001d6e8H"
    "\U0001d6eal\U0001d6ebK\U0001d6edM\U0001d6eeN\U0001d6f0O\U0001d6f2P\U0001d6f5T\U0001d6f6Y\U0001d6f8X\U0001d6fca\U0001d6fey\U0001d704i"
    "\U0001d708v\U0001d70ao\U0001d70cp\U0001d70eo\U0001d710u\U0001d71ap\U0001d71cA\U0001d71dB\U0001d720E\U0001d721Z\U0001d722H\U0001d724l"
    "\U0001d725K\U0001d727M\U0001d728N\U0001d72aO\U0001d72cP\U0001d72fT\U0001d730Y\U0001d732X\U0001d736a\U0001d738y\U0001d73ei\U0001d742v"
    "\U0001d744o\U0001d746p\U0001d748o\U0001d74au\U0001d754p\U0001d756A\U0001d757B\U0001d75aE\U0001d75bZ\U0001d75cH\U0001d75el\U0001d75fK"
    "\U0001d761M\U0001d762N\U0001d764O\U0001d766P\U0001d769T\U0001d76aY\U0001d76cX\U0001d770a\U0001d772y\U0001d778i\U0001d77cv\U0001d77eo"
    "\U0001d780p\U0001d782o\U0001d784u\U0001d78ep\U0001d790A\U0001d791B\U0001d794E\U0001d795Z\U0001d796H\U0001d798l\U0001d799K\U0001d79bM"
    "\U0001d79cN\U0001d79eO\U0001d7a0P\U0001d7a3T\U0001d7a4Y\U0001d7a6X\U0001d7aaa\U0001d7acy\U0001d7b2i\U0001d7b6v\U0001d7b8o\U0001d7bap"
    "\U0001d7bco\U0001d7beu\U0001d7c8p\U0001d7caF\U0001d7ceO\U0001d7cfl\U0001d7d8O\U0001d7d9l\U0001d7e2O\U0001d7e3l\U0001d7ecO\U0001d7edl"
    "\U0001d7f6O\U0001d7f7l\U0001e8c7l\U0001ee00l\U0001ee24o\U0001ee64o\U0001ee80l\U0001ee84o\U0001f12bC\U0001f12cR\U0001f130A\U0001f131B"
    "\U0001f132C\U0001f133D\U0001f134E\U0001f135F\U0001f136G\U0001f137H\U0001f138I\U0001f139J\U0001f13aK\U0001f13bL\U0001f13cM\U0001f13dN"
    "\U0001f13eO\U0001f13fP\U0001f140Q\U0001f141R\U0001f142S\U0001f143T\U0001f144U\U0001f145V\U0001f146W\U0001f147X\U0001f148Y\U0001f149Z"
    "\U0001f74cC\U0001f768T\U0001fbf0O\U0001fbf1l"
)
# /generated-from-confusable-registry:_CONFUSABLE_FOLD_PACKED

# generated-from-confusable-registry:_INVISIBLE_STRIP_PACKED do not edit; run scripts/generate_confusable_tables.py
_INVISIBLE_STRIP_PACKED = (
    "\u00ad\u0300\u0301\u0302\u0303\u0304\u0305\u0306\u0307\u0308\u0309\u030a\u030b\u030c\u030d\u030e"
    "\u030f\u0310\u0311\u0312\u0313\u0314\u0315\u0316\u0317\u0318\u0319\u031a\u031b\u031c\u031d\u031e"
    "\u031f\u0320\u0321\u0322\u0323\u0324\u0325\u0326\u0327\u0328\u0329\u032a\u032b\u032c\u032d\u032e"
    "\u032f\u0330\u0331\u0332\u0333\u0334\u0335\u0336\u0337\u0338\u0339\u033a\u033b\u033c\u033d\u033e"
    "\u033f\u0340\u0341\u0342\u0343\u0344\u0345\u0346\u0347\u0348\u0349\u034a\u034b\u034c\u034d\u034e"
    "\u034f\u0350\u0351\u0352\u0353\u0354\u0355\u0356\u0357\u0358\u0359\u035a\u035b\u035c\u035d\u035e"
    "\u035f\u0360\u0361\u0362\u0363\u0364\u0365\u0366\u0367\u0368\u0369\u036a\u036b\u036c\u036d\u036e"
    "\u036f\u061c\u180e\u1ab0\u1ab1\u1ab2\u1ab3\u1ab4\u1ab5\u1ab6\u1ab7\u1ab8\u1ab9\u1aba\u1abb\u1abc"
    "\u1abd\u1abe\u1abf\u1ac0\u1ac1\u1ac2\u1ac3\u1ac4\u1ac5\u1ac6\u1ac7\u1ac8\u1ac9\u1aca\u1acb\u1acc"
    "\u1acd\u1ace\u1acf\u1ad0\u1ad1\u1ad2\u1ad3\u1ad4\u1ad5\u1ad6\u1ad7\u1ad8\u1ad9\u1ada\u1adb\u1adc"
    "\u1add\u1ade\u1adf\u1ae0\u1ae1\u1ae2\u1ae3\u1ae4\u1ae5\u1ae6\u1ae7\u1ae8\u1ae9\u1aea\u1aeb\u1aec"
    "\u1aed\u1aee\u1aef\u1af0\u1af1\u1af2\u1af3\u1af4\u1af5\u1af6\u1af7\u1af8\u1af9\u1afa\u1afb\u1afc"
    "\u1afd\u1afe\u1aff\u1dc0\u1dc1\u1dc2\u1dc3\u1dc4\u1dc5\u1dc6\u1dc7\u1dc8\u1dc9\u1dca\u1dcb\u1dcc"
    "\u1dcd\u1dce\u1dcf\u1dd0\u1dd1\u1dd2\u1dd3\u1dd4\u1dd5\u1dd6\u1dd7\u1dd8\u1dd9\u1dda\u1ddb\u1ddc"
    "\u1ddd\u1dde\u1ddf\u1de0\u1de1\u1de2\u1de3\u1de4\u1de5\u1de6\u1de7\u1de8\u1de9\u1dea\u1deb\u1dec"
    "\u1ded\u1dee\u1def\u1df0\u1df1\u1df2\u1df3\u1df4\u1df5\u1df6\u1df7\u1df8\u1df9\u1dfa\u1dfb\u1dfc"
    "\u1dfd\u1dfe\u1dff\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2061\u2062"
    "\u2063\u2064\u2066\u2067\u2068\u2069\u20d0\u20d1\u20d2\u20d3\u20d4\u20d5\u20d6\u20d7\u20d8\u20d9"
    "\u20da\u20db\u20dc\u20dd\u20de\u20df\u20e0\u20e1\u20e2\u20e3\u20e4\u20e5\u20e6\u20e7\u20e8\u20e9"
    "\u20ea\u20eb\u20ec\u20ed\u20ee\u20ef\u20f0\u20f1\u20f2\u20f3\u20f4\u20f5\u20f6\u20f7\u20f8\u20f9"
    "\u20fa\u20fb\u20fc\u20fd\u20fe\u20ff\ufe00\ufe01\ufe02\ufe03\ufe04\ufe05\ufe06\ufe07\ufe08\ufe09"
    "\ufe0a\ufe0b\ufe0c\ufe0d\ufe0e\ufe0f\ufe20\ufe21\ufe22\ufe23\ufe24\ufe25\ufe26\ufe27\ufe28\ufe29"
    "\ufe2a\ufe2b\ufe2c\ufe2d\ufe2e\ufe2f\ufeff\U000e0100\U000e0101\U000e0102\U000e0103\U000e0104\U000e0105\U000e0106\U000e0107\U000e0108"
    "\U000e0109\U000e010a\U000e010b\U000e010c\U000e010d\U000e010e\U000e010f\U000e0110\U000e0111\U000e0112\U000e0113\U000e0114\U000e0115\U000e0116\U000e0117\U000e0118"
    "\U000e0119\U000e011a\U000e011b\U000e011c\U000e011d\U000e011e\U000e011f\U000e0120\U000e0121\U000e0122\U000e0123\U000e0124\U000e0125\U000e0126\U000e0127\U000e0128"
    "\U000e0129\U000e012a\U000e012b\U000e012c\U000e012d\U000e012e\U000e012f\U000e0130\U000e0131\U000e0132\U000e0133\U000e0134\U000e0135\U000e0136\U000e0137\U000e0138"
    "\U000e0139\U000e013a\U000e013b\U000e013c\U000e013d\U000e013e\U000e013f\U000e0140\U000e0141\U000e0142\U000e0143\U000e0144\U000e0145\U000e0146\U000e0147\U000e0148"
    "\U000e0149\U000e014a\U000e014b\U000e014c\U000e014d\U000e014e\U000e014f\U000e0150\U000e0151\U000e0152\U000e0153\U000e0154\U000e0155\U000e0156\U000e0157\U000e0158"
    "\U000e0159\U000e015a\U000e015b\U000e015c\U000e015d\U000e015e\U000e015f\U000e0160\U000e0161\U000e0162\U000e0163\U000e0164\U000e0165\U000e0166\U000e0167\U000e0168"
    "\U000e0169\U000e016a\U000e016b\U000e016c\U000e016d\U000e016e\U000e016f\U000e0170\U000e0171\U000e0172\U000e0173\U000e0174\U000e0175\U000e0176\U000e0177\U000e0178"
    "\U000e0179\U000e017a\U000e017b\U000e017c\U000e017d\U000e017e\U000e017f\U000e0180\U000e0181\U000e0182\U000e0183\U000e0184\U000e0185\U000e0186\U000e0187\U000e0188"
    "\U000e0189\U000e018a\U000e018b\U000e018c\U000e018d\U000e018e\U000e018f\U000e0190\U000e0191\U000e0192\U000e0193\U000e0194\U000e0195\U000e0196\U000e0197\U000e0198"
    "\U000e0199\U000e019a\U000e019b\U000e019c\U000e019d\U000e019e\U000e019f\U000e01a0\U000e01a1\U000e01a2\U000e01a3\U000e01a4\U000e01a5\U000e01a6\U000e01a7\U000e01a8"
    "\U000e01a9\U000e01aa\U000e01ab\U000e01ac\U000e01ad\U000e01ae\U000e01af\U000e01b0\U000e01b1\U000e01b2\U000e01b3\U000e01b4\U000e01b5\U000e01b6\U000e01b7\U000e01b8"
    "\U000e01b9\U000e01ba\U000e01bb\U000e01bc\U000e01bd\U000e01be\U000e01bf\U000e01c0\U000e01c1\U000e01c2\U000e01c3\U000e01c4\U000e01c5\U000e01c6\U000e01c7\U000e01c8"
    "\U000e01c9\U000e01ca\U000e01cb\U000e01cc\U000e01cd\U000e01ce\U000e01cf\U000e01d0\U000e01d1\U000e01d2\U000e01d3\U000e01d4\U000e01d5\U000e01d6\U000e01d7\U000e01d8"
    "\U000e01d9\U000e01da\U000e01db\U000e01dc\U000e01dd\U000e01de\U000e01df\U000e01e0\U000e01e1\U000e01e2\U000e01e3\U000e01e4\U000e01e5\U000e01e6\U000e01e7\U000e01e8"
    "\U000e01e9\U000e01ea\U000e01eb\U000e01ec\U000e01ed\U000e01ee\U000e01ef"
)
# /generated-from-confusable-registry:_INVISIBLE_STRIP_PACKED


def _decode_fold_table(fold_packed, strip_packed):
    """Decode the packed fold + strip strings into one `str.translate` table.

    Fold pairs are (escaped source codepoint, literal ASCII-letter target); strip
    entries map to None (which `str.translate` deletes), so the single table both folds
    and strips in one pass. Iterates by CODE POINT -- Python string iteration yields one
    element per codepoint -- so the 919 astral fold sources and 240 astral strip
    codepoints round-trip. Fold and strip keys are disjoint (registry-checked)."""
    table = {}
    chars = iter(fold_packed)
    for src in chars:
        table[ord(src)] = next(chars)
    for ch in strip_packed:
        table[ord(ch)] = None
    return table


_FOLD_AND_STRIP_TABLE = _decode_fold_table(
    _CONFUSABLE_FOLD_PACKED, _INVISIBLE_STRIP_PACKED
)


def _fold_confusables(text):
    """Fold lookalike codepoints to ASCII and delete zero-width/boundary breakers.

    `str.translate` over the combined table -- deliberately NO `re.*` call here (the
    filter-twin unicode guard pins this module's `re.*` call census; a regex in this
    helper would break `test_discovery_finds_the_known_shape`).
    """
    return text.translate(_FOLD_AND_STRIP_TABLE)


def _injection_scan_core(findings, include_h4):
    """
    Shared core behind apply_injection_filter/apply_replay_injection_scan
    (#253). `include_h4` gates heuristic 4 (short description + high
    confidence) -- the ONE heuristic that reads finding.get("confidence"), a
    field detect_disagreement mutates IN PLACE after this scan first runs at
    filter time (the +10 consensus boost on a corroborated finding).
    Heuristics 1/2/3/5-10 read only title/description/file/line_start/id --
    static content that cannot change between a finding's first scan and a
    later re-scan -- so they are safe to re-run against anything that
    already passed them once; heuristic 4 is not, because a finding that
    failed it (80 < 85) at record time can pass it (90 >= 85) after a later
    stage boosts confidence, which would make a re-scan eliminate a finding
    the pipeline just corroborated. See apply_replay_injection_scan below for
    the caller this exists for.

    Remove findings that appear to be prompt-injection artifacts or hallucinations.

    Detection heuristics (from false-positive-exclusions.md § Prompt Injection Artifacts),
    scanned across title/description only:
      1. Combined title+description contains shell commands (rm -rf, curl, wget,
         git push, gh api)
      2. Combined title+description contains a URL to visit/download from, or an
         encoded payload (base64, hex)
      3. Combined title+description instructs the user to bypass controls, skip
         review, or auto-approve
      4. Body has fewer than 10 words AND confidence is high (>= 85) — suspiciously terse
      5. Combined title+description's tone shifts from analytical to instructional
         ("you should run this command")
      6. Combined title+description recommends adding code that introduces a
         vulnerability or disables security features
      7. Title matches known placeholder patterns (Example/Sample/test/demo/Placeholder
         finding stub vocabulary)
      8. Combined title+description contains XML-like injection markers
      9. File path is empty or contains template markers
      10. Duplicate signature (title+file+line)

    #256: all seven `_CONTENT_PATTERN_SETS` content sets (shell/url/encoded/
    bypass/instructional/vuln-intro/body-marker -- heuristics 1/2/3/5/6/8)
    scan `combined` (title+description) uniformly; there is no separate
    title-only pass. A payload split across fields -- the directive in
    `title`, the blob/body in `description` -- still fires, since the
    rendered PR comment concatenates them into one coherent instruction
    (#252 Finding 1, generalized to all seven sets by #256). Every set's
    reason string is bare (no "title "/"description " field-attribution
    prefix) since the scanned text is neither field alone; field
    attribution is a deliberately dropped capability (#256 record). This is
    a strict superset of scanning `title`/`description` separately: none of
    the seven sets' patterns anchor with ``^``/``$``/``\\A``/``\\Z``/``(?m)``
    (guarded by tests/test_filter_twins_unicode_guard.py), and the union whitespace
    class joining title and description includes `\n`, so a match spanning
    either field alone still matches `combined`. The encoded set is
    directive-gated with an adjacency window: a decode-family verb or sink
    syntax must sit within up to ~40 characters of the encoded blob
    (`[^\x00]{0,40}` between them). The url set ships only "visit"/
    "download from" -- a prior pass tried two directive-gated long-bare-URL
    entries with their own adjacency windows, but they false-fired on
    legitimate security findings that quote the same vocabulary an injected
    instruction would use (a real routing bug legitimately states "navigate
    to <url>"), so they were removed rather than tuned; url's directive
    verb must now sit immediately adjacent to the URL (whitespace-only
    gap), not "roughly N characters" away. A far-apart split (directive
    outside the adjacency window, where one applies) still evades by
    design -- adjacency-gating is inherently local, and that residual is
    accepted.

    A finding that survives all ten heuristics then has each of
    `_INJECTION_STRIPPED_PROSE_FIELDS` (`suggestion`, `claude_md_rule`,
    `spec_text`) scanned separately against the shell/url/encoded/bypass/
    instructional/vuln-intro/body-marker sets; a match strips that field
    rather than eliminating the finding (#62, extended #213) — see
    `_strip_injected_prose_fields`.

    Eliminated findings are logged via stderr for the methodology section.

    Returns (passed, eliminated) lists. Each eliminated finding gains an
    "eliminated_by" field set to "injection".
    """
    passed = []
    eliminated = []
    seen_signatures = {}

    # Compile pattern lists once. The seven content sets compile from the
    # module-level _CONTENT_PATTERN_SETS table (#215 round-1 parity-F4/F5) --
    # a single source of truth for phrase + raw-pattern-list per set, order
    # preserved exactly so `_SUGGESTION_SETS` byte-matches its pre-hoist
    # shape and every existing golden stays byte-identical.
    _SUGGESTION_SETS = tuple(
        (phrase, [re.compile(p, re.IGNORECASE | re.ASCII) for p in patterns])
        for phrase, patterns in _CONTENT_PATTERN_SETS
    )
    shell_re = _SUGGESTION_SETS[0][1]
    url_re = _SUGGESTION_SETS[1][1]
    encoded_re = _SUGGESTION_SETS[2][1]
    bypass_re = _SUGGESTION_SETS[3][1]
    instruct_re = _SUGGESTION_SETS[4][1]
    vuln_re = _SUGGESTION_SETS[5][1]
    body_marker_re = _SUGGESTION_SETS[6][1]
    # Placeholder set (heuristic 7) is NOT one of the seven content sets --
    # it never scans description, has no suggestion-field strip role, and is
    # compiled separately, as before.
    title_re = [
        re.compile(p, re.IGNORECASE | re.ASCII) for p in _INJECTION_TITLE_PATTERNS
    ]

    def _first_match(patterns, text, folded=None):
        """Return the pattern of the first regex that matches `text`, or None.

        #242 UNION scan: the RAW text is scanned first; only if `folded` is a
        distinct casefold-reachable-folded copy of `text` is the folded text
        scanned as a second pass. The RAW pass wins reason selection, so a
        finding that already matched at HEAD keeps its exact reason string and
        the fold can only ADD detections, never move or lose one. Passing no
        `folded` (or one equal to `text`) is a plain raw scan.
        """
        for rx in patterns:
            if rx.search(text):
                return rx.pattern
        if folded is not None and folded != text:
            for rx in patterns:
                if rx.search(folded):
                    return rx.pattern
        return None

    # suggestion (and, since #213, claude_md_rule/spec_text) is rendered into
    # posted PR/MR comments and reports, so payload-bearing advice must not
    # reach a human — but a benign finding must not die for its advice
    # (imperative security advice like "Never disable TLS verification"
    # legitimately resembles these patterns), so a match strips the field
    # instead of eliminating the finding (#62).

    def _strip_injected_prose_fields(finding):
        # Returns (kept_finding, first_pattern_strip). Scans
        # _INJECTION_STRIPPED_PROSE_FIELDS in list order; each PRESENT field is
        # independently stripped -- never eliminates the finding -- on a
        # non-string type or the first _SUGGESTION_SETS pattern match (#62,
        # extended to claude_md_rule/spec_text by #213). Scanning continues
        # after a match: every matching field strips.
        #
        # first_pattern_strip is the (field, phrase) pair of the FIRST field a
        # PATTERN (not a type violation) stripped, or None -- this is what
        # feeds `_strip_suggested_fix_code_if_needed`'s propagation trigger
        # (#63/D8c, #213/D2): a type-violation strip never propagates, and
        # among pattern strips only the first-in-order field names the reason.
        #
        # A present field that is not a string (possible via the retained
        # Python CLI's unvalidated --input and checkpoint resume; the JS
        # dispatch boundary's JSON schema pins string-only) is inert to the
        # scan below; a dict/list/number would reach post_review's str()
        # coercion verbatim, and a None (rendered as absent downstream) is
        # stripped too so presence + non-string type is the whole trigger (#62).
        kept = finding
        first_pattern_strip = None
        for field in _INJECTION_STRIPPED_PROSE_FIELDS:
            if field in kept and not isinstance(kept[field], str):
                kept = dict(kept)
                del kept[field]
                kept[f"{field}_removed_by"] = "injection"
                kept[f"{field}_removal_reason"] = f"{field} is not a string"
                continue
            value = kept.get(field, "")
            if not value:
                continue
            value_folded = _fold_confusables(value)
            for phrase, patterns in _SUGGESTION_SETS:
                m = _first_match(patterns, value, value_folded)
                if m:
                    kept = dict(kept)
                    del kept[field]
                    kept[f"{field}_removed_by"] = "injection"
                    kept[f"{field}_removal_reason"] = f"{field} {phrase}: {m!r}"
                    if first_pattern_strip is None:
                        first_pattern_strip = (field, phrase)
                    break
        return kept, first_pattern_strip

    def _strip_suggested_fix_code_if_needed(finding, first_pattern_strip):
        """Mirrors `_strip_injected_prose_fields`'s shape for
        `suggested_fix_code` (#63/D8): non-string strip first, then oversize,
        then propagation-on-pattern-strip. Independent of whether any prose
        field is even present -- the first two checks fire on their own
        regardless of `first_pattern_strip`.

        Deliberately NO pattern scan of the code content itself: #62 measured
        content-pattern sets killing legitimate fixes, and code trips them
        harder than prose does.
        """
        if "suggested_fix_code" not in finding:
            return finding
        code = finding["suggested_fix_code"]
        if not isinstance(code, str):
            kept = dict(finding)
            del kept["suggested_fix_code"]
            kept["suggested_fix_code_removed_by"] = "injection"
            kept["suggested_fix_code_removal_reason"] = (
                "suggested_fix_code is not a string"
            )
            return kept
        normalized = _normalize_fix_code_for_bound(code)
        if (
            len(normalized.split("\n")) > _FIX_MAX_LINES
            or len(normalized) > _FIX_MAX_CHARS
        ):
            kept = dict(finding)
            del kept["suggested_fix_code"]
            kept["suggested_fix_code_removed_by"] = "injection"
            kept["suggested_fix_code_removal_reason"] = (
                "suggested_fix_code exceeds the delivery bound"
            )
            return kept
        if first_pattern_strip is not None:
            # A patch whose accompanying prose was flagged as injection must
            # not survive as a one-click apply -- pattern-free and byte-
            # identical to the JS twin's reason (the parity test only
            # prefix-compares `{field}_removal_reason`, not this key).
            # `field` is the FIRST scanned field (list order) a pattern
            # stripped (#213/D2/D7); "suggestion" reproduces today's bytes.
            prop_field, prop_phrase = first_pattern_strip
            kept = dict(finding)
            del kept["suggested_fix_code"]
            kept["suggested_fix_code_removed_by"] = "injection"
            kept["suggested_fix_code_removal_reason"] = (
                f"{prop_field} carried {prop_phrase}"
            )
            return kept
        return finding

    for finding in findings:
        title = _as_text(finding.get("title"))
        description = _as_text(finding.get("description"))
        filepath = _as_text(finding.get("file"))
        confidence = _as_confidence(finding.get("confidence"))
        combined = f"{title}\n{description}"
        # #242 union scan: fold each scanned text ONCE per finding; the content
        # sets below scan raw-then-folded via _first_match.
        combined_folded = _fold_confusables(combined)
        title_folded = _fold_confusables(title)

        reasons = []

        # 1. Shell commands anywhere in the combined text
        m = _first_match(shell_re, combined, combined_folded)
        if m:
            reasons.append(f"contains shell command pattern: {m!r}")

        # 2a. URLs to visit -- combined title+description (#252 Finding 1:
        # a decode/exfil directive in title and the URL/blob in description
        # must be caught together, since the rendered comment concatenates
        # them into one instruction).
        m = _first_match(url_re, combined, combined_folded)
        if m:
            reasons.append(f"contains visit-URL pattern: {m!r}")

        # 2b. Encoded payloads -- combined title+description (same rationale).
        m = _first_match(encoded_re, combined, combined_folded)
        if m:
            reasons.append(f"contains encoded payload pattern: {m!r}")

        # 3. Bypass / auto-approve instructions anywhere in the combined text
        m = _first_match(bypass_re, combined, combined_folded)
        if m:
            reasons.append(f"contains bypass/auto-approve instruction: {m!r}")

        # 4. Short description with high confidence (suspiciously terse).
        # Gated on include_h4 -- see this function's docstring (#253/D1).
        if include_h4:
            description_word_count = _count_words(description)
            if (
                description_word_count < _MIN_BODY_WORDS
                and confidence >= _HIGH_CONFIDENCE_THRESHOLD
            ):
                reasons.append(
                    f"suspiciously short description ({description_word_count} words) with high confidence ({confidence})"
                )

        # 5. Instructional tone anywhere in the combined text
        m = _first_match(instruct_re, combined, combined_folded)
        if m:
            reasons.append(f"uses instructional tone: {m!r}")

        # 6. Recommends introducing vulnerability or disabling security
        # features, anywhere in the combined text
        m = _first_match(vuln_re, combined, combined_folded)
        if m:
            reasons.append(f"recommends introducing vulnerability: {m!r}")

        # 7. Title matches placeholder patterns
        m = _first_match(title_re, title, title_folded)
        if m:
            reasons.append(f"title matches placeholder pattern: {m!r}")

        # 8. Combined text contains XML-like injection markers
        m = _first_match(body_marker_re, combined, combined_folded)
        if m:
            reasons.append(f"matches injection marker: {m!r}")

        # 9. Empty or template file path
        if not filepath or re.search(r"<[^\n]*?>|\{[^\n]*?\}", filepath):
            reasons.append(
                f"file path is empty or contains template markers: {filepath!r}"
            )

        # 10. Duplicate signature (title+file+line_start). Deliberately built
        # on the UNFOLDED title: heuristic 7 scans folded text (#242), but this
        # signature keeps HEAD's raw title, so two fold-identical titles still
        # hash to distinct signatures exactly as at HEAD (dedup never folded --
        # the h7-folded / h10-unfolded split is intentional). The title strip is
        # the union whitespace class via `_WS_TRIM_RE.sub` (#244 (a), the shared
        # union-trim constant), so the two twins anchor-strip the same six
        # codepoints Python `str.strip()` and JS `trim()` disagreed on;
        # `line_start` stays RAW in the signature, NOT routed through
        # `_line_bucket` (mechanism (b) is a separate site).
        sig = (
            _WS_TRIM_RE.sub("", title.lower()),
            filepath,
            finding.get("line_start"),
        )
        if sig in seen_signatures:
            reasons.append(f"duplicate of finding {seen_signatures[sig]!r}")
        else:
            seen_signatures[sig] = finding.get("id", title)

        if reasons:
            elim = dict(finding)
            elim["eliminated_by"] = "injection"
            elim["elimination_reason"] = "; ".join(reasons)
            eliminated.append(elim)
            warn(
                f"[injection-filter] Discarded finding {finding.get('id', '?')!r}: "
                + reasons[0]
            )
        else:
            kept, first_pattern_strip = _strip_injected_prose_fields(finding)
            passed.append(
                _strip_suggested_fix_code_if_needed(kept, first_pattern_strip)
            )

    return passed, eliminated


def apply_injection_filter(findings):
    """Record-time entry point (main(), via the filter pipeline), byte-
    identical to its pre-#253 shape: all 10 heuristics, including heuristic 4.
    """
    return _injection_scan_core(findings, True)


def apply_replay_injection_scan(findings):
    """#253 replay filtering belt (workflows/src/stages.js): re-scans
    findings that already survived apply_injection_filter once, at record
    time, against a challenge checkpoint the pipeline is now REPLAYING
    (persisted by an earlier version, under earlier content patterns) or a
    fresh challenge-stage output (a no-op by construction there, since the
    filter stage already ran this same content scan this run). Structurally
    excludes heuristic 4 -- see _injection_scan_core's docstring -- so the
    belt's callable unit is confidence-free BY CONSTRUCTION, not by caller
    discipline. Heuristic 10 (duplicate signature) is proven unable to newly
    fire here: nothing between record-time apply_injection_filter and a
    challenge checkpoint mutates a finding's (title, file, line_start)
    triple (detect_disagreement/consolidate_cross_agent/apply_challenges
    touch only confidence/severity/stamp fields), and a dedup re-run over a
    SUBSET of the originally-deduped set can only fire fewer times, never
    newly. This function has no Python caller today (the replay belt is
    JS-only pipeline code) -- it exists so the two runtimes stay parity-
    provable; workflows/test/tools/record_parity.py exercises it directly.
    """
    return _injection_scan_core(findings, False)


# ---------------------------------------------------------------------------
# Disagreement detection
# ---------------------------------------------------------------------------

# Agent names used in suppression rules (must match "agent" field on findings)
_AGENT_BUG_DETECTOR = "bug-detector"
_AGENT_CONVENTIONS = "conventions-and-intent"
_AGENT_TEST_ANALYZER = "test-analyzer"
_AGENT_SECURITY_REVIEWER = "security-reviewer"

# Flat confidence boost applied to consensus findings (spec: +10, capped at 100)
_CONSENSUS_BOOST = 10

# Flat confidence penalty for singleton findings in non-core dimensions (BF-15b)
_SINGLETON_PENALTY = 15

# Core dimensions exempt from singleton penalty (real bugs trigger multiple agents)
_CORE_DIMENSIONS = {"bug", "security", "cross_file_impact", "intent"}


def detect_disagreement(findings):
    """
    Detect consensus, singleton, contradiction, and security-escalation patterns
    across findings from multiple review agents.

    Rules applied (per spec section 6c):

    Consensus:
      Two or more findings land in the same (file, 10-line bucket), regardless of
      agent or concern. Boost confidence +10 (capped at 100). Annotate with
      corroborated_by list (other agents in the bucket, excluding same-agent
      duplicates — which can leave corroborated_by empty).

    Singleton:
      One finding only in its bucket — annotated with consensus_count=1, and
      penalized -15 confidence when its dimension is set and outside
      _CORE_DIMENSIONS (BF-15b).

    Contradiction:
      Agents conflict on the same location (opposing severity signals). Flag with
      contradiction=True for human review.

    Suppression rules:
      - bug-detector AND conventions-and-intent both report on the same location,
        and conventions-and-intent labels the behaviour as intentional: suppress the
        bug finding (eliminated_by="suppressed:intentional").
      - test-analyzer AND conventions-and-intent both report on the same location,
        and conventions-and-intent labels it as generated/scaffolding: suppress the
        test finding (eliminated_by="suppressed:generated").

    Security escalation:
      Security-reviewer flags a location AND another agent says the same location is
      safe (low severity): keep the security finding. Both findings are annotated with
      security_escalation=True.

    Returns (active_findings, suppressed_findings, boosted_count):
      active_findings    list   findings that survived suppression, each with consensus metadata
      suppressed_findings list  findings removed by suppression rules
      boosted_count      int    number of findings whose confidence was boosted
    """

    # -----------------------------------------------------------------------
    # Phase 1: Group findings by (file, line_bucket) for co-location checks
    # -----------------------------------------------------------------------
    location_groups = {}
    for finding in findings:
        key = (finding.get("file", ""), _line_bucket(finding.get("line_start", 0), 10))
        location_groups.setdefault(key, []).append(finding)

    # -----------------------------------------------------------------------
    # Phase 2: Apply suppression rules to co-located findings
    # -----------------------------------------------------------------------
    suppressed_ids = set()
    suppressed = []

    for group in location_groups.values():
        if len(group) < 2:
            continue

        agent_map = {}
        for f in group:
            agent = f.get("agent", "").lower()
            agent_map.setdefault(agent, []).append(f)

        # Suppression rule 1: bug-detector + conventions-and-intent -> intentional
        if _AGENT_BUG_DETECTOR in agent_map and _AGENT_CONVENTIONS in agent_map:
            for conv_finding in agent_map[_AGENT_CONVENTIONS]:
                conv_text = (
                    _as_text(conv_finding.get("description"))
                    + " "
                    + _as_text(conv_finding.get("title"))
                ).lower()
                if re.search(
                    r"\bintentional\b|\bby[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+design\b|\bexpected[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+behavior\b|\bdeliberate\b",
                    conv_text,
                    re.ASCII,
                ):
                    for bug_finding in agent_map[_AGENT_BUG_DETECTOR]:
                        fid = bug_finding.get("id", id(bug_finding))
                        if fid not in suppressed_ids:
                            suppressed_ids.add(fid)
                            sup = dict(bug_finding)
                            sup["eliminated_by"] = "suppressed:intentional"
                            sup["elimination_reason"] = (
                                f"conventions-and-intent confirms behaviour at "
                                f"{bug_finding.get('file', '?')}:{bug_finding.get('line_start', '?')} "
                                f"is intentional"
                            )
                            suppressed.append(sup)
                    break

        # Suppression rule 2: test-analyzer + conventions-and-intent -> generated/scaffolding
        if _AGENT_TEST_ANALYZER in agent_map and _AGENT_CONVENTIONS in agent_map:
            for conv_finding in agent_map[_AGENT_CONVENTIONS]:
                conv_text = (
                    _as_text(conv_finding.get("description"))
                    + " "
                    + _as_text(conv_finding.get("title"))
                ).lower()
                if re.search(
                    r"\bgenerated\b|\bscaffolding\b|\bauto[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]?generated\b|\bboilerplate\b",
                    conv_text,
                    re.ASCII,
                ):
                    for test_finding in agent_map[_AGENT_TEST_ANALYZER]:
                        fid = test_finding.get("id", id(test_finding))
                        if fid not in suppressed_ids:
                            suppressed_ids.add(fid)
                            sup = dict(test_finding)
                            sup["eliminated_by"] = "suppressed:generated"
                            sup["elimination_reason"] = (
                                f"conventions-and-intent confirms code at "
                                f"{test_finding.get('file', '?')}:{test_finding.get('line_start', '?')} "
                                f"is generated/scaffolding"
                            )
                            suppressed.append(sup)
                    break

    # Remove suppressed findings from the active list
    active = [f for f in findings if f.get("id", id(f)) not in suppressed_ids]

    # -----------------------------------------------------------------------
    # Phase 3: Consensus grouping (file + line_bucket + degraded)
    # -----------------------------------------------------------------------
    # `degraded` (origin == "unknown") is folded into the grouping key so a
    # degraded (verify-echo-unavailable) finding only corroborates other
    # degraded findings, and a verified finding only corroborates other
    # verified findings -- never across (#73 D3a). On a UNIFORM-origin run
    # (all-verified or all-degraded), `degraded` is constant across every
    # finding, so it changes no group membership and the boosted output is
    # byte-identical to before this extension (#73 req 2 regression pin).
    consensus_groups = {}
    for finding in active:
        file_ = finding.get("file", "")
        line = _line_bucket(finding.get("line_start", 0), 10)
        degraded = finding.get("origin", "") == "unknown"
        group_key = (file_, line, degraded)
        consensus_groups.setdefault(group_key, []).append(finding)

    boosted_count = 0
    for group in consensus_groups.values():
        count = len(group)
        agents_in_group = [f.get("agent", "") for f in group if f.get("agent")]

        if count > 1:
            # Consensus: 2+ findings in this (file, 10-line bucket) — regardless of
            # agent or concern, so same-agent siblings boost each other too.
            boosted_count += count
            for finding in group:
                other_agents = [
                    a for a in agents_in_group if a != finding.get("agent", "")
                ]
                finding["consensus_count"] = count
                finding["consensus_boost"] = _CONSENSUS_BOOST
                finding["corroborated_by"] = other_agents
                original_conf = _as_confidence(finding.get("confidence"))
                finding["confidence"] = min(original_conf + _CONSENSUS_BOOST, 100)
        else:
            # Singleton — apply confidence penalty for non-core dimensions (BF-15b)
            finding = group[0]
            finding["consensus_count"] = 1
            finding["consensus_boost"] = 0
            finding.setdefault("corroborated_by", [])

            dimension = finding.get("dimension", "").lower()
            if dimension and dimension not in _CORE_DIMENSIONS:
                original_conf = _as_confidence(finding.get("confidence"))
                finding["confidence"] = max(0, original_conf - _SINGLETON_PENALTY)
                finding["singleton_penalty"] = True

    # -----------------------------------------------------------------------
    # Phase 4: Contradiction and security escalation detection
    # -----------------------------------------------------------------------
    location_groups_active = {}
    for finding in active:
        key = (finding.get("file", ""), finding.get("line_start", 0))
        location_groups_active.setdefault(key, []).append(finding)

    for group in location_groups_active.values():
        if len(group) < 2:
            group[0].setdefault("contradiction", False)
            group[0].setdefault("security_escalation", False)
            continue

        severities = {(_as_text(f.get("severity")) or "low").lower() for f in group}
        agents_here = {f.get("agent", "").lower() for f in group}

        # Basic contradiction: critical vs low at same file+line
        has_contradiction = "critical" in severities and "low" in severities

        # Security escalation: security-reviewer flags AND another agent says low/safe
        has_security_escalation = (
            _AGENT_SECURITY_REVIEWER in agents_here
            and len(agents_here) > 1
            and "low" in severities
        )

        for finding in group:
            finding["contradiction"] = has_contradiction
            finding["security_escalation"] = has_security_escalation
            if (
                has_security_escalation
                and finding.get("agent", "").lower() == _AGENT_SECURITY_REVIEWER
            ):
                finding["escalation_note"] = (
                    "Kept: security-reviewer finding retained despite conflicting low-severity "
                    "signal from another agent (security escalation rule)"
                )

    # Ensure all active findings have default metadata fields
    for finding in active:
        finding.setdefault("consensus_count", 1)
        finding.setdefault("consensus_boost", 0)
        finding.setdefault("corroborated_by", [])
        finding.setdefault("contradiction", False)
        finding.setdefault("security_escalation", False)

    return active, suppressed, boosted_count


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------

# Agents whose findings default to the main code-correctness report
_MAIN_REPORT_AGENTS = {
    "bug-detector",
    "security-reviewer",
    "cross-file-impact",
    "type-design-analyzer",
}

# Agents whose findings default to improvement suggestions
_SUGGESTION_AGENTS = {
    "test-analyzer",
    "code-simplifier",
}

# For conventions-and-intent: pass-3 is comment accuracy -> suggestion.
# Passes 1-2 (intent/convention checks) -> main.
# Detection: the finding's dimension is in _COMMENT_ACCURACY_DIMENSIONS.
_CONVENTIONS_AGENT = "conventions-and-intent"
_COMMENT_ACCURACY_DIMENSIONS = {"comment-accuracy", "documentation", "doc-accuracy"}

# ---------------------------------------------------------------------------
# Dimension-based routing (BF-15a)
# ---------------------------------------------------------------------------

# Dimensions that always route to "suggestion" (never real defects)
_SUGGESTION_DIMENSIONS = {"comment_accuracy", "comment-accuracy"}

# Dimensions that always route to "main" (core defect categories)
_MAIN_DIMENSIONS = {"bug", "security", "cross_file_impact", "intent"}

# Dimensions routed to "suggestion" UNLESS functional-violation keywords present
_CONDITIONAL_SUGGESTION_DIMENSIONS = {"test_coverage", "convention", "type_design"}

# Keywords that promote convention/type_design findings from suggestion to main
# These indicate the finding describes a FUNCTIONAL violation, not just style
# generated-from-filter-pattern-registry:_FUNCTIONAL_VIOLATION_KEYWORDS do not edit; run scripts/generate_filter_patterns.py
_FUNCTIONAL_VIOLATION_KEYWORDS = re.compile(
    r"\bcrash\b|\bdata[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+loss\b|\bsilent(?:ly)?\b|\bincorrect\b|\bwrong\b|\bfail(?:s|ure)?\b|\bruntime[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+error\b|\bexception\b|\bpanic\b|\bundefined[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+behavio(?:u)?r\b",
    re.IGNORECASE | re.ASCII,
)
# /generated-from-filter-pattern-registry:_FUNCTIONAL_VIOLATION_KEYWORDS

# Keywords that promote type_design findings from suggestion to main
# These indicate a type safety bug that could cause runtime errors
# generated-from-filter-pattern-registry:_TYPE_SAFETY_BUG_KEYWORDS do not edit; run scripts/generate_filter_patterns.py
_TYPE_SAFETY_BUG_KEYWORDS = re.compile(
    r"\bruntime\b|\bcastexception\b|\btype[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+error\b|\bclasscastexception\b|\bnull[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+pointer\b|\bnullpointer\b|\btype[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+mismatch\b",
    re.IGNORECASE | re.ASCII,
)
# /generated-from-filter-pattern-registry:_TYPE_SAFETY_BUG_KEYWORDS


def _route_by_dimension(finding):
    """
    Determine routing based on the finding's dimension field (BF-15a).

    Returns "main", "suggestion", or None (if dimension doesn't determine routing,
    fall through to agent-based routing).

    Routing rules:
      - bug, security, cross_file_impact, intent -> main (always)
      - comment_accuracy -> suggestion (always)
      - test_coverage -> suggestion (unless functional correctness keywords)
      - convention -> suggestion (unless functional violation keywords)
      - type_design -> suggestion (unless type safety bug keywords)
      - unknown/missing dimension -> None (fall through to agent-based routing)
    """
    dimension = finding.get("dimension", "").lower()
    if not dimension:
        return None

    # Core defect dimensions -> always main
    if dimension in _MAIN_DIMENSIONS:
        return "main"

    # Always-suggestion dimensions
    if dimension in _SUGGESTION_DIMENSIONS:
        return "suggestion"

    # Conditional suggestion dimensions with keyword-based promotion
    if dimension in _CONDITIONAL_SUGGESTION_DIMENSIONS:
        title = _as_text(finding.get("title"))
        description = _as_text(finding.get("description"))
        combined = f"{title}\n{description}"

        if dimension == "test_coverage":
            # Same promotion logic as _is_test_correctness_finding
            for pattern in _TEST_CORRECTNESS_PATTERNS:
                if pattern.search(combined):
                    return "main"
            return "suggestion"

        if dimension == "convention":
            if _FUNCTIONAL_VIOLATION_KEYWORDS.search(combined):
                return "main"
            return "suggestion"

        if dimension == "type_design":
            if _TYPE_SAFETY_BUG_KEYWORDS.search(combined):
                return "main"
            return "suggestion"

    # Unknown dimension -> fall through to agent-based routing
    return None


# Keyword patterns in test-analyzer finding titles/bodies that indicate
# a functional correctness issue today (-> promote to main report).
# These describe bugs that EXIST NOW, not tests that should be written.
# generated-from-filter-pattern-registry:_TEST_CORRECTNESS_PATTERNS do not edit; run scripts/generate_filter_patterns.py
_TEST_CORRECTNESS_PATTERNS = [
    re.compile(
        r"\brace[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+condition\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\balways[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+pass(?:es)?\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\balways[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]pass(?:es)?\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bnever[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+fail(?:s)?\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bvacuous(?:ly)?\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\btautolog(?:y|ical)\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bassert(?:ion)?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:is[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?never[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+reached\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bdeadlock\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bdata[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+race\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bthread[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:safety|unsafe|race)\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\btest[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:never[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?(?:actually[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?(?:verif|test|check)(?:s|ies)?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+nothing\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bfalse[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+positive[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:test|assertion)\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bincorrect(?:ly)?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:assert|verify|test)\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bwrong[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:value|result|output)\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\blocal[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+variable[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:is[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?never[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:used|read)\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bassert(?:s|ion)?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:on[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?(?:a[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?(?:local|copy|snapshot)\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bcompares?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:wrong|incorrect|different)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+object\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\btest[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:does[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+not|doesn'?t)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:wait|join|block)\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\breader[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+thread[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+not[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+waited\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bflaky[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+test\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bassertion[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+always[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:true|passes?|succeed)\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bassert(?:s|ion)?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:is[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?always[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:true|pass(?:es?)?|succeed)\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\btest[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:is[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?always[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:true|pass(?:es?)?|succeed)\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\blogic[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+error\b",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"\bincorrect[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:logic|behavior|behaviour|result)\b",
        re.IGNORECASE | re.ASCII,
    ),
]
# /generated-from-filter-pattern-registry:_TEST_CORRECTNESS_PATTERNS


def _is_test_correctness_finding(finding):
    """
    Return True if a test-analyzer finding describes a functional correctness bug
    that exists today (race condition, logic error, always-pass assertion, etc.)
    rather than a coverage gap ("should add tests for X").

    Decision test: "Does this finding describe a bug that exists today,
    or a test that should be written?"
    """
    title = _as_text(finding.get("title"))
    description = _as_text(finding.get("description"))
    combined = f"{title}\n{description}"

    return any(pattern.search(combined) for pattern in _TEST_CORRECTNESS_PATTERNS)


def group_by_proximity(findings, line_proximity=5):
    """
    Group findings by (file, line_bucket) where line_bucket is computed by
    rounding line_start to the nearest ``line_proximity`` lines.

    Two findings are considered co-located when they reference the same file
    and their line_start values round to the same multiple of ``line_proximity``.
    Note this is bucketing, not a pairwise distance test: two lines straddling a
    bucket boundary are not grouped even when adjacent.

    Returns a dict mapping (file, line_bucket) -> list[finding].

    This utility is shared between consolidate_cross_agent and apply_challenges.py,
    which re-runs that consolidation after challenge scoring. (Challenge results
    are matched to findings by id, not by proximity.)
    """

    groups = {}
    for finding in findings:
        fpath = finding.get("file", "")
        bucket = _line_bucket(finding.get("line_start", 0), line_proximity)
        groups.setdefault((fpath, bucket), []).append(finding)

    return groups


def consolidate_cross_agent(findings):
    """
    Generalized cross-agent consolidation (#22 D1): when two or more findings
    from *different* agents reference the same file and are within 5 lines of
    each other, NOTHING is dropped -- every member with a truthy "id" is
    stamped with a shared ``consolidation_key`` and exactly one is stamped
    ``consolidation_primary=True`` (the rest ``False``). A finding without a
    truthy "id" is immune -- it passes through completely unstamped.

    Primary selection priority (highest priority first) -- deliberately
    ORIGIN-BLIND (#22 D3): since nothing is dropped, origin cannot cost
    delivery here, unlike rank_findings/detect_disagreement which do gate on
    origin.
      1. Core dimension beats non-core.
         Core dimensions: ``bug``, ``security``, ``cross_file_impact``, ``intent``.
      2. Higher ``confidence`` value wins (after the above).
      3. Longer ``description`` string wins (tie-break).

    Groups where all findings come from the *same* agent, and singleton
    groups, are left entirely unstamped.

    Returns (findings, consolidated_count) -- ``findings`` is the SAME list
    object passed in (members mutated in place), ``consolidated_count`` is
    the number of findings stamped with a consolidation_key.
    """
    LINE_PROXIMITY = 5

    # Clear any stamps from a prior pass first: a re-run (apply_challenges.py,
    # after a group's primary is eliminated) must not leave survivors
    # carrying a consolidation_key/consolidation_primary from a group that no
    # longer qualifies.
    for f in findings:
        if isinstance(f, dict):
            f.pop("consolidation_key", None)
            f.pop("consolidation_primary", None)

    def _winner_key(f):
        """Higher key value = better priority (sort descending)."""
        dim = f.get("dimension", "").lower()
        is_core = dim in _CORE_DIMENSIONS
        conf = _as_confidence(f.get("confidence"))
        desc_len = len(_as_text(f.get("description")))
        return (int(is_core), conf, desc_len)

    groups = group_by_proximity(findings, line_proximity=LINE_PROXIMITY)

    consolidated_count = 0

    # The group's own key IS (file, bucket) — reuse it rather than recomputing
    # the bucket here, so group_by_proximity stays the single definition of the
    # bucketing formula.
    for (fpath, bucket), group in groups.items():
        # Only consolidate when 2+ *different* agents appear
        agents_in_group = {f.get("agent", "").lower() for f in group}
        if len(group) < 2 or len(agents_in_group) < 2:
            continue  # no stamps

        consolidation_key = f"{fpath}:{bucket}"

        ranked = sorted(group, key=_winner_key, reverse=True)
        primary = next((f for f in ranked if f.get("id", "")), None)

        for f in group:
            if not f.get("id", ""):
                continue  # id-less findings stay unstamped
            f["consolidation_key"] = consolidation_key
            f["consolidation_primary"] = f is primary
            consolidated_count += 1

    return findings, consolidated_count


def tag_findings(findings):
    """
    Tag each finding as "main" (main report) or "suggestion" (improvement suggestions)
    and apply cross-agent consolidation.

    Step 1 — Cross-agent consolidation: findings from 2+ different agents on
    the same file within 5 lines are stamped (never dropped) with a shared
    consolidation_key and one consolidation_primary, chosen by core-dimension,
    then confidence, then description length (see consolidate_cross_agent).

    Step 2 — Reachability routing: a reachability-demoted finding always routes to
    suggestion and receives ``routed_by="reachability"``.

    Step 3 — Dimension-based routing (BF-15a): Check the finding's dimension field
    first. Dimensions like bug/security/cross_file_impact/intent always route to main.
    Dimensions like comment_accuracy always route to suggestion. Conditional dimensions
    (test_coverage, convention, type_design) route to suggestion unless functional-
    violation keywords are present.

    Step 4 — Agent-based routing (fallback): If dimension routing returned None
    (unknown or missing dimension), fall back to agent-based rules:

      Agent routing:
        Main report:        bug-detector, security-reviewer, cross-file-impact[-analyzer],
                            type-design-analyzer, conventions-and-intent (passes 1-2)
        Improvement suggestion: test-analyzer, code-simplifier,
                            conventions-and-intent (pass 3: comment accuracy)

      Promotion rule (test-analyzer only):
        If a test-analyzer finding describes a functional correctness issue that exists
        TODAY (race condition, logic error, always-pass assertion, flaky test due to
        synchronization) rather than a coverage gap, promote it to main report.
        Decision: "Does this describe a bug today, or a test to write?"

      Conventions-and-intent disambiguation:
        Pass 3 (comment accuracy) is identified by the presence of a dimension in
        _COMMENT_ACCURACY_DIMENSIONS.  All other conventions-and-intent findings
        (passes 1-2: intent and convention checks) -> main report.

      Fallback (unknown agent):
        severity critical/high -> main; otherwise -> main.
        (Unknown agents are conservatively routed to main to avoid suppressing real bugs.)

    Each finding gains a "report_destination" field ("main" | "suggestion").
    The legacy "report_tag" alias is also written for backward compatibility.

    Returns (tagged_findings, consolidated_count, main_count, suggestion_count).
    """
    # Step 1: Cross-agent consolidation (stamps, never drops -- #22 D1)
    findings, consolidated_count = consolidate_cross_agent(findings)

    # Steps 2, 3 & 4: reachability routing, dimension-based routing, then agent-based fallback
    main_count = 0
    suggestion_count = 0

    for finding in findings:
        agent = finding.get("agent", "").lower()
        dimensions = (
            {finding.get("dimension", "").lower()}
            if finding.get("dimension")
            else set()
        )

        # Step 2: Reachability demotion takes precedence over every other route.
        if finding.get("demoted_by") == "reachability":
            destination = "suggestion"
            finding["routed_by"] = "reachability"
        else:
            # Step 3: Try dimension-based routing first (BF-15a)
            dim_route = _route_by_dimension(finding)
            if dim_route is not None:
                destination = dim_route
                if dim_route == "suggestion":
                    finding["routed_by"] = "dimension"
            else:
                # Step 4: Fall back to agent-based routing
                if agent in _MAIN_REPORT_AGENTS:
                    destination = "main"

                elif agent == _CONVENTIONS_AGENT:
                    # Pass 3 (comment accuracy) -> suggestion; passes 1-2 -> main
                    if dimensions & _COMMENT_ACCURACY_DIMENSIONS:
                        destination = "suggestion"
                    else:
                        destination = "main"

                elif agent in _SUGGESTION_AGENTS:
                    if agent == _AGENT_TEST_ANALYZER:
                        # Promotion rule: functional correctness bugs -> main report
                        if _is_test_correctness_finding(finding):
                            destination = "main"
                            finding["promoted_from"] = "test-analyzer"
                            finding["promotion_reason"] = (
                                "test-analyzer finding describes a functional correctness issue "
                                "that exists today (not a missing-coverage gap)"
                            )
                        else:
                            destination = "suggestion"
                    else:
                        destination = "suggestion"
                else:
                    # Unknown agent — conservative fallback: route to main
                    destination = "main"

        finding["report_destination"] = destination
        finding["report_tag"] = destination  # backward-compat alias
        if destination == "main":
            main_count += 1
        else:
            suggestion_count += 1

    return findings, consolidated_count, main_count, suggestion_count


# ---------------------------------------------------------------------------
# Exclusions loader
# ---------------------------------------------------------------------------


def load_exclusions(path):
    """
    Load false-positive exclusion patterns from a markdown file.

    Expects one pattern per line in a fenced code block or bullet list.
    Returns a list of plain string patterns (not compiled regexes).
    """
    if path is None:
        return []

    try:
        with open(path) as fh:
            text = fh.read()
    except FileNotFoundError:
        warn(f"Exclusions file not found at {path!r}; no exclusions applied.")
        return []
    except OSError as e:
        warn(f"Could not read exclusions file: {e}; no exclusions applied.")
        return []

    patterns = []

    # Extract from fenced code blocks first. `.` under DOTALL became `[^\x00]`
    # and the line split became the converged `_split_review_lines` (universal-
    # newline: \r\n | \r | \n) -- both respells mirror the JS twin exactly
    # (issue #243).
    block_match = _REVIEW_EXCL_BLOCK_RE.search(text)
    if block_match:
        for line in _split_review_lines(block_match.group(1)):
            line = _WS_TRIM_RE.sub("", line)
            if line and not line.startswith("#"):
                patterns.append(line)
        return patterns

    # Fallback: bullet list items. The tail is `([^\n]+)$` (explicit, identical
    # in both engines) rather than `(.+)$` -- JS `.` excludes \r/U+2028/U+2029,
    # so the old JS twin silently applied ZERO exclusions on such input.
    for line in _split_review_lines(text):
        m = _REVIEW_EXCL_BULLET_RE.match(line)
        if m:
            patterns.append(_WS_TRIM_RE.sub("", m.group(1)))

    return patterns


def apply_exclusions(findings, exclusion_patterns, config=None):
    """
    Remove findings whose title, description, or suggestion matches an exclusion
    pattern. REVIEW.md patterns come from the root and matching subtree layers;
    external exclusion_patterns are global and run after them. suggestion is
    included because it is rendered into posted PR/MR
    comments same as description — user-authored ignore patterns are the user's
    kill-switch over everything that gets rendered (#62).

    claude_md_rule/spec_text are also rendered into posted comments (#213 gives
    them the same seven-set injection scan as suggestion), but are deliberately
    NOT added here: the actual discriminator is not "gets rendered" (true of
    all three) but cost. A `suggestion` exclusion match costs only that one
    agent-authored field; claude_md_rule/spec_text quote the user's own repo
    text, and a common CLAUDE.md phrasing (e.g. "MUST") would, via this
    whole-finding elimination, mass-eliminate the conventions dimension for an
    unbounded recall cost that the field-strip mechanism above does not carry.
    A user kill-switch reaching rendered citation text was declined on the
    #247 measurement (2026-08-31): the natural CLAUDE.md pattern eliminates 0
    findings today and widens 12 via model boilerplate, not user repo text.

    Returns (passed, eliminated) lists. Each eliminated finding gains
    "eliminated_by" = "exclusion".
    """
    if config is None and not exclusion_patterns:
        return findings, []

    has_config = config is not None
    external_patterns = list(exclusion_patterns or [])
    passed = []
    eliminated = []

    for finding in findings:
        patterns = (
            config_for_file(config, finding.get("file"))["ignore"] + external_patterns
            if has_config
            else external_patterns
        )
        title = _as_text(finding.get("title"))
        description = _as_text(finding.get("description"))
        raw_suggestion = finding.get("suggestion")
        suggestion = raw_suggestion if isinstance(raw_suggestion, str) else ""
        combined = f"{title}\n{description}\n{suggestion}"

        matched_pattern = None
        for pattern in patterns:
            # Deliberately NOT re.ASCII (#211 decision item 1): these are
            # user-authored REVIEW.md ignore patterns over arbitrary-script
            # finding text, not first-party fixed patterns -- re.ASCII here
            # would break e.g. "café" matching "CAFÉ" (measured regression;
            # pinned by the exclusions/case_fold_unicode_cafe fixture and the
            # TestFilterTwinsUnicodeGuard.test_apply_exclusions_has_no_re_ascii
            # structural guard). JS keeps this folding by construction (no /u).
            if re.search(re.escape(pattern), combined, re.IGNORECASE):
                matched_pattern = pattern
                break

        if matched_pattern:
            elim = dict(finding)
            elim["eliminated_by"] = "exclusion"
            elim["elimination_reason"] = (
                f"matched exclusion pattern: {matched_pattern!r}"
            )
            eliminated.append(elim)
        else:
            passed.append(finding)

    return passed, eliminated


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def apply_filter_pipeline(findings, config, exclusion_patterns, generated_at):
    """Compose the pure filtering stages with one effective config per finding."""
    total = len(findings)
    normalize_field_names(findings)
    apply_reachability_demotion(findings)

    all_eliminated = []
    findings, elim_threshold, contested_count = apply_threshold_filter(findings, config)
    all_eliminated.extend(elim_threshold)
    passed_threshold = len(findings)

    findings, elim_exclusions = apply_exclusions(findings, exclusion_patterns, config)
    all_eliminated.extend(elim_exclusions)
    exclusions_removed = len(elim_exclusions)

    findings, elim_injection = apply_injection_filter(findings)
    all_eliminated.extend(elim_injection)
    injections_removed = len(elim_injection)
    prose_fields_removed = {
        f"{field}s_removed": sum(
            1 for f in findings if f.get(f"{field}_removed_by") == "injection"
        )
        for field in _INJECTION_STRIPPED_PROSE_FIELDS
    }
    suggested_fix_codes_removed = sum(
        1 for f in findings if f.get("suggested_fix_code_removed_by") == "injection"
    )

    findings, elim_suppressed, consensus_boosted = detect_disagreement(findings)
    all_eliminated.extend(elim_suppressed)
    findings, cross_agent_consolidated, tagged_main, tagged_suggestion = tag_findings(
        findings
    )
    promoted_count = sum(
        1 for f in findings if f.get("promoted_from") == "test-analyzer"
    )
    dimension_routed = sum(1 for f in findings if f.get("routed_by") == "dimension")
    reachability_demoted = sum(
        1 for f in findings + all_eliminated if f.get("demoted_by") == "reachability"
    )
    singleton_penalized = sum(
        1 for f in findings + all_eliminated if f.get("singleton_penalty")
    )

    return {
        "filtered": findings,
        "eliminated": all_eliminated,
        "stats": {
            "total": total,
            "passed_threshold": passed_threshold,
            "contested_count": contested_count,
            "exclusions_removed": exclusions_removed,
            "injections_removed": injections_removed,
            **prose_fields_removed,
            "suggested_fix_codes_removed": suggested_fix_codes_removed,
            "consensus_boosted": consensus_boosted,
            "singleton_penalized": singleton_penalized,
            "dimension_routed": dimension_routed,
            "reachability_demoted": reachability_demoted,
            "cross_agent_consolidated": cross_agent_consolidated,
            "test_analyzer_promoted": promoted_count,
            "tagged_main": tagged_main,
            "tagged_suggestion": tagged_suggestion,
        },
        "generated_at": generated_at,
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic Phase 6 filter for code-gauntlet findings. "
            "Applies confidence/severity thresholds, injection detection, "
            "disagreement scoring, and output tagging."
        )
    )
    parser.add_argument(
        "findings_json",
        help="Path to verified findings JSON (from verify_findings.py or Phase 5 output).",
    )
    parser.add_argument(
        "--review-md",
        metavar="PATH",
        default=None,
        help="Path to REVIEW.md for custom confidence_threshold, severity_threshold, and ignore patterns.",
    )
    parser.add_argument(
        "--exclusions-md",
        metavar="PATH",
        default=None,
        help="Path to false-positive-exclusions.md. Omit to skip exclusion filtering.",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help="Write output JSON to this file instead of stdout.",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Load input findings
    # ------------------------------------------------------------------
    try:
        with open(args.findings_json) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        die(f"Findings file not found: {args.findings_json}")
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in findings file: {e}")

    # Accept either a bare array or {"findings": [...]} envelope
    if isinstance(raw, list):
        findings = raw
    elif isinstance(raw, dict):
        findings = raw.get("findings", [])
    else:
        die("findings_json must be a JSON array or an object with a 'findings' key.")

    # ------------------------------------------------------------------
    # Parse REVIEW.md config
    # ------------------------------------------------------------------
    # No REVIEW.md at all -- omit the threshold keys entirely (issue #94 F7) rather
    # than pinning DEFAULT_CONFIDENCE_THRESHOLD/DEFAULT_SEVERITY_THRESHOLD, so
    # apply_threshold_filter's own config-absent fallback (55 non-security / 70
    # security) actually takes effect for the retained CLI, matching
    # parse_review_md's contract above.
    config = parse_review_md(args.review_md) if args.review_md else {"ignore": []}

    # ------------------------------------------------------------------
    # Load exclusions
    # ------------------------------------------------------------------
    exclusion_patterns = load_exclusions(args.exclusions_md)
    result = apply_filter_pipeline(
        findings,
        config,
        exclusion_patterns,
        datetime.now(timezone.utc).isoformat(),
    )

    summary = [
        f"Output written to {args.output}",
        f"  {len(result['filtered'])} finding(s) passed, {len(result['eliminated'])} eliminated.",
    ]
    try:
        write_result(args.output, result, summary)
    except OSError as e:
        die(f"Could not write output file: {e}")


if __name__ == "__main__":
    main()
