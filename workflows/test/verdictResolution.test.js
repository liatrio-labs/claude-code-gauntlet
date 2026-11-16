import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyChallenges } from '../src/applyChallenges.js';
import { routeVerdict } from '../src/verdict.js';
import { evaluateAdmission } from '../src/admission.js';
import { disposeFindings } from '../src/dispose.js';
import { makeFinding } from './helpers/pipelineMock.js';

const outcomes = [
  ['confirmed', 'confirmed', ['challenger', 'posted', 0], ['challenger', 'posted', 0]],
  ['confirmed', 'refuted_verified', ['challenger', 'eliminated', 1], ['challenger', 'report-only', 0]],
  ['confirmed', 'refuted_unverified', ['validator', 'posted', 0], ['validator', 'posted', 0]],
  ['confirmed', 'not_established', ['validator', 'posted', 0], ['validator', 'posted', 0]],
  ['refuted_verified', 'confirmed', ['validator', 'eliminated', 1], ['validator', 'report-only', 0]],
  ['refuted_verified', 'refuted_verified', ['challenger', 'eliminated', 1], ['challenger', 'eliminated', 1]],
  ['refuted_verified', 'refuted_unverified', ['validator', 'eliminated', 1], ['validator', 'report-only', 0]],
  ['refuted_verified', 'not_established', ['validator', 'eliminated', 1], ['validator', 'report-only', 0]],
  ['refuted_unverified', 'confirmed', ['challenger', 'posted', 0], ['challenger', 'posted', 0]],
  ['refuted_unverified', 'refuted_verified', ['challenger', 'eliminated', 1], ['challenger', 'report-only', 0]],
  ['refuted_unverified', 'refuted_unverified', ['challenger', 'not-established', 0], ['challenger', 'not-established', 0]],
  ['refuted_unverified', 'not_established', ['challenger', 'not-established', 0], ['challenger', 'not-established', 0]],
  ['not_established', 'confirmed', ['challenger', 'posted', 0], ['challenger', 'posted', 0]],
  ['not_established', 'refuted_verified', ['challenger', 'eliminated', 1], ['challenger', 'report-only', 0]],
  ['not_established', 'refuted_unverified', ['challenger', 'not-established', 0], ['challenger', 'not-established', 0]],
  ['not_established', 'not_established', ['challenger', 'not-established', 0], ['challenger', 'not-established', 0]],
];
function answer(state, reader) {
  return state === 'confirmed' ? { claim: state, consequence: 'present_failure', justification: reader }
    : state === 'not_established' ? { claim: state, missing_fact: 'Which caller?', justification: reader }
      : { claim: 'refuted', justification: reader, refutation_kind: 'guard_exists', refutation: { file: 'guard', line_start: 1, line_end: 1, anchor: 'return safe;' } };
}
for (const [validator, challenger, bug, security] of outcomes) for (const [dimension, expectedRow] of [['bug', bug], ['security', security]]) {
  test(`resolution pair ${dimension}: ${validator}/${challenger}`, () => {
    const v = answer(validator, 'validator'), c = answer(challenger, 'challenger');
    const vcheck = { verified: validator === 'refuted_verified' }, ccheck = { verified: challenger === 'refuted_verified' };
    const f = makeFinding('f', { dimension, validator_verdict: v, verdict: v, validator_refutation_check: vcheck });
    routeVerdict(f); evaluateAdmission(f);
    const out = applyChallenges([f], [{ ...c, id: 'f', refutation_check: ccheck }]);
    const result = [...out.findings, ...out.eliminated][0];
    const [reader, route, removed] = expectedRow;
    const expected = { validator: v, challenger: c }[reader];
    assert.deepEqual(result.verdict, expected);
    assert.deepEqual(result.validator_verdict, v);
    assert.deepEqual(result.challenge_verdict, c);
    assert.equal(result.verdict_route, route);
    assert.equal(out.eliminated.length, removed);
    if (removed) assert.equal(result.elimination_reason, expected.justification);
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

test('a successful challenger recovers a skipped validator without relabelling it', async () => {
  const ctx = { parallel, agent: async (_prompt, options) => options.agentType === 'code-gauntlet:validator' ? null : { verdict: answer('confirmed', 'challenger') } };
  const validated = await validateStage(ctx, { findings: [makeFinding('f')] });
  const out = disposeFindings(await challengeStage(ctx, { findings: validated.findings.map(evaluateAdmission) }));
  assert.deepEqual(out.unverified, []);
  assert.equal(out.findings[0].validation, 'skipped');
  assert.equal(out.findings[0].challenge_verdict.claim, 'confirmed');
  assert.equal(out.findings[0].validator_verdict, undefined);
  assert.equal(out.findings[0].disposition, 'deliver');
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

for (const scope of ['outer', 'inner']) for (const [field, value] of [['route', 'posted'], ['confidence', 100], ['validation', 'validated']]) {
  test(`challenger rejects untrusted extra ${scope}/${field} at intake`, async () => {
    const f = makeFinding('f');
    const out = disposeFindings(await challengeStage({ parallel,
      agent: async () => scope === 'inner' ? { verdict: { ...answer('confirmed', 'challenger'), [field]: value } }
        : { verdict: answer('confirmed', 'challenger'), [field]: value },
    }, { findings: [f] }));
    assert.equal(out.stats.completed, 0);
    assert.equal(out.stats.skipped, 1);
    assert.equal(out.unverified[0].challenge_verdict, undefined);
    assert.equal(out.unverified[0].disposition, 'held_back');
    assert.ok(out.unverified[0].disposition_reasons.includes('challenge_failed'));
  });
}

for (const [invalid, reason] of [
  [{ claim: 'confirmed', justification: 'No consequence' }, '$.consequence: required'],
  [{ ...validRefutation, refutation: { ...validRefutation.refutation, line_start: 0 } }, '$.refutation.line_start: minimum'],
]) test(`validator diagnostics retain ${reason}`, async () => {
  const out = await validateStage({ parallel, agent: async () => ({ validations: [{ ...invalid, finding_id: 'f' }] }) }, { findings: [makeFinding('f')] });
  assert.deepEqual(out.audit.batches[0].rejections[0].reasons, [reason]);
  assert.ok(out.gaps[0].includes(reason));
});

import { schemaErrors } from '../src/schema.js';
test('runtime schemas fail closed on unsupported constraints', () => {
  assert.deepEqual(schemaErrors({}, { type: 'object', maxProperties: 0 }), ['$: unsupported keyword maxProperties']);
});

test('untyped checkpoints cannot use legacy semantic assessments to grant admission', () => {
  const f = makeFinding('legacy', { policy_subject: 'ordinary', validation: 'validated',
    admission_assessments: [{ predicate: 'present_defect', status: 'confirmed', references: ['primary'] }] });
  delete f.verdict;
  delete f.verdict_route;
  evaluateAdmission(f);
  assert.equal(f.admission.status, 'held_back');
  assert.deepEqual(f.admission.reasons, [{ predicate: 'verdict', status: 'held_back' }]);
});

for (const [consequence, disposition] of [['latent', 'deliver'], ['artifact_defect', 'deliver'], ['no_practical_effect', 'report_only']]) {
  test(`sole challenger establishes ${consequence} after validator outage`, async () => {
    const result = { claim: 'confirmed', consequence, justification: 'Independent source inspection',
      ...(consequence === 'latent' ? { latent_condition: 'Other input' } : {}) };
    const ctx = { parallel, agent: async (_prompt, options) => options.agentType === 'code-gauntlet:validator' ? null : { verdict: result } };
    const validated = await validateStage(ctx, { findings: [makeFinding('f')] });
    const out = disposeFindings(await challengeStage(ctx, { findings: validated.findings.map(evaluateAdmission) }));
    assert.deepEqual(out.unverified, []);
    assert.equal(out.findings[0].disposition, disposition);
    assert.equal(out.findings[0].validation, 'skipped');
    assert.equal(out.findings[0].validator_verdict, undefined);
    assert.deepEqual(out.findings[0].challenge_verdict, result);
  });
}
