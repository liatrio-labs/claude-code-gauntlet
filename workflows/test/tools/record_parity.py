#!/usr/bin/env python3
"""Record verify-wire goldens with Python and the JS encoder for slice_inline.

Usage: python3 workflows/test/tools/record_parity.py [--check] [<script>] [<case>]
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
FIXTURES = REPO / "tests" / "fixtures" / "parity"


def _verify_deltas(inp):
    from verify_findings import build_deltas, deltas_checksum

    verified_by_id = {f["id"]: f for f in inp["result"]["verified"]}
    post_by_id = dict(verified_by_id)
    post_by_id.update({f["id"]: f for f in inp["result"]["eliminated"]})
    ordered = [post_by_id[f["id"]] for f in inp["dispatched"]]
    deltas = build_deltas(ordered, inp["result"]["verified"])
    checksum = deltas_checksum(deltas)
    joined = [
        _project_verify_delta(post_by_id[f["id"]])
        for f in inp["dispatched"]
        if f["id"] in verified_by_id
    ]
    return {"deltas": deltas, "checksum": checksum, "joined": joined}


_VERIFY_DELTA_DROP = (
    "blame_metadata",
    "factual_verification",
    "diff_validation",
)


def _project_verify_delta(finding):
    return {k: v for k, v in finding.items() if k not in _VERIFY_DELTA_DROP}


def _slice_input_proof(inp):
    from verify_findings import _input_checksum

    return {"checksum": _input_checksum(inp["doc"])}


def _slice_inline(inp):
    from assemble_artifacts import fnv1a32
    from verify_findings import _input_checksum

    source = (
        "import { encodeSliceInline } from './workflows/src/stages.js'; "
        "import fs from 'node:fs'; "
        "process.stdout.write(encodeSliceInline(JSON.parse(fs.readFileSync(0, 'utf8'))));"
    )
    encoded = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        input=json.dumps(inp["doc"], ensure_ascii=True),
        capture_output=True,
        text=True,
        cwd=REPO,
        check=True,
        encoding="utf-8",
    ).stdout
    return {
        "encoded": encoded,
        "checksum": _input_checksum(inp["doc"]),
        "token_checksum": fnv1a32(encoded),
    }


RECORDERS = {
    "verify_deltas": _verify_deltas,
    "slice_input_proof": _slice_input_proof,
    "slice_inline": _slice_inline,
}


def _compute(script, case_dir):
    inp = json.loads((case_dir / "input.json").read_text(encoding="utf-8"))
    return RECORDERS[script](inp)


def _serialize(out):
    return json.dumps(out, indent=2, sort_keys=True) + "\n"


def record(script, case_dir):
    (case_dir / "expected.json").write_text(
        _serialize(_compute(script, case_dir)), encoding="utf-8", newline=""
    )


def _iter_cases(only_script, only_case):
    for script in RECORDERS:
        if only_script and script != only_script:
            continue
        script_dir = FIXTURES / script
        for input_path in sorted(script_dir.rglob("input.json")):
            case_dir = input_path.parent
            case_label = str(case_dir.relative_to(script_dir))
            if only_case and only_case not in (case_label, case_dir.name):
                continue
            yield script, case_dir, case_label


def check(only_script=None, only_case=None):
    mismatches = []
    for script, case_dir, _case_label in _iter_cases(only_script, only_case):
        rel = case_dir.relative_to(FIXTURES)
        fresh_bytes = _serialize(_compute(script, case_dir))
        committed_path = case_dir / "expected.json"
        if not committed_path.exists():
            mismatches.append(
                f"MISSING committed golden: {rel}/expected.json "
                "-- run record_parity.py to author it"
            )
            continue
        committed_bytes = committed_path.read_text(encoding="utf-8")
        if committed_bytes != fresh_bytes:
            mismatches.append(
                f"STALE golden: {rel}/expected.json -- rerun record_parity.py"
            )
    return mismatches


def main(argv):
    args = argv[1:]
    check_mode = "--check" in args
    positional = [a for a in args if a != "--check"]
    only_script = positional[0] if len(positional) > 0 else None
    only_case = positional[1] if len(positional) > 1 else None

    if check_mode:
        mismatches = check(only_script, only_case)
        if mismatches:
            for line in mismatches:
                print(line, file=sys.stderr)
            print(f"{len(mismatches)} stale/missing golden(s)", file=sys.stderr)
            return 1
        print("all goldens fresh")
        return 0

    for script, case_dir, case_label in _iter_cases(only_script, only_case):
        record(script, case_dir)
        print(f"recorded {script}/{case_label}")
    return 0


if __name__ == "__main__":
    from script_io import run_entrypoint

    run_entrypoint(main, sys.argv)
