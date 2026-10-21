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


@pytest.mark.parametrize(
    "template,gap",
    [
        (
            "${spec.agentType}: agent returned null (dispatch failed) "
            "\u2014 dimensions ${spec.dimensions.join('/')} not covered${conditionalNote}",
            "code-gauntlet:test-analyzer: agent returned null (dispatch failed) "
            "\u2014 dimensions test_coverage not covered",
        ),
        (
            "${spec.agentType}: malformed result (no findings array)",
            "code-gauntlet:test-analyzer: malformed result (no findings array)",
        ),
    ],
)
def test_discovery_failure_gap_wording_matches_producer(tmp_path, template, gap):
    source = (ROOT / "workflows/src/discover.js").read_text(encoding="utf-8")
    assert f"gaps.push(`{template}`);" in source
    assert (
        "gaps.push(`removed-test audit: ${removedTestAudit.status}"
        "${removedTestAudit.reason ? ` (${removedTestAudit.reason})` : ''}"
        "${removedTestAudit.omitted ? ` (${removedTestAudit.omitted} omitted)` : ''}`);"
    ) in source
    record = tmp_path / "workflows/wf_discovery.json"
    record.parent.mkdir()
    record.write_text(
        json.dumps(
            {
                "workflowName": invoke.PIPELINE_META_NAME,
                "result": {"ok": True, "gaps": [gap]},
            }
        ),
        encoding="utf-8",
    )
    assert check.workflow_stage_outcomes(tmp_path)[0] == ("discovery execution failed",)


def _collect_discovery_attempt(tmp_path, fingerprint, out, checkpoint=None):
    run = tmp_path / "run"
    run.mkdir()
    _build_ok_run(run)
    pr = next(run.glob("pr-*"))
    record = pr / "workflows/wf_test-0001.json"
    wire = read_json(record)
    wire["result"] = out
    record.write_text(json.dumps(wire), encoding="utf-8")
    if checkpoint:
        next(pr.glob("code-gauntlet-checkpoint-all-*.json")).write_text(
            json.dumps({"phases": {"discover": checkpoint}}), encoding="utf-8"
        )
    _plant_raw_identity(pr)
    raw = pr / "raw.json"
    raw.write_text(
        json.dumps({**read_json(raw), "modelUsage": {"claude-session-1": {}}}),
        encoding="utf-8",
    )
    attempt = validity.collect_attempt(
        run,
        pr,
        url="https://github.com/example/repo/pull/1",
        result=invoke.InvokeResult(
            "ok",
            payload_path=str(pr / "post-review-payload.json"),
            raw_json_path=str(raw),
        ),
        started_at="2026-10-20T10:00:00Z",
        manifest={"provenance": fingerprint.wire()},
    )
    assert (
        validity.load_attempt(validity.selected_attempt_path(run, attempt.attempt_id))
        == attempt
    )
    return attempt, check.check_run(run)


@pytest.mark.parametrize("carrier", ["gaps", "dimensions", "checkpoint", "replay"])
def test_removed_test_audit_overflow_is_valid_with_limitation(
    tmp_path, fingerprint, carrier
):
    gap = "removed-test audit: overflow (28 omitted)"
    audit = {"status": "overflow", "omitted": 28}
    discover = {"degraded": ["test_coverage"], "gaps": [gap], "removedTestAudit": audit}
    out = {"ok": True, "stats": {"degraded": ["test_coverage"]}}
    checkpoint = None
    if carrier == "gaps":
        out["gaps"] = [gap]
    elif carrier == "dimensions":
        out["dimensions"] = {"removedTestAudit": audit}
    elif carrier == "checkpoint":
        out["stats"] = {}
        checkpoint = discover
    else:
        out["gaps"] = [gap]
        out["checkpoints"] = {"phases": {"discover": discover}}
    attempt, smoke = _collect_discovery_attempt(tmp_path, fingerprint, out, checkpoint)
    assert smoke["ok"], smoke["failures"]
    assert attempt.validity.state == "valid"
    assert attempt.validity.evidence_refs == ()
    assert f"Recorded limitation: {gap}" in attempt.validity.limitations
    assert any(gap in text for text in smoke["stats"]["stage_disclosures"])


@pytest.mark.parametrize(
    "diagnostic",
    [
        "agent returned null (dispatch failed) \u2014 dimensions test_coverage not covered",
        "malformed result (no findings array)",
    ],
)
def test_discovery_agent_failure_stays_invalid_with_audit_limitation(
    tmp_path, fingerprint, diagnostic
):
    audit_gap = "removed-test audit: overflow (28 omitted)"
    out = {
        "ok": True,
        "stats": {"degraded": ["test_coverage"]},
        "dimensions": {"removedTestAudit": {"status": "overflow", "omitted": 28}},
        "gaps": [audit_gap, f"code-gauntlet:test-analyzer: {diagnostic}"],
    }
    attempt, smoke = _collect_discovery_attempt(tmp_path, fingerprint, out)
    assert not smoke["ok"]
    assert attempt.validity.state == "invalid"
    assert "pipeline_degraded" in attempt.validity.reasons
    assert "discovery execution failed" in attempt.validity.evidence_refs
    assert f"Recorded limitation: {audit_gap}" in attempt.validity.limitations


@pytest.mark.parametrize("agent_result", ["findings", "null", "malformed"])
def test_producer_audit_overflow_preserves_discovery_execution_status(
    tmp_path, fingerprint, agent_result
):
    script = r"""
import { measurementScenario } from './workflows/test/helpers/measurementScenario.js';
import { runWith } from './workflows/src/run.js';
import { fnv1a32 } from './workflows/src/wire.js';
const inventory = { status: 'available', items: [], omitted: 28 };
const { args, ctx } = measurementScenario('return', {
  removedTests: { ...inventory, checksum: fnv1a32(JSON.stringify(inventory, null, 2)) },
});
const original = ctx.agent;
const returned = [];
ctx.agent = async (prompt, dispatch) => {
  if (dispatch.label.startsWith('validate-batch-')) {
    return { validations: [...prompt.matchAll(/^- (\S+) \[/gm)].map((m) => ({ finding_id: m[1], confidence: 90 })) };
  }
  if (dispatch.agentType === 'code-gauntlet:test-analyzer') {
    if (process.argv[1] === 'null') return null;
    if (process.argv[1] === 'malformed') return {};
  }
  const result = await original(prompt, dispatch);
  if (dispatch.label.startsWith('code-gauntlet:')) returned.push(dispatch.label);
  return result;
};
const out = await runWith(ctx, args);
delete out.persistReturn;
process.stdout.write(JSON.stringify({ out, returned }));
"""
    produced = subprocess.run(
        ["node", "--input-type=module", "-e", script, agent_result],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    record = json.loads(produced.stdout)
    out = record["out"]
    assert out["ok"]
    assert out["stats"]["degraded"] == ["test_coverage"]
    healthy = agent_result == "findings"
    if healthy:
        assert len(set(record["returned"])) == 7
        assert out["stats"]["discovered"] > 0
        assert "removed-test audit: overflow (28 omitted)" in out["gaps"]
    attempt, smoke = _collect_discovery_attempt(tmp_path, fingerprint, out)
    assert smoke["ok"] is healthy, smoke["failures"]
    assert attempt.validity.state == ("valid" if healthy else "invalid")
    assert any(
        text.startswith("Recorded limitation: removed-test audit: ")
        for text in attempt.validity.limitations
    )
    assert ("discovery execution failed" in attempt.validity.evidence_refs) is (
        not healthy
    )


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
