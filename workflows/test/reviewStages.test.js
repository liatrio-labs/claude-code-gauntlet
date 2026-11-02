import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateStage, filterStage, challengeStage, blindChallengeFields, selectDelivery } from '../src/reviewStages.js';
import { finding } from './helpers/findings.js';
import { stageCtx as rawStageCtx } from './helpers/stageFixtures.js';
import { ADMISSION_PREDICATES, RIGHT_ELIGIBILITY } from '../src/registry.js';
import { evaluateAdmission } from '../src/admission.js';

import { skepticalReturn } from './helpers/verdicts.js';
import { SKEPTICAL_CONTRACTS } from '../src/registry.js';
import { disposeFindings } from '../src/dispose.js';

function stageCtx(options = {}) {
  const upgrade = (value) => {
    const entry = (e) => e && typeof e === 'object' ? { ...skepticalReturn(e.id ?? e.finding_id), confidence: undefined, policy_subject: undefined, ...e } : e;
    if (Array.isArray(value)) return value.map(entry);
    if (Array.isArray(value?.validations)) return { ...value, validations: value.validations.map(entry) };
    return value;
  };
  return rawStageCtx({ ...options, reply: (prompt, opts) => {
    const raw = Object.hasOwn(options.replies || {}, opts.label) ? options.replies[opts.label] : options.reply;
    const value = typeof raw === 'function' ? raw(prompt, opts) : raw;
    return opts.label.startsWith('validate-') ? upgrade(value) : value;
  }, replies: {} });
}

test('validator classification omissions skip core findings and duplicate scores retain base behavior', async () => {
  for (const dimension of ['bug', 'security', 'cross_file_impact', 'intent', 'convention', 'comment_accuracy']) {
    for (const subject of [undefined, 'invalid', 'ordinary']) {
      const ctx = stageCtx({ reply: [{ id: 'f1', confidence: 80, policy_subject: subject }] });
      const out = await validateStage(ctx, { findings: [finding({ dimension })] });
      const f = evaluateAdmission(out.findings[0]);
      assert.equal(f.validation, subject === 'ordinary' ? 'validated' : 'skipped');
      assert.equal(f.admission.status, subject === 'ordinary' ? 'admitted' : 'held_back');
      assert.deepEqual(f.admission.reasons, subject === 'ordinary' ? [] : [{ predicate: 'validation', status: 'held_back' }]);
      assert.deepEqual(out.gaps, subject === 'ordinary' ? [] : [invalidGap('invalid_policy_subject')]);
    }
  }
  const ctx = stageCtx({ reply: [70, 85].map((confidence) => ({ id: 'f1', confidence, policy_subject: 'ordinary', reachability: 'current' })) });
  const out = await validateStage(ctx, { findings: [finding()] });
  assert.deepEqual(out.gaps, []);
  assert.equal(out.findings[0].confidence, 85);
  assert.equal(out.findings[0].reachability, 'current');
  assert.equal(evaluateAdmission(out.findings[0]).admission.status, 'admitted');
});

test('validator asks correctness first and shares predicate definitions across conditional claims', async () => {
  const ctx = stageCtx({ reply: [] });
  await validateStage(ctx, { findings: [finding(),
    finding({ id: 'f2', dimension: 'simplification', quality_basis: 'simpler_equivalent' }),
    finding({ id: 'f3', dimension: 'type_design', quality_basis: 'other' }),
    finding({ id: 'f4', dimension: 'test_coverage', test_kind: 'additional_case' })] });
  const prompt = ctx.calls[0].prompt;
  assert.ok(prompt.indexOf('First judge the correctness') < prompt.indexOf('Then classify policy_subject'));
  assert.ok(prompt.includes('missing admission evidence or posting value must not lower confidence or change reachability'));
  const table = JSON.stringify(ADMISSION_PREDICATES);
  assert.equal(prompt.split(table).length - 1, 1);
  assert.ok(prompt.indexOf(table) < prompt.indexOf('\nFindings:'));
  const contexts = prompt.split('\n').filter((line) => line.includes(' | admission_context: ')).map((line) => JSON.parse(line.split(' | admission_context: ')[1]));
  assert.equal(prompt.split('Use real, refuted or needs_evidence').length - 1, 1);
  assert.deepEqual(contexts.map((context) => context.policy.policy_subjects.ordinary),
    [[], ['present_defect', 'concrete_equivalent', 'behavior_preserved'], ['present_defect'], ['present_defect']]);
  assert.deepEqual(contexts.slice(1).map((context) => context.policy.promotion),
    Array(3).fill({ predicate: 'present_defect', confirmed: ['present_defect'] }));
  assert.deepEqual(contexts.map((context) => context.policy.policy_subjects.test_defect),
    [['confirmed_test_defect'], ['present_defect', 'confirmed_test_defect'], ['present_defect'], ['present_defect']]);
  for (const context of contexts) {
    assert.equal(context.predicates, undefined);
    assert.deepEqual(context.policy.body_fix_task, RIGHT_ELIGIBILITY.body.fixTask);
  }
});

test('validation completeness accepts unlocated assessments without promoting empty confirmations', async () => {
  for (const status of ['rejected', 'confirmed', 'unresolved']) {
    const ctx = stageCtx({ reply: [{ finding_id: 'f1', confidence: 80, policy_subject: 'ordinary',
      admission_assessments: [{ predicate: 'present_defect', status, references: [] }] }] });
    const out = await validateStage(ctx, { findings: [finding({ dimension: 'test_coverage',
      test_kind: 'additional_case', location_status: 'unconfirmed' })] });
    assert.equal(out.stats.skipped, 0, status);
    assert.equal(out.stats.batches_dispatched, 1, status);
    assert.deepEqual(out.gaps, []);
    const f = evaluateAdmission(out.findings[0]);
    assert.equal(f.admission.status, 'report_only', status);
    assert.equal(f.admission.kind, 'additional_case', status);
    assert.deepEqual(f.admission_assessments, [{ predicate: 'present_defect',
      status: status === 'confirmed' ? 'unresolved' : status, references: [] }]);
  }
});

test('validation retries partial search references once and admission holds the finding back', async () => {
  const ctx = stageCtx({ reply: [{ finding_id: 'f1', confidence: 80, policy_subject: 'ordinary',
    admission_assessments: [
      { predicate: 'present_defect', status: 'confirmed', references: ['searched_paths:0'] },
      { predicate: 'resolved_production_element', status: 'confirmed', references: ['production_element'] },
      { predicate: 'complete_searches', status: 'confirmed', references: ['searched_paths:0'] },
      { predicate: 'complete_behavioral_absence', status: 'confirmed', references: ['production_element', 'searched_paths:0'] },
    ] }] });
  const out = await validateStage(ctx, { findings: [finding({ dimension: 'test_coverage', test_kind: 'critical_gap',
    candidate_tests: [], resolved_references: {
      production_element: { file: 'src/x.py', line_start: 10, line_end: 10, location_status: 'confirmed' },
      searched_paths: [{ path: 'tests/unit', available: true }, { path: 'tests/integration', available: true }],
      candidate_tests: [],
    } })] });
  assert.equal(out.stats.skipped, 0);
  assert.equal(out.stats.batches_dispatched, 2);
  assert.deepEqual(out.gaps, []);
  const f = evaluateAdmission(out.findings[0]);
  assert.equal(f.admission.kind, 'critical_gap');
  assert.equal(f.admission.status, 'held_back');
  assert.deepEqual(f.admission.reasons, [
    { predicate: 'complete_searches', status: 'held_back' },
    { predicate: 'complete_behavioral_absence', status: 'held_back' },
    { predicate: 'present_defect', status: 'held_back' },
  ]);
  assert.deepEqual(f.admission_assessments, [
    { predicate: 'resolved_production_element', status: 'confirmed', references: ['production_element'] },
  ]);
});

for (const omitted of [true, false]) test(`validation repairs wrong reference names with coverage assessments ${omitted ? 'omitted' : 'present'}`, async () => {
  const coverage = ['resolved_production_element', 'complete_searches', 'complete_behavioral_absence']
    .map((predicate) => ({ predicate, status: 'confirmed', references: [] }));
  const presentDefect = { predicate: 'present_defect', status: 'confirmed', references: ['primary'] };
  const missing = ['present_defect', ...(omitted ? coverage.map((entry) => entry.predicate) : [])];
  const ctx = stageCtx({ reply: [{ finding_id: 'f1', confidence: 80, policy_subject: 'ordinary', reachability: 'current',
    admission_assessments: [{ ...presentDefect, references: ['a.js:3'] }, ...(omitted ? [] : coverage)] }],
  replies: { 'validate-retry-0': [{ finding_id: 'f1', admission_assessments: [presentDefect, ...coverage] }] } });
  const out = await validateStage(ctx, { findings: [finding({ dimension: 'test_coverage', test_kind: 'critical_gap',
    file: 'a.js', line_start: 3 })] });
  assert.deepEqual(ctx.calls.map((call) => call.label), ['validate-batch-0', 'validate-retry-0']);
  const f = evaluateAdmission(out.findings[0]);
  assert.equal(f.admission.kind, 'production_defect');
  assert.equal(f.admission.status, 'admitted');
  assert.equal(f.confidence, 80);
  assert.equal(f.reachability, 'current');
  assert.deepEqual(out.audit.batches[0].rejections[0].reasons, missing.map((predicate) => `missing_assessment:${predicate}`));
  const context = JSON.parse(ctx.calls[1].prompt.split(' | admission_context: ')[1].split('\n')[0]);
  assert.deepEqual(context.policy.policy_subjects.ordinary, missing);
  assert.deepEqual(ctx.calls[1].schema.properties.validations.items.required, ['finding_id', 'admission_assessments']);
  assert.deepEqual(out.gaps, []);
  assert.deepEqual(ctx.violations, []);
});

test('validation completeness requires one well-formed assessment per predicate', async () => {
  const entry = { predicate: 'present_defect', status: 'rejected', references: [] };
  for (const admission_assessments of [undefined, [], [null], [entry, entry],
    [{ ...entry, status: 'invalid' }], [{ ...entry, references: null }], [{ ...entry, references: [1] }]]) {
    const ctx = stageCtx({ reply: [{ finding_id: 'f1', confidence: 80, policy_subject: 'ordinary', admission_assessments }] });
    const out = await validateStage(ctx, { findings: [finding({ dimension: 'test_coverage', test_kind: 'additional_case' })] });
    assert.equal(out.stats.skipped, 0);
    assert.equal(out.findings[0].validation, 'validated');
    assert.deepEqual(out.gaps, []);
    assert.ok(evaluateAdmission(out.findings[0]).admission.reasons.some((reason) => reason.predicate === 'present_defect' && reason.status === 'held_back'));
    assert.deepEqual(out.audit.batches[0].rejections[0].reasons, ['missing_assessment:present_defect']);
  }
});

const omittedGap = (idx, count) => `validate-batch-${idx}: validator omitted scores — ${count} finding(s) unvalidated (validation=skipped, held back to the report)`;
const invalidGap = (reasons) => `validate-batch-0: validator returned invalid/incomplete entries (${reasons}) — 1 finding(s) unvalidated (validation=skipped, held back to the report)`;
const VALIDATE_STATS = { batches_dispatched: 1, batches_completed: 1, validated: 1, skipped: 1, incomplete: 0, adjusted: 1 };
const VALIDATE_ROWS = [
  { name: 'id adjustment', input: [finding({ id: 'F1', confidence: 90 }), finding({ id: 'F2', confidence: 80 })],
    reply: [{ id: 'F1', confidence: 50, policy_subject: 'ordinary', justification: 'weak' }, { id: 'F2', confidence: 'invalid' }, null],
    gaps: [invalidGap('invalid_confidence, invalid_policy_subject')],
    expected: [['F1', 50, 90, 'weak', undefined, 'validated'], ['F2', 80, undefined, undefined, undefined, 'skipped']] },
  { name: 'finding_id adjustment', input: [finding({ id: 'F1', confidence: 90 }), finding({ id: 'F2', confidence: 80 })],
    reply: [{ finding_id: 'F1', confidence: 40, policy_subject: 'ordinary', justification: 'reachable only hypothetically' }], gaps: [omittedGap(0, 1)],
    expected: [['F1', 40, 90, 'reachable only hypothetically', undefined, 'validated'],
      ['F2', 80, undefined, undefined, undefined, 'skipped']] },
  { name: 'validations envelope', input: [finding({ confidence: 40 })],
    reply: { validations: [{ id: 'f1', confidence: 85, policy_subject: 'ordinary' }] }, stats: { ...VALIDATE_STATS, validated: 1, skipped: 0 },
    expected: [['f1', 85, 40, undefined, undefined, 'validated']] },
  { name: 'known reachability', input: [finding({ id: 'F1', confidence: 80 })],
    reply: [{ finding_id: 'F1', confidence: 80, policy_subject: 'ordinary', reachability: 'future_change_only' }],
    stats: { ...VALIDATE_STATS, validated: 1, skipped: 0 }, expected: [['F1', 80, 80, undefined, 'future_change_only', 'validated']] },
  { name: 'count batches', batch: 2,
    input: ['F1', 'F2', 'F3', 'F4', 'F5'].map((id) => finding({ id, confidence: 80 })), reply: [],
    stats: { batches_dispatched: 3, batches_completed: 3, validated: 0, skipped: 5, adjusted: 0 },
    labels: ['validate-batch-0', 'validate-batch-1', 'validate-batch-2'], gaps: [omittedGap(0, 2), omittedGap(1, 2), omittedGap(2, 1)], expected: [
      ['F1', 80, undefined, undefined, undefined, 'skipped'], ['F2', 80, undefined, undefined, undefined, 'skipped'],
      ['F3', 80, undefined, undefined, undefined, 'skipped'], ['F4', 80, undefined, undefined, undefined, 'skipped'],
      ['F5', 80, undefined, undefined, undefined, 'skipped'],
    ] },
  { name: 'null batch retains confidence', batch: 2,
    input: ['F1', 'F2', 'F3', 'F4'].map((id) => finding({ id, confidence: 80 })), reply: [], nulls: ['validate-batch-1'],
    stats: { batches_dispatched: 2, batches_completed: 1, validated: 0, skipped: 4, adjusted: 0 },
    labels: ['validate-batch-0', 'validate-batch-1'], gaps: [omittedGap(0, 2),
      'validate-batch-1: validator returned null/malformed \u2014 2 finding(s) unvalidated '
      + '(validation=skipped, held back to the report)',
    ], expected: [
      ['F1', 80, undefined, undefined, undefined, 'skipped'], ['F2', 80, undefined, undefined, undefined, 'skipped'],
      ['F3', 80, undefined, undefined, undefined, 'skipped'], ['F4', 80, undefined, undefined, undefined, 'skipped'],
    ] },
  { name: 'malformed object retains confidence', input: [finding({ id: 'F1', confidence: 80 })], reply: {},
    expected: [['F1', 80, undefined, undefined, undefined, 'skipped']],
    stats: { batches_dispatched: 1, batches_completed: 0, validated: 0, skipped: 1, adjusted: 0 }, gaps: [
      'validate-batch-0: validator returned null/malformed \u2014 1 finding(s) unvalidated '
      + '(validation=skipped, held back to the report)',
    ] },
  { name: 'fractional batch clamps to one', batch: 0.5,
    input: [finding({ id: 'F1', confidence: 80 }), finding({ id: 'F2', confidence: 80 })], reply: [],
    expected: [['F1', 80, undefined, undefined, undefined, 'skipped'], ['F2', 80, undefined, undefined, undefined, 'skipped']],
    stats: { batches_dispatched: 2, batches_completed: 2, validated: 0, skipped: 2, adjusted: 0 },
    labels: ['validate-batch-0', 'validate-batch-1'], gaps: [omittedGap(0, 1), omittedGap(1, 1)] },
  { name: 'empty', input: [], reply: [], expected: [], labels: [],
    stats: { batches_dispatched: 0, batches_completed: 0, validated: 0, skipped: 0, adjusted: 0 } },
];
for (const row of VALIDATE_ROWS) test(`validate result: ${row.name}`, async () => {
  const ctx = stageCtx({ reply: row.reply, nulls: row.nulls });
  const out = await validateStage(ctx, { findings: structuredClone(row.input), limits: { validateBatch: row.batch ?? 10 } });
  assert.deepEqual(Object.keys(out), row.input.length ? ['findings', 'gaps', 'audit', 'stats'] : ['findings', 'gaps', 'stats']);
  assert.deepEqual(out.findings.map((f) => [f.id, f.confidence, f.original_confidence,
    f.validation_justification, f.reachability, f.validation]), row.expected);
  const expectedStats = { incomplete: 0, ...(row.stats ?? VALIDATE_STATS) };
  const pendingCount = row.expected.filter((entry) => entry.at(-1) === 'skipped').length;
  const retryCount = Math.ceil(pendingCount / Math.max(1, Math.ceil((row.batch || 10) / 5)));
  expectedStats.batches_dispatched += retryCount;
  if (Array.isArray(row.reply) || Array.isArray(row.reply?.validations)) expectedStats.batches_completed += retryCount;
  assert.deepEqual(out.stats, expectedStats);
  assert.deepEqual(out.gaps, row.gaps ?? []);
  assert.deepEqual(ctx.calls.map((call) => call.label), [...(row.labels ?? ['validate-batch-0']),
    ...Array.from({ length: retryCount }, (_, idx) => `validate-retry-${idx}`)]);
  for (const call of ctx.calls) assert.equal(call.agentType, 'code-gauntlet:validator');
  assert.deepEqual(ctx.violations, []);
});
const VALIDATE_PROMPT_ROWS = [
  { name: 'equal range endpoints print once', fields: { file: 'one.js', line_start: 42, line_end: 42 },
    block: '- f1 [bug/high] one.js:42 \u2014 The function returns the wrong result for a real input.' },
  { name: 'single line', fields: { file: 'SENTINEL_VALIDATE_FILE.js', line_start: 42, description: 'SENTINEL_VALIDATE_DESC' },
    block: '- f1 [bug/high] SENTINEL_VALIDATE_FILE.js:42 \u2014 SENTINEL_VALIDATE_DESC' },
  { name: 'range and evidence', fields: { file: 'range.js', line_start: 12, line_end: 15, evidence: 'INSPECTED_LINES' },
    block: '- f1 [bug/high] range.js:12-15 \u2014 The function returns the wrong result for a real input. | evidence: INSPECTED_LINES' },
];
for (const row of VALIDATE_PROMPT_ROWS) test(`validate prompt: ${row.name}`, async () => {
  const ctx = stageCtx({ reply: [] });
  await validateStage(ctx, { findings: [finding(row.fields)], contextLine: 'VALIDATOR_CONTEXT ' });
  assert.ok(ctx.calls[0].prompt.startsWith('VALIDATOR_CONTEXT Independently validate'));
  assert.ok(ctx.calls[0].prompt.includes('code-evidence and disproof contract'));
  assert.ok(ctx.calls[0].prompt.includes(row.block));
  assert.ok(ctx.calls[0].prompt.includes('verdict (real | refuted | needs_evidence)'));
  const item = ctx.calls[0].schema.properties.validations.items;
  assert.deepEqual(item.properties,{...SKEPTICAL_CONTRACTS.validator.dispatchSchema.properties,finding_id:{type:'string',enum:['f1']}});
  assert.ok(item.required.includes('verdict'));
  assert.ok(item.required.includes('scope_assessments'));
});

const FILTER_ROWS = [
  { name: 'exclusion patterns reach the pipeline', findings: [
    finding({ id: 'EXCLUDED', title: 'Excluded sentinel', file: 'generated/x.py' }),
    finding({ id: 'KEPT', file: 'src/x.py' })], config: {}, exclusions: ['Excluded sentinel'],
    kept: ['KEPT'], eliminated: ['EXCLUDED'] },
  { name: 'threshold, timestamp and determinism', findings: [
    finding({ id: 'F1', file: 'a.js', confidence: 90 }),
    finding({ id: 'F2', file: 'b.js', confidence: 30, severity: 'low', dimension: 'convention', agent: 'conventions-and-intent' }),
  ], config: { confidence_threshold: 70, severity_threshold: 'low', ignore: [] }, kept: ['F1'], eliminated: ['F2'] },
  { name: 'child threshold applies only under its directory', findings: [
    finding({ id: 'api', file: 'api/x.py', confidence: 80 }), finding({ id: 'legacy', file: 'legacy/x.py', confidence: 80 }),
  ], config: { ignore: [], scopes: [{ dir: 'api', confidence_threshold: 90, ignore: [] }] },
    kept: ['legacy'], eliminated: ['api'] },
];
for (const row of FILTER_ROWS) test(`filter forwarding: ${row.name}`, () => {
  const input = { findings: row.findings, reviewConfig: row.config, exclusionPatterns: row.exclusions ?? [],
    generatedAt: '2026-07-18T00:00:00Z' };
  const out = filterStage(structuredClone(input));
  assert.deepEqual(out, filterStage(structuredClone(input)));
  assert.equal(out.generated_at, '2026-07-18T00:00:00Z');
  assert.deepEqual(out.filtered.map((f) => f.id), row.kept);
  assert.deepEqual(out.eliminated.map((f) => f.id), row.eliminated);
});

test('incomplete challenger returns complete dispatch without failing siblings', async () => {
  const oversized = skepticalReturn('oversized', 'challenger');
  oversized.citations[0].line_end = 18;
  oversized.citations[0].quote = Array.from({ length: 9 }, (_, i) => `source line ${i}`).join('\n');
  const ctx = rawStageCtx({ replies: {
    'challenge-0': {}, 'challenge-1': { score: 0 }, 'challenge-2': null,
    'challenge-3': skepticalReturn('real', 'challenger', { finding_id: 'foreign' }),
    'challenge-4': oversized,
  } });
  const input = ['missing', 'legacy', 'failed', 'real', 'oversized'].map((id) => finding({ id }));
  const out = await challengeStage(ctx, { findings: input });
  assert.equal(out.stats.completed, 4);
  assert.equal(out.stats.dispatched, 5);
  assert.equal(out.stats.incomplete, 2);
  assert.deepEqual(out.unverified.map((f) => f.challenge), ['incomplete', 'incomplete', 'failed']);
  assert.deepEqual(out.findings.map((f) => f.id), ['real', 'oversized']);
  assert.equal(out.findings[1].challenge, 'survived');
  const reduced = disposeFindings(out).findings.find((f) => f.id === 'oversized');
  assert.equal(reduced.disposition, 'deliver');
  assert.ok(reduced.disposition_reasons.includes('cite_unmatched'));
  assert.equal(out.findings[0].skeptical_assessments.at(-1).finding_id, 'real');
  assert.deepEqual(out.eliminated, []);
  assert.deepEqual(ctx.violations, []);
});

test('real citation format gaps do not trigger validator retries or held-back counts', async () => {
  for (const fault of ['omitted', 'stage', 'long', 'lines']) {
    const reply = skepticalReturn('f1');
    if (fault === 'omitted') delete reply.citations;
    else if (fault === 'stage') reply.citations[0].stage = 'challenger';
    else if (fault === 'long') reply.citations[0].quote = 'x'.repeat(401);
    else reply.citations[0].quote = Array(9).fill('source line').join('\n');
    const ctx = rawStageCtx({ reply: { validations: [reply] } });
    const out = await validateStage(ctx, { findings: [finding()] });
    assert.equal(ctx.calls.length, 1, fault);
    assert.equal(out.stats.incomplete, 0, fault);
    assert.equal(out.stats.validated, 1, fault);
    const { confidence: _confidence, policy_subject: _policy, ...challenge } = reply;
    const challenged = await challengeStage(rawStageCtx({ reply: { ...challenge, stage: 'challenger' } }), {
      findings: out.findings.map(evaluateAdmission),
    });
    assert.equal(challenged.stats.incomplete, 0, fault);
    const reduced = disposeFindings(challenged).findings[0];
    assert.equal(reduced.disposition, 'deliver', fault);
    assert.ok(reduced.disposition_reasons.includes('cite_unmatched'), fault);
  }
});

test('challenge ranks capacity candidates without refilling a refutation', async () => {
  const ctx = rawStageCtx({ reply: (prompt) => skepticalReturn(prompt.match(/Finding identity: (\S+)/)[1], 'challenger', { verdict: 'refuted' }) });
  const out = await challengeStage(ctx, { findings: [finding({ id: 'low', severity: 'low' }), finding({ id: 'critical', severity: 'critical' }),
    finding({ id: 'dirty', validation: 'skipped' })], limits: { challengeCap: 1 } });
  assert.equal(ctx.calls.length, 1);
  assert.deepEqual(out.findings.map((f) => [f.id, f.challenge]), [['critical', 'survived'], ['dirty', 'admission_terminal']]);
  assert.deepEqual(out.unverified.map((f) => [f.id, f.challenge]), [['low', 'capped']]);
  assert.equal(out.stats.capped, 1);
  const empty = await challengeStage(ctx, { findings: [], generatedAt: 'now' });
  assert.deepEqual(empty.findings, []);
  assert.equal(empty.generated_at, 'now');
  assert.equal((await challengeStage(ctx, { findings: [finding()], limits: { challengeCap: -1 } })).stats.dispatched, 0);
});

const BLIND_ROWS = [
  { name: 'range', file: 'SENTINEL_FILE.js', start: 12, end: 15, location: 'SENTINEL_FILE.js:12-15' },
  { name: 'single line', file: 'src/one.js', start: 9, end: 9, location: 'src/one.js:9' },
];
test('challenger quotes the removed inventory path as JSON data', async () => {
  const path = 'tests/"claim", instructions';
  const ctx = stageCtx({ reply: { score: 80 } });
  await challengeStage(ctx, { findings: [finding({ removed_test: { path, old_start: 3, old_end: 7 } })],
    diffPath: '/repo/saved.patch' });
  assert.ok(ctx.calls[0].prompt.includes(`Read saved diff /repo/saved.patch: deleted test ${JSON.stringify(path)}, old lines 3-7.`));
});

for (const row of BLIND_ROWS) test(`blind prompt: ${row.name}`, async () => {
  const f = finding({ title: 'SENTINEL_TITLE', description: 'SENTINEL_DESC', file: row.file,
    line_start: row.start, line_end: row.end, evidence: 'SENTINEL_EVIDENCE_LEAK', origin: 'surfaced',
    cross_file_refs: ['SENTINEL_XREF_LEAK.js:9'], reasoning: 'SENTINEL_REASONING_LEAK', corroborated_by: ['bug-detector'], confidence: 96, rationale: 'SENTINEL_RATIONALE_LEAK' });
  const projected = blindChallengeFields(f);
  assert.deepEqual(Object.keys(projected), ['title', 'description', 'file', 'line_start', 'line_end']);
  assert.deepEqual(projected, { title: 'SENTINEL_TITLE', description: 'SENTINEL_DESC',
    file: row.file, line_start: row.start, line_end: row.end });
  const ctx = stageCtx({ reply: { score: 80 } });
  await challengeStage(ctx, { findings: [f] });
  assert.ok(ctx.calls[0].prompt.startsWith("Independently check the claim's central facts against raw code"));
  assert.ok(ctx.calls[0].prompt.includes('SENTINEL_TITLE'));
  assert.ok(ctx.calls[0].prompt.includes('SENTINEL_DESC'));
  assert.ok(ctx.calls[0].prompt.includes(row.file));
  assert.ok(ctx.calls[0].prompt.includes('real, refuted or needs_evidence'));
  assert.doesNotMatch(ctx.calls[0].prompt, /SENTINEL_EVIDENCE_LEAK|SENTINEL_REASONING_LEAK|SENTINEL_XREF_LEAK|SENTINEL_RATIONALE_LEAK|96|surfaced/);
  assert.deepEqual(ctx.calls[0].schema, SKEPTICAL_CONTRACTS.challenger.dispatchSchema);
});

const MIXED = [
  finding({ id: 'SUGG_LOW', severity: 'low', report_tag: 'suggestion', report_destination: 'suggestion' }),
  finding({ id: 'MAIN_MED', severity: 'medium', report_tag: 'main', report_destination: 'main' }),
  finding({ id: 'SUGG_HIGH', severity: 'high', report_tag: 'suggestion', report_destination: 'suggestion' }),
  finding({ id: 'MAIN_CRIT', severity: 'critical', report_tag: 'main', report_destination: 'main' }),
];
const DELIVERY_GOLDEN = ['MAIN_CRIT', 'SUGG_HIGH', 'MAIN_MED', 'SUGG_LOW'];
const DELIVERY_ROWS = [
  { name: 'critical corroborator ranks its group above a medium lead', cap: 1, input: [
    finding({ id: 'OTHER', severity: 'medium', confidence: 99 }),
    finding({ id: 'LEAD', severity: 'medium', confidence: 90, consolidation_key: 'issue', consolidation_primary: true }),
    finding({ id: 'CRITICAL', severity: 'critical', confidence: 80, consolidation_key: 'issue' }),
  ], expected: ['LEAD', 'CRITICAL'] },
  { name: 'main only filters each member before grouping', tier: 'main_only', input: [
    finding({ id: 'SUGG_LEAD', report_tag: 'suggestion', consolidation_key: 'one', consolidation_primary: true }),
    finding({ id: 'MAIN_CHILD', report_tag: 'main', consolidation_key: 'one' }),
    finding({ id: 'MAIN_LEAD', report_tag: 'main', consolidation_key: 'two', consolidation_primary: true }),
    finding({ id: 'SUGG_CHILD', report_tag: 'suggestion', consolidation_key: 'two' }),
    finding({ id: 'SUGG_ONLY', report_tag: 'suggestion', consolidation_key: 'three' }),
  ], expected: ['MAIN_CHILD', 'MAIN_LEAD'] },
  { name: 'body groups compete with inline groups under the cap', cap: 1, input: [
    finding({ id: 'INLINE', disposition: 'deliver', severity: 'low' }),
    finding({ id: 'BODY', disposition: 'body', severity: 'critical' }),
  ], expected: ['BODY'] },
  { name: 'zero cap includes body groups', cap: 0, input: [finding({ id: 'BODY', disposition: 'body' })], expected: [] },
  { name: 'persisted membership overrides legacy grouping', cap: 1, input: [
    finding({ id: 'CHILD', confidence: 99, consolidation_key: 'stale-child', consolidation_primary: true,
      delivery_group: { issue_key: 'current', lead_id: 'LEAD', finding_ids: ['LEAD', 'CHILD'] } }),
    finding({ id: 'LEAD', confidence: 95, consolidation_key: 'stale-lead', consolidation_primary: false,
      delivery_group: { issue_key: 'current', lead_id: 'LEAD', finding_ids: ['LEAD', 'CHILD'] } }),
    finding({ id: 'OTHER', confidence: 90 }),
  ], expected: ['LEAD', 'CHILD'] },
  { name: 'absent cap and tier' }, { name: 'null cap and tier', cap: null, tier: null },
  { name: 'nonbinding cap', cap: 25 },
  { name: 'negative cap', cap: -1, expected: [] },
  { name: 'zero cap', cap: 0, expected: [] }, { name: 'binding cap includes suggestion', cap: 2, expected: ['MAIN_CRIT', 'SUGG_HIGH'] },
  { name: 'main only', tier: 'main_only', expected: ['MAIN_CRIT', 'MAIN_MED'] },
  { name: 'main only cap filters before slicing', tier: 'main_only', cap: 2, expected: ['MAIN_CRIT', 'MAIN_MED'] },
  { name: 'main only cap', tier: 'main_only', cap: 1, expected: ['MAIN_CRIT'] },
  { name: 'destination fallback', tier: 'main_only', input: [
    finding({ id: 'D_MAIN', report_destination: 'main' }), finding({ id: 'D_SUGG', report_destination: 'suggestion' }),
  ], expected: ['D_MAIN'] },
  { name: 'severity and confidence ranking', input: [
    finding({ id: 'LOW', severity: 'low', confidence: 99 }), finding({ id: 'CRIT', severity: 'critical', confidence: 60 }),
    finding({ id: 'HIGH_A', confidence: 70 }), finding({ id: 'HIGH_B', confidence: 90 }),
  ], expected: ['CRIT', 'HIGH_B', 'HIGH_A', 'LOW'] },
  { name: 'off-enum winner retains original', cap: 1, input: [
    finding({ id: 'OFF_ENUM', severity: 'bogus', confidence: 95 }), finding({ id: 'LOW', severity: 'low', confidence: 80 }),
  ], expected: ['OFF_ENUM'], severity: 'bogus' },
  { name: 'empty pool', input: [], cap: 5, expected: [] }, { name: 'absent pool', absent: true, cap: 5, expected: [] },
];
for (const row of DELIVERY_ROWS) test(`delivery selection: ${row.name}`, () => {
  const input = row.absent ? undefined : structuredClone(row.input ?? MIXED);
  const before = JSON.stringify(input);
  const out = selectDelivery(input, row.cap, row.tier);
  assert.deepEqual(out.map((f) => f.id), row.expected ?? DELIVERY_GOLDEN);
  assert.equal(JSON.stringify(input), before);
  for (const f of out) assert.strictEqual(f, input.find((original) => original.id === f.id));
  if (row.severity) assert.equal(out[0].severity, row.severity);
  if (out.some((f) => f.id === 'SUGG_HIGH')) assert.equal(out.find((f) => f.id === 'SUGG_HIGH').report_tag, 'suggestion');
});

test('unconfirmed stage inputs expose no claimed address', async () => {
  const f = finding({ location_status: 'unconfirmed', line_start: 999, line_end: 1000, claimed_line_start: 999, anchor: 'SECRET_ANCHOR_TEXT' });
  const ctx = stageCtx({ reply: [] });
  await validateStage(ctx, { findings: [f] });
  assert.match(ctx.calls[0].prompt, /src\/x.py:location unconfirmed/);
  assert.doesNotMatch(ctx.calls[0].prompt, /999|1000|SECRET_ANCHOR_TEXT/);
  const blind = blindChallengeFields(f);
  assert.equal(blind.location, 'location unconfirmed');
  for (const key of ['line_start', 'line_end', 'anchor', 'claimed_line_start']) assert.equal(Object.hasOwn(blind, key), false);
});
