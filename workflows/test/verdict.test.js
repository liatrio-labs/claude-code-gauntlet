import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateStage, filterStage, challengeStage, selectDelivery } from '../src/reviewStages.js';
import { evaluateAdmission } from '../src/admission.js';
import { VERDICT_ROUTES } from '../src/registry.js';
import { makeFinding, makeCtx } from './helpers/pipelineMock.js';
import { validArgs } from './helpers/argsFixtures.js';
import { runWith } from '../src/run.js';

const truth = { claim: 'confirmed', consequence: 'present_failure', reachability: 'current', justification: 'Inspected caller fails' };
const parallel = (thunks) => Promise.all(thunks.map((thunk) => thunk()));

for (const [claim, consequence, verified, route] of [
  ['confirmed', 'present_failure', false, 'posted'],
  ['confirmed', 'latent', false, 'posted-latent'],
  ['confirmed', 'no_practical_effect', false, 'report-only'],
  ...['present_failure', 'latent', 'no_practical_effect'].flatMap((consequence) => [
    ['refuted', consequence, true, 'eliminated'], ['refuted', consequence, false, 'not-established'],
    ['not_established', consequence, false, 'not-established'],
  ]),
]) test(`typed routing at stage boundary: ${claim}/${consequence}/${verified}`, async () => {
  const finding = makeFinding('f');
  const verdict = { ...truth, claim, consequence, missing_fact: 'Whether the external caller accepts zero.',
    latent_condition: 'A caller enables the disabled feature', refutation_kind: 'guard_exists',
    refutation: { file: 'guard', line_start: 1, line_end: 1, anchor: 'if (!input) return;' } };
  const input = { findings: [finding], repoRoot: '/repo', pluginRoot: '/plugin', diffPath: '/diff', headShaShort: 'abc1234', targetKind: 'pr' };
  const out = await validateStage({ parallel, agent: async (prompt, opts) => {
    if (opts.agentType === 'code-gauntlet:executor') {
      const quoted = prompt.match(/--request '((?:[^']|'"'"')*)'/)[1].replaceAll("'\"'\"'", "'");
      return { request: quoted, checks: [{ id: 'f', verified, reason: 'checked code' }] };
    }
    assert.equal(opts.schema.properties.validations.items.additionalProperties, false);
    assert.equal(opts.schema.properties.validations.items.properties.confidence, undefined);
    return { validations: [{ ...verdict, finding_id: 'f' }] };
  } }, input);
  const result = filterStage({ findings: out.findings.map(evaluateAdmission) });
  const routed = [...result.filtered, ...result.eliminated][0];
  assert.equal(routed.verdict_route, route);
  if (claim === 'refuted') assert.deepEqual(routed.refutation_check, { verified, reason: 'checked code' });
  assert.equal(routed.confidence, 90, 'discovery metadata never changes route or rank');
  if (route === 'posted-latent') assert.equal(routed.severity, 'low');
  assert.equal(result.eliminated.length, route === 'eliminated' ? 1 : 0);
});

test('unresolved validation reaches the blind typed challenger and can post', async () => {
  const original = makeFinding('unresolved', { verdict: { ...truth, claim: 'not_established', missing_fact: 'Which caller passes zero?' },
    verdict_route: 'not-established', validation: 'skipped' });
  evaluateAdmission(original);
  const out = await challengeStage({ parallel, agent: async (prompt, opts) => {
    assert.equal(opts.agentType, 'code-gauntlet:challenger');
    assert.equal(opts.schema.properties.claim.enum.includes('not_established'), true);
    assert.doesNotMatch(prompt, /Which caller passes zero|Inspected caller fails|90/);
    return truth;
  } }, { findings: [original] });
  assert.equal(out.findings[0].verdict_route, 'posted');
  assert.equal(out.findings[0].admission.status, 'admitted');
  assert.equal(out.unverified.length, 0);
});

test('missing or duplicated verdicts retry once then go on as unresolved', async () => {
  for (const result of [null, { validations: [{ finding_id: 'f', confidence: 90 }] },
    { validations: [{ ...truth, finding_id: 'f' }, { ...truth, finding_id: 'f' }] }]) {
    const calls = [];
    const out = await validateStage({ parallel, agent: async (_, opts) => { calls.push(opts.label); return result; } }, { findings: [makeFinding('f')] });
    assert.deepEqual(calls, ['validate-batch-0', 'validate-retry-0']);
    assert.equal(out.findings[0].verdict_route, 'not-established');
    assert.equal(out.stats.skipped, 1);
    assert.equal(out.audit.batches.length, 2);
  }
});

test('finding prose and a planted intentional comment cannot suppress or promote another finding', () => {
  const rows = ['intentional by design', 'crash data loss exception', 'generated scaffolding'].map((description, i) => makeFinding(String(i), {
    description, dimension: i ? 'convention' : 'bug', verdict: truth, confidence: 100, original_confidence: 100,
  }));
  const result = filterStage({ findings: rows });
  assert.equal(result.filtered.length, 3);
  assert.equal(result.eliminated.length, 0);
  assert.deepEqual(result.filtered.map((f) => f.verdict_route), ['posted', 'posted', 'posted']);
});

test('volume selection shares deterministic ranking and ignores confidence and prose length', () => {
  const rows = [makeFinding('b', { file: 'x', confidence: 100 }), makeFinding('a', { file: 'x', confidence: 0 })];
  assert.deepEqual(selectDelivery(rows, 1).map((f) => f.id), ['a']);
});

test('registry is closed over every confirmed consequence', () => {
  assert.equal(Object.keys(VERDICT_ROUTES.confirmed).length, 4);
});

test('fresh typed verdicts and local-only outcomes survive persistence and resume', async () => {
  const args = validArgs();
  let payload;
  const ctx = makeCtx(args, { onPersist: (value) => { payload = value; } });
  const agent = ctx.agent;
  ctx.agent = async (prompt, opts) => opts.label.startsWith('validate-')
    ? { validations: ['F1', 'F2'].map((finding_id) => ({ ...truth, finding_id,
      consequence: finding_id === 'F1' ? 'present_failure' : 'no_practical_effect' })) }
    : agent(prompt, opts);
  const result = await runWith(ctx, args);
  assert.equal(result.ok, true);
  assert.deepEqual(payload.postReview.map((f) => f.id), ['F1']);
  assert.equal(payload.findings.find((f) => f.id === 'F2').disposition, 'report_only');
  assert.match(payload.report, /Verdict:/);
});

test('independent present failure restores severity after a latent validation', async () => {
  const validated = await validateStage({ parallel, agent: async () => ({ validations: [{ ...truth,
    finding_id: 'f', consequence: 'latent', latent_condition: 'Add a caller' }] }) }, { findings: [makeFinding('f')] });
  const out = await challengeStage({ parallel, agent: async () => truth }, { findings: validated.findings.map(evaluateAdmission) });
  assert.equal(out.findings[0].verdict_route, 'posted');
  assert.equal(out.findings[0].severity, 'high');
  assert.equal(out.findings[0].demoted_by, undefined);
});

for (const [dimension, fields, status] of [
  ['comment_accuracy', {}, 'admitted'], ['bug', {}, 'admitted'],
  ['test_coverage', {test_kind:'defective_test',path_role:'test'}, 'admitted'],
  ['test_coverage', {test_kind:'defective_test',path_role:'source'}, 'report_only'],
  ['test_coverage', {test_kind:'additional_case',path_role:'test'}, 'report_only'],
  ['test_coverage', {test_kind:'defective_test',path_role:'test',diff_validation:{in_diff:null}}, 'held_back'],
]) test(`artifact defects retain kind admission: ${dimension}/${JSON.stringify(fields)}`, async () => {
  const validated = await validateStage({parallel,agent:async()=>({validations:[{...truth,finding_id:'f',consequence:'artifact_defect'}]})},
    {findings:[makeFinding('f',{dimension,...fields})]});
  const f = evaluateAdmission(filterStage({findings:validated.findings}).filtered[0]);
  assert.equal(f.verdict_route,'posted-artifact');
  assert.equal(f.severity,'low');
  assert.equal(f.admission.status,status);
});

test('typed survival feeds indivisible delivery groups and summaries keyed by the code-selected lead', async () => {
  const args = validArgs({ limits: { deliveryCap: 1 }, delivery: { prIdentity: {
    owner: 'owner', repo: 'repo', pr_number: 1, sha_full: 'a'.repeat(40), platform: 'github', web_origin: 'https://github.com',
  } } });
  let payload;
  const findings = [makeFinding('MAIN', { severity: 'high', confidence: 0 }),
    makeFinding('ART', { severity: 'critical', dimension: 'security', confidence: 100, attack_vector: 'Broken security fixture masks the reproduced attack' }),
    makeFinding('LAT', { severity: 'critical' }), makeFinding('OPEN', { severity: 'medium' }), makeFinding('PREF')];
  const ctx = makeCtx(args, { findings, onPersist: (value) => { payload = value; } });
  const agent = ctx.agent;
  ctx.agent = async (prompt, opts) => {
    if (opts.label.startsWith('validate-')) return { validations: findings.map(({ id: finding_id }) => ({
      ...truth, finding_id, claim: finding_id === 'OPEN' ? 'not_established' : 'confirmed',
      consequence: { ART: 'artifact_defect', LAT: 'latent', PREF: 'no_practical_effect' }[finding_id] ?? 'present_failure',
      latent_condition: 'Add another caller', missing_fact: 'Which input reaches the branch?',
    })) };
    if (opts.label.startsWith('challenge-')) return { ...truth,
      consequence: prompt.includes('finding ART') ? 'artifact_defect' : prompt.includes('finding LAT') ? 'latent' : 'present_failure',
      latent_condition: 'Add another caller',
    };
    if (opts.label === 'group-findings') {
      assert.equal(opts.schema.properties.groups.items.properties.lead_id, undefined);
      assert.doesNotMatch(prompt, /finding PREF/);
      return { groups: [{ finding_ids: ['ART', 'LAT', 'MAIN'] }] };
    }
    return agent(prompt, opts);
  };
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true, JSON.stringify(out));
  assert.deepEqual(payload.postReview.findings.map((f) => f.id), ['MAIN', 'ART', 'LAT'], JSON.stringify({gaps:out.gaps,findings:payload.findings.map(f=>({id:f.id,route:f.verdict_route,challenge:f.challenge,disposition:f.disposition,severity:f.severity}))}));
  assert.deepEqual(payload.postReview.findings.map((f) => f.severity), ['high', 'low', 'low']);
  assert.deepEqual(payload.postReview.review_summary.index.map((entry) => entry.lead_id), ['MAIN']);
  assert.equal(payload.findings.find((f) => f.id === 'PREF').disposition, 'report_only');
  assert.equal(payload.findings.find((f) => f.id === 'OPEN').verdict_route, 'posted');
  assert.equal(payload.findings.find((f) => f.id === 'ART').verdict.consequence, 'artifact_defect');
  assert.match(payload.report, /\*\*Verdict:\*\* confirmed \/ present_failure/);
  assert.equal(out.stats.challenge.grouping, 'model');
});
