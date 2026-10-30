import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateStage } from '../src/reviewStages.js';
import { admissionRequests, hasAdmission } from '../src/admission.js';
import { stageCtx } from './helpers/stageFixtures.js';
import { makeFinding, makeCtx } from './helpers/pipelineMock.js';
import { validArgs } from './helpers/argsFixtures.js';
import { runAndCapture, replayProducedCheckpoint } from './helpers/persistFixtures.js';
import { runWith } from '../src/run.js';

const assessment = (predicate, status = 'confirmed', references = ['primary']) => ({ predicate, status, references });
const score = (id, admission_assessments = []) => ({ finding_id: id, confidence: 80, policy_subject: 'ordinary', admission_assessments });

test('present defect promotion overrides additional cases, quality other and gaps without gap evidence', async () => {
  for (const [id, dimension, agent, fields, predicates] of [
    ['additional', 'test_coverage', 'test-analyzer', { test_kind: 'additional_case' }, []],
    ['other', 'simplification', 'code-simplifier', { quality_basis: 'other' }, []],
    ['type-other', 'type_design', 'type-design-analyzer', { quality_basis: 'other' }, []],
    ['gap', 'test_coverage', 'test-analyzer', { test_kind: 'critical_gap' },
      ['resolved_production_element', 'complete_searches', 'complete_behavioral_absence']],
  ]) {
    for (const status of ['confirmed', 'rejected', 'unresolved']) {
      const f = makeFinding(id, { dimension, agent, confidence: 80, ...fields });
      const entries = [assessment('present_defect', status), ...predicates.map((predicate) => assessment(predicate, 'unresolved', []))];
      const validated = await validateStage(stageCtx({ reply: { validations: [score(id, entries)] } }), { findings: [f] });
      assert.equal(validated.stats.skipped, 0);
      assert.deepEqual(Object.keys(admissionRequests(validated.findings[0])), ['primary']);
      const result = await runAndCapture(validArgs({ checkpoints: { validate: validated }, delivery: { tier: 'main_only' } }));
      const delivered = result.payload.findings.find((finding) => finding.id === id);
      assert.ok(delivered, id);
      assert.equal(delivered.disposition, status === 'confirmed' ? 'deliver' : id === 'gap' ? 'held_back' : 'report_only');
      assert.equal(delivered.agent, agent);
      assert.equal(delivered.dimension, status === 'confirmed' && dimension === 'test_coverage' ? 'bug' : dimension);
      assert.equal(delivered.origin_dimension, status === 'confirmed' ? dimension : undefined);
      for (const [field, value] of Object.entries(fields)) assert.equal(delivered[field], value);
      if (status === 'confirmed') {
        assert.equal(delivered.admission.kind, dimension === 'test_coverage' ? 'production_defect' : 'current_correctness');
        assert.equal(delivered.admission.core, true);
        assert.equal(delivered.report_tag, 'main');
        assert.equal(delivered.confidence, 80);
        assert.equal(delivered.singleton_penalty, undefined);
        assert.equal(delivered.effective_test_kind, dimension === 'test_coverage' ? 'production_defect' : undefined);
        assert.equal(result.payload.postReview.length, 1);
        assert.ok(hasAdmission(delivered));
        const replayed = await replayProducedCheckpoint(validArgs(), result.payload.checkpoints);
        assert.deepEqual(replayed.payload.findings, result.payload.findings);
      }
    }
  }
});

test('present defect promotion governs ranking even when the validator classified a test claim', async () => {
  for (const policy_subject of ['coverage', 'test_defect', 'unresolved']) {
    const f = makeFinding('quality', { agent: 'code-simplifier', dimension: 'simplification', quality_basis: 'other' });
    const entries = [assessment('present_defect'), assessment('confirmed_test_defect', 'unresolved'),
      ...['resolved_production_element', 'complete_searches', 'complete_behavioral_absence'].map((predicate) => assessment(predicate, 'unresolved', []))];
    const validated = await validateStage(stageCtx({ reply: [{ ...score('quality', entries), policy_subject }] }), { findings: [f] });
    assert.equal(validated.stats.skipped, 0);
    const run = await runAndCapture(validArgs({ checkpoints: { validate: validated } }));
    const delivered = run.payload.findings.find((finding) => finding.id === 'quality');
    assert.equal(delivered.disposition, 'deliver');
    assert.equal(delivered.admission.kind, 'current_correctness');
    assert.equal(delivered.singleton_penalty, undefined);
    assert.equal(delivered.confidence, 80);
    assert.equal(delivered.policy_subject, policy_subject);
  }
});

test('incomplete validation gets one small follow-up round and only score or policy omissions degrade', async () => {
  for (const arm of ['score', 'policy_subject', 'assessment', 'duplicate_assessment']) {
    for (const repaired of [true, false]) {
      const f = makeFinding('pending', { dimension: 'simplification', quality_basis: 'other' });
      const complete = score('pending', [assessment('present_defect', 'rejected')]);
      const incomplete = structuredClone(complete);
      if (arm === 'score') delete incomplete.confidence;
      if (arm === 'policy_subject') delete incomplete.policy_subject;
      if (arm === 'assessment') delete incomplete.admission_assessments;
      if (arm === 'duplicate_assessment') incomplete.admission_assessments.push(incomplete.admission_assessments[0]);
      const ctx = stageCtx({ reply: { validations: [incomplete, score('sibling')] },
        replies: { 'validate-retry-0': { validations: [repaired ? complete : incomplete] } } });
      const out = await validateStage(ctx, { findings: [f, makeFinding('sibling')], limits: { validateBatch: 25 } });
      const skipped = !repaired && ['score', 'policy_subject'].includes(arm);
      assert.deepEqual(ctx.calls.map((call) => call.label), ['validate-batch-0', 'validate-retry-0']);
      assert.equal(ctx.calls[1].prompt.includes('- sibling ['), false);
      assert.equal(out.findings[0].validation, skipped ? 'skipped' : 'validated');
      assert.equal(out.findings[0].confidence, skipped ? 90 : 80);
      assert.equal(out.stats.skipped, skipped ? 1 : 0);
      assert.equal(out.gaps.length, skipped ? 1 : 0);
      assert.equal(out.findings[1].validation, 'validated');
      const rejection = out.audit.batches[0].rejections.find((entry) => entry.id === 'pending');
      assert.ok(rejection.reasons.includes(arm === 'score' ? 'invalid_confidence' : arm === 'policy_subject'
        ? 'invalid_policy_subject' : 'missing_assessment:present_defect'));
      if (!repaired) {
        const run = await runAndCapture(validArgs({ checkpoints: { validate: out } }));
        const held = run.payload.findings.find((finding) => finding.id === 'pending');
        assert.equal(held.disposition, 'held_back');
        assert.equal(held.disposition_reasons.includes('validation_skipped'), skipped);
        assert.equal(run.out.stats.validate.skipped, skipped ? 1 : 0);
      }
    }
  }
  const many = Array.from({ length: 12 }, (_, i) => makeFinding(`missing-${i}`));
  const ctx = stageCtx({ reply: [] });
  const out = await validateStage(ctx, { findings: many, limits: { validateBatch: 25 } });
  assert.equal(out.stats.batches_dispatched, 4);
  assert.deepEqual(ctx.calls.slice(1).map((call) => [...call.prompt.matchAll(/^- /gm)].length), [5, 5, 2]);
  assert.equal(out.stats.skipped, 12);
});

test('a score omitted from the main batch is repaired without degrading a fresh review', async () => {
  const args = validArgs({ contextLines: 20, contextChars: 400 });
  let payload;
  const ctx = makeCtx(args, { findings: [makeFinding('kept'), makeFinding('retry')], onPersist: (value) => { payload = value; } });
  const agent = ctx.agent;
  ctx.agent = async (prompt, dispatch) => {
    if (/^validate-(batch|retry)-/.test(dispatch.label)) {
      ctx.calls.push({ prompt, ...dispatch });
      return { validations: dispatch.label === 'validate-batch-0' ? [score('kept')] : [score('retry')] };
    }
    return agent(prompt, dispatch);
  };
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.deepEqual(out.gaps, []);
  assert.deepEqual(out.stats.validate, { batches_dispatched: 2, batches_completed: 2, validated: 2, skipped: 0, adjusted: 2 });
  assert.deepEqual(ctx.calls.filter((call) => call.agentType === 'code-gauntlet:validator').map((call) => call.label), ['validate-batch-0', 'validate-retry-0']);
  assert.ok(payload.findings.every((finding) => finding.disposition === 'deliver' && finding.confidence === 80));
  assert.equal(payload.checkpoints.phases.challenge.validation_audit.batches[0].raw, JSON.stringify({ validations: [score('kept')] }));
});

test('validator schema requires the semantic assessments for each kind and selected policy', async () => {
  const rows = [
    [makeFinding('core'), []],
    [makeFinding('extra', { dimension: 'test_coverage', test_kind: 'additional_case' }), ['present_defect']],
    [makeFinding('gap', { dimension: 'test_coverage', test_kind: 'critical_gap' }),
      ['present_defect', 'resolved_production_element', 'complete_searches', 'complete_behavioral_absence']],
    [makeFinding('quality', { dimension: 'type_design', quality_basis: 'duplication' }), ['present_defect', 'confirmed_duplication']],
    [makeFinding('equivalent', { dimension: 'simplification', quality_basis: 'simpler_equivalent' }),
      ['present_defect', 'concrete_equivalent', 'behavior_preserved']],
    [makeFinding('idiom', { dimension: 'type_design', quality_basis: 'stack_idiom' }), ['present_defect', 'applicable']],
  ];
  const ctx = stageCtx({ reply: [] });
  await validateStage(ctx, { findings: rows.map(([f]) => f) });
  const item = ctx.calls[0].schema.properties.validations.items;
  for (const [f, expected] of rows) {
    const variant = item.anyOf.find((branch) => branch.properties.finding_id.enum[0] === f.id && branch.properties.policy_subject.enum[0] === 'ordinary');
    assert.deepEqual(variant.required, expected.length ? ['admission_assessments'] : []);
    assert.deepEqual((variant.properties.admission_assessments?.allOf || []).map((clause) => clause.contains.properties.predicate.const), expected);
  }
  const mapped = item.anyOf.find((branch) => branch.properties.finding_id.enum[0] === 'core' && branch.properties.policy_subject.enum[0] === 'test_defect');
  assert.deepEqual(mapped.required, ['admission_assessments']);
  assert.deepEqual(mapped.properties.admission_assessments.allOf.map((clause) => clause.contains.properties.predicate.const), ['confirmed_test_defect']);
});

test('raw validator returns and rejections survive bounded checkpoint persistence and replay', async () => {
  const raw = { validations: [{ finding_id: 'f1', confidence: 'bad', policy_subject: 'invalid' }], extra: 'original response' };
  const ctx = stageCtx({ reply: raw });
  const validated = await validateStage(ctx, { findings: [makeFinding('f1')] });
  assert.equal(validated.audit.batches[0].raw, JSON.stringify(raw));
  assert.deepEqual(validated.audit.batches[0].rejections, [{ id: 'f1', reasons: ['invalid_confidence', 'invalid_policy_subject'] }]);
  const run = await runAndCapture(validArgs({ checkpoints: { validate: validated } }));
  assert.deepEqual(run.payload.checkpoints.phases.challenge.validation_audit, validated.audit);
  const replayed = await replayProducedCheckpoint(validArgs(), run.payload.checkpoints);
  assert.deepEqual(replayed.payload.checkpoints.phases.challenge.validation_audit, validated.audit);
  const legacy = structuredClone(run.payload.checkpoints.phases.challenge);
  delete legacy.validation_audit;
  assert.equal((await runAndCapture(validArgs({ checkpoints: { challenge: legacy } }))).out.ok, true);
  const oversized = { ...raw, extra: 'z'.repeat(100000) };
  const bounded = await validateStage(stageCtx({ reply: oversized }), {
    findings: Array.from({ length: 80 }, (_, i) => makeFinding(`f${i}`)), limits: { validateBatch: 1 },
  });
  assert.ok(JSON.stringify(bounded.audit).length <= 65536);
  assert.ok(bounded.audit.omitted_batches > 0);
  assert.ok(bounded.audit.batches[0].truncated);
  assert.equal(bounded.audit.batches[0].raw, JSON.stringify(oversized).slice(0, 8192));
  assert.equal(bounded.audit.batches[0].raw_characters, JSON.stringify(oversized).length);
});
