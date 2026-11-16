"""Workflow evidence survives collection, decoding and caller-level funnel scoring."""

import hashlib
import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from typing import get_args

import pytest

from bench.runner import observations, score, stages
from bench.runner.epochs import (
    file_hash,
    read_json,
)

ROOT = Path(__file__).resolve().parents[2]


def test_old_version_trace_fixture_loads_without_losing_claim_or_provenance():
    path = Path(__file__).parent / "fixtures/measurement_trace_v1.json"
    trace = stages.load_trace(path, expected_sha256=file_hash(path))
    assert trace.schema_version == 1
    assert trace.dropped_stages == ()
    assert (
        trace.versions[0].finding["evidence"] == "Source inspected by the old producer"
    )
    inputs = stages.discovery_inputs({"synthetic": (path, trace)}, {})
    assert "Legacy claim" in inputs["synthetic"]["discover:v0"][0]["text"]


def test_large_trace_keeps_all_discovery_claims_at_the_bench_boundary(tmp_path):
    path = tmp_path / "large.json"
    subprocess.run(
        [
            "node",
            str(ROOT / "workflows/test/tools/emit_measurement_trace.mjs"),
            str(path),
            "large",
        ],
        check=True,
        capture_output=True,
    )
    trace = stages.load_trace(path, expected_sha256=file_hash(path))
    inputs = stages.discovery_inputs({"synthetic": (path, trace)}, {})
    assert trace.schema_version == 2 and trace.complete
    assert trace.dropped_stages == ()
    assert trace.snapshots[0].coverage == "complete"
    assert {v.discovery_dimension for v in trace.versions} == {"bug"}
    assert len(inputs["synthetic"]) == 400
    assert all(
        "long claim with concrete evidence" in entries[0]["text"]
        for entries in inputs["synthetic"].values()
    )


@pytest.mark.parametrize("optional", [(), ("cite",), ("cite", "dispose")])
def test_funnel_coverage_ignores_absent_optional_stages(optional):
    ref = stages.ScoreRef("run", "epoch", "score")
    rows = tuple(
        stages.StageBugObservation(
            ref, "attempt", "url", bug, stage, True, (), None, "complete"
        )
        for bug in ("legacy", "current")
        for stage in get_args(stages.Stage)
        if stage not in ("cite", "dispose") or (bug == "current" and stage in optional)
    )
    funnel = stages.loss_funnel(rows, population={"legacy", "current"})
    assert funnel.coverage == "complete"
    assert funnel.delivered_hits == funnel.denominator == 2
    assert not any(dict(funnel.unjudged_counts).values())
    for stage in ("cite", "dispose"):
        assert dict(funnel.survivor_counts).get(stage, 0) == int(stage in optional)
        assert dict(funnel.cap_hit_counts).get(stage, 0) == 0
    if optional:
        partial = tuple(
            replace(row, survives=None) if row.stage == optional[0] else row
            for row in rows
        )
        funnel = stages.loss_funnel(partial, population={"legacy", "current"})
        assert funnel.coverage == "partial"
        assert dict(funnel.unjudged_counts)[optional[0]] == 1


def emit_trace(tmp_path, mode="return"):
    path = tmp_path / f"trace-{mode}.json"
    subprocess.run(
        [
            "node",
            str(ROOT / "workflows/test/tools/emit_measurement_trace.mjs"),
            str(path),
            mode,
            str(tmp_path / f"artifacts-{mode}"),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    receipt = read_json(path.with_suffix(".json.receipt.json"))
    raw = read_json(path)
    by_id = {v["version_id"]: v for v in raw["versions"]}
    receipt["payload"]["postReview"]["findings"] = [
        by_id[v]["finding"]
        for s in raw["snapshots"]
        if s["stage"] == "select"
        for v in s["finding_version_ids"]
    ]
    return stages.load_trace(path, expected_sha256=receipt["sha256"]), path, receipt


@pytest.mark.parametrize("mode", ["legacy", "derived", "return"])
def test_workflow_trace_crosses_python_boundary_without_checkpoint_members(
    tmp_path, mode
):
    trace, path, receipt = emit_trace(tmp_path, mode)
    assert trace.complete
    assert trace.run_nonce == "nonce-xyz"
    assert tuple(s.stage for s in trace.snapshots) == (
        "discover",
        "merge",
        "verify",
        "validate",
        "filter",
        "admission",
        "challenge",
        "dispose",
        "select",
    )
    assert set(receipt["payload"]["checkpoints"]["phases"]) == {"challenge"}
    assert "snapshots" not in receipt["payload"]["checkpoints"]
    first = next(v for v in trace.versions if v.finding.get("id") == "FILTER")
    assert first.projection()["confidence"] == 65
    changed = [
        v
        for v in trace.versions
        if v.trace_id == first.trace_id
        and v.projection().get("verdict_route") == "report-only"
    ]
    assert changed
    report = next(v for v in trace.versions if v.finding.get("id") == "REPORT")
    selection = next(s for s in trace.snapshots if s.stage == "select")
    decision = next(d for d in selection.decisions if d.trace_id == report.trace_id)
    assert decision.reason_code == "report_only"


@pytest.mark.parametrize(
    "mutation",
    [
        "hash",
        "duplicate_version",
        "reference",
        "dimension",
        "provenance",
        "stage",
        "flag",
        "duplicate_occurrence",
        "reason",
        "folded_elimination",
        "detail_missing_field",
        "detail_surface",
        "detail_status",
        "detail_kind",
        "detail_policy_subject",
        "detail_shape",
        "dropped_unknown",
        "dropped_duplicate",
        "dropped_evidence",
        "dropped_legacy",
    ],
)
def test_trace_reader_refuses_corrupt_evidence(tmp_path, mutation):
    _, path, receipt = emit_trace(tmp_path)
    raw = read_json(path)
    version, snapshot = raw["versions"][0], raw["snapshots"][0]
    if mutation == "hash":
        expected = "0" * 64
    else:
        if mutation == "duplicate_version":
            raw["versions"].append(dict(version))
        elif mutation == "reference":
            snapshot["finding_version_ids"].append("absent")
        elif mutation == "dimension":
            version["discovery_dimension"] = "high"
        elif mutation == "provenance":
            version["finding"]["trace_id"] = "forged"
        elif mutation == "stage":
            snapshot["stage"] = "deliver"
        elif mutation == "reason":
            raw["snapshots"][1]["decisions"][0]["reason_code"] = None
        elif mutation.startswith("dropped_"):
            raw["dropped_stages"] = ["select"]
            if mutation != "dropped_evidence":
                raw["snapshots"][-1].update(
                    finding_version_ids=[], decisions=[], coverage="unavailable"
                )
            if mutation == "dropped_unknown":
                raw["dropped_stages"] = ["deliver"]
            elif mutation == "dropped_duplicate":
                raw["dropped_stages"] *= 2
            elif mutation == "dropped_legacy":
                raw["schema_version"] = 1
        elif mutation.startswith("detail_"):
            detail = next(
                decision["detail"]
                for snap in raw["snapshots"]
                for decision in snap["decisions"]
                if (decision.get("detail") or {}).get("predicates")
            )
            if mutation == "detail_missing_field":
                del detail["original_producer"]
            elif mutation == "detail_surface":
                detail["surface"] = "invalid"
            elif mutation == "detail_status":
                detail["predicates"][0]["status"] = "invalid"
            elif mutation == "detail_kind":
                detail["kind"] = "invalid"
            elif mutation == "detail_policy_subject":
                detail["policy_subject"] = "invalid"
            else:
                detail["predicate"] = "challenge_terminal"
        elif mutation == "folded_elimination":
            raw["snapshots"][1]["decisions"][0]["reason_code"] = "folded"
        elif mutation == "flag":
            snapshot["executed"] = 1
        else:
            snapshot["finding_version_ids"].append(snapshot["finding_version_ids"][0])
        path.write_text(json.dumps(raw), encoding="utf-8")
        expected = file_hash(path)
    with pytest.raises((ValueError, KeyError, TypeError)):
        stages.load_trace(path, expected_sha256=expected)


@pytest.mark.parametrize(
    "trace_status",
    [
        "readable",
        "no_eliminations",
        "missing",
        "unreadable",
        "collection_failed",
        "holdback",
        "disabled",
        "judge_failed",
        "audit_overlap",
        "cut_select",
        "cut_dispose",
        "cut_challenge",
        "cut_filter",
        "cut_verify",
    ],
)
def test_score_to_dimension_credit_and_loss_funnel(
    score_case, tmp_path, monkeypatch, trace_status
):
    from bench.tests.test_provenance import captured_diff, payload_for

    trace, path, receipt = emit_trace(
        tmp_path, "audit" if trace_status == "audit_overlap" else "return"
    )
    pr = score_case.payload.parent
    review = receipt["payload"]["postReview"]
    if trace_status == "no_eliminations":
        raw = read_json(path)
        ids = raw["snapshots"][0]["finding_version_ids"]
        for version in raw["versions"]:
            version["finding"].update(
                location_status="confirmed",
                line_start=10,
                line_end=10,
                disposition="deliver",
            )
            if version["finding"].get("admission"):
                version["finding"]["admission"]["status"] = "admitted"
        review["findings"] = [
            v["finding"] for v in raw["versions"] if v["version_id"] in ids
        ]
        for snapshot in raw["snapshots"]:
            snapshot.update(finding_version_ids=ids, decisions=[])
        path.write_text(json.dumps(raw), encoding="utf-8")
    selected = review["findings"]
    diff = captured_diff(selected)
    (pr / "diff.patch").write_text(diff, encoding="utf-8")
    score._write_json(score_case.payload, payload_for(selected, diff, "github"))
    score._write_json(pr / "code-gauntlet-post-review-abc1234.json", review)
    if trace_status == "collection_failed":

        def fail_collection(*args):
            raise ValueError("collection exploded")

        monkeypatch.setattr(stages, "_collect_trace", fail_collection)
        stages.collect_trace(pr)
    if trace_status == "holdback":
        raw = read_json(path)
        tid = next(
            v.trace_id for v in trace.versions if v.finding.get("id") == "FILTER"
        )
        version_traces = {v["version_id"]: v["trace_id"] for v in raw["versions"]}
        decision = stages.StageDecision(
            tid, "held_back", "validation_skipped", None, ()
        )
        for snapshot in raw["snapshots"]:
            if snapshot["stage"] in ("filter", "admission", "challenge"):
                snapshot["finding_version_ids"] = raw["snapshots"][3][
                    "finding_version_ids"
                ]
                snapshot["decisions"] = []
            if snapshot["stage"] == "dispose":
                snapshot["finding_version_ids"] = [
                    v
                    for v in snapshot["finding_version_ids"]
                    if version_traces[v] != tid
                ]
                snapshot["stage"] = "holdback"
                snapshot["decisions"] = [asdict(decision)]
        path.write_text(json.dumps(raw), encoding="utf-8")
    cut = trace_status.removeprefix("cut_") if trace_status.startswith("cut_") else None
    if cut:
        raw = read_json(path)
        start = next(i for i, s in enumerate(raw["snapshots"]) if s["stage"] == cut)
        raw["dropped_stages"] = [s["stage"] for s in raw["snapshots"][start:]]
        for snapshot in raw["snapshots"][start:]:
            snapshot.update(
                finding_version_ids=[], decisions=[], coverage="unavailable"
            )
        path.write_text(json.dumps(raw), encoding="utf-8")

        def unavailable_treatment(*args):
            pytest.fail("dropped selection must not require a delivery treatment tree")

        monkeypatch.setattr(
            score.provenance, "resolve_treatment_root", unavailable_treatment
        )
    if trace_status not in ("missing", "collection_failed"):
        (pr / "code-gauntlet-measurement-abc1234.json").write_bytes(
            path.read_bytes() if trace_status != "unreadable" else b"{}"
        )
    discovery = next(s for s in trace.snapshots if s.stage == "discover")
    versions = {v.version_id: v for v in trace.versions}
    initial = {versions[v].finding.get("id"): v for v in discovery.finding_version_ids}
    keep, lost = (
        initial["GROUP"],
        initial["AUDIT" if trace_status == "audit_overlap" else "FILTER"],
    )
    score_case.hits.update({"*": (), "deep-review": (0,), "discover:" + lost: (2,)})
    if trace_status == "judge_failed":
        original = type(score_case).scorer

        def fail_judge(self, pin, key, model):
            original(self, pin, key, model)
            if "attribution" in model.parts:
                raise RuntimeError("unavailable judge")

        monkeypatch.setattr(type(score_case), "scorer", fail_judge)
    packet = score_case.execute(
        attribution=trace_status != "disabled", surface_views=bool(cut)
    )
    loaded = observations.load_packet(Path(packet["ledger_row"]["packet_path"]))
    assert {r.stage for r in stages.read_rows(loaded.root)}.isdisjoint({"cite"})
    assert packet["profiles"]["all"]["tp"] == 1
    assert len(score_case.calls) == (
        2
        if trace_status
        in ("readable", "holdback", "judge_failed", "audit_overlap", "cut_select")
        or cut
        else 1
    )
    tools = read_json(loaded.root / "candidates.json")[score_case.url]
    assert list(tools) == ["deep-review"]
    if len(score_case.calls) == 2:
        inputs = (
            loaded.root
            / "work/attribution/results"
            / loaded.manifest.evaluator.judge_pin
            / "candidates.json"
        )
        assert ("discover:" + keep in read_json(inputs)[score_case.url]) == bool(cut)
    if trace_status == "collection_failed":
        assert loaded.manifest.trace_collection[
            score_case.url
        ] == stages.TraceCollection("unavailable", "collection exploded")
    if trace_status == "no_eliminations":
        assert loaded.costs["attribution"] == 0 and len(score_case.calls) == 1
        assert not (loaded.root / "work/attribution").exists()
        return
    if trace_status == "disabled":
        assert stages.read_rows(loaded.root) == ()
        return
    rows = stages.read_rows(loaded.root)
    funnel = stages.loss_funnel(
        rows, population={b.bug_id for b in loaded.observations}
    )
    if cut:
        assert loaded.surface_view is None
        assert read_json(loaded.root / "surface-views.json")["unavailable_reason"]
        discovery_rows = [r for r in rows if r.stage == "discover"]
        assert all(
            r.coverage == "complete" and r.survives is not None for r in discovery_rows
        )
        assert any(r.survives for r in discovery_rows)
        assert all(
            r.coverage == "unavailable" and r.survives is None
            for r in rows
            if r.stage in raw["dropped_stages"]
        )
        assert read_json(loaded.root / "provenance_errors.json") == {}
        assert all(c.provenance_status == "legacy_unknown" for c in loaded.candidates)
        if cut in ("filter", "verify"):
            lost_bugs = {
                r.bug_id for r in discovery_rows if lost in r.matched_version_ids
            }
            assert lost_bugs
            assert all(
                r.loss.status == "unavailable"
                for r in rows
                if r.stage == "deliver" and r.bug_id in lost_bugs
            )
    elif trace_status in ("readable", "holdback", "audit_overlap"):
        assert (
            dict(funnel.losses)[
                "validate"
                if trace_status == "holdback"
                else "verify"
                if trace_status == "audit_overlap"
                else "delivery"
            ]
            == 1
        )
        if trace_status == "audit_overlap":
            assert any(
                r.loss
                and r.loss.reason_code == "audit_overlap"
                and r.loss.physical_stage == "verify"
                and r.loss.status == "established"
                for r in rows
            )
        dimension = versions[keep].discovery_dimension
        assert packet["metrics"]["per_dimension"][dimension]["delivered_bug_hits"] == 1
        assert (
            packet["metrics"]["per_dimension"][dimension]["discovery_recall"]
            is not None
        )
        credited = stages.credited_dimensions(loaded.observations[0], loaded.candidates)
        assert set(credited) == {dimension, "intent"}
    else:
        assert funnel.unavailable_count == len(loaded.observations) - 1
        assert sum(dict(funnel.losses).values()) == 0
        assert all(r.survives is None for r in rows if r.stage == "discover")


def test_trace_vocabularies_match_workflow_source():
    values = json.loads(
        subprocess.check_output(
            [
                "node",
                "--input-type=module",
                "-e",
                "import {TRACE_STAGES,TRACE_REASONS,TRACE_DISCOVERY_DIMENSIONS} from './workflows/src/measurementTrace.js'; console.log(JSON.stringify([TRACE_STAGES,TRACE_REASONS,TRACE_DISCOVERY_DIMENSIONS]));",
            ],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
        )
    )
    assert values == [
        list(stages.get_args(stages.Stage)),
        list(stages.get_args(stages.ReasonCode)),
        list(stages.get_args(observations.Dimension)),
    ]


@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("terminal_reason", ["held_back", "report_only"])
def test_trace_reasons_survive_delivery_overlay(tmp_path, extended, terminal_reason):
    trace, path, _ = emit_trace(tmp_path)
    versions = tuple({v.trace_id: v for v in trace.versions}.values())[:5]
    ids = tuple(v.trace_id for v in versions)
    vids = tuple(v.version_id for v in versions)
    snapshots = [
        replace(s, finding_version_ids=vids, decisions=()) for s in trace.snapshots
    ]
    decisions = tuple(
        stages.StageDecision(tid, decision, reason, reason, related)
        for tid, decision, reason, related in (
            (ids[0], "body_routed", "body_routed", ()),
            (ids[1], "held_back", "held_back", ()),
            (ids[2], "eliminated", "refuted", ()),
            (ids[3], "coalesced", "folded", (ids[4],)),
        )
    )
    if extended:
        index = next(i for i, s in enumerate(snapshots) if s.stage == "challenge") + 1
        snapshots[index:index] = [
            replace(snapshots[index - 1], stage="cite"),
        ]
    snapshots[-1] = replace(snapshots[-1], decisions=decisions)
    trace = replace(
        trace,
        versions=versions,
        snapshots=tuple(replace(s, sequence=i) for i, s in enumerate(snapshots)),
    )
    path.write_text(json.dumps(asdict(trace)), encoding="utf-8")
    trace = stages.load_trace(path, expected_sha256=file_hash(path))
    bound = stages.bind_delivery_trace(trace, ())
    assert bound.snapshots[-1].decisions[:4] == (decisions[3], *decisions[:3])
    assert bound.snapshots[-1].decisions[0].decision == "coalesced"
    assert bound.snapshots[-1].decisions[0].related_trace_ids == (ids[4],)
    assert [
        d.decision for d in bound.snapshots[-1].decisions if d.trace_id == ids[3]
    ] == ["coalesced"]
    # A folded source follows its representative rather than becoming a merge loss.
    snapshots[1] = replace(
        snapshots[1], finding_version_ids=(vids[4],), decisions=(decisions[3],)
    )
    for i in range(2, len(snapshots)):
        snapshots[i] = replace(
            snapshots[i], finding_version_ids=(vids[4],), decisions=()
        )
    snapshots[-1] = replace(
        snapshots[-1],
        finding_version_ids=(),
        decisions=(
            stages.StageDecision(ids[4], terminal_reason, terminal_reason, None, ()),
        ),
    )
    trace = replace(
        trace, snapshots=tuple(replace(s, sequence=i) for i, s in enumerate(snapshots))
    )
    from bench.runner.contracts import ScoreRef

    bug = observations.BugObservation(
        1,
        ScoreRef("run", "epoch", "score"),
        "url",
        "attempt",
        "bug",
        None,
        "missed",
        (),
        (),
        None,
    )
    # Discovery judging uses the public capture boundary and the same synthetic golden records.
    from bench.runner.epochs import GoldenBug, load_epoch

    epoch = replace(
        load_epoch(), bugs=(GoldenBug("bug", "url", 0, "claim", "high", None),)
    )
    request = observations.ScoreRequest(
        bug.score_ref,
        epoch,
        tmp_path,
        ("url",),
        (("url", "attempt"),),
        (),
        None,
        score.evaluator_for(epoch, epoch.judge_pin, epoch.adjudicator_pin),
    )
    inputs = {
        "url": {"discover:" + v.version_id: [{"text": "claim"}] for v in versions}
    }
    evaluations = {
        "url": {
            tool: {
                "tp": int(tool.endswith(vids[3])),
                "fp": 0,
                "fn": int(not tool.endswith(vids[3])),
                "true_positives": [
                    {"golden_comment": "claim", "matched_candidate": "claim"}
                ]
                if tool.endswith(vids[3])
                else [],
                "false_negatives": []
                if tool.endswith(vids[3])
                else [{"golden_comment": "claim"}],
            }
            for tool in inputs["url"]
        }
    }
    rows = stages.capture_score(
        {"url": (path, stages.bind_delivery_trace(trace, ()))},
        (bug,),
        request,
        evaluations,
        {},
        inputs,
        {},
    )
    loss = next(r.loss for r in rows if r.stage == "deliver")
    assert loss.reason_code == terminal_reason and loss.stage == "delivery"


def test_collection_hashes_top_level_trace_and_persists_availability(tmp_path):
    _, trace_path, receipt = emit_trace(tmp_path)
    directory = tmp_path / "pr"
    (directory / "workflows").mkdir(parents=True)
    (directory / "workflows/wf_current.json").write_text(
        json.dumps({"result": receipt["out"]}), encoding="utf-8"
    )
    collected = stages.collect_trace(directory)
    assert collected.status == "available" and collected.reason is None
    binding = read_json(directory / "measurement-artifact.json")
    assert binding["sha256"] == file_hash(trace_path)
    assert (directory / binding["path"]).read_bytes() == trace_path.read_bytes()
    replay_path = directory / "code-gauntlet-delivery-replay.json"
    assert replay_path.read_bytes() == receipt["out"]["measurement"]["replay"][
        "text"
    ].encode("utf-8")
    assert stages.collect_trace(directory) == collected
    missing = stages.collect_trace(tmp_path / "missing")
    assert missing.status == "unavailable" and missing.reason


def test_collection_persists_ledger_decode_gap_without_losing_trace(tmp_path):
    trace, _, receipt = emit_trace(tmp_path)
    measurement = receipt["out"]["measurement"]
    raw = json.loads(measurement["text"])
    raw["coverage_ledger"] = {"version": 2}
    measurement["text"] = json.dumps(raw)
    directory = tmp_path / "pr"
    (directory / "workflows").mkdir(parents=True)
    (directory / "workflows/wf_current.json").write_text(
        json.dumps({"result": receipt["out"]}), encoding="utf-8"
    )
    collected = stages.collect_trace(directory)
    assert collected.status == "available"
    assert collected.reason and "incomplete CoverageLedger" in collected.reason
    assert (
        read_json(directory / "measurement-collection.json")["reason"]
        == collected.reason
    )
    binding = read_json(directory / "measurement-artifact.json")
    assert binding["coverage_ledger_gap"] == collected.reason
    loaded = stages.load_trace(
        directory / binding["path"], expected_sha256=binding["sha256"]
    )
    assert (
        loaded.coverage_ledger is None
        and loaded.coverage_ledger_gap == collected.reason
    )
    assert loaded.versions == trace.versions


@pytest.fixture(scope="module")
def emitted_coverage_ledgers():
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            """
import { discover as rawDiscover } from './workflows/src/discover.js';
import { withPreparedPlan } from './workflows/test/helpers/diskDiscovery.js';
const discover=(ctx,input)=>withPreparedPlan(ctx,input,rawDiscover);
import { boundedCoverageLedger } from './workflows/src/coverageLedger.js';
const regions = [0,1].map(i=>({id:`r${i}`,file:'source',line_start:i?0:1,line_end:i?0:2,reads:[{offset:1,limit:2,bytes:80}]}));
const plan = {version:2,status:'available',fact_packets:[],regions};
const ledgers=[];
for(const reply of [null,{findings:[],complete:false,total_seen:0},{findings:[],complete:true,total_seen:0},
{findings:[{file:'other',line_start:7,title:'Outside focus',description:'A present defect in nearby code',severity:'high',confidence:80,evidence:'source:1',anchor:'return process(value);',change_origin:'introduced'}],complete:true,total_seen:1}]) {
 const ctx={agent:async(_,o)=>o.agentType==='code-gauntlet:gap-hunter'?reply:{findings:[],complete:true,total_seen:0,removed_test_audit:[]},parallel:async ts=>Promise.all(ts.map(t=>t()))};
 for(const input of [{},{agentFlags:{deep:false}},{discoveryPlan:{}},{discoveryPlan:{...plan,regions:regions.map(r=>({...r,reads:[{offset:1,limit:2,bytes:25000}]}))}}]) {
  const out=await discover(ctx,{contextPath:'/prepared',contextLines:10,discoveryPlan:plan,...input});
  if(out.coverageLedger)ledgers.push(out.coverageLedger);
 }
}
const ctx={agent:async()=>null,parallel:async ts=>Promise.all(ts.map(t=>t()))};
ledgers.push((await discover(ctx,{contextPath:'/prepared',contextLines:10,discoveryPlan:plan})).coverageLedger);
ledgers.push(boundedCoverageLedger(ledgers[0],0));
console.log(JSON.stringify(ledgers));
""",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_ledger_wire_keys_and_closed_values_match_live_discovery(
    emitted_coverage_ledgers,
):
    from typing import get_type_hints

    from gauntlet.discovery_plan import CoverageLedger, TrimmedCoverageLedger

    from bench.runner.decode import decode

    for ledger in emitted_coverage_ledgers:
        shape = (
            TrimmedCoverageLedger if ledger["status"] == "trimmed" else CoverageLedger
        )
        assert set(ledger) == set(get_type_hints(shape))
        assert decode(shape, ledger) == ledger
    assert {
        h["status"] for ledger in emitted_coverage_ledgers for h in ledger["hunters"]
    } == {"complete", "incomplete", "failed"}
    assert any(
        h.get("outside_assignment")
        for ledger in emitted_coverage_ledgers
        for h in ledger["hunters"]
    )
    assert {
        r["reason"] for ledger in emitted_coverage_ledgers for r in ledger["omitted"]
    } >= {"scope", "no_head_anchor", "primary_failure", "budget"}


@pytest.mark.parametrize("shape", ["available", "trimmed"])
def test_unknown_ledger_fields_degrade_only_coverage(
    tmp_path, emitted_coverage_ledgers, shape
):
    trace, path, _ = emit_trace(tmp_path)
    ledger = next(
        ledger for ledger in emitted_coverage_ledgers if ledger["status"] == shape
    )
    raw = asdict(trace)
    raw["coverage_ledger"] = {**ledger, "future_key": True}
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = stages.load_trace(path, expected_sha256=file_hash(path))
    assert loaded.coverage_ledger is None
    assert (
        loaded.coverage_ledger_gap
        and "invalid coverage ledger" in loaded.coverage_ledger_gap
    )
    assert loaded.versions == trace.versions and loaded.snapshots == trace.snapshots


def test_legacy_hunter_receipts_preserve_trace_coverage(
    tmp_path, emitted_coverage_ledgers
):
    trace, path, _ = emit_trace(tmp_path)
    ledger = next(
        ledger for ledger in emitted_coverage_ledgers if ledger.get("hunters")
    )
    raw = asdict(trace)
    raw["coverage_ledger"] = {
        **ledger,
        "hunters": [
            {k: v for k, v in h.items() if k != "outside_assignment"}
            for h in ledger["hunters"]
        ],
    }
    path.write_text(json.dumps(raw), encoding="utf-8", newline="")
    loaded = stages.load_trace(path, expected_sha256=file_hash(path))
    assert loaded.coverage_ledger == raw["coverage_ledger"]
    assert loaded.versions == trace.versions


@pytest.mark.parametrize(
    "encoding,text,reason",
    [
        ("unknown", "{}", "unknown measurement encoding"),
        ("json-pool-lzw16-base64-v1", "!", "base64"),
        ("json-pool-lzw16-base64-v1", "AA==", "truncated compressed measurement"),
        ("json-pool-lzw16-base64-v1", "AQA=", "invalid compressed measurement code"),
    ],
)
def test_unreadable_compressed_transport_records_collection_gap(
    tmp_path, encoding, text, reason
):
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "wf_current.json").write_text(
        json.dumps(
            dict(
                result=dict(
                    ok=True,
                    phaseReached="report",
                    measurement=dict(status="available", encoding=encoding, text=text),
                )
            )
        ),
        encoding="utf-8",
    )
    receipt = stages.collect_trace(tmp_path)
    assert receipt.status == "unavailable"
    assert reason in receipt.reason.lower()
    assert (
        read_json(tmp_path / "measurement-collection.json")["reason"] == receipt.reason
    )


@pytest.mark.parametrize(
    "condition", ["valid", "missing", "changed", "escape", "symlink"]
)
def test_external_coverage_artifacts_are_hash_bound_without_losing_trace(
    tmp_path, emitted_coverage_ledgers, condition
):
    from gauntlet.discovery_disk import artifact_name

    trace, path, _ = emit_trace(tmp_path)
    ledger = next(x for x in emitted_coverage_ledgers if x["status"] == "available")
    raw = trace.wire()
    name = artifact_name("coverage-ledger", "abc1234", trace.run_nonce)
    target = path.parent / name
    data = json.dumps(ledger).encode()

    digest = hashlib.sha256(data).hexdigest()
    if condition == "escape":
        name = "../outside.json"
    elif condition == "symlink":
        other = path.parent / "other-ledger.json"
        other.write_bytes(data)
        target.symlink_to(other)
    elif condition != "missing":
        target.write_bytes(data if condition == "valid" else b"changed")
    raw["coverage_ledger_artifact"] = {"path": name, "sha256": digest}
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = stages.load_trace(path, expected_sha256=file_hash(path))
    assert loaded.versions == trace.versions and loaded.snapshots == trace.snapshots
    if condition == "valid":
        assert loaded.coverage_ledger == ledger
        assert loaded.coverage_ledger_gap is None
    else:
        assert loaded.coverage_ledger is None
        assert "coverage ledger artifact" in loaded.coverage_ledger_gap


@pytest.mark.parametrize("copy_ok", [True, False])
def test_trace_collection_copies_the_bound_ledger_from_the_pipeline_output_directory(
    tmp_path, emitted_coverage_ledgers, monkeypatch, copy_ok
):
    from gauntlet.discovery_disk import artifact_name

    trace, _, receipt = emit_trace(tmp_path)
    ledger = next(x for x in emitted_coverage_ledgers if x["status"] == "available")
    output = tmp_path / "output"
    output.mkdir()
    name = artifact_name("coverage-ledger", "abc1234", trace.run_nonce)
    artifact = output / name
    artifact.write_text(json.dumps(ledger), encoding="utf-8")
    raw = trace.wire()
    raw["coverage_ledger_artifact"] = {"path": name, "sha256": file_hash(artifact)}
    receipt["out"]["measurement"]["text"] = json.dumps(raw)
    receipt["out"]["artifactPaths"]["findings"] = str(
        output / "code-gauntlet-findings-abc1234.json"
    )
    directory = tmp_path / "pr"
    (directory / "workflows").mkdir(parents=True)
    (directory / "workflows/wf_current.json").write_text(
        json.dumps({"result": receipt["out"]}), encoding="utf-8"
    )
    if not copy_ok:
        from bench.runner import contracts

        def unavailable_copy(*_):
            raise PermissionError("ledger copy unavailable")

        monkeypatch.setattr(contracts, "copy_exclusive", unavailable_copy)
    collected = stages.collect_trace(directory)
    assert collected.status == "available"
    if copy_ok:
        assert collected.reason is None
        assert (directory / name).read_bytes() == artifact.read_bytes()
    else:
        assert "coverage ledger artifact" in collected.reason
    binding = read_json(directory / "measurement-artifact.json")
    loaded = stages.load_trace(
        directory / binding["path"], expected_sha256=binding["sha256"]
    )
    assert loaded.coverage_ledger == (ledger if copy_ok else None)
    assert loaded.versions == trace.versions


@pytest.mark.parametrize("available", [True, False])
def test_scoring_preserves_coverage_gaps_and_binds_ledger_source_bytes(
    score_case, tmp_path, emitted_coverage_ledgers, available
):
    from gauntlet.discovery_disk import artifact_name

    trace, _, _ = emit_trace(tmp_path)
    pr = score_case.payload.parent
    ledger = next(
        item for item in emitted_coverage_ledgers if item["status"] == "available"
    )
    name = artifact_name("coverage-ledger", "abc1234", trace.run_nonce)
    data = json.dumps(ledger).encode()
    target = pr / name
    if available:
        target.write_bytes(data)
    raw = trace.wire()
    raw["coverage_ledger_artifact"] = {
        "path": name,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    (pr / "code-gauntlet-measurement-trace.json").write_text(
        json.dumps(raw), encoding="utf-8"
    )
    (pr / "measurement-collection.json").write_text(
        json.dumps({"status": "available", "reason": "collector coverage note"}),
        encoding="utf-8",
    )
    packet = score_case.execute()
    loaded = observations.load_packet(Path(packet["ledger_row"]["packet_path"]))
    collection = loaded.manifest.trace_collection[score_case.url]
    assert collection.status == "available"
    if available:
        assert collection.reason == "collector coverage note"
        relative = target.relative_to(score_case.run).as_posix()
        assert (
            loaded.manifest.source_artifact_hashes[relative]
            == hashlib.sha256(data).hexdigest()
        )
        target.write_bytes(data + b"\n")
        assert (
            loaded.manifest.source_artifact_hashes[relative]
            != hashlib.sha256(target.read_bytes()).hexdigest()
        )
    else:
        assert "coverage ledger artifact" in collection.reason


def test_ledger_loader_follows_the_producers_nonce_component_contract(
    tmp_path, emitted_coverage_ledgers, monkeypatch
):
    from gauntlet import discovery_disk

    trace, path, _ = emit_trace(tmp_path)
    monkeypatch.setattr(discovery_disk, "artifact_nonce", lambda _: "a" * 12)
    ledger = next(
        item for item in emitted_coverage_ledgers if item["status"] == "available"
    )
    name = discovery_disk.artifact_name("coverage-ledger", "abc1234", trace.run_nonce)
    target = path.parent / name
    target.write_text(json.dumps(ledger), encoding="utf-8")
    raw = trace.wire()
    raw["coverage_ledger_artifact"] = {"path": name, "sha256": file_hash(target)}
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert (
        stages.load_trace(path, expected_sha256=file_hash(path)).coverage_ledger
        == ledger
    )
