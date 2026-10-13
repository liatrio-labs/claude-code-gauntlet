// Owns methodology knob descriptors, receipt validation, and receipt-driven configuration derivation.
import { REQUIRED, DELIVERY_TIERS, SCOPE_ANSWERS, CONTROL_RE, LIGHT_SCOPE_MAX_CHANGED_LINES, KNOB_VOCAB } from './registry.js';
import { dottedValue, isPlainObject, deepFreeze } from './wire.js';

const safeReceiptValue = (value) => typeof value === 'string' && !CONTROL_RE.test(value) && !value.includes('`');

export const KNOB_REGISTRY = deepFreeze([
  { key: 'model_tier', modes: KNOB_VOCAB.modes, allowedSources: { headless: KNOB_VOCAB.envDefault, interactive: KNOB_VOCAB.fixed },
    rule: { kind: 'enum', values: KNOB_VOCAB.modelTiers }, env: 'CODE_GAUNTLET_MODEL_TIER', reviewMdKey: null,
    defaults: { headless: ['optimized', 'default'], interactive: ['optimized', 'fixed'] }, type: 'string', waistPath: null, waistMap: null,
    derivedFrom: null, deriveWhen: null, nullReceipt: [], resolvedKey: true },
  { key: 'delivery', modes: KNOB_VOCAB.headless, allowedSources: { headless: KNOB_VOCAB.envReviewMdDefault }, rule: { kind: 'csv_subset',
    values: KNOB_VOCAB.deliveryChannels }, env: 'CODE_GAUNTLET_DELIVERY', reviewMdKey: 'default_delivery',
    defaults: { headless: ['markdown', 'default'] }, type: 'csv_list', waistPath: null, waistMap: null, derivedFrom: null, deriveWhen: null,
    nullReceipt: [], resolvedKey: true },
  { key: 'post_mode', modes: KNOB_VOCAB.headless, allowedSources: { headless: KNOB_VOCAB.envDefault }, rule: { kind: 'enum',
    values: KNOB_VOCAB.postModes }, env: 'CODE_GAUNTLET_POST_MODE', reviewMdKey: null, defaults: { headless: ['dry-run', 'default'] },
    type: 'string', waistPath: null, waistMap: null, derivedFrom: null, deriveWhen: null, nullReceipt: [], resolvedKey: true },
  { key: 'pr_comment_cap', modes: KNOB_VOCAB.modes, allowedSources: { headless: KNOB_VOCAB.envDefault, interactive: KNOB_VOCAB.envDefault },
    rule: { headless: { kind: 'positive_digits' }, interactive: { kind: 'digits_or_null' } }, env: 'CODE_GAUNTLET_PR_COMMENT_CAP',
    reviewMdKey: null, defaults: { headless: ['6', 'default'], interactive: ['null', 'default'] }, type: 'int_or_null',
    waistPath: 'limits.deliveryCap', waistMap: null, derivedFrom: null, deriveWhen: null, nullReceipt: KNOB_VOCAB.interactive,
    resolvedKey: false },
  { key: 'delivery_tier', modes: KNOB_VOCAB.modes, allowedSources: { headless: KNOB_VOCAB.envDefault, interactive: KNOB_VOCAB.envDefault },
    rule: { kind: 'enum', values: DELIVERY_TIERS }, env: 'CODE_GAUNTLET_DELIVERY_TIER', reviewMdKey: null, defaults: { headless: ['all',
    'default'], interactive: ['all', 'default'] }, type: 'string', waistPath: 'delivery.tier', waistMap: null, derivedFrom: null,
    deriveWhen: null, nullReceipt: [], resolvedKey: false },
  { key: 'draft_policy', modes: KNOB_VOCAB.headless, allowedSources: { headless: KNOB_VOCAB.envDefault }, rule: { kind: 'enum',
    values: KNOB_VOCAB.draftPolicies }, env: 'CODE_GAUNTLET_DRAFT_POLICY', reviewMdKey: null, defaults: { headless: ['review', 'default'] },
    type: 'string', waistPath: null, waistMap: null, derivedFrom: null, deriveWhen: null, nullReceipt: [], resolvedKey: true },
  { key: 'reviewed_policy', modes: KNOB_VOCAB.headless, allowedSources: { headless: KNOB_VOCAB.envDefault }, rule: { kind: 'enum',
    values: KNOB_VOCAB.reviewedPolicies }, env: 'CODE_GAUNTLET_REVIEWED_POLICY', reviewMdKey: null, defaults: { headless: ['full',
    'default'] }, type: 'string', waistPath: 'reviewScope.requested', waistMap: { skip: 'full' }, derivedFrom: null,
    deriveWhen: 'priorReviewDetector', nullReceipt: [], resolvedKey: true },
  { key: 'pr_not_found_policy', modes: KNOB_VOCAB.headless, allowedSources: { headless: KNOB_VOCAB.envDefault }, rule: { kind: 'enum',
    values: KNOB_VOCAB.prNotFoundPolicies }, env: 'CODE_GAUNTLET_PR_NOT_FOUND_POLICY', reviewMdKey: null, defaults: { headless: ['error',
    'default'] }, type: 'string', waistPath: null, waistMap: null, derivedFrom: null, deriveWhen: null, nullReceipt: [],
    resolvedKey: true },
  { key: 'trivial_scope', modes: KNOB_VOCAB.headless, allowedSources: { headless: KNOB_VOCAB.envDefault }, rule: { kind: 'enum',
    values: SCOPE_ANSWERS }, env: 'CODE_GAUNTLET_TRIVIAL_SCOPE', reviewMdKey: null, defaults: { headless: ['full', 'default'] },
    type: 'string', waistPath: 'scopeAnswer', waistMap: null, derivedFrom: null, deriveWhen: 'lightEligible', nullReceipt: [],
    resolvedKey: false },
  { key: 'review_md', modes: KNOB_VOCAB.interactive, allowedSources: { interactive: KNOB_VOCAB.discovery }, rule: { kind: 'enum',
    values: KNOB_VOCAB.reviewMdValues }, env: null, reviewMdKey: null, defaults: { interactive: ['absent', 'discovery'] }, type: 'string',
    waistPath: null, waistMap: null, derivedFrom: 'reviewMd', deriveWhen: null, nullReceipt: [], resolvedKey: false },
]);

export function matchesRule(rule, value, mode) {
  if (typeof value !== 'string' || !isPlainObject(rule)) return false;
  const selected = typeof rule.kind === 'string' ? rule : rule[mode];
  if (!isPlainObject(selected)) return false;
  if (selected.kind === 'enum') return Array.isArray(selected.values) && selected.values.includes(value);
  if (selected.kind === 'csv_subset') {
    if (!Array.isArray(selected.values) || value === '') return false;
    const parts = value.split(',');
    return parts.every((part) => selected.values.includes(part)) && new Set(parts).size === parts.length;
  }
  if (selected.kind === 'positive_digits') {
    return /^[1-9][0-9]*$/.test(value) && Number.isSafeInteger(Number(value));
  }
  if (selected.kind === 'digits_or_null') {
    return value === 'null' || (/^(0|[1-9][0-9]*)$/.test(value) && Number.isSafeInteger(Number(value)));
  }
  return false;
}

// Validation and dispatch share this eligibility predicate so their scope decisions cannot drift.
export function computeLightEligible(riskTable, changedLines) {
  if (!Array.isArray(riskTable)) return false;
  if (typeof changedLines !== 'number' || !(changedLines < LIGHT_SCOPE_MAX_CHANGED_LINES)) return false;
  // An empty diff is eligible: there are no files whose risk would exclude light scope.
  return riskTable.every((entry) => entry && entry.risk === 'low');
}

export function receiptEntryFor(args, descriptor) {
  const mode = args.mode;
  const entry = args.configEcho[descriptor.key];
  if (!isPlainObject(entry) || typeof entry.value !== 'string' || typeof entry.source !== 'string') return null;
  if (!safeReceiptValue(entry.value) || !matchesRule(descriptor.rule, entry.value, mode)) return null;
  if (!descriptor.allowedSources[mode] || !descriptor.allowedSources[mode].includes(entry.source)) return null;
  return entry;
}

export function receiptEntryForKey(args, key, descriptors = KNOB_REGISTRY) {
  const descriptor = descriptors.find((candidate) => candidate.key === key);
  return descriptor ? receiptEntryFor(args, descriptor) : null;
}

function setDottedValue(object, path, value) {
  const parts = path.split('.');
  let current = object;
  for (const part of parts.slice(0, -1)) {
    if (current[part] === undefined) current[part] = {};
    else if (!isPlainObject(current[part])) return false;
    current[part] = { ...current[part] };
    current = current[part];
  }
  const leaf = parts.at(-1);
  current[leaf] = value;
  return true;
}

function typedReceiptValue(descriptor, value) {
  if (descriptor.type === 'string') return value;
  if (descriptor.type === 'csv_list') return value.split(',');
  if (descriptor.type === 'int_or_null') return value === 'null' ? null : Number(value);
  return undefined;
}

export function mappedReceiptValue(descriptor, value) {
  const mapped = descriptor.waistMap !== null
    && Object.hasOwn(descriptor.waistMap, value)
    ? descriptor.waistMap[value]
    : value;
  return typedReceiptValue(descriptor, mapped);
}

function isPriorReviewDetector(args) {
  return isPlainObject(args?.reviewScope?.detector);
}

export const DERIVE_WHEN = deepFreeze({
  lightEligible: {
    holds: (args) => computeLightEligible(args.riskTable, args.changedLines),
    describe: `every changed file is low risk and fewer than ${LIGHT_SCOPE_MAX_CHANGED_LINES} lines changed`,
  },
  priorReviewDetector: {
    holds: isPriorReviewDetector,
    describe: '`reviewScope.detector` is an object',
  },
});

export const DERIVED_FROM = deepFreeze({
  reviewMd: {
    fill: (args) => {
      if (Array.isArray(args.reviewMd)) {
        return args.reviewMd.length > 0 ? 'present' : 'absent';
      }
      return args.reviewConfigPath != null ? 'present' : 'absent';
    },
    source: 'discovery',
    describe: '`present` when the `reviewMd` array is nonempty, else `absent`; without `reviewMd`, ' +
      '`present` when `reviewConfigPath` is set, else `absent`',
  },
});

export function deriveWhenHolds(descriptor, args, gates = DERIVE_WHEN) {
  return descriptor.deriveWhen === null
    || (Object.hasOwn(gates, descriptor.deriveWhen) && gates[descriptor.deriveWhen].holds(args));
}

export function deriveConfigWaist(args, descriptors = KNOB_REGISTRY) {
  if (!isPlainObject(args) || !isPlainObject(args.configEcho) || typeof args.mode !== 'string') {
    return { args, derivedPaths: [] };
  }
  const out = { ...args };
  const configEcho = { ...args.configEcho };
  const derivedPaths = [];
  let configEchoChanged = false;
  for (const descriptor of descriptors) {
    if (!descriptor.modes.includes(args.mode)) continue;
    if (descriptor.derivedFrom !== null
      && Object.hasOwn(DERIVED_FROM, descriptor.derivedFrom)
      && !Object.hasOwn(configEcho, descriptor.key)) {
      const derivation = DERIVED_FROM[descriptor.derivedFrom];
      configEcho[descriptor.key] = {
        value: derivation.fill(args),
        source: derivation.source,
      };
      configEchoChanged = true;
      continue;
    }
    if (!descriptor.waistPath || dottedValue(out, descriptor.waistPath) !== undefined) continue;
    const root = descriptor.waistPath.split('.')[0];
    if (out[root] === undefined && REQUIRED.includes(root)) continue;
    const entry = receiptEntryFor(args, descriptor);
    if (!entry) continue;
    if (!deriveWhenHolds(descriptor, args)) continue;
    const value = mappedReceiptValue(descriptor, entry.value);
    if (value !== undefined && setDottedValue(out, descriptor.waistPath, value)) {
      derivedPaths.push(descriptor.waistPath);
    }
  }
  if (configEchoChanged) out.configEcho = configEcho;
  return { args: out, derivedPaths };
}

export function configEchoValue(args, key) {
  const entry = args && isPlainObject(args.configEcho) ? args.configEcho[key] : undefined;
  return isPlainObject(entry) && typeof entry.value === 'string' ? entry.value : undefined;
}

export function validateConfigEcho(args, errors, table = KNOB_REGISTRY) {
  if (args.configEcho === undefined) return;
  if (!isPlainObject(args.configEcho)) {
    errors.push('configEcho must be an object keyed by methodology knob');
    return;
  }
  const mode = args.mode;
  const descriptors = table.filter((descriptor) => descriptor.modes.includes(mode));
  const expected = descriptors.map((descriptor) => descriptor.key);
  const actual = Object.keys(args.configEcho);
  const extra = actual.filter((key) => !expected.includes(key));
  const missing = expected.filter((key) => !actual.includes(key));
  if (extra.length) errors.push(`configEcho has unexpected key(s): ${extra.join(', ')}`);
  if (missing.length) errors.push(`configEcho is missing key(s): ${missing.join(', ')}`);

  for (const descriptor of descriptors) {
    const entry = args.configEcho[descriptor.key];
    if (!isPlainObject(entry)) {
      errors.push(`configEcho.${descriptor.key} must be { value, source }`);
      continue;
    }
    const entryKeys = Object.keys(entry);
    const entryExtra = entryKeys.filter((key) => !KNOB_VOCAB.receiptEntryKeys.includes(key));
    if (entryExtra.length) errors.push(`configEcho.${descriptor.key} has unexpected key(s): ${entryExtra.join(', ')}`);
    if (typeof entry.value !== 'string') {
      errors.push(`configEcho.${descriptor.key}.value must be a string`);
    } else {
      if (!safeReceiptValue(entry.value)) {
        errors.push(`configEcho.${descriptor.key}.value must be single-line and contain no controls or backticks`);
      }
      if (!matchesRule(descriptor.rule, entry.value, mode)) errors.push(`configEcho.${descriptor.key}.value is invalid for ${mode}`);
    }
    if (typeof entry.source !== 'string' || !descriptor.allowedSources[mode].includes(entry.source)) {
      errors.push(`configEcho.${descriptor.key}.source is invalid for ${mode}`);
    }
  }
}
