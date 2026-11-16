import { test } from 'node:test';
import assert from 'node:assert/strict';
import { KNOB_REGISTRY, DERIVE_WHEN, matchesRule, computeLightEligible } from '../src/knobs.js';
import { receiptEntryForKey, deriveConfigWaist, configEchoValue, validateConfigEcho } from '../src/knobs.js';
import { interactiveArgs, headlessArgs, validArgs, knobRows, priorReviewDetector } from './helpers/argsFixtures.js';

const RULE_ROWS = [
  { name: 'enum malformed values', rule: { kind: 'enum' }, input: 'optimized', expected: false },
  { name: 'CSV rejects member', rule: { kind: 'csv_subset', values: ['chat'] }, input: 'chat, markdown', expected: false },
  { name: 'CSV spaced member', rule: { kind: 'csv_subset', values: ['chat', 'markdown'] },
    input: 'chat, markdown', expected: false },
  { name: 'CSV case', rule: { kind: 'csv_subset', values: ['chat', 'markdown'] }, input: 'Chat', expected: false },
  { name: 'CSV empty', rule: { kind: 'csv_subset', values: ['chat'] }, input: '', expected: false },
  { name: 'CSV malformed values', rule: { kind: 'csv_subset' }, input: 'chat', expected: false },
  { name: 'positive unsafe', rule: { kind: 'positive_digits' }, input: '9007199254740992', expected: false },
  { name: 'positive trailing space', rule: { kind: 'positive_digits' }, input: '5 ', expected: false },
  { name: 'positive leading space', rule: { kind: 'positive_digits' }, input: ' 5', expected: false },
  { name: 'positive sign', rule: { kind: 'positive_digits' }, input: '-1', expected: false },
  { name: 'positive zero', rule: { kind: 'positive_digits' }, input: '0', expected: false },
  { name: 'positive leading zero', rule: { kind: 'positive_digits' }, input: '01', expected: false },
  { name: 'positive non-ASCII', rule: { kind: 'positive_digits' }, input: '\u0661', expected: false },
  { name: 'digits zero', rule: { kind: 'digits_or_null' }, input: '0', expected: true },
  { name: 'digits null', rule: { kind: 'digits_or_null' }, input: 'null', expected: true },
  { name: 'digits unsafe', rule: { kind: 'digits_or_null' }, input: '9007199254740992', expected: false },
  { name: 'digits leading space', rule: { kind: 'digits_or_null' }, input: ' 5', expected: false },
  { name: 'digits trailing space', rule: { kind: 'digits_or_null' }, input: '5 ', expected: false },
  { name: 'digits zero-prefixed', rule: { kind: 'digits_or_null' }, input: '01', expected: false },
  { name: 'digits non-ASCII', rule: { kind: 'digits_or_null' }, input: '\u0661', expected: false },
  { name: 'unknown kind', rule: { kind: 'bogus' }, input: 'x', expected: false },
  { name: 'missing mode rule', rule: { headless: { kind: 'enum', values: ['x'] } }, input: 'x', expected: false },
  { name: 'non-string', rule: { kind: 'enum', values: ['x'] }, input: null, expected: false },
];
for (const { name, rule, input, expected } of RULE_ROWS) {
  test(`rule: ${name}`, () => assert.equal(matchesRule(rule, input, 'interactive'), expected));
}

const ELIGIBILITY_ROWS = [
  { name: 'line boundary', input: [{ risk: 'low' }], lines: 50, expected: false },
  { name: 'null entry', input: [null], lines: 1, expected: false },
  { name: 'non-array', input: {}, lines: 1, expected: false },
  { name: 'non-number', input: [], lines: '1', expected: false },
];
for (const { name, input, lines, expected } of ELIGIBILITY_ROWS) {
  test(`eligibility: ${name}`, () => assert.equal(computeLightEligible(input, lines), expected));
}

const RECEIPT_ROWS = [
  { name: 'interactive model', mode: 'interactive', key: 'model_tier', entry: { value: 'standard', source: 'fixed' },
    expected: ['configEcho.model_tier.value is invalid for interactive'] },
  { name: 'headless model', mode: 'headless', key: 'model_tier', entry: { value: 'standard', source: 'env' },
    expected: ['configEcho.model_tier.value is invalid for headless'] },
  { name: 'delivery', mode: 'headless', key: 'delivery', entry: { value: 'chat,chat', source: 'env' },
    expected: ['configEcho.delivery.value is invalid for headless'] },
  { name: 'post mode', mode: 'headless', key: 'post_mode', entry: { value: 'later', source: 'default' },
    expected: ['configEcho.post_mode.value is invalid for headless'] },
  { name: 'headless tier', mode: 'headless', key: 'delivery_tier', entry: { value: 'branch_only', source: 'default' },
    expected: ['configEcho.delivery_tier.value is invalid for headless'] },
  { name: 'interactive tier', mode: 'interactive', key: 'delivery_tier', entry: { value: 'branch_only', source: 'default' },
    expected: ['configEcho.delivery_tier.value is invalid for interactive'] },
  { name: 'draft policy', mode: 'headless', key: 'draft_policy', entry: { value: 'defer', source: 'env' },
    expected: ['configEcho.draft_policy.value is invalid for headless'] },
  { name: 'PR not found', mode: 'headless', key: 'pr_not_found_policy', entry: { value: 'ignore', source: 'env' },
    expected: ['configEcho.pr_not_found_policy.value is invalid for headless'] },
  { name: 'headless model_tier source', mode: 'headless', key: 'model_tier',
    entry: { value: 'optimized', source: 'fixed' }, expected: ['configEcho.model_tier.source is invalid for headless'] },
  { name: 'headless delivery source', mode: 'headless', key: 'delivery',
    entry: { value: 'markdown', source: 'fixed' }, expected: ['configEcho.delivery.source is invalid for headless'] },
  { name: 'headless post_mode source', mode: 'headless', key: 'post_mode',
    entry: { value: 'dry-run', source: 'fixed' }, expected: ['configEcho.post_mode.source is invalid for headless'] },
  { name: 'headless pr_comment_cap source', mode: 'headless', key: 'pr_comment_cap',
    entry: { value: '6', source: 'fixed' }, expected: ['configEcho.pr_comment_cap.source is invalid for headless'] },
  { name: 'headless draft_policy source', mode: 'headless', key: 'draft_policy',
    entry: { value: 'review', source: 'fixed' }, expected: ['configEcho.draft_policy.source is invalid for headless'] },
  { name: 'headless reviewed_policy source', mode: 'headless', key: 'reviewed_policy',
    entry: { value: 'full', source: 'fixed' }, expected: ['configEcho.reviewed_policy.source is invalid for headless'] },
  { name: 'headless pr_not_found_policy source', mode: 'headless', key: 'pr_not_found_policy',
    entry: { value: 'error', source: 'fixed' }, expected: ['configEcho.pr_not_found_policy.source is invalid for headless'] },
  { name: 'headless trivial_scope source', mode: 'headless', key: 'trivial_scope',
    entry: { value: 'full', source: 'fixed' }, expected: ['configEcho.trivial_scope.source is invalid for headless'] },
  { name: 'interactive model_tier source', mode: 'interactive', key: 'model_tier',
    entry: { value: 'optimized', source: 'env' }, expected: ['configEcho.model_tier.source is invalid for interactive'] },
  { name: 'interactive pr_comment_cap source', mode: 'interactive', key: 'pr_comment_cap',
    entry: { value: 'null', source: 'fixed' }, expected: ['configEcho.pr_comment_cap.source is invalid for interactive'] },
  { name: 'interactive delivery_tier source', mode: 'interactive', key: 'delivery_tier',
    entry: { value: 'all', source: 'fixed' }, expected: ['configEcho.delivery_tier.source is invalid for interactive'] },
  { name: 'entry shape', mode: 'interactive', key: 'model_tier', entry: [],
    expected: ['configEcho.model_tier must be { value, source }'] },
  { name: 'null entry', mode: 'interactive', key: 'model_tier', entry: null,
    expected: ['configEcho.model_tier must be { value, source }'] },
  { name: 'string entry', mode: 'interactive', key: 'model_tier', entry: 'entry',
    expected: ['configEcho.model_tier must be { value, source }'] },
  { name: 'numeric value', mode: 'interactive', key: 'model_tier', entry: { value: 4, source: 'fixed' },
    expected: ['configEcho.model_tier.value must be a string'] },
  { name: 'boolean value', mode: 'interactive', key: 'model_tier', entry: { value: false, source: 'fixed' },
    expected: ['configEcho.model_tier.value must be a string'] },
  { name: 'entry extra', mode: 'interactive', key: 'model_tier', entry: { value: 'optimized', source: 'fixed', extra: true },
    expected: ['configEcho.model_tier has unexpected key(s): extra'] },
];
for (const { name, mode, key, entry, expected } of RECEIPT_ROWS) {
  test(`receipt: ${name}`, () => {
    const args = mode === 'headless' ? headlessArgs() : interactiveArgs();
    args.configEcho[key] = entry;
    const errors = [];
    validateConfigEcho(args, errors);
    assert.deepEqual(errors, expected);
    if (name !== 'entry extra') assert.equal(receiptEntryForKey(args, key), null);
  });
}

const RECEIPT_SHAPE_ROWS = [
  { name: 'array', input: [], expected: ['configEcho must be an object keyed by methodology knob'] },
  { name: 'string', input: 'echo', expected: ['configEcho must be an object keyed by methodology knob'] },
  { name: 'missing and extra', input: {
    model_tier: { value: 'optimized', source: 'fixed' }, pr_comment_cap: { value: 'null', source: 'default' },
    delivery_tier: { value: 'all', source: 'default' }, unexpected: { value: 'x', source: 'default' },
  }, expected: ['configEcho has unexpected key(s): unexpected'] },
];
for (const { name, input, expected } of RECEIPT_SHAPE_ROWS) {
  test(`receipt shape: ${name}`, () => {
    const errors = [];
    validateConfigEcho(interactiveArgs({ configEcho: input }), errors);
    assert.deepEqual(errors, expected);
  });
}

const SAFE_RECEIPT_ROWS = [
  { name: 'control', input: 'optimized\n', expected: null },
  { name: 'backtick', input: 'optimized`', expected: null },
];
for (const { name, input, expected } of SAFE_RECEIPT_ROWS) {
  test(`safe receipt: ${name}`, () => {
    const table = knobRows({ model_tier: { rule: { kind: 'enum', values: [input] } } });
    const args = interactiveArgs();
    args.configEcho.model_tier.value = input;
    assert.equal(receiptEntryForKey(args, 'model_tier', table), expected);
    const errors = [];
    validateConfigEcho(args, errors, table);
    assert.deepEqual(errors, ['configEcho.model_tier.value must be single-line and contain no controls or backticks']);
  });
}

const VALUE_ROWS = [
  { name: 'missing args', input: null, key: 'x', expected: undefined },
  { name: 'bad value', input: { configEcho: { x: { value: null } } }, key: 'x', expected: undefined },
];
for (const { name, input, key, expected } of VALUE_ROWS) {
  test(`echo value: ${name}`, () => assert.equal(configEchoValue(input, key), expected));
}

const LOOKUP_ROWS = [
  { name: 'unknown', key: 'unknown', expected: null },
];
for (const { name, key, expected } of LOOKUP_ROWS) {
  test(`receipt lookup: ${name}`, () => assert.deepEqual(receiptEntryForKey(interactiveArgs(), key), expected));
}

const nestedCopyInput = headlessArgs('incremental');
nestedCopyInput.configEcho = { reviewed_policy: { value: 'incremental', source: 'default' } };
delete nestedCopyInput.reviewScope.requested;
const DERIVE_ROWS = [
  { name: 'malformed echo', input: { configEcho: [], mode: 'interactive' },
    expected: { args: { configEcho: [], mode: 'interactive' }, derivedPaths: [] } },
  { name: 'nested copy', input: nestedCopyInput, expected: {
    requested: 'incremental', paths: ['reviewScope.requested'],
  } },
];
for (const { name, input, expected } of DERIVE_ROWS) {
  test(`derive: ${name}`, () => {
    const before = structuredClone(input);
    const out = deriveConfigWaist(input);
    const actual = name === 'nested copy'
      ? { requested: out.args.reviewScope.requested, paths: out.derivedPaths } : out;
    assert.deepEqual(actual, expected);
    assert.deepEqual(input, before);
    if (name === 'nested copy') assert.notEqual(out.args.reviewScope, input.reviewScope);
  });
}

for (const mode of ['headless', 'interactive']) {
  test(`default receipt: ${mode}`, () => {
    const configEcho = Object.fromEntries(KNOB_REGISTRY.filter((row) => row.modes.includes(mode)).map((row) => {
      const [value, source] = row.defaults[mode];
      return [row.key, { value, source }];
    }));
    for (const args of mode === 'headless' ? [headlessArgs(), validArgs()] : [interactiveArgs()]) {
      assert.deepEqual(args.configEcho, configEcho);
      const errors = [];
      validateConfigEcho(args, errors);
      assert.deepEqual(errors, []);
      if (mode === 'headless') assert.equal(args.limits.deliveryCap, Number(configEcho.pr_comment_cap.value));
    }
  });
}


const KNOB_DATA_ROWS = [
  { name: 'complete order', input: KNOB_REGISTRY.map((row) => row.key), expected: [
    'model_tier', 'delivery', 'post_mode', 'pr_comment_cap', 'delivery_tier', 'draft_policy',
    'reviewed_policy', 'pr_not_found_policy', 'trivial_scope',
  ] },
  { name: 'headless order', input: KNOB_REGISTRY.filter((d) => d.modes.includes('headless')).map((d) => d.key), expected: [
    'model_tier', 'delivery', 'post_mode', 'pr_comment_cap', 'delivery_tier', 'draft_policy',
    'reviewed_policy', 'pr_not_found_policy', 'trivial_scope',
  ] },
  { name: 'interactive order', input: KNOB_REGISTRY.filter((d) => d.modes.includes('interactive')).map((d) => d.key),
    expected: ['model_tier', 'pr_comment_cap', 'delivery_tier'] },
  { name: 'gate descriptions', input: Object.entries(DERIVE_WHEN).map(([name, row]) => [name, typeof row.holds, row.describe]),
    expected: [
      ['lightEligible', 'function', 'every changed file is low risk and fewer than 50 lines changed'],
      ['priorReviewDetector', 'function', '`reviewScope.detector` is an object'],
    ] },
];
for (const { name, input, expected } of KNOB_DATA_ROWS) {
  test(`knob data: ${name}`, () => assert.deepEqual(input, expected));
}
