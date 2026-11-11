import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateStage, filterStage, challengeStage, selectDelivery } from '../src/reviewStages.js';
import { VERDICT_ROUTES, VERDICT_PROPERTIES } from '../src/registry.js';
import { disposeFindings } from '../src/dispose.js';
import { evaluateAdmission } from '../src/admission.js';
import { makeFinding, makeCtx } from './helpers/pipelineMock.js';
import { validArgs } from './helpers/argsFixtures.js';
import { runWith } from '../src/run.js';

const truth = { claim: 'confirmed', consequence: 'present_failure', justification: 'Inspected caller fails' };
const parallel = (thunks) => Promise.all(thunks.map((thunk) => thunk()));

for (const [claim, consequence, verified, route] of [
  ['confirmed', 'present_failure', false, 'posted'],
  ['confirmed', 'latent', false, 'posted-latent'],
  ['confirmed', 'no_practical_effect', false, 'report-only'],
  ['refuted', undefined, true, 'eliminated'], ['refuted', undefined, false, 'not-established'],
  ['not_established', undefined, false, 'not-established'],
]) test(`typed routing at stage boundary: ${claim}/${consequence}/${verified}`, async () => {
  const finding = makeFinding('f');
  const verdict = { claim, justification: 'Inspected caller',
    ...(claim === 'confirmed' ? { consequence, ...(consequence === 'latent' ? { latent_condition: 'Add a caller' } : {}) }
      : claim === 'refuted' ? { refutation_kind: 'guard_exists', refutation: { file: 'guard', line_start: 1, line_end: 1, anchor: 'if (!input) return;' } }
        : { missing_fact: 'Which caller passes zero?' }) };
  const input = { findings: [finding], repoRoot: '/repo', pluginRoot: '/plugin', diffPath: '/diff', headShaShort: 'abc1234', targetKind: 'pr' };
  const out = await validateStage({ parallel, agent: async (prompt, opts) => {
    if (opts.agentType === 'code-gauntlet:executor') {
      const quoted = prompt.match(/--request '((?:[^']|'"'"')*)'/)[1].replaceAll("'\"'\"'", "'");
      return { request: quoted, checks: [{ id: 'f', verified, reason: 'checked code' }] };
    }
    assert.equal(opts.schema.properties.validations.items.additionalProperties, false);
    const schema = opts.schema.properties.validations.items;
    assert.equal(schema.properties.confidence, undefined);
    assert.equal(schema.properties.reachability, undefined);
    assert.ok(Array.isArray(schema.oneOf));
    const branch = schema.oneOf.find((row) => row.properties.claim.const === claim);
    assert.equal(Object.hasOwn(branch.properties, 'consequence'), claim === 'confirmed');
    if (claim === 'refuted') {
      assert.equal(branch.properties.refutation.properties.anchor.maxLength, 400);
      assert.equal(new RegExp(branch.properties.refutation.properties.anchor.pattern).test(Array(9).fill('line').join('\n')), false);
    }
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
  const original = makeFinding('unresolved', { verdict: { claim: 'not_established', justification: 'Caller unavailable', missing_fact: 'Which caller passes zero?' },
    verdict_route: 'not-established', validation: 'skipped' });
  evaluateAdmission(original);
  const out = await challengeStage({ parallel, agent: async (prompt, opts) => {
    assert.equal(opts.agentType, 'code-gauntlet:challenger');
    assert.equal(opts.schema.properties.verdict.properties.claim.enum.includes('not_established'), true);
    assert.doesNotMatch(prompt, /Which caller passes zero|Inspected caller fails|90/);
    return { verdict: truth };
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

test('co-located convention prose cannot suppress a bug or change a typed destination', () => {
  for (const prose of ['intentional by design', 'generated scaffolding', 'crash data loss exception']) {
    const rows = [makeFinding('BUG', { file: 'same', line_start: 10, agent: 'bug-detector', verdict: truth }),
      makeFinding('TEST', { file: 'same', line_start: 10, agent: 'test-analyzer', dimension: 'test_coverage', test_kind: 'defective_test', verdict: truth }),
      makeFinding('INTENT', { file: 'same', line_start: 10, dimension: 'intent', agent: 'conventions-and-intent',
        description: prose, verdict: { ...truth, consequence: 'latent', latent_condition: 'Another caller' } })];
    const result = filterStage({ findings: rows });
    assert.deepEqual(result.filtered.map((f) => [f.id, f.verdict_route, f.report_destination]),
      [['BUG', 'posted', 'main'], ['TEST', 'posted', 'main'], ['INTENT', 'posted-latent', 'suggestion']]);
    assert.deepEqual(result.eliminated, []);
  }
});

test('volume selection shares deterministic ranking and ignores confidence and prose length', () => {
  const rows = [makeFinding('b', { file: 'x', confidence: 100 }), makeFinding('a', { file: 'x', confidence: 0 })];
  assert.deepEqual(selectDelivery(rows, 1).map((f) => f.id), ['a']);
});

test('fresh typed verdicts and local-only outcomes survive persistence and resume', async () => {
  const args = validArgs();
  let payload;
  const ctx = makeCtx(args, { onPersist: (value) => { payload = value; } });
  const agent = ctx.agent;
  ctx.agent = async (prompt, opts) => opts.label.startsWith('validate-')
    ? { validations: ['F1', 'F2'].map((finding_id) => ({ ...truth, finding_id,
      consequence: finding_id === 'F1' ? 'present_failure' : 'no_practical_effect' })) }
    : opts.label.startsWith('challenge-') && prompt.includes('finding F2')
      ? { verdict: { ...truth, consequence: 'no_practical_effect' } } : agent(prompt, opts);
  const result = await runWith(ctx, args);
  assert.equal(result.ok, true);
  assert.deepEqual(payload.postReview.map((f) => f.id), ['F1']);
  assert.equal(payload.findings.find((f) => f.id === 'F2').disposition, 'report_only');
  assert.match(payload.report, /Verdict:/);
});

test('independent present failure restores severity after a latent validation', async () => {
  const validated = await validateStage({ parallel, agent: async () => ({ validations: [{ ...truth,
    finding_id: 'f', consequence: 'latent', latent_condition: 'Add a caller' }] }) }, { findings: [makeFinding('f')] });
  const out = await challengeStage({ parallel, agent: async () => ({ verdict: truth }) }, { findings: validated.findings.map(evaluateAdmission) });
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
      finding_id, justification: 'Inspected caller',
      ...(finding_id === 'OPEN' ? { claim: 'not_established', missing_fact: 'Which input reaches the branch?' } : {
        claim: 'confirmed', consequence: { ART: 'artifact_defect', LAT: 'latent', PREF: 'no_practical_effect' }[finding_id] ?? 'present_failure',
        latent_condition: 'Add another caller',
      }),
    })) };
    if (opts.label.startsWith('challenge-')) return { verdict: { ...truth,
      consequence: prompt.includes('finding PREF') ? 'no_practical_effect' : prompt.includes('finding ART') ? 'artifact_defect' : prompt.includes('finding LAT') ? 'latent' : 'present_failure',
      latent_condition: 'Add another caller',
    } };
    if (opts.label === 'group-findings') {
      assert.equal(opts.schema.properties.groups.items.properties.lead_id, undefined);
      assert.doesNotMatch(prompt, /finding PREF/);
      return { groups: [{ finding_ids: ['ART', 'LAT', 'MAIN'] }] };
    }
    return agent(prompt, opts);
  };
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true, JSON.stringify(out));
  assert.deepEqual(payload.postReview.findings.map((f) => f.id), ['MAIN', 'LAT', 'ART'], JSON.stringify({gaps:out.gaps,findings:payload.findings.map(f=>({id:f.id,route:f.verdict_route,challenge:f.challenge,disposition:f.disposition,severity:f.severity}))}));
  assert.deepEqual(payload.postReview.findings.map((f) => f.severity), ['high', 'low', 'low']);
  assert.deepEqual(payload.postReview.review_summary.index.map((entry) => entry.lead_id), ['MAIN']);
  assert.equal(payload.findings.find((f) => f.id === 'PREF').disposition, 'report_only');
  assert.equal(payload.findings.find((f) => f.id === 'OPEN').verdict_route, 'posted');
  assert.equal(payload.findings.find((f) => f.id === 'ART').verdict.consequence, 'artifact_defect');
  assert.match(payload.report, /\*\*Verdict:\*\* confirmed \/ present_failure/);
  assert.equal(out.stats.challenge.grouping, 'model');
});

const refuted = { claim: 'refuted', justification: 'Existing guard prevents it', refutation_kind: 'guard_exists',
  refutation: { file: 'guard', line_start: 1, line_end: 1, anchor: 'if (!input) return;' } };
const unresolved = { claim: 'not_established', justification: 'Caller unavailable', missing_fact: 'Which caller supplies zero?' };

for (const [validator, challenger, vcheck, ccheck, route] of [
  [refuted, refuted, true, true, 'eliminated'], [refuted, refuted, false, true, 'report-only'],
  [refuted, truth, true, false, 'report-only'], [truth, refuted, false, true, 'report-only'],
  [refuted, unresolved, true, false, 'report-only'], [unresolved, truth, false, false, 'posted'],
]) test(`security floor uses both independent verdicts: ${validator.claim}/${challenger.claim}/${vcheck}/${ccheck}`, async () => {
  const ctx = { parallel, agent: async (prompt, opts) => {
    if (opts.agentType === 'code-gauntlet:executor') {
      const request = prompt.match(/--request '((?:[^']|'"'"')*)'/)[1].replaceAll("'\"'\"'", "'");
      return { request, checks: [{ id: 'SEC', verified: opts.label === 'validate-refutations' ? vcheck : ccheck, reason: 'source checked' }] };
    }
    return opts.agentType === 'code-gauntlet:validator' ? { validations: [{ ...validator, finding_id: 'SEC' }] } : { verdict: challenger };
  } };
  const input = { findings: [makeFinding('SEC', { dimension: 'security' })], repoRoot: '/repo', pluginRoot: '/plugin',
    diffPath: '/diff', headShaShort: 'abc1234', targetKind: 'pr' };
  const first = await validateStage(ctx, input);
  const filtered = filterStage({ findings: first.findings.map(evaluateAdmission) });
  assert.equal(filtered.eliminated.length, 0);
  const final = await challengeStage(ctx, { ...input, findings: filtered.filtered });
  const f = [...final.findings, ...final.eliminated, ...final.unverified][0];
  assert.equal(f.verdict_route, route);
  assert.equal(final.eliminated.length, route === 'eliminated' ? 1 : 0);
});

test('malformed site returns isolate a finding and unconfirmed ranges are explicit data', async () => {
  const out = await validateStage({ parallel, agent: async (prompt) => {
    assert.match(prompt, /f:4-9 \| location_status=unconfirmed/);
    return { validations: [{ ...truth, finding_id: 'bad', site_confirmations: {} }, { ...truth, finding_id: 'good' }] };
  } }, { findings: [makeFinding('bad', { file: 'f', line_start: 4, line_end: 9, location_status: 'unconfirmed' }), makeFinding('good')] });
  assert.equal(out.stats.validated, 1);
  assert.equal(out.findings.find((f) => f.id === 'bad').verdict_route, 'not-established');
});

test('not-established challenge answers agree with survival counts', async () => {
  const f = makeFinding('f', { verdict: truth, verdict_route: 'posted' });
  evaluateAdmission(f);
  const out = await challengeStage({ parallel, agent: async () => ({ verdict: unresolved }) }, { findings: [f] });
  assert.equal(out.findings.length, 0);
  assert.equal(out.unverified.length, 1);
  assert.equal(out.stats.final_count, 0);
  assert.equal(out.stats.challenge_survived, 0);
});

for (const [dimension, fields] of [
  ['simplification', { quality_basis: 'current_correctness' }],
  ['type_design', { quality_basis: 'simpler_equivalent', suggestion: 'Return the same value', behavior_preserved: 'Equivalent result' }],
]) test(`artifact truth supplies semantic proof for ${dimension}/${fields.quality_basis}`, async () => {
  const stage = await validateStage({ parallel, agent: async () => ({ validations: [{ ...truth, consequence: 'artifact_defect', finding_id: 'f' }] }) },
    { findings: [makeFinding('f', { dimension, ...fields })] });
  const f = evaluateAdmission(filterStage({ findings: stage.findings }).filtered[0]);
  assert.equal(f.admission.status, 'admitted');
  assert.equal(f.admission_assessments, undefined);
});

test('both readers store the same closed verdict and contradictory reachability is rejected', async () => {
  const response = { ...truth, finding_id: 'f', validator_only: 'untrusted', verdict_route: 'eliminated' };
  const first = await validateStage({ parallel, agent: async () => ({ validations: [response] }) }, { findings: [makeFinding('f')] });
  assert.deepEqual(first.findings[0].verdict, truth);
  const final = await challengeStage({ parallel, agent: async () => ({ verdict: response }) }, { findings: first.findings.map(evaluateAdmission) });
  assert.deepEqual(final.findings[0].verdict, truth);
  const bad = await validateStage({ parallel, agent: async () => ({ validations: [{ ...truth, finding_id: 'f', reachability: 'future_change_only' }] }) }, { findings: [makeFinding('f')] });
  assert.equal(bad.stats.skipped, 1);
  assert.equal(bad.findings[0].verdict_route, 'not-established');
});

test('a broken citation executor records a report gap instead of trusting refutations', async () => {
  const out = await validateStage({ parallel, agent: async (_, opts) => {
    if (opts.agentType === 'code-gauntlet:executor') throw new Error('executor failed');
    return { validations: [{ ...refuted, finding_id: 'f' }] };
  } }, { findings: [makeFinding('f')], repoRoot: '/repo', pluginRoot: '/plugin', diffPath: '/diff', headShaShort: 'abc1234', targetKind: 'pr' });
  assert.equal(out.findings[0].verdict_route, 'not-established');
  assert.match(out.gaps.join('\n'), /citation checker failed/);
});

test('resume without a verify handoff can validate and challenge its saved findings', async () => {
  const args = validArgs({ verify: undefined, checkpoints: { verify: { findings: [makeFinding('SAVED')], verified: true } } });
  const out = await runWith(makeCtx(args), args);
  assert.equal(out.ok, true, JSON.stringify(out));
});


const confirmedAnswer = (consequence) => ({ ...truth, consequence,
  ...(consequence === 'latent' ? { latent_condition: 'A caller enables the unused mode' } : {}) });
async function twoReaders(validator, challenger, fields = {}) {
  let calls = 0;
  const ctx = { parallel, agent: async (_prompt, options) => {
    if (options.agentType === 'code-gauntlet:validator') return { validations: [{ ...validator, finding_id: 'f' }] };
    assert.equal(options.agentType, 'code-gauntlet:challenger');
    calls++;
    return { verdict: challenger };
  } };
  const validated = await validateStage(ctx, { findings: [makeFinding('f', fields)] });
  const filtered = filterStage({ findings: validated.findings.map(evaluateAdmission) });
  const challenged = await challengeStage(ctx, { findings: filtered.filtered.map(evaluateAdmission) });
  return { ...disposeFindings(challenged), calls };
}

const seriousness = ['present_failure', 'latent', 'artifact_defect', 'no_practical_effect'];
test('schema consequences have the declared seriousness order and a routing row', () => {
  assert.deepEqual(VERDICT_PROPERTIES.consequence.enum, seriousness);
  for (const consequence of VERDICT_PROPERTIES.consequence.enum) {
    const rule = VERDICT_ROUTES.confirmed[consequence];
    assert.ok(rule);
    if (rule.status !== 'admitted') assert.equal(rule.challenge, true);
  }
});
for (const validator of seriousness) for (const challenger of seriousness) {
  test(`two confirming readers resolve ${validator} and ${challenger} without withholding the stronger answer`, async () => {
    const out = await twoReaders(confirmedAnswer(validator), confirmedAnswer(challenger));
    assert.equal(out.calls, 1);
    const f = out.findings[0];
    const expected = seriousness[Math.min(seriousness.indexOf(validator), seriousness.indexOf(challenger))];
    assert.equal(f.verdict.consequence, expected);
    assert.equal(f.validator_verdict.consequence, validator);
    assert.equal(f.challenge_verdict.consequence, challenger);
    assert.equal(f.verdict_route, VERDICT_ROUTES.confirmed[expected].route);
    assert.equal(f.disposition, expected === 'no_practical_effect' ? 'report_only' : 'deliver');
    assert.equal(f.severity, expected === 'present_failure' || expected === 'no_practical_effect' ? 'high' : 'low');
    assert.equal(f.reachability, undefined);
  });
}
for (const [consequence, rule] of Object.entries(VERDICT_ROUTES.confirmed).filter(([, rule]) => rule.route.startsWith('posted'))) {
  for (const placement of ['on-diff', 'off-diff', 'unconfirmed']) {
    test(`posting table row ${consequence} uses only source placement for ${placement}`, async () => {
      const fields = placement === 'off-diff' ? { diff_validation: { in_diff: false } }
        : placement === 'unconfirmed' ? { location_status: 'unconfirmed' } : {};
      const out = await twoReaders(confirmedAnswer(consequence), confirmedAnswer(consequence), fields);
      assert.equal(out.findings[0].verdict_route, rule.route);
      assert.equal(out.findings[0].disposition, placement === 'on-diff' ? 'deliver' : 'body');
    });
  }
}

test('a low present failure ranks ahead of an inline latent security finding', () => {
  const present = evaluateAdmission(makeFinding('present', { severity: 'low', verdict: confirmedAnswer('present_failure'), verdict_route: 'posted', report_tag: 'main', disposition: 'deliver' }));
  const latent = evaluateAdmission(makeFinding('latent', { severity: 'low', dimension: 'security', verdict: confirmedAnswer('latent'), verdict_route: 'posted-latent', report_tag: 'suggestion', disposition: 'deliver' }));
  assert.deepEqual(selectDelivery([latent, present], 1).map((f) => f.id), ['present']);
});
