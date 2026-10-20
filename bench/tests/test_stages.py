"""Workflow evidence survives collection, decoding and caller-level funnel scoring."""

import json
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from bench.runner import observations, score, stages
from bench.runner.epochs import (
    file_hash,
    read_json,
)

ROOT = Path(__file__).resolve().parents[2]


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
    return stages.load_trace(path, expected_sha256=receipt["sha256"]), path, receipt


@pytest.mark.parametrize("mode", ["legacy", "derived", "return"])
def test_workflow_trace_crosses_python_boundary_without_checkpoint_members(
    tmp_path, mode
):
    trace, path, receipt = emit_trace(tmp_path, mode)
    assert trace.complete
    assert trace.run_nonce == "nonce-xyz"
    assert tuple(s.stage for s in trace.snapshots) == stages.get_args(stages.Stage)
    assert set(receipt["payload"]["checkpoints"]["phases"]) == {"challenge"}
    assert "snapshots" not in receipt["payload"]["checkpoints"]
    first = next(v for v in trace.versions if v.model_finding_id == "FILTER")
    assert first.projection()["confidence"] == 65
    changed = [
        v
        for v in trace.versions
        if v.trace_id == first.trace_id and v.projection()["confidence"] == 45
    ]
    assert changed


@pytest.mark.parametrize(
    "mutation",
    ["hash", "cycle", "reference", "dimension", "provenance", "stage", "flag", "nan"],
)
def test_trace_reader_refuses_corrupt_evidence(tmp_path, mutation):
    _, path, receipt = emit_trace(tmp_path)
    raw = read_json(path)
    version, snapshot = raw["versions"][0], raw["snapshots"][0]
    if mutation == "hash":
        expected = "0" * 64
    else:
        if mutation == "cycle":
            version["base_version_id"] = version["version_id"]
        elif mutation == "reference":
            snapshot["finding_version_ids"].append("absent")
        elif mutation == "dimension":
            version["discovery_dimension"] = "high"
        elif mutation == "provenance":
            version["finding"]["trace_id"] = "forged"
        elif mutation == "stage":
            snapshot["stage"] = "deliver"
        elif mutation == "flag":
            snapshot["executed"] = 1
        else:
            version["finding"]["confidence"] = float("nan")
        path.write_text(json.dumps(raw), encoding="utf-8")
        expected = file_hash(path)
    with pytest.raises((ValueError, KeyError, TypeError)):
        stages.load_trace(path, expected_sha256=expected)


@pytest.mark.parametrize(
    "trace_status",
    [
        "readable",
        "missing",
        "unreadable",
        "collection_failed",
        "holdback",
        "disabled",
        "judge_failed",
    ],
)
def test_score_to_dimension_credit_and_loss_funnel(
    score_case, tmp_path, monkeypatch, trace_status
):
    from bench.tests.test_provenance import captured_diff, payload_for

    trace, path, receipt = emit_trace(tmp_path)
    pr = score_case.payload.parent
    review = receipt["payload"]["postReview"]
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
        tid = next(v.trace_id for v in trace.versions if v.model_finding_id == "FILTER")
        decision = stages.StageDecision(
            tid, "held_back", "validation_skipped", None, ()
        )
        for snapshot in raw["snapshots"]:
            if snapshot["stage"] in ("filter", "challenge"):
                snapshot["finding_version_ids"] = raw["snapshots"][3][
                    "finding_version_ids"
                ]
                snapshot["decisions"] = []
            if snapshot["stage"] == "holdback":
                snapshot["decisions"] = [asdict(decision)]
        path.write_text(json.dumps(raw), encoding="utf-8")
    if trace_status not in ("missing", "collection_failed"):
        (pr / "code-gauntlet-measurement-abc1234.json").write_bytes(
            path.read_bytes() if trace_status != "unreadable" else b"{}"
        )
    discovery = next(s for s in trace.snapshots if s.stage == "discover")
    versions = {v.version_id: v for v in trace.versions}
    initial = {versions[v].model_finding_id: v for v in discovery.finding_version_ids}
    keep, lost = initial["GROUP"], initial["FILTER"]
    score_case.hits.update({"*": (), "deep-review": (0,), "discover:" + lost: (2,)})
    if trace_status == "judge_failed":
        original = type(score_case).scorer

        def fail_judge(self, pin, key, model):
            original(self, pin, key, model)
            if "attribution" in model.parts:
                raise RuntimeError("unavailable judge")

        monkeypatch.setattr(type(score_case), "scorer", fail_judge)
    packet = score_case.execute(attribution=trace_status != "disabled")
    loaded = observations.load_packet(Path(packet["ledger_row"]["packet_path"]))
    assert packet["profiles"]["all"]["tp"] == 1
    assert len(score_case.calls) == (
        2 if trace_status in ("readable", "holdback", "judge_failed") else 1
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
        assert "discover:" + keep not in read_json(inputs)[score_case.url]
    if trace_status == "collection_failed":
        assert loaded.manifest.trace_collection[
            score_case.url
        ] == stages.TraceCollection("unavailable", "collection exploded")
    if trace_status == "disabled":
        assert stages.read_rows(loaded.root) == ()
        return
    rows = stages.read_rows(loaded.root)
    funnel = stages.loss_funnel(
        rows, population={b.bug_id for b in loaded.observations}
    )
    if trace_status in ("readable", "holdback"):
        assert (
            dict(funnel.losses)["validate" if trace_status == "holdback" else "filter"]
            == 1
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
