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
        env = merge(
            findings_dir=fd,
            session_sha=args["session_sha"],
            agents=args["agents"],
            text_dir=td,
            base_branch=args["base_branch"],
            head_sha=args["head_sha"],
            pr_number=args["pr_number"],
            owner=args["owner"],
            repo=args["repo"],
        )
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
        passed, eliminated, contested_count = ff.apply_threshold_filter(
            inp["findings"], inp["config"]
        )
        return {
            "kept": passed,
            "eliminated": eliminated,
            "contested_count": contested_count,
        }
    if fn == "apply_injection_filter":
        kept, eliminated = ff.apply_injection_filter(inp["findings"])
        return {"kept": kept, "eliminated": eliminated}
    if fn == "apply_exclusions":
        kept, eliminated = ff.apply_exclusions(
            inp["findings"], inp["exclusion_patterns"]
        )
        return {"kept": kept, "eliminated": eliminated}
    if fn == "detect_disagreement":
        active, suppressed, boosted_count = ff.detect_disagreement(inp["findings"])
        return {
            "active": active,
            "suppressed": suppressed,
            "boosted_count": boosted_count,
        }
    if fn == "_route_by_dimension":
        # Single-finding-in, route-out -- no list plumbing needed.
        return {"route": ff._route_by_dimension(inp["finding"])}
    if fn == "consolidate_cross_agent":
        findings, consolidated_count = ff.consolidate_cross_agent(inp["findings"])
        return {"findings": findings, "consolidated_count": consolidated_count}
    if fn == "tag_findings":
        tagged, consolidated_count, main_count, suggestion_count = ff.tag_findings(
            inp["findings"]
        )
        return {
            "tagged": tagged,
            "consolidated_count": consolidated_count,
            "main_count": main_count,
            "suggestion_count": suggestion_count,
        }
    raise ValueError(fn)


def _apply_validations(inp):
    import copy

    from apply_validations import apply_validations

    findings = copy.deepcopy(inp["findings"])
    adjusted_count, unmatched_ids = apply_validations(findings, inp["validations"])
    return {
        "findings": findings,
        "adjusted_count": adjusted_count,
        "unmatched_ids": unmatched_ids,
    }


# The script's own audit trail (blame_metadata / factual_verification / diff_validation
# -- see verify_findings.py's "DELIBERATELY EXCLUDED" comment above its _DELTA_FIELDS
# constant). No workflow schema declares any of these, and joinVerifyDeltas (stages.js)
# only ever writes DELTA_VALUE_KEYS onto the finding it already holds -- it never carries
# any of these across the join either -- so a golden "joined" finding must not either.
# `agent` is DELIBERATELY NOT in this drop list as of #22: the join now keeps it
# deterministically on both the trusted and degraded paths (see stages.js
# joinVerifyDeltas and its doc comment).
_VERIFY_DELTA_DROP = (
    "blame_metadata",
    "factual_verification",
    "diff_validation",
)


def _project_verify_delta(finding):
    return {k: v for k, v in finding.items() if k not in _VERIFY_DELTA_DROP}


def _verify_deltas(inp):
    # Pins issue #25 requirement 1's equivalence claim: the findings the workflow
    # rebuilds by joining the delta onto the dispatched slice must equal, for every
    # field any downstream stage consumes, what verify_findings.py itself left on the
    # finding (minus its own audit trail and the withheld `agent` -- see
    # _VERIFY_DELTA_DROP above). Python (this function, via the real build_deltas/
    # deltas_checksum) owns the producing half; the JS twin (joinVerifyDeltas/
    # deltaContentProof, asserted in workflows/test/parity.test.js) owns the
    # reconstructing half; this golden is what sits between them.
    from verify_findings import build_deltas, deltas_checksum

    verified_by_id = {f["id"]: f for f in inp["result"]["verified"]}
    post_by_id = dict(verified_by_id)
    post_by_id.update({f["id"]: f for f in inp["result"]["eliminated"]})

    # Reorder to DISPATCH order using the SAME dict objects as result["verified"] /
    # result["eliminated"] (looked up from post_by_id, not re-serialized) -- build_deltas
    # decides membership by object identity (id(finding) in {id(f) for f in verified}),
    # exactly mirroring how verify_findings.py's own findings/verified pair are the same
    # mutated-in-place objects. Passing the DISPATCHED (pre-verification) objects instead
    # would make every finding look eliminated, since none of them is literally one of the
    # objects in result["verified"].
    ordered = [post_by_id[f["id"]] for f in inp["dispatched"]]

    deltas = build_deltas(ordered, inp["result"]["verified"])
    checksum = deltas_checksum(deltas)
    joined = [
        _project_verify_delta(post_by_id[f["id"]])
        for f in inp["dispatched"]
        if f["id"] in verified_by_id
    ]
    return {"deltas": deltas, "checksum": checksum, "joined": joined}


def _apply_challenges(inp):
    # Mirrors apply_challenges.py main()'s bridge composition (:444-480) --
    # apply_challenges() -> consolidate_cross_agent() re-run -> rank_findings() --
    # minus the file I/O (load_filtered/load_challenges) and prior_eliminated
    # concatenation, which belong to the skill/stage layer, not this pure
    # transform. Matches the JS twin's applyChallenges() return shape.
    import copy

    from apply_challenges import apply_challenges, rank_findings
    from filter_findings import consolidate_cross_agent

    findings = copy.deepcopy(inp["findings"])
    challenges = inp["challenges"]
    total_input = len(findings)
    active, challenge_eliminated, challenge_stats = apply_challenges(
        findings, challenges
    )
    active, cross_agent_consolidated = consolidate_cross_agent(active)
    active = rank_findings(active)
    stats = {
        "total_input": total_input,
        "challenge_removed": challenge_stats["challenge_removed"],
        "challenge_downgraded": challenge_stats["challenge_downgraded"],
        "challenge_contested": challenge_stats["challenge_contested"],
        "challenge_survived": challenge_stats["challenge_survived"],
        "unchallenged": challenge_stats["unchallenged"],
        "cross_agent_consolidated": cross_agent_consolidated,
        "final_count": len(active),
    }
    return {
        "findings": active,
        "eliminated": challenge_eliminated,
        "stats": stats,
    }


def _slice_input_proof(inp):
    # The slice-input content proof (issue #69 / #25 req 4-6). Python computes it over
    # the document verify_findings.py parses off disk; the JS twin computes the same
    # value over the content materializeVerifySlices dispatched, and trustSlice compares
    # them. A divergence between the runtimes would present as a corrupt slice input
    # rather than as a bug, so it is pinned by a golden rather than by each side
    # agreeing with itself.
    from verify_findings import _input_checksum

    return {"checksum": _input_checksum(inp["doc"])}


# Registered per-script recorders. Later tasks append entries here.
RECORDERS = {
    "finding_dedup": _finding_dedup,
    "merge_findings": _merge_findings,
    "filter_findings": _filter_findings,
    "apply_validations": _apply_validations,
    "apply_challenges": _apply_challenges,
    "verify_deltas": _verify_deltas,
    "slice_input_proof": _slice_input_proof,
}


def _compute(script, case_dir):
    inp = json.loads((case_dir / "input.json").read_text())
    return RECORDERS[script](inp)


def _serialize(out):
    return json.dumps(out, indent=2, sort_keys=True) + "\n"


def record(script, case_dir):
    (case_dir / "expected.json").write_text(_serialize(_compute(script, case_dir)))


def _iter_cases(only_script, only_case):
    """Yield (script, case_dir, case_label) for every input.json under FIXTURES,
    scoped by the optional script/case filters. Shared by in-place recording and
    --check so the two modes see exactly the same case set."""
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
            yield script, case_dir, case_label


def check(only_script=None, only_case=None):
    """Record every in-scope case into a TEMP tree (never touching the working
    tree) and diff the result against the committed goldens.

    Returns a list of human-readable mismatch lines (empty = fresh). Covers
    both directions -- a case whose committed expected.json disagrees with a
    fresh recording, AND a case with input.json but no committed expected.json
    at all (a brand-new fixture that was never authored/recorded; #214's
    TestGoldenFreshness could not see this direction at all, since it only
    ever compared bytes for expected.json paths that already existed on disk
    -- issue #211 review F7).
    """
    import tempfile

    mismatches = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        for script, case_dir, case_label in _iter_cases(only_script, only_case):
            rel = case_dir.relative_to(FIXTURES)
            fresh_bytes = _serialize(_compute(script, case_dir))
            fresh_path = tmp_root / rel / "expected.json"
            fresh_path.parent.mkdir(parents=True, exist_ok=True)
            fresh_path.write_text(fresh_bytes)

            committed_path = case_dir / "expected.json"
            if not committed_path.exists():
                mismatches.append(
                    f"MISSING committed golden: {rel}/expected.json "
                    "-- run record_parity.py to author it"
                )
                continue
            committed_bytes = committed_path.read_text()
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
    raise SystemExit(main(sys.argv))
