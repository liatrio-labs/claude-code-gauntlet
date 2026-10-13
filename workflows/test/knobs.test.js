import { test } from 'node:test';
import assert from 'node:assert/strict';
import { KNOB_REGISTRY, DERIVE_WHEN, DERIVED_FROM, matchesRule, computeLightEligible } from '../src/knobs.js';
import { receiptEntryForKey, deriveConfigWaist, configEchoValue, validateConfigEcho } from '../src/knobs.js';
import { interactiveArgs, headlessArgs, knobRows, priorReviewDetector } from './helpers/argsFixtures.js';

const RULE_ROWS = [
  { name: 'enum malformed values', rule: { kind: 'enum' }, input: 'optimized', expected: false },
  { name: 'CSV rejects member', rule: { kind: 'csv_subset', values: ['chat'] }, input: 'chat, markdown', expected: false },
  { name: 'CSV spaced member', rule: { kind: 'csv_subset', values: ['chat', 'markdown'] },
    input: 'chat, markdown', expected: false },
  { name: 'CSV case', rule: { kind: 'csv_subset', values: ['chat', 'markdown'] }, input: 'Chat', expected: false },
  { name: 'CSV empty', rule: { kind: 'csv_subset', values: ['chat'] }, input: '', expected: false },
  { name: 'CSV malformed values', rule: { kind: 'csv_subset' }, input: 'chat', expected: false },
  { name: 'positive accepts safe maximum', rule: { kind: 'positive_digits' }, input: '9007199254740991', expected: true },
  { name: 'positive unsafe', rule: { kind: 'positive_digits' }, input: '9007199254740992', expected: false },
  { name: 'positive trailing space', rule: { kind: 'positive_digits' }, input: '5 ', expected: false },
  { name: 'positive leading space', rule: { kind: 'positive_digits' }, input: ' 5', expected: false },
  { name: 'positive sign', rule: { kind: 'positive_digits' }, input: '-1', expected: false },
  { name: 'positive zero', rule: { kind: 'positive_digits' }, input: '0', expected: false },
  { name: 'positive leading zero', rule: { kind: 'positive_digits' }, input: '01', expected: false },
  { name: 'positive non-ASCII', rule: { kind: 'positive_digits' }, input: '\u0661', expected: false },
  { name: 'digits zero', rule: { kind: 'digits_or_null' }, input: '0', expected: true },
  { name: 'digits null', rule: { kind: 'digits_or_null' }, input: 'null', expected: true },
  { name: 'digits safe maximum', rule: { kind: 'digits_or_null' }, input: '9007199254740991', expected: true },
  { name: 'digits unsafe', rule: { kind: 'digits_or_null' }, input: '9007199254740992', expected: false },
  { name: 'digits leading zero', rule: { kind: 'digits_or_null' }, input: '00', expected: false },
  { name: 'digits leading space', rule: { kind: 'digits_or_null' }, input: ' 5', expected: false },
  { name: 'digits trailing space', rule: { kind: 'digits_or_null' }, input: '5 ', expected: false },
  { name: 'digits zero-prefixed', rule: { kind: 'digits_or_null' }, input: '01', expected: false },
  { name: 'digits non-ASCII', rule: { kind: 'digits_or_null' }, input: '\u0661', expected: false },
  { name: 'unknown kind', rule: { kind: 'bogus' }, input: 'x', expected: false },
  { name: 'missing mode rule', rule: { headless: { kind: 'enum', values: ['x'] } }, input: 'x', expected: false },
  { name: 'mode rule', rule: { interactive: { kind: 'enum', values: ['x'] } }, input: 'x', expected: true },
  { name: 'non-string', rule: { kind: 'enum', values: ['x'] }, input: null, expected: false },
  { name: 'non-object rule', rule: [], input: 'x', expected: false },
];
for (const { name, rule, input, expected } of RULE_ROWS) {
  test(`rule: ${name}`, () => assert.equal(matchesRule(rule, input, 'interactive'), expected));
}

const ELIGIBILITY_ROWS = [
  { name: 'empty diff', input: [], lines: 0, expected: true },
  { name: 'low boundary', input: [{ risk: 'low' }], lines: 49, expected: true },
  { name: 'line boundary', input: [{ risk: 'low' }], lines: 50, expected: false },
  { name: 'medium', input: [{ risk: 'medium' }], lines: 1, expected: false },
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
  { name: 'delivery', mode: 'headless', key: 'delivery', entry: { value: 'chat,chat', source: 'review_md' },
    expected: ['configEcho.delivery.value is invalid for headless'] },
  { name: 'post mode', mode: 'headless', key: 'post_mode', entry: { value: 'later', source: 'default' },
    expected: ['configEcho.post_mode.value is invalid for headless'] },
  { name: 'headless cap', mode: 'headless', key: 'pr_comment_cap', entry: { value: '0', source: 'env' },
    expected: ['configEcho.pr_comment_cap.value is invalid for headless'] },
  { name: 'interactive cap', mode: 'interactive', key: 'pr_comment_cap', entry: { value: 'not-a-cap', source: 'env' },
    expected: ['configEcho.pr_comment_cap.value is invalid for interactive'] },
  { name: 'headless tier', mode: 'headless', key: 'delivery_tier', entry: { value: 'branch_only', source: 'default' },
    expected: ['configEcho.delivery_tier.value is invalid for headless'] },
  { name: 'interactive tier', mode: 'interactive', key: 'delivery_tier', entry: { value: 'branch_only', source: 'default' },
    expected: ['configEcho.delivery_tier.value is invalid for interactive'] },
  { name: 'draft policy', mode: 'headless', key: 'draft_policy', entry: { value: 'defer', source: 'env' },
    expected: ['configEcho.draft_policy.value is invalid for headless'] },
  { name: 'reviewed policy', mode: 'headless', key: 'reviewed_policy', entry: { value: 'partial', source: 'default' },
    expected: ['configEcho.reviewed_policy.value is invalid for headless'] },
  { name: 'PR not found', mode: 'headless', key: 'pr_not_found_policy', entry: { value: 'ignore', source: 'env' },
    expected: ['configEcho.pr_not_found_policy.value is invalid for headless'] },
  { name: 'trivial scope', mode: 'headless', key: 'trivial_scope', entry: { value: 'none', source: 'default' },
    expected: ['configEcho.trivial_scope.value is invalid for headless'] },
  { name: 'review md', mode: 'interactive', key: 'review_md', entry: { value: 'other', source: 'discovery' },
    expected: ['configEcho.review_md.value is invalid for interactive'] },
  { name: 'headless model_tier source', mode: 'headless', key: 'model_tier',
    entry: { value: 'optimized', source: 'fixed' }, expected: ['configEcho.model_tier.source is invalid for headless'] },
  { name: 'headless delivery source', mode: 'headless', key: 'delivery',
    entry: { value: 'markdown', source: 'fixed' }, expected: ['configEcho.delivery.source is invalid for headless'] },
  { name: 'headless post_mode source', mode: 'headless', key: 'post_mode',
    entry: { value: 'dry-run', source: 'fixed' }, expected: ['configEcho.post_mode.source is invalid for headless'] },
  { name: 'headless pr_comment_cap source', mode: 'headless', key: 'pr_comment_cap',
    entry: { value: '6', source: 'fixed' }, expected: ['configEcho.pr_comment_cap.source is invalid for headless'] },
  { name: 'headless delivery_tier source', mode: 'headless', key: 'delivery_tier',
    entry: { value: 'all', source: 'fixed' }, expected: ['configEcho.delivery_tier.source is invalid for headless'] },
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
  { name: 'interactive review_md source', mode: 'interactive', key: 'review_md',
    entry: { value: 'absent', source: 'fixed' }, expected: ['configEcho.review_md.source is invalid for interactive'] },
  { name: 'entry shape', mode: 'interactive', key: 'model_tier', entry: [],
    expected: ['configEcho.model_tier must be { value, source }'] },
  { name: 'null entry', mode: 'interactive', key: 'model_tier', entry: null,
    expected: ['configEcho.model_tier must be { value, source }'] },
  { name: 'string entry', mode: 'interactive', key: 'model_tier', entry: 'entry',
    expected: ['configEcho.model_tier must be { value, source }'] },
  { name: 'value type', mode: 'interactive', key: 'model_tier', entry: { value: null, source: 'fixed' },
    expected: ['configEcho.model_tier.value must be a string'] },
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
  { name: 'absent', input: undefined, expected: [] },
  { name: 'array', input: [], expected: ['configEcho must be an object keyed by methodology knob'] },
  { name: 'string', input: 'echo', expected: ['configEcho must be an object keyed by methodology knob'] },
  { name: 'missing and extra', input: {
    model_tier: { value: 'optimized', source: 'fixed' }, pr_comment_cap: { value: 'null', source: 'default' },
    delivery_tier: { value: 'all', source: 'default' }, unexpected: { value: 'x', source: 'default' },
  }, expected: ['configEcho has unexpected key(s): unexpected', 'configEcho is missing key(s): review_md',
    'configEcho.review_md must be { value, source }'] },
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
  { name: 'read string', input: { configEcho: { x: { value: 'v' } } }, key: 'x', expected: 'v' },
  { name: 'missing args', input: null, key: 'x', expected: undefined },
  { name: 'bad receipt container', input: { configEcho: [] }, key: 'x', expected: undefined },
  { name: 'bad entry', input: { configEcho: { x: Object.assign([], { value: 'v' }) } }, key: 'x', expected: undefined },
  { name: 'bad value', input: { configEcho: { x: { value: null } } }, key: 'x', expected: undefined },
];
for (const { name, input, key, expected } of VALUE_ROWS) {
  test(`echo value: ${name}`, () => assert.equal(configEchoValue(input, key), expected));
}

const LOOKUP_ROWS = [
  { name: 'valid', key: 'model_tier', expected: { value: 'optimized', source: 'fixed' } },
  { name: 'unknown', key: 'unknown', expected: null },
];
for (const { name, key, expected } of LOOKUP_ROWS) {
  test(`receipt lookup: ${name}`, () => assert.deepEqual(receiptEntryForKey(interactiveArgs(), key), expected));
}

const DERIVE_ROWS = [
  { name: 'malformed args', input: null, expected: { args: null, derivedPaths: [] } },
  { name: 'malformed echo', input: { configEcho: [], mode: 'interactive' },
    expected: { args: { configEcho: [], mode: 'interactive' }, derivedPaths: [] } },
  { name: 'missing mode', input: { configEcho: {} }, expected: { args: { configEcho: {} }, derivedPaths: [] } },
  { name: 'nested copy', input: { mode: 'headless', configEcho: { reviewed_policy: { value: 'skip', source: 'default' } },
    reviewScope: { detector: priorReviewDetector() } },
    expected: { requested: 'full', paths: ['reviewScope.requested'] } },
];
for (const { name, input, expected } of DERIVE_ROWS) {
  test(`derive: ${name}`, () => {
    const before = structuredClone(input);
    const out = deriveConfigWaist(input);
    const actual = name === 'nested copy'
      ? { requested: out.args.reviewScope.requested, paths: out.derivedPaths } : out;
    assert.deepEqual(actual, expected);
    assert.deepEqual(input, before);
  });
}

const DESCRIPTOR_ROWS = [
  { name: 'model_tier', expected: [
    ['headless', 'interactive'], { headless: ['env', 'default'], interactive: ['fixed'] },
    { kind: 'enum', values: ['optimized'] }, 'CODE_GAUNTLET_MODEL_TIER', null,
    { headless: ['optimized', 'default'], interactive: ['optimized', 'fixed'] }, 'string', null, null, null, null, [], true,
  ] },
  { name: 'delivery', expected: [
    ['headless'], { headless: ['env', 'review_md', 'default'] }, { kind: 'csv_subset', values: ['chat', 'pr_comments', 'markdown'] },
    'CODE_GAUNTLET_DELIVERY', 'default_delivery', { headless: ['markdown', 'default'] },
    'csv_list', null, null, null, null, [], true,
  ] },
  { name: 'post_mode', expected: [
    ['headless'], { headless: ['env', 'default'] }, { kind: 'enum', values: ['dry-run', 'live'] },
    'CODE_GAUNTLET_POST_MODE', null, { headless: ['dry-run', 'default'] }, 'string', null, null, null, null, [], true,
  ] },
  { name: 'pr_comment_cap', expected: [
    ['headless', 'interactive'], { headless: ['env', 'default'], interactive: ['env', 'default'] },
    { headless: { kind: 'positive_digits' }, interactive: { kind: 'digits_or_null' } }, 'CODE_GAUNTLET_PR_COMMENT_CAP', null,
    { headless: ['6', 'default'], interactive: ['null', 'default'] }, 'int_or_null', 'limits.deliveryCap', null, null, null,
    ['interactive'], false,
  ] },
  { name: 'delivery_tier', expected: [
    ['headless', 'interactive'], { headless: ['env', 'default'], interactive: ['env', 'default'] },
    { kind: 'enum', values: ['all', 'main_only'] }, 'CODE_GAUNTLET_DELIVERY_TIER', null,
    { headless: ['all', 'default'], interactive: ['all', 'default'] }, 'string', 'delivery.tier', null, null, null, [], false,
  ] },
  { name: 'draft_policy', expected: [
    ['headless'], { headless: ['env', 'default'] }, { kind: 'enum', values: ['review', 'skip'] },
    'CODE_GAUNTLET_DRAFT_POLICY', null, { headless: ['review', 'default'] }, 'string', null, null, null, null, [], true,
  ] },
  { name: 'reviewed_policy', expected: [
    ['headless'], { headless: ['env', 'default'] }, { kind: 'enum', values: ['incremental', 'full', 'skip'] },
    'CODE_GAUNTLET_REVIEWED_POLICY', null, { headless: ['full', 'default'] },
    'string', 'reviewScope.requested', { skip: 'full' }, null, 'priorReviewDetector', [], true,
  ] },
  { name: 'pr_not_found_policy', expected: [
    ['headless'], { headless: ['env', 'default'] }, { kind: 'enum', values: ['local', 'error'] },
    'CODE_GAUNTLET_PR_NOT_FOUND_POLICY', null, { headless: ['error', 'default'] }, 'string', null, null, null, null, [], true,
  ] },
  { name: 'trivial_scope', expected: [
    ['headless'], { headless: ['env', 'default'] }, { kind: 'enum', values: ['light', 'full'] },
    'CODE_GAUNTLET_TRIVIAL_SCOPE', null, { headless: ['full', 'default'] },
    'string', 'scopeAnswer', null, null, 'lightEligible', [], false,
  ] },
  { name: 'review_md', expected: [
    ['interactive'], { interactive: ['discovery'] }, { kind: 'enum', values: ['present', 'absent'] },
    null, null, { interactive: ['absent', 'discovery'] }, 'string', null, null, 'reviewMd', null, [], false,
  ] },
];
for (const { name, expected } of DESCRIPTOR_ROWS) {
  test(`descriptor: ${name}`, () => {
    const row = KNOB_REGISTRY.find((d) => d.key === name);
    assert.deepEqual([row.modes, row.allowedSources, row.rule, row.env, row.reviewMdKey, row.defaults,
      row.type, row.waistPath, row.waistMap, row.derivedFrom, row.deriveWhen, row.nullReceipt, row.resolvedKey], expected);
    assert.deepEqual(Object.keys(row), [
      'key', 'modes', 'allowedSources', 'rule', 'env', 'reviewMdKey', 'defaults', 'type', 'waistPath', 'waistMap',
      'derivedFrom', 'deriveWhen', 'nullReceipt', 'resolvedKey',
    ]);
    assert.ok(Object.isFrozen(row));
    assert.ok(Object.isFrozen(row.rule));
    assert.ok(Object.isFrozen(row.defaults));
  });
}

const KNOB_DATA_ROWS = [
  { name: 'complete order', input: KNOB_REGISTRY.map((row) => row.key), expected: [
    'model_tier', 'delivery', 'post_mode', 'pr_comment_cap', 'delivery_tier', 'draft_policy',
    'reviewed_policy', 'pr_not_found_policy', 'trivial_scope', 'review_md',
  ] },
  { name: 'headless order', input: KNOB_REGISTRY.filter((d) => d.modes.includes('headless')).map((d) => d.key), expected: [
    'model_tier', 'delivery', 'post_mode', 'pr_comment_cap', 'delivery_tier', 'draft_policy',
    'reviewed_policy', 'pr_not_found_policy', 'trivial_scope',
  ] },
  { name: 'interactive order', input: KNOB_REGISTRY.filter((d) => d.modes.includes('interactive')).map((d) => d.key),
    expected: ['model_tier', 'pr_comment_cap', 'delivery_tier', 'review_md'] },
  { name: 'gate descriptions', input: Object.entries(DERIVE_WHEN).map(([name, row]) => [name, typeof row.holds, row.describe]),
    expected: [
      ['lightEligible', 'function', 'every changed file is low risk and fewer than 50 lines changed'],
      ['priorReviewDetector', 'function', '`reviewScope.detector` is an object'],
    ] },
  { name: 'source descriptions', input: Object.entries(DERIVED_FROM).map(([name, row]) =>
    [name, typeof row.fill, row.source, row.describe]), expected: [
    ['reviewMd', 'function', 'discovery', '`present` when the `reviewMd` array is nonempty, else `absent`; without `reviewMd`, ' +
      '`present` when `reviewConfigPath` is set, else `absent`'],
  ] },
];
for (const { name, input, expected } of KNOB_DATA_ROWS) {
  test(`knob data: ${name}`, () => assert.deepEqual(input, expected));
}
