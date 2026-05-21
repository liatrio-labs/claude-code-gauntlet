#!/usr/bin/env python3
"""
finding_dedup.py — Standalone finding deduplication module.

Provides a standalone copy of the deduplication logic from merge_findings.py
as an importable, testable unit. Useful for:
  - Integration with external systems (Grove, Slack, custom dashboards)
  - Cross-session finding persistence (same finding on re-review shouldn't reappear)
  - Building on top of deep-review's output in your own pipeline

No external dependencies. stdlib only.

Usage (library):
    from scripts.finding_dedup import (
        dedup_by_id,
        dedup_by_location,
        FindingStore,
    )

    # Deduplicate by finding ID (original merge_findings.py behavior)
    merged, dupes, dropped = dedup_by_id(ndjson_findings, text_findings)

    # Deduplicate by code location (cross-agent, same-location detection)
    merged, dupes = dedup_by_location(flat_findings)

    # Cross-session persistence
    store = FindingStore("/tmp/review-session-abc1234.json")
    new_findings = store.filter_new(current_findings)
    store.save(current_findings)

Usage (CLI):
    # Dedup a merged JSON file by location, write result to stdout
    python3 scripts/finding_dedup.py merged.json --mode location
    python3 scripts/finding_dedup.py merged.json --mode id --output deduped.json

    # Check for findings from a previous session and remove them
    python3 scripts/finding_dedup.py merged.json --prior-session prior.json
"""

import argparse
import hashlib
import json
import os
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Dedup key types
# ---------------------------------------------------------------------------

def _id_key(finding: dict) -> Optional[str]:
    """Return finding ID, or None if missing."""
    return finding.get("id")


_LOCATIONLESS_KEY = ("", 0, 0, "")


def _location_key(finding: dict) -> tuple:
    """Return (file, line_start, line_end, dimension) dedup key.

    Exact-match semantics: two findings collide only when all four fields
    match. This catches the same location reported by multiple agents under
    different IDs — cross-agent deduplication that ID-based dedup misses.

    Unlike filter_findings.dedup_cross_agent (group_by_proximity with a
    5-line bucket on file+line, no line_end or dimension), this module
    requires exact line_start, line_end, and dimension equality.
    """
    return (
        finding.get("file", ""),
        finding.get("line_start", 0),
        finding.get("line_end", finding.get("line_start", 0)),
        finding.get("dimension", ""),
    )


# ---------------------------------------------------------------------------
# ID-based dedup (preserves merge_findings.py semantics)
# ---------------------------------------------------------------------------

def dedup_by_id(
    ndjson_findings: dict[str, list[dict]],
    text_findings: dict[str, list[dict]],
) -> tuple[list[dict], int, int]:
    """Deduplicate findings by ID, preferring NDJSON over text fallback.

    Delegated by merge_findings.deduplicate() — single source of truth.
    Returns (merged_list, duplicates_resolved_count, dropped_no_id_count).
    """
    # id -> (finding, priority)  ndjson=2 wins over text=1
    seen: dict[str, tuple[dict, int]] = {}
    duplicates_resolved = 0
    dropped_no_id = 0

    def _add(finding: dict, priority: int) -> None:
        nonlocal duplicates_resolved, dropped_no_id
        fid = finding.get("id")
        if fid is None:
            dropped_no_id += 1
            return
        if fid in seen:
            existing_priority = seen[fid][1]
            if priority > existing_priority:
                seen[fid] = (finding, priority)
            duplicates_resolved += 1
        else:
            seen[fid] = (finding, priority)

    for _agent, findings in text_findings.items():
        for f in findings:
            _add(f, 1)

    for _agent, findings in ndjson_findings.items():
        for f in findings:
            _add(f, 2)

    merged = [item[0] for item in seen.values()]
    return merged, duplicates_resolved, dropped_no_id


# ---------------------------------------------------------------------------
# Location-based dedup (cross-agent, cross-session)
# ---------------------------------------------------------------------------

def dedup_by_location(
    findings: list[dict],
    prefer_field: str = "confidence",
    higher_is_better: bool = True,
) -> tuple[list[dict], int]:
    """Deduplicate findings by (file, line_start, line_end, dimension).

    Catches the same code location reported by multiple agents under different
    IDs. Winner is determined by `prefer_field` (default: higher confidence).

    Findings with no location fields pass through unchanged (not collapsed
    on the empty sentinel key).

    Returns (deduplicated_list, duplicates_resolved_count).
    """
    seen: dict[tuple, dict] = {}
    locationless: list[dict] = []
    duplicates_resolved = 0

    for finding in findings:
        key = _location_key(finding)
        if key == _LOCATIONLESS_KEY:
            locationless.append(finding)
            continue
        if key in seen:
            existing = seen[key]
            existing_val = existing.get(prefer_field, 0)
            new_val = finding.get(prefer_field, 0)
            if (higher_is_better and new_val > existing_val) or (
                not higher_is_better and new_val < existing_val
            ):
                seen[key] = finding
            duplicates_resolved += 1
        else:
            seen[key] = finding

    return list(seen.values()) + locationless, duplicates_resolved


# ---------------------------------------------------------------------------
# Cross-session finding store
# ---------------------------------------------------------------------------

class FindingStore:
    """Persist findings across review sessions to avoid re-reporting known issues.

    Storage: a JSON file keyed by location hash. Compatible with the
    deep-review output schema.

    Grove integration note: to use Grove as the backing store instead of a
    local file, replace _load/_save with Grove API calls using the finding's
    location hash as the message key in a dedicated channel (e.g.
    #deep-review-findings). This lets findings persist across machines and
    be visible to the whole team.
    """

    def __init__(self, store_path: str) -> None:
        self._path = store_path
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, findings: list[dict]) -> None:
        """Persist findings to the store. Call after a successful review."""
        for f in findings:
            key = self._finding_hash(f)
            self._data[key] = {
                "file": f.get("file", ""),
                "line_start": f.get("line_start", 0),
                "line_end": f.get("line_end", 0),
                "dimension": f.get("dimension", ""),
                "title": f.get("title", ""),
                "severity": f.get("severity", ""),
            }
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)

    def filter_new(self, findings: list[dict]) -> tuple[list[dict], int]:
        """Return only findings not seen in a previous session.

        Returns (new_findings, count_of_prior_findings_suppressed).
        """
        new = []
        suppressed = 0
        for f in findings:
            key = self._finding_hash(f)
            if key in self._data:
                suppressed += 1
            else:
                new.append(f)
        return new, suppressed

    @staticmethod
    def _finding_hash(finding: dict) -> str:
        key = _location_key(finding)
        return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Standalone finding deduplication. Reads merged JSON, writes deduped JSON."
    )
    p.add_argument("input", help="Path to merged findings JSON (from merge_findings.py)")
    p.add_argument(
        "--mode", choices=["id", "location"], default="location",
        help="Dedup mode: 'id' (finding ID, ndjson-preferred) or 'location' (file+line+dim)"
    )
    p.add_argument(
        "--output", default="-",
        help="Output path (default: stdout)"
    )
    p.add_argument(
        "--prior-session", default=None,
        help="Path to a FindingStore JSON from a previous session; suppress known findings"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    with open(args.input, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    # Accept both bare list and envelope with "findings" key
    if isinstance(data, list):
        findings = data
    else:
        findings = data.get("findings", [])

    if args.mode == "id":
        # In id mode we artificially assign all findings to the ndjson side with priority 2
        # by passing {"loaded": findings} and {}. This is a deliberate simplification
        # where the original ndjson/text distinction (and priority behavior) is
        # intentionally collapsed for id-mode deduplication.
        merged, dupes, dropped_no_id = dedup_by_id({"loaded": findings}, {})
    else:
        merged, dupes = dedup_by_location(findings)
        dropped_no_id = 0

    prior_suppressed = 0
    if args.prior_session:
        store = FindingStore(args.prior_session)
        merged, prior_suppressed = store.filter_new(merged)

    result = {
        "findings": merged,
        "dedup_stats": {
            "mode": args.mode,
            "input_count": len(findings),
            "output_count": len(merged),
            "duplicates_resolved": dupes,
            "dropped_no_id": dropped_no_id,
            "prior_session_suppressed": prior_suppressed,
        },
    }

    output_json = json.dumps(result, indent=2)
    if args.output == "-":
        print(output_json)
    else:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output_json)
        print(
            f"finding_dedup: {len(merged)} findings written "
            f"(dupes={dupes}, prior_suppressed={prior_suppressed})",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
