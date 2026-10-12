import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  planVerifySlices, predictVerifySliceInlineLength, worstCaseAgentCount, coarsenLimits,
  effectiveVerifyBaseBranch, effectiveSliceSize, effectiveChallengeCap, effectiveBucketSize, effectiveBatchSize,
} from '../src/capacity.js';
import { encodeSliceInline, projectVerifySliceFinding } from '../src/verifyWire.js';
import { summarize } from '../src/discover.js';
import { randomFindings, stageCtx } from './helpers/stageFixtures.js';

const BASIC = [
  { id: 'A', file: 'a.js', line_start: 1, origin: 'new', cross_file_refs: [] },
  { id: 'B', file: 'b.js', line_start: 2, origin: 'new', cross_file_refs: [] },
  { id: 'C', file: 'c.js', line_start: 3, origin: 'new', cross_file_refs: [], description: 'z'.repeat(100) },
];
const PLAN_GOLDEN = { slices: [['A', 'B'], ['C']], oversize: [], closeReasons: ['count', null] };
const PLAN_ROWS = [
  { name: 'count closes a slice', input: BASIC, size: 2, budget: 100000 },
  { name: 'single finding exceeds budget', input: [BASIC[0]], size: 2, budget: 10,
    expected: { slices: [], oversize: ['A'], closeReasons: [] } },
  { name: 'budget closes a slice', input: BASIC, size: 3, budget: 228,
    expected: { slices: [['A', 'B'], ['C']], oversize: [], closeReasons: ['budget', null] } },
  { name: 'oversize flushes preceding slice', size: 2, budget: 50000,
    input: [{ id: 'A', origin: 'new', cross_file_refs: [] },
      { id: 'BIG', origin: 'new', cross_file_refs: [], description: 'x'.repeat(50000) },
      { id: 'C', origin: 'new', cross_file_refs: [] }],
    expected: { slices: [['A'], ['C']], oversize: ['BIG'], closeReasons: ['oversize', null] } },
  { name: 'exact long-branch equality fits', size: 2, budget: 1425, branch: 'b'.repeat(1000), length: 1425,
    input: [
      { id: 'fit-a', file: 'a.js', line_start: 1, origin: 'new', cross_file_refs: [], description: 'x'.repeat(100) },
      { id: 'fit-b', file: 'b.js', line_start: 2, origin: 'new', cross_file_refs: [], description: 'y'.repeat(100) },
    ], expected: { slices: [['fit-a', 'fit-b']], oversize: [], closeReasons: [null] } },
];
for (const row of PLAN_ROWS) test(`slice planning: ${row.name}`, () => {
  const input = JSON.parse(JSON.stringify(row.input));
  const out = planVerifySlices(input, row.size, row.budget, row.branch);
  assert.deepEqual(input, row.input);
  assert.deepEqual({ slices: out.slices.map((slice) => slice.map((f) => f.id)),
    oversize: out.oversize.map((f) => f.id), closeReasons: out.closeReasons }, row.expected ?? PLAN_GOLDEN);
  for (const slice of out.slices) {
    assert.ok(encodeSliceInline({ findings: slice.map(projectVerifySliceFinding),
      base_branch: row.branch ?? 'main' }).length <= row.budget);
  }
  if (row.length) assert.equal(predictVerifySliceInlineLength(input, row.branch), row.length);
});
const COST_ROWS = [
  { branch: 'b', lengths: [3952, 4102, 3863, 3420, 4037, 851] },
  { branch: 'main', lengths: [3955, 4105, 3866, 3423, 4040, 854] },
  { branch: `feature/${'b'.repeat(99)}`, lengths: [4058, 4208, 3969, 3526, 4143, 957] },
  { branch: 'b'.repeat(1000), lengths: [4951, 4562, 4999, 4419, 4848, 2440] },
];
for (const row of COST_ROWS) test(`encoder costs: branch length ${row.branch.length}`, () => {
  const plan = planVerifySlices(randomFindings(275, 37), 7, 5000, row.branch);
  assert.deepEqual(plan.oversize, []);
  assert.deepEqual(plan.slices.map((slice) => predictVerifySliceInlineLength(slice, row.branch)), row.lengths);
  assert.deepEqual(plan.slices.map((slice) => encodeSliceInline({
    findings: slice.map(projectVerifySliceFinding), base_branch: row.branch,
  }).length), row.lengths);
  for (const length of row.lengths) assert.ok(length <= 5000);
});

const COUNT_GOLDEN = { summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 200 };
const COUNT_ROWS = [
  { name: 'ordinary estimate', limits: { ...COUNT_GOLDEN, validateBatch: 10 }, files: 500, findings: 3000, count: 405 },
  { name: 'absent caps challenge all', limits: { summarizeBucketSize: 20, validateBatch: 25, verifySliceSize: 200 },
    files: 0, findings: 1000, count: 1060 },
  { name: 'explicit all cap', limits: { ...COUNT_GOLDEN, challengeCap: 1000 }, files: 0, findings: 1000, count: 1060 },
  { name: 'zero cap is real', limits: { ...COUNT_GOLDEN, challengeCap: 0 }, files: 0, findings: 1000, count: 60 },
  { name: 'all sizes absent', limits: {}, files: 20000, findings: 1000, count: 2013 },
  { name: 'zero sizes mirror stage defaults',
    limits: { summarizeBucketSize: 0, verifySliceSize: 0, validateBatch: 0, challengeCap: 40 },
    files: 200, findings: 100, count: 63 },
  { name: 'explicit default sizes', limits: { verifySliceSize: 100, validateBatch: 100, challengeCap: 40 },
    files: 200, findings: 100, count: 63 },
  { name: 'numeric estimate ignores wire split', limits: COUNT_GOLDEN, files: 0, findings: 2, count: 15 },
  { name: 'array wire split includes its retry', limits: COUNT_GOLDEN, files: 0,
    findings: [{ id: 'A', description: 'x'.repeat(30000) }, { id: 'B', description: 'y'.repeat(30000) }], count: 17 },
];
for (const row of COUNT_ROWS) test(`agent count: ${row.name}`, () => {
  assert.equal(worstCaseAgentCount(row.limits, row.files, row.findings), row.count);
});
const COARSEN_ROWS = [
  { name: 'huge findings', limits: { summarizeBucketSize: 20, validateBatch: 2, challengeCap: 900, verifySliceSize: 5 },
    files: 500, findings: 5000, expected: { summarizeBucketSize: 20, validateBatch: 16, challengeCap: 225, verifySliceSize: 40 },
    count: 823 },
  { name: 'summarize term dominates', limits: { ...COUNT_GOLDEN, validateBatch: 10 }, files: 20000, findings: 3000,
    expected: { summarizeBucketSize: 40, validateBatch: 10, challengeCap: 40, verifySliceSize: 200 }, count: 880 },
  { name: 'absent cap becomes concrete', limits: { summarizeBucketSize: 20, validateBatch: 25, verifySliceSize: 200 },
    files: 0, findings: 5000,
    expected: { summarizeBucketSize: 20, validateBatch: 25, verifySliceSize: 200, challengeCap: 625 }, count: 885 },
  { name: 'benchmark shape unchanged', limits: COUNT_GOLDEN, files: 23, findings: 40, expected: COUNT_GOLDEN, count: 56 },
  { name: 'smallest verify slice converges', limits: { ...COUNT_GOLDEN, verifySliceSize: 1 }, files: 20, findings: 5000,
    expected: { summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 16 }, count: 877 },
  { name: 'all limits absent converge', limits: {}, files: 20000, findings: 5000,
    expected: { summarizeBucketSize: 80, challengeCap: 625 }, count: 888 },
  { name: 'wire-bound term stops making progress',
    limits: { summarizeBucketSize: 20, validateBatch: 5000, challengeCap: 5, verifySliceSize: 1 }, files: 0,
    findings: Array.from({ length: 1000 }, (_, i) => ({ id: `F${i}`, description: 'x'.repeat(22000) })),
    expected: { summarizeBucketSize: 20, validateBatch: 10000, challengeCap: 5, verifySliceSize: 4 }, count: 1016 },
];
for (const row of COARSEN_ROWS) test(`coarsening: ${row.name}`, () => {
  const before = JSON.stringify(row.limits);
  const coarse = coarsenLimits(row.limits, row.files, row.findings);
  assert.deepEqual(coarse, row.expected);
  assert.equal(JSON.stringify(row.limits), before);
  assert.equal(worstCaseAgentCount(coarse, row.files, row.findings), row.count);
  assert.ok(row.name === 'wire-bound term stops making progress' ? row.count >= 900 : row.count < 900);
});
const BRANCH_ROWS = [
  { name: 'main', branch: 'main', slices: 400, count: 811, coarseCount: 811,
    coarse: { summarizeBucketSize: 20, validateBatch: 5000, challengeCap: 0, verifySliceSize: 25 } },
  { name: 'long branch binds wire budget', branch: `${'b'.repeat(249)}/${'b'.repeat(249)}/${'b'.repeat(249)}/${'b'.repeat(250)}`,
    slices: 800, count: 1611, coarseCount: 1616,
    coarse: { summarizeBucketSize: 20, validateBatch: 10000, challengeCap: 5, verifySliceSize: 50 } },
];
for (const row of BRANCH_ROWS) test(`array guard branch: ${row.name}`, () => {
  const findings = Array.from({ length: 800 }, (_, i) => ({ id: `F${i}`, description: (i % 2 ? 'y' : 'x').repeat(24500) }));
  const limits = { summarizeBucketSize: 20, validateBatch: 5000, challengeCap: 0, verifySliceSize: 25 };
  assert.equal(planVerifySlices(findings, 25, 50000, row.branch).slices.length, row.slices);
  assert.equal(worstCaseAgentCount(limits, 0, findings, row.branch), row.count);
  assert.deepEqual(coarsenLimits(limits, 0, findings, row.branch), row.coarse);
  assert.equal(worstCaseAgentCount(row.coarse, 0, findings, row.branch), row.coarseCount);
});
const EMPTY_BRANCH_ROWS = [undefined, null, '', 'main'];
for (const [index, branch] of EMPTY_BRANCH_ROWS.entries()) test(`empty branch normalization: ${index}`, () => {
  const findings = Array.from({ length: 4 }, (_, i) => ({ id: `F${i}`, description: 'x'.repeat(100) }));
  assert.equal(effectiveVerifyBaseBranch(branch), 'main');
  assert.deepEqual(planVerifySlices(findings, 25, 352, branch).slices.map((slice) => slice.map((f) => f.id)),
    [['F0', 'F1'], ['F2', 'F3']]);
  assert.equal(worstCaseAgentCount({ validateBatch: 5000, challengeCap: 0, verifySliceSize: 25 }, 0, findings, branch), 13);
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
const BUCKET_ROWS = [{ files: 47, count: 13, labels: [
  'summarize-bucket-0', 'summarize-bucket-1', 'summarize-bucket-2', 'summarize-merge',
] }];
for (const row of BUCKET_ROWS) test(`absent bucket limit: ${row.files} files`, async () => {
  const ctx = stageCtx({ reply: { summary: 's' } });
  const out = await summarize(ctx, { changedFiles: Array.from({ length: row.files }, (_, i) => `f${i}.js`),
    changedLines: 1211, limits: {} });
  assert.equal(worstCaseAgentCount({}, 47, 0), row.count);
  assert.deepEqual(ctx.calls.map((call) => call.label), row.labels);
  assert.deepEqual(out, { summary: 's', gaps: [] });
  assert.deepEqual(ctx.violations, []);
  for (const call of ctx.calls) {
    assert.ok(call.prompt.includes('The authoritative changed-line count is 1211; cite that number verbatim'));
    assert.deepEqual(call.schema, { type: 'object', properties: { summary: { type: 'string' } }, required: ['summary'] });
  }
});
