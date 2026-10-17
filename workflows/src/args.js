import { normalizeAbsoluteRoot } from './paths.js';
import { REQUIRED, DELIVERY_TIERS, SCOPE_ANSWERS, CONTROL_RE } from './registry.js';
import { LIMIT_DEFAULTS, LIGHT_SCOPE_MAX_CHANGED_LINES } from './registry.js';
import { PR_IDENTITY_FIELDS, POLICY_PROVIDERS, MODEL_ALIASES, TRUSTED_SCRIPT_BASENAMES } from './registry.js';
import { REVIEW_SETTING_KEYS, SEVERITY_ORDER, KNOB_VOCAB } from './registry.js';
import { KNOB_REGISTRY, computeLightEligible, receiptEntryFor, receiptEntryForKey, mappedReceiptValue, DERIVED_FROM } from './knobs.js';
import { deriveWhenHolds, deriveConfigWaist, configEchoValue, validateConfigEcho } from './knobs.js';
import { dottedValue, isPlainObject, deepFreeze, fnv1a32, normalizeReviewText } from './wire.js';
import { buildReviewConfig, loadExclusions } from './reviewConfig.js';

export const ARGS_VERSION = 1;

// The nonce becomes a bare verify-command argument, so whitespace and shell metacharacters are unsafe.
const NONCE_RE = /^[A-Za-z0-9._-]+$/;

// Keep null tolerance narrow: null delivery caps and model overrides carry configuration data.
// Dropped nulls that change validation require disclosure because they can change review outcomes.
const NULLABLE_TOP_LEVEL = deepFreeze(['reviewConfig', 'exclusionPatterns', 'reviewMd', 'exclusionsText', 'delivery', 'checkpoints',
  'persist', 'scopeAnswer']);

const RISK_LEVELS = deepFreeze(['low', 'medium', 'high']);

const NULL_TOLERANCE_CONSEQUENCE = deepFreeze({
  reviewConfig: 'the review runs on the Filter stage built-in thresholds (non-security 55, security 70) with ' +
    'no ignore list, NOT your REVIEW.md configuration, so the delivered findings can differ',
  exclusionPatterns: 'no exclusion patterns are applied, so the delivered findings can differ',
  reviewMd: 'the review runs on the Filter stage built-in thresholds (non-security 55, security 70) with ' +
    'no ignore list, NOT your REVIEW.md configuration, so the delivered findings can differ',
  exclusionsText: 'no exclusion patterns are applied, so the delivered findings can differ',
  delivery: 'delivery falls back to tier "all" with no PR identity',
  'delivery.tier': 'delivery falls back to tier "all", so a narrowing intent is lost',
  'delivery.prIdentity': 'the post-review artifact is persisted as a bare findings array instead of the post_review-ready wrapper',
  'delivery.prIdentity.title': 'the report title falls back to owner/repo#N (or "local changes")',
  checkpoints: 'no resume state is replayed — every phase re-runs from scratch',
  persist: 'the artifact-writer takes the legacy full by-value persist path',
  scopeAnswer: 'the scope gate is treated as never having been asked, so deriveAgentFlags cannot honour a light answer',
});

const CONTEXT_SIZE_MAX = deepFreeze({ contextLines: 5000000, contextChars: 500000000 });

const LIMIT_KEYS = deepFreeze([
  'summarizeBucketSize', 'validateBatch', 'challengeCap', 'verifySliceSize',
  'deliveryCap', 'discoveryCap', 'maxLineSpan',
]);

const ARGS_SHAPES = deepFreeze({
  pathFields: ['repoRoot', 'outputDir', 'headShaShort', 'diffPath', 'pluginRoot'],
  riskFields: ['path', 'risk'],
  reviewMdFields: ['path', 'text', 'checksum'],
  scopeLayerFields: ['dir', 'ignore', ...REVIEW_SETTING_KEYS],
  scopeFields: ['requested', 'kind', 'since', 'commits', 'detector'],
  scopeKinds: ['full', 'incremental'],
  detectorFields: ['previously_reviewed', 'sha_resolvable', 'head_advanced', 'sha_is_ancestor', 'incremental_safe', 'error'],
  scriptFields: [
    { root: 'persist', key: 'assembleScriptPath', script: 'assemble' },
    { root: 'verify', key: 'scriptPath', script: 'verify' },
  ],
});

const ARGS_FIELD_CHECKS = deepFreeze({
  policy: [
    { key: 'provider', presence: 'nonnull', values: POLICY_PROVIDERS,
      message: 'invalid {label}: {value} (expected one of {values})' },
    { key: 'tier', values: KNOB_VOCAB.modelTiers, message: 'invalid {label}: {value} (expected optimized)' },
    { key: 'gateway', presence: 'nonnull', domain: 'boolean', message: 'invalid {label}: {value} (expected a boolean)' },
    { key: 'mappedAliases', presence: 'nonnull', domain: 'aliasList',
      message: `invalid {label}: {value} (expected distinct values from ${MODEL_ALIASES.join(', ')})` },
  ],
  changed: [
    { key: 'changedFiles', domain: 'array', message: 'changedFiles must be an array of repo-relative paths' },
    { key: 'changedLines', domain: 'number', message: 'changedLines must be a number' },
  ],
  context: ['contextLines', 'contextChars'].map((key) => ({
    key, domain: 'safeInteger', minimum: 1, maximum: CONTEXT_SIZE_MAX[key],
    message: '{label} must be a positive safe integer (the measured size of the shared context file) when present',
    boundMessage: '{label} is {value}, above the {maximum} ceiling '
      + '\u2014 that is not a review context, it is a mis-measurement',
  })),
  review: REVIEW_SETTING_KEYS.map((key) => key === 'severity_threshold'
    ? { key, presence: 'in', values: SEVERITY_ORDER, separator: '|',
      message: '{label} must be one of {values} when present (pre-parsed REVIEW.md enum)' }
    : { key, presence: 'in', domain: 'integer', minimum: 0, maximum: 100,
      message: '{label} must be an integer from 0 through 100 when present' }),
  exclusions: [
    { key: 'exclusionsText', domain: 'string', message: 'exclusionsText must be a string when present' },
  ],
  persist: [
    { key: 'assembleScriptPath', domain: 'nonEmptyString',
      message: 'persist.assembleScriptPath must be a non-empty string path to scripts/assemble_artifacts.py' },
    { key: 'returnPrimaries', domain: 'boolean', message: 'persist.returnPrimaries must be a boolean when present' },
  ],
  // Preserve error order independently of the vocabulary order so focused errors precede cap errors.
  limits: [
    ...['summarizeBucketSize', 'validateBatch', 'verifySliceSize', 'maxLineSpan'].map((key) => ({
      key, domain: 'safeInteger', minimum: 1, message: 'limits.{key} must be a positive safe integer when present',
    })),
    { key: 'challengeCap', domain: 'safeInteger', minimum: 0,
      message: 'limits.challengeCap must be a non-negative safe integer when present' },
    { key: 'deliveryCap', presence: 'nonnull', domain: 'safeInteger', minimum: 0,
      message: 'limits.deliveryCap must be null, absent, or a non-negative safe integer when present' },
    { key: 'discoveryCap', domain: 'safeInteger', minimum: 1,
      message: 'limits.discoveryCap must be a positive safe integer when present' },
  ],
});

const FIELD_DOMAINS = deepFreeze({
  aliasList: (value) => Array.isArray(value)
    && value.every((alias) => MODEL_ALIASES.includes(alias)) && new Set(value).size === value.length,
  array: Array.isArray,
  number: (value) => typeof value === 'number',
  boolean: (value) => typeof value === 'boolean',
  string: (value) => typeof value === 'string',
  nonEmptyString: (value) => typeof value === 'string' && value !== '',
  integer: Number.isInteger,
  safeInteger: Number.isSafeInteger,
});

function validateArgsFields(value, group, errors, label = '') {
  const prefix = label ? `${label}.` : '';
  for (const row of ARGS_FIELD_CHECKS[group]) {
    if (row.presence === 'in' && !(row.key in value)) continue;
    const field = value[row.key];
    if (row.presence !== 'in' && field === undefined) continue;
    if (row.presence === 'nonnull' && field === null) continue;
    const domainInvalid = row.domain && !FIELD_DOMAINS[row.domain](field);
    const valuesInvalid = row.values && !row.values.includes(field);
    const minimumInvalid = !domainInvalid && row.minimum !== undefined && field < row.minimum;
    const maximumInvalid = !domainInvalid && row.maximum !== undefined && field > row.maximum;
    if (!domainInvalid && !valuesInvalid && !minimumInvalid && !maximumInvalid) continue;
    const message = maximumInvalid && row.boundMessage
      ? row.boundMessage : row.message;
    const params = {
      key: row.key, label: `${prefix}${row.key}`, value: field, maximum: row.maximum,
      values: row.values?.join(row.separator || ', '),
    };
    errors.push(message.replace(/\{(\w+)\}/g, (_, key) => `${params[key]}`));
  }
}

function validatePrIdentity(id, errors) {
  if (!isPlainObject(id)) {
    const required = PR_IDENTITY_FIELDS.filter((field) => field.required).map((field) => field.name);
    const optional = PR_IDENTITY_FIELDS.filter((field) => !field.required).map((field) => field.name);
    errors.push(`delivery.prIdentity must be an object { ${required.join(', ')}[, ${optional.join(', ')}] } when present`);
    return;
  }
  for (const field of PR_IDENTITY_FIELDS) {
    const value = id[field.name];
    const valid = value !== undefined && field.check(value);
    if ((value === undefined && field.required) || (value !== undefined && !valid)) {
      errors.push(`delivery.prIdentity.${field.name} must be ${field.describe}`);
    }
    if (valid) {
      for (const extra of field.extraChecks || []) {
        if (!extra.check(value)) errors.push(`delivery.prIdentity.${field.name} ${extra.message}`);
      }
    }
  }
}

function validateScriptPaths(args) {
  const errors = [];
  if (typeof args.pluginRoot !== 'string' || !args.pluginRoot.startsWith('/')) return errors;
  const root = args.pluginRoot.replace(/\/+$/, '') || '/';
  const scriptRoot = root === '/' ? '/scripts' : `${root}/scripts`;
  for (const row of ARGS_SHAPES.scriptFields) {
    const member = args[row.root];
    const expected = `${scriptRoot}/${TRUSTED_SCRIPT_BASENAMES[row.script]}`;
    if (isPlainObject(member) && typeof member[row.key] === 'string' && member[row.key] !== expected) {
      errors.push(`${row.root}.${row.key} must equal ${expected}`);
    }
  }
  return errors;
}

function validatePathSegments(value, label, reviewFile = false) {
  const errors = [];
  if (reviewFile) {
    if (value.startsWith('/')) errors.push(`${label} must be repo-relative (must not start with /)`);
    if (value.endsWith('/')) errors.push(`${label} must not have a trailing /`);
  } else if (value.startsWith('/') || value.endsWith('/')) {
    errors.push(`${label} must not have a leading or trailing /`);
  }
  if (value.includes('\\')) errors.push(`${label} must use / separators, not backslashes`);
  if (CONTROL_RE.test(value)) errors.push(`${label} must not contain a control character`);
  const segments = value.split('/');
  if (segments.some((segment) => segment === '')) errors.push(`${label} must not contain an empty path segment`);
  if (segments.some((segment) => segment === '.' || segment === '..')) errors.push(`${label} must not contain . or .. path segments`);
  if (reviewFile && segments[segments.length - 1] !== 'REVIEW.md') {
    errors.push(`${label} must end with exactly REVIEW.md`);
  }
  return errors;
}

function validateFlatPatterns(value, label, errors, ignore = false) {
  if (!Array.isArray(value)) {
    errors.push(`${label} must be an array of flat pattern strings`);
    return;
  }
  for (let i = 0; i < value.length; i++) {
    if (typeof value[i] !== 'string') {
      const suffix = ignore ? ' \u2014 parseReviewMd emits strings, never objects' : '';
      errors.push(`${label}[${i}] must be a flat pattern string (got ${typeof value[i]})${suffix}`);
    }
  }
}

export function nullToleranceGap(key, consequences = NULL_TOLERANCE_CONSEQUENCE) {
  const why = consequences[key] || 'the run proceeds as if the field had not been supplied';
  return `null_arg: args.${key} arrived as a literal null and was treated as ABSENT — ${why}. Omit ` +
    `${key} entirely (or stamp a well-formed value); do not stamp null.`;
}

export function nullRespellGap(key) {
  return `null_receipt: args.${key} arrived as a literal null and was spelled as the printed token ` +
    `"null"; the receipt and the configuration are unchanged; stamp the string "null".`;
}

// Probe validation rather than maintain a second rejection list that could drift.
// Re-derived fields and nulls already accepted by validation do not represent a lost configuration.
export function nullToleranceRejectedKeys(cleanArgs, dropped, derivedPaths = [], descriptors = KNOB_REGISTRY) {
  if (!Array.isArray(dropped) || dropped.length === 0) return [];
  const derived = new Set(Array.isArray(derivedPaths) ? derivedPaths : []);
  const baseline = validateArgs(cleanArgs, descriptors).errors.length;
  return dropped
    .filter((key) => !derived.has(key))
    .filter((key) => validateArgs(withNullAt(cleanArgs, key), descriptors).errors.length > baseline);
}

// Copy each traversed level so probing a dropped null cannot mutate the caller's waist.
function withNullAt(args, key) {
  const base = isPlainObject(args) ? args : {};
  const parts = key.split('.');
  const out = { ...base };
  let source = base;
  let target = out;
  for (const part of parts.slice(0, -1)) {
    const value = isPlainObject(source)
      ? source[part]
      : undefined;
    const copy = isPlainObject(value) ? { ...value } : {};
    target[part] = copy;
    target = copy;
    source = value;
  }
  target[parts.at(-1)] = null;
  return out;
}

export function stripNullOptionalsReport(args, descriptors = KNOB_REGISTRY) {
  if (!isPlainObject(args)) return { args, dropped: [], respelled: [] };
  const dropped = [];
  const respelled = [];
  const out = { ...args };
  for (const k of NULLABLE_TOP_LEVEL) {
    if (out[k] === null) { delete out[k]; dropped.push(k); }
  }
  if (isPlainObject(out.delivery)) {
    const delivery = { ...out.delivery };
    if (delivery.prIdentity === null) { delete delivery.prIdentity; dropped.push('delivery.prIdentity'); }
    if (
      isPlainObject(delivery.prIdentity)
      && delivery.prIdentity.title === null
    ) {
      const id = { ...delivery.prIdentity };
      delete id.title;
      delivery.prIdentity = id;
      dropped.push('delivery.prIdentity.title');
    }
    if (delivery.tier === null) { delete delivery.tier; dropped.push('delivery.tier'); }
    out.delivery = delivery;
  }
  // Only undefined permits a default: zero caps are deliberate values.
  // Leave missing or malformed limits for validation rather than repairing their shape.
  if (isPlainObject(out.limits)) {
    const limits = { ...out.limits };
    for (const [k, v] of Object.entries(LIMIT_DEFAULTS)) {
      if (limits[k] === undefined) limits[k] = v;
    }
    out.limits = limits;
  }
  // Interactive receipts spell null as text; disclose respelling while keeping receipt validation strict.
  if (isPlainObject(out.configEcho) && typeof out.mode === 'string') {
    const configEcho = { ...out.configEcho };
    for (const descriptor of descriptors) {
      if (!descriptor.nullReceipt || !descriptor.nullReceipt.includes(out.mode)) continue;
      const entry = configEcho[descriptor.key];
      if (isPlainObject(entry) && entry.value === null) {
        configEcho[descriptor.key] = { ...entry, value: 'null' };
        respelled.push(`configEcho.${descriptor.key}.value`);
      }
    }
    out.configEcho = configEcho;
  }
  return { args: out, dropped, respelled };
}

// Strip nulls before receipt derivation so newly absent fields can be filled and their drops tracked.
export function normalizeArgsReport(raw, descriptors = KNOB_REGISTRY) {
  const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw;
  const report = stripNullOptionalsReport(parsed, descriptors);
  const { args, derivedPaths } = deriveConfigWaist(report.args, descriptors);
  report.args = args;
  report.derivedPaths = derivedPaths;
  return report;
}

export function normalizeArgs(raw, descriptors = KNOB_REGISTRY) {
  return normalizeArgsReport(raw, descriptors).args;
}

function extraKeys(value, allowed, sorted = false) {
  const keys = Object.keys(value).filter((key) => !allowed.includes(key));
  return sorted ? keys.sort() : keys;
}

export function validateArgs(args, descriptors = KNOB_REGISTRY) {
  const errors = [];
  if (!args || typeof args !== 'object') return { ok: false, errors: ['args is not an object'] };
  if (args.argsVersion !== ARGS_VERSION) errors.push(`unsupported argsVersion ${args.argsVersion} (expected ${ARGS_VERSION})`);
  for (const k of REQUIRED) if (args[k] === undefined) errors.push(`missing required field: ${k}`);
  if (args.mode && !KNOB_VOCAB.modes.includes(args.mode)) errors.push(`invalid mode: ${args.mode}`);
  validateConfigEcho(args, errors, descriptors);
  if (args.nonce !== undefined && (typeof args.nonce !== 'string' || !NONCE_RE.test(args.nonce))) {
    errors.push(`invalid nonce: must match ${NONCE_RE} (AST-safe, non-splitting — interpolated into the verify command argv per slice)`);
  }
  // The workflow has no cwd for resolving relative roots and uses POSIX separators.
  for (const field of ARGS_SHAPES.pathFields) {
    const v = args[field];
    if (v === undefined) continue;
    if (typeof v !== 'string' || v.trim() === '') {
      errors.push(`${field} must be a non-empty string when present`);
      continue;
    }
    if (CONTROL_RE.test(v)) {
      errors.push(`${field} must not contain a control character`);
      continue;
    }
    if (field === 'pluginRoot' && v.includes('`')) {
      errors.push('pluginRoot must not contain a backtick');
      continue;
    }
    if (field === 'pluginRoot' && v.split('/').includes('..')) {
      errors.push('pluginRoot must not contain a .. path segment');
      continue;
    }
    // A short SHA is a bare verify argv token, so constrain it to the safe token alphabet.
    if (field === 'headShaShort' && !NONCE_RE.test(v)) {
      errors.push(`headShaShort must match ${NONCE_RE} (AST-safe, non-splitting — interpolated into the verify command argv)`);
    }
    if (field === 'repoRoot' && normalizeAbsoluteRoot(v) === null) {
      errors.push('repoRoot must be an absolute path without .. segments or backslashes');
    }
    if ((field === 'outputDir' || field === 'pluginRoot') && !v.startsWith('/')) {
      errors.push(`${field} must be an absolute path (POSIX /-prefix)`);
    }
  }
  // Reject caller-stamped agentFlags so scope cannot have a second authority alongside riskTable and scopeAnswer.
  if (args.agentFlags !== undefined) {
    errors.push('agentFlags is no longer accepted — the pipeline derives dimension flags itself from ' +
      'riskTable/scopeAnswer (issue #24). Stamp riskTable (the Phase 2e per-file risk ' +
      'classification) and, only when the light/full gate asked, scopeAnswer; do not stamp ' +
      'agentFlags.');
  }
  // Require the risk path set to match the diff: missing entries leave files unclassified,
  // while extra entries describe a different or stale diff.
  if (args.riskTable !== undefined) {
    if (!Array.isArray(args.riskTable)) {
      errors.push('riskTable must be an array of {path, risk} objects');
    } else {
      const seenPaths = new Set();
      for (let i = 0; i < args.riskTable.length; i++) {
        const entry = args.riskTable[i];
        if (!isPlainObject(entry)) {
          errors.push(`riskTable[${i}] must be an object of the form {path, risk}`);
          continue;
        }
        const extra = extraKeys(entry, ARGS_SHAPES.riskFields, true);
        if (extra.length > 0) {
          errors.push(`riskTable[${i}] has unexpected key(s): ${extra.join(', ')} (only path and risk are allowed)`);
        }
        if (typeof entry.path !== 'string' || entry.path === '') {
          errors.push(`riskTable[${i}].path must be a non-empty string`);
        } else {
          seenPaths.add(entry.path);
        }
        if (!RISK_LEVELS.includes(entry.risk)) {
          errors.push(`riskTable[${i}].risk must be one of ${RISK_LEVELS.join(', ')} (got ${JSON.stringify(entry.risk)})`);
        }
      }
      if (Array.isArray(args.changedFiles)) {
        const changedSet = new Set(args.changedFiles);
        const missing = args.changedFiles.filter((p) => !seenPaths.has(p));
        const extraPaths = [...seenPaths].filter((p) => !changedSet.has(p));
        if (missing.length > 0) {
          errors.push(`riskTable is missing risk classification for changed file(s): ${missing.join(', ')}`);
        }
        if (extraPaths.length > 0) {
          errors.push(`riskTable classifies file(s) not present in changedFiles: ${extraPaths.join(', ')}`);
        }
      }
    }
  }
  if (args.scopeAnswer !== undefined && !SCOPE_ANSWERS.includes(args.scopeAnswer)) {
    errors.push(`invalid scopeAnswer: ${args.scopeAnswer} (expected one of ${SCOPE_ANSWERS.join(', ')})`);
  }
  // Check scope coherence before dispatch, after shape checks, to avoid duplicate garbage-input errors.
  const riskTableWellFormed = Array.isArray(args.riskTable)
    && args.riskTable.every((e) => e && typeof e === 'object' && RISK_LEVELS.includes(e.risk) && typeof e.path === 'string' &&
      e.path !== '');
  const scopeAnswerWellFormed = args.scopeAnswer === undefined || SCOPE_ANSWERS.includes(args.scopeAnswer);
  if (riskTableWellFormed && scopeAnswerWellFormed && typeof args.changedLines === 'number') {
    const lightEligible = computeLightEligible(args.riskTable, args.changedLines);
    if ((args.scopeAnswer !== undefined) !== lightEligible) {
      if (args.scopeAnswer !== undefined) {
        errors.push(`scopeAnswer is "${args.scopeAnswer}" but the riskTable/changedLines are not light-eligible ` +
          `(not every file is low risk, or changedLines >= ${LIGHT_SCOPE_MAX_CHANGED_LINES}) — the ` +
          `orchestrator answered a light/full question the gate never asked; omit scopeAnswer`);
      } else {
        errors.push('riskTable/changedLines are light-eligible but scopeAnswer is missing — the light/full gate ' +
          'must have asked, and its answer must be stamped as scopeAnswer');
      }
    }
  }
  // Validate policy before dispatch because providers, tiers, and gateway settings affect model resolution.
  if (isPlainObject(args.policy)) validateArgsFields(args.policy, 'policy', errors, 'policy');
  validateArgsFields(args, 'changed', errors);
  // The host cannot inspect the context file, so supplied sizes drive its read plan.
  // Bounds prevent an uncatchable allocation failure; missing line counts remain a disclosed degradation.
  validateArgsFields(args, 'context', errors);
  if (args.contextChars !== undefined && args.contextLines === undefined) {
    errors.push('contextChars requires contextLines — chars alone cannot size a line-offset read plan');
  }
  // Validate every config layer before paid stages so malformed subtree settings cannot fail during filtering.
  if (args.reviewConfig !== undefined) {
    if (!isPlainObject(args.reviewConfig)) {
      errors.push('reviewConfig must be an object (the parseReviewMd output shape) when present');
    } else {
      const validateLayer = (layer, label, allowDir) => {
        validateArgsFields(layer, 'review', errors, label);
        if (layer.ignore !== undefined) validateFlatPatterns(layer.ignore, `${label}.ignore`, errors, true);
        if (!allowDir) return;
        if (typeof layer.dir !== 'string' || layer.dir === '') {
          errors.push(`${label}.dir must be a non-empty string`);
          return;
        }
        errors.push(...validatePathSegments(`${layer.dir}`, `${label}.dir`));
      };

      validateLayer(args.reviewConfig, 'reviewConfig', false);
      if (args.reviewConfig.scopes !== undefined) {
        if (!Array.isArray(args.reviewConfig.scopes)) {
          errors.push('reviewConfig.scopes must be an array when present');
        } else {
          const seenDirs = new Set();
          for (let i = 0; i < args.reviewConfig.scopes.length; i++) {
            const label = `reviewConfig.scopes[${i}]`;
            const scope = args.reviewConfig.scopes[i];
            if (!isPlainObject(scope)) {
              errors.push(`${label} must be a plain object`);
              continue;
            }
            const extra = extraKeys(scope, ARGS_SHAPES.scopeLayerFields, true);
            if (extra.length > 0) errors.push(`${label} has unexpected key(s): ${extra.join(', ')}`);
            validateLayer(scope, label, true);
            if (typeof scope.dir === 'string' && scope.dir !== '') {
              if (seenDirs.has(scope.dir)) errors.push(`${label}.dir duplicates another scope dir: ${scope.dir}`);
              seenDirs.add(scope.dir);
            }
          }
        }
      }
    }
  }
  // Reject non-string patterns here before literal regex escaping can throw during filtering.
  if (args.exclusionPatterns !== undefined) validateFlatPatterns(args.exclusionPatterns, 'exclusionPatterns', errors);
  // An empty raw discovery is authoritative absence; an omitted field means discovery was not supplied.
  if (args.reviewMd !== undefined) {
    if (typeof args.reviewMdSetChecksum !== 'string') {
      errors.push('reviewMdSetChecksum must be a string when reviewMd is present');
    }
    if (!Array.isArray(args.reviewMd)) {
      errors.push('reviewMd must be an array of {path, text, checksum} objects when present');
    } else {
      for (let i = 0; i < args.reviewMd.length; i++) {
        const entry = args.reviewMd[i];
        if (!isPlainObject(entry)) {
          errors.push(`reviewMd[${i}] must be an object of the form {path, text, checksum}`);
          continue;
        }
        const extra = extraKeys(entry, ARGS_SHAPES.reviewMdFields, true);
        if (extra.length > 0) {
          errors.push(`reviewMd[${i}] has unexpected key(s): ${extra.join(', ')} (only path, text and checksum are allowed)`);
        }
        if (typeof entry.path !== 'string' || entry.path === '') {
          errors.push(`reviewMd[${i}].path must be a non-empty string`);
        } else {
          errors.push(...validatePathSegments(entry.path, `reviewMd[${i}].path`, true));
        }
        if (entry.checksum !== null && typeof entry.checksum !== 'string') {
          errors.push(`reviewMd[${i}].checksum must be a string or null`);
        }
        if (typeof entry.text !== 'string') {
          errors.push(`reviewMd[${i}].text must be a string`);
        }
      }
      const hasRootReviewMd = args.reviewMd.some((entry) => (
        isPlainObject(entry) && entry.path === 'REVIEW.md'
      ));
      const hasReviewConfigPath = args.reviewConfigPath != null;
      if (hasRootReviewMd !== hasReviewConfigPath) {
        errors.push('reviewMd and reviewConfigPath must agree: reviewConfigPath must be non-null iff reviewMd ' +
          'contains an entry with path exactly REVIEW.md');
      }
    }
  }
  validateArgsFields(args, 'exclusions', errors);
  // Refuse competing raw and parsed authorities so config cannot be silently substituted.
  if (args.reviewMd !== undefined && args.reviewConfig !== undefined) {
    errors.push('reviewMd and reviewConfig are both present — pass raw reviewMd or pre-parsed reviewConfig, not both (single authority)');
  }
  if (args.exclusionsText !== undefined && args.exclusionPatterns !== undefined) {
    errors.push('exclusionsText and exclusionPatterns are both present — pass raw exclusionsText or ' +
      'pre-parsed exclusionPatterns, not both (single authority)');
  }
  // Reject unknown tiers before selection can default to all and lose the operator's narrowing intent.
  if (args.delivery !== undefined) {
    if (!isPlainObject(args.delivery)) {
      errors.push('delivery must be an object of the form { tier } when present');
    } else {
      if (args.delivery.tier !== undefined && !DELIVERY_TIERS.includes(args.delivery.tier)) {
        errors.push(`invalid delivery.tier: ${args.delivery.tier} (expected one of ${DELIVERY_TIERS.join(', ')})`);
      }
      // Local-diff reviews need no PR identity; PR reviews use it to persist the delivery wrapper.
      const id = args.delivery.prIdentity;
      if (id !== undefined) validatePrIdentity(id, errors);
    }
  }
  // Validate the persist shape before paid stages so malformed writer configuration cannot fail at persistence.
  if (args.persist !== undefined) {
    if (!isPlainObject(args.persist)) {
      errors.push('persist must be an object of the form { assembleScriptPath, returnPrimaries } when present');
    } else {
      validateArgsFields(args.persist, 'persist', errors);
    }
  }

  // pluginRoot binds the receipt to the scripts executed; allowing another root would break that trust boundary.
  errors.push(...validateScriptPaths(args));
  // Reject unknown limit keys so a typo cannot silently fall back to a different stage size.
  if (args.limits !== undefined) {
    if (!isPlainObject(args.limits)) {
      errors.push('limits must be an object when present');
    } else {
      for (const k of Object.keys(args.limits)) {
        if (!LIMIT_KEYS.includes(k)) errors.push(`unknown limits key: ${k} (expected one of ${LIMIT_KEYS.join(', ')})`);
      }
      validateArgsFields(args.limits, 'limits', errors);
    }
  }

  // Require explicit scope even for local targets so the renderer does not infer provenance.
  // Fallback explanations derive from detector facts rather than caller-authored prose.
  if (args.reviewScope !== undefined) {
    if (!isPlainObject(args.reviewScope)) {
      errors.push('reviewScope must be { requested, kind, since, commits, detector }');
    } else {
      const scopeExtra = extraKeys(args.reviewScope, ARGS_SHAPES.scopeFields);
      if (scopeExtra.length) errors.push(`reviewScope has unexpected key(s): ${scopeExtra.join(', ')}`);
      const { requested, kind, since, commits, detector } = args.reviewScope;
      if (!ARGS_SHAPES.scopeKinds.includes(requested)) {
        errors.push('reviewScope.requested must be full or incremental');
      }
      if (!ARGS_SHAPES.scopeKinds.includes(kind)) errors.push(`reviewScope.kind must be full or incremental`);
      if (kind === 'incremental') {
        if (requested !== 'incremental') errors.push('reviewScope.kind incremental requires requested incremental');
        if (typeof since !== 'string' ||
          !NONCE_RE.test(since)) errors.push('reviewScope.since must be a NONCE_RE-safe string for incremental scope');
        if (commits !== null && (!Number.isSafeInteger(commits) ||
          commits < 0)) errors.push('reviewScope.commits must be a non-negative safe integer or null');
      } else if (kind === 'full') {
        if (since !== null) errors.push('reviewScope.since must be null for full scope');
        if (commits !== null) errors.push('reviewScope.commits must be null for full scope');
      }
      if (detector !== null) {
        if (!isPlainObject(detector)) {
          errors.push('reviewScope.detector must be null or an object copied from detect_prior_review.py');
        } else {
          const detectorKeys = ARGS_SHAPES.detectorFields;
          const detectorExtra = extraKeys(detector, detectorKeys);
          const detectorMissing = detectorKeys.filter((key) => !Object.hasOwn(detector, key));
          if (detectorExtra.length) errors.push(`reviewScope.detector has unexpected key(s): ${detectorExtra.join(', ')}`);
          if (detectorMissing.length) errors.push(`reviewScope.detector is missing key(s): ${detectorMissing.join(', ')}`);
          for (const key of detectorKeys.slice(0, -1)) {
            if (typeof detector[key] !== 'boolean') errors.push(`reviewScope.detector.${key} must be a boolean`);
          }
          if (detector.error !== null && typeof detector.error !== 'string') {
            errors.push('reviewScope.detector.error must be a string or null');
          } else if (typeof detector.error === 'string' && CONTROL_RE.test(detector.error)) {
            errors.push('reviewScope.detector.error must be single-line without control characters');
          }
        }
      }
      if (kind === 'incremental' && (!isPlainObject(detector) || detector.incremental_safe !== true)) {
        errors.push('reviewScope.kind incremental requires detector.incremental_safe true');
      }
      if (kind === 'full' && requested === 'incremental' && detector === null) {
        errors.push('reviewScope full fallback requires detector facts when incremental was requested');
      }
    }
  }

  // Special presence and effective-value checks need focused errors; reported suppresses duplicate row mismatches.
  const reported = new Set();
  if (isPlainObject(args.configEcho)) {
    const capEntry = receiptEntryForKey(args, 'pr_comment_cap', descriptors);
    if (isPlainObject(args.limits) && capEntry !== null) {
      const capValue = args.limits.deliveryCap;
      const capShapeInvalid = capValue !== undefined && capValue !== null
        && (!Number.isSafeInteger(capValue) || capValue < 0);
      if (capShapeInvalid) {
        reported.add('limits.deliveryCap');
      } else if (args.mode === 'headless' && (capValue === undefined || capValue === null)) {
        errors.push('headless limits.deliveryCap must be a number matching configEcho.pr_comment_cap');
        reported.add('limits.deliveryCap');
      } else if (args.mode === 'interactive'
        && (capValue === null || capValue === undefined)
        && capEntry.value !== 'null') {
        errors.push('configEcho.pr_comment_cap must be null when limits.deliveryCap is absent or null');
        reported.add('limits.deliveryCap');
      }
    }

    const tierEntry = receiptEntryForKey(args, 'delivery_tier', descriptors);
    const stampedTier = dottedValue(args, 'delivery.tier');
    if (tierEntry !== null
      && (args.delivery === undefined || args.delivery === null || isPlainObject(args.delivery))
      && (stampedTier === undefined || stampedTier === null)) {
      reported.add('delivery.tier');
      if (tierEntry.value !== 'all') errors.push('configEcho.delivery_tier does not match delivery.tier');
    }

    if (args.mode === 'headless') {
      const trivialEntry = receiptEntryForKey(args, 'trivial_scope', descriptors);
      if (trivialEntry !== null && trivialEntry.value === 'light'
        && !computeLightEligible(args.riskTable, args.changedLines)) {
        errors.push(`configEcho.trivial_scope is "light" but the riskTable/changedLines are not light-eligible ` +
          `(not every file is low risk, or changedLines >= ${LIGHT_SCOPE_MAX_CHANGED_LINES}) — the ` +
          `orchestrator answered a light/full question the gate never asked`);
        reported.add('scopeAnswer');
      }
      const deliveryEcho = configEchoValue(args, 'delivery');
      if (deliveryEcho && deliveryEcho.split(',').includes('pr_comments')
        && (!isPlainObject(args.delivery) || args.delivery.prIdentity === undefined || args.delivery.prIdentity === null)) {
        errors.push('delivery.prIdentity is required when configEcho.delivery contains pr_comments');
      }
    }
  }

  // Keep generic receipt equality after special checks so malformed receipts receive only their focused refusal.
  if (isPlainObject(args.configEcho)) {
    for (const descriptor of descriptors) {
      if (!descriptor.modes.includes(args.mode) || descriptor.waistPath === null) continue;
      if (reported.has(descriptor.waistPath)) continue;
      const entry = receiptEntryFor(args, descriptor);
      if (entry === null) continue;
      if (!deriveWhenHolds(descriptor, args)) continue;
      const stamped = dottedValue(args, descriptor.waistPath);
      if (stamped === undefined) continue;
      const expected = mappedReceiptValue(descriptor, entry.value);
      if (expected === undefined) continue;
      const matches = descriptor.type === 'csv_list'
        ? Array.isArray(stamped)
          && stamped.length === expected.length
          && stamped.every((value, index) => value === expected[index])
        : stamped === expected;
      if (!matches) errors.push(`${descriptor.waistPath} does not match configEcho.${descriptor.key}`);
    }
    for (const descriptor of descriptors) {
      if (!descriptor.modes.includes(args.mode)
        || descriptor.derivedFrom === null
        || !Object.hasOwn(DERIVED_FROM, descriptor.derivedFrom)) continue;
      const entry = receiptEntryFor(args, descriptor);
      if (entry !== null && entry.value !== DERIVED_FROM[descriptor.derivedFrom].fill(args)) {
        errors.push(`configEcho.${descriptor.key} does not match ${descriptor.derivedFrom}`);
      }
    }
  }
  return { ok: errors.length === 0, errors };
}

export function reviewMdSetChecksum(entries) {
  return fnv1a32(entries.map((entry) => `${entry.path}\0${entry.checksum || 'null'}`).join('\n'));
}

// Resolve each config axis independently because raw discovery on one axis can coexist with parsed data on the other.
// Present empty raw inputs remain authoritative and must not fall back to pre-parsed values.
export function resolveReviewConfig(A) {
  const a = A || {};
  const reviewConfigSource = a.reviewMd !== undefined ? 'reviewMd'
    : (a.reviewConfig !== undefined ? 'preParsed' : 'none');
  const exclusionsSource = a.exclusionsText !== undefined ? 'exclusionsText'
    : (a.exclusionPatterns !== undefined ? 'preParsed' : 'none');

  let reviewConfig;
  let reviewMdEntryCount = 0;
  const gaps = [];
  if (a.reviewMd !== undefined) {
    const entries = a.reviewMd.map((entry) => {
      const text = normalizeReviewText(entry.text);
      if (entry.checksum === null) {
        gaps.push(`review_md: ${entry.path} text is unproven (no collector checksum); its settings were applied as stamped`);
      } else if (fnv1a32(text) !== entry.checksum) {
        gaps.push(`review_md: ${entry.path} text differs from the collected file (checksum mismatch); its settings were applied as stamped`);
      }
      return { ...entry, text };
    });
    reviewConfig = buildReviewConfig(entries);
    reviewMdEntryCount = a.reviewMd.length;
  } else {
    reviewConfig = a.reviewConfig || {};
  }

  if ((a.reviewMd !== undefined || a.reviewMdSetChecksum !== undefined) &&
      reviewMdSetChecksum(a.reviewMd || []) !== a.reviewMdSetChecksum) {
    gaps.push('review_md: stamped entries differ from the collected list (omitted, extra or reordered REVIEW.md); settings were applied as stamped');
  }

  const exclusionPatterns = a.exclusionsText !== undefined ? loadExclusions(a.exclusionsText)
    : (a.exclusionPatterns || []);

  return { reviewConfig, exclusionPatterns, reviewConfigSource, exclusionsSource, reviewMdEntryCount, gaps };
}
