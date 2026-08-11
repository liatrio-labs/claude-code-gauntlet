import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ARGS_VERSION, normalizeArgs, validateArgs, parseEntryArgs,
  stripNullOptionalsReport, normalizeArgsReport, nullToleranceGap,
} from '../src/args.js';

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
// Bedrock live failure (2026-08-11 transcript): a typo'd provider would silently flip the
// registry between the full-ID-pin arm and the bare-alias arm, so unknown spellings fail
// loud at the waist. null and absent both mean 'firstParty' (older waists omit the field).
test('validateArgs accepts every known policy.provider and tolerates null/absent', () => {
  for (const provider of ['firstParty', 'bedrock', 'vertex', 'foundry', 'gateway']) {
    assert.deepEqual(validateArgs({ ...good, policy: { ...good.policy, provider } }), { ok: true, errors: [] });
  }
  assert.deepEqual(validateArgs({ ...good, policy: { ...good.policy, provider: null } }), { ok: true, errors: [] });
  assert.deepEqual(validateArgs(good), { ok: true, errors: [] }); // absent
});
test('validateArgs rejects an unknown policy.provider', () => {
  const r = validateArgs({ ...good, policy: { ...good.policy, provider: 'aws' } });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /invalid policy\.provider: aws/);
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
// Entry-args guard (live-run L1; superseded by issue #27's classified refusal design —
// see entry_guard.test.js): a raw-string invocation ("PR 310") must throw an actionable
// redirect to the skill, not a native JSON.parse stack. "PR 310" classifies as a
// pr_shorthand review target (workflows/src/args.js classifyReviewTarget), so the thrown
// message also carries the exact copy-paste Skill(...) line naming it back — pinned here,
// not just loosely matched, because that line is the deliverable of the issue.
test('parseEntryArgs: a non-JSON raw string throws with a skill redirect (no native parse stack)', () => {
  assert.throws(() => parseEntryArgs('PR 310'), /code-gauntlet skill/);
  assert.throws(() => parseEntryArgs('PR 310'), /PR 310/); // echoes the offending input
  assert.throws(() => parseEntryArgs('PR 310'), /Skill\("code-gauntlet:code-gauntlet", args="PR 310"\)/);
});
test('parseEntryArgs: passes through the two legitimate forms unchanged', () => {
  assert.deepEqual(parseEntryArgs(JSON.stringify(good)), good); // session tool-call form
  assert.deepEqual(parseEntryArgs(good), good);                  // workflow-nesting form
});
// Issue #27 requirement 5: parseEntryArgs(undefined) used to return `undefined` SILENTLY,
// leaving the caller least likely to parse a return value (the naked Workflow-tool call) to
// fall through to a generic downstream validateArgs rejection with no recovery line at all.
// Absent args is now refused at the entry, exactly like every other unusable shape — this
// inverts the old assertion `parseEntryArgs(undefined) === undefined` on purpose.
test('parseEntryArgs(undefined) now THROWS instead of silently returning undefined (requirement 5)', () => {
  assert.throws(() => parseEntryArgs(undefined), /code-gauntlet skill/);
  assert.throws(() => parseEntryArgs(undefined), /\(got undefined\)/);
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

// D4 — args-waist null tolerance (issue #38 A1). A dispatch was rejected solely because
// reviewConfig arrived as a stamped `null` rather than absent. normalizeArgs now strips a
// literal null for a narrow allowlist of optional top-level fields so a stamped null costs
// nothing at the waist.
test('stripNullOptionalsReport deletes a null reviewConfig/exclusionPatterns/delivery/checkpoints', () => {
  const a = { ...good, reviewConfig: null, exclusionPatterns: null, delivery: null, checkpoints: null };
  const stripped = stripNullOptionalsReport(a).args;
  assert.equal('reviewConfig' in stripped, false);
  assert.equal('exclusionPatterns' in stripped, false);
  assert.equal('delivery' in stripped, false);
  assert.equal('checkpoints' in stripped, false);
});
test('stripNullOptionalsReport drops delivery.prIdentity: null and delivery.tier: null inside a present delivery object', () => {
  const a = { ...good, delivery: { tier: null, prIdentity: null } };
  const stripped = stripNullOptionalsReport(a).args;
  assert.deepEqual(stripped.delivery, {});
});
test('stripNullOptionalsReport does NOT strip limits.deliveryCap: null (uncapped is load-bearing)', () => {
  const a = { ...good, limits: { ...good.limits, deliveryCap: null } };
  const stripped = stripNullOptionalsReport(a).args;
  assert.equal(stripped.limits.deliveryCap, null);
});
test('stripNullOptionalsReport does NOT strip policy.subagentModel: null (no-override is load-bearing)', () => {
  const stripped = stripNullOptionalsReport(good).args; // good already carries policy.subagentModel: null
  assert.equal(stripped.policy.subagentModel, null);
});
test('stripNullOptionalsReport leaves a non-null, non-allowlisted-field waist untouched', () => {
  assert.deepEqual(stripNullOptionalsReport(good).args, good);
});
test('stripNullOptionalsReport passes through non-object input (undefined, string) without throwing', () => {
  assert.equal(stripNullOptionalsReport(undefined).args, undefined);
  assert.equal(stripNullOptionalsReport(null).args, null);
});

test('normalizeArgs strips stamped nulls on the object-passthrough form so validateArgs accepts them', () => {
  const a = { ...good, reviewConfig: null, exclusionPatterns: null, delivery: null, checkpoints: null };
  const normalized = normalizeArgs(a);
  assert.equal('reviewConfig' in normalized, false);
  assert.equal('exclusionPatterns' in normalized, false);
  assert.equal('delivery' in normalized, false);
  assert.equal('checkpoints' in normalized, false);
  assert.deepEqual(validateArgs(normalized), { ok: true, errors: [] });
});
test('normalizeArgs strips stamped nulls on the JSON-string form too', () => {
  const raw = JSON.stringify({ ...good, reviewConfig: null, delivery: { tier: null, prIdentity: null } });
  const normalized = normalizeArgs(raw);
  assert.equal('reviewConfig' in normalized, false);
  assert.deepEqual(normalized.delivery, {});
  assert.deepEqual(validateArgs(normalized), { ok: true, errors: [] });
});
test('normalizeArgs leaves a malformed non-null reviewConfig alone for validateArgs to reject loudly', () => {
  const a = { ...good, reviewConfig: { ignore: [{ pattern: 'x', reason: 'y' }] } };
  const r = validateArgs(normalizeArgs(a));
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('reviewConfig.ignore')));
});
test('normalizeArgs still round-trips a fully well-formed waist unchanged (no over-stripping)', () => {
  assert.deepEqual(normalizeArgs(good), good);
  assert.deepEqual(normalizeArgs(JSON.stringify(good)), good);
});

// --- L3-1: the persist waist is null-tolerated AND shape-checked -------------
// The persist field (issue #38 D3.4) was added to the waist but left OFF the very
// null-tolerance allowlist the same diff introduced for its siblings, so a stamped
// `persist: null` hard-rejected the whole run — the exact 21.3s-round-trip cost the
// allowlist exists to remove.

test('validateArgs accepts a well-formed persist waist, an empty one, and its absence', () => {
  assert.deepEqual(
    validateArgs({ ...good, persist: { assembleScriptPath: '/plugin/scripts/assemble_artifacts.py' } }),
    { ok: true, errors: [] },
  );
  assert.deepEqual(validateArgs({ ...good, persist: {} }), { ok: true, errors: [] });
  assert.deepEqual(validateArgs(good), { ok: true, errors: [] }); // `good` carries no persist
});
test('validateArgs shape-checks a present persist (non-object, and a non-string/empty script path)', () => {
  // Same present-then-shape-checked treatment as `delivery`: malformed fails loud at the
  // waist, before any paid stage is dispatched.
  const r = validateArgs({ ...good, persist: '/plugin/scripts/assemble_artifacts.py' });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /persist must be an object/);
  const r2 = validateArgs({ ...good, persist: ['/p.py'] });
  assert.equal(r2.ok, false);
  assert.match(r2.errors.join(' '), /persist must be an object/);
  const r3 = validateArgs({ ...good, persist: { assembleScriptPath: 42 } });
  assert.equal(r3.ok, false);
  assert.match(r3.errors.join(' '), /persist\.assembleScriptPath/);
  const r4 = validateArgs({ ...good, persist: { assembleScriptPath: '' } });
  assert.equal(r4.ok, false);
  assert.match(r4.errors.join(' '), /persist\.assembleScriptPath/);
});
test('stripNullOptionalsReport deletes a null persist (same stand-in-for-absent treatment as its siblings)', () => {
  const stripped = stripNullOptionalsReport({ ...good, persist: null }).args;
  assert.equal('persist' in stripped, false);
});
test('normalizeArgs strips a stamped persist:null so validateArgs accepts the run instead of rejecting it', () => {
  const normalized = normalizeArgs({ ...good, persist: null });
  assert.equal('persist' in normalized, false);
  assert.deepEqual(validateArgs(normalized), { ok: true, errors: [] });
  // ...and via the JSON-string form too.
  assert.deepEqual(validateArgs(normalizeArgs(JSON.stringify({ ...good, persist: null }))), { ok: true, errors: [] });
});
test('a stamped persist:null still leaves a MALFORMED persist to fail loud (tolerance is null-only)', () => {
  const r = validateArgs(normalizeArgs({ ...good, persist: { assembleScriptPath: '' } }));
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /persist\.assembleScriptPath/);
});

// --- L5-2: null tolerance is DISCLOSED, not silent ---------------------------
// Tolerating a stamped null removes a fail-loud guard. A mis-stamped reviewConfig: null
// would otherwise review under the Filter stage's config-absent defaults (55/70) rather
// than the operator's REVIEW.md thresholds — a silent DELIVERED-findings change. The strip
// therefore reports what it dropped so runWith can surface an actionable gap.

test('stripNullOptionalsReport names every key it dropped', () => {
  const { args: stripped, dropped } = stripNullOptionalsReport({
    ...good, reviewConfig: null, exclusionPatterns: null, checkpoints: null, persist: null,
  });
  assert.deepEqual(dropped.slice().sort(), ['checkpoints', 'exclusionPatterns', 'persist', 'reviewConfig']);
  for (const k of dropped) assert.equal(k in stripped, false, `${k} was dropped from the waist`);
});
test('stripNullOptionalsReport names the delivery sub-field drops with their dotted paths', () => {
  const { dropped } = stripNullOptionalsReport({ ...good, delivery: { tier: null, prIdentity: null } });
  assert.deepEqual(dropped.slice().sort(), ['delivery.prIdentity', 'delivery.tier']);
});
test('stripNullOptionalsReport reports NOTHING for a clean waist (no false disclosure)', () => {
  assert.deepEqual(stripNullOptionalsReport(good).dropped, []);
  // The two load-bearing nulls are not drops and must never be reported as such.
  assert.deepEqual(
    stripNullOptionalsReport({ ...good, limits: { ...good.limits, deliveryCap: null } }).dropped,
    [],
  );
  assert.deepEqual(stripNullOptionalsReport(undefined).dropped, []);
  assert.deepEqual(stripNullOptionalsReport('not an object').dropped, []);
});
test('the strip never mutates the caller object — nested delivery included', () => {
  const caller = { ...good, reviewConfig: null, persist: null, delivery: { tier: null, prIdentity: null } };
  const beforeDelivery = caller.delivery;
  const { args: stripped } = stripNullOptionalsReport(caller);
  assert.equal(caller.reviewConfig, null, 'caller keeps its stamped null');
  assert.equal(caller.persist, null);
  assert.equal(caller.delivery, beforeDelivery, 'the caller delivery object is the same reference');
  assert.equal(caller.delivery.tier, null, 'the caller delivery sub-fields are untouched');
  assert.equal(caller.delivery.prIdentity, null);
  assert.notEqual(stripped.delivery, beforeDelivery, 'the returned delivery is a copy, not the caller object');
  assert.deepEqual(stripped.delivery, {});
});
test('nullToleranceGap names the field, says it was treated as absent, and says to omit it', () => {
  const g = nullToleranceGap('reviewConfig');
  assert.match(g, /^null_arg: /);
  assert.match(g, /reviewConfig/);
  assert.match(g, /ABSENT/);
  assert.match(g, /Omit/);
  // The reviewConfig wording must name the concrete consequence: config-absent thresholds.
  assert.match(g, /55/);
  assert.match(g, /70/);
  // An unlisted key still produces a well-formed, actionable line.
  const g2 = nullToleranceGap('somethingNew');
  assert.match(g2, /somethingNew/);
  assert.match(g2, /Omit/);
});
test('normalizeArgsReport reports drops on both the object and JSON-string forms', () => {
  const r1 = normalizeArgsReport({ ...good, reviewConfig: null });
  assert.deepEqual(r1.dropped, ['reviewConfig']);
  assert.equal('reviewConfig' in r1.args, false);
  const r2 = normalizeArgsReport(JSON.stringify({ ...good, exclusionPatterns: null, persist: null }));
  assert.deepEqual(r2.dropped.slice().sort(), ['exclusionPatterns', 'persist']);
  assert.deepEqual(normalizeArgsReport(good).dropped, []);
});
test('parseEntryArgs does NOT strip stamped nulls — runWith owns the strip so it can disclose it', () => {
  // If the entry stripped first, runWith would see an already-clean waist and the silent
  // config substitution would go unreported on exactly the live path that matters.
  // Issue #27 unchanged this: the entry now classifies/refuses non-object shapes and
  // absent args, but a well-formed OBJECT with internal stamped nulls is still a plain
  // object (accepted) and passes through untouched — the strip contract below still holds.
  const parsed = parseEntryArgs(JSON.stringify({ ...good, reviewConfig: null, persist: null }));
  assert.equal(parsed.reviewConfig, null);
  assert.equal(parsed.persist, null);
  const parsedObj = parseEntryArgs({ ...good, reviewConfig: null });
  assert.equal(parsedObj.reviewConfig, null);
});

// --- contextLines / contextChars: the shared-context size waist (issue #48) ---
//
// The workflow has no disk, so the only way it can know how much of the shared context
// file exists is for the skill to measure it and stamp it. contextReadPlan turns the
// measurement into the exact Read calls the dispatch prompts enumerate; a malformed
// measurement would yield a plan that misses the file's tail, which is precisely the
// silent under-read #48 exists to stop. So the waist shape-checks both, hard.
test('contextLines/contextChars are OPTIONAL — a pre-#48 waist stays valid', () => {
  // Bench and every caller predating this field stamp neither. They must keep running
  // (degrading to the count-free read-to-end wording), not be rejected.
  assert.equal(validateArgs(good).ok, true);
  assert.equal(validateArgs({ ...good, contextLines: 2028 }).ok, true);
  assert.equal(validateArgs({ ...good, contextLines: 2028, contextChars: 94784 }).ok, true);
  assert.equal(validateArgs({ ...good, contextLines: 1, contextChars: 1 }).ok, true);
});

test('contextLines/contextChars reject any value that would corrupt the read plan', () => {
  for (const bad of [0, -1, 1.5, NaN, Infinity, '2028', null, {}, [], Number.MAX_SAFE_INTEGER + 2]) {
    const r = validateArgs({ ...good, contextLines: bad });
    assert.equal(r.ok, false, `contextLines=${String(bad)} should be rejected`);
    assert.ok(r.errors.some((e) => e.includes('contextLines')), r.errors.join('; '));
  }
  for (const bad of [0, -1, 2.5, NaN, '95057', null, {}]) {
    const r = validateArgs({ ...good, contextLines: 2028, contextChars: bad });
    assert.equal(r.ok, false, `contextChars=${String(bad)} should be rejected`);
    assert.ok(r.errors.some((e) => e.includes('contextChars')), r.errors.join('; '));
  }
});

test('contextChars without contextLines is rejected — chars alone cannot size a line plan', () => {
  const r = validateArgs({ ...good, contextChars: 94784 });
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('contextChars requires contextLines')), r.errors.join('; '));
});

test('a stamped null contextLines is NOT silently tolerated — it is a measurement, not an optional object', () => {
  // NULLABLE_TOP_LEVEL is deliberately narrow (args.js). A null measurement must fail
  // loud rather than degrade quietly to the count-free wording: the skill measured
  // something and got null, which is a bug in the producer, not an omission.
  assert.deepEqual(stripNullOptionalsReport({ ...good, contextLines: null }).dropped, []);
  assert.equal(validateArgs({ ...good, contextLines: null }).ok, false);
});

test('contextLines/contextChars are bounded above — an absurd measurement fails loud at the waist', () => {
  // Found by the adversarial review of this change: the guard checked only
  // Number.isSafeInteger && > 0, so contextLines = Number.MAX_SAFE_INTEGER validated
  // cleanly and then OOM-killed the node process inside contextReadPlan, uncatchably,
  // before any dispatch. contextReadPlan carries its own chunk ceiling as the last line
  // of defence; this is the fail-loud one, where the bad value is still attributable to
  // the producer that stamped it.
  for (const [k, over] of [['contextLines', 5000001], ['contextChars', 500000001]]) {
    const base = k === 'contextChars' ? { ...good, contextLines: 2028 } : { ...good };
    const r = validateArgs({ ...base, [k]: over });
    assert.equal(r.ok, false, `${k}=${over} should be rejected`);
    assert.ok(r.errors.some((e) => e.includes(k) && e.includes('ceiling')), r.errors.join('; '));
  }
  assert.equal(validateArgs({ ...good, contextLines: 5000000 }).ok, true, 'the ceiling itself is accepted');
  assert.equal(validateArgs({ ...good, contextLines: Number.MAX_SAFE_INTEGER }).ok, false);
});

// --- Requirement 6 (issue #27): path-bearing waist fields are type/shape-checked ----------
// Per-field reachability:
//   - outputDir / headShaShort → shared-context path
//     (`${outputDir}/code-gauntlet-context-${headShaShort}.md`, built in runWith), which
//     reaches every discovery prompt
//   - headShaShort / diffPath → verify executor argv (--head-sha, --diff-file) — the same
//     argv-splitting hazard NONCE_RE already guards against
//   - repoRoot → provenance-only, unread by every stage; absolute-shape-checked for stamp
//     honesty (issue #81)
// A present-but-garbage value on a consumed path field would otherwise render a junk path
// into every paid dispatch instead of failing at the waist. Absence stays a REQUIRED-field
// error (tested elsewhere); these checks fire only when the field is PRESENT.
const PATH_FIELDS = ['repoRoot', 'outputDir', 'headShaShort', 'diffPath'];

test('validateArgs rejects a non-string value for each path-bearing field when present', () => {
  for (const field of PATH_FIELDS) {
    for (const bad of [42, {}, null]) {
      const r = validateArgs({ ...good, [field]: bad });
      assert.equal(r.ok, false, `${field}=${JSON.stringify(bad)} should be rejected`);
      assert.ok(r.errors.some((e) => e.includes(field)), `${field}: ${r.errors.join('; ')}`);
    }
  }
});

test('validateArgs rejects an empty or whitespace-only value for each path-bearing field', () => {
  for (const field of PATH_FIELDS) {
    for (const bad of ['', '   ']) {
      const r = validateArgs({ ...good, [field]: bad });
      assert.equal(r.ok, false, `${field}=${JSON.stringify(bad)} should be rejected`);
      assert.ok(r.errors.some((e) => e.includes(field)), `${field}: ${r.errors.join('; ')}`);
    }
  }
});

test('validateArgs rejects a path-bearing field carrying an embedded control character (\\n)', () => {
  // Write the class with unicode escapes at the implementation site, never a literal
  // control byte (spec 1f) — this test drives that guard with an actual embedded newline,
  // the one every path-bearing field is most likely to receive from a mis-templated stamp.
  for (const field of PATH_FIELDS) {
    const r = validateArgs({ ...good, [field]: 'a\nb' });
    assert.equal(r.ok, false, `${field} with an embedded newline should be rejected`);
    assert.ok(r.errors.some((e) => e.includes(field)), `${field}: ${r.errors.join('; ')}`);
  }
});

test('validateArgs rejects headShaShort that would split or inject in the verify argv', () => {
  // headShaShort reaches the shell-run string verifyCommand builds (stages.js), where it
  // is carried as a bare word. Apply the same NONCE_RE charset as the nonce — whitespace,
  // shell metacharacters, and anything outside [A-Za-z0-9._-] must fail at the waist. A
  // real short SHA never needs them (unlike a path, which verifyCommand quotes instead).
  for (const bad of ['abc 1234', 'abc;id', 'abc$(id)', 'abc`id`', "abc'x", 'abc|x']) {
    const r = validateArgs({ ...good, headShaShort: bad });
    assert.equal(r.ok, false, `headShaShort=${JSON.stringify(bad)} should be rejected`);
    assert.ok(r.errors.some((e) => e.includes('headShaShort')), `${bad}: ${r.errors.join('; ')}`);
  }
});

test('validateArgs still accepts the existing good waist untouched (the path-field guard is not overzealous)', () => {
  assert.deepEqual(validateArgs(good), { ok: true, errors: [] });
});

// Issue #86: outputDir must be absolute (POSIX /-prefix). Relative paths type-checked
// fine before this guard; the waist rejects them rather than resolving (no reliable cwd).
test('validateArgs rejects a relative outputDir', () => {
  const r = validateArgs({ ...good, outputDir: '.code-gauntlet' });
  assert.equal(r.ok, false);
  assert.ok(
    r.errors.some((e) => e.includes('outputDir') && e.includes('absolute')),
    r.errors.join('; '),
  );
});

test('validateArgs accepts an absolute outputDir (POSIX /-prefix)', () => {
  assert.deepEqual(
    validateArgs({ ...good, outputDir: '/r/.code-gauntlet' }),
    { ok: true, errors: [] },
  );
});

// Issue #81: repoRoot must be absolute (POSIX /-prefix). Provenance-only / unread, but the
// waist rejects a relative stamp rather than resolving (no reliable cwd; no FS probe).
test('validateArgs rejects a relative repoRoot', () => {
  for (const bad of ['.', 'repo']) {
    const r = validateArgs({ ...good, repoRoot: bad });
    assert.equal(r.ok, false, `repoRoot=${JSON.stringify(bad)} should be rejected`);
    assert.ok(
      r.errors.some((e) => e.includes('repoRoot') && e.includes('absolute')),
      r.errors.join('; '),
    );
  }
});

test('validateArgs accepts an absolute repoRoot (POSIX /-prefix)', () => {
  assert.deepEqual(
    validateArgs({ ...good, repoRoot: '/r' }),
    { ok: true, errors: [] },
  );
});
