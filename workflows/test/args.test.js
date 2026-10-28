import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeArgs, normalizeArgsReport, validateArgs, stripNullOptionalsReport, resolveRemovedTestsArgs } from '../src/args.js';
import { fnv1a32 } from '../src/wire.js';
import { nullToleranceGap, nullRespellGap, nullToleranceRejectedKeys, resolveReviewConfig } from '../src/args.js';
import { interactiveArgs, headlessArgs, priorReviewDetector, prIdentity, reviewMdArgs } from './helpers/argsFixtures.js';

for (const intentPath of [null, 3, '', '   ', '/repo/intent\n.md']) {
  test(`intent path rejects ${JSON.stringify(intentPath)}`, () => {
    const result = validateArgs(interactiveArgs({ intentPath }));
    assert.equal(result.ok, false);
    assert.ok(result.errors.some((error) => error.startsWith('intentPath ')));
  });
}

test('removed-test args treat every inventory path control character as unavailable', () => {
  const controls = [...Array.from({ length: 32 }, (_, i) => i),
    ...Array.from({ length: 33 }, (_, i) => i + 127), 0x2028, 0x2029];
  for (const code of controls) {
    const body = { status: 'available', items: [{ id: '0123456789abcdef',
      path: `tests/control${String.fromCodePoint(code)}path`, old_start: 1, old_end: 2, kind: 'deleted_file' }], omitted: 0 };
    const removedTests = { ...body, checksum: fnv1a32(JSON.stringify(body, null, 2)) };
    assert.deepEqual(resolveRemovedTestsArgs({ removedTests }), {
      status: 'unavailable', items: [], omitted: 0, reason: 'invalid inventory item',
    }, `U+${code.toString(16)}`);
  }
});

const REJECTION_ROWS = [
  ...[['python', 'python3;id', 'python3, python, py -3']].map(([key, value, allowed]) => ({
    name: `${key} allowlist ${value}`, input: (a) => { a[key] = value; }, expected: [`${key} must be one of ${allowed}`],
  })),
  ...[undefined, 1].map((checksum) => ({
    name: `review checksum ${JSON.stringify(checksum)}`,
    input: (a) => { Object.assign(a, reviewMdArgs([{ path: 'api/REVIEW.md', text: '' }]));
      a.configEcho.review_md.value = 'present';
      if (checksum === undefined) delete a.reviewMd[0].checksum;
      else a.reviewMd[0].checksum = checksum; },
    expected: ['reviewMd[0].checksum must be a string or null'],
  })),
  ...[undefined, null].map((checksum) => ({
    name: `review set checksum ${JSON.stringify(checksum)}`,
    input: (a) => { Object.assign(a, reviewMdArgs([]));
      if (checksum === undefined) delete a.reviewMdSetChecksum;
      else a.reviewMdSetChecksum = checksum; },
    expected: ['reviewMdSetChecksum must be a string when reviewMd is present'],
  })),
  { name: 'version', input: (a) => { a.argsVersion = 2; }, expected: [ "unsupported argsVersion 2 (expected 1)" ] },
  { name: 'mode', input: (a) => { a.mode = "bogus"; }, expected: [
    "invalid mode: bogus",
    "configEcho has unexpected key(s): model_tier, delivery_tier, pr_comment_cap, review_md",
  ] },
  { name: 'nonce', input: (a) => { a.nonce = "n 1"; }, expected: [
    "invalid nonce: must match /^[A-Za-z0-9._-]+$/ (AST-safe, non-splitting \u2014 interpolated into the verify " +
      "command argv per slice)",
  ] },
  { name: 'agent flags', input: (a) => { a.agentFlags = {}; }, expected: [
    "agentFlags is no longer accepted \u2014 the pipeline derives dimension flags itself from riskTable/scopeAnswer " +
      "(issue #24). Stamp riskTable (the Phase 2e per-file risk classification) and, only when the light/full gate " +
      "asked, scopeAnswer; do not stamp agentFlags.",
  ] },
  { name: 'policy provider',
    input: (a) => { a.policy.provider = "aws"; },
    expected: [ "invalid policy.provider: aws (expected one of firstParty, bedrock, vertex, foundry)" ] },
  { name: 'policy tier',
    input: (a) => { a.policy.tier = "standard"; }, expected: [ "invalid policy.tier: standard (expected optimized)" ] },
  { name: 'policy gateway',
    input: (a) => { a.policy.gateway = "true"; }, expected: [ "invalid policy.gateway: true (expected a boolean)" ] },
  ...[
    ['unknown alias', ['fable'], 'fable'],
    ['duplicate alias', ['sonnet', 'sonnet'], 'sonnet,sonnet'],
    ['non-array aliases', 'sonnet', 'sonnet'],
  ].map(([name, value, display]) => ({ name,
    input: (a) => { a.policy.mappedAliases = value; },
    expected: [`invalid policy.mappedAliases: ${display} (expected distinct values from sonnet, opus, haiku)`] })),
  { name: 'repoRoot control', input: (a) => { a.repoRoot = "/p\n"; }, expected: [ "repoRoot must not contain a control character" ] },
  { name: 'headShaShort shell metacharacter', input: (a) => { a.headShaShort = 'abc;id'; }, expected: [
    "headShaShort must match /^[A-Za-z0-9._-]+$/ (AST-safe, non-splitting \u2014 interpolated into the verify command " +
      "argv)",
  ] },
  { name: 'blank path', input: (a) => { a.diffPath = "   "; }, expected: [ "diffPath must be a non-empty string when present" ] },
  { name: 'relative output',
    input: (a) => { a.outputDir = "relative"; }, expected: [ "outputDir must be an absolute confined root without .. segments or backslashes" ] },
  { name: 'relative plugin root', input: (a) => {
    a.pluginRoot = 'plugin'; a.verify = { scriptPath: '/untrusted/verify_findings.py' };
  }, expected: ['pluginRoot must be an absolute confined root without .. segments or backslashes'] },
  { name: 'root traversal',
    input: (a) => { a.repoRoot = "/r/../other"; }, expected: [ "repoRoot must be an absolute path without .. segments or backslashes" ] },
  { name: 'plugin backtick', input: (a) => { a.pluginRoot = "/plugin`root"; }, expected: [ "pluginRoot must not contain a backtick" ] },
  { name: 'plugin traversal',
    input: (a) => { a.pluginRoot = "/plugin/../other"; }, expected: [ "pluginRoot must not contain a .. path segment" ] },
  { name: 'changed files type',
    input: (a) => { a.changedFiles = {}; }, expected: [ "changedFiles must be an array of repo-relative paths" ] },
  { name: 'risk container', input: (a) => { a.riskTable = {}; }, expected: [ "riskTable must be an array of {path, risk} objects" ] },
  { name: 'risk enum',
    input: (a) => { a.riskTable[0].risk = "bogus"; }, expected: [ "riskTable[0].risk must be one of low, medium, high (got \"bogus\")" ] },
  { name: 'risk extra',
    input: (a) => { a.riskTable[0].note = 1; }, expected: [ "riskTable[0] has unexpected key(s): note (only path and risk are allowed)" ] },
  { name: 'risk missing path',
    input: (a) => { a.changedFiles.push("b.js"); }, expected: [ "riskTable is missing risk classification for changed file(s): b.js" ] },
  { name: 'risk surplus path',
    input: (a) => { a.riskTable.push({ path: "ghost.js", risk: "low" }); },
    expected: [ "riskTable classifies file(s) not present in changedFiles: ghost.js" ] },
  { name: 'scope enum',
    input: (a) => { a.scopeAnswer = "partial"; }, expected: [ "invalid scopeAnswer: partial (expected one of light, full)" ] },
  { name: 'eligible missing answer', input: (a) => { a.riskTable[0].risk = "low"; }, expected: [
    "riskTable/changedLines are light-eligible but scopeAnswer is missing \u2014 the light/full gate must have asked, " +
      "and its answer must be stamped as scopeAnswer",
  ] },
  { name: 'contextLines ceiling',
    input: (a) => { a.contextLines = 5000001; },
    expected: [ "contextLines is 5000001, above the 5000000 ceiling \u2014 that is not a review context, it is a mis-measurement" ] },
  { name: 'chars alone',
    input: (a) => { a.contextChars = 94784; },
    expected: [ "contextChars requires contextLines \u2014 chars alone cannot size a line-offset read plan" ] },
  { name: 'scope layer extra',
    input: (a) => { a.reviewConfig = { scopes: [{ dir: "api", extra: true }] }; },
    expected: [ "reviewConfig.scopes[0] has unexpected key(s): extra" ] },
  { name: 'duplicate scopes',
    input: (a) => { a.reviewConfig = { scopes: [{ dir: "api" }, { dir: "api" }] }; },
    expected: [ "reviewConfig.scopes[1].dir duplicates another scope dir: api" ] },
  { name: 'exclusion shape',
    input: (a) => { a.exclusionPatterns = {}; }, expected: [ "exclusionPatterns must be an array of flat pattern strings" ] },
  { name: 'review md extra', input: (a) => {
    a.reviewMdSetChecksum = ""; a.reviewMd = [{ checksum: null, path: "api/REVIEW.md", text: "", extra: 1 }]; a.configEcho.review_md.value = "present";
  }, expected: [ "reviewMd[0] has unexpected key(s): extra (only path, text and checksum are allowed)" ] },
  { name: 'review md receipt mismatch', input: (a) => {
    a.reviewMdSetChecksum = ""; a.reviewMd = [{ checksum: null, path: 'api/REVIEW.md', text: '' }];
  }, expected: ['configEcho.review_md does not match reviewMd'] },
  { name: 'review md text type', input: (a) => {
    a.reviewMdSetChecksum = ""; a.reviewMd = [{ checksum: null, path: "api/REVIEW.md", text: 1 }]; a.configEcho.review_md.value = "present";
  }, expected: [ "reviewMd[0].text must be a string" ] },
  { name: 'review path absolute', input: (a) => {
    a.reviewMdSetChecksum = ""; a.reviewMd = [{ checksum: null, path: "/api/REVIEW.md", text: "" }]; a.configEcho.review_md.value = "present";
  }, expected: [
    "reviewMd[0].path must be repo-relative (must not start with /)",
    "reviewMd[0].path must not contain an empty path segment",
  ] },
  { name: 'raw parsed review', input: (a) => { a.reviewMdSetChecksum = ""; a.reviewMd = []; a.reviewConfig = {}; }, expected: [
    "reviewMd and reviewConfig are both present \u2014 pass raw reviewMd or pre-parsed reviewConfig, not both (single " +
      "authority)",
  ] },
  { name: 'raw parsed exclusions', input: (a) => { a.exclusionsText = ""; a.exclusionPatterns = []; }, expected: [
    "exclusionsText and exclusionPatterns are both present \u2014 pass raw exclusionsText or pre-parsed " +
      "exclusionPatterns, not both (single authority)",
  ] },
  { name: 'delivery tier', input: (a) => { a.delivery = { tier: "suggestions_only" }; }, expected: [
    "invalid delivery.tier: suggestions_only (expected one of all, main_only)",
    "delivery.tier does not match configEcho.delivery_tier",
  ] },
  { name: 'identity pr_number', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.pr_number = 0;
  }, expected: [ "delivery.prIdentity.pr_number must be a positive safe integer" ] },
  { name: 'identity sha_full', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.sha_full = "D".repeat(40);
  }, expected: [ "delivery.prIdentity.sha_full must be a 40-character lowercase hex commit id" ] },
  { name: 'identity web_origin', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    a.delivery.prIdentity.web_origin = "github.com";
  }, expected: [ "delivery.prIdentity.web_origin must be an http(s) origin: scheme, host and optional port only" ] },
  { name: 'identity repo slash',
    input: (a) => { a.delivery = { prIdentity: prIdentity() }; a.delivery.prIdentity.repo = "a/b"; },
    expected: [ "delivery.prIdentity.repo must not contain \"/\"" ] },
  { name: 'identity required missing', input: (a) => {
    a.delivery = { prIdentity: prIdentity() };
    delete a.delivery.prIdentity.platform;
  }, expected: [ "delivery.prIdentity.platform must be one of github, gitlab" ] },
  { name: 'persist returns',
    input: (a) => { a.persist = { returnPrimaries: "false" }; }, expected: [ "persist.returnPrimaries must be a boolean when present" ] },
  { name: 'persist shape', input: (a) => { a.persist = []; },
    expected: ['persist must be an object of the form { returnPrimaries } when present'] },
  { name: 'limits unknown', input: (a) => { a.limits.verifySclieSize = 50; }, expected: [
    "unknown limits key: verifySclieSize (expected one of summarizeBucketSize, validateBatch, challengeCap, " +
      "verifySliceSize, deliveryCap, discoveryCap, maxLineSpan)",
  ] },
  { name: 'multi-error limits', input: (a) => {
    a.limits = { summarizeBucketSize: 0, validateBatch: -1, challengeCap: -1, verifySliceSize: 0,
    deliveryCap: -1, discoveryCap: 0, maxLineSpan: 0 };
  }, expected: [
    "limits.summarizeBucketSize must be a positive safe integer when present",
    "limits.validateBatch must be a positive safe integer when present",
    "limits.verifySliceSize must be a positive safe integer when present",
    "limits.maxLineSpan must be a positive safe integer when present",
    "limits.challengeCap must be a non-negative safe integer when present",
    "limits.deliveryCap must be null, absent, or a non-negative safe integer when present",
    "limits.discoveryCap must be a positive safe integer when present",
  ] },
  { name: 'reviewScope extra',
    input: (a) => { a.reviewScope.reason = "old reason"; }, expected: [ "reviewScope has unexpected key(s): reason" ] },
  { name: 'scope kind', input: (a) => { a.reviewScope.kind = "bogus"; }, expected: [ "reviewScope.kind must be full or incremental" ] },
  { name: 'incremental since', input: (a) => {
    a.reviewScope = { requested: "incremental", kind: "incremental", since: "abc-1._x", commits: null,
    detector: priorReviewDetector() }; a.reviewScope.since = "bad sha!";
  }, expected: [ "reviewScope.since must be a NONCE_RE-safe string for incremental scope" ] },
  { name: 'incremental unsafe', input: (a) => {
    a.reviewScope = { requested: "incremental", kind: "incremental", since: "abc-1._x", commits: null,
    detector: priorReviewDetector() }; a.reviewScope.detector.incremental_safe = false;
  }, expected: [ "reviewScope.kind incremental requires detector.incremental_safe true" ] },
  { name: 'full fallback',
    input: (a) => { a.reviewScope.requested = "incremental"; },
    expected: [ "reviewScope full fallback requires detector facts when incremental was requested" ] },
  { name: 'detector extra',
    input: (a) => { a.reviewScope.detector = priorReviewDetector({ extra: true }); },
    expected: [ "reviewScope.detector has unexpected key(s): extra" ] },
  { name: 'detector missing', input: (a) => {
    a.reviewScope.detector = priorReviewDetector(); delete a.reviewScope.detector.error;
  }, expected: [
    "reviewScope.detector is missing key(s): error",
    "reviewScope.detector.error must be a string or null",
  ] },
  { name: 'detector previously_reviewed',
    input: (a) => { a.reviewScope.detector = priorReviewDetector({ previously_reviewed: "no" }); },
    expected: [ "reviewScope.detector.previously_reviewed must be a boolean" ] },
  { name: 'headless cap absent', base: headlessArgs,
    input: (a) => { delete a.limits.deliveryCap; },
    expected: [ "headless limits.deliveryCap must be a number matching configEcho.pr_comment_cap" ] },
  { name: 'interactive cap null mismatch',
    input: (a) => { a.limits.deliveryCap = null; a.configEcho.pr_comment_cap.value = "5"; },
    expected: [ "configEcho.pr_comment_cap must be null when limits.deliveryCap is absent or null" ] },
  { name: 'tier malformed receipt', base: headlessArgs, input: (a) => {
    a.delivery.tier = "main_only"; a.configEcho.delivery_tier.source = "fixed";
  }, expected: [ "configEcho.delivery_tier.source is invalid for headless" ] },
  { name: 'two scope refusals', base: headlessArgs, input: (a) => {
    a.scopeAnswer = "full"; a.configEcho.trivial_scope.value = "light";
  }, expected: [
    "scopeAnswer is \"full\" but the riskTable/changedLines are not light-eligible (not every file is low risk, or " +
      "changedLines >= 50) \u2014 the orchestrator answered a light/full question the gate never asked; omit scopeAnswer",
    "configEcho.trivial_scope is \"light\" but the riskTable/changedLines are not light-eligible (not every file is" +
      " low risk, or changedLines >= 50) \u2014 the orchestrator answered a light/full question the gate never asked",
  ] },
  { name: 'PR comments identity', base: headlessArgs,
    input: (a) => { a.configEcho.delivery.value = "pr_comments"; },
    expected: [ "delivery.prIdentity is required when configEcho.delivery contains pr_comments" ] },
  { name: 'malformed cap keeps later lockstep', base: headlessArgs, input: (a) => {
    a.limits.deliveryCap = 1; a.configEcho.pr_comment_cap.value = '007';
    a.reviewScope.requested = 'incremental';
  }, expected: [
    'configEcho.pr_comment_cap.value is invalid for headless',
    'reviewScope.requested does not match configEcho.reviewed_policy',
  ] },
  { name: 'headless answer lockstep', base: headlessArgs, input: (a) => {
    a.riskTable[0].risk = "low"; a.scopeAnswer = "full"; a.configEcho.trivial_scope.value = "light";
  }, expected: [ "scopeAnswer does not match configEcho.trivial_scope" ] },
  { name: 'required limits remains absent', base: headlessArgs,
    input: (a) => { delete a.limits; },
    expected: [ "missing required field: limits" ], normalize: true, check: (out) => { assert.equal("limits" in out, false); } },
  { name: 'null model receipt stays null', input: (a) => { a.configEcho.model_tier.value = null; },
    expected: ['configEcho.model_tier.value must be a string'], normalize: true,
    check: (out) => { assert.equal(out.configEcho.model_tier.value, null); } },
 ];
for (const { name, input, expected, base = interactiveArgs, normalize, check } of REJECTION_ROWS) {
  test(`validate: ${name}`, () => {
    const args = base();
    input(args);
    const out = normalize ? normalizeArgs(args) : args;
    assert.deepEqual(validateArgs(out), { ok: false, errors: expected });
    if (check) check(out);
  });
}

const NON_OBJECT_ROWS = [
  { name: 'string', input: 'waist', expected: { ok: false, errors: ['args is not an object'] } },
];
for (const { name, input, expected } of NON_OBJECT_ROWS) {
  test(`validate scalar: ${name}`, () => assert.deepEqual(validateArgs(input), expected));
}

const ACCEPTANCE_ROWS = [
  { name: 'drive roots and host selections', input: () => interactiveArgs({
    repoRoot: 'D:/repo', outputDir: 'D:/repo/.code-gauntlet', pluginRoot: 'C:/plug in', python: 'py -3',
  }), expected: { ok: true, errors: [] } },
  ...['fnv1a32:0x811c9dc5', null].map((checksum) => ({ name: `review checksum accepted ${checksum}`,
    input: () => interactiveArgs({ ...reviewMdArgs([{ path: 'api/REVIEW.md', text: '', checksum }]),
      configEcho: { ...interactiveArgs().configEcho, review_md: { value: 'present', source: 'discovery' } } }),
    expected: { ok: true, errors: [] },
  })),
  { name: 'local scope ignores policy lockstep', input: () => headlessArgs('incremental', { detector: null }),
    expected: { ok: true, errors: [] } },
  { name: 'optional domains', input: () => interactiveArgs({
    nonce: 'n-1.0_ab', policy: { tier: 'optimized', provider: null, gateway: null },
    contextLines: 5000000, contextChars: 500000000,
    reviewConfig: { confidence_threshold: 0, security_min_confidence: 100, severity_threshold: 'low', ignore: ['a.js'] },
    exclusionPatterns: ['*.js'], delivery: {}, persist: { returnPrimaries: false },
    limits: { challengeCap: 0, deliveryCap: null, discoveryCap: 1 },
  }), expected: { ok: true, errors: [] } },
  { name: 'incremental', input: () => interactiveArgs({
    reviewScope: { requested: 'incremental', kind: 'incremental', since: 'abc-1._x', commits: 0, detector: priorReviewDetector() },
  }), expected: { ok: true, errors: [] } },
  { name: 'single-line detector failure', input: () => headlessArgs('incremental', {
    requested: 'incremental', kind: 'full', since: null, commits: null,
    detector: priorReviewDetector({ head_advanced: false, error: 'fetch failed' }),
  }), expected: { ok: true, errors: [] } },
  { name: 'scripts at root', input: () => interactiveArgs({
    pluginRoot: '/', persist: { assembleScriptPath: '/scripts/assemble_artifacts.py' },
    verify: { scriptPath: '/scripts/verify_findings.py' },
  }), expected: { ok: true, errors: [] } },
  { name: 'IPv6 identity', input: () => interactiveArgs({ delivery: { prIdentity: {
    owner: 'o', repo: 'r', pr_number: 310, sha_full: 'dddddddddddddddddddddddddddddddddddddddd',
    platform: 'gitlab', web_origin: 'http://[::1]:8443', title: 'A useful title',
  } } }), expected: { ok: true, errors: [] } },
];
for (const { name, input, expected } of ACCEPTANCE_ROWS) {
  test(`accept: ${name}`, () => assert.deepEqual(validateArgs(input()), expected));
}

const REQUIRED_ROWS = [
  { name: 'ordered inventory', input: { argsVersion: 1 }, expected: { ok: false, errors: [
    'missing required field: mode', 'missing required field: repoRoot', 'missing required field: outputDir',
    'missing required field: headShaShort', 'missing required field: nonce', 'missing required field: generatedAt',
    'missing required field: diffPath', 'missing required field: changedFiles', 'missing required field: changedLines',
    'missing required field: riskTable', 'missing required field: policy', 'missing required field: limits',
    'missing required field: configEcho', 'missing required field: pluginRoot', 'missing required field: reviewScope',
  ] } },
];
for (const { name, input, expected } of REQUIRED_ROWS) {
  test(`required: ${name}`, () => assert.deepEqual(validateArgs(input), expected));
}

const STRIP_ROWS = [
  { name: 'optional inventory', input: {
    reviewConfig: null, exclusionPatterns: null, reviewMd: null, exclusionsText: null,
    delivery: null, checkpoints: null, persist: null, scopeAnswer: null,
  }, expected: { args: {}, dropped: [
    'reviewConfig', 'exclusionPatterns', 'reviewMd', 'exclusionsText', 'delivery', 'checkpoints', 'persist', 'scopeAnswer',
  ], respelled: [] } },
  { name: 'nested inventory', input: { delivery: { tier: null, prIdentity: null } },
    expected: { args: { delivery: {} }, dropped: ['delivery.prIdentity', 'delivery.tier'], respelled: [] } },
  { name: 'interactive null receipt', input: {
    mode: 'interactive', configEcho: { pr_comment_cap: { value: null, source: 'default' } },
  }, expected: { args: { mode: 'interactive', configEcho: { pr_comment_cap: { value: 'null', source: 'default' } } },
    dropped: [], respelled: ['configEcho.pr_comment_cap.value'] } },
];
for (const { name, input, expected } of STRIP_ROWS) {
  test(`strip: ${name}`, () => {
    const before = structuredClone(input);
    assert.deepEqual(stripNullOptionalsReport(input), expected);
    assert.deepEqual(input, before);
  });
}

const NORMALIZE_ROWS = [
  { name: 'JSON null', input: () => '{"reviewConfig":null}',
    expected: { args: {}, dropped: ['reviewConfig'], respelled: [], derivedPaths: [] } },
  { name: 'null cap derivation', input: () => ({
    mode: 'interactive', limits: {}, configEcho: { pr_comment_cap: { value: 'null', source: 'default' } },
  }), expected: { args: {
    mode: 'interactive', limits: {
      summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 25, maxLineSpan: 100, deliveryCap: null,
    }, configEcho: {
      pr_comment_cap: { value: 'null', source: 'default' }, review_md: { value: 'absent', source: 'discovery' },
    },
  }, dropped: [], respelled: [], derivedPaths: ['limits.deliveryCap'] } },
];
for (const { name, input, expected } of NORMALIZE_ROWS) {
  test(`normalize: ${name}`, () => {
    const raw = input();
    const before = structuredClone(raw);
    assert.deepEqual(normalizeArgsReport(raw), expected);
    assert.deepEqual(normalizeArgs(raw), expected.args);
    assert.deepEqual(raw, before);
  });
}

const PROVENANCE_ROWS = [
  { name: 'empty raw overrides legacy', input: { reviewMd: [], reviewConfigPath: '/r/REVIEW.md' },
    expected: { value: 'absent', source: 'discovery' } },
];
for (const { name, input, expected } of PROVENANCE_ROWS) {
  test(`provenance: ${name}`, () => {
    const args = interactiveArgs(input);
    delete args.configEcho.review_md;
    assert.deepEqual(normalizeArgs(args).configEcho.review_md, expected);
  });
}

const POLICY_DERIVE_ROWS = [
  { name: 'skip mapped', input: 'skip', expected: 'full' },
];
for (const { name, input, expected } of POLICY_DERIVE_ROWS) {
  test(`policy derivation: ${name}`, () => {
    const args = headlessArgs(input);
    delete args.reviewScope.requested;
    const before = structuredClone(args);
    assert.equal(normalizeArgs(args).reviewScope.requested, expected);
    assert.deepEqual(args, before);
  });
}

const SCOPE_DERIVE_ROWS = [
  { name: 'light', input: 'light', expected: { answer: 'light', drops: ['scopeAnswer'],
    paths: ['delivery.tier', 'scopeAnswer'], rejected: [], result: { ok: true, errors: [] } } },
];
for (const { name, input, expected } of SCOPE_DERIVE_ROWS) {
  test(`scope derivation: ${name}`, () => {
    const args = headlessArgs();
    args.riskTable[0].risk = 'low';
    args.scopeAnswer = null;
    args.configEcho.trivial_scope.value = input;
    const report = normalizeArgsReport(args);
    assert.deepEqual({ answer: report.args.scopeAnswer, drops: report.dropped, paths: report.derivedPaths,
      rejected: nullToleranceRejectedKeys(report.args, report.dropped, report.derivedPaths), result: validateArgs(report.args) }, expected);
  });
}

const NULL_PROBE_ROWS = [
  { name: 'null identity rejected', input: ['delivery.prIdentity'], derived: [], expected: ['delivery.prIdentity'] },
];
for (const { name, input, derived, expected } of NULL_PROBE_ROWS) {
  test(`null probe: ${name}`, () => {
    const args = interactiveArgs();
    const before = structuredClone(args);
    assert.deepEqual(nullToleranceRejectedKeys(args, input, derived), expected);
    assert.deepEqual(args, before);
  });
}

const RESOLVE_ROWS = [
  { name: 'none', input: undefined, expected: {
    reviewConfig: {}, exclusionPatterns: [], reviewConfigSource: 'none', exclusionsSource: 'none', reviewMdEntryCount: 0,
  } },
  { name: 'parsed axes', input: { reviewConfig: { ignore: ['a'] }, exclusionPatterns: ['b'] }, expected: {
    reviewConfig: { ignore: ['a'] }, exclusionPatterns: ['b'],
    reviewConfigSource: 'preParsed', exclusionsSource: 'preParsed', reviewMdEntryCount: 0,
  } },
  { name: 'empty raw axes override parsed', input: {
    reviewMd: [], exclusionsText: '', reviewConfig: { confidence_threshold: 99 }, exclusionPatterns: ['old'],
  }, expected: { reviewConfig: { ignore: [] }, exclusionPatterns: [],
    reviewConfigSource: 'reviewMd', exclusionsSource: 'exclusionsText', reviewMdEntryCount: 0 } },
  { name: 'raw root and child settings', input: { reviewMd: [
    { path: 'src/deep/REVIEW.md', text: '```yaml code-gauntlet\nconfidence_threshold: 90\nsecurity_min_confidence: 85\n' +
      'severity_threshold: high\nignore:\n  - c.js\n```' },
    { path: 'REVIEW.md', text: '```yaml code-gauntlet\nconfidence_threshold: 70\nsecurity_min_confidence: 75\n' +
      'severity_threshold: low\nignore:\n  - a.js\n  - b.js\n```' },
  ], exclusionsText: '- foo.js\n- bar/*.js\n' }, expected: {
    reviewConfig: { confidence_threshold: 70, security_min_confidence: 75, severity_threshold: 'low', ignore: ['a.js', 'b.js'],
      scopes: [{ dir: 'src/deep', confidence_threshold: 90, security_min_confidence: 85, severity_threshold: 'high', ignore: ['c.js'] }] },
    exclusionPatterns: ['foo.js', 'bar/*.js'], reviewConfigSource: 'reviewMd', exclusionsSource: 'exclusionsText', reviewMdEntryCount: 2,
  } },
];
const PROOF_TEXT = '```yaml code-gauntlet\nconfidence_threshold: 90\nignore:\n  - one  two.js\n```';
const TEXT_MISMATCH = 'review_md: api/REVIEW.md text differs from the collected file (checksum mismatch); its settings were applied as stamped';
const SET_MISMATCH = 'review_md: stamped entries differ from the collected list (omitted, extra or reordered REVIEW.md); settings were applied as stamped';
RESOLVE_ROWS.push({ name: 'whole array omitted', input: {
  reviewMdSetChecksum: reviewMdArgs([{ path: 'api/REVIEW.md', text: '' }]).reviewMdSetChecksum,
}, expected: { reviewConfig: {}, exclusionPatterns: [], reviewConfigSource: 'none', exclusionsSource: 'none',
  reviewMdEntryCount: 0, gaps: [SET_MISMATCH] } });
for (const row of [
  { name: 'proven', text: PROOF_TEXT },
  { name: 'CRLF equal', text: PROOF_TEXT.replaceAll('\n', '\r\n') },
  { name: 'lone CR equal and parsed', text: PROOF_TEXT.replaceAll('\n', '\r') },
  { name: 'BOM equal', text: '\uFEFF' + PROOF_TEXT },
  { name: 'trailing newlines equal', text: PROOF_TEXT + '\n\n' },
  { name: 'trailing spaces and tabs equal', text: PROOF_TEXT.replaceAll('\n', ' \t\n') + '\t ' },
  { name: 'interior whitespace differs', text: PROOF_TEXT.replace('one  two.js', 'one two.js'),
    gaps: [TEXT_MISMATCH], ignore: ['one two.js'] },
  { name: 'null checksum', text: PROOF_TEXT, checksum: null,
    gaps: ['review_md: api/REVIEW.md text is unproven (no collector checksum); its settings were applied as stamped'] },
]) {
  const input = reviewMdArgs([{ path: 'api/REVIEW.md', text: PROOF_TEXT,
    ...(Object.hasOwn(row, 'checksum') ? { checksum: row.checksum } : {}) }]);
  input.reviewMd[0].text = row.text;
  RESOLVE_ROWS.push({ name: row.name, input, expected: {
    reviewConfig: { ignore: [], scopes: [{ dir: 'api', confidence_threshold: 90, ignore: row.ignore || ['one  two.js'] }] },
    exclusionPatterns: [], reviewConfigSource: 'reviewMd', exclusionsSource: 'none', reviewMdEntryCount: 1,
    gaps: row.gaps || [],
  } });
}
for (const kind of ['omitted', 'extra', 'reordered']) {
  const input = reviewMdArgs([{ path: 'api/REVIEW.md', text: '' }, { path: 'docs/REVIEW.md', text: '' }]);
  if (kind === 'omitted') input.reviewMd.pop();
  if (kind === 'extra') input.reviewMd.push({ ...input.reviewMd[0], path: 'other/REVIEW.md' });
  if (kind === 'reordered') input.reviewMd.reverse();
  RESOLVE_ROWS.push({ name: kind, input, expected: {
    reviewConfig: { ignore: [], scopes: input.reviewMd.map((entry) => ({ dir: entry.path.split('/')[0], ignore: [] })) },
    exclusionPatterns: [], reviewConfigSource: 'reviewMd', exclusionsSource: 'none', reviewMdEntryCount: input.reviewMd.length,
    gaps: [SET_MISMATCH],
  } });
}
for (const { name, input, expected } of RESOLVE_ROWS) {
  test(`resolve config: ${name}`, () => {
    const stamped = input?.reviewMd && input.reviewMdSetChecksum === undefined ? { ...input, ...reviewMdArgs(input.reviewMd) } : input;
    const before = structuredClone(stamped);
    assert.deepEqual(resolveReviewConfig(stamped), { ...expected, gaps: expected.gaps || [] });
    assert.deepEqual(stamped, before);
  });
}

const GAP_ROWS = [
  { name: 'reviewConfig', input: 'reviewConfig', expected:
    "null_arg: args.reviewConfig arrived as a literal null and was treated as ABSENT \u2014 the review runs on " +
    "the Filter stage built-in thresholds (non-security 55, security 70) with no ignore list, NOT your " +
    "REVIEW.md configuration, so the delivered findings can differ. Omit reviewConfig entirely (or stamp a " +
    "well-formed value); do not stamp null." },
  { name: 'respelling', input: 'pr_comment_cap', respell: true, expected:
    'null_receipt: args.pr_comment_cap arrived as a literal null and was spelled as the printed ' +
      'token "null"; the receipt and the configuration are unchanged; stamp the string "null".' },

];
for (const { name, input, respell, expected } of GAP_ROWS) {
  test(`gap: ${name}`, () => assert.equal(respell ? nullRespellGap(input) : nullToleranceGap(input), expected));
}

const VALIDATION_BOUNDARY_ROWS = [
  { name: 'zero cap mismatch', input: () => {
    const a = interactiveArgs(); a.limits.deliveryCap = 0; return a;
  }, expected: { ok: false, errors: ['limits.deliveryCap does not match configEcho.pr_comment_cap'] } },
  { name: 'invalid interactive cap receipt', input: () => {
    const a = interactiveArgs(); a.configEcho.pr_comment_cap.value = '00'; return a;
  }, expected: { ok: false, errors: ['configEcho.pr_comment_cap.value is invalid for interactive'] } },
  { name: 'zero numeric receipt', input: () => {
    const a = interactiveArgs(); a.limits.deliveryCap = 0; a.configEcho.pr_comment_cap = { value: '0', source: 'env' }; return a;
  }, expected: { ok: true, errors: [] } },
  { name: 'review path dot-dot', input: () => {
    const a = interactiveArgs({ reviewMd: [{ path: 'api/../REVIEW.md', text: '' }] });
    a.configEcho.review_md.value = 'present'; return a;
  }, expected: { ok: false, errors: ['reviewMd[0].path must not contain . or .. path segments'] } },
  { name: 'PR comments complete identity', input: () => {
    const a = headlessArgs();
    a.configEcho.delivery.value = 'chat,pr_comments,markdown';
    a.delivery.prIdentity = { owner: 'o', repo: 'r', pr_number: 1, sha_full: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      platform: 'github', web_origin: 'https://github.com' };
    return a;
  }, expected: { ok: true, errors: [] } },
];
for (const { name, input, expected } of VALIDATION_BOUNDARY_ROWS) {
  test(`validate boundary: ${name}`, () => assert.deepEqual(validateArgs(input()), expected));
}
