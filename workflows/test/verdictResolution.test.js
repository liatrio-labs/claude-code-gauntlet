import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyChallenges } from '../src/applyChallenges.js';
import { routeVerdict } from '../src/verdict.js';
import { evaluateAdmission } from '../src/admission.js';
import { disposeFindings } from '../src/dispose.js';
import { VERDICT_RESOLUTIONS } from '../src/registry.js';
import { makeFinding } from './helpers/pipelineMock.js';

const states = ['confirmed', 'refuted_verified', 'refuted_unverified', 'not_established'];
const choices = [ ['stronger', 'challenger', 'validator', 'validator'], ['validator', 'challenger', 'validator', 'validator'],
  ['challenger', 'challenger', 'challenger', 'challenger'], ['challenger', 'challenger', 'challenger', 'challenger'] ];
function answer(state, reader) {
  return state === 'confirmed' ? { claim: state, consequence: 'present_failure', justification: reader }
    : state === 'not_established' ? { claim: state, missing_fact: 'Which caller?', justification: reader }
      : { claim: 'refuted', justification: reader, refutation_kind: 'guard_exists', refutation: { file: 'guard', line_start: 1, line_end: 1, anchor: 'return safe;' } };
}
for (const [i, validator] of states.entries()) for (const [j, challenger] of states.entries()) for (const dimension of ['bug', 'security']) {
  test(`resolution pair ${dimension}: ${validator}/${challenger}`, () => {
    const v = answer(validator, 'validator'), c = answer(challenger, 'challenger');
    const vcheck = { verified: validator === 'refuted_verified' }, ccheck = { verified: challenger === 'refuted_verified' };
    const f = makeFinding('f', { dimension, validator_verdict: v, verdict: v, validator_refutation_check: vcheck });
    routeVerdict(f); evaluateAdmission(f);
    const out = applyChallenges([f], [{ ...c, id: 'f', refutation_check: ccheck }]);
    const result = [...out.findings, ...out.eliminated][0];
    const expected = choices[i][j] === 'validator' ? v : c;
    assert.deepEqual(result.verdict, expected);
    assert.deepEqual(result.validator_verdict, v);
    assert.deepEqual(result.challenge_verdict, c);
    const verifiedCount = Number(vcheck.verified) + Number(ccheck.verified);
    const route = dimension === 'security' && verifiedCount === 1 ? 'report-only'
      : verifiedCount ? 'eliminated' : expected.claim === 'confirmed' ? 'posted' : 'not-established';
    assert.equal(result.verdict_route, route);
    assert.equal(out.eliminated.length, route === 'eliminated' ? 1 : 0);
    if (route === 'eliminated') assert.equal(result.elimination_reason, expected.justification);
    assert.equal(VERDICT_RESOLUTIONS[validator][challenger], choices[i][j] === 'stronger' ? 'more_serious' : choices[i][j]);
  });
}

for (const consequence of ['present_failure', 'latent', 'artifact_defect']) for (const [fields, status] of [
  [{ test_kind: 'defective_test', path_role: 'test', diff_validation: { in_diff: true } }, 'admitted'],
  [{ test_kind: 'defective_test', path_role: 'test', diff_validation: { in_diff: false } }, 'report_only'],
  [{ test_kind: 'defective_test', path_role: 'source', diff_validation: { in_diff: true } }, 'report_only'],
  [{ test_kind: 'defective_test', path_role: 'test', diff_validation: { in_diff: null } }, 'held_back'],
  [{ test_kind: 'additional_case', path_role: 'test' }, 'report_only'],
]) test(`deterministic kind admission for ${consequence}/${JSON.stringify(fields)}`, () => {
  const verdict = { claim: 'confirmed', consequence, justification: 'Inspected test defect', ...(consequence === 'latent' ? { latent_condition: 'Enable the other path' } : {}) };
  const f = makeFinding('f', { dimension: 'test_coverage', ...fields, verdict, validator_verdict: verdict });
  routeVerdict(f); evaluateAdmission(f);
  const applied = applyChallenges([f], [{ ...verdict, id: 'f' }]);
  assert.equal(applied.findings[0].admission.status, status);
  const disposed = disposeFindings(applied);
  assert.equal([...disposed.findings, ...disposed.unverified][0].disposition, status === 'admitted' ? 'deliver' : status);
});

import { validateStage, challengeStage } from '../src/reviewStages.js';
import { VERDICT_PROPERTIES } from '../src/registry.js';
const parallel = (thunks) => Promise.all(thunks.map((thunk) => thunk()));
const validRefutation = answer('refuted_verified', 'validator');
for (const [name, change] of [
  ['unknown field', (v) => ({ ...v, route: 'posted' })],
  ['prototype-named field', (v) => ({ ...v, toString: 'model text' })],
  ['empty justification', (v) => ({ ...v, justification: '' })],
  ['blank justification', (v) => ({ ...v, justification: '   ' })],
  ['zero source line', (v) => ({ ...v, refutation: { ...v.refutation, line_start: 0 } })],
  ['fractional source line', (v) => ({ ...v, refutation: { ...v.refutation, line_start: 1.5 } })],
  ['missing quote', (v) => ({ ...v, refutation: { file: 'source', line_start: 1, line_end: 1 } })],
  ['extra citation field', (v) => ({ ...v, refutation: { ...v.refutation, intentional: true } })],
  ['long quote', (v) => ({ ...v, refutation: { ...v.refutation, anchor: 'x'.repeat(VERDICT_PROPERTIES.refutation.properties.anchor.maxLength + 1) } })],
  ['nine quote lines', (v) => ({ ...v, refutation: { ...v.refutation, anchor: Array(9).fill('line').join('\n') } })],
  ['null site confirmations', (v) => ({ ...v, site_confirmations: null })],
]) test(`validator boundary rejects schema-invalid ${name}`, async () => {
  const out = await validateStage({ parallel, agent: async () => ({ validations: [{ ...change(validRefutation), finding_id: 'f' }] }) }, { findings: [makeFinding('f')] });
  assert.equal(out.stats.skipped, 1);
  assert.equal(out.findings[0].validation, 'skipped');
});

test('a successful challenger does not relabel a skipped validator', async () => {
  const ctx = { parallel, agent: async (_prompt, options) => options.agentType === 'code-gauntlet:validator' ? null : { verdict: answer('confirmed', 'challenger') } };
  const validated = await validateStage(ctx, { findings: [makeFinding('f')] });
  const out = disposeFindings(await challengeStage(ctx, { findings: validated.findings.map(evaluateAdmission) }));
  assert.equal(out.unverified[0].validation, 'skipped');
  assert.equal(out.unverified[0].challenge_verdict.claim, 'confirmed');
  assert.ok(out.unverified[0].disposition_reasons.includes('validation_skipped'));
});

for (const consequence of ['present_failure', 'latent', 'artifact_defect']) test(`typed ${consequence} retains reference-resolution facts without semantic assessments`, () => {
  const v = { claim: 'confirmed', consequence, justification: 'Inspected coverage gap', ...(consequence === 'latent' ? { latent_condition: 'Other input' } : {}) };
  const base = { dimension: 'test_coverage', test_kind: 'critical_gap', criticality: 9,
    candidate_tests: [], resolved_references: { searched_paths: [{ available: true }], candidate_tests: [] },
    verdict: v, validator_verdict: v };
  const cases = [
    [{}, 'report_only'],
    [{ production_element: { file: 'production', line_start: 1, line_end: 1 } }, 'held_back'],
    [{ resolved_references: { ...base.resolved_references, production_element: { file: 'production', line_start: 1, line_end: 1, location_status: 'confirmed' } } }, 'admitted'],
  ];
  for (const [fields, status] of cases) {
    const f = makeFinding('f', { ...base, ...fields });
    routeVerdict(f); evaluateAdmission(f);
    const out = disposeFindings(applyChallenges([f], [{ id: 'f', ...v }]));
    assert.equal([...out.findings, ...out.unverified][0].disposition, status === 'admitted' ? 'deliver' : status);
  }
});
