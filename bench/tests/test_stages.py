"""Workflow evidence survives collection, decoding and caller-level funnel scoring."""

import json
import subprocess
from pathlib import Path

import pytest

from bench.runner import observations, stages
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
    receipt = json.loads(
        path.with_suffix(".json.receipt.json").read_text(encoding="utf-8")
    )
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
    raw = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "hash":
        expected = "0" * 64
    else:
        if mutation == "cycle":
            raw["versions"][0]["base_version_id"] = raw["versions"][0]["version_id"]
        elif mutation == "reference":
            raw["snapshots"][0]["finding_version_ids"].append("absent")
        elif mutation == "dimension":
            raw["versions"][0]["discovery_dimension"] = "high"
        elif mutation == "provenance":
            raw["versions"][0]["finding"]["trace_id"] = "forged"
        elif mutation == "stage":
            raw["snapshots"][0]["stage"] = "deliver"
        elif mutation == "flag":
            raw["snapshots"][0]["executed"] = 1
        else:
            raw["versions"][0]["finding"]["confidence"] = float("nan")
        path.write_text(json.dumps(raw), encoding="utf-8")
        expected = file_hash(path)
    with pytest.raises((ValueError, KeyError, TypeError)):
        stages.load_trace(path, expected_sha256=expected)


@pytest.mark.parametrize("trace_status", ["readable", "missing", "unreadable"])
def test_score_to_dimension_credit_and_loss_funnel(
    score_case, tmp_path, monkeypatch, trace_status
):
    from bench.tests.test_provenance import captured_diff, payload_for

    trace, path, receipt = emit_trace(tmp_path)
    pr = score_case.payload.parent
    selected = receipt["payload"]["postReview"]["findings"]
    diff = captured_diff(selected)
    (pr / "diff.patch").write_text(diff, encoding="utf-8")
    score_case.payload.write_text(
        json.dumps(payload_for(selected, diff, "github")), encoding="utf-8"
    )
    (pr / "code-gauntlet-post-review-abc1234.json").write_text(
        json.dumps(receipt["payload"]["postReview"]), encoding="utf-8"
    )
    if trace_status != "missing":
        (pr / "code-gauntlet-measurement-abc1234.json").write_bytes(
            path.read_bytes() if trace_status == "readable" else b"{}"
        )
    discovery = next(s for s in trace.snapshots if s.stage == "discover")
    versions = {v.version_id: v for v in trace.versions}
    keep = next(
        v
        for v in discovery.finding_version_ids
        if versions[v].model_finding_id == "GROUP"
    )
    lost = next(
        v
        for v in discovery.finding_version_ids
        if versions[v].model_finding_id == "FILTER"
    )

    def scorer(self, pin, key, model):
        self.calls.append(model)
        golden = read_json(model.parent / "benchmark_data.json")[self.url][
            "golden_comments"
        ]
        candidates = read_json(model / "candidates.json")[self.url]
        evaluations = {}
        for tool, entries in candidates.items():
            hits = (
                [0]
                if tool == "deep-review" or tool == "discover:" + keep
                else [2]
                if tool == "discover:" + lost
                else []
            )
            evaluations[tool] = {
                "tp": len(hits),
                "fp": 0,
                "fn": len(golden) - len(hits),
                "errors": [],
                "true_positives": [
                    {
                        "golden_comment": golden[i]["comment"],
                        "matched_candidate": entries[0]["text"],
                        "confidence": 1.0,
                    }
                    for i in hits
                ],
                "false_negatives": [
                    {"golden_comment": g["comment"]}
                    for i, g in enumerate(golden)
                    if i not in hits
                ],
            }
        (model / "evaluations.json").write_text(
            json.dumps({self.url: evaluations}), encoding="utf-8"
        )
        (model / "dedup_groups.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(type(score_case), "scorer", scorer)
    packet = score_case.execute()
    loaded = observations.load_packet(Path(packet["ledger_row"]["packet_path"]))
    assert packet["profiles"]["all"]["tp"] == 1 and len(score_case.calls) == 1
    rows = stages.read_rows(loaded.root)
    funnel = stages.loss_funnel(
        rows, population={b.bug_id for b in loaded.observations}
    )
    if trace_status == "readable":
        assert dict(funnel.losses)["filter"] == 1
        dimension = versions[keep].discovery_dimension
        assert packet["metrics"]["per_dimension"][dimension]["delivered_bug_hits"] == 1
        assert (
            packet["metrics"]["per_dimension"][dimension]["discovery_recall"]
            is not None
        )
        credited = stages.credited_dimensions(
            loaded.observations[0], loaded.candidates, [versions[keep]]
        )
        assert credited == (dimension,)
        assert (
            stages.credited_dimensions(loaded.observations[0], loaded.candidates, [])
            == ()
        )
    else:
        assert funnel.unavailable_count == len(loaded.observations) - 1
        assert sum(dict(funnel.losses).values()) == 0
        assert all(r.survives is None for r in rows if r.stage == "discover")
