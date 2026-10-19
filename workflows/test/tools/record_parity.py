#!/usr/bin/env python3
"""Record verify-wire goldens with Python and the JS encoder for slice_inline.

Usage: python3 workflows/test/tools/record_parity.py [--check] [<script>] [<case>]
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests" / "fixtures" / "parity"
FROZEN = {"decisions", "symbol_extractor"}


def _verify_deltas(inp):
    from gauntlet.verify.wire import build_deltas, checksum_or_none

    verified_by_id = {f["id"]: f for f in inp["result"]["verified"]}
    post_by_id = dict(verified_by_id)
    post_by_id.update({f["id"]: f for f in inp["result"]["eliminated"]})
    ordered = [post_by_id[f["id"]] for f in inp["dispatched"]]
    deltas = build_deltas(ordered, inp["result"]["verified"])
    checksum = checksum_or_none(deltas)
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
    from gauntlet.verify.wire import checksum_or_none

    return {"checksum": checksum_or_none(inp["doc"])}


def _slice_inline(inp):
    from gauntlet.jsjson import fnv1a32
    from gauntlet.verify.wire import checksum_or_none

    source = (
        "import { encodeSliceInline } from './workflows/src/verifyWire.js'; "
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
        "checksum": checksum_or_none(inp["doc"]),
        "token_checksum": fnv1a32(encoded),
    }


def _review_text_checksum(inp):
    from gauntlet.jsjson import fnv1a32, normalize_review_text

    normalized = normalize_review_text(inp["text"])
    return {"normalized": normalized, "checksum": fnv1a32(normalized)}


def _review_md_set_checksum(inp):
    from gauntlet.project_rules import review_md_set_checksum

    return {"checksum": review_md_set_checksum(inp["entries"])}


def _decisions(inp):
    from tests.support.verify_parity import replay_decisions

    with tempfile.TemporaryDirectory(prefix="verify-decisions-") as directory:
        return replay_decisions(inp, Path(directory))


def _symbol_extractor(inp):
    from tests.support.pipeline import run_node

    return json.loads(
        run_node(
            "import { extractSymbols } from './workflows/src/verifySymbols.js';"
            "let source = ''; for await (const chunk of process.stdin) source += chunk;"
            "process.stdout.write(JSON.stringify(JSON.parse(source).cases.map("
            "row => [...extractSymbols(row.description, row.evidence)].sort())));",
            inp,
        ).stdout
    )


RECORDERS = {
    "decisions": _decisions,
    "symbol_extractor": _symbol_extractor,
    "review_md_set_checksum": _review_md_set_checksum,
    "review_text_checksum": _review_text_checksum,
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
    if script in FROZEN:
        raise ValueError(f"{script} is a frozen Python oracle")
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
            remedy = (
                "investigate replay against the frozen Python oracle"
                if script in FROZEN
                else "rerun record_parity.py"
            )
            mismatches.append(f"STALE golden: {rel}/expected.json -- {remedy}")
    return mismatches


def main(argv=None):
    args = (sys.argv if argv is None else argv)[1:]
    check_mode = "--check" in args
    positional = [a for a in args if a != "--check"]
    only_script = positional[0] if len(positional) > 0 else None
    only_case = positional[1] if len(positional) > 1 else None

    if only_script is not None and only_script not in RECORDERS:
        print(f"unknown recorder family: {only_script}", file=sys.stderr)
        return 2

    if check_mode:
        mismatches = check(only_script, only_case)
        if mismatches:
            for line in mismatches:
                print(line, file=sys.stderr)
            print(f"{len(mismatches)} stale/missing golden(s)", file=sys.stderr)
            return 1
        print("all goldens fresh")
        return 0

    if only_script in FROZEN:
        print(f"{only_script} is a frozen Python oracle; use --check", file=sys.stderr)
        return 2
    for script, case_dir, case_label in _iter_cases(only_script, only_case):
        if script in FROZEN:
            continue
        record(script, case_dir)
        print(f"recorded {script}/{case_label}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    from gauntlet.cli import utf8_stdio

    utf8_stdio()
    raise SystemExit(main())
