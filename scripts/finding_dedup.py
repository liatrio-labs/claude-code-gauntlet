#!/usr/bin/env python3
"""
finding_dedup.py — Canonical finding deduplication module.

Houses the canonical deduplication logic (merge_findings.deduplicate()
delegates here). Also usable standalone for tests and tooling.

No external dependencies. stdlib only.

Usage:
    from scripts.finding_dedup import dedup_by_id

    merged, dupes, dropped = dedup_by_id(ndjson_findings, text_findings)
"""


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
