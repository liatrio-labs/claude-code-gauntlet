import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateStage, filterStage, challengeStage, blindChallengeFields, selectDelivery } from '../src/reviewStages.js';
import { finding } from './helpers/findings.js';
import { stageCtx } from './helpers/stageFixtures.js';

const omittedGap = (idx, count) => `validate-batch-${idx}: validator omitted scores — ${count} finding(s) unvalidated (validation=skipped, held back to the report)`;
const VALIDATE_STATS = { batches_dispatched: 1, batches_completed: 1, validated: 1, skipped: 1, adjusted: 1 };
const VALIDATE_ROWS = [
  { name: 'id adjustment', input: [finding({ id: 'F1', confidence: 90 }), finding({ id: 'F2', confidence: 80 })],
    reply: [{ id: 'F1', confidence: 50, justification: 'weak' }, { id: 'F2', confidence: 'invalid' }, null],
    gaps: [omittedGap(0, 1)],
    expected: [['F1', 50, 90, 'weak', undefined, 'validated'], ['F2', 80, undefined, undefined, undefined, 'skipped']] },
  { name: 'finding_id adjustment', input: [finding({ id: 'F1', confidence: 90 }), finding({ id: 'F2', confidence: 80 })],
    reply: [{ finding_id: 'F1', confidence: 40, justification: 'reachable only hypothetically' }], gaps: [omittedGap(0, 1)],
    expected: [['F1', 40, 90, 'reachable only hypothetically', undefined, 'validated'],
      ['F2', 80, undefined, undefined, undefined, 'skipped']] },
  { name: 'validations envelope', input: [finding({ confidence: 40 })],
    reply: { validations: [{ id: 'f1', confidence: 85 }] }, stats: { ...VALIDATE_STATS, validated: 1, skipped: 0 },
    expected: [['f1', 85, 40, undefined, undefined, 'validated']] },
  { name: 'known reachability', input: [finding({ id: 'F1', confidence: 80 })],
    reply: [{ finding_id: 'F1', confidence: 80, reachability: 'future_change_only' }],
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
  assert.deepEqual(Object.keys(out), ['findings', 'gaps', 'stats']);
  assert.deepEqual(out.findings.map((f) => [f.id, f.confidence, f.original_confidence,
    f.validation_justification, f.reachability, f.validation]), row.expected);
  assert.deepEqual(out.stats, row.stats ?? VALIDATE_STATS);
  assert.deepEqual(out.gaps, row.gaps ?? []);
  assert.deepEqual(ctx.calls.map((call) => call.label), row.labels ?? ['validate-batch-0']);
  for (const call of ctx.calls) assert.equal(call.agentType, 'code-gauntlet:validator');
  assert.deepEqual(ctx.violations, []);
});
const VALIDATE_TAIL = '\nReturn { validations: [{ finding_id, confidence, justification, '
  + 'reachability (current | future_change_only | uncertain) }] } \u2014 confidence 0-100 '
  + '(one entry per finding).';
const VALIDATE_PROMPT_ROWS = [
  { name: 'equal range endpoints print once', fields: { file: 'one.js', line_start: 42, line_end: 42 },
    block: '- f1 [bug/high] one.js:42 \u2014 The function returns the wrong result for a real input.' },
  { name: 'single line', fields: { file: 'SENTINEL_VALIDATE_FILE.js', line_start: 42, description: 'SENTINEL_VALIDATE_DESC' },
    block: '- f1 [bug/high] SENTINEL_VALIDATE_FILE.js:42 \u2014 SENTINEL_VALIDATE_DESC' },
  { name: 'range and evidence', fields: { file: 'range.js', line_start: 12, line_end: 20, evidence: 'INSPECTED_LINES' },
    block: '- f1 [bug/high] range.js:12-20 \u2014 The function returns the wrong result for a real input. | evidence: INSPECTED_LINES' },
];
for (const row of VALIDATE_PROMPT_ROWS) test(`validate prompt: ${row.name}`, async () => {
  const ctx = stageCtx({ reply: [] });
  await validateStage(ctx, { findings: [finding(row.fields)], contextLine: 'VALIDATOR_CONTEXT ' });
  assert.ok(ctx.calls[0].prompt.startsWith('VALIDATOR_CONTEXT Independently validate'));
  assert.ok(ctx.calls[0].prompt.includes('code-evidence and disproof contract'));
  assert.ok(ctx.calls[0].prompt.includes(row.block));
  assert.ok(ctx.calls[0].prompt.endsWith(VALIDATE_TAIL));
  assert.deepEqual(ctx.calls[0].schema, { type: 'object', properties: { validations: {
    type: 'array', items: { type: 'object', properties: {
      finding_id: { type: 'string' }, confidence: { type: 'number' }, justification: { type: 'string' },
      reachability: { type: 'string', enum: ['current', 'future_change_only', 'uncertain'] },
    }, required: ['finding_id', 'confidence'] },
  } }, required: ['validations'] });
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

const CHALLENGE_GOLDEN = {
  total_input: 1, dispatched: 1, completed: 1, skipped: 0, challenge_removed: 0, challenge_downgraded: 0,
  challenge_contested: 0, challenge_survived: 1, unchallenged: 0, cross_agent_consolidated: 0, final_count: 1,
};
const CHALLENGE_ROWS = [
  { name: 'negative cap clamps to zero', input: [finding({ id: 'F1' }), finding({ id: 'F2', severity: 'critical' })],
    cap: -1, reply: { score: 80 }, kept: [], scores: [], labels: [], unverified: [['F2', 'capped'], ['F1', 'capped']],
    gaps: ['challenge: 2 finding(s) over challengeCap=0 left unchallenged (challenge=capped, pipeline-degraded)'],
    stats: { ...CHALLENGE_GOLDEN, total_input: 2, dispatched: 0, completed: 0, skipped: 2,
      challenge_survived: 0, final_count: 0 } },
  { name: 'failed candidates stay ranked in unverified', input: [finding({ id: 'LOW', severity: 'low' }),
    finding({ id: 'HIGH', severity: 'high' }), finding({ id: 'CRIT', severity: 'critical' })],
    reply: null, kept: [], scores: [], labels: ['challenge-0', 'challenge-1', 'challenge-2'],
    unverified: [['CRIT', 'failed'], ['HIGH', 'failed'], ['LOW', 'failed']], gaps: [
      'challenge-0: challenger returned null/unscored \u2014 finding CRIT unchallenged (challenge=failed, pipeline-degraded)',
      'challenge-1: challenger returned null/unscored \u2014 finding HIGH unchallenged (challenge=failed, pipeline-degraded)',
      'challenge-2: challenger returned null/unscored \u2014 finding LOW unchallenged (challenge=failed, pipeline-degraded)',
    ], stats: { ...CHALLENGE_GOLDEN, total_input: 3, dispatched: 3, completed: 0, skipped: 3,
      challenge_survived: 0, final_count: 0 } },
  { name: 'legacy score and stage envelope', input: [finding()], reply: { score: 80 }, kept: ['f1'], scores: [80] },
  { name: 'canonical scores follow ranked indices', input: [finding({ id: 'F1' }), finding({ id: 'F2', severity: 'critical' })],
    reply: { confidence_claim_is_correct: 90, justification: 'claim holds' }, kept: ['F2', 'F1'], scores: [90, 90],
    labels: ['challenge-0', 'challenge-1'], stats: { ...CHALLENGE_GOLDEN,
      total_input: 2, dispatched: 2, completed: 2, challenge_survived: 2, final_count: 2 }, justifications: ['claim holds',
        'claim holds'] },
  { name: 'legacy low score removes', input: [finding({ id: 'F1', validation: 'skipped', origin: 'unknown' })], reply: { score: 10, justification: 'does not hold' },
    kept: [], scores: [], eliminated: ['F1'],
    stats: { ...CHALLENGE_GOLDEN, challenge_removed: 1, challenge_survived: 0, final_count: 0 } },
  { name: 'canonical zero is scored', input: [finding({ id: 'F1' })],
    reply: { confidence_claim_is_correct: 0, justification: 'disproved' }, kept: [], scores: [], eliminated: ['F1'],
    stats: { ...CHALLENGE_GOLDEN, challenge_removed: 1, challenge_survived: 0, final_count: 0 } },
  { name: 'invalid score stays unverified', input: [finding({ id: 'F1' })], reply: { score: 'not-a-number', justification: 'x' },
    kept: [], scores: [], unverified: [['F1', 'failed']], gaps: [
      'challenge-0: challenger returned null/unscored \u2014 finding F1 unchallenged (challenge=failed, pipeline-degraded)',
    ], stats: { ...CHALLENGE_GOLDEN, completed: 0, skipped: 1, challenge_survived: 0, final_count: 0 } },
  { name: 'null member is positional', input: [finding({ id: 'F1', severity: 'critical' }), finding({ id: 'F2', severity: 'low' })],
    reply: { score: 80 }, nulls: ['challenge-1'], kept: ['F1'], scores: [80], unverified: [['F2', 'failed']],
    labels: ['challenge-0', 'challenge-1'], gaps: [
      'challenge-1: challenger returned null/unscored \u2014 finding F2 unchallenged (challenge=failed, pipeline-degraded)',
    ], stats: { ...CHALLENGE_GOLDEN, total_input: 2, dispatched: 2, skipped: 1 } },
  { name: 'rejection does not refill cap', input: [finding({ id: 'FIRST', severity: 'critical' }),
    finding({ id: 'OVERFLOW', severity: 'low' })], reply: { score: 0 }, cap: 1,
    kept: [], scores: [], eliminated: ['FIRST'], unverified: [['OVERFLOW', 'capped']],
    gaps: ['challenge: 1 finding(s) over challengeCap=1 left unchallenged (challenge=capped, pipeline-degraded)'],
    stats: { ...CHALLENGE_GOLDEN, total_input: 2, skipped: 1, challenge_removed: 1, challenge_survived: 0, final_count: 0 } },
  { name: 'cap overflow stays unverified', input: [finding({ id: 'F3', severity: 'low' }),
    finding({ id: 'F2' }), finding({ id: 'F1', severity: 'critical' })], reply: { score: 80 }, cap: 2,
    kept: ['F1', 'F2'], scores: [80, 80], unverified: [['F3', 'capped']], labels: ['challenge-0', 'challenge-1'], gaps: [
      'challenge: 1 finding(s) over challengeCap=2 left unchallenged (challenge=capped, pipeline-degraded)',
    ], stats: { ...CHALLENGE_GOLDEN, total_input: 3, dispatched: 2, completed: 2, skipped: 1,
      challenge_survived: 2, final_count: 2 } },
  { name: 'clean candidates fill cap first', input: [finding({ id: 'DIRTY', severity: 'critical', validation: 'skipped' }),
    finding({ id: 'CLEAN', severity: 'low' })], reply: { score: 80 }, cap: 1, kept: ['CLEAN'], scores: [80],
    unverified: [['DIRTY', 'capped']], gaps: [
      'challenge: 1 finding(s) over challengeCap=1 left unchallenged (challenge=capped, pipeline-degraded)',
    ], stats: { ...CHALLENGE_GOLDEN, total_input: 2, skipped: 1 } },
  { name: 'empty with timestamp', input: [], reply: { score: 80 }, kept: [], scores: [], labels: [],
    generatedAt: '2026-07-18T00:00:00Z',
    stats: { ...CHALLENGE_GOLDEN, total_input: 0, dispatched: 0, completed: 0, challenge_survived: 0, final_count: 0 } },
];
for (const row of CHALLENGE_ROWS) test(`challenge result: ${row.name}`, async () => {
  const ctx = stageCtx({ reply: row.reply, nulls: row.nulls });
  const input = structuredClone(row.input);
  const out = await challengeStage(ctx, { findings: input, limits: { challengeCap: row.cap ?? 40 },
    generatedAt: row.generatedAt ?? 't' });
  assert.deepEqual(input, row.input);
  assert.deepEqual(Object.keys(out), ['findings', 'unverified', 'eliminated', 'gaps', 'stats', 'generated_at']);
  assert.deepEqual(out.findings.map((f) => f.id), row.kept);
  assert.deepEqual(out.findings.map((f) => f.challenge_score), row.scores);
  if (row.justifications) assert.deepEqual(out.findings.map((f) => f.challenge_justification), row.justifications);
  assert.deepEqual(out.unverified.map((f) => [f.id, f.challenge]), row.unverified ?? []);
  assert.deepEqual(out.eliminated.map((f) => f.id), row.eliminated ?? []);
  assert.deepEqual(out.gaps, row.gaps ?? []);
  assert.deepEqual(out.stats, row.stats ?? CHALLENGE_GOLDEN);
  assert.equal(out.generated_at, row.generatedAt ?? 't');
  assert.deepEqual(ctx.calls.map((call) => call.label), row.labels ?? ['challenge-0']);
  for (const call of ctx.calls) assert.equal(call.agentType, 'code-gauntlet:challenger');
  assert.deepEqual(ctx.violations, []);
});
const BLIND_TAIL = '\nReturn { confidence_claim_is_correct, justification }; confidence_claim_is_correct '
  + '0-100 (higher = the claim holds).';
const BLIND_ROWS = [
  { name: 'range', file: 'SENTINEL_FILE.js', start: 12, end: 20, location: 'SENTINEL_FILE.js:12-20' },
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
  assert.ok(ctx.calls[0].prompt.includes('Claim: SENTINEL_TITLE\nSENTINEL_DESC'));
  assert.ok(ctx.calls[0].prompt.includes(`Location to inspect: ${row.location}`));
  assert.ok(ctx.calls[0].prompt.endsWith(BLIND_TAIL));
  assert.doesNotMatch(ctx.calls[0].prompt, /SENTINEL_EVIDENCE_LEAK|SENTINEL_REASONING_LEAK|SENTINEL_XREF_LEAK|SENTINEL_RATIONALE_LEAK|96|surfaced/);
  assert.deepEqual(ctx.calls[0].schema, { type: 'object', properties: {
    confidence_claim_is_correct: { type: 'number' }, justification: { type: 'string' },
  }, required: ['confidence_claim_is_correct'] });
});

const MIXED = [
  finding({ id: 'SUGG_LOW', severity: 'low', report_tag: 'suggestion', report_destination: 'suggestion' }),
  finding({ id: 'MAIN_MED', severity: 'medium', report_tag: 'main', report_destination: 'main' }),
  finding({ id: 'SUGG_HIGH', severity: 'high', report_tag: 'suggestion', report_destination: 'suggestion' }),
  finding({ id: 'MAIN_CRIT', severity: 'critical', report_tag: 'main', report_destination: 'main' }),
];
const DELIVERY_GOLDEN = ['MAIN_CRIT', 'SUGG_HIGH', 'MAIN_MED', 'SUGG_LOW'];
const DELIVERY_ROWS = [
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
