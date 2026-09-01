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
    r"(?:^|\n)[ \t]*confidence_threshold[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[:=][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*([0-9]{1,3})",
    re.IGNORECASE | re.ASCII,
)
# /generated-from-filter-pattern-registry:_REVIEW_CONFIDENCE_RE
# generated-from-filter-pattern-registry:_REVIEW_SECURITY_RE do not edit; run scripts/generate_filter_patterns.py
_REVIEW_SECURITY_RE = re.compile(
    r"(?:^|\n)[ \t]*security_min_confidence[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[:=][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*([0-9]{1,3})",
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


def parse_review_md(path):
    """
    Extract confidence_threshold, severity_threshold, and ignore patterns from REVIEW.md.

    Returns a dict with `ignore` always present (default: []). `confidence_threshold`,
    `security_min_confidence`, and `severity_threshold` are present ONLY when the
    corresponding key was actually found in the file (issue #94 adversarial review
    F7) -- this dict is not pre-filled with DEFAULT_CONFIDENCE_THRESHOLD /
    DEFAULT_SECURITY_MIN_CONFIDENCE / DEFAULT_SEVERITY_THRESHOLD. A caller reading an
    absent key must use `.get(key, DEFAULT)` -- apply_threshold_filter already does,
    and that is what lets its own non-security/security default split (55/70) take
    effect for a config-absent REVIEW.md, matching the JS pipeline's contract of
    only stamping keys REVIEW.md actually set.
    """
    config: dict[str, Any] = {"ignore": []}

    try:
        with open(path) as fh:
            text = fh.read()
    except FileNotFoundError:
        warn(f"REVIEW.md not found at {path!r}; using default thresholds.")
        return config
    except OSError as e:
        warn(f"Could not read REVIEW.md: {e}; using default thresholds.")
        return config

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
        warn(
            f"REVIEW.md at {path!r}: no code-gauntlet config block found; falling back to whole-file scan."
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
            item = _REVIEW_IGNORE_ITEM_RE.sub("", line).strip()
            if item:
                config["ignore"].append(_strip_matching_quotes(item))

    return config


# ---------------------------------------------------------------------------
# Filter: confidence / severity threshold (with validator contestation)
# ---------------------------------------------------------------------------


def apply_threshold_filter(findings, config):
    """
    Remove findings that fall below confidence or severity thresholds.

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

    sev_threshold_idx = SEVERITY_ORDER.index(
        config.get("severity_threshold", DEFAULT_SEVERITY_THRESHOLD)
    )

    for finding in findings:
        confidence = _as_confidence(finding.get("confidence"))
        severity = (_as_text(finding.get("severity")) or "low").lower()
        dimensions = (
            [finding.get("dimension", "").lower()] if finding.get("dimension") else []
        )

        # Determine effective confidence threshold
        is_security = "security" in dimensions
        if is_security:
            min_conf = config.get(
                "security_min_confidence", DEFAULT_SECURITY_MIN_CONFIDENCE
            )
            effective_threshold = min(
                config.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD),
                min_conf,
            )
        else:
            effective_threshold = config.get(
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


# Casefold-reachable homoglyph fold table (#242): the four codepoints that
# case-fold to a plain ASCII letter, mapped to that letter. Hand-copied from
# scripts/filter_patterns_registry.py's ASCII_CASEFOLD_REACHABLE (and pinned to
# it by tests/test_filter_findings.py) -- an NFKC/normalize pre-pass would
# diverge the twins (CPython UCD vs Node ICU ship different Unicode versions),
# so the map is hand-pinned, not derived. The injection scan folds the SCANNED
# text with this and unions the result with the raw scan (see _first_match); it
# is never used to REPLACE the scanned text, because [U+017F]/[U+0131]/[U+0130]/K are non-word under
# ASCII and folding them in place would flip the `\b`/`(?<!\w)` boundaries the
# raw text already satisfies and DESTROY detections HEAD makes.
_CASEFOLD_REACHABLE_FOLD = {0x017F: "s", 0x0131: "i", 0x0130: "i", 0x212A: "k"}


def _fold_casefold_reachable(text):
    """Fold the four casefold-reachable homoglyphs to their ASCII letters.

    `str.translate` over the fold table -- deliberately NO `re.*` call here (the
    filter-twin unicode guard pins this module's `re.*` call census; a regex in
    this helper would break `test_discovery_finds_the_known_shape`).
    """
    return text.translate(_CASEFOLD_REACHABLE_FOLD)


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
            value_folded = _fold_casefold_reachable(value)
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
        combined_folded = _fold_casefold_reachable(combined)
        title_folded = _fold_casefold_reachable(title)

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
        # hash to distinct signatures exactly as at HEAD (dedup never folded).
        # Respelling it to a folded/whitespace-normalized key is #244's change,
        # not S3's.
        sig = (title.lower().strip(), filepath, finding.get("line_start"))
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
    def _line_bucket(line):
        """Round line to nearest 10 to group nearby findings."""
        try:
            return round(int(line) / 10) * 10
        except (TypeError, ValueError):
            return 0

    location_groups = {}
    for finding in findings:
        key = (finding.get("file", ""), _line_bucket(finding.get("line_start", 0)))
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
        line = _line_bucket(finding.get("line_start", 0))
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

    def _bucket(line, proximity):
        try:
            return round(int(line) / proximity) * proximity
        except (TypeError, ValueError):
            return 0

    groups = {}
    for finding in findings:
        fpath = finding.get("file", "")
        bucket = _bucket(finding.get("line_start", 0), line_proximity)
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

    Step 2 — Dimension-based routing (BF-15a): Check the finding's dimension field
    first. Dimensions like bug/security/cross_file_impact/intent always route to main.
    Dimensions like comment_accuracy always route to suggestion. Conditional dimensions
    (test_coverage, convention, type_design) route to suggestion unless functional-
    violation keywords are present.

    Step 3 — Agent-based routing (fallback): If dimension routing returned None
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

    # Step 2 & 3: Dimension-based routing, then agent-based fallback
    main_count = 0
    suggestion_count = 0

    for finding in findings:
        agent = finding.get("agent", "").lower()
        dimensions = (
            {finding.get("dimension", "").lower()}
            if finding.get("dimension")
            else set()
        )

        # Step 2: Try dimension-based routing first (BF-15a)
        dim_route = _route_by_dimension(finding)
        if dim_route is not None:
            destination = dim_route
            if dim_route == "suggestion":
                finding["routed_by"] = "dimension"
        else:
            # Step 3: Fall back to agent-based routing
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
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
        return patterns

    # Fallback: bullet list items. The tail is `([^\n]+)$` (explicit, identical
    # in both engines) rather than `(.+)$` -- JS `.` excludes \r/U+2028/U+2029,
    # so the old JS twin silently applied ZERO exclusions on such input.
    for line in _split_review_lines(text):
        m = _REVIEW_EXCL_BULLET_RE.match(line)
        if m:
            patterns.append(m.group(1).strip())

    return patterns


def apply_exclusions(findings, exclusion_patterns):
    """
    Remove findings whose title, description, or suggestion matches an exclusion
    pattern. suggestion is included because it is rendered into posted PR/MR
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
    if not exclusion_patterns:
        return findings, []

    passed = []
    eliminated = []

    for finding in findings:
        title = _as_text(finding.get("title"))
        description = _as_text(finding.get("description"))
        raw_suggestion = finding.get("suggestion")
        suggestion = raw_suggestion if isinstance(raw_suggestion, str) else ""
        combined = f"{title}\n{description}\n{suggestion}"

        matched_pattern = None
        for pattern in exclusion_patterns:
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

    total = len(findings)

    # ------------------------------------------------------------------
    # Normalize legacy field names (BF-14)
    # ------------------------------------------------------------------
    normalize_field_names(findings)

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
    exclusion_patterns = config.get("ignore", []) + load_exclusions(args.exclusions_md)

    # ------------------------------------------------------------------
    # Pipeline: threshold -> exclusions -> injection -> disagreement -> tag
    # ------------------------------------------------------------------
    all_eliminated = []

    # Step 1: threshold filter (with contestation)
    findings, elim_threshold, contested_count = apply_threshold_filter(findings, config)
    all_eliminated.extend(elim_threshold)
    passed_threshold = len(findings)

    # Step 2: exclusion filter (before injection so explicit overrides take priority)
    findings, elim_exclusions = apply_exclusions(findings, exclusion_patterns)
    all_eliminated.extend(elim_exclusions)
    exclusions_removed = len(elim_exclusions)

    # Step 3: injection filter
    findings, elim_injection = apply_injection_filter(findings)
    all_eliminated.extend(elim_injection)
    injections_removed = len(elim_injection)
    # One `{field}s_removed` stat per _INJECTION_STRIPPED_PROSE_FIELDS entry --
    # looping the shared list (rather than one hardcoded sum() per field) means
    # adding a field to the list is the only edit a future extension needs (#213).
    prose_fields_removed = {
        f"{field}s_removed": sum(
            1 for f in findings if f.get(f"{field}_removed_by") == "injection"
        )
        for field in _INJECTION_STRIPPED_PROSE_FIELDS
    }
    suggested_fix_codes_removed = sum(
        1 for f in findings if f.get("suggested_fix_code_removed_by") == "injection"
    )

    # Step 4: disagreement detection (returns active findings, suppressed, boosted_count)
    findings, elim_suppressed, consensus_boosted = detect_disagreement(findings)
    all_eliminated.extend(elim_suppressed)

    # Step 5: tag for output routing (also applies cross-agent consolidation)
    findings, cross_agent_consolidated, tagged_main, tagged_suggestion = tag_findings(
        findings
    )

    # Count promotions (test-analyzer findings promoted to main report)
    promoted_count = sum(
        1 for f in findings if f.get("promoted_from") == "test-analyzer"
    )

    # Count dimension-routed and singleton-penalized findings (BF-15)
    dimension_routed = sum(1 for f in findings if f.get("routed_by") == "dimension")
    singleton_penalized = sum(
        1 for f in findings + all_eliminated if f.get("singleton_penalty")
    )

    # ------------------------------------------------------------------
    # Compose output
    # ------------------------------------------------------------------
    result = {
        "filtered": findings,
        "eliminated": all_eliminated,
        "stats": {
            "total": total,
            "passed_threshold": passed_threshold,
            "contested_count": contested_count,
            "exclusions_removed": exclusions_removed,
            "injections_removed": injections_removed,
            # Spliced, not hand-listed: prose_fields_removed's keys/order are exactly
            # _INJECTION_STRIPPED_PROSE_FIELDS's (dict comprehension, insertion-ordered),
            # so adding a field to that list is the only edit a future stat needs -- no
            # second key to add here.
            **prose_fields_removed,
            "suggested_fix_codes_removed": suggested_fix_codes_removed,
            "consensus_boosted": consensus_boosted,
            "singleton_penalized": singleton_penalized,
            "dimension_routed": dimension_routed,
            "cross_agent_consolidated": cross_agent_consolidated,
            "test_analyzer_promoted": promoted_count,
            "tagged_main": tagged_main,
            "tagged_suggestion": tagged_suggestion,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    summary = [
        f"Output written to {args.output}",
        f"  {len(findings)} finding(s) passed, {len(all_eliminated)} eliminated.",
    ]
    try:
        write_result(args.output, result, summary)
    except OSError as e:
        die(f"Could not write output file: {e}")


if __name__ == "__main__":
    main()
