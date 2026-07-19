// apply_challenges.test.js — the deepClone helper (structuredClone replacement) and the
// no-input-mutation guarantee it backs. The workflow runtime sandbox has no structuredClone
// (a node/browser global); deepClone (JSON round-trip) is the JSON-safe stand-in, and
// applyChallenges must never alias/mutate the caller's findings when it applies a score.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { deepClone, applyChallenges } from '../src/applyChallenges.js';

test('deepClone returns a structurally-independent copy (no shared references)', () => {
  const src = { a: 1, nested: { b: [1, 2, 3] }, list: [{ x: 1 }] };
  const copy = deepClone(src);
  assert.deepEqual(copy, src);
  assert.notEqual(copy, src);
  assert.notEqual(copy.nested, src.nested);
  assert.notEqual(copy.nested.b, src.nested.b);
  assert.notEqual(copy.list[0], src.list[0]);
  copy.nested.b.push(4);
  copy.list[0].x = 99;
  assert.deepEqual(src.nested.b, [1, 2, 3], 'mutating the copy never touches the source');
  assert.equal(src.list[0].x, 1);
});

test('deepClone does NOT depend on structuredClone (works with the global removed)', () => {
  const saved = globalThis.structuredClone;
  try {
    delete globalThis.structuredClone; // simulate the workflow sandbox
    const copy = deepClone({ id: 'F1', line_start: 10 });
    assert.deepEqual(copy, { id: 'F1', line_start: 10 });
  } finally {
    globalThis.structuredClone = saved;
  }
});

test('applyChallenges never mutates the caller input, even with structuredClone absent', () => {
  const saved = globalThis.structuredClone;
  try {
    delete globalThis.structuredClone; // the sandbox has no structuredClone
    const findings = [{ id: 'F1', severity: 'high', dimension: 'bug', confidence: 90, description: 'x' }];
    const before = JSON.parse(JSON.stringify(findings));
    // score 80 -> survive; the survivor is deep-cloned before challenge_score is stamped.
    const out = applyChallenges(findings, [{ id: 'F1', score: 80 }]);
    assert.deepEqual(findings, before, 'input findings unchanged (deep-cloned before mutation)');
    assert.equal(out.findings[0].challenge_score, 80, 'the returned (cloned) finding carries the score');
    assert.ok(!('challenge_score' in findings[0]), 'the caller object never gained the score');
  } finally {
    globalThis.structuredClone = saved;
  }
});
