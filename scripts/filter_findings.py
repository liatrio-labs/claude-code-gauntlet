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
            "injections_removed":      N,   # removed by injection filter
            "consensus_boosted":       N,   # confidence boosted due to multi-agent consensus
            "singleton_penalized":     N,   # singleton findings penalized -15 confidence (non-core dims)
            "dimension_routed":        N,   # findings routed to suggestion by dimension (BF-15a)
            "cross_agent_deduped":      N,   # cross-agent duplicates dropped (winner by priority)
            "test_analyzer_deduped":   N,   # backward-compatible alias for cross_agent_deduped
            "test_analyzer_promoted":  N,   # test-analyzer findings promoted to main report
            "tagged_main":             N,   # tagged for main report
            "tagged_suggestion":       N    # tagged as improvement suggestions
        }
    }

Each filtered finding includes:
    "report_destination":  "main" | "suggestion"  # routing destination for Phase 8
    "report_tag":          "main" | "suggestion"  # backward-compatible alias for report_destination

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

# Default thresholds used when REVIEW.md is absent or does not specify them
DEFAULT_CONFIDENCE_THRESHOLD = 70
DEFAULT_SECURITY_MIN_CONFIDENCE = 70
DEFAULT_SEVERITY_THRESHOLD = "low"  # pass all severities by default

# Contestation: if the validator dropped confidence by more than this amount,
# the finding is marked as contested and bypasses the threshold check.
_CONTESTATION_DROP_THRESHOLD = 25


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
        warn(f"REVIEW.md at {path!r}: no deep-review config block found; falling back to whole-file scan.")
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
# Filter: confidence / severity threshold (with validator contestation)
# ---------------------------------------------------------------------------

def apply_threshold_filter(findings, config):
    """
    Remove findings that fall below confidence or severity thresholds.

    A finding passes if:
      - confidence >= config["confidence_threshold"]
        (security dimensions use config["security_min_confidence"] as minimum)
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
        confidence = finding.get("confidence", 0)
        severity = finding.get("severity", "low").lower()
        dimensions = [finding.get("dimension", "").lower()] if finding.get("dimension") else []

        # Determine effective confidence threshold
        is_security = "security" in dimensions
        if is_security:
            min_conf = config.get("security_min_confidence", DEFAULT_SECURITY_MIN_CONFIDENCE)
            effective_threshold = min(
                config.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD), min_conf
            )
        else:
            effective_threshold = config.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD)

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

    return passed, eliminated, contested_count


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

# Shell command patterns — presence in a finding description/title indicates the agent
# was manipulated by adversarial content embedded in the code under review.
# These match the patterns documented in false-positive-exclusions.md
_INJECTION_SHELL_PATTERNS = [
    r"\brm\s+-[rf]",
    r"\bcurl\s+https?://",
    r"\bwget\s+https?://",
    r"\bgit\s+push\b",
    r"\bgh\s+api\b",
]

# URL patterns — findings should reference code locations, not external URLs to visit/fetch
_INJECTION_URL_PATTERNS = [
    r"https?://[^\s)>\"']{20,}",
    r"\bvisit\s+https?://",
    r"\bdownload from\s+https?://",
    r"\bnavigate to\b",
]

# Encoded payload patterns — base64 or hex blobs in findings are injection artifacts
_INJECTION_ENCODED_PATTERNS = [
    r"\b[A-Za-z0-9+/]{40,}={0,2}\b",
    r"(?<!\w)(?:0x)?[0-9a-fA-F]{32,}(?!\w)",
]

# Bypass / auto-approve instruction patterns
_INJECTION_BYPASS_PATTERNS = [
    r"\bskip\s+review\b",
    r"\bauto[-\s]?approve\b",
    r"\bbypass\s+(?:security\s+)?controls?\b",
    r"\bbypass\s+(?:the\s+)?(?:auth|authentication|authorization)\b",
    r"\bdisable\s+(?:auth|authentication|authorization)\b",
    r"\bmark\s+(?:this\s+)?(?:finding\s+)?as\s+safe\b",
    r"\bapprove\s+(?:this|the)\s+(?:PR|pull\s+request|change)\b",
]

# Instructional tone patterns — analytical findings do not issue commands to the user
_INJECTION_INSTRUCTIONAL_PATTERNS = [
    r"\byou\s+should\s+run\b",
    r"\bexecute\s+the\s+following\b",
    r"\brun\s+this\s+command\b",
    r"\bplease\s+run\b",
    r"\bpaste\s+(?:this|the\s+following)\s+into\s+(?:your\s+)?terminal",
    r"\bcopy\s+and\s+paste\s+the\s+following\b",
]

# Patterns that recommend introducing vulnerabilities or disabling security features
_INJECTION_VULN_INTRO_PATTERNS = [
    r"\badd\s+eval\s*\(",
    r"\buse\s+eval\s*\(",
    r"\bdisable\s+(?:CORS|CSP|content[-\s]security[-\s]policy)\b",
    r"\bdisable\s+(?:CSRF|csrf)\s+(?:protection|check|token)\b",
    r"\ballow\s+all\s+origins\b",
    r"\bset\s+secure\s+to\s+false\b",
    r"\bdisable\s+(?:TLS|SSL|HTTPS)\s+(?:verification|validation)\b",
    r"\bskip\s+(?:certificate|cert)\s+(?:verification|validation)\b",
    r"\bdisable\s+security\s+(?:check|feature|control)\b",
]

# Minimum word count for a valid finding description; fewer words + high confidence = suspicious
_MIN_BODY_WORDS = 10
_HIGH_CONFIDENCE_THRESHOLD = 85


def _count_words(text):
    """Return the number of words in text (whitespace-split)."""
    return len(text.split()) if text.strip() else 0


def apply_injection_filter(findings):
    """
    Remove findings that appear to be prompt-injection artifacts or hallucinations.

    Detection heuristics (from false-positive-exclusions.md § Prompt Injection Artifacts):
      1. Body or title contains shell commands (rm -rf, curl, wget, git push, gh api)
      2. Body contains URLs to visit or encoded payloads (base64, hex)
      3. Body instructs the user to bypass controls, skip review, or auto-approve
      4. Body has fewer than 10 words AND confidence is high (>= 85) — suspiciously terse
      5. Tone shifts from analytical to instructional ("you should run this command")
      6. Recommends adding code that introduces a vulnerability or disables security features
      7. Title matches known placeholder patterns (TODO, FIXME, etc.)
      8. Body contains XML-like injection markers
      9. File path is empty or contains template markers
      10. Duplicate signature (title+file+line)

    Eliminated findings are logged via stderr for the methodology section.

    Returns (passed, eliminated) lists. Each eliminated finding gains an
    "eliminated_by" field set to "injection".
    """
    passed = []
    eliminated = []
    seen_signatures = {}

    # Compile pattern lists once
    shell_re = [re.compile(p, re.IGNORECASE) for p in _INJECTION_SHELL_PATTERNS]
    url_re = [re.compile(p, re.IGNORECASE) for p in _INJECTION_URL_PATTERNS]
    encoded_re = [re.compile(p, re.IGNORECASE) for p in _INJECTION_ENCODED_PATTERNS]
    bypass_re = [re.compile(p, re.IGNORECASE) for p in _INJECTION_BYPASS_PATTERNS]
    instruct_re = [re.compile(p, re.IGNORECASE) for p in _INJECTION_INSTRUCTIONAL_PATTERNS]
    vuln_re = [re.compile(p, re.IGNORECASE) for p in _INJECTION_VULN_INTRO_PATTERNS]
    title_re = [re.compile(p, re.IGNORECASE) for p in _INJECTION_TITLE_PATTERNS]
    body_marker_re = [re.compile(p, re.IGNORECASE) for p in _INJECTION_BODY_PATTERNS]

    def _first_match(patterns, text):
        """Return the pattern of the first regex that matches text, or None."""
        for rx in patterns:
            if rx.search(text):
                return rx.pattern
        return None

    for finding in findings:
        title = finding.get("title", "")
        description = finding.get("description", "")
        filepath = finding.get("file", "")
        confidence = finding.get("confidence", 0)
        combined = f"{title}\n{description}"

        reasons = []

        # 1. Shell commands anywhere in the combined text
        m = _first_match(shell_re, combined)
        if m:
            reasons.append(f"contains shell command pattern: {m!r}")

        # 2a. URLs to visit in description
        m = _first_match(url_re, description)
        if m:
            reasons.append(f"description contains visit-URL pattern: {m!r}")

        # 2b. Encoded payloads in description
        m = _first_match(encoded_re, description)
        if m:
            reasons.append(f"description contains encoded payload pattern: {m!r}")

        # 3. Bypass / auto-approve instructions in description
        m = _first_match(bypass_re, description)
        if m:
            reasons.append(f"description contains bypass/auto-approve instruction: {m!r}")

        # 4. Short description with high confidence (suspiciously terse)
        description_word_count = _count_words(description)
        if description_word_count < _MIN_BODY_WORDS and confidence >= _HIGH_CONFIDENCE_THRESHOLD:
            reasons.append(
                f"suspiciously short description ({description_word_count} words) with high confidence ({confidence})"
            )

        # 5. Instructional tone in description
        m = _first_match(instruct_re, description)
        if m:
            reasons.append(f"description uses instructional tone: {m!r}")

        # 6. Recommends introducing vulnerability or disabling security features
        m = _first_match(vuln_re, description)
        if m:
            reasons.append(f"description recommends introducing vulnerability: {m!r}")

        # 7. Title matches placeholder patterns
        m = _first_match(title_re, title)
        if m:
            reasons.append(f"title matches placeholder pattern: {m!r}")

        # 8. Description contains XML-like injection markers
        m = _first_match(body_marker_re, description)
        if m:
            reasons.append(f"description matches injection marker: {m!r}")

        # 9. Empty or template file path
        if not filepath or re.search(r"<.*?>|\{.*?\}", filepath):
            reasons.append(f"file path is empty or contains template markers: {filepath!r}")

        # 10. Duplicate signature (title+file+line_start)
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
            passed.append(finding)

    return passed, eliminated


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
      Multiple agents flag the same file + overlapping line range + related concern.
      Boost confidence +10 (capped at 100). Annotate with corroborated_by list.

    Singleton:
      One agent only — pass through unchanged.

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

    for key, group in location_groups.items():
        if len(group) < 2:
            continue

        agent_map = {}
        for f in group:
            agent = f.get("agent", "").lower()
            agent_map.setdefault(agent, []).append(f)

        # Suppression rule 1: bug-detector + conventions-and-intent -> intentional
        if _AGENT_BUG_DETECTOR in agent_map and _AGENT_CONVENTIONS in agent_map:
            for conv_finding in agent_map[_AGENT_CONVENTIONS]:
                conv_text = (conv_finding.get("description", "") + " " + conv_finding.get("title", "")).lower()
                if re.search(r"\bintentional\b|\bby\s+design\b|\bexpected\s+behavior\b|\bdeliberate\b", conv_text):
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
                conv_text = (conv_finding.get("description", "") + " " + conv_finding.get("title", "")).lower()
                if re.search(r"\bgenerated\b|\bscaffolding\b|\bauto[-\s]?generated\b|\bboilerplate\b", conv_text):
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
    # Phase 3: Consensus grouping (file + line_bucket)
    # -----------------------------------------------------------------------
    consensus_groups = {}
    for finding in active:
        file_ = finding.get("file", "")
        line = _line_bucket(finding.get("line_start", 0))
        group_key = (file_, line)
        consensus_groups.setdefault(group_key, []).append(finding)

    boosted_count = 0
    for group_key, group in consensus_groups.items():
        count = len(group)
        agents_in_group = [f.get("agent", "") for f in group if f.get("agent")]

        if count > 1:
            # Consensus: multiple agents flagged the same location + concern
            boosted_count += count
            for finding in group:
                other_agents = [a for a in agents_in_group if a != finding.get("agent", "")]
                finding["consensus_count"] = count
                finding["consensus_boost"] = _CONSENSUS_BOOST
                finding["corroborated_by"] = other_agents
                original_conf = finding.get("confidence", 0)
                finding["confidence"] = min(original_conf + _CONSENSUS_BOOST, 100)
        else:
            # Singleton — apply confidence penalty for non-core dimensions (BF-15b)
            finding = group[0]
            finding["consensus_count"] = 1
            finding["consensus_boost"] = 0
            finding.setdefault("corroborated_by", [])

            dimension = finding.get("dimension", "").lower()
            if dimension and dimension not in _CORE_DIMENSIONS:
                original_conf = finding.get("confidence", 0)
                finding["confidence"] = max(0, original_conf - _SINGLETON_PENALTY)
                finding["singleton_penalty"] = True

    # -----------------------------------------------------------------------
    # Phase 4: Contradiction and security escalation detection
    # -----------------------------------------------------------------------
    location_groups_active = {}
    for finding in active:
        key = (finding.get("file", ""), finding.get("line_start", 0))
        location_groups_active.setdefault(key, []).append(finding)

    for key, group in location_groups_active.items():
        if len(group) < 2:
            group[0].setdefault("contradiction", False)
            group[0].setdefault("security_escalation", False)
            continue

        severities = {f.get("severity", "low").lower() for f in group}
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
# Detection: finding has dimension "comment-accuracy" or "documentation"
# or the finding's category/subcategory field contains "comment".
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
_FUNCTIONAL_VIOLATION_KEYWORDS = re.compile(
    r"\bcrash\b|\bdata\s+loss\b|\bsilent(?:ly)?\b|\bincorrect\b|\bwrong\b|\bfail(?:s|ure)?\b"
    r"|\bruntime\s+error\b|\bexception\b|\bpanic\b|\bundefined\s+behavio(?:u)?r\b",
    re.IGNORECASE,
)

# Keywords that promote type_design findings from suggestion to main
# These indicate a type safety bug that could cause runtime errors
_TYPE_SAFETY_BUG_KEYWORDS = re.compile(
    r"\bruntime\b|\bcastexception\b|\btype\s+error\b|\bclasscastexception\b"
    r"|\bnull\s+pointer\b|\bnullpointer\b|\btype\s+mismatch\b",
    re.IGNORECASE,
)


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
        title = finding.get("title", "")
        description = finding.get("description", "")
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
_TEST_CORRECTNESS_PATTERNS = [
    re.compile(r"\brace\s+condition\b", re.IGNORECASE),
    re.compile(r"\balways\s+pass(?:es)?\b", re.IGNORECASE),
    re.compile(r"\balways[-\s]pass(?:es)?\b", re.IGNORECASE),
    re.compile(r"\bnever\s+fail(?:s)?\b", re.IGNORECASE),
    re.compile(r"\bvacuous(?:ly)?\b", re.IGNORECASE),
    re.compile(r"\btautolog(?:y|ical)\b", re.IGNORECASE),
    re.compile(r"\bassert(?:ion)?\s+(?:is\s+)?never\s+reached\b", re.IGNORECASE),
    re.compile(r"\bdeadlock\b", re.IGNORECASE),
    re.compile(r"\bdata\s+race\b", re.IGNORECASE),
    re.compile(r"\bthread\s+(?:safety|unsafe|race)\b", re.IGNORECASE),
    re.compile(r"\btest\s+(?:never\s+)?(?:actually\s+)?(?:verif|test|check)(?:s|ies)?\s+nothing\b", re.IGNORECASE),
    re.compile(r"\bfalse\s+positive\s+(?:test|assertion)\b", re.IGNORECASE),
    re.compile(r"\bincorrect(?:ly)?\s+(?:assert|verify|test)\b", re.IGNORECASE),
    re.compile(r"\bwrong\s+(?:value|result|output)\b", re.IGNORECASE),
    re.compile(r"\blocal\s+variable\s+(?:is\s+)?never\s+(?:used|read)\b", re.IGNORECASE),
    re.compile(r"\bassert(?:s|ion)?\s+(?:on\s+)?(?:a\s+)?(?:local|copy|snapshot)\b", re.IGNORECASE),
    re.compile(r"\bcompares?\s+(?:wrong|incorrect|different)\s+object\b", re.IGNORECASE),
    re.compile(r"\btest\s+(?:does\s+not|doesn'?t)\s+(?:wait|join|block)\b", re.IGNORECASE),
    re.compile(r"\breader\s+thread\s+not\s+waited\b", re.IGNORECASE),
    re.compile(r"\bflaky\s+test\b", re.IGNORECASE),
    re.compile(r"\bassertion\s+always\s+(?:true|passes?|succeed)\b", re.IGNORECASE),
    re.compile(r"\bassert(?:s|ion)?\s+(?:is\s+)?always\s+(?:true|pass(?:es?)?|succeed)\b", re.IGNORECASE),
    re.compile(r"\btest\s+(?:is\s+)?always\s+(?:true|pass(?:es?)?|succeed)\b", re.IGNORECASE),
    re.compile(r"\blogic\s+error\b", re.IGNORECASE),
    re.compile(r"\bincorrect\s+(?:logic|behavior|behaviour|result)\b", re.IGNORECASE),
]


def _is_test_correctness_finding(finding):
    """
    Return True if a test-analyzer finding describes a functional correctness bug
    that exists today (race condition, logic error, always-pass assertion, etc.)
    rather than a coverage gap ("should add tests for X").

    Decision test: "Does this finding describe a bug that exists today,
    or a test that should be written?"
    """
    title = finding.get("title", "")
    description = finding.get("description", "")
    combined = f"{title}\n{description}"

    for pattern in _TEST_CORRECTNESS_PATTERNS:
        if pattern.search(combined):
            return True
    return False


def group_by_proximity(findings, line_proximity=5):
    """
    Group findings by (file, line_bucket) where line_bucket is computed by
    rounding line_start to the nearest ``line_proximity`` lines.

    Two findings are considered co-located when they reference the same file
    and their line_start values differ by at most ``line_proximity``.

    Returns a dict mapping (file, line_bucket) -> list[finding].

    This utility is shared between dedup_cross_agent and apply_challenges.py
    (T04), which uses the same proximity grouping to correlate challenge results
    with original findings.
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


def dedup_cross_agent(findings):
    """
    Generalized cross-agent dedup: when two or more findings from *different*
    agents reference the same file and are within 5 lines of each other, keep
    only the best finding and eliminate the rest.

    Winner selection priority (highest priority first):
      1. Core dimension beats non-core.
         Core dimensions: ``bug``, ``security``, ``cross_file_impact``, ``intent``.
      2. Higher ``confidence`` value wins (after the above).
      3. Longer ``description`` string wins (tie-break).

    Losers receive ``eliminated_by="dedup:cross-agent"`` and an
    ``elimination_reason`` explaining why the winner was chosen.

    Groups where all findings come from the *same* agent are left intact so
    that within-agent findings are not incorrectly deduplicated.

    Returns (deduplicated_findings, dropped_duplicates).
    """
    LINE_PROXIMITY = 5

    def _safe_int_line(f):
        try:
            return int(f.get("line_start", 0))
        except (TypeError, ValueError):
            return 0

    def _winner_key(f):
        """Higher key value = better priority (sort descending)."""
        dim = f.get("dimension", "").lower()
        is_core = dim in _CORE_DIMENSIONS
        conf = f.get("confidence", 0)
        desc_len = len(f.get("description", ""))
        return (int(is_core), conf, desc_len)

    groups = group_by_proximity(findings, line_proximity=LINE_PROXIMITY)

    kept_finding_ids = set()   # tracks finding["id"] values, not Python id()
    dropped = []

    for group in groups.values():
        # Only apply cross-agent dedup when 2+ *different* agents appear
        agents_in_group = {f.get("agent", "").lower() for f in group}
        if len(group) < 2 or len(agents_in_group) < 2:
            for f in group:
                fid = f.get("id", "")
                if fid:
                    kept_finding_ids.add(fid)
            continue

        # Sort by priority (best first)
        ranked = sorted(group, key=_winner_key, reverse=True)
        winner = ranked[0]
        winner_agent = winner.get("agent", "").lower()
        winner_id = winner.get("id", "")
        if winner_id:
            kept_finding_ids.add(winner_id)

        for loser in ranked[1:]:
            loser_agent = loser.get("agent", "").lower()
            loser_id = loser.get("id", "")
            # Keep same-agent siblings of the winner — only drop different-agent findings
            if loser_agent == winner_agent:
                if loser_id:
                    kept_finding_ids.add(loser_id)
                continue
            loser_line = _safe_int_line(loser)
            winner_line = _safe_int_line(winner)
            dup = dict(loser)
            dup["eliminated_by"] = "dedup:cross-agent"
            dup["elimination_reason"] = (
                f"cross-agent dedup: finding at "
                f"{loser.get('file', '?')}:{loser_line} "
                f"(agent={loser.get('agent', '?')!r}, "
                f"dim={loser.get('dimension', '?')!r}, "
                f"conf={loser.get('confidence', '?')}) "
                f"lost to agent={winner.get('agent', '?')!r} "
                f"at line {winner_line} within {LINE_PROXIMITY} lines"
            )
            dropped.append(dup)
            warn(
                f"[dedup] Dropped finding {loser.get('id', '?')!r} "
                f"(agent={loser.get('agent', '?')!r}) at "
                f"{loser.get('file', '?')}:{loser_line} "
                f"— lost to {winner.get('agent', '?')!r} (cross-agent dedup)"
            )

    # Findings without an "id" field pass through (they can't be tracked for dedup)
    kept = [f for f in findings
            if f.get("id", "") in kept_finding_ids or not f.get("id")]
    return kept, dropped


def _dedup_test_analyzer(findings):
    """
    Backward-compatible wrapper: delegates to dedup_cross_agent.

    Retained so tests and external callers that import _dedup_test_analyzer
    directly continue to work. New code should call dedup_cross_agent instead.
    """
    return dedup_cross_agent(findings)


def tag_findings(findings):
    """
    Tag each finding as "main" (main report) or "suggestion" (improvement suggestions)
    and apply the test-analyzer dedup rule.

    Step 1 — Dedup: If a test-analyzer finding overlaps with another agent's finding
    at the same file/line range, the non-test-analyzer finding wins and the
    test-analyzer duplicate is dropped.

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

    Returns (tagged_findings, eliminated_duplicates, main_count, suggestion_count).
    """
    # Step 1: Cross-agent dedup (generalizes old test-analyzer-only dedup)
    findings, dedup_dropped = dedup_cross_agent(findings)

    # Step 2 & 3: Dimension-based routing, then agent-based fallback
    main_count = 0
    suggestion_count = 0

    for finding in findings:
        agent = finding.get("agent", "").lower()
        dimensions = {finding.get("dimension", "").lower()} if finding.get("dimension") else set()

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

    return findings, dedup_dropped, main_count, suggestion_count


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
    Remove findings whose title or description matches an exclusion pattern.

    Returns (passed, eliminated) lists. Each eliminated finding gains
    "eliminated_by" = "exclusion".
    """
    if not exclusion_patterns:
        return findings, []

    passed = []
    eliminated = []

    for finding in findings:
        title = finding.get("title", "")
        description = finding.get("description", "")
        combined = f"{title}\n{description}"

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
    # Normalize legacy field names (BF-14)
    # ------------------------------------------------------------------
    normalize_field_names(findings)

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

    # Step 3: injection filter
    findings, elim_injection = apply_injection_filter(findings)
    all_eliminated.extend(elim_injection)
    injections_removed = len(elim_injection)

    # Step 4: disagreement detection (returns active findings, suppressed, boosted_count)
    findings, elim_suppressed, consensus_boosted = detect_disagreement(findings)
    all_eliminated.extend(elim_suppressed)

    # Step 5: tag for output routing (also applies test-analyzer dedup)
    findings, elim_dedup, tagged_main, tagged_suggestion = tag_findings(findings)
    all_eliminated.extend(elim_dedup)

    # Count promotions (test-analyzer findings promoted to main report)
    promoted_count = sum(1 for f in findings if f.get("promoted_from") == "test-analyzer")

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
            "injections_removed": injections_removed,
            "consensus_boosted": consensus_boosted,
            "singleton_penalized": singleton_penalized,
            "dimension_routed": dimension_routed,
            "cross_agent_deduped": len(elim_dedup),
            "test_analyzer_deduped": len(elim_dedup),  # backward-compat alias
            "test_analyzer_promoted": promoted_count,
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
