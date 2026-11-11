import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  verifyCommand, verifyInlineCost, planVerifySlices, predictVerifySliceInlineLength, worstCaseAgentCount, coarsenLimits,
  verifyInlineBudgets, effectiveVerifyBaseBranch, effectiveSliceSize, effectiveChallengeCap, effectiveBucketSize, effectiveBatchSize,
} from '../src/capacity.js';
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
  const windows = verifyInlineBudgets({ [key]: 'd:/work/repo' });
  assert.equal(windows.copy, 5000);
  assert.ok(windows.hard < 32767);
  assert.deepEqual(verifyInlineBudgets({ [key]: '/work/repo' }), { copy: 5000, hard: 50000 });
  const input = { [key]: 'd:/work/repo', verify: { contextFile: 'x'.repeat(27000) } };
  const constrained = verifyInlineBudgets(input);
  assert.equal(constrained.copy, constrained.hard);
  assert.ok(constrained.copy < 5000);
  assert.deepEqual(verifyInlineBudgets({ ...input, verify: { contextFile: 'x'.repeat(32767) } }), { copy: 0, hard: 0 });
});
const PLAN_ROWS = [
  { name: 'Windows quoted JSON budget', size: 25, budget: 250, hostPlatform: 'windows', length: 281,
    input: Array.from({ length: 4 }, (_, i) => ({ id: `F${i}`, file: 'a'.repeat(30) })),
    expected: { slices: [['F0', 'F1', 'F2'], ['F3']], oversize: [], closeReasons: ['budget', null] } },
  { name: 'count closes a slice', input: BASIC, size: 2, budget: 50000 },
  { name: 'single finding exceeds budget', input: [BASIC[0]], size: 2, budget: 10,
    expected: { slices: [['A']], oversize: [], closeReasons: ['budget'] } },
  { name: 'native command limit withholds an indivisible finding', input: [BASIC[0]], size: 2, budget: 10, hardBudget: 10,
    expected: { slices: [], oversize: ['A'], closeReasons: [] } },
  { name: 'budget closes a slice', input: BASIC, size: 3, budget: 228,
    expected: { slices: [['A', 'B'], ['C']], oversize: [], closeReasons: ['budget', null] } },
  { name: 'oversize gets its own slice between neighbors', size: 2, budget: 5000,
    input: [{ id: 'A', origin: 'new', cross_file_refs: [] },
      { id: 'BIG', origin: 'new', cross_file_refs: [], file: 'x'.repeat(5000) },
      { id: 'C', origin: 'new', cross_file_refs: [] }],
    expected: { slices: [['A'], ['BIG'], ['C']], oversize: [], closeReasons: ['budget', 'budget', null] } },
  { name: 'hard-oversize flushes the preceding slice between neighbors', size: 3, budget: 5000,
    input: [{ id: 'A' }, { id: 'BIG', file: 'x'.repeat(50000) }, { id: 'C' }],
    expected: { slices: [['A'], ['C']], oversize: ['BIG'], closeReasons: ['oversize', null] } },
  { name: 'exact long-branch equality fits', size: 2, budget: 1311, branch: 'b'.repeat(1000), length: 1311,
    input: [
      { id: 'fit-a', file: 'x'.repeat(100), line_start: 1, origin: 'new', cross_file_refs: [] },
      { id: 'fit-b', file: 'y'.repeat(100), line_start: 2, origin: 'new', cross_file_refs: [] },
    ], expected: { slices: [['fit-a', 'fit-b']], oversize: [], closeReasons: [null] } },
];
for (const row of PLAN_ROWS) test(`slice planning: ${row.name}`, () => {
  const input = JSON.parse(JSON.stringify(row.input));
  const out = planVerifySlices(input, row.size, { copy: row.budget, hard: row.hardBudget ?? 50000 }, row.branch, row.hostPlatform);
  assert.deepEqual(input, row.input);
  assert.deepEqual({ slices: out.slices.map((slice) => slice.map((f) => f.id)),
    oversize: out.oversize.map((f) => f.id), closeReasons: out.closeReasons }, row.expected ?? PLAN_GOLDEN);
  for (const slice of out.slices) {
    assert.ok(encodeSliceInline({ findings: slice.map(projectVerifySliceFinding),
      base_branch: row.branch ?? 'main' }).length <= row.budget || slice.length === 1);
  }
  if (row.length) assert.equal(predictVerifySliceInlineLength(input, row.branch, row.hostPlatform), row.length);
});

for (const budgets of [undefined, { copy: 5000 }, { copy: 5000, hard: Infinity },
  { copy: -1, hard: 50000 }, { copy: 5000, hard: 4999 }]) test(`planner rejects incomplete or invalid budgets: ${JSON.stringify(budgets)}`, () => {
  assert.throws(() => planVerifySlices(BASIC, 2, budgets));
});

test('copy budget splits many small findings without losing or reordering them', () => {
  const findings = Array.from({ length: 100 }, (_, i) => ({ id: `F${i}`, file: 'x'.repeat(200) }));
  const plan = planVerifySlices(findings, 200, verifyInlineBudgets());
  assert.deepEqual(plan.slices.map((slice) => slice.length), [22, 22, 22, 22, 12]);
  assert.deepEqual(plan.slices.map((slice) => predictVerifySliceInlineLength(slice)), [4931, 4941, 4941, 4941, 2711]);
  assert.deepEqual(plan.slices.flat(), findings);
  assert.deepEqual(plan.oversize, []);
});
const COST_ROWS = [
  { branch: 'b', lengths: [1043, 970, 1072, 1073, 1122, 279] },
  { branch: 'main', lengths: [1046, 973, 1075, 1076, 1125, 282] },
  { branch: `feature/${'b'.repeat(99)}`, lengths: [1149, 1076, 1178, 1179, 1228, 385] },
  { branch: 'b'.repeat(1000), lengths: [2042, 1969, 2071, 2072, 2121, 1278] },
];
for (const row of COST_ROWS) test(`encoder costs: branch length ${row.branch.length}`, () => {
  const plan = planVerifySlices(randomFindings(275, 37), 7, verifyInlineBudgets(), row.branch);
  assert.deepEqual(plan.oversize, []);
  assert.deepEqual(plan.slices.map((slice) => predictVerifySliceInlineLength(slice, row.branch)), row.lengths);
  assert.deepEqual(plan.slices.map((slice) => encodeSliceInline({
    findings: slice.map(projectVerifySliceFinding), base_branch: row.branch,
  }).length), row.lengths);
});

const COUNT_GOLDEN = { summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 200 };
const COUNT_ROWS = [
  { name: 'ordinary estimate', limits: { ...COUNT_GOLDEN, validateBatch: 10 }, files: 500, findings: 3000, count: 1952 },
  { name: 'absent caps challenge all', limits: { summarizeBucketSize: 20, validateBatch: 25, verifySliceSize: 200 },
    files: 0, findings: 1000, count: 1277 },
  { name: 'explicit all cap', limits: { ...COUNT_GOLDEN, challengeCap: 1000 }, files: 0, findings: 1000, count: 1277 },
  { name: 'zero cap is real', limits: { ...COUNT_GOLDEN, challengeCap: 0 }, files: 0, findings: 1000, count: 277 },
  { name: 'all sizes absent', limits: {}, files: 20000, findings: 1000, count: 2023 },
  { name: 'explicit default sizes', limits: { verifySliceSize: 100, validateBatch: 100, challengeCap: 40 },
    files: 200, findings: 100, count: 73 },
  { name: 'numeric estimate ignores wire split', limits: COUNT_GOLDEN, files: 0, findings: 2, count: 21 },
  { name: 'array wire split includes its retry', limits: COUNT_GOLDEN, files: 0,
    findings: [{ id: 'A', file: 'x'.repeat(30000) }, { id: 'B', file: 'y'.repeat(30000) }], count: 20 },
  { name: 'small copy slices include every retry', limits: COUNT_GOLDEN, files: 0,
    findings: Array.from({ length: 100 }, (_, i) => ({ id: `F${i}`, file: 'x'.repeat(200) })), count: 101 },
  { name: 'hard-oversize finding consumes no verify dispatch', limits: COUNT_GOLDEN, files: 0,
    findings: [{ id: 'BIG', file: 'x'.repeat(50000) }], count: 14 },
];
for (const row of COUNT_ROWS) test(`agent count: ${row.name}`, () => {
  assert.equal(worstCaseAgentCount(row.limits, row.files, row.findings), row.count);
});
const COARSEN_ROWS = [
  { name: 'huge findings', limits: { summarizeBucketSize: 20, validateBatch: 2, challengeCap: 900, verifySliceSize: 5 },
    files: 500, findings: 5000, coarse: { summarizeBucketSize: 20, validateBatch: 128, challengeCap: 225, verifySliceSize: 80 }, count: 810 },
  { name: 'summarize term dominates', limits: { ...COUNT_GOLDEN, validateBatch: 10 }, files: 20000, findings: 3000,
    coarse: { summarizeBucketSize: 80, validateBatch: 40, challengeCap: 40, verifySliceSize: 200 }, count: 827 },
  { name: 'absent cap becomes concrete', limits: { summarizeBucketSize: 20, validateBatch: 25, verifySliceSize: 200 },
    files: 0, findings: 5000, coarse: { summarizeBucketSize: 20, validateBatch: 100, verifySliceSize: 200, challengeCap: 312 }, count: 749 },
  { name: 'small review shape', limits: COUNT_GOLDEN, files: 23, findings: 40, coarse: { summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 200 }, count: 69 },
  { name: 'smallest verify slice converges', limits: { ...COUNT_GOLDEN, verifySliceSize: 1 }, files: 20, findings: 5000,
    coarse: { summarizeBucketSize: 20, validateBatch: 100, challengeCap: 40, verifySliceSize: 64 }, count: 748 },
  { name: 'all limits absent converge', limits: {}, files: 20000, findings: 5000,
    coarse: { summarizeBucketSize: 80, challengeCap: 625 }, count: 898 },
  { name: 'wire-bound term stops making progress',
    limits: { summarizeBucketSize: 20, validateBatch: 5000, challengeCap: 5, verifySliceSize: 1 }, files: 0,
    findings: Array.from({ length: 1000 }, (_, i) => ({ id: `F${i}`, file: 'x'.repeat(2200) })),
    coarse: { summarizeBucketSize: 20, validateBatch: 10000, challengeCap: 5, verifySliceSize: 4 }, count: 1519 },
  { name: 'oversize singleton calls cannot coarsen away',
    limits: { summarizeBucketSize: 20, validateBatch: 5000, challengeCap: 5, verifySliceSize: 1 }, files: 0,
    findings: Array.from({ length: 1000 }, (_, i) => ({ id: `F${i}`, file: 'x'.repeat(6000) })),
    coarse: { summarizeBucketSize: 20, validateBatch: 10000, challengeCap: 5, verifySliceSize: 2 }, count: 2019 },
];
for (const row of COARSEN_ROWS) test(`coarsening: ${row.name}`, () => {
  const before = JSON.stringify(row.limits);
  const coarse = coarsenLimits(row.limits, row.files, row.findings);
  assert.equal(JSON.stringify(row.limits), before);
  assert.deepEqual(coarse, row.coarse);
  assert.equal(worstCaseAgentCount(coarse, row.files, row.findings), row.count);
});
const BRANCH_ROWS = [
  { name: 'windows quoted copy budget', branch: 'main', input: { repoRoot: 'D:/repo' }, slices: 800, count: 1614,
    coarse: { summarizeBucketSize: 20, validateBatch: 10000, challengeCap: 0, verifySliceSize: 50 } },
  { name: 'windows command budget below copy target', branch: 'main',
    input: { repoRoot: 'D:/repo', verify: { contextFile: 'x'.repeat(27000) } }, slices: 800, count: 1614,
    coarse: { summarizeBucketSize: 20, validateBatch: 10000, challengeCap: 0, verifySliceSize: 50 } },
  { name: 'main', branch: 'main', slices: 400, count: 1214,
    coarse: { summarizeBucketSize: 20, validateBatch: 10000, challengeCap: 0, verifySliceSize: 50 } },
  { name: 'long branch binds wire budget', branch: `${'b'.repeat(249)}/${'b'.repeat(249)}/${'b'.repeat(249)}/${'b'.repeat(250)}`,
    slices: 800, count: 1614, coarse: { summarizeBucketSize: 20, validateBatch: 10000, challengeCap: 0, verifySliceSize: 50 } },
];
for (const row of BRANCH_ROWS) test(`array guard branch: ${row.name}`, () => {
  const findings = Array.from({ length: 800 }, (_, i) => ({ id: `F${i}`, file: (i % 2 ? 'y' : 'x').repeat(2450) }));
  const limits = { summarizeBucketSize: 20, validateBatch: 5000, challengeCap: 0, verifySliceSize: 25 };
  assert.equal(planVerifySlices(findings, 25, verifyInlineBudgets(row.input), row.branch,
    row.input ? 'windows' : 'posix').slices.length, row.slices);
  assert.equal(worstCaseAgentCount(limits, 0, findings, row.branch, row.input), row.count);
  const coarse = coarsenLimits(limits, 0, findings, row.branch, row.input);
  assert.deepEqual(coarse, row.coarse);
  assert.equal(worstCaseAgentCount(coarse, 0, findings, row.branch, row.input), row.count);
});

test('capacity reserves only dispatched stages and bounded retries', () => {
  const limits = { summarizeBucketSize: 20, verifySliceSize: 10, validateBatch: 2, challengeCap: 5 };
  assert.equal(worstCaseAgentCount(limits, 0, 0), 11);
  assert.equal(worstCaseAgentCount(limits, 0, 5), 30);
  assert.equal(worstCaseAgentCount({ ...limits, challengeCap: 0 }, 0, 5), 25);
  assert.deepEqual(coarsenLimits({ validateBatch: 1, challengeCap: 400, verifySliceSize: 1 }, 0, 300),
    { validateBatch: 8, challengeCap: 400, verifySliceSize: 4 });
  const encoded = Array.from({ length: 500 }, (_, i) => ({ id: String(i), file: 'x'.repeat(2600) }));
  const coarse = coarsenLimits({ ...limits, challengeCap: 0 }, 0, encoded);
  assert.equal(coarse.challengeCap, 0);
  assert.equal(worstCaseAgentCount(coarse, 0, encoded), 1014);
  assert.equal(coarsenLimits({ ...limits, challengeCap: 1 }, 0, encoded).challengeCap, 1);
  const large = coarsenLimits({ ...limits, challengeCap: 10000 }, 20000, 10000);
  assert.ok(worstCaseAgentCount(large, 20000, 10000) < 900);
  assert.deepEqual(coarsenLimits(large, 20000, 10000), large);
});
const EMPTY_BRANCH_ROWS = [undefined, null, '', 'main'];
for (const [index, branch] of EMPTY_BRANCH_ROWS.entries()) test(`empty branch normalization: ${index}`, () => {
  const findings = Array.from({ length: 4 }, (_, i) => ({ id: `F${i}`, file: 'x'.repeat(100) }));
  assert.equal(effectiveVerifyBaseBranch(branch), 'main');
  assert.deepEqual(planVerifySlices(findings, 25, { ...verifyInlineBudgets(), copy: 352 }, branch).slices.map((slice) => slice.map((f) => f.id)),
    [['F0', 'F1'], ['F2', 'F3']]);
  assert.equal(worstCaseAgentCount({ validateBatch: 5000, challengeCap: 0, verifySliceSize: 25 }, 0, findings, branch), 19);
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
  assert.equal(worstCaseAgentCount({}, 47, 0), 14);
  assert.deepEqual(ctx.calls.map((call) => call.label), [
    'summarize-bucket-0', 'summarize-bucket-1', 'summarize-bucket-2', 'summarize-merge',
  ]);
  assert.deepEqual(out, { summary: 's', gaps: [] });
  assert.deepEqual(ctx.violations, []);
  for (const call of ctx.calls) {
    assert.ok(call.prompt.includes('1211 changed lines'));
    assert.deepEqual(call.schema, { type: 'object', properties: { summary: { type: 'string' } }, required: ['summary'] });
  }
});


test('anchor and map flags obey encoded command budget', () => {
  const anchor = Array.from({ length: 4 }, () => '\u{1F600}'.repeat(90)).join('\n');
  const findings = Array.from({ length: 6 }, (_, n) => ({ id: `f${n}`, file: 'source', anchor }));
  const inp = { repoRoot: 'D:/repo', nonce: 'n', headShaShort: 'abcd', verify: {
    targetKind: 'local', contextFile: 'D:/out/context.md', contextMap: 'D:/out/map.json', diffPath: 'D:/out/raw.patch',
  } };
  const one = predictVerifySliceInlineLength(findings.slice(0, 1), 'main', 'windows');
  const plan = planVerifySlices(findings, 6, { ...verifyInlineBudgets(inp), copy: one + 2 }, 'main', 'windows');
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
  }
  assert.equal(verifyInlineBudgets(inp).copy, 5000);
  assert.ok(verifyInlineBudgets(inp).hard < verifyInlineBudgets({ ...inp, verify: {} }).hard);
});
