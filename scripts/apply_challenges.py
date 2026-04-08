#!/usr/bin/env python3
"""
apply_challenges.py — Phase 7→8 bridge for deep-review.

Reads Phase 6 output (filter_findings.py) from disk, applies blind-challenge
scores to each finding, re-routes and culls according to the challenge
thresholds, re-runs cross-agent dedup, caps and ranks the final set, then
writes delivery-ready JSON to disk.

Usage:
    python3 apply_challenges.py <filtered_json> <challenges_json> \\
        [--output PATH] [--max-findings N]

Arguments:
    filtered_json    Path to Phase 6 output JSON (filter_findings.py output).
                     Accepts any of:
                       {"filtered": [...], "eliminated": [...], "stats": {...}}
                       {"findings": [...]}
                       [...]   (bare array)
    challenges_json  Path to Phase 7 challenge results JSON:
                       [{id, score, justification?}, ...]
                     or {"challenges": [...]}
    --output PATH    Write result JSON to this file.  Defaults to stdout.
    --max-findings N Cap the final filtered list at N findings (default: no cap).
                     Findings are ranked by severity → confidence → description
                     length before the cap is applied.

Challenge score thresholds
--------------------------
score < 25   non-security: remove (eliminated_by="challenge:removed")
             security (dimension="security"): downgrade severity instead of remove
score 25-49  downgrade   severity dropped one step; finding moves to suggestion;
                         eliminated_by="challenge:downgraded" only if the downgraded
                         severity now falls below the pipeline's minimum (low), in
                         which case the finding is removed.  Otherwise kept with
                         report_destination="suggestion".
score 50-74  contest     finding survives; marked challenge_contested=True;
                         surfaced findings (origin="surfaced") re-routed to
                         suggestion.
score >= 75  survive     finding kept as-is.

Surfaced findings (origin="surfaced") with score < 50 are additionally
re-routed to suggestion regardless of their existing report_destination.

Cross-agent dedup is re-run after challenge processing using the shared
group_by_proximity utility from filter_findings.py.

Output JSON:
    {
        "findings": [...],    # delivery-ready findings (canonical schema: description/line_start/line_end; orchestrator maps to body/line/end_line for post_review.py)
        "eliminated": [...],  # findings removed at any stage (Phase 6 + Phase 7)
        "stats": {
            "total_input":          N,  # findings entering this script
            "challenge_removed":    N,  # score < 25, hard removed
            "challenge_downgraded": N,  # score 25-49, severity/route downgraded
            "challenge_contested":  N,  # score 50-74, flagged but kept
            "challenge_survived":   N,  # score >= 75, fully passed
            "unchallenged":         N,  # findings with no challenge score
            "dedup_dropped":        N,  # dropped by cross-agent dedup
            "cap_dropped":          N,  # dropped by max_findings cap
            "final_count":          N   # len(findings)
        },
        "generated_at": "..."
    }

No external Python dependencies — stdlib only.
"""

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone

# Import shared dedup utility from filter_findings (stdlib only, same package)
sys.path.insert(0, os.path.dirname(__file__))
from filter_findings import dedup_cross_agent  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


# Severity ordering for downgrade step.  Lower index = higher severity.
SEVERITY_ORDER = ["critical", "high", "medium", "low"]


def _downgrade_severity(severity):
    """
    Return the next lower severity, or None if already at the lowest.

    "critical" -> "high" -> "medium" -> "low" -> None
    """
    try:
        idx = SEVERITY_ORDER.index(severity.lower())
    except (ValueError, AttributeError):
        return None
    if idx + 1 >= len(SEVERITY_ORDER):
        return None
    return SEVERITY_ORDER[idx + 1]


def _rank_key(finding):
    """
    Sort key for ranking findings before applying the max_findings cap.

    Primary:   severity (critical first, low last)
    Secondary: confidence (higher first)
    Tertiary:  risk_level when present (higher first), else description length
               (longer first — proxy for information density)
    """
    sev = finding.get("severity", "low").lower()
    try:
        sev_idx = SEVERITY_ORDER.index(sev)
    except ValueError:
        sev_idx = len(SEVERITY_ORDER)
    conf = finding.get("confidence", 0)
    risk_level = finding.get("risk_level")
    if risk_level is not None:
        try:
            tertiary = -float(risk_level)
        except (TypeError, ValueError):
            tertiary = -len(finding.get("description", ""))
    else:
        tertiary = -len(finding.get("description", ""))
    # Lower sev_idx is better; negate conf and tertiary so sorted() (ascending)
    # produces the desired order.
    return (sev_idx, -conf, tertiary)


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

def load_filtered(path):
    """
    Load Phase 6 findings from path.

    Accepts three shapes:
      1. {"filtered": [...], "eliminated": [...], "stats": {...}}  — filter_findings output
      2. {"findings": [...]}                                        — generic envelope
      3. [...]                                                      — bare array

    Returns (findings_list, all_eliminated, envelope) where:
      - findings_list   the active findings to process
      - all_eliminated  already-eliminated findings to pass through
      - envelope        the original dict (None for bare arrays)
    """
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        die(f"Filtered findings file not found: {path}")
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in filtered findings file: {e}")

    if isinstance(raw, list):
        return raw, [], None

    if isinstance(raw, dict):
        if "filtered" in raw:
            return (
                raw["filtered"],
                raw.get("eliminated", []),
                raw,
            )
        if "findings" in raw:
            return raw["findings"], [], raw
        die(
            "filtered_json must be a JSON array, or an object with a "
            "'filtered' or 'findings' key."
        )

    die("filtered_json must be a JSON array or object.")


def load_challenges(path):
    """
    Load challenge results from path.

    Accepts:
      1. [{id, score, justification?}, ...]
      2. {"challenges": [...]}

    Returns a list of challenge dicts.
    """
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        die(f"Challenges file not found: {path}")
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in challenges file: {e}")

    if isinstance(raw, list):
        return raw

    if isinstance(raw, dict):
        if "challenges" in raw:
            return raw["challenges"]
        die(
            "challenges_json must be a JSON array or an object with a "
            "'challenges' key."
        )

    die("challenges_json must be a JSON array or object.")


# ---------------------------------------------------------------------------
# Core: apply challenge thresholds
# ---------------------------------------------------------------------------

def apply_challenges(findings, challenges):
    """
    Apply blind-challenge scores to findings.

    For each challenge entry match:
      score < 25   → non-security: remove (eliminated_by="challenge:removed")
                     security (dimension="security"): downgrade severity instead
      score 25-49  → downgrade severity one step; re-route to suggestion;
                     if downgrade produces None (already at "low"), remove
                     (eliminated_by="challenge:downgraded")
      score 50-74  → contest; mark challenge_contested=True; surfaced findings
                     re-routed to suggestion
      score >= 75  → survive unchanged

    Surfaced findings (origin="surfaced") with score < 50 are additionally
    re-routed to suggestion.

    Returns:
        active          list — findings that survive challenge processing
        eliminated      list — findings removed (each has eliminated_by set)
        stats           dict — counters for each outcome
    """
    # Build id -> challenge score map (O(n) lookup)
    challenge_by_id = {}
    for entry in challenges:
        cid = entry.get("id")
        if cid is None:
            warn("[apply_challenges] Challenge entry missing 'id' — skipped.")
            continue
        score = entry.get("score")
        if score is None:
            warn(
                f"[apply_challenges] Challenge entry id={cid!r} missing "
                "'score' — skipped."
            )
            continue
        try:
            score = int(score)
        except (TypeError, ValueError):
            warn(
                f"[apply_challenges] Challenge entry id={cid!r} has "
                f"non-integer score={entry.get('score')!r} — skipped."
            )
            continue
        challenge_by_id[cid] = entry

    active = []
    eliminated = []
    stats = {
        "challenge_removed": 0,
        "challenge_downgraded": 0,
        "challenge_contested": 0,
        "challenge_survived": 0,
        "unchallenged": 0,
    }

    for finding in findings:
        fid = finding.get("id")
        entry = challenge_by_id.get(fid)

        if entry is None:
            # No challenge result — pass through unchanged
            stats["unchallenged"] += 1
            active.append(finding)
            continue

        score = int(entry.get("score", 0))
        justification = entry.get("justification")

        # Annotate challenge metadata on the finding
        finding = copy.deepcopy(finding)  # deep copy so nested structures are independent
        finding["challenge_score"] = score
        if justification:
            finding["challenge_justification"] = justification

        is_surfaced = finding.get("origin", "").lower() == "surfaced"
        is_security = finding.get("dimension", "").lower() == "security"

        if score < 25:
            if is_security:
                # Security findings with score < 25 are downgraded rather than removed
                current_sev = finding.get("severity", "low").lower()
                new_sev = _downgrade_severity(current_sev)
                finding["challenge_contested"] = False
                if new_sev is None:
                    # Already at lowest — remove
                    elim = dict(finding)
                    elim["eliminated_by"] = "challenge:removed"
                    elim["elimination_reason"] = (
                        f"challenge score {score} < 25; security finding severity already "
                        f"'{current_sev}' (lowest) — finding removed"
                    )
                    eliminated.append(elim)
                    stats["challenge_removed"] += 1
                    warn(
                        f"[challenge] Removed security finding {fid!r} "
                        f"(score={score}, severity={current_sev} already at lowest)"
                    )
                else:
                    finding["severity"] = new_sev
                    finding["severity_downgraded"] = True
                    finding["original_severity"] = current_sev
                    finding["report_destination"] = "suggestion"
                    finding["report_tag"] = "suggestion"
                    active.append(finding)
                    stats["challenge_downgraded"] += 1
                    warn(
                        f"[challenge] Downgraded security finding {fid!r} "
                        f"severity {current_sev!r} → {new_sev!r} (score={score} < 25)"
                    )
            else:
                # Hard remove for non-security findings
                elim = dict(finding)
                elim["eliminated_by"] = "challenge:removed"
                elim["elimination_reason"] = (
                    f"challenge score {score} < 25; finding does not survive blind challenge"
                )
                eliminated.append(elim)
                stats["challenge_removed"] += 1
                warn(
                    f"[challenge] Removed finding {fid!r} (score={score})"
                )

        elif score < 50:
            # Downgrade severity one step; re-route to suggestion
            current_sev = finding.get("severity", "low").lower()
            new_sev = _downgrade_severity(current_sev)
            finding["challenge_contested"] = False

            if new_sev is None:
                # Already at lowest severity — remove
                elim = dict(finding)
                elim["eliminated_by"] = "challenge:downgraded"
                elim["elimination_reason"] = (
                    f"challenge score {score} in 25-49 range; severity already "
                    f"'{current_sev}' (lowest) — finding removed"
                )
                eliminated.append(elim)
                stats["challenge_downgraded"] += 1
                warn(
                    f"[challenge] Downgrade-removed finding {fid!r} "
                    f"(score={score}, severity={current_sev})"
                )
            else:
                finding["severity"] = new_sev
                finding["severity_downgraded"] = True
                finding["original_severity"] = current_sev
                finding["report_destination"] = "suggestion"
                finding["report_tag"] = "suggestion"
                # Surfaced with score < 50 → suggestion (already done above)
                active.append(finding)
                stats["challenge_downgraded"] += 1
                warn(
                    f"[challenge] Downgraded finding {fid!r} "
                    f"severity {current_sev!r} → {new_sev!r} (score={score})"
                )

        elif score < 75:
            # Contest: keep but flag; surfaced → suggestion
            finding["challenge_contested"] = True
            if is_surfaced:
                finding["report_destination"] = "suggestion"
                finding["report_tag"] = "suggestion"
            active.append(finding)
            stats["challenge_contested"] += 1

        else:
            # Survive
            finding["challenge_contested"] = False
            active.append(finding)
            stats["challenge_survived"] += 1

    return active, eliminated, stats


# ---------------------------------------------------------------------------
# Post-challenge processing
# ---------------------------------------------------------------------------

def rank_findings(findings):
    """
    Return findings sorted by severity → confidence → description length.

    severity: critical > high > medium > low
    confidence: higher is better
    description length: longer is better (information density proxy)
    """
    return sorted(findings, key=_rank_key)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Phase 7→8 bridge for deep-review. "
            "Applies blind-challenge scores to Phase 6 findings, re-runs "
            "cross-agent dedup, caps and ranks the final set, and writes "
            "delivery-ready JSON."
        )
    )
    parser.add_argument(
        "filtered_json",
        help="Path to Phase 6 output JSON (filter_findings.py output).",
    )
    parser.add_argument(
        "challenges_json",
        help="Path to Phase 7 challenge results JSON: [{id, score, justification?}, ...].",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help="Write result JSON to this file instead of stdout.",
    )
    parser.add_argument(
        "--max-findings",
        metavar="N",
        type=int,
        default=None,
        help=(
            "Cap the final filtered list at N findings (default: no cap). "
            "Pass 0 for no limit (same as omitting). "
            "Findings are ranked by severity → confidence → description length "
            "before the cap is applied."
        ),
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Load inputs
    # ------------------------------------------------------------------
    findings, prior_eliminated, envelope = load_filtered(args.filtered_json)
    challenges = load_challenges(args.challenges_json)

    total_input = len(findings)

    # ------------------------------------------------------------------
    # Apply challenge thresholds
    # ------------------------------------------------------------------
    active, challenge_eliminated, challenge_stats = apply_challenges(findings, challenges)

    # ------------------------------------------------------------------
    # Cross-agent dedup (re-run after challenge processing)
    # ------------------------------------------------------------------
    active, dedup_dropped = dedup_cross_agent(active)
    dedup_elim = list(dedup_dropped)  # dedup already sets eliminated_by="dedup:cross-agent"

    # ------------------------------------------------------------------
    # Rank and cap
    # ------------------------------------------------------------------
    active = rank_findings(active)

    cap_dropped_elim = []
    # --max-findings 0 or negative means "no limit" (spec convention)
    if args.max_findings is not None and args.max_findings <= 0:
        args.max_findings = None
    if args.max_findings is not None and len(active) > args.max_findings:
        cap_overflow = active[args.max_findings:]
        active = active[: args.max_findings]
        for f in cap_overflow:
            elim = dict(f)
            elim["eliminated_by"] = "cap:max_findings"
            elim["elimination_reason"] = (
                f"max_findings cap of {args.max_findings} reached; "
                f"finding ranked outside top {args.max_findings}"
            )
            cap_dropped_elim.append(elim)
        warn(
            f"[cap] Dropped {len(cap_dropped_elim)} finding(s) to enforce "
            f"--max-findings={args.max_findings}"
        )

    # ------------------------------------------------------------------
    # Compose output
    # ------------------------------------------------------------------
    all_eliminated = (
        list(prior_eliminated)
        + challenge_eliminated
        + dedup_elim
        + cap_dropped_elim
    )

    stats = {
        "total_input": total_input,
        "challenge_removed": challenge_stats["challenge_removed"],
        "challenge_downgraded": challenge_stats["challenge_downgraded"],
        "challenge_contested": challenge_stats["challenge_contested"],
        "challenge_survived": challenge_stats["challenge_survived"],
        "unchallenged": challenge_stats["unchallenged"],
        "dedup_dropped": len(dedup_elim),
        "cap_dropped": len(cap_dropped_elim),
        "final_count": len(active),
    }

    result = {
        "findings": active,
        "eliminated": all_eliminated,
        "stats": stats,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    output_text = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        try:
            with open(args.output, "w") as fh:
                fh.write(output_text)
                fh.write("\n")
            print(f"Output written to {args.output}", file=sys.stderr)
            print(
                f"  {total_input} input finding(s); "
                f"{len(active)} final finding(s); "
                f"{len(all_eliminated) - len(prior_eliminated)} eliminated in Phase 7.",
                file=sys.stderr,
            )
        except OSError as e:
            die(f"Could not write output file: {e}")
    else:
        print(output_text)

    print(
        f"Done: {len(active)} finding(s) delivered, "
        f"{challenge_stats['challenge_removed']} removed, "
        f"{challenge_stats['challenge_downgraded']} downgraded, "
        f"{challenge_stats['challenge_contested']} contested.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
