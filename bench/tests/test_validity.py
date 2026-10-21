"""Measurement spend boundaries and immutable two-strike outcomes."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bench import run
from bench.runner import checkpoint
from bench.runner import validity as v
from bench.runner.epochs import DEFAULT_ROOT, load_epoch, read_json
from bench.runner.invoke import InvokeResult

NOW = "2026-10-20T12:00:00Z"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(v, "utc_now", lambda: NOW)
    monkeypatch.setattr(v, "require_clean_treatment", lambda *args: None)


@pytest.fixture
def fingerprint(tmp_path):
    binary = tmp_path / "claude"
    binary.write_text("public binary fixture", encoding="utf-8")
    return v.capture_fingerprint(
        cli_path=binary,
        child_model="claude-session-1",
        auth_mode="api",
        epoch=load_epoch(root=DEFAULT_ROOT),
        judge_pin="judge",
        adjudicator_pin="adjudicator",
        env={"DISABLE_AUTOUPDATER": "1"},
        policy={},
        cli_probe=lambda p: "2.1.200",
        model_probe=lambda root, policy: {
            "code-gauntlet:bug-detector": "claude-agent-1"
        },
    )


@pytest.fixture
def window(fingerprint):
    return v.MeasurementWindow(
        1,
        "test-window",
        "2026-10-20T00:00:00Z",
        "2026-10-21T00:00:00Z",
        fingerprint.controls,
        tuple(read_json(DEFAULT_ROOT / "subsets.json")["gate"]),
        (0, 1, 2, 3),
        1,
        {a: fingerprint.treatment for a in ("baseline", "candidate")},
    )


@pytest.mark.parametrize("mode", ["new", "resume", "retry-failed"])
@pytest.mark.parametrize(
    "group,field",
    [
        (g, f)
        for g, cls in (("controls", v.SharedControls), ("treatment", v.Treatment))
        for f in cls.__dataclass_fields__
    ],
)
def test_run_main_refuses_identity_before_any_dispatch(
    tmp_path, monkeypatch, window, fingerprint, group, field, mode
):
    path = tmp_path / "window.json"
    v.exclusive_json(path, window.wire())
    monkeypatch.setattr(run, "RUNS_ROOT", tmp_path / "runs")
    args = [
        "--measurement-window",
        str(path),
        "--child-model",
        "claude-session-1",
        "--run-index",
        "0",
        "--arm",
        "baseline",
        "--tier",
        "subset",
    ]
    if mode != "new":
        directory = run.RUNS_ROOT / "prior"
        directory.mkdir(parents=True)
        v.exclusive_json(
            directory / "run.json",
            {
                "child_auth": "api",
                "pr_urls": list(window.pr_urls),
                "measurement_window": str(path),
                "provenance": fingerprint.wire(),
                "child_model": "claude-session-1",
                "arm": "baseline",
                "run_index": 0,
            },
        )
        args += ["--" + mode, "prior"]
    record = getattr(fingerprint, group)
    value = getattr(record, field)
    changed = (
        {"changed": "claude-changed"}
        if isinstance(value, dict)
        else ("changed",)
        if isinstance(value, tuple)
        else False
        if isinstance(value, bool)
        else "subscription"
        if field == "auth_mode"
        else "changed"
    )
    monkeypatch.setattr(
        v,
        "capture_fingerprint",
        lambda **kwargs: replace(
            fingerprint, **{group: replace(record, **{field: changed})}
        ),
    )
    monkeypatch.setattr(
        run, "check_prereqs", lambda **kwargs: pytest.fail("spent before preflight")
    )
    assert run.main(args) == 2


def test_each_arm_can_remove_an_agent_but_cannot_change_undeclared_treatment(
    window, fingerprint
):
    candidate = replace(
        fingerprint,
        treatment=replace(
            fingerprint.treatment,
            treatment_git_sha="candidate",
            resolved_models={"orchestrator": "claude-session-1"},
            agent_efforts={"orchestrator": "high"},
        ),
    )
    window = replace(
        window,
        arms={"baseline": fingerprint.treatment, "candidate": candidate.treatment},
    )
    assert v.check_window(window, fingerprint, arm="baseline", now=NOW).state == "valid"
    assert v.check_window(window, candidate, arm="candidate", now=NOW).state == "valid"
    assert v.check_window(window, candidate, arm="baseline", now=NOW).state == "invalid"
    assert v.window_from_wire(json.loads(json.dumps(window.wire()))) == window
    with pytest.raises(ValueError):
        v.window_from_wire({**window.wire(), "max_parallel_prs": 2})


@pytest.mark.parametrize("auth", ["api", "subscription"])
@pytest.mark.parametrize("second", ["ok", "timeout", "invalid", "failed", "drifted"])
def test_attempt_collection_two_strikes_preserves_first_bytes(
    tmp_path, monkeypatch, fingerprint, auth, second
):
    fp = replace(fingerprint, controls=replace(fingerprint.controls, auth_mode=auth))
    monkeypatch.setattr(v, "recheck_cli", lambda fp: None)
    directory = tmp_path / "run"
    pr = directory / "pr-1"
    pr.mkdir(parents=True)
    payload = pr / "post-review-payload.json"
    payload.write_text("{}", encoding="utf-8")
    manifest = {
        "provenance": fp.wire(),
        "child_auth": auth,
        "window_id": "window",
        "anchor": "naive",
    }
    monkeypatch.setattr("bench.runner.check.check_attempt", lambda *a, **k: ())
    first = v.collect_attempt(
        directory,
        pr,
        url="u",
        result=InvokeResult("failed", reason="first cause"),
        started_at="2026-10-20T10:00:00Z",
        manifest=manifest,
    )
    assert first.validity.state == "invalid" and first.validity.retryable
    original = next(directory.glob("attempts/*/*/attempt.json"))
    original_bytes = original.read_bytes()
    raw = pr / "raw.json"
    raw.write_text(
        json.dumps({"type": "result", "modelUsage": {"claude-session-1": {}}}),
        encoding="utf-8",
    )
    if auth == "subscription":
        with pytest.raises(ValueError, match="reset_not_confirmed"):
            v.check_retry(first, now="2026-10-20T14:00:00Z")
    monkeypatch.setattr(v, "utc_now", lambda: "2026-10-20T15:00:00Z")
    result = InvokeResult(
        second,
        reason="different cause",
        payload_path=str(payload),
        raw_json_path=str(raw),
    )
    retry = v.collect_attempt(
        directory,
        pr,
        url="u",
        result=result,
        started_at="2026-10-20T14:00:00Z",
        manifest=manifest,
        prior=first,
        reset_at="2026-10-20T13:00:00Z" if auth == "subscription" else None,
    )
    assert retry.validity.state == (
        "valid" if second == "ok" else "operational_failure"
    )
    assert retry.validity.retry_of == first.attempt_id
    assert original.read_bytes() == original_bytes
    wire_path = next(directory.glob(f"attempts/*/{retry.attempt_id}/attempt.json"))
    assert v.load_attempt(wire_path).validity == retry.validity
    cp = checkpoint.Checkpoint(directory)
    cp.mark("u", first.checkpoint_status, {"validity": first.validity.wire()})
    assert cp.failed(["u"]) == ["u"]


@pytest.mark.parametrize(
    "change",
    [
        "reset_future",
        "reset_before_failure",
        "different_pr",
        "different_window",
        "different_auth",
    ],
)
def test_unrelated_or_unreset_retry_cannot_become_operational_failure(
    tmp_path, monkeypatch, fingerprint, change
):
    monkeypatch.setattr(v, "utc_now", lambda: "2026-10-20T15:00:00Z")
    fp = replace(
        fingerprint, controls=replace(fingerprint.controls, auth_mode="subscription")
    )
    directory, pr = tmp_path / "run", tmp_path / "run/pr-1"
    manifest = {"provenance": fp.wire(), "child_auth": "subscription", "window_id": "w"}
    first = v.collect_attempt(
        directory,
        pr,
        url="u",
        result=InvokeResult("timeout"),
        started_at="2026-10-20T10:00:00Z",
        manifest=manifest,
    )
    reset = (
        "2026-10-20T17:00:00Z"
        if change == "reset_future"
        else "2026-10-20T11:00:00Z"
        if change == "reset_before_failure"
        else "2026-10-20T15:30:00Z"
    )
    if change == "different_window":
        manifest["window_id"] = "other"
    if change == "different_auth":
        manifest["provenance"] = fingerprint.wire()
    monkeypatch.setattr(v, "utc_now", lambda: "2026-10-20T17:00:00Z")
    retry = v.collect_attempt(
        directory,
        pr,
        url="other" if change == "different_pr" else "u",
        result=InvokeResult("failed"),
        started_at="2026-10-20T16:00:00Z",
        manifest=manifest,
        prior=first,
        reset_at=reset,
    )
    assert retry.validity.state == "invalid" and retry.validity.retryable


def test_boundary_decoder_and_binary_pin_reject_wrong_inputs(fingerprint):
    assert v.fingerprint_from_wire(fingerprint.wire()) == fingerprint
    for group, field, wrong in [
        ("controls", "auth_mode", "other"),
        ("controls", "cli_version", 42),
        ("controls", "auto_update_disabled", False),
        ("treatment", "resolved_models", {"a": "inherit"}),
    ]:
        with pytest.raises(ValueError):
            v.fingerprint_from_wire(
                {
                    **fingerprint.wire(),
                    group: {**fingerprint.wire()[group], field: wrong},
                }
            )
    Path(fingerprint.controls.cli_realpath).write_text("replacement", encoding="utf-8")
    with pytest.raises(ValueError, match="cli_mismatch"):
        v.recheck_cli(fingerprint, probe=lambda p: fingerprint.controls.cli_version)


def test_recurrence_can_link_independent_runs_in_the_same_window(
    tmp_path, fingerprint, monkeypatch
):
    manifest = {"provenance": fingerprint.wire(), "window_id": "w"}
    first_dir, retry_dir = tmp_path / "first", tmp_path / "retry"
    first = v.collect_attempt(
        first_dir,
        first_dir / "pr",
        url="u",
        result=InvokeResult("failed"),
        started_at="2026-10-20T10:00:00Z",
        manifest=manifest,
    )
    with pytest.raises(ValueError, match="first completed attempt"):
        v.check_retry(first, now="2026-10-20T09:00:00Z")
    monkeypatch.setattr(v, "utc_now", lambda: "2026-10-20T15:00:00Z")
    retry = v.collect_attempt(
        retry_dir,
        retry_dir / "pr",
        url="u",
        result=InvokeResult("timeout"),
        started_at="2026-10-20T14:00:00Z",
        manifest=manifest,
        prior=first,
    )
    assert retry.validity.state == "operational_failure"
    assert v.load_attempt(next(retry_dir.glob("attempts/*/*/attempt.json"))) == retry


@pytest.mark.parametrize("second_status", ["ok", "failed"])
def test_scoring_selects_immutable_retry_and_retains_failed_consumption(
    score_case, fingerprint, monkeypatch, second_status
):
    from bench.runner import observations, score

    monkeypatch.setattr("bench.runner.check.check_attempt", lambda *a, **k: ())
    directory = score_case.run
    pr = score_case.payload.parent
    manifest = {
        "run_id": directory.name,
        "urls": [score_case.url],
        "provenance": fingerprint.wire(),
        "window_id": "window",
    }
    score._write_json(directory / "run.json", manifest)
    raw = pr / "raw.json"
    score._write_json(
        raw,
        {"total_cost_usd": 1, "modelUsage": {"claude-session-1": {}}},
    )
    first = v.collect_attempt(
        directory,
        pr,
        url=score_case.url,
        result=InvokeResult("failed", raw_json_path=str(raw), cost_usd=1),
        started_at="2026-10-20T10:00:00Z",
        manifest=manifest,
    )
    score._write_json(
        raw,
        {"total_cost_usd": 2, "modelUsage": {"claude-session-1": {}}},
    )
    selected = v.collect_attempt(
        directory,
        pr,
        url=score_case.url,
        result=InvokeResult(
            second_status,
            payload_path=str(score_case.payload),
            raw_json_path=str(raw),
            cost_usd=2,
        ),
        started_at=NOW,
        manifest=manifest,
        prior=first,
    )
    score._write_json(
        directory / "state/a.json",
        {
            "url": score_case.url,
            "status": second_status,
            "detail": {"attempt_id": selected.attempt_id},
        },
    )
    # Current deliverables may change; a score must read the selected snapshot.
    score._write_json(score_case.payload, {"payload": {"comments": []}})
    result = score_case.execute()
    packet = observations.load_packet(Path(result["ledger_row"]["packet_path"]))
    assert packet.manifest.selected_attempts == {score_case.url: selected.attempt_id}
    assert packet.metadata["review_attempt_costs"] == {
        first.attempt_id: 1,
        selected.attempt_id: 2,
    }
    assert (
        v.attempt_from_wire(
            read_json(packet.root / "attempts" / (selected.attempt_id + ".json"))
        )
        == selected
    )
    outcomes = {b.outcome for b in packet.observations}
    if second_status == "ok":
        assert result["metrics"]["tp"] == 2
        assert outcomes == {"matched", "missed"}
        assert len(packet.candidates) == 3
    else:
        assert result["metrics"]["tp"] == 0
        assert outcomes == {"operational_failure"}
        assert not packet.candidates


@pytest.mark.parametrize("defect", ["invalid", "missing", "forged", "payload", "cost"])
def test_scoring_refuses_bad_attempt_evidence_before_judging(
    score_case, monkeypatch, defect
):
    from bench.runner import score

    directory, pr = score_case.run, score_case.payload.parent
    attempt = v.collect_attempt(
        directory,
        pr,
        url=score_case.url,
        result=InvokeResult(
            "failed" if defect == "invalid" else "ok",
            payload_path=str(score_case.payload),
        ),
        started_at=NOW,
        manifest={"run_id": directory.name},
    )
    score._write_json(
        directory / "state/a.json",
        {
            "url": score_case.url,
            "status": attempt.checkpoint_status,
            "detail": {"attempt_id": attempt.attempt_id},
        },
    )
    selection = v.scoring_selection(directory)
    if defect == "missing":
        selection = None
    elif defect == "forged":
        selection[score_case.url] = replace(attempt, run_id="other")
    elif defect == "payload":
        path = next(directory.glob("attempts/*/*/artifacts/post-review-payload.json"))
        path.write_text("changed", encoding="utf-8")
    elif defect == "cost":
        path = next(directory.glob("attempts/*/*/artifacts/attempt-result.json"))
        path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        score.score_run(
            directory,
            env={},
            attempt_selection=selection,
            run_scorer=lambda *a: pytest.fail("judge ran before evidence validation"),
        )
    assert not score_case.ledger.exists()
