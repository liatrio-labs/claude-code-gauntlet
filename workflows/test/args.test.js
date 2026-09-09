import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ARGS_VERSION, normalizeArgs, validateArgs, parseEntryArgs,
  stripNullOptionalsReport, normalizeArgsReport, nullToleranceGap, nullRespellGap, LIMIT_DEFAULTS,
  resolveReviewConfig, computeLightEligible, nullToleranceRejectedKeys, KNOB_REGISTRY, safeReceiptValue,
  REQUIRED, LIGHT_SCOPE_MAX_CHANGED_LINES, DERIVE_WHEN, DERIVED_FROM,
  matchesRule, isPriorReviewDetector,
} from '../src/args.js';

const good = {
  argsVersion: 1, mode: 'interactive', repoRoot: '/r', outputDir: '/r/.code-gauntlet',
  headShaShort: 'abc123', nonce: 'n-1', generatedAt: '2026-07-18T00:00:00Z',
  diffPath: '/r/.code-gauntlet/d.patch', changedFilesPath: '/r/.code-gauntlet/f.json',
  changedFiles: ['a.js'], changedLines: 1,
  // 'medium', not 'low': changedLines(1) < 50, so a 'low' entry would be light-eligible and
  // require a coherent scopeAnswer — keep the base fixture scope-neutral (full scope, no
  // scopeAnswer needed) the same way it always was ({} agentFlags == full scope).
  reviewConfigPath: null, riskTable: [{ path: 'a.js', risk: 'medium' }],
  policy: { tier: 'optimized', subagentModel: null },
  configEcho: {
    model_tier: { value: 'optimized', source: 'fixed' },
    delivery_tier: { value: 'all', source: 'default' },
    pr_comment_cap: { value: 'null', source: 'default' },
    review_md: { value: 'absent', source: 'discovery' },
  },
  pluginRoot: '/plugin',
  reviewScope: { requested: 'full', kind: 'full', since: null, commits: null, detector: null },
  limits: {
    summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 25,
    maxLineSpan: 100,
  },
};

const priorReviewDetector = {
  previously_reviewed: true,
  sha_resolvable: true,
  head_advanced: true,
  sha_is_ancestor: true,
  incremental_safe: true,
  error: null,
};

function headlessArgs(reviewedPolicy = 'full', reviewScope = {}) {
  return {
    ...good,
    mode: 'headless',
    delivery: {},
    limits: { ...good.limits, deliveryCap: 6 },
    configEcho: {
      model_tier: { value: 'optimized', source: 'default' },
      delivery: { value: 'markdown', source: 'default' },
      post_mode: { value: 'dry-run', source: 'default' },
      pr_comment_cap: { value: '6', source: 'default' },
      delivery_tier: { value: 'all', source: 'default' },
      draft_policy: { value: 'review', source: 'default' },
      reviewed_policy: { value: reviewedPolicy, source: 'default' },
      pr_not_found_policy: { value: 'error', source: 'default' },
      trivial_scope: { value: 'full', source: 'default' },
    },
    reviewScope: {
      requested: 'full', kind: 'full', since: null, commits: null,
      detector: priorReviewDetector,
      ...reviewScope,
    },
  };
}

test('normalizeArgs parses a JSON string (session tool-call form)', () => {
  assert.deepEqual(normalizeArgs(JSON.stringify(good)), {
    ...good,
    delivery: { tier: 'all' },
    limits: { ...good.limits, deliveryCap: null },
  });
});
test('normalizeArgs passes an object through (workflow-nesting form)', () => {
  assert.deepEqual(normalizeArgs(good), {
    ...good,
    delivery: { tier: 'all' },
    limits: { ...good.limits, deliveryCap: null },
  });
});
test('validateArgs accepts a well-formed waist', () => {
  assert.deepEqual(validateArgs(good), { ok: true, errors: [] });
});

test('T182-ARGS: required receipt has the registry-defined mode key sets and render order', () => {
  assert.deepEqual(
    KNOB_REGISTRY.filter((d) => d.modes.includes('headless')).map((d) => d.key),
    ['model_tier', 'delivery', 'post_mode', 'pr_comment_cap', 'delivery_tier', 'draft_policy', 'reviewed_policy', 'pr_not_found_policy', 'trivial_scope'],
  );
  assert.deepEqual(
    KNOB_REGISTRY.filter((d) => d.modes.includes('interactive')).map((d) => d.key),
    ['model_tier', 'pr_comment_cap', 'delivery_tier', 'review_md'],
  );
  for (const field of ['configEcho', 'pluginRoot', 'reviewScope']) {
    const a = { ...good }; delete a[field];
    const result = validateArgs(a);
    assert.equal(result.ok, false);
    assert.ok(result.errors.some((error) => error.includes(`missing required field: ${field}`)));
  }
});

test('T182-ARGS: configEcho top-level shapes are rejected directly', () => {
  for (const bad of [null, [], 'echo']) {
    const result = validateArgs({ ...good, configEcho: bad });
    assert.equal(result.ok, false, JSON.stringify(bad));
    assert.ok(result.errors.some((error) => error.includes('configEcho must be an object')));
  }
});

test('T182-ARGS: configEcho entry shape and value type are rejected directly', () => {
  for (const bad of [null, [], 'entry']) {
    const result = validateArgs({ ...good, configEcho: { ...good.configEcho, model_tier: bad } });
    assert.equal(result.ok, false, JSON.stringify(bad));
    assert.ok(result.errors.some((error) => error.includes('configEcho.model_tier must be { value, source }')));
  }
  for (const bad of [null, 4, false]) {
    const result = validateArgs({ ...good, configEcho: { ...good.configEcho, model_tier: { value: bad, source: 'fixed' } } });
    assert.equal(result.ok, false, JSON.stringify(bad));
    assert.ok(result.errors.some((error) => error.includes('configEcho.model_tier.value must be a string')));
  }
});

test('T182-ARGS: safeReceiptValue rejects control and backtick suffixes on an enum-valid value', () => {
  for (const value of ['optimized\n', 'optimized`']) {
    assert.equal(safeReceiptValue(value), false, JSON.stringify(value));
  }
});

test('T182-ARGS: every registry receipt value rule and source rule is focused', () => {
  const headless = {
    ...good,
    mode: 'headless',
    delivery: { tier: 'all', prIdentity: { owner: 'o', repo: 'r', pr_number: 1, sha_full: 's' } },
    configEcho: {
      model_tier: { value: 'optimized', source: 'env' },
      delivery: { value: 'chat,pr_comments,markdown', source: 'review_md' },
      post_mode: { value: 'live', source: 'default' },
      pr_comment_cap: { value: '1', source: 'env' },
      delivery_tier: { value: 'all', source: 'default' },
      draft_policy: { value: 'skip', source: 'env' },
      reviewed_policy: { value: 'full', source: 'default' },
      pr_not_found_policy: { value: 'local', source: 'env' },
      trivial_scope: { value: 'full', source: 'default' },
    },
    limits: { ...good.limits, deliveryCap: 1 },
  };
  assert.equal(validateArgs(headless).ok, true);
  const missingKey = { ...headless, configEcho: { ...headless.configEcho } };
  delete missingKey.configEcho.delivery;
  assert.equal(validateArgs(missingKey).ok, false);
  const extraKey = { ...headless, configEcho: { ...headless.configEcho, unexpected: { value: 'x', source: 'default' } } };
  assert.equal(validateArgs(extraKey).ok, false);
  const malformedEntry = { ...headless, configEcho: { ...headless.configEcho, model_tier: { value: 'optimized', source: 'env', extra: true } } };
  assert.equal(validateArgs(malformedEntry).ok, false);

  const invalidValues = new Map([
    ['model_tier', 'standard'], ['delivery', 'chat,chat'], ['post_mode', 'later'],
    ['pr_comment_cap', '0'], ['delivery_tier', 'branch_only'], ['draft_policy', 'defer'],
    ['reviewed_policy', 'partial'], ['pr_not_found_policy', 'ignore'], ['trivial_scope', 'none'],
  ]);
  const validHeadlessSources = new Map([
    ['model_tier', 'env'], ['delivery', 'review_md'], ['post_mode', 'default'],
    ['pr_comment_cap', 'env'], ['delivery_tier', 'default'], ['draft_policy', 'env'],
    ['reviewed_policy', 'default'], ['pr_not_found_policy', 'env'], ['trivial_scope', 'default'],
  ]);
  const expectedHeadless = ['model_tier', 'delivery', 'post_mode', 'pr_comment_cap', 'delivery_tier', 'draft_policy', 'reviewed_policy', 'pr_not_found_policy', 'trivial_scope'];
  assert.deepEqual([...invalidValues.keys()], expectedHeadless);
  for (const value of ['optimized\n', 'optimized`']) {
    const result = validateArgs({ ...headless, configEcho: { ...headless.configEcho, model_tier: { value, source: 'env' } } });
    assert.equal(result.ok, false);
    assert.ok(result.errors.some((error) => error.includes('single-line and contain no controls or backticks')));
  }

  const interactive = {
    ...good,
    mode: 'interactive',
    limits: { ...good.limits, deliveryCap: 0 },
    configEcho: {
      model_tier: { value: 'optimized', source: 'fixed' },
      pr_comment_cap: { value: '0', source: 'env' },
      delivery_tier: { value: 'all', source: 'default' },
      review_md: { value: 'absent', source: 'discovery' },
    },
  };
  assert.equal(validateArgs(interactive).ok, true);
  const invalidInteractiveValues = new Map([
    ['model_tier', 'standard'], ['pr_comment_cap', 'not-a-cap'],
    ['delivery_tier', 'branch_only'], ['review_md', 'other'],
  ]);
  const expectedInteractive = ['model_tier', 'pr_comment_cap', 'delivery_tier', 'review_md'];
  assert.deepEqual([...invalidInteractiveValues.keys()], expectedInteractive);
  const invalidSources = {
    headless: new Map(expectedHeadless.map((key) => [key, 'fixed'])),
    interactive: new Map([
      ['model_tier', 'env'], ['pr_comment_cap', 'fixed'],
      ['delivery_tier', 'fixed'], ['review_md', 'env'],
    ]),
  };

  // This is deliberately one registry-driven loop for both modes. The hard-coded pins above
  // keep a deleted descriptor from silently shrinking either half of the loop.
  for (const [mode, fixture, invalidByKey, validSources] of [
    ['headless', headless, invalidValues, validHeadlessSources],
    ['interactive', interactive, invalidInteractiveValues, null],
  ]) {
    const expectedKeys = mode === 'headless' ? expectedHeadless : expectedInteractive;
    assert.deepEqual(KNOB_REGISTRY.filter((d) => d.modes.includes(mode)).map((d) => d.key), expectedKeys);
    for (const descriptor of KNOB_REGISTRY.filter((d) => d.modes.includes(mode))) {
      const key = descriptor.key;
      const invalidValue = invalidByKey.get(key);
      const valueLimits = mode === 'headless' && key === 'pr_comment_cap'
        ? { ...fixture.limits, deliveryCap: 0 }
        : fixture.limits;
      const valueResult = validateArgs({
        ...fixture,
        limits: valueLimits,
        configEcho: {
          ...fixture.configEcho,
          [key]: {
            value: invalidValue,
            source: mode === 'headless' ? validSources.get(key) : descriptor.allowedSources.interactive[0],
          },
        },
      });
      assert.equal(valueResult.ok, false, `${mode} ${key}=${invalidValue} must be rejected`);
      assert.ok(valueResult.errors.some((error) => error.includes(`configEcho.${key}.value`)), valueResult.errors.join('; '));

      const badSource = invalidSources[mode].get(key);
      const sourceResult = validateArgs({
        ...fixture,
        configEcho: { ...fixture.configEcho, [key]: { ...fixture.configEcho[key], source: badSource } },
      });
      assert.equal(sourceResult.ok, false, `${mode} ${key} must reject ${badSource} source`);
      assert.ok(sourceResult.errors.some((error) => error.includes(`configEcho.${key}.source`)), sourceResult.errors.join('; '));
    }
  }
  assert.equal(validateArgs({ ...interactive, configEcho: { ...interactive.configEcho, pr_comment_cap: { value: 'null', source: 'env' } } }).ok, false);
  assert.equal(validateArgs({ ...interactive, configEcho: { ...interactive.configEcho, pr_comment_cap: { value: '1', source: 'default' } } }).ok, false);
  assert.equal(validateArgs({ ...interactive, configEcho: { ...interactive.configEcho, model_tier: { value: 'optimized', source: 'env' } } }).ok, false);
  assert.equal(validateArgs({ ...interactive, configEcho: { ...interactive.configEcho, review_md: { value: 'present', source: 'discovery' } } }).ok, false);
  const interactiveInvalid = validateArgs({ ...interactive, configEcho: { ...interactive.configEcho, review_md: { value: 'other', source: 'discovery' } } });
  assert.ok(interactiveInvalid.errors.some((error) => error.includes('configEcho.review_md.value')));
});

test('registry rule metadata has the complete projected row shape', () => {
  const rowKeys = ['key', 'modes', 'allowedSources', 'rule', 'env', 'reviewMdKey', 'defaults', 'type', 'waistPath', 'waistMap', 'derivedFrom', 'deriveWhen', 'nullReceipt', 'resolvedKey'];
  const resolvedKeys = new Map([
    ['model_tier', true], ['delivery', true], ['post_mode', true], ['draft_policy', true],
    ['reviewed_policy', true], ['pr_not_found_policy', true], ['pr_comment_cap', false],
    ['delivery_tier', false], ['trivial_scope', false], ['review_md', false],
  ]);
  for (const descriptor of KNOB_REGISTRY) {
    assert.deepEqual(Object.keys(descriptor), rowKeys);
    assert.equal('example' in descriptor, false);
    assert.equal('valueRule' in descriptor, false);
    assert.equal(typeof descriptor.resolvedKey, 'boolean');
    assert.equal(descriptor.resolvedKey, resolvedKeys.get(descriptor.key));
    assert.deepEqual(
      descriptor.waistMap,
      descriptor.key === 'reviewed_policy' ? { skip: 'full' } : null,
    );
    for (const mode of descriptor.modes) {
      const defaults = descriptor.defaults[mode];
      assert.ok(Array.isArray(defaults), `${descriptor.key} needs ${mode} defaults`);
      const [value, source] = defaults;
      const fixture = mode === 'headless'
        ? {
          ...good,
          mode,
          delivery: { tier: 'all', prIdentity: { owner: 'o', repo: 'r', pr_number: 1, sha_full: 's' } },
          configEcho: {
            model_tier: { value: 'optimized', source: 'default' },
            delivery: { value: 'markdown', source: 'default' },
            post_mode: { value: 'dry-run', source: 'default' },
            pr_comment_cap: { value: '6', source: 'default' },
            delivery_tier: { value: 'all', source: 'default' },
            draft_policy: { value: 'review', source: 'default' },
            reviewed_policy: { value: 'full', source: 'default' },
            pr_not_found_policy: { value: 'error', source: 'default' },
            trivial_scope: { value: 'full', source: 'default' },
          },
          limits: { ...good.limits, deliveryCap: 6 },
        }
        : {
          ...good,
          configEcho: {
            model_tier: { value: 'optimized', source: 'fixed' },
            pr_comment_cap: { value: 'null', source: 'default' },
            delivery_tier: { value: 'all', source: 'default' },
            review_md: { value: 'absent', source: 'discovery' },
          },
          limits: { ...good.limits, deliveryCap: null },
        };
      fixture.configEcho = { ...fixture.configEcho, [descriptor.key]: { value, source } };
      if (descriptor.key === 'pr_comment_cap') fixture.limits.deliveryCap = value === 'null' ? null : Number(value);
      if (descriptor.key === 'delivery_tier') fixture.delivery = { tier: value };
      assert.equal(validateArgs(fixture).ok, true, `${mode} ${descriptor.key} default value`);
      assert.ok(descriptor.allowedSources[mode].includes(source), `${mode} ${descriptor.key} default source`);
    }
  }
});

test('registry waist maps use valid source keys and typed destination domains', () => {
  const waistDomains = {
    'delivery.tier': ['all', 'main_only'],
    scopeAnswer: ['light', 'full'],
    'reviewScope.requested': ['full', 'incremental'],
  };
  for (const descriptor of KNOB_REGISTRY.filter(({ waistMap }) => waistMap !== null)) {
    const domain = waistDomains[descriptor.waistPath];
    assert.ok(domain, `${descriptor.key} needs a hand-typed waist domain`);
    for (const mode of descriptor.modes) {
      const rule = descriptor.rule.kind ? descriptor.rule : descriptor.rule[mode];
      for (const key of Object.keys(descriptor.waistMap)) {
        assert.equal(matchesRule(descriptor.rule, key, mode), true, `${descriptor.key} maps invalid ${key}`);
      }
      for (const sourceValue of rule.values || []) {
        const mappedValue = Object.hasOwn(descriptor.waistMap, sourceValue)
          ? descriptor.waistMap[sourceValue]
          : sourceValue;
        assert.ok(domain.includes(mappedValue), `${descriptor.key} maps ${sourceValue} outside ${descriptor.waistPath}`);
      }
    }
  }
});

test('T2: the light eligibility predicate, description, and validation arms share one limit', () => {
  assert.match(DERIVE_WHEN.lightEligible.describe, new RegExp(String(LIGHT_SCOPE_MAX_CHANGED_LINES)));
  assert.equal(
    DERIVE_WHEN.lightEligible.describe,
    'every changed file is low risk and fewer than 50 lines changed',
  );
  assert.equal(
    computeLightEligible([{ path: 'a', risk: 'low' }], LIGHT_SCOPE_MAX_CHANGED_LINES - 1),
    true,
  );
  assert.equal(
    computeLightEligible([{ path: 'a', risk: 'low' }], LIGHT_SCOPE_MAX_CHANGED_LINES),
    false,
  );

  const lightAnswer = validateArgs({
    ...good,
    changedLines: LIGHT_SCOPE_MAX_CHANGED_LINES,
    riskTable: [{ path: 'a.js', risk: 'low' }],
    scopeAnswer: 'light',
  });
  assert.ok(lightAnswer.errors.some((error) => error.includes(`changedLines >= ${LIGHT_SCOPE_MAX_CHANGED_LINES}`)));

  const headless = headlessArgs();
  headless.riskTable = [{ path: 'a.js', risk: 'medium' }];
  headless.configEcho.trivial_scope = { value: 'light', source: 'default' };
  const receiptAnswer = validateArgs(headless);
  assert.ok(receiptAnswer.errors.some((error) => error.includes(`changedLines >= ${LIGHT_SCOPE_MAX_CHANGED_LINES}`)));
});

test('T5: derivation tables and registry names are bidirectionally complete', () => {
  const deriveNames = new Set(
    KNOB_REGISTRY.filter(({ deriveWhen }) => deriveWhen !== null).map(({ deriveWhen }) => deriveWhen),
  );
  const derivedNames = new Set(
    KNOB_REGISTRY.filter(({ derivedFrom }) => derivedFrom !== null).map(({ derivedFrom }) => derivedFrom),
  );
  assert.deepEqual(deriveNames, new Set(Object.keys(DERIVE_WHEN)));
  assert.deepEqual(derivedNames, new Set(Object.keys(DERIVED_FROM)));
  for (const [name, entry] of Object.entries(DERIVE_WHEN)) {
    assert.equal(typeof entry.holds, 'function', name);
    assert.equal(typeof entry.describe, 'string', name);
    assert.ok(entry.describe.trim(), name);
  }
  for (const [name, entry] of Object.entries(DERIVED_FROM)) {
    assert.equal(typeof entry.fill, 'function', name);
    assert.equal(typeof entry.source, 'string', name);
    assert.equal(typeof entry.describe, 'string', name);
    assert.ok(entry.describe.trim(), name);
  }
  assert.equal(
    KNOB_REGISTRY.some(({ derivedFrom, waistPath }) => derivedFrom !== null && waistPath !== null),
    false,
  );
});

test('matchesRule evaluates every rule kind and fails closed for lures', () => {
  const enumRule = { kind: 'enum', values: ['optimized'] };
  const csvRule = { kind: 'csv_subset', values: ['chat', 'markdown'] };
  const positiveRule = { kind: 'positive_digits' };
  const digitsRule = { kind: 'digits_or_null' };
  const cases = [
    [enumRule, 'optimized', 'headless', true],
    [enumRule, 'Optimized', 'headless', false],
    [csvRule, '', 'headless', false],
    [csvRule, 'chat,markdown', 'headless', true],
    [csvRule, 'chat,,markdown', 'headless', false],
    [csvRule, 'chat,chat', 'headless', false],
    [csvRule, 'chat, markdown', 'headless', false],
    [csvRule, 'Chat', 'headless', false],
    [positiveRule, '1', 'headless', true],
    [positiveRule, '0', 'headless', false],
    [positiveRule, '00', 'headless', false],
    [positiveRule, '01', 'headless', false],
    [positiveRule, '-1', 'headless', false],
    [positiveRule, ' 5', 'headless', false],
    [positiveRule, '5 ', 'headless', false],
    [positiveRule, '١', 'headless', false],
    [positiveRule, '9007199254740992', 'headless', false],
    [positiveRule, '9007199254740991', 'headless', true],
    [digitsRule, '0', 'interactive', true],
    [digitsRule, 'null', 'interactive', true],
    [digitsRule, '00', 'interactive', false],
    [digitsRule, '01', 'interactive', false],
    [digitsRule, '-1', 'interactive', false],
    [digitsRule, ' 5', 'interactive', false],
    [digitsRule, '5 ', 'interactive', false],
    [digitsRule, '١', 'interactive', false],
    [digitsRule, '9007199254740991', 'interactive', true],
    [digitsRule, '9007199254740992', 'interactive', false],
    [{ kind: 'bogus' }, 'x', 'headless', false],
    [{ headless: enumRule }, 'optimized', 'interactive', false],
  ];
  for (const [rule, value, mode, expected] of cases) {
    assert.equal(matchesRule(rule, value, mode), expected, JSON.stringify({ rule, value, mode }));
  }
  for (const value of [null, 1, false, [], {}]) {
    assert.equal(matchesRule(enumRule, value, 'headless'), false, JSON.stringify(value));
  }
});

test('T305-ARGS: registry rule interpreter is exact, canonical, and fail-closed', () => {
  const valid = (mode, key, value, overrides = {}) => {
    const fixture = mode === 'headless'
      ? {
        ...good,
        mode,
        delivery: { tier: 'all', prIdentity: { owner: 'o', repo: 'r', pr_number: 1, sha_full: 's' } },
        configEcho: {
          model_tier: { value: 'optimized', source: 'default' },
          delivery: { value: 'markdown', source: 'default' },
          post_mode: { value: 'dry-run', source: 'default' },
          pr_comment_cap: { value: '6', source: 'default' },
          delivery_tier: { value: 'all', source: 'default' },
          draft_policy: { value: 'review', source: 'default' },
          reviewed_policy: { value: 'full', source: 'default' },
          pr_not_found_policy: { value: 'error', source: 'default' },
          trivial_scope: { value: 'full', source: 'default' },
        },
        limits: { ...good.limits, deliveryCap: 6 },
      }
      : {
        ...good,
        configEcho: {
          model_tier: { value: 'optimized', source: 'fixed' },
          pr_comment_cap: { value: 'null', source: 'default' },
          delivery_tier: { value: 'all', source: 'default' },
          review_md: { value: 'absent', source: 'discovery' },
        },
        limits: { ...good.limits, deliveryCap: null },
      };
    fixture.configEcho = { ...fixture.configEcho, [key]: { ...fixture.configEcho[key], value } };
    if (key === 'pr_comment_cap' && ['0', '6', '9007199254740991'].includes(value)) {
      fixture.limits.deliveryCap = Number(value);
    }
    if (key === 'pr_comment_cap' && value === 'null') fixture.limits.deliveryCap = null;
    if (key === 'delivery_tier' && ['all', 'main_only'].includes(value)) fixture.delivery = { tier: value };
    return validateArgs({ ...fixture, ...overrides });
  };

  assert.equal(valid('headless', 'model_tier', 'optimized').ok, true);
  assert.equal(valid('headless', 'model_tier', 'Optimized').ok, false);
  assert.equal(valid('headless', 'delivery', 'chat,markdown').ok, true);
  for (const lure of ['', 'chat,,markdown', 'chat,chat', 'chat, markdown', 'Chat']) {
    assert.equal(valid('headless', 'delivery', lure).ok, false, `csv lure ${JSON.stringify(lure)}`);
  }
  for (const lure of ['0', '00', '01', '-1', ' 5', '5 ', '9007199254740992', '١']) {
    assert.equal(valid('headless', 'pr_comment_cap', lure).ok, false, `positive lure ${JSON.stringify(lure)}`);
  }
  assert.equal(valid('headless', 'pr_comment_cap', '9007199254740991').ok, true);
  for (const lure of ['00', '01', '-1', ' 5', '5 ', '9007199254740992', '١']) {
    assert.equal(valid('interactive', 'pr_comment_cap', lure).ok, false, `cap lure ${JSON.stringify(lure)}`);
  }
  assert.equal(valid('interactive', 'pr_comment_cap', '0').ok, true);
  assert.equal(valid('interactive', 'pr_comment_cap', '9007199254740991').ok, true);
  assert.equal(valid('interactive', 'pr_comment_cap', 'null').ok, true);
  assert.equal(valid('headless', 'post_mode', 'bogus').ok, false);
});

test('T305-ARGS: normalizeArgs derives typed waist fields from the receipt once', () => {
  const interactive = {
    ...good,
    configEcho: {
      model_tier: { value: 'optimized', source: 'fixed' },
      pr_comment_cap: { value: '7', source: 'env' },
      delivery_tier: { value: 'main_only', source: 'env' },
    },
  };
  const normalizedInteractive = normalizeArgs(interactive);
  assert.deepEqual(normalizedInteractive.limits.deliveryCap, 7);
  assert.deepEqual(normalizedInteractive.delivery, { tier: 'main_only' });
  assert.deepEqual(normalizedInteractive.configEcho.review_md, { value: 'absent', source: 'discovery' });
  assert.deepEqual(validateArgs(normalizedInteractive), { ok: true, errors: [] });

  const headless = {
    ...good,
    mode: 'headless',
    riskTable: [{ path: 'a.js', risk: 'low' }],
    configEcho: {
      model_tier: { value: 'optimized', source: 'default' },
      delivery: { value: 'markdown', source: 'default' },
      post_mode: { value: 'dry-run', source: 'default' },
      pr_comment_cap: { value: '6', source: 'default' },
      delivery_tier: { value: 'all', source: 'default' },
      draft_policy: { value: 'review', source: 'default' },
      reviewed_policy: { value: 'full', source: 'default' },
      pr_not_found_policy: { value: 'error', source: 'default' },
      trivial_scope: { value: 'light', source: 'default' },
    },
  };
  const normalizedHeadless = normalizeArgs(headless);
  assert.equal(normalizedHeadless.limits.deliveryCap, 6);
  assert.deepEqual(normalizedHeadless.delivery, { tier: 'all' });
  assert.equal(normalizedHeadless.scopeAnswer, 'light');
  assert.deepEqual(validateArgs(normalizedHeadless), { ok: true, errors: [] });
  const fullHeadless = normalizeArgs({
    ...headless,
    configEcho: { ...headless.configEcho, trivial_scope: { value: 'full', source: 'default' } },
  });
  assert.equal(fullHeadless.scopeAnswer, 'full');
  assert.deepEqual(validateArgs(fullHeadless), { ok: true, errors: [] });

  const scopeDescriptor = KNOB_REGISTRY.find(({ key }) => key === 'trivial_scope');
  const originalDeriveWhen = scopeDescriptor.deriveWhen;
  const ineligibleHeadless = {
    ...headless,
    riskTable: [{ path: 'a.js', risk: 'medium' }],
  };
  try {
    scopeDescriptor.deriveWhen = null;
    assert.equal(normalizeArgs(ineligibleHeadless).scopeAnswer, 'light');
    scopeDescriptor.deriveWhen = originalDeriveWhen;
    assert.equal(normalizeArgs(ineligibleHeadless).scopeAnswer, undefined);
  } finally {
    scopeDescriptor.deriveWhen = originalDeriveWhen;
  }

  const stamped = normalizeArgs({
    ...interactive,
    limits: { ...interactive.limits, deliveryCap: 3 },
    delivery: { tier: 'all' },
  });
  assert.equal(stamped.limits.deliveryCap, 3);
  assert.equal(stamped.delivery.tier, 'all');
  assert.equal(interactive.delivery, undefined);
  assert.equal(interactive.limits.deliveryCap, undefined);
});

test('T309-ARGS: headless PR review scope derives requested through the registry map', () => {
  const cases = [
    ['full', { kind: 'full', since: null, commits: null }, 'full'],
    ['incremental', { kind: 'incremental', since: 'abc123', commits: null }, 'incremental'],
    ['skip', { kind: 'full', since: null, commits: null }, 'full'],
  ];
  for (const [policy, scope, expected] of cases) {
    const input = headlessArgs(policy, scope);
    delete input.reviewScope.requested;
    const before = JSON.parse(JSON.stringify(input));
    const normalized = normalizeArgs(input);
    assert.equal(normalized.reviewScope.requested, expected, policy);
    assert.deepEqual(validateArgs(normalized), { ok: true, errors: [] }, policy);
    assert.deepEqual(input, before, `${policy} normalization must not mutate the caller object`);
    assert.equal(isPriorReviewDetector(input), true);
  }
});

test('T309-ARGS: review policy waistMap drives derivation and caller lockstep together', () => {
  const descriptor = KNOB_REGISTRY.find(({ key }) => key === 'reviewed_policy');
  const original = descriptor.waistMap;
  try {
    descriptor.waistMap = { skip: 'incremental' };
    const omitted = headlessArgs('skip', { kind: 'full', since: null, commits: null });
    delete omitted.reviewScope.requested;
    assert.equal(normalizeArgs(omitted).reviewScope.requested, 'incremental');
    const stamped = headlessArgs('skip', { requested: 'incremental', kind: 'full', since: null, commits: null });
    assert.deepEqual(validateArgs(stamped), { ok: true, errors: [] });
  } finally {
    descriptor.waistMap = original;
  }
});

test('T309-ARGS: interactive review policy receipts never derive reviewScope.requested', () => {
  const input = {
    ...good,
    configEcho: {
      ...good.configEcho,
      reviewed_policy: { value: 'incremental', source: 'env' },
    },
    reviewScope: {
      kind: 'full', since: null, commits: null, detector: priorReviewDetector,
    },
  };
  assert.equal(normalizeArgs(input).reviewScope.requested, undefined);
  assert.equal(isPriorReviewDetector(input), true);

  const descriptor = KNOB_REGISTRY.find(({ key }) => key === 'reviewed_policy');
  const originalModes = descriptor.modes;
  const originalSources = descriptor.allowedSources;
  try {
    descriptor.modes = [...originalModes, 'interactive'];
    descriptor.allowedSources = {
      ...originalSources,
      interactive: ['env', 'default'],
    };
    assert.equal(normalizeArgs(input).reviewScope.requested, 'incremental');
  } finally {
    descriptor.modes = originalModes;
    descriptor.allowedSources = originalSources;
  }
});

test('T309-ARGS: a stamped requested value survives and reports only the detector-backed lockstep mismatch', () => {
  const args = headlessArgs('full', {
    requested: 'incremental', kind: 'full', since: null, commits: null,
  });
  const normalized = normalizeArgs(args);
  assert.equal(normalized.reviewScope.requested, 'incremental');
  assert.deepEqual(validateArgs(normalized).errors, [
    'reviewScope.requested does not match configEcho.reviewed_policy',
  ]);
});

test('T309-ARGS: required reviewScope is not synthesized by receipt derivation', () => {
  const input = headlessArgs('full');
  delete input.reviewScope;
  // The priorReviewDetector gate also blocks this derivation.
  // Whole-mechanism mutation: remove the REQUIRED-root guard and deriveWhen gate together.
  const normalized = normalizeArgs(input);
  assert.equal(normalized.reviewScope, undefined);
  assert.deepEqual(validateArgs(normalized).errors, [
    'missing required field: reviewScope',
  ]);
});

test('T309-ARGS: required limits is not synthesized by receipt derivation', () => {
  const input = headlessArgs('full');
  delete input.limits;
  // Mutation: remove the REQUIRED-root guard alone.
  const normalized = normalizeArgs(input);
  assert.equal(normalized.limits, undefined);
  assert.deepEqual(validateArgs(normalized).errors, [
    'missing required field: limits',
  ]);
});

test('T309-ARGS: malformed reviewed policy receipt derives nothing and reports the requested enum error', () => {
  const input = headlessArgs('bogus', { kind: 'full', since: null, commits: null });
  delete input.reviewScope.requested;
  const normalized = normalizeArgs(input);
  assert.equal(normalized.reviewScope.requested, undefined);
  const result = validateArgs(normalized);
  assert.ok(result.errors.includes('reviewScope.requested must be full or incremental'));
});

test('T4: unknown deriveWhen names fail closed without throwing', () => {
  const descriptors = ['nonsense', 'toString'].map((deriveWhen, index) => ({
    key: `unknown_${index}`,
    modes: ['headless'],
    allowedSources: { headless: ['default'] },
    rule: { kind: 'enum', values: ['value'] },
    env: null,
    reviewMdKey: null,
    defaults: { headless: ['value', 'default'] },
    type: 'string',
    waistPath: `unknown_${index}.field`,
    waistMap: null,
    derivedFrom: null,
    deriveWhen,
    nullReceipt: [],
    resolvedKey: false,
  }));
  const input = headlessArgs();
  for (const descriptor of descriptors) {
    input.configEcho[descriptor.key] = { value: 'value', source: 'default' };
  }
  const originalLength = KNOB_REGISTRY.length;
  try {
    KNOB_REGISTRY.push(...descriptors);
    const unstampedInput = headlessArgs();
    for (const descriptor of descriptors) {
      unstampedInput.configEcho[descriptor.key] = { value: 'value', source: 'default' };
    }
    let unstampedReport;
    assert.doesNotThrow(() => {
      unstampedReport = normalizeArgsReport(unstampedInput);
    });
    // Mutation: bypass the unknown-name guard in deriveConfigWaist only. Neither
    // unknown deriveWhen name may derive its waist path from a receipt.
    for (const descriptor of descriptors) {
      const root = descriptor.waistPath.split('.')[0];
      assert.equal(unstampedReport.args[root], undefined, descriptor.deriveWhen);
      assert.equal(unstampedReport.derivedPaths.includes(descriptor.waistPath), false, descriptor.deriveWhen);
    }
    for (const descriptor of descriptors) {
      input[descriptor.waistPath.split('.')[0]] = { field: 'value' };
    }
    const normalized = normalizeArgsReport(input).args;
    for (const descriptor of descriptors) {
      assert.deepEqual(normalized[descriptor.waistPath.split('.')[0]], { field: 'value' }, descriptor.deriveWhen);
    }
    // Mutation: remove the Object.hasOwn() half of deriveWhenHolds. Both an unknown
    // own name and inherited Object.prototype.toString must still be accepted here.
    assert.deepEqual(validateArgs(normalized), { ok: true, errors: [] });
  } finally {
    KNOB_REGISTRY.length = originalLength;
  }
});

test('T1: a newly registered waist row is validated against its mapped receipt', () => {
  const originalLength = KNOB_REGISTRY.length;
  try {
    KNOB_REGISTRY.push({
      key: 'lockstep_probe', modes: ['headless'], allowedSources: { headless: ['default'] },
      rule: { kind: 'enum', values: ['raw'] }, env: null, reviewMdKey: null,
      defaults: { headless: ['raw', 'default'] }, type: 'string', waistPath: 'probeWaist.field',
      waistMap: { raw: 'mapped' }, derivedFrom: null, deriveWhen: null, nullReceipt: [],
      resolvedKey: false,
    });
    const base = headlessArgs();
    base.configEcho.lockstep_probe = { value: 'raw', source: 'default' };
    assert.deepEqual(
      validateArgs({ ...base, probeWaist: { field: 'raw' } }).errors,
      ['probeWaist.field does not match configEcho.lockstep_probe'],
    );
    // Mutation: delete the generic pass, or compare entry.value without applying waistMap.
    assert.deepEqual(validateArgs({ ...base, probeWaist: { field: 'mapped' } }), { ok: true, errors: [] });
  } finally {
    KNOB_REGISTRY.length = originalLength;
  }
});

test('T2: every real waist row and mode has one exact lockstep oracle pair', () => {
  const cases = {
    'headless:limits.deliveryCap': {
      path: 'limits.deliveryCap', key: 'pr_comment_cap', receipt: '6',
      agree: headlessArgs(),
      disagree: { ...headlessArgs(), limits: { ...headlessArgs().limits, deliveryCap: 7 } },
    },
    'headless:delivery.tier': {
      path: 'delivery.tier', key: 'delivery_tier', receipt: 'all',
      agree: { ...headlessArgs(), delivery: { tier: 'all' } },
      disagree: { ...headlessArgs(), delivery: { tier: 'main_only' } },
    },
    'headless:reviewScope.requested': {
      path: 'reviewScope.requested', key: 'reviewed_policy', receipt: 'full',
      agree: headlessArgs('full'),
      disagree: headlessArgs('full', { requested: 'incremental' }),
    },
    'headless:scopeAnswer': {
      path: 'scopeAnswer', key: 'trivial_scope', receipt: 'light',
      agree: {
        ...headlessArgs(), riskTable: [{ path: 'a.js', risk: 'low' }], changedLines: 1,
        scopeAnswer: 'light',
        configEcho: { ...headlessArgs().configEcho, trivial_scope: { value: 'light', source: 'default' } },
      },
      disagree: {
        ...headlessArgs(), riskTable: [{ path: 'a.js', risk: 'low' }], changedLines: 1,
        scopeAnswer: 'full',
        configEcho: { ...headlessArgs().configEcho, trivial_scope: { value: 'light', source: 'default' } },
      },
    },
    'interactive:limits.deliveryCap': {
      path: 'limits.deliveryCap', key: 'pr_comment_cap', receipt: 'null',
      agree: { ...good, limits: { ...good.limits, deliveryCap: null } },
      disagree: { ...good, limits: { ...good.limits, deliveryCap: 1 } },
    },
    'interactive:delivery.tier': {
      path: 'delivery.tier', key: 'delivery_tier', receipt: 'all',
      agree: { ...good, delivery: { tier: 'all' } },
      disagree: { ...good, delivery: { tier: 'main_only' } },
    },
  };
  const registryPairs = KNOB_REGISTRY
    .filter(({ waistPath }) => waistPath !== null)
    .flatMap((descriptor) => descriptor.modes.map((mode) => `${mode}:${descriptor.waistPath}`));
  assert.deepEqual(Object.keys(cases).sort(), registryPairs.sort());
  for (const [pair, fixture] of Object.entries(cases)) {
    assert.equal(fixture.agree.configEcho[fixture.key].value, fixture.receipt, pair);
    assert.equal(fixture.disagree.configEcho[fixture.key].value, fixture.receipt, pair);
    assert.deepEqual(validateArgs(fixture.agree).errors, [], `${pair} agreeing value`);
    // Mutation: filter any registry row out of the pass, or retain an old equality arm.
    assert.deepEqual(validateArgs(fixture.disagree).errors, [
      `${fixture.path} does not match configEcho.${fixture.key}`,
    ], `${pair} disagreeing value`);
  }
});

test('T3: derivedFrom rows reject a receipt that disagrees with their source', () => {
  const originalLength = KNOB_REGISTRY.length;
  try {
    KNOB_REGISTRY.push({
      key: 'derived_probe', modes: ['interactive'],
      allowedSources: { interactive: ['discovery'] },
      rule: { kind: 'enum', values: ['present', 'absent'] }, env: null, reviewMdKey: null,
      defaults: { interactive: ['absent', 'discovery'] }, type: 'string', waistPath: null,
      waistMap: null, derivedFrom: 'reviewConfigPath', deriveWhen: null, nullReceipt: [],
      resolvedKey: false,
    });
    const args = {
      ...good,
      configEcho: { ...good.configEcho, derived_probe: { value: 'present', source: 'discovery' } },
    };
    // Mutation: delete the generic derivedFrom loop.
    assert.deepEqual(validateArgs(args).errors, ['configEcho.derived_probe does not match reviewConfigPath']);
  } finally {
    KNOB_REGISTRY.length = originalLength;
  }
});

test('T5: an unhandled registry type is accepted without a false lockstep violation', () => {
  const originalLength = KNOB_REGISTRY.length;
  try {
    KNOB_REGISTRY.push({
      key: 'bool_probe', modes: ['headless'], allowedSources: { headless: ['default'] },
      rule: { kind: 'enum', values: ['true'] }, env: null, reviewMdKey: null,
      defaults: { headless: ['true', 'default'] }, type: 'bool', waistPath: 'boolProbe.value',
      waistMap: null, derivedFrom: null, deriveWhen: null, nullReceipt: [], resolvedKey: false,
    });
    const args = headlessArgs();
    args.configEcho.bool_probe = { value: 'true', source: 'default' };
    args.boolProbe = { value: false };
    // Mutation: remove the expected === undefined guard.
    assert.deepEqual(validateArgs(args), { ok: true, errors: [] });
  } finally {
    KNOB_REGISTRY.length = originalLength;
  }
});

test('T6: csv_list lockstep equality is ordered and element-wise', () => {
  const originalLength = KNOB_REGISTRY.length;
  try {
    KNOB_REGISTRY.push({
      key: 'csv_probe', modes: ['headless'], allowedSources: { headless: ['default'] },
      rule: { kind: 'csv_subset', values: ['a', 'b'] }, env: null, reviewMdKey: null,
      defaults: { headless: ['a,b', 'default'] }, type: 'csv_list', waistPath: 'csvProbe.items',
      waistMap: null, derivedFrom: null, deriveWhen: null, nullReceipt: [], resolvedKey: false,
    });
    const base = headlessArgs();
    base.configEcho.csv_probe = { value: 'a,b', source: 'default' };
    for (const [value, expected] of [[['a', 'b'], true], [['b', 'a'], false], [['a'], false]]) {
      const result = validateArgs({ ...base, csvProbe: { items: value } });
      // Mutation: replace element-wise comparison with === or an always-true branch.
      assert.equal(result.ok, expected, JSON.stringify(value));
      if (!expected) assert.deepEqual(result.errors, ['csvProbe.items does not match configEcho.csv_probe']);
    }
  } finally {
    KNOB_REGISTRY.length = originalLength;
  }
});

test('T7: a stamped null is data and is compared by the generic pass', () => {
  const originalLength = KNOB_REGISTRY.length;
  try {
    KNOB_REGISTRY.push({
      key: 'null_probe', modes: ['headless'], allowedSources: { headless: ['default'] },
      rule: { kind: 'enum', values: ['value'] }, env: null, reviewMdKey: null,
      defaults: { headless: ['value', 'default'] }, type: 'string', waistPath: 'nullProbe.value',
      waistMap: null, derivedFrom: null, deriveWhen: null, nullReceipt: [], resolvedKey: false,
    });
    const args = headlessArgs();
    args.configEcho.null_probe = { value: 'value', source: 'default' };
    args.nullProbe = { value: null };
    // Mutation: change the undefined skip to a nullish skip.
    assert.deepEqual(validateArgs(args).errors, ['nullProbe.value does not match configEcho.null_probe']);
  } finally {
    KNOB_REGISTRY.length = originalLength;
  }
});

test('T8: malformed receipts produce focused errors without lockstep cascades', () => {
  const cap = headlessArgs();
  cap.configEcho.pr_comment_cap = { value: '007', source: 'default' };
  cap.limits = { ...cap.limits, deliveryCap: 1 };
  assert.deepEqual(validateArgs(cap).errors, ['configEcho.pr_comment_cap.value is invalid for headless']);

  const tier = headlessArgs();
  tier.configEcho.delivery_tier = { value: 'all', source: 'fixed' };
  tier.delivery = { tier: 'main_only' };
  // Mutation: read configEchoValue directly instead of receiptEntryFor in the pass.
  assert.deepEqual(validateArgs(tier).errors, ['configEcho.delivery_tier.source is invalid for headless']);
});

test('T9: special cap arms report one fault and suppress the generic template', () => {
  const headless = headlessArgs();
  headless.limits = { ...headless.limits, deliveryCap: '1' };
  const headlessResult = validateArgs(headless);
  // Mutation: restore the every-non-number presence arm; a present string must keep its shape
  // error rather than being rewritten as a receipt-presence error.
  assert.deepEqual(headlessResult.errors, [
    'limits.deliveryCap must be null, absent, or a non-negative safe integer when present',
  ]);

  const interactive = { ...good, limits: { ...good.limits, deliveryCap: null }, configEcho: {
    ...good.configEcho, pr_comment_cap: { value: '5', source: 'default' },
  } };
  const interactiveResult = validateArgs(interactive);
  // Mutation: remove the interactive absence arm or let it fall through to equality.
  assert.deepEqual(interactiveResult.errors, ['configEcho.pr_comment_cap must be null when limits.deliveryCap is absent or null']);
});

test('T10: a mode twin is not lockstep-validated outside its declared modes', () => {
  const originalLength = KNOB_REGISTRY.length;
  try {
    KNOB_REGISTRY.push({
      key: 'mode_twin_probe', modes: ['headless'],
      allowedSources: { headless: ['default'], interactive: ['default'] },
      rule: { kind: 'enum', values: ['value'] }, env: null, reviewMdKey: null,
      defaults: { headless: ['value', 'default'], interactive: ['value', 'default'] },
      type: 'string', waistPath: 'modeTwinProbe.value', waistMap: null, derivedFrom: null,
      deriveWhen: null, nullReceipt: [], resolvedKey: false,
    });
    const args = { ...good, configEcho: { ...good.configEcho, mode_twin_probe: { value: 'value', source: 'default' } }, modeTwinProbe: { value: 'wrong' } };
    const result = validateArgs(args);
    // Mutation: removing descriptor.modes.includes(args.mode) from the lockstep pass turns it red.
    assert.equal(result.errors.some((error) => error.includes('configEcho has unexpected key(s): mode_twin_probe')), true);
    assert.equal(result.errors.some((error) => /does not match configEcho/.test(error)), false);
  } finally {
    KNOB_REGISTRY.length = originalLength;
  }
});

test('T11: missing or null configEcho stays a focused validation refusal', () => {
  for (const configEcho of [undefined, null]) {
    const args = { ...good };
    if (configEcho === undefined) delete args.configEcho;
    else args.configEcho = configEcho;
    // Mutation: remove the isPlainObject(args.configEcho) guard around the pass.
    assert.doesNotThrow(() => validateArgs(args));
    assert.equal(validateArgs(args).errors.some((error) => error.includes('configEcho')), true);
  }
});

test('T12: any present scopeAnswer is refused when the light gate was ineligible', () => {
  // Mutation: revert the biconditional to the old light-only and missing-only checks.
  const interactive = validateArgs({ ...good, scopeAnswer: 'full' });
  assert.equal(interactive.errors.filter((error) => /scopeAnswer/.test(error)).length, 1);
  assert.match(interactive.errors[interactive.errors.length - 1], /the orchestrator answered a light\/full question the gate never asked/);
  assert.match(interactive.errors[interactive.errors.length - 1], /omit scopeAnswer/);

  const headless = headlessArgs();
  headless.scopeAnswer = 'full';
  const result = validateArgs(headless);
  assert.equal(result.errors.filter((error) => /scopeAnswer/.test(error)).length, 1);
  assert.match(result.errors.find((error) => /scopeAnswer is/.test(error)), /the orchestrator answered a light\/full question the gate never asked/);
});

test('T13: eligible scope accepts both values and a headless receipt derives either value', () => {
  for (const scopeAnswer of ['light', 'full']) {
    // Mutation: implement the biconditional as scopeAnswer === "light" iff eligible.
    assert.deepEqual(validateArgs({ ...good, riskTable: [{ path: 'a.js', risk: 'low' }], changedLines: 10, scopeAnswer }), { ok: true, errors: [] });
  }
  const derived = normalizeArgs({
    ...headlessArgs(), riskTable: [{ path: 'a.js', risk: 'low' }], changedLines: 10,
    configEcho: { ...headlessArgs().configEcho, trivial_scope: { value: 'full', source: 'default' } },
  });
  assert.deepEqual(validateArgs(derived), { ok: true, errors: [] });
  assert.equal(derived.scopeAnswer, 'full');
});

test('T14: a headless caller-stamped scopeAnswer equal to the derived receipt stays accepted', () => {
  const args = headlessArgs();
  args.riskTable = [{ path: 'a.js', risk: 'low' }];
  args.changedLines = 10;
  args.scopeAnswer = 'light';
  args.configEcho.trivial_scope = { value: 'light', source: 'default' };
  // Mutation: add caller-provenance rejection even when the stamped value equals the receipt.
  // Decision pin: caller provenance is intentionally not rejected when the value is correct.
  assert.deepEqual(validateArgs(normalizeArgs(args)), { ok: true, errors: [] });
});

test('T15: ineligible light receipt plus a present scopeAnswer uses the two dedicated refusals', () => {
  const args = headlessArgs();
  args.riskTable = [{ path: 'a.js', risk: 'medium' }];
  args.scopeAnswer = 'full';
  args.configEcho.trivial_scope = { value: 'light', source: 'default' };
  const result = validateArgs(args);
  assert.ok(result.errors.some((error) => error.startsWith('scopeAnswer is "full"')));
  const receiptError = result.errors.find((error) => error.startsWith('configEcho.trivial_scope is "light"'));
  assert.ok(receiptError);
  // Mutation: append the scopeAnswer fix to the receipt arm, which has no waist operand.
  assert.doesNotMatch(receiptError, /omit scopeAnswer/);
  // Mutation: retain the deleted scopeAnswer equality arm; no generic equality is valid here.
  assert.equal(result.errors.some((error) => error.includes('scopeAnswer does not match configEcho')), false);
});

test('T16: derived null scopeAnswer is disclosed as no gap after normalization', () => {
  const input = headlessArgs();
  input.riskTable = [{ path: 'a.js', risk: 'low' }];
  input.changedLines = 10;
  input.scopeAnswer = null;
  input.configEcho.trivial_scope = { value: 'light', source: 'default' };
  const report = normalizeArgsReport(input);
  assert.equal(report.args.scopeAnswer, 'light');
  assert.deepEqual(report.derivedPaths.sort(), ['delivery.tier', 'scopeAnswer']);
  // Mutation: omit derivedPaths from nullToleranceRejectedKeys so the re-derived null is disclosed.
  assert.deepEqual(nullToleranceRejectedKeys(report.args, report.dropped, report.derivedPaths), []);
});

test('T18: direct special-arm fixtures isolate absence rules from generic equality', () => {
  const interactiveCap = {
    ...good,
    limits: { ...good.limits },
    configEcho: { ...good.configEcho, pr_comment_cap: { value: '5', source: 'default' } },
  };
  assert.deepEqual(validateArgs(interactiveCap).errors, [
    'configEcho.pr_comment_cap must be null when limits.deliveryCap is absent or null',
  ]);

  for (const delivery of [undefined, {}]) {
    const args = { ...good, configEcho: { ...good.configEcho, delivery_tier: { value: 'main_only', source: 'default' } } };
    if (delivery === undefined) delete args.delivery;
    else args.delivery = delivery;
    assert.deepEqual(validateArgs(args).errors, ['configEcho.delivery_tier does not match delivery.tier']);
  }

  const headlessCap = headlessArgs();
  delete headlessCap.limits.deliveryCap;
  // Mutation: delete the named presence arms; the generic pass skips undefined and misses these.
  assert.deepEqual(validateArgs(headlessCap).errors, [
    'headless limits.deliveryCap must be a number matching configEcho.pr_comment_cap',
  ]);
});

test('T309-ARGS: local and branch scopes stay stamped while detector-backed lockstep is gated', () => {
  for (const policy of ['full', 'incremental', 'skip']) {
    const stamped = headlessArgs(policy, {
      requested: 'full', kind: 'full', since: null, commits: null, detector: null,
    });
    assert.deepEqual(validateArgs(normalizeArgs(stamped)), { ok: true, errors: [] }, policy);

    const omitted = headlessArgs(policy, {
      kind: 'full', since: null, commits: null, detector: null,
    });
    delete omitted.reviewScope.requested;
    const normalized = normalizeArgs(omitted);
    assert.equal(normalized.reviewScope.requested, undefined, policy);
    assert.ok(validateArgs(normalized).errors.includes('reviewScope.requested must be full or incremental'), policy);
  }
});

test('T11: headless local or branch scope does not derive requested', () => {
  const input = headlessArgs('full', {
    requested: undefined,
    kind: 'full',
    since: null,
    commits: null,
    detector: null,
  });
  delete input.reviewScope.requested;
  const normalized = normalizeArgs(input);
  assert.equal(normalized.reviewScope.requested, undefined);
  assert.ok(validateArgs(normalized).errors.includes('reviewScope.requested must be full or incremental'));
});

function dottedFixtureValue(object, path) {
  return path.split('.').reduce((current, part) => current?.[part], object);
}

function deleteDottedFixtureValue(object, path) {
  const parts = path.split('.');
  const leaf = parts.pop();
  const parent = parts.reduce((current, part) => current?.[part], object);
  if (parent) delete parent[leaf];
}

function derivedWaistFixture(mode) {
  if (mode === 'headless') {
    const input = headlessArgs('skip');
    input.riskTable = [{ path: 'a.js', risk: 'low' }];
    input.changedLines = 1;
    input.configEcho.trivial_scope = { value: 'light', source: 'default' };
    delete input.limits;
    input.delivery = {};
    delete input.reviewScope.requested;
    const requiredRoots = new Set(REQUIRED);
    for (const descriptor of KNOB_REGISTRY.filter(({ waistPath }) => waistPath !== null)) {
      const root = descriptor.waistPath.split('.')[0];
      if (descriptor.modes.includes(mode) && requiredRoots.has(root) && input[root] === undefined) {
        input[root] = {};
      }
    }
    return input;
  }
  const input = structuredClone(good);
  input.configEcho = {
    ...input.configEcho,
    pr_comment_cap: { value: '7', source: 'env' },
    delivery_tier: { value: 'main_only', source: 'env' },
  };
  delete input.limits;
  const requiredRoots = new Set(REQUIRED);
  for (const descriptor of KNOB_REGISTRY.filter(({ waistPath }) => waistPath !== null)) {
    const root = descriptor.waistPath.split('.')[0];
    if (descriptor.modes.includes(mode) && requiredRoots.has(root) && input[root] === undefined) {
      input[root] = {};
    }
  }
  return input;
}

test('T10: every waist row derives in each declared mode and not in its mode twin', () => {
  for (const descriptor of KNOB_REGISTRY.filter(({ waistPath }) => waistPath !== null)) {
    for (const mode of descriptor.modes) {
      const input = derivedWaistFixture(mode);
      deleteDottedFixtureValue(input, descriptor.waistPath);
      const normalized = normalizeArgs(input);
      assert.notEqual(dottedFixtureValue(normalized, descriptor.waistPath), undefined, `${descriptor.key}/${mode}`);
    }
  }

  const originalLength = KNOB_REGISTRY.length;
  try {
    KNOB_REGISTRY.push({
      key: 'twin_probe',
      modes: ['headless'],
      allowedSources: { headless: ['default'], interactive: ['default'] },
      rule: { kind: 'enum', values: ['v'] },
      env: null,
      reviewMdKey: null,
      defaults: { headless: ['v', 'default'], interactive: ['v', 'default'] },
      type: 'string',
      waistPath: 'twinProbe',
      waistMap: null,
      derivedFrom: null,
      deriveWhen: null,
      nullReceipt: [],
      resolvedKey: false,
    });
    const twin = derivedWaistFixture('interactive');
    twin.configEcho.twin_probe = { value: 'v', source: 'default' };
    assert.equal(normalizeArgs(twin).twinProbe, undefined);
  } finally {
    KNOB_REGISTRY.length = originalLength;
  }
});

test('T305-ARGS: derivation does not synthesize a missing required limits root', () => {
  const input = { ...good };
  delete input.limits;
  const normalized = normalizeArgs(input);
  assert.equal('limits' in normalized, false);
  assert.equal(validateArgs(normalized).ok, false);
});

test('T305-ARGS: an ineligible light receipt is rejected and is not derived', () => {
  const input = {
    ...good,
    mode: 'headless',
    configEcho: {
      model_tier: { value: 'optimized', source: 'default' },
      delivery: { value: 'markdown', source: 'default' },
      post_mode: { value: 'dry-run', source: 'default' },
      pr_comment_cap: { value: '6', source: 'default' },
      delivery_tier: { value: 'all', source: 'default' },
      draft_policy: { value: 'review', source: 'default' },
      reviewed_policy: { value: 'full', source: 'default' },
      pr_not_found_policy: { value: 'error', source: 'default' },
      trivial_scope: { value: 'light', source: 'default' },
    },
  };
  const normalized = normalizeArgs(input);
  assert.equal(normalized.limits.deliveryCap, 6);
  assert.deepEqual(normalized.delivery, { tier: 'all' });
  assert.equal(normalized.scopeAnswer, undefined);
  const result = validateArgs(normalized);
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.includes('configEcho.trivial_scope')));
});

test('T182-ARGS: receipt lockstep rejects mismatches before any stage can dispatch', () => {
  const deliveryMismatch = validateArgs({ ...good, delivery: { tier: 'main_only' } });
  assert.equal(deliveryMismatch.ok, false);
  assert.ok(deliveryMismatch.errors.some((error) => error.includes('delivery_tier')));

  const capMismatch = validateArgs({ ...good, limits: { ...good.limits, deliveryCap: 24 } });
  assert.equal(capMismatch.ok, false);
  assert.ok(capMismatch.errors.some((error) => error.includes('pr_comment_cap')));

  const nullDeletedDelivery = normalizeArgs({ ...good, delivery: null });
  assert.equal(validateArgs(nullDeletedDelivery).ok, true, 'null delivery uses effective tier all');
  assert.equal(validateArgs({ ...nullDeletedDelivery, configEcho: { ...good.configEcho, delivery_tier: { value: 'main_only', source: 'default' } } }).ok, false);

  const scopeMismatch = validateArgs({
    ...good,
    mode: 'headless',
    riskTable: [{ path: 'a.js', risk: 'low' }],
    scopeAnswer: 'light',
    configEcho: { ...good.configEcho, trivial_scope: { value: 'full', source: 'default' } },
  });
  assert.ok(scopeMismatch.errors.some((error) => error.includes('scopeAnswer')));

  const noIdentity = validateArgs({
    ...good,
    mode: 'headless',
    delivery: { tier: 'all' },
    limits: { ...good.limits, deliveryCap: 25 },
    configEcho: {
      model_tier: { value: 'optimized', source: 'default' },
      delivery: { value: 'pr_comments', source: 'env' },
      post_mode: { value: 'dry-run', source: 'default' },
      pr_comment_cap: { value: '25', source: 'default' },
      delivery_tier: { value: 'all', source: 'default' },
      draft_policy: { value: 'review', source: 'default' },
      reviewed_policy: { value: 'full', source: 'default' },
      pr_not_found_policy: { value: 'error', source: 'default' },
      trivial_scope: { value: 'full', source: 'default' },
    },
  });
  assert.ok(noIdentity.errors.some((error) => error.includes('prIdentity')));
});

test('T182-ARGS: pluginRoot enforces path trust and script coherence', () => {
  for (const bad of ['relative/plugin', '/plugin\nroot', '/plugin`root', '/plugin/../other']) {
    const result = validateArgs({ ...good, pluginRoot: bad });
    assert.equal(result.ok, false, `${bad} must be rejected`);
    assert.ok(result.errors.some((error) => error.includes('pluginRoot')));
  }
  for (const bad of [null, [], 42, '', '   ']) {
    const result = validateArgs({ ...good, pluginRoot: bad });
    assert.equal(result.ok, false, `pluginRoot=${JSON.stringify(bad)} must be rejected`);
    assert.ok(result.errors.some((error) => error.includes('pluginRoot')));
  }
  assert.equal(validateArgs({ ...good, persist: { assembleScriptPath: '/other/scripts/assemble_artifacts.py' } }).ok, false);
  assert.equal(validateArgs({ ...good, verify: { scriptPath: '/other/scripts/verify_findings.py' } }).ok, false);
  assert.equal(validateArgs({ ...good, persist: { assembleScriptPath: '/plugin/scripts/assemble_artifacts.py' }, verify: { scriptPath: '/plugin/scripts/verify_findings.py' } }).ok, true);
  assert.equal(validateArgs({ ...good, verify: { scriptPath: '/plugin/scripts/../verify_findings.py' } }).ok, false);
  assert.equal(validateArgs({ ...good, pluginRoot: '/plugin/', persist: { assembleScriptPath: '/plugin/scripts/assemble_artifacts.py' }, verify: { scriptPath: '/plugin/scripts/verify_findings.py' } }).ok, true);
  assert.equal(validateArgs({ ...good, persist: { assembleScriptPath: '/plugin/scripts/nested/assemble_artifacts.py' } }).ok, false);

});

test('T182-ARGS: incremental reviewScope accepts only a safe detector-backed scope', () => {
  const detector = { previously_reviewed: true, sha_resolvable: true, head_advanced: true, sha_is_ancestor: true, incremental_safe: true, error: null };
  const incremental = { requested: 'incremental', kind: 'incremental', since: 'abc-1._x', commits: null, detector };
  assert.equal(validateArgs({ ...good, reviewScope: incremental }).ok, true);
  assert.equal(validateArgs({ ...good, reviewScope: { ...incremental, since: 'bad sha!' } }).ok, false);
  assert.ok(validateArgs({ ...good, reviewScope: { ...incremental, since: 'bad sha!' } }).errors.some((error) => error.includes('reviewScope.since')));
});

test('T182-ARGS: reviewScope requested/kind and incremental detector safety stay in lockstep', () => {
  const detector = { previously_reviewed: true, sha_resolvable: true, head_advanced: true, sha_is_ancestor: true, incremental_safe: true, error: null };
  const incremental = { requested: 'incremental', kind: 'incremental', since: 'abc-1._x', commits: null, detector };
  assert.equal(validateArgs({ ...good, reviewScope: incremental }).ok, true);
  const mismatch = validateArgs({ ...good, reviewScope: { ...incremental, requested: 'full' } });
  assert.equal(mismatch.ok, false);
  assert.ok(mismatch.errors.some((error) => error.includes('kind incremental requires requested incremental')));

  const unsafe = validateArgs({ ...good, reviewScope: { ...incremental, detector: { ...detector, incremental_safe: false } } });
  assert.equal(unsafe.ok, false);
  assert.ok(unsafe.errors.some((error) => error.includes('detector.incremental_safe true')));

  const missing = validateArgs({ ...good, reviewScope: { ...incremental, detector: null } });
  assert.equal(missing.ok, false);
  assert.ok(missing.errors.some((error) => error.includes('detector.incremental_safe true')));
});

test('T182-ARGS: reviewScope incremental commits validation is standalone', () => {
  const detector = { previously_reviewed: true, sha_resolvable: true, head_advanced: true, sha_is_ancestor: true, incremental_safe: true, error: null };
  const result = validateArgs({ ...good, reviewScope: { requested: 'incremental', kind: 'incremental', since: 'abc', commits: -1, detector } });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.includes('reviewScope.commits')));
});

test('T182-ARGS: reviewScope kind enum validation is standalone', () => {
  const result = validateArgs({ ...good, reviewScope: { requested: 'full', kind: 'bogus', since: null, commits: null, detector: null } });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.includes('reviewScope.kind')));
});

test('T182-ARGS: reviewScope full since validation is standalone', () => {
  const result = validateArgs({ ...good, reviewScope: { requested: 'full', kind: 'full', since: 'abc', commits: null, detector: null } });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.includes('reviewScope.since')));
});

test('T182-ARGS: reviewScope full commits validation is standalone', () => {
  const result = validateArgs({ ...good, reviewScope: { requested: 'full', kind: 'full', since: null, commits: 1, detector: null } });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.includes('reviewScope.commits')));
});

test('T182-ARGS: reviewScope requested enum and fallback detector requirement are standalone', () => {
  const badRequested = validateArgs({ ...good, reviewScope: { requested: 'skip', kind: 'full', since: null, commits: null, detector: null } });
  assert.ok(badRequested.errors.some((error) => error.includes('reviewScope.requested')));
  const missingDetector = validateArgs({ ...good, reviewScope: { requested: 'incremental', kind: 'full', since: null, commits: null, detector: null } });
  assert.ok(missingDetector.errors.some((error) => error.includes('fallback requires detector')));
});

test('T182-ARGS: reviewScope non-object shapes are rejected independently', () => {
  for (const bad of [null, [], 'scope']) {
    const result = validateArgs({ ...good, reviewScope: bad });
    assert.equal(result.ok, false, JSON.stringify(bad));
    assert.ok(result.errors.some((error) => error.includes('reviewScope must be')));
  }
});

test('T182-ARGS: reviewScope detector shape is strict and independently checked', () => {
  const base = { requested: 'full', kind: 'full', since: null, commits: null };
  const cases = [
    ['scalar', 1, 'must be null or an object'],
    ['array', [], 'must be null or an object'],
    ['missing key', { previously_reviewed: false }, 'is missing key'],
    ['wrong boolean', { previously_reviewed: 'no', sha_resolvable: false, head_advanced: false, sha_is_ancestor: false, incremental_safe: false, error: null }, 'previously_reviewed must be a boolean'],
    ['extra key', { previously_reviewed: false, sha_resolvable: false, head_advanced: false, sha_is_ancestor: false, incremental_safe: false, error: null, extra: true }, 'unexpected key'],
    ['bad error', { previously_reviewed: false, sha_resolvable: false, head_advanced: false, sha_is_ancestor: false, incremental_safe: false, error: 2 }, 'error must be a string or null'],
    ['unsafe error', { previously_reviewed: false, sha_resolvable: false, head_advanced: false, sha_is_ancestor: false, incremental_safe: false, error: 'line\nbreak' }, 'error must be single-line'],
  ];
  for (const [label, detector, message] of cases) {
    const result = validateArgs({ ...good, reviewScope: { ...base, detector } });
    assert.equal(result.ok, false, label);
    assert.ok(result.errors.some((error) => error.includes(message)), `${label}: ${result.errors.join('; ')}`);
  }
});

test('T182-ARGS: reviewScope extra keys are rejected independently', () => {
  const detector = { previously_reviewed: false, sha_resolvable: false, head_advanced: false, sha_is_ancestor: false, incremental_safe: false, error: null };
  const result = validateArgs({ ...good, reviewScope: { requested: 'full', kind: 'full', since: null, commits: null, detector, reason: 'old free-text fallback reason' } });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.includes('reviewScope has unexpected key')));
});

test('T182-ARGS: headless review policy and reviewScope requested value stay in lockstep', () => {
  const headless = {
    ...good,
    mode: 'headless',
    configEcho: {
      model_tier: { value: 'optimized', source: 'default' }, delivery: { value: 'markdown', source: 'default' },
      post_mode: { value: 'dry-run', source: 'default' }, pr_comment_cap: { value: '1', source: 'default' },
      delivery_tier: { value: 'all', source: 'default' }, draft_policy: { value: 'review', source: 'default' },
      reviewed_policy: { value: 'incremental', source: 'default' }, pr_not_found_policy: { value: 'error', source: 'default' },
      trivial_scope: { value: 'full', source: 'default' },
    },
    limits: { ...good.limits, deliveryCap: 1 },
    delivery: {},
    reviewScope: { requested: 'full', kind: 'full', since: null, commits: null, detector: priorReviewDetector },
  };
  const result = validateArgs(headless);
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.includes('reviewScope.requested')));
});

test('T182-ARGS: headless deliveryCap must be numeric for the receipt', () => {
  const base = {
    ...good,
    mode: 'headless',
    delivery: {},
    configEcho: {
      model_tier: { value: 'optimized', source: 'default' }, delivery: { value: 'markdown', source: 'default' },
      post_mode: { value: 'dry-run', source: 'default' }, pr_comment_cap: { value: '1', source: 'default' },
      delivery_tier: { value: 'all', source: 'default' }, draft_policy: { value: 'review', source: 'default' },
      reviewed_policy: { value: 'full', source: 'default' }, pr_not_found_policy: { value: 'error', source: 'default' },
      trivial_scope: { value: 'full', source: 'default' },
    },
  };
  for (const [label, deliveryCap] of [['absent', undefined], ['null', null], ['non-number', '1']]) {
    const limits = { ...base.limits };
    if (label !== 'absent') limits.deliveryCap = deliveryCap;
    const result = validateArgs({ ...base, limits });
    assert.equal(result.ok, false, label);
    // Mutation: restore the every-non-number presence arm; the string case must retain only
    // its focused shape error.
    const expected = label === 'non-number'
      ? 'limits.deliveryCap must be null, absent, or a non-negative safe integer when present'
      : 'headless limits.deliveryCap must be a number matching configEcho.pr_comment_cap';
    assert.deepEqual(result.errors, [expected], label);
  }
});

test('T182-ARGS: interactive null deliveryCap lockstep has its own branch', () => {
  const args = {
    ...good,
    limits: { ...good.limits, deliveryCap: null },
    configEcho: { ...good.configEcho, pr_comment_cap: { value: '0', source: 'env' } },
  };
  const result = validateArgs(args);
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.includes('must be null when limits.deliveryCap is absent or null')));
});

test('T182-ARGS: policy.tier is checked when present but omitted fixture policy remains valid', () => {
  assert.equal(validateArgs(good).ok, true);
  assert.equal(validateArgs({ ...good, policy: { tier: 'standard' } }).ok, false);
  assert.equal(validateArgs({ ...good, policy: { tier: 'optimized' } }).ok, true);
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
  assert.deepEqual(validateArgs({ ...good, delivery: { tier: 'main_only' }, configEcho: { ...good.configEcho, delivery_tier: { value: 'main_only', source: 'default' } } }), { ok: true, errors: [] });
});
test('validateArgs accepts an empty delivery object (tier defaults to all downstream)', () => {
  assert.deepEqual(validateArgs({ ...good, delivery: {} }), { ok: true, errors: [] });
});
// Bedrock live failure (2026-08-11 transcript): a typo'd provider would silently flip the
// registry between the full-ID-pin arm and the bare-alias arm, so unknown spellings fail
// loud at the waist. null and absent both mean 'firstParty' (older waists omit the field).
test('validateArgs accepts every known policy.provider and tolerates null/absent', () => {
  for (const provider of ['firstParty', 'bedrock', 'vertex', 'foundry']) {
    assert.deepEqual(validateArgs({ ...good, policy: { ...good.policy, provider } }), { ok: true, errors: [] });
  }
  assert.deepEqual(validateArgs({ ...good, policy: { ...good.policy, provider: null } }), { ok: true, errors: [] });
  assert.deepEqual(validateArgs(good), { ok: true, errors: [] }); // absent
});
test('validateArgs rejects an unknown policy.provider', () => {
  const r = validateArgs({ ...good, policy: { ...good.policy, provider: 'aws' } });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /invalid policy\.provider: aws/);
  // 'gateway' is deliberately NOT a provider: an LLM gateway proxies the Anthropic API and
  // expects standard Claude model names, so gateway sessions stay firstParty (bugbot #179).
  assert.equal(validateArgs({ ...good, policy: { ...good.policy, provider: 'gateway' } }).ok, false);
});
// issue #218: policy.gateway gates registry.js's conditionalSchemaActive. A non-boolean
// (e.g. the string "true") must fail loud at the waist rather than silently coerce.
test('validateArgs accepts policy.gateway true/false and tolerates null/absent', () => {
  assert.deepEqual(validateArgs({ ...good, policy: { ...good.policy, gateway: true } }), { ok: true, errors: [] });
  assert.deepEqual(validateArgs({ ...good, policy: { ...good.policy, gateway: false } }), { ok: true, errors: [] });
  assert.deepEqual(validateArgs({ ...good, policy: { ...good.policy, gateway: null } }), { ok: true, errors: [] });
  assert.deepEqual(validateArgs(good), { ok: true, errors: [] }); // absent
});
test('validateArgs rejects a non-boolean policy.gateway', () => {
  const r = validateArgs({ ...good, policy: { ...good.policy, gateway: 'true' } });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /invalid policy\.gateway: true/);
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
test('T-ARGS: prIdentity.title is optional, shape-checked, and null-stripped without caller mutation', () => {
  const id = { owner: 'o', repo: 'r', pr_number: 310, sha_full: 'deadbeefcafe' };
  assert.deepEqual(
    validateArgs({ ...good, delivery: { prIdentity: { ...id, title: 'A useful title' } } }),
    { ok: true, errors: [] },
  );
  assert.deepEqual(
    validateArgs({ ...good, delivery: { prIdentity: id } }),
    { ok: true, errors: [] },
  );
  for (const title of [5, '  ']) {
    const result = validateArgs({ ...good, delivery: { prIdentity: { ...id, title } } });
    assert.equal(result.ok, false);
    assert.ok(result.errors.includes('delivery.prIdentity.title must be a non-empty string when present'));
  }

  const caller = { ...good, delivery: { prIdentity: { ...id, title: null } } };
  const beforeIdentity = caller.delivery.prIdentity;
  const { args: stripped, dropped } = stripNullOptionalsReport(caller);
  assert.deepEqual(dropped, ['delivery.prIdentity.title']);
  assert.deepEqual(stripped.delivery.prIdentity, id);
  assert.equal(caller.delivery.prIdentity, beforeIdentity);
  assert.equal(caller.delivery.prIdentity.title, null);
  assert.match(nullToleranceGap('delivery.prIdentity.title'), /falls back to owner\/repo#N/);
});
test('T-ARGS-NULL: a two-level null probe is rejected and disclosed', () => {
  assert.deepEqual(
    nullToleranceRejectedKeys(good, ['delivery.prIdentity.title']),
    ['delivery.prIdentity.title'],
  );
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

// Single authority (issue #24 req 4, PR3): agentFlags is retired entirely.
test('validateArgs rejects a caller-supplied agentFlags — single authority', () => {
  const r = validateArgs({ ...good, agentFlags: {} });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /agentFlags is no longer accepted/);
  assert.match(r.errors.join(' '), /riskTable\/scopeAnswer/);
});

// riskTable — the Phase 2e per-file risk classification (issue #24 req 1/2, PR3).
test('validateArgs accepts a well-formed riskTable', () => {
  assert.deepEqual(validateArgs(good), { ok: true, errors: [] });
});
test('validateArgs rejects a non-array riskTable', () => {
  const r = validateArgs({ ...good, riskTable: {} });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /riskTable must be an array/);
});
test('validateArgs rejects an invalid risk literal', () => {
  const r = validateArgs({ ...good, riskTable: [{ path: 'a.js', risk: 'critical' }] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /riskTable\[0\]\.risk must be one of low, medium, high/);
});
test('validateArgs rejects a non-object riskTable entry', () => {
  const r = validateArgs({ ...good, riskTable: ['a.js'] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /riskTable\[0\] must be an object of the form \{path, risk\}/);
});
test('validateArgs rejects a riskTable entry with an empty path', () => {
  const r = validateArgs({ ...good, riskTable: [{ path: '', risk: 'low' }] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /riskTable\[0\]\.path must be a non-empty string/);
});
test('validateArgs rejects a riskTable entry with an extra key', () => {
  const r = validateArgs({ ...good, riskTable: [{ path: 'a.js', risk: 'low', note: 'x' }] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /unexpected key\(s\): note/);
});
test('validateArgs rejects riskTable missing a changed file (path-set mismatch, missing arm)', () => {
  const r = validateArgs({ ...good, changedFiles: ['a.js', 'b.js'], riskTable: [{ path: 'a.js', risk: 'low' }] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /missing risk classification for changed file\(s\): b\.js/);
});
test('validateArgs rejects riskTable with an extra path not in changedFiles (path-set mismatch, extra arm)', () => {
  const r = validateArgs({ ...good, riskTable: [{ path: 'a.js', risk: 'medium' }, { path: 'ghost.js', risk: 'low' }] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /classifies file\(s\) not present in changedFiles: ghost\.js/);
});

// scopeAnswer — the light/full answer, only meaningful alongside a light-eligible riskTable.
test('validateArgs accepts a coherent light scopeAnswer', () => {
  const r = validateArgs({ ...good, riskTable: [{ path: 'a.js', risk: 'low' }], scopeAnswer: 'light' });
  assert.deepEqual(r, { ok: true, errors: [] });
});
test('validateArgs accepts a coherent full scopeAnswer even when eligible', () => {
  const r = validateArgs({ ...good, riskTable: [{ path: 'a.js', risk: 'low' }], scopeAnswer: 'full' });
  assert.deepEqual(r, { ok: true, errors: [] });
});
test('validateArgs rejects an unknown scopeAnswer literal', () => {
  const r = validateArgs({ ...good, scopeAnswer: 'partial' });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /invalid scopeAnswer: partial/);
});
test('validateArgs rejects scopeAnswer "light" when the riskTable is not light-eligible (medium/high present)', () => {
  const r = validateArgs({ ...good, scopeAnswer: 'light' }); // fixture's riskTable is 'medium'
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /gate never asked/);
});
test('validateArgs rejects scopeAnswer "light" when changedLines >= 50 even if every file is low', () => {
  const r = validateArgs({ ...good, changedLines: 50, riskTable: [{ path: 'a.js', risk: 'low' }], scopeAnswer: 'light' });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /gate never asked/);
});
test('validateArgs accepts changedLines exactly 49 as light-eligible (boundary)', () => {
  const r = validateArgs({ ...good, changedLines: 49, riskTable: [{ path: 'a.js', risk: 'low' }], scopeAnswer: 'light' });
  assert.deepEqual(r, { ok: true, errors: [] });
});
test('validateArgs rejects a light-eligible waist with no scopeAnswer (the gate must have asked)', () => {
  const r = validateArgs({ ...good, riskTable: [{ path: 'a.js', risk: 'low' }] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /scopeAnswer is missing/);
});
test('validateArgs accepts a not-eligible waist with no scopeAnswer', () => {
  assert.deepEqual(validateArgs(good), { ok: true, errors: [] }); // fixture riskTable is 'medium'
});
test('validateArgs strips a stamped scopeAnswer: null the same way as its NULLABLE_TOP_LEVEL siblings', () => {
  const { args, dropped } = stripNullOptionalsReport({ ...good, scopeAnswer: null });
  assert.equal(args.scopeAnswer, undefined);
  assert.ok(dropped.includes('scopeAnswer'));
});

// computeLightEligible — the ONE eligibility rule, shared by validateArgs's coherence guard
// and stages.js's deriveAgentFlags.
test('computeLightEligible: true iff every entry is low AND changedLines < 50', () => {
  assert.equal(computeLightEligible([{ path: 'a', risk: 'low' }], 49), true);
  assert.equal(computeLightEligible([{ path: 'a', risk: 'low' }], 50), false);
  assert.equal(computeLightEligible([{ path: 'a', risk: 'medium' }], 1), false);
  assert.equal(computeLightEligible([{ path: 'a', risk: 'low' }, { path: 'b', risk: 'high' }], 1), false);
  assert.equal(computeLightEligible([], 1), true); // vacuously true for an empty riskTable
  assert.equal(computeLightEligible(null, 1), false);
  assert.equal(computeLightEligible([{ path: 'a', risk: 'low' }], 'not-a-number'), false);
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
test('stripNullOptionalsReport deletes a null reviewMd/exclusionsText and validateArgs accepts the stripped result', () => {
  const a = { ...good, reviewMd: null, exclusionsText: null };
  const { args: stripped, dropped } = stripNullOptionalsReport(a);
  assert.equal('reviewMd' in stripped, false);
  assert.equal('exclusionsText' in stripped, false);
  assert.deepEqual([...dropped].sort(), ['exclusionsText', 'reviewMd']);
  assert.equal(validateArgs(stripped).ok, true);
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
test('stripNullOptionalsReport passes through non-object input without throwing', () => {
  for (const input of [undefined, null, 'not an object', ['not', 'an', 'object']]) {
    const report = stripNullOptionalsReport(input);
    assert.equal(report.args, input);
    assert.deepEqual(report.dropped, []);
    assert.deepEqual(report.respelled, []);
  }
});

test('normalizeArgs strips stamped nulls on the object-passthrough form so validateArgs accepts them', () => {
  const a = { ...good, reviewConfig: null, exclusionPatterns: null, delivery: null, checkpoints: null };
  const normalized = normalizeArgs(a);
  assert.equal('reviewConfig' in normalized, false);
  assert.equal('exclusionPatterns' in normalized, false);
  assert.deepEqual(normalized.delivery, { tier: 'all' });
  assert.equal('checkpoints' in normalized, false);
  assert.deepEqual(validateArgs(normalized), { ok: true, errors: [] });
});
test('normalizeArgs strips stamped nulls on the JSON-string form too', () => {
  const raw = JSON.stringify({ ...good, reviewConfig: null, delivery: { tier: null, prIdentity: null } });
  const normalized = normalizeArgs(raw);
  assert.equal('reviewConfig' in normalized, false);
  assert.deepEqual(normalized.delivery, { tier: 'all' });
  assert.deepEqual(validateArgs(normalized), { ok: true, errors: [] });
});
test('normalizeArgs leaves a malformed non-null reviewConfig alone for validateArgs to reject loudly', () => {
  const a = { ...good, reviewConfig: { ignore: [{ pattern: 'x', reason: 'y' }] } };
  const r = validateArgs(normalizeArgs(a));
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes('reviewConfig.ignore')));
});
test('normalizeArgs still round-trips a fully well-formed waist unchanged (no over-stripping)', () => {
  const expected = { ...good, delivery: { tier: 'all' }, limits: { ...good.limits, deliveryCap: null } };
  assert.deepEqual(normalizeArgs(good), expected);
  assert.deepEqual(normalizeArgs(JSON.stringify(good)), expected);
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

test('T303-ARGS: interactive JSON null cap is respelled, validated, and reported without mutation', () => {
  const input = {
    ...good,
    limits: { ...good.limits, deliveryCap: null },
    configEcho: { ...good.configEcho, pr_comment_cap: { value: null, source: 'default' } },
  };
  const before = JSON.parse(JSON.stringify(input));
  const normalized = normalizeArgs(input);
  const report = normalizeArgsReport(input);

  assert.equal(normalized.configEcho.pr_comment_cap.value, 'null');
  assert.deepEqual(report.respelled, ['configEcho.pr_comment_cap.value']);
  assert.deepEqual(validateArgs(normalized), { ok: true, errors: [] });
  assert.deepEqual(input, before, 'normalization must not mutate the caller object');
  assert.equal(nullRespellGap(report.respelled[0]).includes('printed token "null"'), true);
});

test('T303-ARGS: null respelling consults the nullReceipt mode allowlist', () => {
  const input = {
    ...good,
    mode: 'Interactive',
    configEcho: { ...good.configEcho, pr_comment_cap: { value: null, source: 'default' } },
  };
  const report = normalizeArgsReport(JSON.stringify(input));

  assert.equal(report.args.configEcho.pr_comment_cap.value, null);
  assert.deepEqual(report.respelled, []);
  const result = validateArgs(report.args);
  assert.equal(result.ok, false);
  assert.ok(result.errors.includes('invalid mode: Interactive'));
});

test('T303-ARGS: an absent receipt value is not respelled', () => {
  const input = {
    ...good,
    configEcho: { ...good.configEcho, pr_comment_cap: { source: 'default' } },
  };
  const report = normalizeArgsReport(input);

  assert.equal('value' in report.args.configEcho.pr_comment_cap, false);
  assert.deepEqual(report.respelled, []);
});

test('T303-ARGS: nullRespellGap keeps its complete operator disclosure', () => {
  assert.equal(
    nullRespellGap('pr_comment_cap'),
    'null_receipt: args.pr_comment_cap arrived as a literal null and was spelled as the printed token "null"; the receipt and the configuration are unchanged; stamp the string "null".',
  );
});

test('T303-ARGS: headless JSON null cap is still refused', () => {
  const headless = {
    ...good,
    mode: 'headless',
    limits: { ...good.limits, deliveryCap: null },
    configEcho: {
      model_tier: { value: 'optimized', source: 'default' },
      delivery: { value: 'markdown', source: 'default' },
      post_mode: { value: 'dry-run', source: 'default' },
      pr_comment_cap: { value: null, source: 'default' },
      delivery_tier: { value: 'all', source: 'default' },
      draft_policy: { value: 'review', source: 'default' },
      reviewed_policy: { value: 'full', source: 'default' },
      pr_not_found_policy: { value: 'error', source: 'default' },
      trivial_scope: { value: 'full', source: 'default' },
    },
  };
  const report = normalizeArgsReport(JSON.stringify(headless));
  assert.deepEqual(report.respelled, []);
  const result = validateArgs(report.args);
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.includes('configEcho.pr_comment_cap.value')));
});

test('T303-ARGS: interactive JSON null on model_tier is still refused', () => {
  const input = {
    ...good,
    configEcho: { ...good.configEcho, model_tier: { value: null, source: 'fixed' } },
  };
  const normalized = normalizeArgs(input);
  assert.equal(normalized.configEcho.model_tier.value, null);
  assert.equal(validateArgs(normalized).ok, false);
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

// --- Issue #24 req 7: LIMIT_DEFAULTS at the args waist ----------------------------------

test('LIMIT_DEFAULTS covers exactly the five benchmarked/bound keys, never deliveryCap/discoveryCap', () => {
  assert.deepEqual(LIMIT_DEFAULTS, {
    summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 25,
    maxLineSpan: 100,
  });
});

test('normalizeArgs fills every absent limits key from LIMIT_DEFAULTS', () => {
  const raw = { ...good, limits: {} };
  const out = normalizeArgs(raw);
  assert.deepEqual(out.limits, { ...LIMIT_DEFAULTS, deliveryCap: null });
});

test('normalizeArgs fills only the MISSING limits keys, leaving every provided value untouched', () => {
  const raw = { ...good, limits: { summarizeBucketSize: 7, deliveryCap: 3 } };
  const out = normalizeArgs(raw);
  assert.deepEqual(out.limits, {
    summarizeBucketSize: 7, deliveryCap: 3,
    validateBatch: 25, challengeCap: 40, verifySliceSize: 25, maxLineSpan: 100,
  });
});

test('normalizeArgs treats a provided challengeCap:0 as a real value, not an absent one to default over', () => {
  const raw = { ...good, limits: { challengeCap: 0 } };
  const out = normalizeArgs(raw);
  assert.equal(out.limits.challengeCap, 0);
});

test('normalizeArgs treats a provided deliveryCap:null as a real value (uncapped), never defaulted', () => {
  const raw = { ...good, limits: { deliveryCap: null } };
  const out = normalizeArgs(raw);
  assert.equal(out.limits.deliveryCap, null);
  assert.ok(!('deliveryCap' in LIMIT_DEFAULTS), 'deliveryCap must never be in LIMIT_DEFAULTS');
});

test('normalizeArgs leaves a non-object limits alone for validateArgs to reject', () => {
  const out = normalizeArgs({ ...good, limits: 'nope' });
  assert.equal(out.limits, 'nope');
});

test('validateArgs accepts a normalized (fully defaulted) limits object', () => {
  const out = normalizeArgs({ ...good, limits: {} });
  assert.deepEqual(validateArgs(out), { ok: true, errors: [] });
});

test('validateArgs accepts limits absent entirely, deliveryCap null, discoveryCap absent', () => {
  const r = validateArgs({ ...good, limits: { ...good.limits, deliveryCap: null } });
  assert.deepEqual(r, { ok: true, errors: [] });
});

test('validateArgs accepts limits.challengeCap: 0 (challenge nothing is a legal cap)', () => {
  const r = validateArgs({ ...good, limits: { ...good.limits, challengeCap: 0 } });
  assert.deepEqual(r, { ok: true, errors: [] });
});

test('validateArgs accepts a positive limits.discoveryCap', () => {
  const r = validateArgs({ ...good, limits: { ...good.limits, discoveryCap: 10 } });
  assert.deepEqual(r, { ok: true, errors: [] });
});

test('validateArgs accepts a positive limits.maxLineSpan (issue #204)', () => {
  const r = validateArgs({ ...good, limits: { ...good.limits, maxLineSpan: 250 } });
  assert.deepEqual(r, { ok: true, errors: [] });
});

test('validateArgs rejects a non-object limits', () => {
  for (const bad of [null, 'x', 5, [], []]) {
    const r = validateArgs({ ...good, limits: bad });
    assert.equal(r.ok, false, `limits=${JSON.stringify(bad)} should be rejected`);
  }
});

test('validateArgs rejects an unknown limits key — the silent-typo case (issue #24)', () => {
  const r = validateArgs({ ...good, limits: { ...good.limits, verifySclieSize: 50 } });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /unknown limits key: verifySclieSize/);
});

test('validateArgs rejects zero/negative/non-integer summarizeBucketSize, validateBatch, verifySliceSize, maxLineSpan', () => {
  for (const key of ['summarizeBucketSize', 'validateBatch', 'verifySliceSize', 'maxLineSpan']) {
    for (const bad of [0, -1, 1.5, 'x', NaN]) {
      const r = validateArgs({ ...good, limits: { ...good.limits, [key]: bad } });
      assert.equal(r.ok, false, `limits.${key}=${bad} should be rejected`);
      assert.match(r.errors.join(' '), new RegExp(`limits\\.${key} must be a positive safe integer`));
    }
  }
});

test('validateArgs rejects a negative or non-integer challengeCap, but accepts 0', () => {
  for (const bad of [-1, 1.5, 'x']) {
    const r = validateArgs({ ...good, limits: { ...good.limits, challengeCap: bad } });
    assert.equal(r.ok, false, `limits.challengeCap=${bad} should be rejected`);
  }
  assert.deepEqual(
    validateArgs({ ...good, limits: { ...good.limits, challengeCap: 0 } }),
    { ok: true, errors: [] },
  );
});

test('validateArgs rejects a negative or non-integer deliveryCap, but accepts null/absent', () => {
  for (const bad of [-1, 1.5, 'x']) {
    const r = validateArgs({ ...good, limits: { ...good.limits, deliveryCap: bad } });
    assert.equal(r.ok, false, `limits.deliveryCap=${bad} should be rejected`);
  }
  assert.deepEqual(
    validateArgs({ ...good, limits: { ...good.limits, deliveryCap: null } }),
    { ok: true, errors: [] },
  );
});

test('validateArgs rejects a zero/negative/non-integer discoveryCap when present, but accepts absence', () => {
  for (const bad of [0, -1, 1.5, 'x']) {
    const r = validateArgs({ ...good, limits: { ...good.limits, discoveryCap: bad } });
    assert.equal(r.ok, false, `limits.discoveryCap=${bad} should be rejected`);
  }
  assert.deepEqual(validateArgs(good), { ok: true, errors: [] });
});

// --- reviewMd / exclusionsText (issue #24 PR2) -------------------------------

test('validateArgs accepts a well-formed reviewMd array', () => {
  const r = validateArgs({ ...good, reviewMd: [{ path: 'REVIEW.md', text: '' }, { path: 'src/REVIEW.md', text: 'ignore:\n  - foo' }] });
  assert.deepEqual(r, { ok: true, errors: [] });
});
test('validateArgs accepts an empty reviewMd array (authoritative "found nothing")', () => {
  assert.deepEqual(validateArgs({ ...good, reviewMd: [] }), { ok: true, errors: [] });
});
test('validateArgs rejects a non-array reviewMd', () => {
  const r = validateArgs({ ...good, reviewMd: { path: 'x', text: '' } });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /reviewMd must be an array/);
});
test('validateArgs rejects a reviewMd entry with an extra key', () => {
  const r = validateArgs({ ...good, reviewMd: [{ path: 'x', text: '', extra: 1 }] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /unexpected key/);
});
test('validateArgs rejects a reviewMd entry missing path or text', () => {
  assert.equal(validateArgs({ ...good, reviewMd: [{ text: 'x' }] }).ok, false);
  assert.equal(validateArgs({ ...good, reviewMd: [{ path: 'x' }] }).ok, false);
});
test('validateArgs rejects an absolute reviewMd path', () => {
  const r = validateArgs({ ...good, reviewMd: [{ path: '/foo/REVIEW.md', text: '' }] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /repo-relative/);
});
test('validateArgs rejects non-string reviewMd text', () => {
  const r = validateArgs({ ...good, reviewMd: [{ path: 'x', text: 123 }] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /text must be a string/);
});
test('validateArgs rejects non-string reviewMd path', () => {
  const r = validateArgs({ ...good, reviewMd: [{ path: 123, text: '' }] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /path must be a non-empty string/);
});
test('validateArgs rejects a reviewMd path with a control character', () => {
  const r = validateArgs({ ...good, reviewMd: [{ path: 'foo\u0000bar', text: '' }] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /control character/);
});
test('validateArgs rejects reviewMd + reviewConfig both present (single authority)', () => {
  const r = validateArgs({ ...good, reviewMd: [], reviewConfig: { ignore: [] } });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /reviewMd and reviewConfig are both present/);
});
test('validateArgs rejects exclusionsText + exclusionPatterns both present (single authority)', () => {
  const r = validateArgs({ ...good, exclusionsText: '', exclusionPatterns: [] });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /exclusionsText and exclusionPatterns are both present/);
});
test('validateArgs accepts exclusionsText alone', () => {
  assert.deepEqual(validateArgs({ ...good, exclusionsText: 'foo.js\n' }), { ok: true, errors: [] });
});
test('validateArgs rejects non-string exclusionsText', () => {
  const r = validateArgs({ ...good, exclusionsText: 42 });
  assert.equal(r.ok, false);
  assert.match(r.errors.join(' '), /exclusionsText must be a string/);
});
test('validateArgs: reviewConfig alone (no reviewMd) still validates cleanly (req 8 backward compat)', () => {
  assert.deepEqual(validateArgs({ ...good, reviewConfig: { ignore: ['x'] } }), { ok: true, errors: [] });
});

function assertReviewPathError(path, phrase) {
  const result = validateArgs({ ...good, reviewMd: [{ path, text: '' }] });
  assert.equal(result.ok, false, path);
  assert.ok(result.errors.some((error) => error.includes(phrase)), `${path}: ${phrase}`);
}

test('validateArgs: reviewMd rejects a backslash path', () => {
  assertReviewPathError('api\\REVIEW.md', 'must use / separators');
});
test('validateArgs: reviewMd rejects a trailing slash', () => {
  assertReviewPathError('api/REVIEW.md/', 'trailing /');
});
test('validateArgs: reviewMd rejects an empty path segment', () => {
  assertReviewPathError('api//REVIEW.md', 'empty path segment');
});
test('validateArgs: reviewMd rejects a dot path segment', () => {
  assertReviewPathError('api/./REVIEW.md', 'must not contain . or ..');
});
test('validateArgs: reviewMd rejects a dot-dot path segment', () => {
  assertReviewPathError('api/../REVIEW.md', 'must not contain . or ..');
});
test('validateArgs: reviewMd rejects a dot-prefix path', () => {
  assertReviewPathError('./REVIEW.md', 'must not contain . or ..');
});
test('validateArgs: reviewMd rejects a wrong basename', () => {
  assertReviewPathError('api/NOT-REVIEW.md', 'must end with exactly REVIEW.md');
});

test('validateArgs: reviewConfig validates threshold domains at root and scope', () => {
  for (const key of ['confidence_threshold', 'security_min_confidence']) {
    for (const value of [-1, 101, 1.5, '80']) {
      const result = validateArgs({ ...good, reviewConfig: { ignore: [], [key]: value } });
      assert.equal(result.ok, false, `${key}=${value}`);
    }
  }
  for (const value of ['urgent', 1]) {
    const result = validateArgs({ ...good, reviewConfig: { ignore: [], severity_threshold: value } });
    assert.equal(result.ok, false, `severity=${value}`);
  }
  const scoped = validateArgs({ ...good, reviewConfig: {
    ignore: [], confidence_threshold: 0, security_min_confidence: 100, severity_threshold: 'critical',
    scopes: [{ dir: 'api', ignore: [], confidence_threshold: 90, security_min_confidence: 80, severity_threshold: 'high' }],
  } });
  assert.deepEqual(scoped, { ok: true, errors: [] });
});

test('validateArgs: reviewConfig rejects an explicitly undefined threshold key', () => {
  const result = validateArgs({ ...good, reviewConfig: { ignore: [], confidence_threshold: undefined } });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.includes('confidence_threshold must be an integer')));
});

function assertScopeError(scope, phrase) {
  const result = validateArgs({ ...good, reviewConfig: { ignore: [], scopes: [scope] } });
  assert.equal(result.ok, false, JSON.stringify(scope));
  assert.ok(result.errors.some((error) => error.includes(phrase)), `${JSON.stringify(scope)}: ${phrase}`);
}

test('validateArgs: reviewConfig scopes require plain objects', () => {
  assertScopeError(null, 'scopes[0] must be a plain object');
  assertScopeError([], 'scopes[0] must be a plain object');
});
test('validateArgs: reviewConfig scopes must be an array', () => {
  const result = validateArgs({ ...good, reviewConfig: { ignore: [], scopes: {} } });
  assert.equal(result.ok, false);
  assert.ok(result.errors.some((error) => error.includes('reviewConfig.scopes must be an array')));
});
test('validateArgs: reviewConfig scope dir is non-empty', () => {
  assertScopeError({ dir: '', ignore: [] }, 'scopes[0].dir must be a non-empty string');
});
test('validateArgs: reviewConfig scope dir has no leading slash', () => {
  assertScopeError({ dir: '/api', ignore: [] }, 'leading or trailing /');
});
test('validateArgs: reviewConfig scope dir has no trailing slash', () => {
  assertScopeError({ dir: 'api/', ignore: [] }, 'leading or trailing /');
});
test('validateArgs: reviewConfig scope dir rejects backslashes', () => {
  assertScopeError({ dir: 'api\\v1', ignore: [] }, 'must use / separators');
});
test('validateArgs: reviewConfig scope dir rejects control characters', () => {
  assertScopeError({ dir: 'api\n', ignore: [] }, 'control character');
});
test('validateArgs: reviewConfig scope dir rejects empty segments', () => {
  assertScopeError({ dir: 'api//v1', ignore: [] }, 'empty path segment');
});
test('validateArgs: reviewConfig scope dir rejects dot segments', () => {
  assertScopeError({ dir: 'api/./v1', ignore: [] }, 'must not contain . or ..');
});
test('validateArgs: reviewConfig scope dir rejects dot-dot segments', () => {
  assertScopeError({ dir: 'api/../v1', ignore: [] }, 'must not contain . or ..');
});
test('validateArgs: reviewConfig scope rejects unknown keys', () => {
  assertScopeError({ dir: 'api', ignore: [], unexpected: true }, 'unexpected key(s)');
});
test('validateArgs: reviewConfig scope rejects an invalid threshold value', () => {
  assertScopeError({ dir: 'api', ignore: [], confidence_threshold: '90' }, 'confidence_threshold must be an integer');
});
test('validateArgs: reviewConfig scope rejects an invalid severity enum', () => {
  assertScopeError({ dir: 'api', ignore: [], severity_threshold: 'urgent' }, 'severity_threshold must be one of');
});
test('validateArgs: reviewConfig scope rejects non-string ignores', () => {
  assertScopeError({ dir: 'api', ignore: [42] }, 'ignore[0] must be a flat pattern string');
});

test('validateArgs: reviewConfig scopes reject duplicate dirs', () => {
  const duplicate = validateArgs({ ...good, reviewConfig: {
    ignore: [], scopes: [{ dir: 'api', ignore: [] }, { dir: 'api', ignore: [] }],
  } });
  assert.equal(duplicate.ok, false);
  assert.ok(duplicate.errors.some((error) => error.includes('scopes[1]')));
});

// --- resolveReviewConfig (issue #24 PR2) -------------------------------------

test('resolveReviewConfig: absent reviewMd/exclusionsText falls back to reviewConfig/exclusionPatterns unchanged (req 8)', () => {
  const A = { reviewConfig: { ignore: ['a'] }, exclusionPatterns: ['b'] };
  assert.deepEqual(resolveReviewConfig(A), {
    reviewConfig: { ignore: ['a'] }, exclusionPatterns: ['b'],
    reviewConfigSource: 'preParsed', exclusionsSource: 'preParsed', reviewMdEntryCount: 0,
  });
});
test('resolveReviewConfig: neither present -> source "none"', () => {
  assert.deepEqual(resolveReviewConfig({}), {
    reviewConfig: {}, exclusionPatterns: [], reviewConfigSource: 'none', exclusionsSource: 'none', reviewMdEntryCount: 0,
  });
});
test('resolveReviewConfig: single reviewMd entry parses correctly', () => {
  const text = '```yaml code-gauntlet\nconfidence_threshold: 80\nignore:\n  - foo.js\n```';
  const out = resolveReviewConfig({ reviewMd: [{ path: 'REVIEW.md', text }] });
  assert.deepEqual(out.reviewConfig, { confidence_threshold: 80, ignore: ['foo.js'] });
  assert.equal(out.reviewConfig.confidence_threshold, 80);
  assert.deepEqual(out.reviewConfig.ignore, ['foo.js']);
  assert.equal(out.reviewConfigSource, 'reviewMd');
  assert.equal(out.reviewMdEntryCount, 1);
});
test('resolveReviewConfig: deeper entry overrides an earlier entry\'s threshold setting', () => {
  const root = '```yaml code-gauntlet\nconfidence_threshold: 70\n```';
  const deep = '```yaml code-gauntlet\nconfidence_threshold: 90\n```';
  const out = resolveReviewConfig({ reviewMd: [{ path: 'REVIEW.md', text: root }, { path: 'src/REVIEW.md', text: deep }] });
  assert.equal(out.reviewConfig.confidence_threshold, 70);
  assert.deepEqual(out.reviewConfig.scopes, [{ dir: 'src', confidence_threshold: 90, ignore: [] }]);
});
test('resolveReviewConfig: ignore lists accumulate across entries, in order', () => {
  const root = '```yaml code-gauntlet\nignore:\n  - a.js\n  - b.js\n```';
  const deep = '```yaml code-gauntlet\nignore:\n  - c.js\n```';
  const out = resolveReviewConfig({ reviewMd: [{ path: 'REVIEW.md', text: root }, { path: 'src/REVIEW.md', text: deep }] });
  assert.deepEqual(out.reviewConfig.ignore, ['a.js', 'b.js']);
  assert.deepEqual(out.reviewConfig.scopes, [{ dir: 'src', ignore: ['c.js'] }]);
});
test('resolveReviewConfig: absent settings stay strictly undefined after merge (no default fill)', () => {
  const out = resolveReviewConfig({ reviewMd: [{ path: 'REVIEW.md', text: 'no config block here' }] });
  assert.equal(out.reviewConfig.confidence_threshold, undefined);
  assert.equal(out.reviewConfig.security_min_confidence, undefined);
  assert.equal(out.reviewConfig.severity_threshold, undefined);
  assert.deepEqual(out.reviewConfig.ignore, []);
});
test('resolveReviewConfig: empty reviewMd array is equivalent to today\'s no-REVIEW.md path', () => {
  const out = resolveReviewConfig({ reviewMd: [] });
  assert.deepEqual(out.reviewConfig, { ignore: [] });
  assert.equal(out.reviewConfigSource, 'reviewMd');
  assert.equal(out.reviewMdEntryCount, 0);
});
test('resolveReviewConfig: exclusionsText is parsed via loadExclusions', () => {
  const out = resolveReviewConfig({ exclusionsText: '- foo.js\n- bar/*.js\n' });
  assert.deepEqual(out.exclusionPatterns, ['foo.js', 'bar/*.js']);
});
test('resolveReviewConfig: deeper entry overrides an earlier entry\'s severity_threshold setting', () => {
  const root = '```yaml code-gauntlet\nseverity_threshold: low\n```';
  const deep = '```yaml code-gauntlet\nseverity_threshold: high\n```';
  const out = resolveReviewConfig({ reviewMd: [{ path: 'REVIEW.md', text: root }, { path: 'src/REVIEW.md', text: deep }] });
  assert.equal(out.reviewConfig.severity_threshold, 'low');
  assert.equal(out.reviewConfig.scopes[0].severity_threshold, 'high');
});
test('resolveReviewConfig: deeper entry overrides an earlier entry\'s security_min_confidence setting', () => {
  const root = '```yaml code-gauntlet\nsecurity_min_confidence: 60\n```';
  const deep = '```yaml code-gauntlet\nsecurity_min_confidence: 85\n```';
  const out = resolveReviewConfig({ reviewMd: [{ path: 'REVIEW.md', text: root }, { path: 'src/REVIEW.md', text: deep }] });
  assert.equal(out.reviewConfig.security_min_confidence, 60);
  assert.equal(out.reviewConfig.scopes[0].security_min_confidence, 85);
});
test('resolveReviewConfig: reviewMd + exclusionsText together — reviewConfig from reviewMd, exclusions actually parsed from exclusionsText', () => {
  const text = '```yaml code-gauntlet\nconfidence_threshold: 80\n```';
  const out = resolveReviewConfig({
    reviewMd: [{ path: 'REVIEW.md', text }],
    exclusionsText: '- foo.js\n- bar/*.js\n',
  });
  assert.equal(out.reviewConfig.confidence_threshold, 80);
  assert.equal(out.reviewConfigSource, 'reviewMd');
  assert.equal(out.exclusionsSource, 'exclusionsText');
  assert.deepEqual(out.exclusionPatterns, ['foo.js', 'bar/*.js']);
});
test('resolveReviewConfig: reviewMd present + exclusionsText absent falls back to legacy exclusionPatterns', () => {
  const text = '```yaml code-gauntlet\nconfidence_threshold: 80\n```';
  const out = resolveReviewConfig({
    reviewMd: [{ path: 'REVIEW.md', text }],
    exclusionPatterns: ['legacy.js'],
  });
  assert.deepEqual(out.exclusionPatterns, ['legacy.js']);
  assert.equal(out.exclusionsSource, 'preParsed');
});
test('resolveReviewConfig: exclusionsText-only (no reviewMd) echoes reviewConfigSource "preParsed" when reviewConfig was also stamped, never "reviewMd"', () => {
  const out = resolveReviewConfig({ exclusionsText: '- foo.js\n', reviewConfig: { confidence_threshold: 70 } });
  assert.equal(out.reviewConfigSource, 'preParsed');
  assert.equal(out.exclusionsSource, 'exclusionsText');
  assert.equal(out.reviewConfig.confidence_threshold, 70);
});
test('resolveReviewConfig: reviewMd scopes are depth-ordered regardless of input order', () => {
  const root = '```yaml code-gauntlet\nconfidence_threshold: 70\n```';
  const deep = '```yaml code-gauntlet\nconfidence_threshold: 90\n```';
  // Deliberately reversed input order (deep before root) — the root and scope remain
  // separate, and the scope array is ordered structurally.
  const out = resolveReviewConfig({ reviewMd: [{ path: 'src/deep/REVIEW.md', text: deep }, { path: 'REVIEW.md', text: root }] });
  assert.equal(out.reviewConfig.confidence_threshold, 70);
  assert.deepEqual(out.reviewConfig.scopes, [{ dir: 'src/deep', confidence_threshold: 90, ignore: [] }]);
});
