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
        env={},
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
        fingerprint.controls(),
        tuple(read_json(DEFAULT_ROOT / "subsets.json")["gate"]),
        (0, 1, 2, 3),
        1,
        {a: fingerprint.treatment() for a in ("baseline", "candidate")},
    )


@pytest.mark.parametrize("mode", ["new", "resume", "retry-failed"])
@pytest.mark.parametrize("field", list(v.RunFingerprint.__dataclass_fields__))
def test_run_main_refuses_identity_before_any_dispatch(
    tmp_path, monkeypatch, window, fingerprint, field, mode
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
            },
        )
        args += ["--" + mode, "prior"]
    value = getattr(fingerprint, field)
    changed = (
        (("changed", "claude-changed"),)
        if field == "resolved_models"
        else (("changed", None),)
        if field == "effort_env"
        else ("changed",)
        if isinstance(value, tuple)
        else "subscription"
        if field == "auth_mode"
        else "changed"
    )
    monkeypatch.setattr(
        v,
        "capture_fingerprint",
        lambda **kwargs: replace(fingerprint, **{field: changed}),
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
        treatment_git_sha="candidate",
        resolved_models=(("orchestrator", "claude-session-1"),),
        agent_efforts=(("orchestrator", "high"),),
    )
    window = replace(
        window,
        arms={"baseline": fingerprint.treatment(), "candidate": candidate.treatment()},
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
    fp = replace(fingerprint, auth_mode=auth)
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
    fp = replace(fingerprint, auth_mode="subscription")
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
    for field, wrong in [
        ("auth_mode", "other"),
        ("cli_version", 42),
        ("resolved_models", {"a": "inherit"}),
    ]:
        with pytest.raises(ValueError):
            v.fingerprint_from_wire({**fingerprint.wire(), field: wrong})
    Path(fingerprint.cli_realpath).write_text("replacement", encoding="utf-8")
    with pytest.raises(ValueError, match="cli_mismatch"):
        v.recheck_cli(fingerprint, probe=lambda p: fingerprint.cli_version)


def test_complete_matrix_requires_every_arm_pr_cell_and_zero_failure_hits(
    window, fingerprint
):
    from types import SimpleNamespace

    from bench.runner.observations import ScoreRef

    window = replace(window, pr_urls=("u", "v"), planned_run_indices=(0,))
    epoch = SimpleNamespace(
        bugs=tuple(SimpleNamespace(pr_url=p, bug_id=p) for p in window.pr_urls)
    )
    cells = []
    for arm in window.arms:
        for pr in window.pr_urls:
            aid, rid = arm + pr, "run-" + arm
            attempt = v.AttemptRecord(
                1,
                aid,
                rid,
                pr,
                NOW,
                NOW,
                "ok",
                None,
                v.Validity("valid", window_id=window.window_id, observed=fingerprint),
                (),
                1,
                (),
                (),
            )
            row = v.MatrixObservation(arm, 0, pr, pr, True, rid, aid)
            cells.append(
                v.CellSelection(
                    arm,
                    0,
                    pr,
                    aid,
                    ScoreRef(rid, "epoch", "score"),
                    attempt=attempt,
                    observations=(row,),
                )
            )
    assert (
        len(
            v.validate_matrix(
                cells, window, (), epoch=epoch, expected_run_indices=(0,)
            ).observations
        )
        == 4
    )
    for broken in (cells[:-1], [*cells, cells[0]]):
        with pytest.raises(ValueError, match="PR/run matrix"):
            v.validate_matrix(
                broken, window, (), epoch=epoch, expected_run_indices=(0,)
            )
    failure = replace(
        cells[0],
        failure_confirmation_ref="first",
        attempt=replace(
            cells[0].attempt,
            validity=replace(cells[0].attempt.validity, state="operational_failure"),
        ),
    )
    with pytest.raises(ValueError, match="nonzero operational failure"):
        v.validate_matrix(
            [failure, *cells[1:]], window, (), epoch=epoch, expected_run_indices=(0,)
        )


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
