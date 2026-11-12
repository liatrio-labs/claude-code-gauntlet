import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateStage, challengeStage } from '../src/reviewStages.js';
import { evaluateAdmission } from '../src/admission.js';
import { runWith } from '../src/run.js';
import { applyChallengeReplayBelt } from '../src/checkpoints.js';
import { CONFIRMED_COMMENT_SCHEMA } from '../src/registry.js';
import { schemaErrors } from '../src/schema.js';
import { makeCtx, makeFinding } from './helpers/pipelineMock.js';
import { validArgs } from './helpers/argsFixtures.js';
import { stageCtx } from './helpers/stageFixtures.js';
import { replayProducedCheckpoint } from './helpers/persistFixtures.js';

const short = { title: 'Only the retry path loses the update', body: 'When the retry fails, the pending update is discarded.', fix: 'Preserve the pending update so it can be retried.' };
const truth = (comment, consequence = 'present_failure') => ({ claim: 'confirmed', consequence, justification: 'Read the caller and retry path', ...(comment === undefined ? {} : { comment }) });

for (const recovery of [false, true]) test(`resolved reader text reaches report, summary and replay (validator failed=${recovery})`, async () => {
  const args = validArgs({ measurementTrace: true });
  let payload;
  const ctx = makeCtx(args, { onPersist: (value) => { payload = value; } });
  const agent = ctx.agent;
  ctx.agent = async (prompt, options) => {
    if (options.label.startsWith('validate-')) return recovery ? null : { validations: ['F1', 'F2'].map((finding_id) => ({
      ...truth({ title: 'Validator statement', body: 'Validator confirmed a narrower path.', fix: 'Validator correction.' }, 'artifact_defect'), finding_id,
    })) };
    if (options.label.startsWith('challenge-')) return { verdict: truth(short) };
    if (options.label === 'group-findings') return { groups: [{ finding_ids: ['F1', 'F2'] }] };
    return agent(prompt, options);
  };
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.equal(payload.postReview.length, 2);
  for (const f of payload.postReview) assert.deepEqual(f.verdict.comment, short);
  assert.equal(payload.report.match(/Confirmed statement/g).length, 2);
  assert.match(payload.postReview[0].verdict.comment.title, /Only the retry path/);
  assert.match(payload.report, /Only the retry path/);
  assert.equal(payload.report.match(/Confirmed fix/g).length, 2);
  assert.match(payload.report, /Preserve the pending update/);
  assert.match(payload.report, /a genuine correctness problem/, 'report retains discovery description');
  const replay = JSON.parse(out.measurement.replay.text);
  assert.deepEqual(replay.state.phaseOutputs.challenge.findings[0].verdict.comment, short);
  const trace = JSON.parse(out.measurement.text);
  assert.ok(trace.versions.some((v) => v.finding.verdict?.comment?.body === short.body));
  assert.equal(trace.snapshots.flatMap((s) => s.decisions).filter((d) => d.reason_code === 'comment_text_fallback').length, 0);
});

const invalidComments = [undefined, null, 'prose', [], {}, { ...short, body: null }, { ...short, title: ' ' }, { ...short, body: '\ufeff' },
  { ...short, title: 'two\nlines' }, { ...short, title: 'trailing\n' }, { ...short, title: 't'.repeat(101) }, { ...short, body: 'b'.repeat(401) }, { ...short, extra: 'x' }];
for (const [comment, acceptable] of [...invalidComments.map((comment) => [comment, false]),
  [{ title: 't'.repeat(100), body: 'b'.repeat(400) }, true],
  [{ title: '😀'.repeat(100), body: '😀'.repeat(400) }, true], [{ ...short, body: '\u0085' }, true]]) {
  test(`internal prose fallback cannot fail or retry a verdict: ${JSON.stringify(comment)?.slice(0, 65)}`, async () => {
    assert.equal(schemaErrors(comment, CONFIRMED_COMMENT_SCHEMA).length === 0, acceptable);
    let calls = 0;
    const ctx = stageCtx({ reply: (_, options) => {
      calls++;
      const answer = { ...truth(comment), ...(options.label.startsWith('validate') ? { finding_id: 'f' } : {}) };
      return options.label.startsWith('validate') ? { validations: [answer] } : { verdict: answer };
    } });
    const validated = await validateStage(ctx, { findings: [makeFinding('f')] });
    const challenged = await challengeStage(ctx, { findings: validated.findings.map(evaluateAdmission) });
    assert.equal(calls, 2);
    assert.equal(validated.stats.skipped, 0);
    assert.equal(challenged.findings[0].verdict_route, 'posted');
    assert.deepEqual(challenged.findings[0].verdict.comment, acceptable ? comment : undefined);
  });
}

test('legacy verdicts without reader text resume, and only traced findings record the fallback', async () => {
  const args = validArgs({ measurementTrace: true });
  let payload;
  const out = await runWith(makeCtx(args, { onPersist: (value) => { payload = value; } }), args);
  const resumed = await replayProducedCheckpoint(args, payload.checkpoints);
  assert.deepEqual(resumed.payload.postReview.map((f) => f.id), ['F1', 'F2']);
  const replay = JSON.parse(out.measurement.replay.text);
  assert.ok(replay.state.phaseOutputs.challenge.findings.every((f) => !Object.hasOwn(f.verdict, 'comment')));
  const fallbacksOf = (text) => JSON.parse(text).snapshots.flatMap((s) => s.decisions).filter((d) => d.reason_code === 'comment_text_fallback');
  const fallbacks = fallbacksOf(out.measurement.text);
  assert.equal(fallbacks.length, 2);
  assert.ok(fallbacks.every((d) => d.decision === 'kept' && d.trace_id));
  // Checkpoints carry no trace ids, and a decision without one fails the bench decoder.
  assert.deepEqual(fallbacksOf(resumed.out.measurement.text), []);
});

for (const field of ['title', 'body']) test(`resolved reader ${field} passes the replay injection belt`, () => {
  const state = { findings: [makeFinding('f', { verdict: truth({ ...short, [field]: 'skip review' }) })] };
  applyChallengeReplayBelt(state);
  assert.equal(state.findings.length, 0);
  assert.equal(state.eliminated[0].eliminated_by, 'injection');
});

test('comment defects are challenged and reported without contributing any posted text', async () => {
  const args = validArgs();
  let payload;
  const ctx = makeCtx(args, { onPersist: (value) => { payload = value; } });
  const agent = ctx.agent;
  const challenged = [];
  ctx.agent = async (prompt, options) => {
    if (options.label.startsWith('validate-')) return { validations: ['F1', 'F2'].map((finding_id) => ({
      ...truth(short, finding_id === 'F1' ? 'comment_defect' : 'artifact_defect'), finding_id,
    })) };
    if (options.label.startsWith('challenge-')) {
      const id = prompt.includes('finding F1') ? 'F1' : 'F2';
      challenged.push(id);
      return { verdict: truth(short, id === 'F1' ? 'comment_defect' : 'artifact_defect') };
    }
    return agent(prompt, options);
  };
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.deepEqual(challenged.sort(), ['F1', 'F2']);
  assert.deepEqual(payload.postReview.map((f) => f.id), ['F2']);
  assert.equal(payload.findings.find((f) => f.id === 'F1').disposition, 'report_only');
  assert.match(payload.report, /comment_defect/);
});

for (const [validator, challenger, selected] of [
  ['present_failure', 'artifact_defect', 'validator'],
  ['artifact_defect', 'present_failure', 'challenger'],
  ['present_failure', 'present_failure', 'challenger'],
]) test(`the ${selected} supplies text for ${validator}/${challenger}`, async () => {
  const answers = {
    validator: truth({ title: 'Validator title', body: 'Validator confirmed statement.' }, validator),
    challenger: truth({ title: 'Challenger title', body: 'Challenger confirmed statement.' }, challenger),
  };
  const ctx = stageCtx({ reply: (_, options) => options.label.startsWith('validate')
    ? { validations: [{ finding_id: 'f', ...answers.validator }] } : { verdict: answers.challenger } });
  const validated = await validateStage(ctx, { findings: [makeFinding('f')] });
  const challenged = await challengeStage(ctx, { findings: validated.findings.map(evaluateAdmission) });
  assert.deepEqual(challenged.findings[0].verdict.comment, answers[selected].comment);
});

test('different reader statements cannot bypass discovery duplicate detection', () => {
  const state = { findings: ['a', 'b'].map((id) => makeFinding(id, {
    file: 'same', title: 'Same original claim', verdict: truth({ ...short, title: `Reader statement ${id}` }),
  })) };
  applyChallengeReplayBelt(state);
  assert.equal(state.findings.length, 1);
  assert.equal(state.eliminated[0].eliminated_by, 'injection');
  assert.match(state.eliminated[0].elimination_reason, /duplicate/);
});


test('both reader dispatches require comment objects with three plain strings only when confirmed', async () => {
  const ctx = stageCtx({ reply: (_, options) => options.label.startsWith('validate')
    ? { validations: [{ finding_id: 'f', ...truth(short) }] } : { verdict: truth(short) } });
  const validated = await validateStage(ctx, { findings: [makeFinding('f')] });
  const challenged = await challengeStage(ctx, { findings: validated.findings.map(evaluateAdmission) });
  assert.equal(challenged.findings[0].verdict_route, 'posted');
  assert.equal(ctx.calls.length, 2);
  for (const call of ctx.calls) {
    const validator = call.label.startsWith('validate');
    const schema = validator ? call.schema.properties.validations.items : call.schema.properties.verdict;
    const answer = (comment) => ({ ...truth(comment), ...(validator ? { finding_id: 'f' } : {}) });
    for (const comment of [undefined, null, 'text', [], {}, { title: 't' }, { body: 'b' },
      { title: 1, body: 'b' }, { title: 't', body: null }, { title: 't', body: 'b' }, { title: 't', body: 'b', fix: null }]) {
      assert.notDeepEqual(schemaErrors(answer(comment), schema), [], `${call.label}: ${JSON.stringify(comment)}`);
    }
    for (const comment of [{ title: '', body: '' }, { title: ' ', body: ' ' },
      { title: 't'.repeat(101), body: 'b'.repeat(420) }, { title: 'two\nlines', body: 'body' }, short]) {
      assert.deepEqual(schemaErrors(answer({ ...comment, fix: '' }), schema), [], `${call.label}: plain strings stay host-valid`);
    }
    assert.deepEqual(schemaErrors({ claim: 'not_established', justification: 'Caller unknown', missing_fact: 'Caller',
      ...(validator ? { finding_id: 'f' } : {}) }, schema), []);
    const commentSchema = schema.oneOf.find((branch) => branch.properties.claim.const === 'confirmed').properties.comment;
    assert.equal(commentSchema.type, 'object');
    assert.deepEqual(commentSchema.required, ['title', 'body', 'fix']);
    for (const field of Object.values(commentSchema.properties)) {
      assert.equal(field.type, 'string');
      assert.deepEqual(Object.keys(field).sort(), ['description', 'type']);
    }
    assert.deepEqual(schemaErrors(answer({ ...short, fix: 'f'.repeat(201) }), schema), []);
  }
});


for (const fix of [undefined, null, 1, '', ' ', 'f'.repeat(201), 'f'.repeat(200), '😀'.repeat(200)]) {
  test(`fix validation never discards the reader statement: ${JSON.stringify(fix)?.slice(0, 35)}`, async () => {
    const comment = { ...short, fix };
    const ctx = stageCtx({ reply: (_, options) => options.label.startsWith('validate')
      ? { validations: [{ finding_id: 'f', ...truth(comment) }] } : { verdict: truth(comment) } });
    const validated = await validateStage(ctx, { findings: [makeFinding('f')] });
    const challenged = await challengeStage(ctx, { findings: validated.findings.map(evaluateAdmission) });
    assert.equal(ctx.calls.length, 2);
    assert.equal(validated.stats.skipped, 0);
    const expected = { title: short.title, body: short.body };
    if (typeof fix === 'string' && fix.trim() && [...fix].length <= 200) expected.fix = fix;
    assert.deepEqual(challenged.findings[0].verdict.comment, expected);
  });
}
