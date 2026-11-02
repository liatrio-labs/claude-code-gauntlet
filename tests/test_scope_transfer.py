"""Cited tool exclusions distinguish current behavior defects from quality claims."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("stage", ["validator", "challenger"])
@pytest.mark.parametrize(
    "dimension,reachability,promoted,expected",
    [
        ("bug", "current", False, "deliver"),
        ("cross_file_impact", "current", False, "deliver"),
        ("convention", "current", False, "eliminate"),
        ("simplification", "current", False, "eliminate"),
        ("type_design", "current", False, "eliminate"),
        ("simplification", "current", True, "deliver"),
        ("type_design", "current", True, "deliver"),
        ("test_coverage", "current", True, "deliver"),
        ("bug", "future_change_only", False, "eliminate"),
        ("bug", "uncertain", False, "held_back"),
        ("convention", None, False, "held_back"),
    ],
)
def test_cited_tool_exclusion_protects_only_current_correctness_claims(
    stage, dimension, reachability, promoted, expected
):
    script = r"""
import { runWith } from './workflows/src/run.js';
import { makeFinding, makeCtx } from './workflows/test/helpers/pipelineMock.js';
import { validArgs } from './workflows/test/helpers/argsFixtures.js';
import { challengeOutput } from './workflows/test/helpers/persistFixtures.js';
import { skepticalReturn, skepticalAssessment, sourceCapture } from './workflows/test/helpers/verdicts.js';
const [stage, dimension, reachability, promoted] = JSON.parse(process.argv[1]);
const admission_assessments = [{ predicate: 'present_defect', status: promoted ? 'confirmed' : 'rejected', references: ['primary'] }];
const claim = skepticalReturn('SCOPE', stage).citations[0];
const scope = { finding_id: 'SCOPE', stage, category: 'lint_catchable', applicability: 'applicable',
  citations: ['rule', 'applicability'].map(role => ({ ...claim, role, request_id: `tool-${role}`,
    file: role === 'rule' ? '.toolrules' : claim.file })) };
const finding = makeFinding('SCOPE', { dimension, reachability, admission_assessments,
  quality_basis: 'other', test_kind: 'additional_case',
  skeptical_assessments: ['validator', 'challenger'].map(s => skepticalAssessment('SCOPE', s,
    { ...(s === 'validator' ? { admission_assessments } : {}),
      ...(s === stage ? { scope_assessments: [scope] } : {}) })) });
const args = validArgs({ nonce: 'run', headShaShort: 'aaaaaaaa', verify: { targetKind: 'branch', sourceCapture },
  checkpoints: { challenge: challengeOutput({ findings: [finding] }) } });
let payload;
const ctx = makeCtx(args, { onPersist: value => { payload = value; } });
const out = await runWith(ctx, args);
const result = payload.findings.find(f => f.id === 'SCOPE');
process.stdout.write(JSON.stringify({ ok: out.ok, disposition: result.disposition,
  decision: result.scope_decisions.find(r => r.category === 'lint_catchable' && r.stage === stage).applies,
  posted: payload.postReview.some(f => f.id === 'SCOPE') }));
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            json.dumps([stage, dimension, reachability, promoted]),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert json.loads(result.stdout) == {
        "ok": True,
        "disposition": expected,
        "decision": {"deliver": False, "eliminate": True, "held_back": None}[expected],
        "posted": expected == "deliver",
    }
