"""Admission omissions retain skeptical validation after the bounded retry."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "missing",
    [
        "complete_searches",
        "complete_behavioral_absence",
        "present_defect",
        "confidence",
        "policy_subject",
    ],
)
def test_validator_retry_separates_admission_evidence_from_scoring(missing):
    script = r"""
import assert from 'node:assert/strict';
import { runWith } from './workflows/src/run.js';
import { makeFinding, makeCtx } from './workflows/test/helpers/pipelineMock.js';
import { validArgs } from './workflows/test/helpers/argsFixtures.js';
const missing = JSON.parse(process.argv[1]);
const ref = { file: 'engine', anchor: 'return value;', line_start: 10, line_end: 10, location_status: 'confirmed' };
const finding = makeFinding('gap', { dimension: 'test_coverage', agent: 'test-analyzer',
  test_kind: 'critical_gap', criticality: 9, failure_scenario: 'A critical behavior lacks coverage',
  production_element: ref, searched_paths: ['tests'], candidate_tests: [],
  resolved_references: { production_element: ref, searched_paths: [{path:'tests', available:true}], candidate_tests: [] } });
const entries = [
  {predicate:'present_defect', status:'rejected', references:['primary']},
  {predicate:'resolved_production_element', status:'confirmed', references:['production_element']},
  {predicate:'complete_searches', status:'confirmed', references:['searched_paths:0']},
  {predicate:'complete_behavioral_absence', status:'confirmed', references:['production_element','searched_paths:0']},
];
const score = { finding_id:'gap', confidence:80, policy_subject:'ordinary', admission_assessments:entries };
const validationSkipped = ['confidence','policy_subject'].includes(missing);
if (validationSkipped) delete score[missing];
else score.admission_assessments = entries.filter(e=>e.predicate !== missing);
const args = validArgs({contextLines:20, contextChars:400, checkpoints:{verify:{findings:[finding], gaps:[]}}});
let payload;
const ctx = makeCtx(args, {onPersist: value=>{payload=value;}});
const agent = ctx.agent;
ctx.agent = async (prompt, dispatch) => {
  if (/^validate-(batch|retry)-/.test(dispatch.label)) {
    ctx.calls.push({prompt,...dispatch});
    return {validations:[score]};
  }
  return agent(prompt, dispatch);
};
const out = await runWith(ctx, args);
assert.equal(out.ok, true);
assert.deepEqual(ctx.calls.filter(c=>c.agentType==='code-gauntlet:validator').map(c=>c.label),
  ['validate-batch-0','validate-retry-0']);
const held = payload.findings.find(f=>f.id==='gap');
assert.equal(held.disposition,'held_back');
assert.deepEqual(payload.postReview,[]);
assert.equal(held.validation,validationSkipped?'skipped':'validated');
assert.equal(out.stats.validate.adjusted,validationSkipped?0:1);
assert.equal(held.disposition_reasons.includes('validation_skipped'),validationSkipped);
assert.equal(out.stats.validate.skipped,validationSkipped?1:0);
assert.equal(out.stats.challenge.skipped,0);
if (validationSkipped) {
  assert.equal(out.gaps.length,1);
  assert.match(out.gaps[0],/validation=skipped/);
  assert.doesNotMatch(out.gaps[0],/missing_assessment/);
  assert.match(payload.report,/validator did not score/);
} else {
  assert.deepEqual(out.gaps,[]);
  assert.equal(held.validator_confidence,80);
  assert.equal(held.admission.status,'held_back');
  assert.deepEqual(held.admission.reasons,[{predicate:missing,status:'held_back'}]);
  assert.match(payload.report,/held back by admission/);
  assert.ok(payload.report.includes(`admission predicates not established: ${missing}`));
  assert.doesNotMatch(payload.report,/pipeline-degraded|stage was skipped or failed|not skeptically validated|validation=skipped/);
}
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(missing)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
