"""Real Workflow stage failures cannot become valid observations without a trace."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from bench.runner import check, invoke, validity
from bench.runner.epochs import read_json
from bench.tests.test_check import _build_ok_run, _plant_raw_identity
from bench.tests.test_validity import fingerprint as fingerprint
from bench.tests.test_validity import window as window

ROOT = Path(__file__).resolve().parents[2]


def test_archived_named_workflow_failure_survives_treatment_bundle_switch(tmp_path):
    path = tmp_path / "workflows/wf_archived.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "workflowName": invoke.PIPELINE_META_NAME,
                "script": "previous treatment bundle",
                "result": {"ok": True, "stats": {"validate": {"skipped": 1}}},
            }
        ),
        encoding="utf-8",
    )
    failures, _ = check.workflow_stage_outcomes(tmp_path)
    assert failures == ("validate execution failed",)


@pytest.mark.parametrize(
    "stage,result_status",
    [
        (None, "ok"),
        *(
            (s, status)
            for s in ("discover", "verify", "validate", "challenge")
            for status in ("ok", "failed", "timeout")
        ),
    ],
)
@pytest.mark.parametrize("replay", [False, True])
def test_producer_failures_are_invalid_without_optional_trace(
    tmp_path, fingerprint, stage, result_status, replay, monkeypatch
):
    monkeypatch.setattr(validity, "utc_now", lambda: "2026-10-20T12:00:00Z")
    script = """
import { measurementScenario } from './workflows/test/helpers/measurementScenario.js';
import { runWith } from './workflows/src/run.js';
const stage = process.argv[1];
const { args, ctx } = measurementScenario('return');
const original = ctx.agent;
ctx.agent = async (prompt, dispatch) => {
  const label = dispatch.label;
  if ((stage === 'discover' && dispatch.agentType === 'code-gauntlet:bug-detector') ||
      (stage === 'verify' && label.startsWith('verify-slice-')) ||
      (stage === 'validate' && label.startsWith('validate-batch-')) ||
      (stage === 'challenge' && label.startsWith('challenge-'))) throw new Error('execution failed');
  if (label.startsWith('validate-batch-')) return { validations: [...prompt.matchAll(/^- (\\S+) \\[/gm)].map((m) => ({ finding_id: m[1], confidence: 90 })) };
  return original(prompt, dispatch);
};
const out = await runWith(ctx, args);
process.stdout.write(JSON.stringify(out));
"""
    produced = subprocess.run(
        ["node", "--input-type=module", "-e", script, stage or "healthy"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    out = json.loads(produced.stdout)
    assert out["ok"]
    run = tmp_path / "run"
    run.mkdir()
    _build_ok_run(run)
    pr = next(run.glob("pr-*"))
    record = pr / "workflows/wf_test-0001.json"
    wire = read_json(record)
    out.pop("persistReturn", None)
    out.pop("measurement", None)
    wire["result"] = out
    wire["args"] = {
        "outputDir": str(run / "output"),
        **({"checkpoints": {"completed": ["discover"]}} if replay else {}),
    }
    record.write_text(json.dumps(wire), encoding="utf-8")
    _plant_raw_identity(pr)
    raw = pr / "raw.json"
    envelope = read_json(raw)
    envelope["modelUsage"] = {"claude-session-1": {}}
    raw.write_text(json.dumps(envelope), encoding="utf-8")
    assert not list(pr.glob("code-gauntlet-measurement-*"))
    assert check.check_attempt(pr) == []
    smoke = check.check_run(run)
    assert smoke["ok"] is (stage is None)
    if stage is None:
        assert any(
            "challenge cap" in text for text in smoke["stats"]["stage_disclosures"]
        )
    attempt = validity.collect_attempt(
        run,
        pr,
        url="https://github.com/example/repo/pull/1",
        result=invoke.InvokeResult(
            result_status,
            payload_path=str(pr / "post-review-payload.json"),
            raw_json_path=str(raw),
        ),
        started_at="2026-10-20T10:00:00Z",
        manifest={"provenance": fingerprint.wire()},
    )
    assert attempt.validity.state == ("invalid" if stage else "valid")
    if stage:
        assert "pipeline_degraded" in attempt.validity.reasons
        assert (
            f"{stage if stage != 'discover' else 'discovery'} execution failed"
            in attempt.validity.evidence_refs
        )
    else:
        assert any("challenge cap" in text for text in attempt.validity.limitations)
    if replay:
        assert any("Checkpoint replay" in text for text in attempt.validity.limitations)
    path = next((run / "attempts").glob(f"*/{attempt.attempt_id}/attempt.json"))
    if stage:
        old = replace(
            attempt, validity=replace(attempt.validity, state="valid", reasons=())
        )
        path.write_text(json.dumps(old.wire()), encoding="utf-8")
        state = run / "state/pr-1.json"
        state_wire = read_json(state)
        state_wire["detail"] = {"attempt_id": attempt.attempt_id}
        state.write_text(json.dumps(state_wire), encoding="utf-8")
        with pytest.raises(ValueError, match="invalid selected attempt"):
            validity.load_attempt(path)
    else:
        assert validity.load_attempt(path).validity.state == "valid"


@pytest.mark.parametrize(
    "measurement",
    [
        None,
        {},
        {"status": "available"},
        {"status": "unavailable", "reason": "return_budget"},
    ],
)
def test_window_attempt_requires_terminal_measurement_status(
    tmp_path, fingerprint, window, monkeypatch, measurement
):
    monkeypatch.setattr(validity, "utc_now", lambda: "2026-10-20T12:00:00Z")
    monkeypatch.setattr(validity, "recheck_cli", lambda fp: None)
    path = tmp_path / "window.json"
    validity.exclusive_json(path, window.wire())
    run = tmp_path / "run"
    run.mkdir()
    _build_ok_run(run)
    pr = next(run.glob("pr-*"))
    record = pr / "workflows/wf_test-0001.json"
    wire = read_json(record)
    wire["result"] = {"ok": True, "stats": {}, "measurement": measurement}
    record.write_text(json.dumps(wire), encoding="utf-8")
    raw = pr / "raw.json"
    _plant_raw_identity(pr)
    raw.write_text(
        json.dumps({**read_json(raw), "modelUsage": {"claude-session-1": {}}}),
        encoding="utf-8",
    )
    attempt = validity.collect_attempt(
        run,
        pr,
        url="u",
        result=invoke.InvokeResult(
            "ok",
            payload_path=str(pr / "post-review-payload.json"),
            raw_json_path=str(raw),
        ),
        started_at="2026-10-20T10:00:00Z",
        manifest={
            "provenance": fingerprint.wire(),
            "measurement_window": str(path),
            "arm": "baseline",
        },
    )
    missing = not measurement
    assert attempt.validity.state == ("invalid" if missing else "valid")
    assert ("measurement_trace_missing" in attempt.validity.reasons) is missing
