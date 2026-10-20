"""Workflow evidence survives collection, decoding and caller-level funnel scoring."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from bench.runner import observations, stages
from bench.runner.epochs import DEFAULT_ROOT, GoldenBug, digest, file_hash, load_epoch

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
    # Every persistence mode produces byte-identical evidence for the same fixed attempt.
    other = tmp_path / "other"
    other.mkdir()
    _, reference, _ = emit_trace(other)
    assert path.read_bytes() == reference.read_bytes()


def loss_case(tmp_path):
    trace, _, receipt = emit_trace(tmp_path)
    epoch = load_epoch(root=DEFAULT_ROOT)
    url = "https://github.com/synthetic/fixture/pull/1"
    markers = [
        "merge loss describes",
        "finding VERIFY",
        "finding FILTER",
        "finding REMOVE",
        "finding CAP",
        "finding SKIP",
        "finding DELIVERY",
        "shared group defect",
        "never emitted marker",
    ]
    golden = tuple(
        GoldenBug(f"g:{i}", url, i, marker, "high", "bug", digest(marker))
        for i, marker in enumerate(markers)
    )
    epoch = replace(epoch, bugs=golden)
    ref = observations.ScoreRef("synthetic", epoch.epoch_id, "s")
    bugs = tuple(
        observations.BugObservation(
            1,
            ref,
            url,
            "attempt",
            b.bug_id,
            "bug",
            "matched" if i == 7 else "missed",
            (),
            (),
            None,
        )
        for i, b in enumerate(golden)
    )
    return trace, epoch, bugs, receipt


def test_known_loss_funnel_uses_trusted_transitions_and_preserves_coalesced_hit(
    tmp_path,
):
    trace, epoch, bugs, _ = loss_case(tmp_path)
    calls = []

    def match(golden, claim):
        calls.append((golden, claim))
        return {
            "match": golden in claim,
            "confidence": 1,
            "reasoning": "fixed synthetic pair",
            "actual_cost_usd": 0.001,
        }

    rows = stages.evaluate_stages(
        trace,
        epoch,
        delivered_observations=bugs,
        matcher=match,
        stage_cost_cap_usd=10,
        price_estimator=lambda *args: 0.002,
    )
    final = [r for r in rows if r.stage == "deliver"]
    assert [r.loss.stage if r.loss else "hit" for r in final] == [
        "merge",
        "verify",
        "filter",
        "challenge",
        "cap",
        "validate",
        "cap",
        "hit",
        "not_found",
    ]
    assert final[2].loss.cause_stage == "validate"
    assert final[5].loss.physical_stage == "holdback"
    assert "trusted executor disproved" in final[1].loss.reason
    assert not any(golden == "shared group defect" for golden, _ in calls)
    assert len(calls) == len(set(calls))
    funnel = stages.loss_funnel(rows, population=[b.bug_id for b in bugs])
    assert funnel.denominator == 9
    assert funnel.delivered_hits == 1
    assert sum(dict(funnel.losses).values()) == 8
    assert funnel.unavailable_count == 0
    assert dict(funnel.unjudged_counts)["discover"] == 1
    repeated = stages.loss_funnel(
        rows
        + tuple(
            replace(r, score_ref=replace(r.score_ref, score_id="repeat")) for r in rows
        ),
        population=[b.bug_id for b in bugs],
    )
    assert repeated.denominator == 18
    assert repeated.delivered_hits == 2


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


@pytest.mark.parametrize("cap", [None, -1, float("inf"), float("nan"), True])
def test_score_boundary_rejects_invalid_stage_cap_before_delivered_dispatch(
    score_case, cap
):
    with pytest.raises(ValueError, match="finite nonnegative"):
        score_case.execute(capture_stages=True, stage_cost_cap_usd=cap)
    assert score_case.calls == []


def test_score_default_off_and_missing_trace_preserve_delivered_scores(score_case):
    calls = []
    first = score_case.execute(stage_matcher=lambda *args: calls.append(args))
    second = score_case.execute(
        capture_stages=True,
        stage_cost_cap_usd=0,
        stage_matcher=lambda *args: calls.append(args),
    )
    assert not calls
    assert first["stage_capture"]["status"] == "off"
    assert second["stage_capture"]["status"] == "unavailable"
    assert first["profiles"] == second["profiles"]
    loaded = observations.load_packet(Path(second["ledger_row"]["packet_path"]))
    assert loaded.stage_capture.enabled


def test_estimate_refusal_and_retry_reservations_bound_entire_score(tmp_path):
    trace, epoch, bugs, _ = loss_case(tmp_path)
    calls = []

    def failing(*pair):
        calls.append(pair)
        return {"match": None, "error": "synthetic transient"}

    failing.max_retries = 1
    args = dict(
        traces={bugs[0].pr_url: (tmp_path / "trace-return.json", trace)},
        bugs=bugs,
        epoch=epoch,
        enabled=True,
        matcher=failing,
        price_estimator=lambda *args: 1.0,
        errors={},
        evaluator_id="e",
        pin="p",
        api_key=None,
        prices=None,
    )
    _, _, refused, journal = stages.capture_score(**args, cap=0)
    assert refused["status"] == "refused"
    assert refused["pairwise_call_count"] > 0
    assert calls == [] and not journal
    _, _, capped, journal = stages.capture_score(
        **args, cap=float(refused["pairwise_call_count"])
    )
    assert capped["status"] == "capped"
    assert capped["actual_call_count"] == len(calls) == len(journal)
    assert sum(j["reserved_max_usd"] for j in journal) <= refused["pairwise_call_count"]
    assert capped["actual_cost_usd"] is None
    assert any(j["retry"] == 1 for j in journal)


def test_collection_binds_return_bytes_and_ignores_superseded_records(tmp_path):
    trace, path, receipt = emit_trace(tmp_path)
    pr = tmp_path / "pr"
    (pr / "workflows/superseded").mkdir(parents=True)
    record = {"result": receipt["out"]}
    (pr / "workflows/wf_current.json").write_text(json.dumps(record), encoding="utf-8")
    (pr / "workflows/superseded/wf_old.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    binding = stages.collect_trace(pr)
    assert binding["run_nonce"] == trace.run_nonce
    collected = pr / binding["path"]
    assert collected.read_bytes() == path.read_bytes()
    assert binding["sha256"] == file_hash(collected)
    assert stages.collect_trace(pr) == binding
    collected.write_text("corrupt", encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        stages.collect_trace(pr)


def test_score_packet_carries_source_attribution_and_bounded_stage_matches(
    score_case, tmp_path
):
    from bench.tests.test_provenance import captured_diff, payload_for

    trace, path, receipt = emit_trace(tmp_path)
    pr = score_case.payload.parent
    (pr / "code-gauntlet-measurement-abc1234.json").write_bytes(path.read_bytes())
    selected = receipt["payload"]["postReview"]["findings"]
    (pr / "code-gauntlet-post-review-abc1234.json").write_text(
        json.dumps(receipt["payload"]["postReview"]), encoding="utf-8"
    )
    diff = captured_diff(selected)
    (pr / "diff.patch").write_text(diff, encoding="utf-8")
    score_case.payload.write_text(
        json.dumps(payload_for(selected, diff, "github")), encoding="utf-8"
    )
    calls = []

    def matcher(*pair):
        calls.append(pair)
        return {
            "match": False,
            "confidence": 1.0,
            "reasoning": "fixed negative",
            "actual_cost_usd": 0.00001,
        }

    packet = score_case.execute(
        capture_stages=True,
        stage_cost_cap_usd=10,
        stage_matcher=matcher,
        stage_price_estimator=lambda *pair: 0.00002,
    )
    assert packet["stage_capture"]["status"] == "complete"
    assert packet["stage_capture"]["actual_call_count"] == len(calls) > 0
    assert packet["manifest"]["provenance_status"] == "complete"
    packet_dir = Path(packet["ledger_row"]["packet_path"])
    loaded = observations.load_packet(packet_dir)
    assert len(loaded.candidates) == 1
    assert set(loaded.candidates[0].discovery_dimensions) == {"bug", "intent"}
    assert len(loaded.candidates[0].source_trace_ids) == 2
    assert set(packet["metrics"]["per_dimension"]) == {"bug", "intent"}
    assert all(
        m["discovery_recall"] is None
        for m in packet["metrics"]["per_dimension"].values()
    )
    assert (packet_dir / "stage_matches.jsonl").stat().st_size > 0
    assert len(
        json.loads((packet_dir / "stage_calls.json").read_text(encoding="utf-8"))
    ) == len(calls)
    raw_rows = [
        json.loads(line)
        for line in (packet_dir / "stages.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(raw_rows) == len(loaded.observations) * 9
    matched_bugs = {b.bug_id for b in loaded.observations if b.outcome == "matched"}
    assert not any(
        json.loads(line)["bug_id"] in matched_bugs
        for line in (packet_dir / "stage_matches.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    old_hashes = {
        p.relative_to(packet_dir): file_hash(p)
        for p in packet_dir.rglob("*")
        if p.is_file()
    }
    calls.clear()
    refused = score_case.execute(
        capture_stages=True,
        stage_cost_cap_usd=0,
        stage_matcher=matcher,
        stage_price_estimator=lambda *pair: 0.00002,
    )
    assert refused["stage_capture"]["status"] == "refused"
    assert not calls
    assert packet["profiles"] == refused["profiles"]
    assert old_hashes == {
        p.relative_to(packet_dir): file_hash(p)
        for p in packet_dir.rglob("*")
        if p.is_file()
    }


def test_absent_population_rows_remain_unavailable_in_fixed_denominator():
    report = stages.loss_funnel((), population=["g:missing"])
    assert report.denominator == 1
    assert report.unavailable_count == 1
    assert dict(report.unjudged_counts)["discover"] == 1


def test_delivered_hits_and_missing_prices_dispatch_no_stage_pairs(tmp_path):
    trace, epoch, bugs, _ = loss_case(tmp_path)
    calls = []
    hits = tuple(replace(bug, outcome="matched") for bug in bugs)
    rows, matches, capture, journal = stages.capture_score(
        {bugs[0].pr_url: (tmp_path / "trace-return.json", trace)},
        hits,
        epoch,
        enabled=True,
        cap=0,
        matcher=lambda *pair: calls.append(pair),
        price_estimator=None,
        errors={},
        evaluator_id="e",
        pin="p",
        api_key=None,
        prices=None,
    )
    assert capture["pairwise_call_count"] == 0
    assert not calls and not journal and not matches
    assert all(r.survives for r in rows if r.stage == "deliver")


def test_trace_loss_reappearance_uses_the_last_matching_transition(tmp_path):
    trace, epoch, bugs, _ = loss_case(tmp_path)
    # A matcher inconsistency at verify cannot override a later established filter survivor.
    snapshots = tuple(
        replace(s, finding_version_ids=()) if s.stage == "verify" else s
        for s in trace.snapshots
    )
    trace = replace(trace, snapshots=snapshots)
    bug = next(
        b
        for b in bugs
        if "DELIVERY" in next(g.comment for g in epoch.bugs if g.bug_id == b.bug_id)
    )
    rows = stages.evaluate_stages(
        trace,
        epoch,
        delivered_observations=(bug,),
        matcher=lambda golden, claim: {"match": golden in claim},
        stage_cost_cap_usd=1,
        price_estimator=lambda *pair: 0,
    )
    loss = next(r.loss for r in rows if r.stage == "deliver")
    assert loss.stage == "cap" and loss.physical_stage == "select"


@pytest.mark.parametrize("mode", ["legacy", "derived", "return"])
def test_real_materializer_writes_separate_attempt_bound_trace(tmp_path, mode):
    from gauntlet import materialize, tasks

    trace, original, receipt = emit_trace(tmp_path, mode)
    output = Path(receipt["out"]["measurement"]["path"]).parent
    output.mkdir()
    task = tmp_path / "task.output"
    task.write_text(json.dumps({"result": receipt["out"]}), encoding="utf-8")
    materialized = materialize.materialize(
        str(task), trace.run_nonce, str(output), tasks.TaskRoots((str(tmp_path),), None)
    )
    assert materialized["ok"], materialized["errors"]
    artifact = output / "code-gauntlet-measurement-abc1234.json"
    assert artifact.read_bytes() == original.read_bytes()
    assert stages.load_trace(artifact, expected_sha256=receipt["sha256"]) == trace
    if mode == "return":
        checkpoint = json.loads(
            (output / "code-gauntlet-checkpoint-all-abc1234.json").read_text(
                encoding="utf-8"
            )
        )
        assert "measurement" not in checkpoint
        assert set(checkpoint["phases"]) == {"challenge"}
    else:
        assert materialized["assemble"] is None
        assert len(materialized["materialized"]) == 1


def test_discovery_cap_is_censored_coverage_and_never_a_particular_bug_loss(tmp_path):
    trace, epoch, bugs, _ = loss_case(tmp_path)
    trace = replace(
        trace,
        snapshots=tuple(
            replace(s, coverage="partial", cap_hit=True) if s.stage == "discover" else s
            for s in trace.snapshots
        ),
    )
    bug = bugs[-1]
    rows = stages.evaluate_stages(
        trace,
        epoch,
        delivered_observations=(bug,),
        matcher=lambda *pair: {"match": False},
        stage_cost_cap_usd=0,
        price_estimator=lambda *pair: 0,
    )
    final = next(r for r in rows if r.stage == "deliver")
    assert final.loss.stage == "not_found"
    assert "coverage partial" in final.loss.reason
    assert stages.loss_funnel(rows, population=[bug.bug_id]).coverage == "partial"


def test_corrupt_optional_trace_is_a_provenance_error_and_preserves_delivered_recall(
    score_case,
):
    first = score_case.execute()
    (score_case.payload.parent / "code-gauntlet-measurement-abc1234.json").write_text(
        "[]", encoding="utf-8"
    )
    second = score_case.execute(capture_stages=True, stage_cost_cap_usd=0)
    assert second["profiles"] == first["profiles"]
    assert second["stage_capture"]["status"] == "unavailable"
    assert second["manifest"]["provenance_status"] == "ambiguous"
    path = Path(second["ledger_row"]["packet_path"])
    assert score_case.url in json.loads(
        (path / "provenance_errors.json").read_text(encoding="utf-8")
    )
    assert (
        observations.load_packet(path).metrics.golden_recall
        == second["metrics"]["golden_recall"]
    )
