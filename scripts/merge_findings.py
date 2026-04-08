#!/usr/bin/env python3
"""
merge_findings.py — Deterministic Phase 3→4 merge for deep-review.

Reads agent findings from two channels:
  Channel 1 (primary): NDJSON files on disk at
      {findings_dir}/deep-review-{agent}-{session_sha}.ndjson
  Channel 2 (fallback): Agent text returns at
      {text_dir}/deep-review-text-{agent}-{session_sha}.txt

Processing: parse both channels, deduplicate by finding ID (NDJSON preferred),
inject agent field, validate dimension and required fields, detect truncation,
assemble Phase 4 input envelope with methodology diagnostics.

Usage:
    python3 merge_findings.py \\
      --findings-dir .deep-review \\
      --session-sha abc12345 \\
      --agents bug-detector security-reviewer cross-file-impact test-analyzer \\
               conventions-and-intent type-design-analyzer code-simplifier \\
      --text-dir .deep-review \\
      --base-branch main \\
      --head-sha abc123 \\
      --pr-number 42 \\
      --owner org \\
      --repo name \\
      --output merged.json

Output JSON schema:
    {
        "findings": [...],
        "base_branch": "main",
        "head_sha": "abc123",
        "pr_number": 42,
        "owner": "org",
        "repo": "name",
        "methodology": {
            "agents_dispatched": [...],
            "findings_per_channel": {"ndjson": N, "text_fallback": N},
            "duplicates_resolved": N,
            "truncation_warnings": [...],
            "validation_warnings": [...]
        }
    }
"""
import argparse
import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KNOWN_DIMENSIONS = {
    "bug",
    "security",
    "cross_file_impact",
    "test_coverage",
    "convention",
    "intent",
    "comment_accuracy",
    "type_design",
    "simplification",
}

REQUIRED_FIELDS = {"id", "file", "line_start", "title", "description", "severity", "confidence"}


# ---------------------------------------------------------------------------
# Channel 1: NDJSON file parsing
# ---------------------------------------------------------------------------

def _ndjson_path(findings_dir: str, agent: str, session_sha: str) -> str:
    return os.path.join(findings_dir, f"deep-review-{agent}-{session_sha}.ndjson")


def parse_ndjson_file(path: str, agent: str) -> tuple[list[dict], list[str]]:
    """Parse a single NDJSON file. Returns (findings, warnings)."""
    findings = []
    parse_warnings = []

    if not os.path.exists(path):
        return findings, parse_warnings

    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_warnings.append(
                    f"[{agent}] NDJSON line {lineno}: invalid JSON — {exc}"
                )
                continue
            if not isinstance(obj, dict):
                parse_warnings.append(
                    f"[{agent}] NDJSON line {lineno}: expected object, got {type(obj).__name__}"
                )
                continue
            findings.append(obj)

    return findings, parse_warnings


# ---------------------------------------------------------------------------
# Channel 2: Text file fallback parsing
# ---------------------------------------------------------------------------

def _text_path(text_dir: str, agent: str, session_sha: str) -> str:
    return os.path.join(text_dir, f"deep-review-text-{agent}-{session_sha}.txt")


def _extract_json_blocks(text: str) -> list[dict]:
    """Extract all valid top-level JSON objects containing an 'id' field."""
    results = []
    # Walk the text looking for '{' and try progressively larger slices
    i = 0
    while i < len(text):
        if text[i] != '{':
            i += 1
            continue
        # Try to parse from this position with increasing depth
        obj = _try_parse_json_at(text, i)
        if obj is not None and isinstance(obj, dict) and "id" in obj:
            results.append(obj)
            # Skip past this object (approximate — find the matching close brace)
            i = _find_end_of_json(text, i)
        else:
            i += 1
    return results


def _try_parse_json_at(text: str, start: int) -> dict | None:
    """Try to parse a JSON object starting at position start."""
    depth = 0
    in_string = False
    escape_next = False
    i = start
    while i < len(text):
        ch = text[i]
        if escape_next:
            escape_next = False
            i += 1
            continue
        if ch == '\\' and in_string:
            escape_next = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
        i += 1
    return None


def _find_end_of_json(text: str, start: int) -> int:
    """Return position after the JSON object starting at start."""
    depth = 0
    in_string = False
    escape_next = False
    i = start
    while i < len(text):
        ch = text[i]
        if escape_next:
            escape_next = False
            i += 1
            continue
        if ch == '\\' and in_string:
            escape_next = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i + 1


def parse_text_file(path: str, agent: str) -> tuple[list[dict], list[str], bool, bool]:
    """Parse a text return file for inline JSON blocks.

    Returns:
        (findings, warnings, has_prose, has_skip_lines)
        has_prose: True if text has meaningful non-JSON, non-SKIP content
        has_skip_lines: True if text contains SKIP: lines
    """
    findings = []
    parse_warnings = []
    has_prose = False
    has_skip_lines = False

    if not os.path.exists(path):
        return findings, parse_warnings, has_prose, has_skip_lines

    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    if not text.strip():
        return findings, parse_warnings, has_prose, has_skip_lines

    # Check for SKIP: lines
    if re.search(r'^\s*SKIP\s*:', text, re.MULTILINE | re.IGNORECASE):
        has_skip_lines = True

    # Extract JSON objects
    json_blocks = _extract_json_blocks(text)
    for obj in json_blocks:
        if "id" in obj:
            findings.append(obj)

    # Check for prose (non-trivial text content beyond JSON blocks)
    # Strip out all JSON blocks and see if meaningful content remains
    stripped = re.sub(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', '', text, flags=re.DOTALL)
    stripped = re.sub(r'^\s*SKIP\s*:.*$', '', stripped, flags=re.MULTILINE | re.IGNORECASE)
    stripped = stripped.strip()
    # Prose means at least 20 chars of non-whitespace text remains
    if len(re.sub(r'\s+', '', stripped)) > 20:
        has_prose = True

    return findings, parse_warnings, has_prose, has_skip_lines


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(
    ndjson_findings: dict[str, list[dict]],
    text_findings: dict[str, list[dict]],
) -> tuple[list[dict], int]:
    """Merge findings from both channels, preferring NDJSON on ID collision.

    Args:
        ndjson_findings: {agent: [finding, ...]} from Channel 1
        text_findings:   {agent: [finding, ...]} from Channel 2

    Returns:
        (merged_list, duplicates_resolved_count)
    """
    # id -> (finding, source_priority)  — ndjson=2 wins over text=1
    seen: dict[str, tuple[dict, int]] = {}
    duplicates_resolved = 0

    def _add(finding: dict, priority: int) -> None:
        nonlocal duplicates_resolved
        fid = finding.get("id")
        if fid is None:
            return
        if fid in seen:
            existing_priority = seen[fid][1]
            if priority > existing_priority:
                seen[fid] = (finding, priority)
            duplicates_resolved += 1
        else:
            seen[fid] = (finding, priority)

    # Channel 2 first (lower priority)
    for _agent, findings in text_findings.items():
        for f in findings:
            _add(f, 1)

    # Channel 1 second (higher priority — overwrites text version)
    for _agent, findings in ndjson_findings.items():
        for f in findings:
            _add(f, 2)

    merged = [item[0] for item in seen.values()]
    return merged, duplicates_resolved


# ---------------------------------------------------------------------------
# Agent field injection
# ---------------------------------------------------------------------------

def inject_agent_field(
    ndjson_findings: dict[str, list[dict]],
    text_findings: dict[str, list[dict]],
) -> None:
    """Inject the 'agent' field into every finding based on its source agent.

    Modifies findings in-place. NDJSON findings take priority, so we process
    text findings first and then NDJSON overwrites the agent field.
    """
    for agent, findings in text_findings.items():
        for f in findings:
            f["agent"] = agent

    for agent, findings in ndjson_findings.items():
        for f in findings:
            f["agent"] = agent


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_findings(findings: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate dimension and required fields. Return (valid_findings, warnings)."""
    valid = []
    val_warnings = []

    for f in findings:
        fid = f.get("id", "<no id>")
        reject = False

        # Required fields check
        for field in REQUIRED_FIELDS:
            if field not in f or f[field] is None or f[field] == "":
                val_warnings.append(
                    f"[{fid}] Missing required field '{field}' — finding rejected"
                )
                reject = True
                break

        if reject:
            continue

        # Dimension validation (warn but keep)
        dim = f.get("dimension")
        if dim is None:
            val_warnings.append(
                f"[{fid}] Missing 'dimension' field — finding kept with warning"
            )
        elif dim not in KNOWN_DIMENSIONS:
            val_warnings.append(
                f"[{fid}] Unknown dimension '{dim}' — finding kept with warning"
            )

        valid.append(f)

    return valid, val_warnings


# ---------------------------------------------------------------------------
# Truncation detection
# ---------------------------------------------------------------------------

def detect_truncation(
    agents: list[str],
    ndjson_raw_counts: dict[str, int],
    text_findings: dict[str, list[dict]],
    text_has_prose: dict[str, bool],
    text_has_skip: dict[str, bool],
) -> list[str]:
    """Detect agents whose output may have been truncated.

    Truncation suspected when:
      - NDJSON file missing or empty (no raw lines parsed, pre-validation)
      - Text fallback also has no valid JSON findings
      - Text has prose content (agent investigated something)
      - Text has no SKIP: lines (agent didn't explicitly skip)

    Using pre-validation NDJSON counts avoids false positives when findings
    exist in the NDJSON file but are rejected by validation.  Checking that
    the text channel also produced no findings avoids false positives when the
    text fallback successfully parsed valid JSON blocks.
    """
    truncation_warnings = []

    for agent in agents:
        ndjson_empty = ndjson_raw_counts.get(agent, 0) == 0
        text_empty = len(text_findings.get(agent, [])) == 0
        has_prose = text_has_prose.get(agent, False)
        has_skip = text_has_skip.get(agent, False)

        if ndjson_empty and text_empty and has_prose and not has_skip:
            truncation_warnings.append(
                f"[{agent}] Possible truncation: no structured findings, "
                f"prose present in text output, no SKIP lines detected"
            )

    return truncation_warnings


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------

def assemble_output(
    findings: list[dict],
    agents: list[str],
    ndjson_count: int,
    text_fallback_count: int,
    duplicates_resolved: int,
    truncation_warnings: list[str],
    validation_warnings: list[str],
    base_branch: str,
    head_sha: str,
    pr_number: int,
    owner: str,
    repo: str,
) -> dict:
    """Build the Phase 4 input envelope."""
    return {
        "findings": findings,
        "base_branch": base_branch,
        "head_sha": head_sha,
        "pr_number": pr_number,
        "owner": owner,
        "repo": repo,
        "methodology": {
            "agents_dispatched": agents,
            "findings_per_channel": {
                "ndjson": ndjson_count,
                "text_fallback": text_fallback_count,
            },
            "duplicates_resolved": duplicates_resolved,
            "truncation_warnings": truncation_warnings,
            "validation_warnings": validation_warnings,
        },
    }


# ---------------------------------------------------------------------------
# Main merge pipeline
# ---------------------------------------------------------------------------

def merge(
    findings_dir: str,
    session_sha: str,
    agents: list[str],
    text_dir: str,
    base_branch: str,
    head_sha: str,
    pr_number: int,
    owner: str,
    repo: str,
) -> dict:
    """Run the full merge pipeline. Returns the assembled output dict."""
    all_warnings: list[str] = []

    # --- Channel 1: NDJSON ---
    ndjson_findings: dict[str, list[dict]] = {}
    ndjson_paths: dict[str, str] = {}
    for agent in agents:
        path = _ndjson_path(findings_dir, agent, session_sha)
        ndjson_paths[agent] = path
        findings, warns = parse_ndjson_file(path, agent)
        ndjson_findings[agent] = findings
        all_warnings.extend(warns)

    # --- Channel 2: Text fallback ---
    text_findings: dict[str, list[dict]] = {}
    text_has_prose: dict[str, bool] = {}
    text_has_skip: dict[str, bool] = {}
    for agent in agents:
        path = _text_path(text_dir, agent, session_sha)
        findings, warns, has_prose, has_skip = parse_text_file(path, agent)
        text_findings[agent] = findings
        text_has_prose[agent] = has_prose
        text_has_skip[agent] = has_skip
        all_warnings.extend(warns)

    # --- Pre-validation raw counts (for truncation detection only) ---
    ndjson_raw_counts = {agent: len(findings) for agent, findings in ndjson_findings.items()}

    # --- Inject agent fields ---
    inject_agent_field(ndjson_findings, text_findings)

    # --- Validate each channel (before dedup so warnings cover all raw findings) ---
    all_ndjson_findings_flat = [f for findings in ndjson_findings.values() for f in findings]
    all_text_findings_flat = [f for findings in text_findings.values() for f in findings]
    _, pre_val_warnings = validate_findings(all_ndjson_findings_flat + all_text_findings_flat)

    # Filter each channel to only valid findings
    def _filter_valid(findings_dict: dict) -> dict:
        out = {}
        for agent, findings in findings_dict.items():
            valid, _ = validate_findings(findings)
            out[agent] = valid
        return out

    ndjson_findings = _filter_valid(ndjson_findings)
    # Preserve pre-validation text findings for truncation detection before filtering
    text_findings_pre_validation = {agent: list(findings) for agent, findings in text_findings.items()}
    text_findings = _filter_valid(text_findings)

    # --- Deduplicate ---
    merged_raw, duplicates_resolved = deduplicate(ndjson_findings, text_findings)

    # Channel counts (both post-validation, post-dedup for consistent measurement)
    ndjson_ids = {f["id"] for findings in ndjson_findings.values() for f in findings if "id" in f}
    ndjson_count = sum(1 for f in merged_raw if f.get("id") in ndjson_ids)
    text_fallback_count = sum(
        1 for f in merged_raw
        if f.get("id") not in ndjson_ids
    )

    # After dedup, no additional validation needed (already validated above)
    valid_findings = merged_raw
    val_warnings = pre_val_warnings

    # --- Truncation detection ---
    # Use pre-validation counts and pre-validation text findings to avoid false positives:
    # M4: findings that fail validation shouldn't make NDJSON appear empty
    # M5: text fallback with valid JSON blocks shouldn't trigger truncation warning
    truncation_warnings = detect_truncation(
        agents, ndjson_raw_counts, text_findings_pre_validation,
        text_has_prose, text_has_skip
    )

    # Aggregate all warnings into categories
    validation_warnings = all_warnings + val_warnings

    # --- Assemble output ---
    return assemble_output(
        findings=valid_findings,
        agents=agents,
        ndjson_count=ndjson_count,
        text_fallback_count=text_fallback_count,
        duplicates_resolved=duplicates_resolved,
        truncation_warnings=truncation_warnings,
        validation_warnings=validation_warnings,
        base_branch=base_branch,
        head_sha=head_sha,
        pr_number=pr_number,
        owner=owner,
        repo=repo,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Deterministic merge of Phase 3 agent findings into Phase 4 input JSON."
    )
    p.add_argument("--findings-dir", required=True,
                   help="Directory containing NDJSON finding files")
    p.add_argument("--session-sha", required=True,
                   help="Short SHA used as suffix in finding filenames")
    p.add_argument("--agents", nargs="+", required=True,
                   help="List of agent names (e.g. bug-detector security-reviewer)")
    p.add_argument("--text-dir", required=True,
                   help="Directory containing agent text return files")
    p.add_argument("--base-branch", required=True,
                   help="Base branch name (e.g. main)")
    p.add_argument("--head-sha", required=True,
                   help="Full or short head commit SHA")
    p.add_argument("--pr-number", type=int, default=0,
                   help="Pull request number (0 for local reviews)")
    p.add_argument("--owner", default="",
                   help="Repository owner (empty for local reviews)")
    p.add_argument("--repo", default="",
                   help="Repository name (empty for local reviews)")
    p.add_argument("--output", required=True,
                   help="Output JSON file path")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    result = merge(
        findings_dir=args.findings_dir,
        session_sha=args.session_sha,
        agents=args.agents,
        text_dir=args.text_dir,
        base_branch=args.base_branch,
        head_sha=args.head_sha,
        pr_number=args.pr_number,
        owner=args.owner,
        repo=args.repo,
    )

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)

    # Print summary to stderr
    m = result["methodology"]
    print(
        f"merge_findings: {len(result['findings'])} findings "
        f"(ndjson={m['findings_per_channel']['ndjson']}, "
        f"text_fallback={m['findings_per_channel']['text_fallback']}, "
        f"dupes={m['duplicates_resolved']})",
        file=sys.stderr,
    )
    if m["truncation_warnings"]:
        for w in m["truncation_warnings"]:
            print(f"  TRUNCATION WARNING: {w}", file=sys.stderr)
    if m["validation_warnings"]:
        for w in m["validation_warnings"]:
            print(f"  VALIDATION WARNING: {w}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
