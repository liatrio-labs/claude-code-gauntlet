import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ARGS_VERSION, normalizeArgs, validateArgs } from '../src/args.js';

const good = {
  argsVersion: 1, mode: 'interactive', repoRoot: '/r', outputDir: '/r/.code-gauntlet',
  headShaShort: 'abc123', nonce: 'n-1', generatedAt: '2026-07-18T00:00:00Z',
  diffPath: '/r/.code-gauntlet/d.patch', changedFilesPath: '/r/.code-gauntlet/f.json',
  changedFiles: ['a.js'], changedLines: 1,
  reviewConfigPath: null, agentFlags: {},
  policy: { tier: 'optimized', subagentModel: null },
  limits: { summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 200 },
};

test('normalizeArgs parses a JSON string (session tool-call form)', () => {
  assert.deepEqual(normalizeArgs(JSON.stringify(good)), good);
});
test('normalizeArgs passes an object through (workflow-nesting form)', () => {
  assert.deepEqual(normalizeArgs(good), good);
});
test('validateArgs accepts a well-formed waist', () => {
  assert.deepEqual(validateArgs(good), { ok: true, errors: [] });
});
test('validateArgs rejects an unknown argsVersion loudly', () => {
  const r = validateArgs({ ...good, argsVersion: 2 });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /argsVersion/);
});
test('validateArgs reports every missing required field', () => {
  const r = validateArgs({ argsVersion: 1 });
  assert.equal(r.ok, false);
  assert.ok(r.errors.length >= 5);
});
test('ARGS_VERSION is 1', () => { assert.equal(ARGS_VERSION, 1); });
test('validateArgs rejects an unrecognized mode', () => {
  const r = validateArgs({ ...good, mode: 'bogus' });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /invalid mode: bogus/);
});
test('normalizeArgs(undefined) returns undefined without throwing', () => {
  assert.equal(normalizeArgs(undefined), undefined);
});
test('validateArgs rejects a nonce with characters outside [A-Za-z0-9._-]', () => {
  // The nonce is interpolated into the verify executor command argv (per slice as
  // `${nonce}.${i}`); anything with whitespace or shell metacharacters could split
  // argv or break AST-safe emission. Reject it at the waist.
  const r = validateArgs({ ...good, nonce: 'n 1; rm -rf /' });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /nonce/);
});
test('validateArgs accepts a dotted/hyphenated/underscored nonce (per-slice charset)', () => {
  // `.` and `-` must be allowed: the verify stage derives per-slice nonces `n-1.0`.
  assert.deepEqual(validateArgs({ ...good, nonce: 'n-1.0_ab' }), { ok: true, errors: [] });
});
test('validateArgs treats the delivery selector as optional (absent is fine)', () => {
  // `good` carries no `delivery` field — the workflow defaults the tier to 'all'.
  assert.deepEqual(validateArgs(good), { ok: true, errors: [] });
});
test('validateArgs accepts delivery.tier "all" and "main_only"', () => {
  assert.deepEqual(validateArgs({ ...good, delivery: { tier: 'all' } }), { ok: true, errors: [] });
  assert.deepEqual(validateArgs({ ...good, delivery: { tier: 'main_only' } }), { ok: true, errors: [] });
});
test('validateArgs accepts an empty delivery object (tier defaults to all downstream)', () => {
  assert.deepEqual(validateArgs({ ...good, delivery: {} }), { ok: true, errors: [] });
});
test('validateArgs rejects an unknown delivery.tier', () => {
  const r = validateArgs({ ...good, delivery: { tier: 'suggestions_only' } });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /invalid delivery\.tier: suggestions_only/);
});
test('validateArgs rejects a non-object delivery field', () => {
  const r = validateArgs({ ...good, delivery: 'all' });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /delivery must be an object/);
});
test('validateArgs requires the consumed by-value fields changedFiles + changedLines', () => {
  // REQUIRED mirrors consumption: summarize bucketing and the agent-count guard read
  // these by value; a waist without them dispatches on garbage instead of failing loud.
  const a = { ...good }; delete a.changedFiles; delete a.changedLines;
  const r = validateArgs(a);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('changedFiles')));
  assert.ok(r.errors.some((e) => e.includes('changedLines')));
});
test('validateArgs accepts an args waist without changedFilesPath (optional provenance)', () => {
  const a = { ...good, changedFiles: ['a.js'], changedLines: 3 }; delete a.changedFilesPath;
  assert.deepEqual(validateArgs(a), { ok: true, errors: [] });
});
test('validateArgs type-checks changedFiles (array) and changedLines (number)', () => {
  const a = { ...good, changedFiles: 'a.js,b.js', changedLines: '3' };
  const r = validateArgs(a);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('changedFiles')));
  assert.ok(r.errors.some((e) => e.includes('changedLines')));
});
