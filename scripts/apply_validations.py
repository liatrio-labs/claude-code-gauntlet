#!/usr/bin/env python3
"""
apply_validations.py — Phase 5→6 bridge for code-gauntlet.

Reads Phase 4 output (full findings with descriptions intact) from disk,
applies validator confidence adjustments from a [{id, confidence}] JSON array,
and writes the updated findings back to disk.  Descriptions never leave disk —
this eliminates the orchestrator description-compression that triggered the
injection filter in the sentry benchmark.

Usage:
    python3 apply_validations.py <findings_json> <validations_json> [--output PATH]

Arguments:
    findings_json      Path to Phase 4 output JSON (object with "verified" or
                       "findings" key, or a bare array of finding objects).
    validations_json   Path to validator output JSON: [{id, confidence, ...}, ...]
                       or an object with a "validations" key.
    --output PATH      Write result to this file.  Defaults to stdout.

Input — findings_json:
    Accepts any of the three common shapes produced by verify_findings.py:
        {"verified": [...], "eliminated": [...], "batches": [...], "stats": {...}}
        {"findings": [...]}
        [...]   (bare array)

    Only the "verified" / "findings" / bare-array entries are updated.
    Any additional top-level keys (eliminated, batches, stats) are preserved
    as-is in the output so downstream scripts can still reference them.

Input — validations_json:
    [{
        "id":         "bug-1",        # required
        "confidence": 72,             # required
        "justification": "..."        # optional — preserved on the finding
    }, ...]
    or:
    {"validations": [...]}

Output JSON:
    {
        "findings":       [...],     # updated findings (all, including those
                                     # without a matching validation)
        "stats": {
            "total":             N,  # total input findings
            "adjusted":          N,  # findings whose confidence was updated
            "unmatched":         N,  # validations with no matching finding id
            "pass_through":      N   # findings without a matching validation
        },
        "generated_at": "..."
    }

    Each adjusted finding gains:
        "original_confidence":  <pre-validation score>   (always set, even if
                                                           new == old)
        "validator_confidence": <score from validations_json>
        "validation_justification": <justification string, if present>

No external Python dependencies — stdlib only.
"""

import argparse
import json
import sys
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

def load_findings(path):
    """
    Load findings from path.  Accepts three input shapes:
      1. {"verified": [...], ...}   — verify_findings.py output
      2. {"findings": [...], ...}   — generic findings envelope
      3. [...]                      — bare array

    Returns (findings_list, envelope) where envelope is the original dict
    (or None when input was a bare array).  The envelope is preserved so
    callers can round-trip extra keys (eliminated, batches, stats).
    """
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        die(f"Findings file not found: {path}")
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in findings file: {e}")

    if isinstance(raw, list):
        return raw, None

    if isinstance(raw, dict):
        if "verified" in raw:
            return raw["verified"], raw
        if "findings" in raw:
            return raw["findings"], raw
        die(
            "findings_json must be a JSON array, or an object with a "
            "'verified' or 'findings' key."
        )

    die("findings_json must be a JSON array or object.")


def load_validations(path):
    """
    Load validations from path.  Accepts:
      1. [{"id": "...", "confidence": N, ...}, ...]
      2. {"validations": [...]}

    Returns a list of validation dicts.
    """
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        die(f"Validations file not found: {path}")
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in validations file: {e}")

    if isinstance(raw, list):
        return raw

    if isinstance(raw, dict):
        if "validations" in raw:
            return raw["validations"]
        die(
            "validations_json must be a JSON array or an object with a "
            "'validations' key."
        )

    die("validations_json must be a JSON array or object.")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def apply_validations(findings, validations):
    """
    Merge validator confidence adjustments into a list of findings in-place.

    For each validation entry:
    - Find the matching finding by id.
    - Save original_confidence (always set, even when unchanged).
    - Update confidence to the validator's score.
    - Copy justification if present.

    Findings without a matching validation pass through unchanged (but do NOT
    get original_confidence set — callers can detect un-validated findings by
    the absence of that field).

    Returns:
        adjusted_count   int  — number of findings whose confidence was updated
        unmatched_ids    list — validation ids that had no corresponding finding
    """
    # Build id -> finding index for O(n) lookup
    finding_by_id = {}
    for finding in findings:
        fid = finding.get("id")
        if fid is not None:
            finding_by_id[fid] = finding

    adjusted_count = 0
    unmatched_ids = []

    for validation in validations:
        vid = validation.get("id")
        if vid is None:
            warn("[apply_validations] Validation entry missing 'id' — skipped.")
            continue

        new_conf = validation.get("confidence")
        if new_conf is None:
            warn(
                f"[apply_validations] Validation for id={vid!r} missing "
                "'confidence' — skipped."
            )
            continue

        # Clamp to [0, 100]
        try:
            new_conf = max(0, min(100, int(new_conf)))
        except (TypeError, ValueError):
            warn(
                f"[apply_validations] Validation for id={vid!r} has "
                f"non-integer confidence={validation.get('confidence')!r} — skipped."
            )
            continue

        finding = finding_by_id.get(vid)
        if finding is None:
            unmatched_ids.append(vid)
            warn(
                f"[apply_validations] No finding found for validation id={vid!r}."
            )
            continue

        # Save original_confidence before updating (only on first validation)
        if "original_confidence" not in finding:
            finding["original_confidence"] = finding.get("confidence", 0)
        finding["validator_confidence"] = new_conf
        finding["confidence"] = new_conf

        justification = validation.get("justification")
        if justification:
            finding["validation_justification"] = justification

        adjusted_count += 1

    return adjusted_count, unmatched_ids


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Phase 5→6 bridge for code-gauntlet. "
            "Applies validator confidence adjustments to Phase 4 findings on disk "
            "without passing descriptions through the orchestrator."
        )
    )
    parser.add_argument(
        "findings_json",
        help=(
            "Path to Phase 4 output JSON (verify_findings.py output or findings array)."
        ),
    )
    parser.add_argument(
        "validations_json",
        help=(
            "Path to validator output JSON: [{id, confidence, justification?}, ...]."
        ),
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help="Write result JSON to this file instead of stdout.",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Load inputs
    # ------------------------------------------------------------------
    findings, envelope = load_findings(args.findings_json)
    validations = load_validations(args.validations_json)

    total = len(findings)

    # ------------------------------------------------------------------
    # Apply adjustments
    # ------------------------------------------------------------------
    adjusted_count, unmatched_ids = apply_validations(findings, validations)

    pass_through = total - adjusted_count

    if unmatched_ids:
        warn(
            f"[apply_validations] {len(unmatched_ids)} validation(s) had no "
            f"matching finding: {unmatched_ids}"
        )

    # ------------------------------------------------------------------
    # Compose output
    # ------------------------------------------------------------------
    result = {
        "findings": findings,
        "stats": {
            "total": total,
            "adjusted": adjusted_count,
            "unmatched": len(unmatched_ids),
            "pass_through": pass_through,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Preserve extra keys from the input envelope (eliminated, batches, etc.)
    if envelope is not None:
        for key in ("eliminated", "batches"):
            if key in envelope:
                result[key] = envelope[key]

    output_text = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        try:
            with open(args.output, "w") as fh:
                fh.write(output_text)
                fh.write("\n")
            print(f"Output written to {args.output}", file=sys.stderr)
            print(
                f"  {total} finding(s) total, "
                f"{adjusted_count} adjusted, "
                f"{pass_through} passed through unchanged.",
                file=sys.stderr,
            )
        except OSError as e:
            die(f"Could not write output file: {e}")
    else:
        print(output_text)

    print(
        f"Done: {adjusted_count}/{total} finding(s) adjusted, "
        f"{len(unmatched_ids)} unmatched validation(s).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
