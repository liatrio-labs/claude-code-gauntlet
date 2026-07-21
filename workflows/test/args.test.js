import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ARGS_VERSION, normalizeArgs, validateArgs } from '../src/args.js';

const good = {
  argsVersion: 1, mode: 'interactive', repoRoot: '/r', outputDir: '/r/.deep-review',
  headShaShort: 'abc123', nonce: 'n-1', generatedAt: '2026-07-18T00:00:00Z',
  diffPath: '/r/.deep-review/d.patch', changedFilesPath: '/r/.deep-review/f.json',
  reviewConfigPath: null, agentFlags: {},
  policy: { tier: 'optimized', frontier: false, frontierModelId: null, subagentModel: null },
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
test('validateArgs rejects frontier:true without a frontierModelId', () => {
  const r = validateArgs({ ...good, policy: { tier: 'optimized', frontier: true, frontierModelId: null, subagentModel: null } });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /frontierModelId/);
});
test('ARGS_VERSION is 1', () => { assert.equal(ARGS_VERSION, 1); });
test('validateArgs rejects an unrecognized mode', () => {
  const r = validateArgs({ ...good, mode: 'bogus' });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /invalid mode: bogus/);
});
test('validateArgs accepts frontier:true with a valid frontierModelId', () => {
  const r = validateArgs({ ...good, policy: { tier: 'optimized', frontier: true, frontierModelId: 'claude-fable-5', subagentModel: null } });
  assert.deepEqual(r, { ok: true, errors: [] });
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
