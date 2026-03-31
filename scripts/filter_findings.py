#!/usr/bin/env python3
"""
filter_findings.py — Deterministic Phase 6 filtering for deep-review.

Usage:
    python3 filter_findings.py <findings_json> [--review-md path] [--exclusions-md path]

Arguments:
    findings_json     Path to verified findings JSON (from verify_findings.py or Phase 5 output).
    --review-md       Path to REVIEW.md for custom thresholds and ignore patterns.
                      When omitted, built-in defaults are used.
    --exclusions-md   Path to false-positive-exclusions.md.
                      When omitted, the bundled exclusions list is used.

Input JSON schema:
    A JSON object or array of verified findings. When an object is given, the
    "findings" key is read. Each finding must have at minimum:
        {
            "id":         "unique string",
            "file":       "src/foo.py",
            "line":       42,
            "severity":   "critical|high|medium|low",
            "confidence": 85,          # 0-100 integer
            "title":      "...",
            "body":       "...",
            "blame_tag":  "new|surfaced|pre-existing",   # optional
            "dimensions": ["security", "logic"]           # optional
        }

Output JSON schema:
    {
        "filtered": [...],    # findings that passed all filters, tagged for output
        "eliminated": [...],  # findings removed by any filter, with "eliminated_by" field
        "stats": {
            "total":               N,   # total input findings
            "passed_threshold":    N,   # passed confidence + severity threshold
            "injections_removed":  N,   # removed by injection filter
            "consensus_boosted":   N,   # confidence boosted due to multi-agent consensus
            "tagged_main":         N,   # tagged for main report
            "tagged_suggestion":   N    # tagged as improvement suggestions
        }
    }

REVIEW.md parsing:
    Looks for a fenced code block or YAML-style section containing:
        confidence_threshold: 80
        severity_threshold: medium
        ignore:
          - pattern to ignore

No external Python dependencies -- stdlib only.
"""

import argparse
import json
import re
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
# REVIEW.md parser
# ---------------------------------------------------------------------------

# Severity ordering for threshold comparisons (lower index = higher severity)
SEVERITY_ORDER = ["critical", "high", "medium", "low"]

# Default thresholds used when REVIEW.md is absent or does not specify them
DEFAULT_CONFIDENCE_THRESHOLD = 80
DEFAULT_SECURITY_MIN_CONFIDENCE = 70
DEFAULT_SEVERITY_THRESHOLD = "low"  # pass all severities by default


def parse_review_md(path):
    """
    Extract confidence_threshold, severity_threshold, and ignore patterns from REVIEW.md.

    Returns a dict with keys:
        confidence_threshold    int   (default: DEFAULT_CONFIDENCE_THRESHOLD)
        security_min_confidence int   (default: DEFAULT_SECURITY_MIN_CONFIDENCE)
        severity_threshold      str   (default: DEFAULT_SEVERITY_THRESHOLD)
        ignore                  list  (default: [])
    """
    config = {
        "confidence_threshold": DEFAULT_CONFIDENCE_THRESHOLD,
        "security_min_confidence": DEFAULT_SECURITY_MIN_CONFIDENCE,
        "severity_threshold": DEFAULT_SEVERITY_THRESHOLD,
        "ignore": [],
    }

    try:
        with open(path) as fh:
            text = fh.read()
    except FileNotFoundError:
        warn(f"REVIEW.md not found at {path!r}; using default thresholds.")
        return config
    except OSError as e:
        warn(f"Could not read REVIEW.md: {e}; using default thresholds.")
        return config

    # Match a YAML-style deep-review config block.
    # Accepts:
    #   ```yaml\n# deep-review\n...\n```
    #   <!-- deep-review-config\n...\n-->
    #   ## deep-review config\nkey: value (until blank line or next heading)
    block_patterns = [
        # Fenced code block (yaml or plain)
        r"```(?:yaml|)[\s]*#?\s*deep-review(?:[^\n]*)?\n(.*?)```",
        # HTML comment block
        r"<!--\s*deep-review-config\s*\n(.*?)-->",
    ]

    block_text = ""
    for pattern in block_patterns:
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if m:
            block_text = m.group(1)
            break

    # Also scan the whole file for bare key: value lines if no block found
    if not block_text:
        block_text = text

    # confidence_threshold
    m = re.search(r"confidence_threshold\s*[:=]\s*(\d+)", block_text)
    if m:
        config["confidence_threshold"] = int(m.group(1))

    # security_min_confidence
    m = re.search(r"security_min_confidence\s*[:=]\s*(\d+)", block_text)
    if m:
        config["security_min_confidence"] = int(m.group(1))

    # severity_threshold
    m = re.search(
        r"severity_threshold\s*[:=]\s*(critical|high|medium|low)", block_text, re.IGNORECASE
    )
    if m:
        config["severity_threshold"] = m.group(1).lower()

    # ignore list -- lines after "ignore:" that start with "  -" or "- "
    ignore_section = re.search(r"ignore\s*:\s*\n((?:[ \t]*-[^\n]*\n?)+)", block_text)
    if ignore_section:
        for line in ignore_section.group(1).splitlines():
            item = re.sub(r"^\s*-\s*", "", line).strip()
            if item:
                config["ignore"].append(item)

    return config


# ---------------------------------------------------------------------------
# Filter: confidence / severity threshold
# ---------------------------------------------------------------------------

def apply_threshold_filter(findings, config):
    """
    Remove findings that fall below confidence or severity thresholds.

    A finding passes if:
      - confidence >= config["confidence_threshold"]
        (security dimensions use config["security_min_confidence"] as minimum)
      - severity is at or above config["severity_threshold"] in SEVERITY_ORDER

    Returns (passed, eliminated) lists. Each eliminated finding gains an
    "eliminated_by" field set to "threshold".
    """
    passed = []
    eliminated = []

    sev_threshold_idx = SEVERITY_ORDER.index(
        config.get("severity_threshold", DEFAULT_SEVERITY_THRESHOLD)
    )

    for finding in findings:
        confidence = finding.get("confidence", 0)
        severity = finding.get("severity", "low").lower()
        dimensions = [d.lower() for d in finding.get("dimensions", [])]

        # Determine effective confidence threshold
        is_security = "security" in dimensions
        if is_security:
            min_conf = config.get("security_min_confidence", DEFAULT_SECURITY_MIN_CONFIDENCE)
            effective_threshold = min(
                config.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD), min_conf
            )
        else:
            effective_threshold = config.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD)

        # Check confidence
        if confidence < effective_threshold:
            elim = dict(finding)
            elim["eliminated_by"] = "threshold"
            elim["elimination_reason"] = (
                f"confidence {confidence} < threshold {effective_threshold}"
            )
            eliminated.append(elim)
            continue

        # Check severity
        if severity not in SEVERITY_ORDER:
            warn(f"Unknown severity {severity!r} on finding {finding.get('id', '?')}; treating as low.")
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

    return passed, eliminated


# ---------------------------------------------------------------------------
# Filter: injection artifact detection
# ---------------------------------------------------------------------------

# Patterns that suggest a finding was injected by a prompt artifact or
# hallucinated without grounding in actual code.
_INJECTION_TITLE_PATTERNS = [
    r"\bTODO\b",
    r"\bFIXME\b",
    r"\bPlaceholder\b",
    r"\bExample finding\b",
    r"\bSample finding\b",
    r"\btest finding\b",
    r"\bdemo finding\b",
]

_INJECTION_BODY_PATTERNS = [
    r"<finding>",
    r"<example>",
    r"\[\s*INSERT\s*\]",
    r"lorem ipsum",
]


def apply_injection_filter(findings):
    """
    Remove findings that appear to be prompt-injection artifacts or hallucinations.

    Detection heuristics:
      - Title matches known placeholder patterns (TODO, FIXME, etc.)
      - Body contains XML-like injection markers
      - File path is empty, non-existent placeholder, or contains template markers
      - Both title and body are identical (copy-paste artifact)

    Returns (passed, eliminated) lists. Each eliminated finding gains an
    "eliminated_by" field set to "injection".
    """
    passed = []
    eliminated = []
    seen_signatures = {}

    for finding in findings:
        title = finding.get("title", "")
        body = finding.get("body", "")
        filepath = finding.get("file", "")

        reasons = []

        # Check title patterns
        for pattern in _INJECTION_TITLE_PATTERNS:
            if re.search(pattern, title, re.IGNORECASE):
                reasons.append(f"title matches injection pattern: {pattern!r}")
                break

        # Check body patterns
        for pattern in _INJECTION_BODY_PATTERNS:
            if re.search(pattern, body, re.IGNORECASE):
                reasons.append(f"body matches injection pattern: {pattern!r}")
                break

        # Check for empty or template file paths
        if not filepath or re.search(r"<.*?>|\{.*?\}", filepath):
            reasons.append(f"file path is empty or contains template markers: {filepath!r}")

        # Check for duplicate signature (title+file+line)
        sig = (title.lower().strip(), filepath, finding.get("line"))
        if sig in seen_signatures:
            reasons.append(f"duplicate of finding {seen_signatures[sig]!r}")
        else:
            seen_signatures[sig] = finding.get("id", title)

        if reasons:
            elim = dict(finding)
            elim["eliminated_by"] = "injection"
            elim["elimination_reason"] = "; ".join(reasons)
            eliminated.append(elim)
        else:
            passed.append(finding)

    return passed, eliminated


# ---------------------------------------------------------------------------
# Disagreement detection
# ---------------------------------------------------------------------------

def detect_disagreement(findings):
    """
    Detect consensus, singleton, and contradiction patterns across findings.

    A "consensus" finding is one where multiple review agents reported the same
    issue (same file + approximate line range + similar title). Consensus findings
    receive a confidence boost.

    A "contradiction" is where one finding asserts an issue that another finding
    explicitly says is safe (e.g., one flags auth bypass, another confirms auth is correct).
    Contradictions are flagged for human review.

    Returns the same findings list with added fields on each finding:
        consensus_count   int    number of agents that reported this finding (1 = singleton)
        consensus_boost   int    confidence added due to consensus (0 if singleton)
        contradiction     bool   True if a contradictory finding exists

    Also returns a boosted_count int for stats reporting.
    """
    # Group by (file, approximate line, normalized title prefix)
    groups = {}
    for finding in findings:
        file_ = finding.get("file", "")
        line = finding.get("line", 0)
        # Round line to nearest 5 to group nearby findings
        line_bucket = round(line / 5) * 5 if line else 0
        title_key = re.sub(r"\s+", " ", finding.get("title", "")).lower()[:40]
        group_key = (file_, line_bucket, title_key)

        if group_key not in groups:
            groups[group_key] = []
        groups[group_key].append(finding)

    # Apply consensus metadata
    boosted_count = 0
    for group_key, group in groups.items():
        count = len(group)
        boost = 0
        if count > 1:
            # Each additional confirming agent adds up to 5 points, capped at 15
            boost = min((count - 1) * 5, 15)
            boosted_count += count

        for finding in group:
            finding["consensus_count"] = count
            finding["consensus_boost"] = boost
            if boost:
                original_conf = finding.get("confidence", 0)
                finding["confidence"] = min(original_conf + boost, 100)

    # Simple contradiction detection: findings that share a file+line but have
    # directly opposing severity signals (one critical, one low on same location)
    line_groups = {}
    for finding in findings:
        key = (finding.get("file", ""), finding.get("line", 0))
        line_groups.setdefault(key, []).append(finding)

    for key, group in line_groups.items():
        if len(group) > 1:
            severities = {f.get("severity", "low").lower() for f in group}
            has_contradiction = "critical" in severities and "low" in severities
            for finding in group:
                finding["contradiction"] = has_contradiction
        else:
            group[0].setdefault("contradiction", False)

    # Ensure all findings have defaults
    for finding in findings:
        finding.setdefault("consensus_count", 1)
        finding.setdefault("consensus_boost", 0)
        finding.setdefault("contradiction", False)

    return findings, boosted_count


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------

# Dimensions that route to the main code-correctness report
MAIN_REPORT_DIMENSIONS = {"security", "logic", "null-handling", "data-integrity", "error-handling"}

# Dimensions that route to improvement suggestions
SUGGESTION_DIMENSIONS = {"test-coverage", "code-quality", "conventions", "performance"}


def tag_findings(findings):
    """
    Tag each finding as "main" (main report) or "suggestion" (improvement suggestions).

    Tagging rules (in priority order):
      1. severity=critical or high -> "main" regardless of dimension
      2. dimension in MAIN_REPORT_DIMENSIONS -> "main"
      3. dimension in SUGGESTION_DIMENSIONS -> "suggestion"
      4. Default: "main"

    Adds a "report_tag" field to each finding. Returns (tagged, main_count, suggestion_count).
    """
    main_count = 0
    suggestion_count = 0

    for finding in findings:
        severity = finding.get("severity", "low").lower()
        dimensions = {d.lower() for d in finding.get("dimensions", [])}

        if severity in ("critical", "high"):
            tag = "main"
        elif dimensions & MAIN_REPORT_DIMENSIONS:
            tag = "main"
        elif dimensions & SUGGESTION_DIMENSIONS:
            tag = "suggestion"
        else:
            tag = "main"

        finding["report_tag"] = tag
        if tag == "main":
            main_count += 1
        else:
            suggestion_count += 1

    return findings, main_count, suggestion_count


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

    # Extract from fenced code blocks first
    block_match = re.search(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
    if block_match:
        for line in block_match.group(1).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
        return patterns

    # Fallback: bullet list items
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]\s+(.+)$", line)
        if m:
            patterns.append(m.group(1).strip())

    return patterns


def apply_exclusions(findings, exclusion_patterns):
    """
    Remove findings whose title or body matches an exclusion pattern.

    Returns (passed, eliminated) lists. Each eliminated finding gains
    "eliminated_by" = "exclusion".
    """
    if not exclusion_patterns:
        return findings, []

    passed = []
    eliminated = []

    for finding in findings:
        title = finding.get("title", "")
        body = finding.get("body", "")
        combined = f"{title}\n{body}"

        matched_pattern = None
        for pattern in exclusion_patterns:
            if re.search(re.escape(pattern), combined, re.IGNORECASE):
                matched_pattern = pattern
                break

        if matched_pattern:
            elim = dict(finding)
            elim["eliminated_by"] = "exclusion"
            elim["elimination_reason"] = f"matched exclusion pattern: {matched_pattern!r}"
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
            "Deterministic Phase 6 filter for deep-review findings. "
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
    # Parse REVIEW.md config
    # ------------------------------------------------------------------
    if args.review_md:
        config = parse_review_md(args.review_md)
    else:
        config = {
            "confidence_threshold": DEFAULT_CONFIDENCE_THRESHOLD,
            "security_min_confidence": DEFAULT_SECURITY_MIN_CONFIDENCE,
            "severity_threshold": DEFAULT_SEVERITY_THRESHOLD,
            "ignore": [],
        }

    # ------------------------------------------------------------------
    # Load exclusions
    # ------------------------------------------------------------------
    exclusion_patterns = load_exclusions(args.exclusions_md)

    # ------------------------------------------------------------------
    # Pipeline: threshold -> exclusions -> injection -> disagreement -> tag
    # ------------------------------------------------------------------
    all_eliminated = []

    # Step 1: threshold filter
    findings, elim_threshold = apply_threshold_filter(findings, config)
    all_eliminated.extend(elim_threshold)
    passed_threshold = len(findings)

    # Step 2: exclusion filter (before injection so explicit overrides take priority)
    findings, elim_exclusions = apply_exclusions(findings, exclusion_patterns)
    all_eliminated.extend(elim_exclusions)

    # Step 3: injection filter
    findings, elim_injection = apply_injection_filter(findings)
    all_eliminated.extend(elim_injection)
    injections_removed = len(elim_injection)

    # Step 4: disagreement detection (mutates findings in-place, adds metadata)
    findings, consensus_boosted = detect_disagreement(findings)

    # Step 5: tag for output routing
    findings, tagged_main, tagged_suggestion = tag_findings(findings)

    # ------------------------------------------------------------------
    # Compose output
    # ------------------------------------------------------------------
    result = {
        "filtered": findings,
        "eliminated": all_eliminated,
        "stats": {
            "total": total,
            "passed_threshold": passed_threshold,
            "injections_removed": injections_removed,
            "consensus_boosted": consensus_boosted,
            "tagged_main": tagged_main,
            "tagged_suggestion": tagged_suggestion,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    output_text = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        try:
            with open(args.output, "w") as fh:
                fh.write(output_text)
                fh.write("\n")
            print(f"Output written to {args.output}")
            print(
                f"  {len(findings)} finding(s) passed, "
                f"{len(all_eliminated)} eliminated."
            )
        except OSError as e:
            die(f"Could not write output file: {e}")
    else:
        print(output_text)


if __name__ == "__main__":
    main()
