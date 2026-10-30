"""Attribute persisted caller outcomes without interpreting reviewer prose."""

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

from bench.runner.compare import verdict_diagnostics
from bench.runner.stages import load_trace

ROOT = Path(__file__).resolve().parents[2]
NODE = r"""
import { runWith } from './workflows/src/run.js';
import { validArgs } from './workflows/test/helpers/argsFixtures.js';
import { makeCtx, makeFinding } from './workflows/test/helpers/pipelineMock.js';
import { sourceCapture, skepticalReturn, citationEnvelope } from './workflows/test/helpers/verdicts.js';
const results = [];
for (const stage of ['validator', 'challenger']) for (const fault of [
  'matched', 'refuted', 'missing', 'malformed', 'outage',
  'needs_evidence', 'omitted', 'scope', 'scope_unmatched', 'contradicted',
  'no_return', 'capped', 'refuted_unmatched', 'refuted_outage', 'partial', 'empty']) {
  const f = makeFinding('F', { agent: 'test-analyzer', test_kind: 'production_defect',
    admission_assessments: [{ predicate: 'present_defect', status: 'confirmed', references: ['primary'] }],
    trace_id: 'run:0:0', discovery_agent: 'test-analyzer',
    discovery_dimension: 'test_coverage', diff_validation: { in_diff: fault === 'matched' ? false : true } });
  const a = f.skeptical_assessments.find((a) => a.stage === stage);
  if (fault.startsWith('refuted')) a.verdict = 'refuted';
  if (['no_return', 'capped'].includes(fault)) {
    f.skeptical_assessments = f.skeptical_assessments.filter((a) => a.stage !== stage);
    if (stage === 'validator') f.validation = 'skipped';
    else f.challenge = fault === 'capped' ? 'capped' : 'failed';
  }
  if (fault === 'missing') a.citations = [];
  if (fault === 'malformed') a.citations[0].quote = '';
  if (fault === 'needs_evidence') { a.verdict = 'needs_evidence'; a.missing_evidence = 'caller contract'; delete a.citations; }
  if (fault === 'omitted') delete a.scope_assessments;
  if (fault === 'scope') f.reachability = 'future_change_only';
  if (['scope', 'scope_unmatched', 'contradicted'].includes(fault)) a.scope_assessments = [{ finding_id: 'F', stage,
    category: 'lint_catchable', applicability: fault === 'contradicted' ? 'contradicted' : 'applicable',
    citations: ['rule', 'applicability'].map((role) => ({ ...a.citations[0], role, request_id: role })) }];
  // Several successful quotes must still earn only one successful assessment.
  if (['matched', 'partial'].includes(fault)) a.citations.push({ ...a.citations[0], request_id: 'second' });
  const sibling = makeFinding('SIBLING', { agent: 'bug-detector', trace_id: 'run:0:1',
    discovery_agent: 'bug-detector', discovery_dimension: 'bug' });
  const findings = fault === 'empty' ? [] : [f, sibling];
  const phases = { summarize: { summary: 'diagnostic fixture' },
    discover: { findings, dispatched: ['code-gauntlet:bug-detector'], degraded: [] },
    merge: { findings, methodology: {} }, verify: { findings, verified: true, inputProof: {} },
    validate: { findings, stats: {} }, filter: { filtered: findings, stats: {} },
    challenge: { findings, unverified: [], eliminated: [], stats: { dispatched: findings.length - (stage === 'challenger' && fault === 'capped' ? 1 : 0), completed: findings.length } } };
  if (stage === 'challenger' && fault === 'capped') {
    phases.challenge.findings = [sibling]; phases.challenge.unverified = [f];
  }
  const args = validArgs({ nonce: 'run', headShaShort: 'aaaaaaaa', measurementTrace: true,
    verify: { targetKind: 'branch', sourceCapture }, checkpoints: structuredClone(phases) });
  const ctx = makeCtx(args);
  const agent = ctx.agent;
  ctx.agent = (prompt, dispatch) => dispatch.label.startsWith('cite-')
    ? fault.endsWith('outage') ? null : citationEnvelope(prompt, args, (r) =>
      r.stage === stage && r.finding_id === 'F' && (fault === 'refuted_unmatched'
        || (fault === 'partial' && r.request.request_id === 'second')
        || (fault === 'scope_unmatched' && r.request.role !== 'claim'))
        ? 'unmatched' : 'matched')
    : agent(prompt, dispatch);
  const out = await runWith(ctx, args);
  if (out.measurement?.status !== 'available') throw new Error(JSON.stringify(out));
  results.push({ stage, fault, trace: out.measurement.text });
}
console.log(JSON.stringify(results));
"""


def test_proof_scope_and_disposition_denominators_include_failures(tmp_path):
    rows = json.loads(
        subprocess.run(
            ["node", "--input-type=module", "-e", NODE],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
    )
    for row in rows:
        path = tmp_path / "trace.json"
        data = row["trace"].encode()
        path.write_bytes(data)
        trace = load_trace(path, expected_sha256=hashlib.sha256(data).hexdigest())
        diagnostic = verdict_diagnostics(trace)
        fault = row["fault"]
        aggregate = diagnostic["aggregate"]
        stamps = {
            (v.trace_id, r["stage"]): {
                k: value for k, value in r.items() if k != "stage"
            }
            for v in trace.versions
            for r in v.finding.get("reduction_proofs", [])
        }
        for snapshot in trace.snapshots:
            for assessment in snapshot.verdicts or ():
                stamp = stamps[assessment.trace_id, assessment.stage]
                assert {k: getattr(assessment, k) for k in stamp} == stamp
        assert diagnostic["assessments"] == [
            {
                "trace_id": r["trace_id"],
                "stage": r["stage"],
                **stamps[r["trace_id"], r["stage"]],
            }
            for r in diagnostic["assessments"]
        ]
        if row["stage"] == "challenger" and fault in ("capped", "empty"):
            baseline = replace(
                trace,
                snapshots=tuple(
                    replace(
                        s,
                        sequence=i,
                        stage="holdback" if s.stage == "dispose" else s.stage,
                        verdicts=None,
                        workload=None,
                    )
                    for i, s in enumerate(
                        s for s in trace.snapshots if s.stage != "cite"
                    )
                ),
            )
            base = verdict_diagnostics(baseline)
            assert "aggregate" not in base
            assert base["candidate_records"] == (0 if fault == "empty" else 2)
            assert base["challenge_dispatched"] == (0 if fault == "empty" else 1)
            assert base["challenge_overflow"] == (0 if fault == "empty" else 1)
            assert base["challenge_overflow_rate"] == (
                None if fault == "empty" else 0.5
            )
            assert base["workload"]["validate"]["latency_ms"] is None
            assert base["workload"]["challenge"]["cost_usd"] is None
        if fault == "matched":
            altered = replace(
                trace,
                snapshots=tuple(
                    replace(
                        s, verdicts=tuple(replace(r, proved=False) for r in s.verdicts)
                    )
                    if s.verdicts is not None
                    else s
                    for s in trace.snapshots
                ),
            )
            assert verdict_diagnostics(altered)["aggregate"] == aggregate
            identified = replace(
                trace,
                versions=tuple(
                    replace(v, issue_id="recorded-issue") for v in trace.versions
                ),
                snapshots=tuple(
                    replace(
                        s,
                        decisions=tuple(
                            replace(
                                d, detail={**d.detail, "issue_id": "recorded-issue"}
                            )
                            for d in s.decisions
                        ),
                    )
                    if s.stage == "dispose"
                    else s
                    for s in trace.snapshots
                ),
            )
            assert {
                r["issue_id"] for r in verdict_diagnostics(identified)["records"]
            } == {"recorded-issue"}
            timed = replace(
                trace,
                snapshots=tuple(
                    replace(
                        s,
                        workload={
                            **s.workload,
                            "attempts": [
                                {
                                    "started_at": "2026-10-30T12:00:00Z",
                                    "completed_at": "2026-10-30T12:00:01.250Z",
                                },
                                {
                                    "started_at": "2026-10-30T12:00:02Z",
                                    "completed_at": "2026-10-30T12:00:02.125Z",
                                },
                            ],
                        },
                    )
                    if s.stage == "validate"
                    else s
                    for s in trace.snapshots
                ),
            )
            workload = verdict_diagnostics(timed)["workload"]
            assert workload["validate"]["latency_ms"] == 1375
            assert workload["validate"]["cost_usd"] is None
            assert workload["cite"]["latency_ms"] is None
        if fault == "empty":
            assert aggregate["required"] == diagnostic["candidate_records"] == 0
            assert aggregate["proof_rate"] is None
            continue
        assert aggregate["required"] == 4
        assert aggregate["proof_denominator"] == (
            3 if fault in ("needs_evidence", "no_return", "capped") else 4
        )
        proved = 0 if fault.endswith("outage") else 3
        if fault in (
            "matched",
            "refuted",
            "scope",
            "scope_unmatched",
            "contradicted",
        ):
            proved = 4
        assert aggregate["proved"] == proved, fault
        assert aggregate["unresolved"] == int(fault in ("needs_evidence", "omitted"))
        assert diagnostic["stages"][row["stage"]]["required"] == 2
        record = next(r for r in diagnostic["records"] if r["finding_id"] == "F")
        assert record["original_producer"] == "test-analyzer"
        assert record["member_id"] == "run:0:0"
        assert record["issue_id"]
        assert "unknown" not in record["reasons"]
        if fault == "omitted":
            assert any(
                d.detail["reduction"].get("missing_fields")
                for s in trace.snapshots
                for d in s.decisions
                if d.detail
            )
        if fault in ("refuted", "scope"):
            assert record["disposition"] == "eliminate"
            assert record["reasons"] == [
                "refuted" if fault == "refuted" else "out_of_scope"
            ]
        if fault == "contradicted":
            decisions = [
                d
                for s in trace.snapshots
                if s.stage == "dispose"
                for d in s.decisions
                if d.reason_code == "scope_contradicted"
                and d.trace_id == record["member_id"]
            ]
            assert len(decisions) == 1
            assert any(
                d.reason_code == "scope_contradicted"
                and d.decision == "kept"
                and d.detail["category"] == "lint_catchable"
                and d.detail["predicate"] == "tool_covers_claim"
                and d.detail["stage"] == row["stage"]
                and d.detail["source"]
                for d in decisions
            )
        if fault.endswith("outage"):
            assert aggregate["unavailable"] == 4
            assert diagnostic["workload"]["cite"]["retries"] > 0
        if fault == "matched":
            assert record["disposition"] == "body"
        if fault in ("no_return", "capped"):
            expected_code = (
                "validation_skipped"
                if row["stage"] == "validator"
                else ("challenge_cap" if fault == "capped" else "challenge_failed")
            )
            assert {
                d.reason_code
                for s in trace.snapshots
                if s.stage == "cite"
                for d in s.decisions
                if d.trace_id == record["member_id"]
                and d.detail["stage"] == row["stage"]
            } == {expected_code}
            assert aggregate["operational_gaps"] == 1
            assert aggregate["non_dispatch"] == int(
                row["stage"] == "validator" or fault == "capped"
            )
            assert diagnostic["challenge_dispatched"] == (
                1 if row["stage"] == "challenger" and fault == "capped" else 2
            )
        if fault == "refuted_outage":
            decisions = [
                d
                for s in trace.snapshots
                if s.stage == "dispose"
                for d in s.decisions
                if d.trace_id == record["member_id"]
                and d.reason_code == "cite_unavailable"
            ]
            assert {d.detail["stage"] for d in decisions} == {"validator", "challenger"}
            assert all(
                d.detail["stage"] == d.detail["reduction"]["stage"] for d in decisions
            )
            assert all(
                d.detail["reduction"]["verdict"]
                == ("refuted" if d.detail["stage"] == row["stage"] else "real")
                for d in decisions
            )
        assert diagnostic["workload"]["cite"]["source_capture"]["copied_bytes"] == 0
