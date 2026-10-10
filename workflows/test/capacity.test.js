import { test } from 'node:test';
import assert from 'node:assert/strict';
import { planVerifySlices, predictVerifySliceInlineLength } from '../src/capacity.js';
import { encodeSliceInline, projectVerifySliceFinding, VERIFY_INLINE_CHAR_BUDGET } from '../src/verifyWire.js';

const RANDOM_TEXT_CHARS = ['a', 'B', ' ', '0', '/', "'", '\\', '%', '\n', '\u2014', '\u{1F600}'];

function randomFindings(seed, count) {
  let state = seed >>> 0;
  const next = (limit) => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state % limit;
  };
  const text = (minimum, maximum) => {
    const length = minimum + next(maximum - minimum + 1);
    return Array.from({ length }, () => RANDOM_TEXT_CHARS[next(RANDOM_TEXT_CHARS.length)]).join('');
  };
  return Array.from({ length: count }, (_, i) => ({
    id: `F${i}-${text(1, 8)}`,
    file: `${text(2, 10)}.js`,
    line_start: 1 + next(500),
    line_end: 1 + next(500),
    description: text(20, 100),
    evidence: text(5, 50),
    severity: ['low', 'medium', 'high'][next(3)],
    confidence: next(101),
    cross_file_refs: Array.from({ length: next(3) }, () => `${text(2, 8)}:${1 + next(90)}`),
    origin: ['new', 'modified', 'unknown'][next(3)],
  }));
}

test('planner is pure, count-bounded, budget-bounded, and isolates oversize findings', () => {
  const findings = [
    { id: 'A', file: 'a.js', line_start: 1, origin: 'new', cross_file_refs: [] },
    { id: 'B', file: 'b.js', line_start: 2, origin: 'new', cross_file_refs: [] },
    { id: 'C', file: 'c.js', line_start: 3, origin: 'new', cross_file_refs: [], description: 'z'.repeat(100) },
  ];
  const copy = JSON.parse(JSON.stringify(findings));
  const plan = planVerifySlices(findings, 2, 100000);
  assert.deepEqual(findings, copy);
  assert.deepEqual(plan.oversize, []);
  assert.deepEqual(plan.slices.map((slice) => slice.map((finding) => finding.id)), [['A', 'B'], ['C']]);
  const oversized = planVerifySlices([findings[0]], 2, 10);
  assert.deepEqual(oversized.slices, []);
  assert.deepEqual(oversized.oversize.map((finding) => finding.id), ['A']);
  for (const slice of plan.slices) {
    assert.ok(encodeSliceInline({ findings: slice.map(projectVerifySliceFinding), base_branch: 'main' }).length <= 100000);
  }
});

test('planner predicts exact encoded lengths for random findings across branch lengths', () => {
  const findings = randomFindings(275, 37);
  const branches = ['b', 'main', `feature/${'b'.repeat(99)}`, 'b'.repeat(1000)];
  for (const baseBranch of branches) {
    const plan = planVerifySlices(findings, 7, 5000, baseBranch);
    assert.deepEqual(plan.oversize, [], `unexpected oversize finding for branch length ${baseBranch.length}`);
    for (const [i, slice] of plan.slices.entries()) {
      const content = { findings: slice.map(projectVerifySliceFinding), base_branch: baseBranch };
      const actualLength = encodeSliceInline(content).length;
      assert.equal(
        actualLength,
        predictVerifySliceInlineLength(slice, baseBranch),
        `slice ${i} at branch length ${baseBranch.length}`,
      );
      assert.ok(actualLength <= 5000);
    }
  }
});

test('planner accepts a slice whose exact encoded length equals the budget', () => {
  const baseBranch = 'b'.repeat(1000);
  const findings = [
    { id: 'fit-a', file: 'a.js', line_start: 1, origin: 'new', cross_file_refs: [], description: 'x'.repeat(100) },
    { id: 'fit-b', file: 'b.js', line_start: 2, origin: 'new', cross_file_refs: [], description: 'y'.repeat(100) },
  ];
  const content = { findings: findings.map(projectVerifySliceFinding), base_branch: baseBranch };
  const budget = encodeSliceInline(content).length;
  assert.equal(predictVerifySliceInlineLength(findings, baseBranch), budget);
  const plan = planVerifySlices(findings, 2, budget, baseBranch);
  assert.deepEqual(plan.slices, [findings]);
  assert.deepEqual(plan.oversize, []);
});

test('planner flushes a preceding slice before isolating an oversize finding', () => {
  const findings = [
    { id: 'A', origin: 'new', cross_file_refs: [] },
    { id: 'BIG', origin: 'new', cross_file_refs: [], description: 'x'.repeat(50000) },
    { id: 'C', origin: 'new', cross_file_refs: [] },
  ];
  const plan = planVerifySlices(findings, 2, VERIFY_INLINE_CHAR_BUDGET);
  assert.deepEqual(plan.slices.map((slice) => slice.map((finding) => finding.id)), [['A'], ['C']]);
  assert.deepEqual(plan.oversize.map((finding) => finding.id), ['BIG']);
  assert.deepEqual(plan.closeReasons, ['oversize', null]);
});
