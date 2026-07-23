import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ARGS_VERSION, normalizeArgs, validateArgs, parseEntryArgs } from '../src/args.js';

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
// delivery.prIdentity (live-run L3): optional PR identity for the post_review-ready wrapper.
test('validateArgs accepts a well-formed delivery.prIdentity and its absence (local-diff reviews)', () => {
  const id = { owner: 'o', repo: 'r', pr_number: 310, sha_full: 'deadbeefcafe' };
  assert.deepEqual(validateArgs({ ...good, delivery: { tier: 'all', prIdentity: id } }), { ok: true, errors: [] });
  assert.deepEqual(validateArgs({ ...good, delivery: { tier: 'all' } }), { ok: true, errors: [] });
});
test('validateArgs rejects a malformed delivery.prIdentity (shape-checked when present)', () => {
  const r = validateArgs({ ...good, delivery: { prIdentity: { owner: 'o', repo: 'r', pr_number: '310', sha_full: '' } } });
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('pr_number')));
  assert.ok(r.errors.some((e) => e.includes('sha_full')));
  const r2 = validateArgs({ ...good, delivery: { prIdentity: 'org/repo#310' } });
  assert.equal(r2.ok, false);
  assert.match(r2.errors.join(' '), /prIdentity must be an object/);
});
// Entry-args guard (live-run L1): a raw-string invocation ("PR 310") must throw an
// actionable redirect to the skill, not a native JSON.parse stack.
test('parseEntryArgs: a non-JSON raw string throws with a skill redirect (no native parse stack)', () => {
  assert.throws(() => parseEntryArgs('PR 310'), /code-gauntlet skill/);
  assert.throws(() => parseEntryArgs('PR 310'), /PR 310/); // echoes the offending input
});
test('parseEntryArgs: passes through the two legitimate forms unchanged', () => {
  assert.deepEqual(parseEntryArgs(JSON.stringify(good)), good); // session tool-call form
  assert.deepEqual(parseEntryArgs(good), good);                  // workflow-nesting form
  assert.equal(parseEntryArgs(undefined), undefined);            // absent args -> graceful validateArgs envelope downstream
});

// reviewConfig waist validation (live-run L2): the skill session assembled ignore entries
// as {pattern, reason} objects; escapeRegExp assumes strings and crashed at Filter AFTER
// five paid stages. The waist rejects the malformed shape up front.
test('validateArgs rejects reviewConfig.ignore entries that are not strings', () => {
  const a = { ...good, reviewConfig: { ignore: [{ pattern: 'x', reason: 'y' }] } };
  const r = validateArgs(a);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('reviewConfig.ignore')));
});
test('validateArgs accepts reviewConfig.ignore as an array of flat strings (and absent reviewConfig)', () => {
  assert.deepEqual(
    validateArgs({ ...good, reviewConfig: { ignore: ['test_coverage:"*.generated.cs"'] } }),
    { ok: true, errors: [] },
  );
  const a = { ...good }; delete a.reviewConfig;
  assert.deepEqual(validateArgs(a), { ok: true, errors: [] });
});
test('validateArgs rejects a non-object reviewConfig and a non-array ignore', () => {
  const r = validateArgs({ ...good, reviewConfig: 'ignore stuff' });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /reviewConfig must be an object/);
  const r2 = validateArgs({ ...good, reviewConfig: { ignore: 'pattern' } });
  assert.equal(r2.ok, false);
  assert.ok(r2.errors.some((e) => e.includes('reviewConfig.ignore')));
});

// exclusionPatterns waist validation: consumed identically to reviewConfig.ignore (both feed
// escapeRegExp in the Filter stage's applyFilterPipeline), so it gets the same shape guard.
test('validateArgs rejects a non-string exclusionPatterns entry and a non-array exclusionPatterns', () => {
  const r = validateArgs({ ...good, exclusionPatterns: [{ pattern: 'x' }] });
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('exclusionPatterns')));
  const r2 = validateArgs({ ...good, exclusionPatterns: 'foo' });
  assert.equal(r2.ok, false);
  assert.match(r2.errors.join(' '), /exclusionPatterns must be an array/);
});
test('validateArgs accepts exclusionPatterns as an array of flat strings (and absent exclusionPatterns)', () => {
  assert.deepEqual(
    validateArgs({ ...good, exclusionPatterns: ['literal one', 'literal two'] }),
    { ok: true, errors: [] },
  );
  assert.deepEqual(validateArgs(good), { ok: true, errors: [] });
});

// agentFlags scope-gating map (item 7). Empty ({}) = full scope; { deep: false } = light.
test('validateArgs accepts the light-scope agentFlags map { deep: false }', () => {
  assert.deepEqual(validateArgs({ ...good, agentFlags: { deep: false } }), { ok: true, errors: [] });
});
test('validateArgs rejects a non-object agentFlags map', () => {
  const r = validateArgs({ ...good, agentFlags: 'deep' });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /agentFlags must be an object/);
  const r2 = validateArgs({ ...good, agentFlags: ['deep'] });
  assert.equal(r2.ok, false);
  assert.match(r2.errors.join(' '), /agentFlags must be an object/);
});
test('validateArgs rejects a non-boolean agentFlags value (only literal false gates)', () => {
  // A truthy-string like "false" would slip past agentActive's strict `!== false` and read
  // as ON, silently ignoring an operator's intent to disable — the waist rejects it.
  const r = validateArgs({ ...good, agentFlags: { deep: 'false' } });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /invalid agentFlags\.deep: must be a boolean/);
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
