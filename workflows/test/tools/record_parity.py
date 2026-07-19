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


def _merge_findings(inp):
    import tempfile
    from merge_findings import merge
    args = inp["args"]
    with tempfile.TemporaryDirectory() as fd, tempfile.TemporaryDirectory() as td:
        for name, text in inp.get("findings_dir_files", {}).items():
            (Path(fd) / name).write_text(text)
        for name, text in inp.get("text_dir_files", {}).items():
            (Path(td) / name).write_text(text)
        env = merge(findings_dir=fd, session_sha=args["session_sha"], agents=args["agents"],
                    text_dir=td, base_branch=args["base_branch"], head_sha=args["head_sha"],
                    pr_number=args["pr_number"], owner=args["owner"], repo=args["repo"])
    return env


def _filter_findings(inp):
    import tempfile
    import filter_findings as ff
    fn = inp["fn"]
    if fn == "normalize_field_names":
        findings = inp["findings"]
        ff.normalize_field_names(findings)
        return {"findings": findings}
    if fn == "parse_review_md":
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as t:
            t.write(inp["markdown"])
            path = t.name
        return {"config": ff.parse_review_md(path)}
    if fn == "load_exclusions":
        # Not in the Task 4 brief's Step 1 skeleton — added because loadExclusions
        # is a Produced part-1 function (brief interfaces list) and the exclusions/
        # fixture case names (fenced_block_match, bullet_list_fallback) describe
        # load_exclusions's two parse paths, not apply_exclusions's matching.
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as t:
            t.write(inp["markdown"])
            path = t.name
        return {"patterns": ff.load_exclusions(path)}
    if fn == "apply_threshold_filter":
        # apply_threshold_filter returns a 3-tuple (passed, eliminated, contested_count),
        # not the 2-tuple the brief's Step 1 skeleton unpacks -- corrected per the
        # brief's own instruction to confirm arity against scripts/filter_findings.py.
        passed, eliminated, contested_count = ff.apply_threshold_filter(inp["findings"], inp["config"])
        return {"kept": passed, "eliminated": eliminated, "contested_count": contested_count}
    if fn == "apply_injection_filter":
        kept, eliminated = ff.apply_injection_filter(inp["findings"])
        return {"kept": kept, "eliminated": eliminated}
    if fn == "apply_exclusions":
        kept, eliminated = ff.apply_exclusions(inp["findings"], inp["exclusion_patterns"])
        return {"kept": kept, "eliminated": eliminated}
    if fn == "detect_disagreement":
        active, suppressed, boosted_count = ff.detect_disagreement(inp["findings"])
        return {"active": active, "suppressed": suppressed, "boosted_count": boosted_count}
    if fn == "_route_by_dimension":
        # Single-finding-in, route-out -- no list plumbing needed.
        return {"route": ff._route_by_dimension(inp["finding"])}
    if fn == "dedup_cross_agent":
        kept, dropped = ff.dedup_cross_agent(inp["findings"])
        return {"kept": kept, "dropped": dropped}
    if fn == "tag_findings":
        tagged, dedup_dropped, main_count, suggestion_count = ff.tag_findings(inp["findings"])
        return {
            "tagged": tagged,
            "dedup_dropped": dedup_dropped,
            "main_count": main_count,
            "suggestion_count": suggestion_count,
        }
    raise ValueError(fn)


def _apply_validations(inp):
    import copy
    from apply_validations import apply_validations
    findings = copy.deepcopy(inp["findings"])
    adjusted_count, unmatched_ids = apply_validations(findings, inp["validations"])
    return {"findings": findings, "adjusted_count": adjusted_count, "unmatched_ids": unmatched_ids}


# Registered per-script recorders. Later tasks append entries here.
RECORDERS = {
    "finding_dedup": _finding_dedup,
    "merge_findings": _merge_findings,
    "filter_findings": _filter_findings,
    "apply_validations": _apply_validations,
}


def record(script, case_dir):
    inp = json.loads((case_dir / "input.json").read_text())
    out = RECORDERS[script](inp)
    (case_dir / "expected.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")


def main(argv):
    only_script = argv[1] if len(argv) > 1 else None
    only_case = argv[2] if len(argv) > 2 else None
    for script in RECORDERS:
        if only_script and script != only_script:
            continue
        script_dir = FIXTURES / script
        # rglob (not iterdir) so both flat (<script>/<case>/) and grouped
        # (<script>/<group>/<case>/, e.g. filter_findings/threshold/<case>/) fixture
        # layouts are found uniformly, at whatever depth input.json actually lives.
        for input_path in sorted(script_dir.rglob("input.json")):
            case_dir = input_path.parent
            case_label = str(case_dir.relative_to(script_dir))
            if only_case and only_case not in (case_label, case_dir.name):
                continue
            record(script, case_dir)
            print(f"recorded {script}/{case_label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
