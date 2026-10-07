"""Delivery findings, skipped groups and persisted report inputs."""

import json
from pathlib import Path

from gauntlet.delivery.compose import SkippedEntry


def finding(*, omit: tuple[str, ...] = (), **values: object) -> dict[str, object]:
    fields = {
        "file": "foo.py",
        "line": 2,
        "end_line": 3,
        "severity": "high",
        "title": "T",
        "body": "b",
        **values,
    }
    for key in omit:
        fields.pop(key, None)
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
