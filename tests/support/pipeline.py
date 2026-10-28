"""Transport and input builders for cross-runtime pipeline seams."""

import json
import subprocess
from pathlib import Path
from typing import Any, TypedDict

REPO = Path(__file__).resolve().parents[2]


class PersistedPayload(TypedDict):
    findings: list[dict[str, Any]]
    postReview: dict[str, object]


def run_node(
    script: str, payload: object, *, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=True,
    )


def build_gh_diff(findings: list[dict[str, Any]]) -> str:
    """Make each persisted finding's location a valid inline anchor."""
    max_line: dict[str, int] = {}
    for finding in findings:
        line = int(finding.get("line") or finding.get("line_start") or 1)
        path = finding["file"]
        max_line[path] = max(max_line.get(path, 0), line)
    parts = []
    for path, last_line in max_line.items():
        count = last_line + 1
        parts.extend(
            [
                f"diff --git a/{path} b/{path}\n",
                f"--- a/{path}\n",
                f"+++ b/{path}\n",
                f"@@ -1,1 +1,{count} @@\n",
                " context\n",
            ]
        )
        parts.extend("+return process(value);\n" for line in range(2, count + 1))
    return "".join(parts)


def methodology_input(
    mode: str,
    echo: dict[str, Any],
    *,
    pipeline_version: str = "3.26.0",
    plugin_root: str = "/absolute/path/to/claude-code-gauntlet",
    delivery_cap: int | None = None,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "configEcho": echo,
        "pluginRoot": plugin_root,
        "pipelineVersion": pipeline_version,
        "reviewScope": {
            "requested": "full",
            "kind": "full",
            "since": None,
            "commits": None,
            "detector": None,
        },
        "policy": {"tier": "optimized", "provider": "firstParty", "gateway": False},
        "deliveryTier": "all",
        "deliveryCap": delivery_cap,
        "gapCount": 0,
        "summary": "summary",
        "findings": [],
        "unverified": [],
        "dimensions": {"dispatched": [], "degraded": []},
        "stats": {
            "discovered": 0,
            "validate": {},
            "filter": {},
            "challenge": {},
            "merge": {},
        },
    }
