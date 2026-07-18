#!/usr/bin/env python3
"""Regenerate expected.json golden files from the authoritative Python twins.

Usage: python3 workflows/test/tools/record_parity.py [<script>] [<case>]
Reads each case's input.json, dispatches to the Python function, writes expected.json.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
FIXTURES = REPO / "tests" / "fixtures" / "parity"


def _finding_dedup(inp):
    from finding_dedup import dedup_by_id
    merged, dupes, dropped = dedup_by_id(inp["ndjson_findings"], inp["text_findings"])
    return {"merged": merged, "duplicates_resolved": dupes, "dropped_no_id": dropped}


# Registered per-script recorders. Later tasks append entries here.
RECORDERS = {"finding_dedup": _finding_dedup}


def record(script, case_dir):
    inp = json.loads((case_dir / "input.json").read_text())
    out = RECORDERS[script](inp)
    (case_dir / "expected.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")


def main(argv):
    only_script = argv[1] if len(argv) > 1 else None
    only_case = argv[2] if len(argv) > 2 else None
    for script, _fn in RECORDERS.items():
        if only_script and script != only_script:
            continue
        for case_dir in sorted((FIXTURES / script).iterdir()):
            if not case_dir.is_dir() or (only_case and case_dir.name != only_case):
                continue
            record(script, case_dir)
            print(f"recorded {script}/{case_dir.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
