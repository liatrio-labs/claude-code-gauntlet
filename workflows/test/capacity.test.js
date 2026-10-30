import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  verifyCommand, verifyInlineCost, planVerifySlices, predictVerifySliceInlineLength, worstCaseAgentCount, coarsenLimits,
  verifyInlineBudget, effectiveVerifyBaseBranch, effectiveSliceSize, effectiveChallengeCap, effectiveBucketSize, effectiveBatchSize,
} from '../src/capacity.js';
import { STAGE_COSTS } from '../src/registry.js';
import { collectCitations, planCiteBatches, citeRecordKey, citeSource } from '../src/citeWire.js';
import { encodeSliceInline, projectVerifySliceFinding } from '../src/verifyWire.js';
import { summarize } from '../src/discover.js';
import { randomFindings, stageCtx } from './helpers/stageFixtures.js';

const BASIC = [
  { id: 'A', file: 'a.js', line_start: 1, origin: 'new', cross_file_refs: [] },
  { id: 'B', file: 'b.js', line_start: 2, origin: 'new', cross_file_refs: [] },
  { id: 'C', file: 'z'.repeat(100), line_start: 3, origin: 'new', cross_file_refs: [] },
];
const PLAN_GOLDEN = { slices: [['A', 'B'], ['C']], oversize: [], closeReasons: ['count', null] };

for (const key of ['repoRoot', 'outputDir', 'pluginRoot']) test(`Windows budget derives from ${key}`, () => {
  assert.ok(verifyInlineBudget({ [key]: 'd:/work/repo' }) < 32767);
  assert.equal(verifyInlineBudget({ [key]: '/work/repo' }), 50000);
});
const PLAN_ROWS = [
  { name: 'Windows quoted JSON budget', size: 25, budget: 250, hostPlatform: 'windows', length: 281,
    input: Array.from({ length: 4 }, (_, i) => ({ id: `F${i}`, file: 'a'.repeat(30) })),
    expected: { slices: [['F0', 'F1', 'F2'], ['F3']], oversize: [], closeReasons: ['budget', null] } },
  { name: 'count closes a slice', input: BASIC, size: 2, budget: 100000 },
  { name: 'single finding exceeds budget', input: [BASIC[0]], size: 2, budget: 10,
    expected: { slices: [], oversize: ['A'], closeReasons: [] } },
  { name: 'budget closes a slice', input: BASIC, size: 3, budget: 228,
    expected: { slices: [['A', 'B'], ['C']], oversize: [], closeReasons: ['budget', null] } },
  { name: 'oversize flushes preceding slice', size: 2, budget: 50000,
    input: [{ id: 'A', origin: 'new', cross_file_refs: [] },
      { id: 'BIG', origin: 'new', cross_file_refs: [], file: 'x'.repeat(50000) },
      { id: 'C', origin: 'new', cross_file_refs: [] }],
    expected: { slices: [['A'], ['C']], oversize: ['BIG'], closeReasons: ['oversize', null] } },
  { name: 'exact long-branch equality fits', size: 2, budget: 1311, branch: 'b'.repeat(1000), length: 1311,
    input: [
      { id: 'fit-a', file: 'x'.repeat(100), line_start: 1, origin: 'new', cross_file_refs: [] },
      { id: 'fit-b', file: 'y'.repeat(100), line_start: 2, origin: 'new', cross_file_refs: [] },
    ], expected: { slices: [['fit-a', 'fit-b']], oversize: [], closeReasons: [null] } },
];
for (const row of PLAN_ROWS) test(`slice planning: ${row.name}`, () => {
  const input = JSON.parse(JSON.stringify(row.input));
  const out = planVerifySlices(input, row.size, row.budget, row.branch, row.hostPlatform);
  assert.deepEqual(input, row.input);
  assert.deepEqual({ slices: out.slices.map((slice) => slice.map((f) => f.id)),
    oversize: out.oversize.map((f) => f.id), closeReasons: out.closeReasons }, row.expected ?? PLAN_GOLDEN);
  for (const slice of out.slices) {
    assert.ok(encodeSliceInline({ findings: slice.map(projectVerifySliceFinding),
      base_branch: row.branch ?? 'main' }).length <= row.budget);
  }
  if (row.length) assert.equal(predictVerifySliceInlineLength(input, row.branch, row.hostPlatform), row.length);
});
const COST_ROWS = [
  { branch: 'b', lengths: [1043, 970, 1072, 1073, 1122, 279] },
  { branch: 'main', lengths: [1046, 973, 1075, 1076, 1125, 282] },
  { branch: `feature/${'b'.repeat(99)}`, lengths: [1149, 1076, 1178, 1179, 1228, 385] },
  { branch: 'b'.repeat(1000), lengths: [2042, 1969, 2071, 2072, 2121, 1278] },
];
for (const row of COST_ROWS) test(`encoder costs: branch length ${row.branch.length}`, () => {
  const plan = planVerifySlices(randomFindings(275, 37), 7, 5000, row.branch);
  assert.deepEqual(plan.oversize, []);
  assert.deepEqual(plan.slices.map((slice) => predictVerifySliceInlineLength(slice, row.branch)), row.lengths);
  assert.deepEqual(plan.slices.map((slice) => encodeSliceInline({
    findings: slice.map(projectVerifySliceFinding), base_branch: row.branch,
  }).length), row.lengths);
});

const COUNT_GOLDEN = { summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 200 };
const COUNT_ROWS = [
  { name: 'ordinary estimate', limits: { ...COUNT_GOLDEN, validateBatch: 10 }, files: 500, findings: 3000, count: 405 },
  { name: 'absent caps challenge all', limits: { summarizeBucketSize: 20, validateBatch: 25, verifySliceSize: 200 },
    files: 0, findings: 1000, count: 1060 },
  { name: 'explicit all cap', limits: { ...COUNT_GOLDEN, challengeCap: 1000 }, files: 0, findings: 1000, count: 1060 },
  { name: 'zero cap is real', limits: { ...COUNT_GOLDEN, challengeCap: 0 }, files: 0, findings: 1000, count: 60 },
  { name: 'all sizes absent', limits: {}, files: 20000, findings: 1000, count: 2013 },
  { name: 'explicit default sizes', limits: { verifySliceSize: 100, validateBatch: 100, challengeCap: 40 },
    files: 200, findings: 100, count: 63 },
  { name: 'numeric estimate ignores wire split', limits: COUNT_GOLDEN, files: 0, findings: 2, count: 15 },
  { name: 'array wire split includes its retry', limits: COUNT_GOLDEN, files: 0,
    findings: [{ id: 'A', file: 'x'.repeat(30000) }, { id: 'B', file: 'y'.repeat(30000) }], count: 17 },
];
for (const row of COUNT_ROWS) test(`agent count: ${row.name}`, () => {
  const count = Array.isArray(row.findings) ? row.findings.length : row.findings;
  assert.equal(worstCaseAgentCount(row.limits, row.files, row.findings, 'main', {}, false), row.count + Math.ceil(count / Math.max(1, Math.ceil((row.limits.validateBatch || count || 1) / 5))));
});
const COARSEN_ROWS = [
  { name: 'huge findings', limits: { summarizeBucketSize: 20, validateBatch: 2, challengeCap: 900, verifySliceSize: 5 },
    files: 500, findings: 5000 },
  { name: 'summarize term dominates', limits: { ...COUNT_GOLDEN, validateBatch: 10 }, files: 20000, findings: 3000,
  },
  { name: 'absent cap becomes concrete', limits: { summarizeBucketSize: 20, validateBatch: 25, verifySliceSize: 200 },
    files: 0, findings: 5000 },
  { name: 'small review shape', limits: COUNT_GOLDEN, files: 23, findings: 40 },
  { name: 'smallest verify slice converges', limits: { ...COUNT_GOLDEN, verifySliceSize: 1 }, files: 20, findings: 5000,
  },
  { name: 'all limits absent converge', limits: {}, files: 20000, findings: 5000,
  },
  { name: 'wire-bound term stops making progress',
    limits: { summarizeBucketSize: 20, validateBatch: 5000, challengeCap: 5, verifySliceSize: 1 }, files: 0,
    findings: Array.from({ length: 1000 }, (_, i) => ({ id: `F${i}`, file: 'x'.repeat(22000) })) },
];
for (const row of COARSEN_ROWS) test(`coarsening: ${row.name}`, () => {
  const before = JSON.stringify(row.limits);
  const known = Array.isArray(row.findings) ? row.findings.map(f=>({...f,skeptical_assessments:[]})) : Array.from({length:row.findings},(_,i)=>({id:String(i),skeptical_assessments:[]}));
  const coarse = coarsenLimits(row.limits, row.files, known);
  assert.equal(JSON.stringify(row.limits), before);
  assert.ok(worstCaseAgentCount(coarse, row.files, row.findings, 'main', {}, false) <= worstCaseAgentCount(row.limits, row.files, row.findings, 'main', {}, false));
  if (row.name !== 'wire-bound term stops making progress') assert.ok(worstCaseAgentCount(coarse, row.files, row.findings, 'main', {}, false) < 900);
});
const BRANCH_ROWS = [
  { name: 'windows command budget', branch: 'main', input: { repoRoot: 'D:/repo' }, slices: 800, count: 1612 },
  { name: 'main', branch: 'main', slices: 400, count: 812 },
  { name: 'long branch binds wire budget', branch: `${'b'.repeat(249)}/${'b'.repeat(249)}/${'b'.repeat(249)}/${'b'.repeat(250)}`,
    slices: 800, count: 1612 },
];
for (const row of BRANCH_ROWS) test(`array guard branch: ${row.name}`, () => {
  const findings = Array.from({ length: 800 }, (_, i) => ({ id: `F${i}`, file: (i % 2 ? 'y' : 'x').repeat(24500), skeptical_assessments: [] }));
  const limits = { summarizeBucketSize: 20, validateBatch: 5000, challengeCap: 0, verifySliceSize: 25 };
  assert.equal(planVerifySlices(findings, 25, verifyInlineBudget(row.input), row.branch).slices.length, row.slices);
  assert.equal(worstCaseAgentCount(limits, 0, findings, row.branch, row.input), row.count);
  const coarse = coarsenLimits(limits, 0, findings, row.branch, row.input);
  assert.equal(coarse.challengeCap, 0);
  assert.ok(worstCaseAgentCount(coarse, 0, findings, row.branch, row.input) <= row.count);
});

test('cite capacity reserves future proof and reuses completed attempts', () => {
  assert.equal(STAGE_COSTS.cite.attempts, 2);
  const limits = { summarizeBucketSize: 20, verifySliceSize: 10, validateBatch: 2, challengeCap: 5 };
  assert.equal(worstCaseAgentCount(limits, 0, 0), 10);
  const without = worstCaseAgentCount(limits, 0, 5, 'main', {}, false);
  assert.ok(worstCaseAgentCount(limits, 0, 5) > without);
  const noChallenge = { ...limits, challengeCap: 0 };
  assert.ok(worstCaseAgentCount(noChallenge, 0, 5) > worstCaseAgentCount(noChallenge, 0, 5, 'main', {}, false));
  const known = [{id:'F',skeptical_assessments:[]}];
  assert.equal(worstCaseAgentCount(limits,0,known),worstCaseAgentCount(limits,0,known,'main',{},false));
});

test('cite reserve includes both skeptical stages and byte bounded retries', () => {
  const limits = { verifySliceSize: 10, validateBatch: 1, challengeCap: 0 };
  const request = { finding_id: 'F', stage: 'validator', request_id: 'claim', role: 'claim',
    snapshot_id: 'snapshot', file: 'source', side: 'head', line_start: 1, line_end: 1, quote: 'return source_value;' };
  const finding = { id: 'F', skeptical_assessments: [{ stage: 'validator', verdict: 'refuted', citations: [request] }] };
  const empty = [{ ...finding, skeptical_assessments: [] }];
  const base = worstCaseAgentCount(limits, 0, empty);
  assert.equal(worstCaseAgentCount(limits, 0, [finding]) - base, 2);
  finding.skeptical_assessments.push({ stage: 'challenger', citations: [{ ...request, stage: 'challenger' }] });
  assert.equal(worstCaseAgentCount(limits, 0, [finding]) - base, 2);
  assert.equal(worstCaseAgentCount({ ...limits, validateBatch: 2 }, 0, [finding])
    - worstCaseAgentCount({ ...limits, validateBatch: 2 }, 0, empty), 2);
  const records = collectCitations([finding]);
  const input = { verify: { sourceCapture: { manifest_sha256: 'snapshot' } } };
  const source = citeSource(input.verify.sourceCapture);
  input.cite_ledger = { entries: records.map((r) => ({ key: citeRecordKey(r, source),
    result: { request: r.request, source, status: 'matched', detail: 'explicit_range' } })),
  batches: [{ keys: records.map((r) => citeRecordKey(r, source)), attempts: 1 }] };
  assert.equal(worstCaseAgentCount(limits, 0, [finding], 'main', input) - base, 0);
  delete input.cite_ledger.entries[0].result;
  assert.equal(worstCaseAgentCount(limits, 0, [finding], 'main', input) - base, 1);
  input.cite_ledger.batches[0].attempts = 2;
  assert.equal(worstCaseAgentCount(limits, 0, [finding], 'main', input) - base, 0);
  const budget = Math.max(...records.map((r) => encodeSliceInline({ requests: [r], base_branch: 'main' }).length));
  assert.equal(planCiteBatches(records, budget, 'main').batches.length, 2);
  const huge = { ...records[0], request: { ...request, file: 'é'.repeat(10000) } };
  assert.deepEqual(planCiteBatches([huge, records[1]], 50000, 'main').oversize, [huge]);
});
const EMPTY_BRANCH_ROWS = [undefined, null, '', 'main'];
for (const [index, branch] of EMPTY_BRANCH_ROWS.entries()) test(`empty branch normalization: ${index}`, () => {
  const findings = Array.from({ length: 4 }, (_, i) => ({ id: `F${i}`, file: 'x'.repeat(100) }));
  assert.equal(effectiveVerifyBaseBranch(branch), 'main');
  assert.deepEqual(planVerifySlices(findings, 25, 352, branch).slices.map((slice) => slice.map((f) => f.id)),
    [['F0', 'F1'], ['F2', 'F3']]);
  assert.equal(worstCaseAgentCount({ validateBatch: 5000, challengeCap: 0, verifySliceSize: 25 }, 0, findings, branch, {}, false), 14);
});
const ACCESSOR_ROWS = [
  { name: 'absent', limits: {}, n: 7, expected: [20, 7, 7, 7] },
  { name: 'zero', limits: { summarizeBucketSize: 0, validateBatch: 0, verifySliceSize: 0, challengeCap: 0 },
    n: 7, expected: [20, 7, 7, 0] },
  { name: 'empty findings', limits: {}, n: 0, expected: [20, 1, 1, 0] },
  { name: 'explicit', limits: { summarizeBucketSize: 3, validateBatch: 2, verifySliceSize: 4, challengeCap: 5 },
    n: 7, expected: [3, 2, 4, 5] },
];
for (const row of ACCESSOR_ROWS) test(`effective accessors: ${row.name}`, () => {
  assert.deepEqual([effectiveBucketSize(row.limits), effectiveBatchSize(row.limits, row.n),
    effectiveSliceSize(row.limits, row.n), effectiveChallengeCap(row.limits, row.n)], row.expected);
});
test('absent bucket limit: 47 files', async () => {
  const ctx = stageCtx({ reply: { summary: 's' } });
  const out = await summarize(ctx, { changedFiles: Array.from({ length: 47 }, (_, i) => `f${i}.js`),
    changedLines: 1211, limits: {} });
  assert.equal(worstCaseAgentCount({}, 47, 0), 13);
  assert.deepEqual(ctx.calls.map((call) => call.label), [
    'summarize-bucket-0', 'summarize-bucket-1', 'summarize-bucket-2', 'summarize-merge',
  ]);
  assert.deepEqual(out, { summary: 's', gaps: [] });
  assert.deepEqual(ctx.violations, []);
  for (const call of ctx.calls) {
    assert.ok(call.prompt.includes('The authoritative changed-line count is 1211; cite that number verbatim'));
    assert.deepEqual(call.schema, { type: 'object', properties: { summary: { type: 'string' } }, required: ['summary'] });
  }
});


test('anchor and map flags obey encoded command budget', () => {
  const anchor = Array.from({ length: 4 }, () => '\u{1F600}'.repeat(90)).join('\n');
  const findings = Array.from({ length: 6 }, (_, n) => ({ id: `f${n}`, file: 'source', anchor }));
  const inp = { repoRoot: 'D:/repo', outputDir: 'D:/out', nonce: 'n', headShaShort: 'abcd', verify: {
    targetKind: 'local', contextFile: 'D:/out/context.md', contextMap: 'D:/out/map.json', diffPath: 'D:/out/raw.patch',
    sourceCapture: { nonce: 'n', manifest_sha256: 'a'.repeat(64) },
  } };
  const one = predictVerifySliceInlineLength(findings.slice(0, 1), 'main', 'windows');
  const plan = planVerifySlices(findings, 6, one + 2, 'main', 'windows');
  assert.equal(plan.slices.length, 6);
  for (const slice of plan.slices) {
    const content = { findings: slice.map(projectVerifySliceFinding), base_branch: 'main' };
    const token = encodeSliceInline(content);
    assert.equal(predictVerifySliceInlineLength(slice, 'main', 'windows'), verifyInlineCost(token, 'windows'));
    assert.equal(slice[0].anchor, anchor);
    const command = verifyCommand(inp, 0, 'n.0', token);
    assert.ok(command.includes('--target-kind local'));
    assert.ok(command.includes('--context-map D:/out/map.json'));
    assert.ok(command.includes('--context-file D:/out/context.md'));
    assert.ok(command.includes('--source-capture'));
    assert.ok(command.includes('--capture-output-dir D:/out'));
  }
  assert.ok(verifyInlineBudget(inp) < verifyInlineBudget({ ...inp, verify: {} }));
});
