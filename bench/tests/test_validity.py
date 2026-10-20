"""The runner's spend boundary and immutable retry evidence enforce validity."""

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from bench import run
from bench.runner import checkpoint
from bench.runner import validity as v
from bench.runner.epochs import DEFAULT_ROOT, load_epoch, read_json
from bench.runner.invoke import InvokeResult
from bench.tests.test_harness_boundaries import harness as harness

NOW = "2026-10-20T12:00:00Z"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(v, "utc_now", lambda: NOW)


@pytest.fixture
def fingerprint(tmp_path):
    binary = tmp_path / "claude"
    binary.write_text("public binary fixture", encoding="utf-8")
    epoch = load_epoch(root=DEFAULT_ROOT)
    return v.capture_fingerprint(
        cli_path=binary,
        child_model="claude-session-1",
        auth_mode="api",
        epoch=epoch,
        judge_pin="judge",
        adjudicator_pin="adjudicator",
        env={},
        policy={},
        cli_probe=lambda path: "2.1.200",
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
        fingerprint,
        tuple(read_json(DEFAULT_ROOT / "subsets.json")["gate"]),
        (0, 1, 2, 3),
        1,
    )


@pytest.mark.parametrize("field", list(v.RunFingerprint.__dataclass_fields__))
def test_check_window_reports_every_fingerprint_mismatch(window, field):
    value = getattr(window.expected, field)
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
    result = v.check_window(
        window, replace(window.expected, **{field: changed}), now=NOW
    )
    assert result.state == "invalid" and result.reasons
    assert v.LIMITATIONS[0] in result.limitations


@pytest.mark.parametrize("mode", ["new", "resume", "retry-failed"])
@pytest.mark.parametrize("anchor", [None, "naive"])
@pytest.mark.parametrize("field", list(v.RunFingerprint.__dataclass_fields__))
def test_run_main_refuses_identity_before_any_dispatch(
    tmp_path, monkeypatch, window, field, mode, anchor
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
    if anchor:
        args += ["--anchor", anchor]
    if mode != "new":
        directory = run.RUNS_ROOT / "prior"
        directory.mkdir(parents=True)
        v.exclusive_json(
            directory / "run.json",
            {
                "child_auth": "api",
                "pr_urls": list(window.pr_urls),
                "measurement_window": str(path),
                "provenance": window.expected.wire(),
                "child_model": "claude-session-1",
            },
        )
        args += ["--" + mode, "prior"]
    value = getattr(window.expected, field)
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
    observed = replace(window.expected, **{field: changed})
    monkeypatch.setattr(v, "capture_fingerprint", lambda **kwargs: observed)
    monkeypatch.setattr(
        run, "check_prereqs", lambda **kwargs: pytest.fail("preflight was bypassed")
    )
    monkeypatch.setattr(
        run, "_new_run", lambda *args: pytest.fail("new run dispatched")
    )
    monkeypatch.setattr(
        run, "_resume", lambda *args, **kwargs: pytest.fail("resume dispatched")
    )
    assert run.main(args) == 2


def test_effort_absent_empty_and_noncredential_fingerprint(fingerprint):
    wire = fingerprint.wire()
    assert wire["effort_env"]["CLAUDE_CODE_EFFORT_LEVEL"] is None
    assert "ANTHROPIC_API_KEY" not in json.dumps(wire)
    changed = replace(
        fingerprint,
        effort_env=tuple(
            (k, "" if k == "CLAUDE_CODE_EFFORT_LEVEL" else val)
            for k, val in fingerprint.effort_env
        ),
    )
    assert fingerprint != changed
    with pytest.raises(FrozenInstanceError):
        fingerprint.cli_version = "changed"
    result = InvokeResult("ok", per_model={"model": {"tokens": 1, "cost_usd": 2.0}})
    assert result.model_usage_wire() == {"model": {"tokens": 1, "cost_usd": 2.0}}
    with pytest.raises(FrozenInstanceError):
        result.per_model["model"].tokens = 2


def test_binary_replacement_cannot_spend_next_child(fingerprint):
    v.recheck_cli(fingerprint, probe=lambda path: "2.1.200")
    Path(fingerprint.cli_realpath).write_text("replacement", encoding="utf-8")
    with pytest.raises(ValueError, match="cli_mismatch"):
        v.recheck_cli(fingerprint, probe=lambda path: "2.1.200")


@pytest.mark.parametrize("status", ["failed", "timeout", "invalid"])
def test_subscription_first_nonok_is_unscored_and_needs_explicit_reset(
    tmp_path, fingerprint, status
):
    result = InvokeResult(
        status, reason="fast exit" if status == "failed" else "watchdog_timeout"
    )
    value = v.classify_attempt(result, auth_mode="subscription")
    assert (
        value.state == "invalid"
        and value.reasons == ("subscription_stall_unknown",)
        and value.retryable
    )
    original = v.AttemptRecord(
        1,
        "a",
        "run",
        "pr",
        NOW,
        NOW,
        status,
        result.reason,
        replace(value, observed=replace(fingerprint, auth_mode="subscription")),
        (),
        1,
        (),
        (),
    )
    with pytest.raises(ValueError, match="reset_not_confirmed"):
        v.check_retry(original, now="2026-10-20T14:00:00Z")
    evidence = tmp_path / "public-reset.json"
    evidence.write_text('{"reset":true}', encoding="utf-8")
    assert v.check_retry(
        original,
        now="2026-10-20T14:00:00Z",
        reset_confirmed_at="2026-10-20T13:00:00Z",
        reset_evidence=evidence,
    )
    with pytest.raises(ValueError, match="reset_not_confirmed"):
        v.check_retry(
            original,
            now=NOW,
            reset_confirmed_at="2026-10-20T13:00:00Z",
            reset_evidence=evidence,
        )


def test_quota_recognition_is_disabled_and_only_injected_trusted_schema_can_recognize(
    tmp_path,
):
    result = InvokeResult("failed", reason="limit 429 quoted in prompt")
    positive = {
        "source": "child_result",
        "authenticated": True,
        "signature": "synthetic-v1",
        "quota_state": "exhausted",
        "reset_at": "2026-10-20T13:00:00Z",
        "source_file_sha256": "synthetic",
        "offset": 3,
    }
    assert v.POSITIVE_SIGNATURE_REGISTRY == ()
    assert v.classify_attempt(
        result, auth_mode="subscription", trusted_events=[positive]
    ).reasons == ("subscription_stall_unknown",)
    value = v.classify_attempt(
        result,
        auth_mode="subscription",
        trusted_events=[positive],
        signature_registry=("synthetic-v1",),
    )
    assert (
        value.reasons == ("usage_limit",)
        and value.retry_not_before == positive["reset_at"]
    )
    for source in ("prompt", "tool_result", "evaluator"):
        assert v.classify_attempt(
            result,
            auth_mode="subscription",
            trusted_events=[{**positive, "source": source}],
            signature_registry=("synthetic-v1",),
        ).reasons != ("usage_limit",)


def test_api_transient_requires_same_fingerprint_on_independent_confirmation(
    fingerprint,
):
    result = InvokeResult("failed", reason="error")
    event = {
        "source": "child_result",
        "authenticated": True,
        "code": "overloaded",
        "phase": "review",
        "diagnostic_sha256": "same",
    }
    value = v.classify_attempt(result, auth_mode="api", trusted_events=[event])
    assert value.reasons == ("provider_transient",) and value.state == "invalid"
    original = v.AttemptRecord(
        1,
        "a",
        "run",
        "pr",
        NOW,
        NOW,
        "failed",
        "error",
        replace(value, observed=fingerprint),
        (),
        1,
        (),
        (),
    )
    retry = replace(
        original,
        attempt_id="b",
        sequence=2,
        validity=replace(value, observed=fingerprint, retry_of="a"),
    )
    assert (
        v.confirm_failure(original, retry, reset_evidence=None).attempt.validity.state
        == "operational_failure"
    )
    changed = replace(
        retry, validity=replace(retry.validity, failure_fingerprint="changed")
    )
    with pytest.raises(ValueError, match="changed"):
        v.confirm_failure(original, changed, reset_evidence=None)
    success = replace(
        retry,
        checkpoint_status="ok",
        validity=replace(retry.validity, state="valid", retryable=False),
    )
    assert v.confirm_failure(original, success, reset_evidence=None).attempt == success
    quota = replace(retry, validity=replace(retry.validity, reasons=("usage_limit",)))
    with pytest.raises(ValueError, match="quota"):
        v.confirm_failure(original, quota, reset_evidence="public")
    assert v.classify_attempt(
        InvokeResult("failed", reason="quoted overloaded 503"), auth_mode="api"
    ).reasons == ("review_failure_unconfirmed",)
    assert v.classify_attempt(
        result, auth_mode="api", trusted_events=[{**event, "source": "tool_result"}]
    ).reasons == ("review_failure_unconfirmed",)


def test_attempt_snapshot_and_checkpoint_retry_keep_prior_bytes(tmp_path, fingerprint):
    pr = tmp_path / "pr"
    pr.mkdir()
    (pr / "raw.json").write_text("original", encoding="utf-8")
    original = v.snapshot_attempt(
        tmp_path,
        pr,
        run_id="run",
        pr_url="pr",
        result=InvokeResult("failed", reason="stop"),
        validity=replace(
            v.classify_attempt(InvokeResult("failed", reason="stop"), auth_mode="api"),
            observed=fingerprint,
        ),
        started_at=NOW,
    )
    first = next((tmp_path / "attempts").glob("*/*/attempt.json"))
    before = first.read_bytes()
    (pr / "raw.json").write_text("retry", encoding="utf-8")
    retry = v.snapshot_attempt(
        tmp_path,
        pr,
        run_id="run",
        pr_url="pr",
        result=InvokeResult("ok"),
        validity=v.Validity("valid", retry_of=original.attempt_id),
        started_at=NOW,
    )
    assert retry.attempt_id != original.attempt_id and retry.sequence == 2
    assert first.read_bytes() == before
    assert (first.parent / "artifacts/raw.json").read_text(
        encoding="utf-8"
    ) == "original"
    cp = checkpoint.Checkpoint(tmp_path)
    for cause in v.RETRY_REASONS:
        cp.mark(
            cause,
            "invalid",
            {"validity": v.Validity("invalid", (cause,), retryable=True).wire()},
        )
    cp.mark(
        "other", "invalid", {"validity": v.Validity("invalid", ("input_drift",)).wire()}
    )
    assert cp.failed([*sorted(v.RETRY_REASONS), "other"]) == sorted(v.RETRY_REASONS)
    with pytest.raises(ValueError, match="unrelated"):
        v.check_retry(
            replace(original, validity=v.Validity("invalid", ("input_drift",))), now=NOW
        )


def test_measurement_requires_explicit_service_id(tmp_path):
    epoch = load_epoch(root=DEFAULT_ROOT)
    for model in ("inherit", "opus", "opus[1m]"):
        with pytest.raises(ValueError, match="explicit"):
            v.capture_fingerprint(
                cli_path=tmp_path / "binary",
                child_model=model,
                auth_mode="api",
                epoch=epoch,
                judge_pin="judge",
                adjudicator_pin="judge",
                env={},
                policy={},
            )


@pytest.mark.parametrize(
    "mode,auth,cause",
    [
        ("fast_exit", "subscription", "subscription_stall_unknown"),
        ("provider_overload", "api", "provider_transient"),
        ("quoted_overload", "api", "review_failure_unconfirmed"),
    ],
)
def test_fake_child_failure_envelope_is_classified_at_collection(
    tmp_path, mode, auth, cause
):
    import os
    import subprocess
    import sys

    pr = tmp_path / "pr"
    pr.mkdir()
    raw = pr / "raw.json"
    fake = Path(__file__).parent / "fakes/fake_claude.py"
    result = subprocess.run(
        [sys.executable, str(fake), "-p"],
        env={**os.environ, "FAKE_CLAUDE_MODE": mode},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 1
    raw.write_text(result.stdout, encoding="utf-8")
    attempt = v.collect_attempt(
        tmp_path,
        pr,
        url="pr",
        result=InvokeResult("failed", raw_json_path=str(raw)),
        started_at=NOW,
        manifest={"run_id": "synthetic", "child_auth": auth},
    )
    assert attempt.validity.reasons == (cause,) and attempt.validity.state == "invalid"
    assert (
        attempt.validity.failure_fingerprint is not None
        if mode != "fast_exit"
        else attempt.validity.failure_fingerprint is None
    )


@pytest.mark.parametrize("auth", ["api", "subscription"])
@pytest.mark.parametrize(
    "fixture", ["api_connection_closed", "api_bad_request", "aborted_tools"]
)
def test_recorded_cli_failure_is_confirmed_after_its_retry_gate(
    tmp_path, monkeypatch, fingerprint, auth, fixture
):
    envelope = read_json(
        Path(__file__).parent / "fixtures/cli_failures" / f"{fixture}.json"
    )
    pr = tmp_path / "pr"
    pr.mkdir()
    raw = pr / "raw.json"
    raw.write_text(json.dumps(envelope), encoding="utf-8")
    manifest = {
        "child_auth": auth,
        "provenance": replace(fingerprint, auth_mode=auth).wire(),
    }
    result = InvokeResult("failed", raw_json_path=str(raw))
    original = v.collect_attempt(
        tmp_path, pr, url="pr", result=result, started_at=NOW, manifest=manifest
    )
    assert original.validity.failure_fingerprint
    assert original.validity.state == "invalid"
    reset = tmp_path / "public-reset.json"
    reset.write_text('{"reset":true}', encoding="utf-8")
    if auth == "subscription":
        with pytest.raises(ValueError, match="reset_not_confirmed"):
            v.check_retry(original, now="2026-10-20T14:00:00Z")
    reset_ref = v.check_retry(
        original,
        now="2026-10-20T14:00:00Z",
        reset_evidence=reset,
        reset_confirmed_at="2026-10-20T13:00:00Z",
    )
    monkeypatch.setattr(v, "utc_now", lambda: "2026-10-20T14:00:00Z")
    retry = v.collect_attempt(
        tmp_path,
        pr,
        url="pr",
        result=result,
        started_at=v.utc_now(),
        manifest=manifest,
        prior=original,
        reset_at="2026-10-20T13:00:00Z" if reset_ref else None,
        reset_ref=reset_ref,
    )
    assert retry.validity.state == "operational_failure"
    assert retry.validity.failure_fingerprint == original.validity.failure_fingerprint
    assert v.confirmed_failure(retry)


@pytest.mark.parametrize("status", [429, 500, 503, 529, 600, 400])
def test_cli_api_status_classification_and_normalized_confirmation(
    tmp_path, monkeypatch, status
):
    pr = tmp_path / "pr"
    pr.mkdir()
    raw = pr / "raw.json"
    envelope = read_json(
        Path(__file__).parent / "fixtures/cli_failures/api_bad_request.json"
    )
    envelope["api_error_status"] = status
    manifest = {"child_auth": "api"}
    result = InvokeResult("failed", raw_json_path=str(raw))

    def collect(prior, diagnostic):
        raw.write_text(
            json.dumps({**envelope, "errors": [diagnostic]}), encoding="utf-8"
        )
        return v.collect_attempt(
            tmp_path,
            pr,
            url="pr",
            result=result,
            started_at=v.utc_now(),
            manifest=manifest,
            prior=prior,
        )

    original = collect(
        None,
        "Connection closed request_id=req_first at 2026-10-20T12:00:00.123Z byte offset 123",
    )
    assert original.validity.reasons == (
        ("provider_transient",) if status != 400 else ("review_failure_unconfirmed",)
    )
    monkeypatch.setattr(v, "utc_now", lambda: "2026-10-20T14:00:00Z")
    retry = collect(
        original,
        "Connection closed request_id=req_second at 2026-10-20T14:00:00.999Z byte offset 987",
    )
    assert retry.validity.state == "operational_failure"
    assert retry.validity.failure_fingerprint == original.validity.failure_fingerprint
    changed = collect(
        original,
        "Malformed tool arguments request_id=req_third at 2026-10-20T14:00:00Z byte offset 789",
    )
    assert changed.validity.state == "invalid" and changed.validity.retryable
    assert changed.validity.failure_fingerprint != original.validity.failure_fingerprint


@pytest.mark.parametrize("auth", ["api", "subscription"])
@pytest.mark.parametrize(
    "status,reason", [("timeout", "watchdog_timeout"), ("failed", "fast_exit")]
)
def test_failure_without_fingerprint_counts_only_after_same_pr_retry(
    tmp_path, monkeypatch, fingerprint, auth, status, reason
):
    pr = tmp_path / "pr"
    pr.mkdir()
    result = InvokeResult(status, reason=reason)
    manifest = {
        "child_auth": auth,
        "provenance": replace(fingerprint, auth_mode=auth).wire(),
    }
    original = v.collect_attempt(
        tmp_path, pr, url="pr", result=result, started_at=NOW, manifest=manifest
    )
    assert (
        original.validity.state == "invalid"
        and original.validity.failure_fingerprint is None
    )
    reset = tmp_path / "public-reset.json"
    reset.write_text('{"reset":true}', encoding="utf-8")
    reset_at = "2026-10-20T13:00:00Z"
    if auth == "subscription":
        monkeypatch.setattr(v, "utc_now", lambda: "2026-10-20T14:00:00Z")
        unconfirmed = v.collect_attempt(
            tmp_path,
            pr,
            url="pr",
            result=result,
            started_at=v.utc_now(),
            manifest=manifest,
            prior=original,
        )
        assert unconfirmed.validity.state == "invalid"
        with pytest.raises(ValueError, match="reset_not_confirmed"):
            v.check_retry(original, now=v.utc_now())
    reset_ref = v.check_retry(
        original,
        now="2026-10-20T14:00:00Z",
        reset_evidence=reset,
        reset_confirmed_at=reset_at,
    )
    monkeypatch.setattr(v, "utc_now", lambda: "2026-10-20T14:00:00Z")
    retry = v.collect_attempt(
        tmp_path,
        pr,
        url="pr",
        result=result,
        started_at=v.utc_now(),
        manifest=manifest,
        prior=original,
        reset_at=reset_at if reset_ref else None,
        reset_ref=reset_ref,
    )
    assert retry.validity.state == "operational_failure"
    assert retry.validity.failure_fingerprint is None
    assert (
        "failure-confirmation:recurring-same-pr-without-stable-fingerprint"
        in retry.validity.evidence_refs
    )
    assert (
        "original-failure-reason:" + ",".join(original.validity.reasons)
        in retry.validity.evidence_refs
    )
    with pytest.raises(ValueError, match="same PR"):
        v.confirm_failure(
            original, replace(retry, pr_url="other"), reset_evidence=reset_ref
        )


@pytest.mark.parametrize(
    "reason",
    ["mirror_error", "claude_not_found", "unexpected_error:auth setup unavailable"],
)
def test_pre_dispatch_subscription_failure_needs_no_reset_and_never_counts(
    fingerprint, reason
):
    value = v.classify_attempt(
        InvokeResult("failed", reason=reason, child_dispatched=False),
        auth_mode="subscription",
    )
    original = v.AttemptRecord(
        1,
        "a",
        "run",
        "pr",
        NOW,
        NOW,
        "failed",
        "mirror_error",
        replace(value, observed=replace(fingerprint, auth_mode="subscription")),
        (),
        1,
        (),
        (),
    )
    assert v.check_retry(original, now=NOW) is None
    retry = replace(
        original, attempt_id="b", validity=replace(original.validity, retry_of="a")
    )
    with pytest.raises(ValueError, match="not retryable"):
        v.confirm_failure(original, retry, reset_evidence=None)


@pytest.mark.parametrize("mutation", [None, "duplicate", "missing", "different"])
def test_run_window_pr_identity_and_manifest_order(
    harness, monkeypatch, window, mutation
):
    window = replace(window, pr_urls=tuple(reversed(window.pr_urls)))
    path = harness.workspace / "window.json"
    v.exclusive_json(path, window.wire())
    monkeypatch.setattr(v, "run_provenance", lambda **kwargs: window.expected.wire())
    monkeypatch.setattr(v, "recheck_cli", lambda fp: None)
    urls = list(reversed(window.pr_urls))
    if mutation == "duplicate":
        urls.append(urls[0])
    elif mutation == "missing":
        urls.pop()
    elif mutation == "different":
        urls[0] = next(
            url
            for url in run._load_json(run.GOLDEN_DIR / "shas.json")
            if url not in urls
        )
    calls = []

    def invoke(worktree, pr, run_dir, **kwargs):
        calls.append(pr["url"])
        return InvokeResult("failed", reason="fast_exit")

    monkeypatch.setattr(run.invoke, "invoke_review", invoke)
    args = [
        "--prs",
        ",".join(urls),
        "--measurement-window",
        str(path),
        "--child-model",
        "claude-session-1",
        "--run-index",
        "0",
        "--arm",
        "baseline",
    ]
    assert run.main(args) == (1 if mutation is None else 2)
    if mutation:
        assert not calls and not harness.commands
    else:
        manifest = read_json(next(run.RUNS_ROOT.glob("*/run.json")))
        assert manifest["pr_urls"] == list(window.pr_urls)
        assert calls == list(window.pr_urls)


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "runtime_model",
        "dispatch_model",
        "missing_map",
        "missing_payload",
        "broken_trace",
        "closed_window",
    ],
)
def test_successful_collection_requires_payload_and_independent_model_identity(
    tmp_path, monkeypatch, fingerprint, mutation, window
):
    from bench.runner import check

    monkeypatch.setattr(check, "check_attempt", lambda *args, **kwargs: ())
    pr = tmp_path / "pr"
    pr.mkdir()
    payload = pr / "post-review-payload.json"
    payload.write_text(
        '{"platform":"github","payload":{"comments":[]}}', encoding="utf-8"
    )
    raw = pr / "raw.json"
    raw.write_text(
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "modelUsage": {
                    "claude-unpinned"
                    if mutation == "runtime_model"
                    else "claude-session-1": {}
                },
            }
        ),
        encoding="utf-8",
    )
    models = {k: val for k, val in fingerprint.resolved_models if k != "orchestrator"}
    if mutation == "dispatch_model":
        models["code-gauntlet:bug-detector"] = "claude-other"
    if mutation != "missing_map":
        (pr / "workflows").mkdir()
        (pr / "workflows/wf.json").write_text(
            json.dumps(
                {
                    "scriptPath": str(run.REPO_ROOT / "workflows/pipeline.js"),
                    "args": {"outputDir": str(tmp_path / "output")},
                    "result": {"resolvedPolicy": {"resolvedModels": models}},
                }
            ),
            encoding="utf-8",
        )
    if mutation == "missing_payload":
        payload.unlink()
    if mutation == "broken_trace":
        (pr / "code-gauntlet-measurement-fake.json").write_text(
            "{broken", encoding="utf-8"
        )
    manifest = {
        "run_id": "synthetic",
        "provenance": fingerprint.wire(),
        "window_id": "test",
        "child_model": "claude-session-1",
    }
    if mutation == "closed_window":
        manifest["measurement_window"] = "synthetic-window.json"
        monkeypatch.setattr(v, "load_window", lambda path: window)
        monkeypatch.setattr(v, "recheck_cli", lambda *args: None)
        monkeypatch.setattr(v, "utc_now", lambda: "2026-10-22T00:00:00Z")
    attempt = v.collect_attempt(
        tmp_path,
        pr,
        url="pr",
        result=InvokeResult("ok", payload_path=str(payload), raw_json_path=str(raw)),
        started_at=NOW,
        manifest=manifest,
    )
    assert attempt.validity.state == (
        "valid" if mutation in (None, "broken_trace") else "invalid"
    )
    assert attempt.observed_model_ids == (
        ("claude-unpinned",) if mutation == "runtime_model" else ("claude-session-1",)
    )


def test_run_main_rechecks_binary_before_second_pr_mirror_or_child(
    tmp_path, monkeypatch, window
):
    import hashlib

    path = tmp_path / "window.json"
    v.exclusive_json(path, window.wire())
    monkeypatch.setattr(run, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(run, "MIRRORS_DIR", tmp_path / "mirrors")
    monkeypatch.setattr(run, "check_prereqs", lambda **kwargs: [])
    monkeypatch.setattr(run, "_judge_key_note", lambda *args: None)
    monkeypatch.setattr(run, "_compute_diff", lambda *args: "")
    monkeypatch.setattr(
        run.invoke, "_claude_home", lambda *args: tmp_path / "claude-home"
    )

    def fingerprint(**kwargs):
        return replace(
            window.expected,
            cli_sha256=hashlib.sha256(
                Path(window.expected.cli_realpath).read_bytes()
            ).hexdigest(),
        )

    monkeypatch.setattr(v, "capture_fingerprint", fingerprint)
    mirrors, calls = [], []

    def mirror(*args):
        mirrors.append(args)
        return tmp_path / "mirror"

    monkeypatch.setattr(run.mirrors, "ensure_mirror", mirror)
    monkeypatch.setattr(
        run.mirrors,
        "make_worktree",
        lambda *args, **kwargs: Path(args[4]).mkdir(parents=True),
    )
    monkeypatch.setattr(run.mirrors, "remove_worktree", lambda *args: None)

    def invoke(worktree, pr, run_dir, **kwargs):
        assert kwargs["child_model"] == "claude-session-1"
        calls.append(pr)
        output = Path(run_dir) / "output"
        output.mkdir(exist_ok=True)
        payload = output / "post-review-payload.json"
        payload.write_text('{"payload":{"comments":[]}}', encoding="utf-8")
        Path(window.expected.cli_realpath).write_text("replacement", encoding="utf-8")
        return InvokeResult("ok", payload_path=str(payload))

    monkeypatch.setattr(run.invoke, "invoke_review", invoke)
    assert (
        run.main(
            [
                "--tier",
                "subset",
                "--measurement-window",
                str(path),
                "--child-model",
                "claude-session-1",
                "--arm",
                "baseline",
                "--run-index",
                "0",
            ]
        )
        == 2
    )
    assert len(mirrors) == len(calls) == 1


@pytest.mark.parametrize(
    "key", ["CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"]
)
def test_measurement_rejects_unresolved_provider_auth_overrides(tmp_path, key):
    epoch = load_epoch(root=DEFAULT_ROOT)
    with pytest.raises(ValueError, match="provider/auth"):
        v.capture_fingerprint(
            cli_path=tmp_path / "binary",
            child_model="claude-session-1",
            auth_mode="api",
            epoch=epoch,
            judge_pin="judge",
            adjudicator_pin="judge",
            env={key: "unrecorded-sentinel"},
            policy={},
        )
