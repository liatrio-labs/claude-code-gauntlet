"""Delivery findings, skipped groups and persisted report inputs."""

import json
from pathlib import Path
from typing import cast

from gauntlet.delivery.compose import SkippedEntry
from gauntlet.delivery.input import Finding
from gauntlet.registry import Admission


def admitted_stamp() -> Admission:
    return {
        "version": 1,
        "status": "admitted",
        "input_proof": "fixture",
        "input_fields": [],
        "reasons": [],
        "kind": "ordinary",
        "core": True,
        "tier": "main",
        "inline_eligible": True,
    }


def finding(*, omit: tuple[str, ...] = (), **values: object) -> dict[str, object]:
    start = values.get("line", 2)
    end = values.get("end_line", start + 1 if isinstance(start, int) else 3)
    fields = {
        "file": "foo.py",
        "line": 2,
        "end_line": 3,
        "line_start": values.get("line", 2),
        "line_end": end,
        "claimed_line_start": values.get("line", 2),
        "claimed_line_end": end,
        "location_status": "confirmed",
        "disposition": "deliver",
        "admission": admitted_stamp(),
        "anchor": "    return 1\n    # tail",
        "severity": "high",
        "title": "T",
        "body": "b",
        **values,
    }
    for key in omit:
        fields.pop(key, None)
        if key == "end_line":
            fields.pop("claimed_line_end", None)
    return fields


def skipped_group(
    filepath: object = "foo.py",
    line: object = 99,
    *,
    omit: tuple[str, ...] = (),
    **values: object,
) -> list[SkippedEntry]:
    return [
        SkippedEntry(
            filepath, line, finding(omit=omit, file=filepath, line=line, **values)
        )
    ]


def fix_finding(*, omit: tuple[str, ...] = (), **values: object) -> dict[str, object]:
    return finding(
        omit=omit, **{"suggested_fix_code": "    return 2\n    # done", **values}
    )


def patch_inputs(root: Path, findings: object, capture: bytes | None = None) -> None:
    (root / "code-gauntlet-findings-abc1234.json").write_bytes(
        findings if isinstance(findings, bytes) else json.dumps(findings).encode()
    )
    if capture is not None:
        (root / "code-gauntlet-diff-abc1234.patch").write_bytes(capture)


def date_override_members() -> list[Finding]:
    spans = [(107, 109), (114, 114), (125, 127), (141, 141)]
    agents = ["bug-detector", "cross-file-impact", "bug-detector", "security-reviewer"]
    stamp = {
        "issue_key": "group:F107",
        "lead_id": "F107",
        "finding_ids": [f"F{start}" for start, _ in spans],
    }
    rows = []
    for (start, end), agent in zip(spans, agents, strict=True):
        description = (
            "When a day has multiple date overrides, each range replaces the previous range. Valid slots in earlier ranges become unavailable."
            if start == 107
            else f"At site {start}, overlapping date overrides erase slots that should remain available."
        )
        rows.append(
            cast(
                Finding,
                finding(
                    id=f"F{start}",
                    file="src/availability/slots.ts",
                    line=start,
                    end_line=end,
                    title="Multiple date overrides on one day make valid slots unavailable"
                    if start == 107
                    else f"Repeated date override defect at {start}",
                    body=description,
                    description=description,
                    agent=agent,
                    dimension="bug",
                    confidence=90,
                    suggestion="Combine all override ranges for the day before filtering slots.",
                    report_tag="main",
                    delivery_group=stamp,
                ),
            )
        )
    return rows
