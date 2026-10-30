"""Attribute persisted caller outcomes without interpreting reviewer prose."""

import hashlib
import json
import subprocess
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
  'matched', 'refuted', 'missing', 'malformed', 'rejected', 'unmatched', 'outage',
  'wrong_snapshot', 'needs_evidence', 'invalid', 'omitted', 'scope', 'contradicted',
  'no_return', 'capped', 'refuted_unmatched', 'refuted_outage', 'partial', 'group', 'early_scope', 'pre_existing', 'empty']) {
  const f = makeFinding('F', { agent: 'test-analyzer', test_kind: 'production_defect',
    admission_assessments: [{ predicate: 'present_defect', status: 'confirmed', references: ['primary'] }],
    trace_id: 'run:0:0', discovery_agent: 'test-analyzer',
    discovery_dimension: 'test_coverage', diff_validation: { in_diff: fault === 'matched' ? false : true } });
  const a = f.skeptical_assessments.find((a) => a.stage === stage);
  if (fault === 'early_scope') f.scope_facts = { file: f.file,
    snapshot_id: sourceCapture.manifest_sha256, linguist_generated: true };
  if (fault === 'pre_existing') { f.scope_facts = { file: f.file, snapshot_id: sourceCapture.manifest_sha256 };
    f.blame_class = 'pre_existing'; f.blame_source = 'git_blame'; f.diff_validation.in_diff = false; }
  if (fault.startsWith('refuted')) a.verdict = 'refuted';
  if (['no_return', 'capped'].includes(fault)) {
    f.skeptical_assessments = f.skeptical_assessments.filter((a) => a.stage !== stage);
    if (stage === 'validator') f.validation = 'skipped';
    else f.challenge = fault === 'capped' ? 'capped' : 'failed';
  }
  if (fault === 'missing') a.citations = [];
  if (fault === 'malformed') a.citations[0].quote = '';
  if (fault === 'wrong_snapshot') a.citations[0].snapshot_id = 'e'.repeat(64);
  if (fault === 'needs_evidence') { a.verdict = 'needs_evidence'; a.missing_evidence = 'caller contract'; delete a.citations; }
  if (fault === 'invalid') a.verdict = 'invented';
  if (fault === 'omitted') delete a.scope_assessments;
  if (['scope', 'contradicted'].includes(fault)) a.scope_assessments = [{ finding_id: 'F', stage,
    category: 'lint_catchable', applicability: fault === 'scope' ? 'applicable' : 'contradicted',
    citations: ['rule', 'applicability'].map((role) => ({ ...a.citations[0], role, request_id: role })) }];
  // Several successful quotes must still earn only one successful assessment.
  if (['matched', 'partial'].includes(fault)) a.citations.push({ ...a.citations[0], request_id: 'second' });
  const sibling = makeFinding('SIBLING', { agent: 'bug-detector', trace_id: 'run:0:1',
    discovery_agent: 'bug-detector', discovery_dimension: 'bug', ...(fault === 'group' ? { file: f.file } : {}) });
  const findings = fault === 'empty' ? [] : [f, sibling];
  const phases = { summarize: { summary: 'diagnostic fixture' },
    discover: { findings, dispatched: ['code-gauntlet:bug-detector'], degraded: [] },
    merge: { findings, methodology: {} }, verify: { findings, verified: true, inputProof: {} },
    validate: { findings, stats: {} }, filter: { filtered: findings, stats: {} },
    challenge: { findings, unverified: [], eliminated: [], stats: { dispatched: findings.length, completed: findings.length } } };
  const args = validArgs({ nonce: 'run', headShaShort: 'aaaaaaaa', measurementTrace: true,
    verify: { targetKind: 'branch', sourceCapture }, checkpoints: structuredClone(phases) });
  const ctx = makeCtx(args);
  const agent = ctx.agent;
  ctx.agent = (prompt, dispatch) => dispatch.label.startsWith('cite-')
    ? fault.endsWith('outage') ? null : citationEnvelope(prompt, args, (r) =>
      r.stage === stage && r.finding_id === 'F' && (['rejected', 'unmatched', 'refuted_unmatched'].includes(fault)
        || (fault === 'partial' && r.request.request_id === 'second'))
        ? fault === 'rejected' ? 'rejected' : 'unmatched' : 'matched')
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
        if fault == "empty":
            assert aggregate["required"] == diagnostic["candidate_records"] == 0
            assert aggregate["proof_rate"] is None
            continue
        assert aggregate["required"] == 4
        assert aggregate["proof_denominator"] == (
            3 if fault in ("needs_evidence", "invalid", "no_return", "capped") else 4
        )
        proved = 0 if fault.endswith("outage") else 3
        if fault in (
            "matched",
            "refuted",
            "scope",
            "contradicted",
            "group",
            "early_scope",
            "pre_existing",
        ):
            proved = 4
        assert aggregate["proved"] == proved, fault
        assert aggregate["unresolved"] == int(
            fault in ("needs_evidence", "invalid", "omitted", "no_return", "capped")
        )
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
        if fault in ("refuted", "scope", "early_scope"):
            assert record["disposition"] == "eliminate"
            assert record["reasons"] == [
                "refuted" if fault == "refuted" else "out_of_scope"
            ]
        if fault == "contradicted":
            assert any(
                d.reason_code == "scope_contradicted"
                and d.decision == "kept"
                and d.detail["category"] == "lint_catchable"
                and d.detail["predicate"] == "tool_covers_claim"
                and d.detail["stage"] == row["stage"]
                and d.detail["source"]
                for s in trace.snapshots
                for d in s.decisions
            )
        if fault == "early_scope":
            assert any(
                d.detail["stage"] == "early" and d.detail["source"]["snapshot_id"]
                for s in trace.snapshots
                for d in s.decisions
                if d.reason_code == "out_of_scope"
            )
        if fault.endswith("outage"):
            assert aggregate["unavailable"] == 4
            assert diagnostic["workload"]["cite"]["retries"] > 0
        if fault == "matched":
            assert record["disposition"] == "body"
        if fault == "pre_existing":
            assert record["disposition"] == "report_only"
        if row["stage"] == "challenger" and fault in ("no_return", "capped"):
            assert aggregate["operational_gaps"] == 1
            assert diagnostic["challenge_dispatched"] == (1 if fault == "capped" else 2)
        assert diagnostic["workload"]["cite"]["source_capture"]["copied_bytes"] == 0
