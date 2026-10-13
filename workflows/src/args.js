import { normalizeAbsoluteRoot } from './paths.js';
import { ARGS_VERSION, REQUIRED, NONCE_RE, DELIVERY_TIERS, SCOPE_ANSWERS, CONTROL_RE } from './registry.js';
import { NULLABLE_TOP_LEVEL, RISK_LEVELS, LIMIT_DEFAULTS, NULL_TOLERANCE_CONSEQUENCE, LIGHT_SCOPE_MAX_CHANGED_LINES } from './registry.js';
import { MODE_VALUES, LIMIT_KEYS, ARGS_SHAPES, validateArgsFields, validatePrIdentity, validateScriptPaths } from './registry.js';
import { KNOB_REGISTRY, computeLightEligible, receiptEntryFor, receiptEntryForKey, mappedReceiptValue, DERIVED_FROM } from './knobs.js';
import { deriveWhenHolds, deriveConfigWaist, configEchoValue, validateConfigEcho } from './knobs.js';
import { dottedValue, isPlainObject } from './wire.js';
import { buildReviewConfig, loadExclusions } from './reviewConfig.js';

// args.js — the pipeline args waist: ARGS_VERSION, normalizeArgs, validateArgs.
// Single producer of the waist shape that bench and the pipeline entry both consume.
//
// policy shape: { tier, subagentModel, provider, gateway } — tier records the resolved
// model_tier knob (its only valid value today is "optimized"; alternate modes are roadmap
// #17 V3.2).
//   - policy.subagentModel is passed to registry.js's resolvePolicy() as opts.subagentModelEnv.
//     This is a RENAME, not a passthrough — dispatch sites must map the field name.
//   - policy.tier is carried through the waist but is not read by resolvePolicy today.
//   - policy.provider (optional; POLICY_PROVIDERS) is the skill's Phase 2 capture of which
//     API provider the session runs on (the workflow cannot read process.env, so this
//     capture is the only path). resolvePolicy pins full first-party model IDs only for
//     'firstParty'/absent; any other provider dispatches bare aliases, which the harness
//     resolves through the provider's deployment mapping — first-party IDs like
//     claude-sonnet-5-5 are passed through unchecked on Bedrock/Vertex/Foundry and 400.
//   - policy.gateway (optional boolean; issue #218) is the skill's Phase 2 capture of
//     whether ANTHROPIC_BASE_URL is set — an LLM gateway proxies the Anthropic API, so it
//     does not change policy.provider (see POLICY_PROVIDERS below), but it DOES gate off
//     registry.js's conditionalSchemaActive: a gateway forwards input_schema verbatim to
//     whatever backend it fronts, which could be an unmeasured third-party surface even
//     while the session itself reads as firstParty.

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

// nullToleranceGap(key) -> the operator-actionable gap line for one dropped null. Names the
// field, states it was treated as ABSENT, states the consequence, and says to omit it.
// Pure string building — no host globals (the workflow sandbox has no console/process).
export function nullToleranceGap(key, consequences = NULL_TOLERANCE_CONSEQUENCE) {
  const why = consequences[key] || 'the run proceeds as if the field had not been supplied';
  return `null_arg: args.${key} arrived as a literal null and was treated as ABSENT — ${why}. Omit ` +
    `${key} entirely (or stamp a well-formed value); do not stamp null.`;
}

// nullRespellGap(key) -> the disclosure for a JSON null respelled as a printed receipt token.
// The receipt and typed configuration stay unchanged; only the receipt spelling crosses the waist.
export function nullRespellGap(key) {
  return `null_receipt: args.${key} arrived as a literal null and was spelled as the printed token ` +
    `"null"; the receipt and the configuration are unchanged; stamp the string "null".`;
}

// nullToleranceRejectedKeys(cleanArgs, dropped, derivedPaths) -> the subset of `dropped` whose
// stamped null validateArgs would ACTUALLY have rejected — i.e. the keys where the tolerance
// changed the outcome and there is therefore something to disclose. A dropped field that
// normalizeArgs immediately re-derived is not a changed outcome and is filtered first.
//
// `checkpoints` is why this exists (issue #38 F4-3): validateArgs has never carried a
// checkpoints shape check, so a stamped `checkpoints: null` was ALWAYS equivalent to absence.
// Nothing was tolerated and nothing was lost, so a gap there announced a degradation that
// never happened — and noise in the gap channel is corrosive precisely because gaps are how
// this pipeline stays honest about the degradations that DID happen.
//
// The subset is COMPUTED, not listed: one differential validateArgs call per dropped key (the
// stripped waist, vs the same waist with that null put back). A second hand-maintained list
// would drift the moment someone adds or removes a shape check. Pure — validateArgs is pure,
// and the probe objects are shallow copies, so the caller's waist is untouched.
export function nullToleranceRejectedKeys(cleanArgs, dropped, derivedPaths = [], descriptors = KNOB_REGISTRY) {
  if (!Array.isArray(dropped) || dropped.length === 0) return [];
  const derived = new Set(Array.isArray(derivedPaths) ? derivedPaths : []);
  const baseline = validateArgs(cleanArgs, descriptors).errors.length;
  return dropped
    .filter((key) => !derived.has(key))
    .filter((key) => validateArgs(withNullAt(cleanArgs, key), descriptors).errors.length > baseline);
}

// The stripped waist with ONE dropped null put back, addressed by the same full dotted
// path spelling stripNullOptionalsReport reports. Every traversed level is shallow-copied,
// so an arbitrarily deep optional can be probed without mutating the caller's waist.
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

// stripNullOptionalsReport(args) -> { args, dropped, respelled }
// Drops a literal top-level null for the narrow NULLABLE_TOP_LEVEL allowlist (and, inside a
// present `delivery` object, a literal null for its `prIdentity`/`tier` sub-fields), and
// names every key it dropped so the caller can DISCLOSE the substitution.
// Non-mutating: the returned waist is a fresh shallow copy and a touched `delivery` is
// itself copied, so a caller holding the original object sees no change (its stamped nulls
// are still there). Non-object input (undefined, null, a string) passes through as-is with
// an empty `dropped`; there is nothing to strip.
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
  // Fill ONLY absent LIMIT_DEFAULTS keys — never overwrite a caller-provided value, and
  // never fill deliveryCap/discoveryCap (both absent from LIMIT_DEFAULTS on purpose; see
  // its comment). A provided `challengeCap: 0` is a real cap of zero and must survive
  // untouched, which `=== undefined` (not falsy) guarantees. A non-object `limits` (or one
  // missing entirely) is left for validateArgs to reject — this step does not repair shape.
  if (isPlainObject(out.limits)) {
    const limits = { ...out.limits };
    for (const [k, v] of Object.entries(LIMIT_DEFAULTS)) {
      if (limits[k] === undefined) limits[k] = v;
    }
    out.limits = limits;
  }
  // The receipt is the printed token: interactively, a JSON null is accepted as the spelling
  // of printed null, disclosed as a gap while validateConfigEcho remains strict.
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

// normalizeArgsReport(raw) -> { args, dropped, respelled, derivedPaths }
// `derivedPaths` identifies waist paths the receipt filled because they were absent after null-stripping,
// whether originally missing or dropped as an explicit null.
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

export function validateArgs(args, descriptors = KNOB_REGISTRY) {
  const errors = [];
  if (!args || typeof args !== 'object') return { ok: false, errors: ['args is not an object'] };
  if (args.argsVersion !== ARGS_VERSION) errors.push(`unsupported argsVersion ${args.argsVersion} (expected ${ARGS_VERSION})`);
  for (const k of REQUIRED) if (args[k] === undefined) errors.push(`missing required field: ${k}`);
  if (args.mode && !MODE_VALUES.includes(args.mode)) errors.push(`invalid mode: ${args.mode}`);
  validateConfigEcho(args, errors, descriptors);
  // Only charset-check a present nonce (absence is already a REQUIRED error above).
  if (args.nonce !== undefined && (typeof args.nonce !== 'string' || !NONCE_RE.test(args.nonce))) {
    errors.push(`invalid nonce: must match ${NONCE_RE} (AST-safe, non-splitting — interpolated into the verify command argv per slice)`);
  }
  // Validate consumed path fields before they reach prompts, executor args, or merge.
  // The workflow has no cwd to resolve relative roots against and uses POSIX separators.
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
    // Fail on relative roots instead of resolving them against an unavailable cwd.
    if (field === 'repoRoot' && normalizeAbsoluteRoot(v) === null) {
      errors.push('repoRoot must be an absolute path without .. segments or backslashes');
    }
    if ((field === 'outputDir' || field === 'pluginRoot') && !v.startsWith('/')) {
      errors.push(`${field} must be an absolute path (POSIX /-prefix)`);
    }
  }
  // Single authority (issue #24 req 4, PR3): the orchestrator-stamped `agentFlags` path is
  // RETIRED, not left as a second half-authority alongside riskTable/scopeAnswer. A caller
  // that still stamps agentFlags is a stale producer (an un-migrated skill build, a bench
  // fixture) — refuse loud rather than silently honour or silently ignore its scope intent.
  if (args.agentFlags !== undefined) {
    errors.push('agentFlags is no longer accepted — the pipeline derives dimension flags itself from ' +
      'riskTable/scopeAnswer (issue #24). Stamp riskTable (the Phase 2e per-file risk ' +
      'classification) and, only when the light/full gate asked, scopeAnswer; do not stamp ' +
      'agentFlags.');
  }
  // riskTable is the orchestrator's Phase 2e per-file risk classification, REQUIRED (see
  // REQUIRED above) so deriveAgentFlags (discover.js) has a deterministic input instead of a
  // hand-stamped agentFlags map. Malformed or missing NEVER silently resolves to full scope —
  // it refuses the run (req 2). The path set must equal changedFiles' set EXACTLY: a missing
  // path means a file was reviewed with no risk classification (silently under-scoped), an
  // extra path means the table describes a file that was not in the diff (stale/wrong table).
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
        const keys = Object.keys(entry).sort();
        const extra = keys.filter((k) => !ARGS_SHAPES.riskFields.includes(k));
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
  // scopeAnswer: the light/full answer, echoed ONLY when the trivial-scope gate actually
  // asked (absent otherwise — see NULL_TOLERANCE_CONSEQUENCE above for why a stamped null is
  // treated the same as absent here).
  if (args.scopeAnswer !== undefined && !SCOPE_ANSWERS.includes(args.scopeAnswer)) {
    errors.push(`invalid scopeAnswer: ${args.scopeAnswer} (expected one of ${SCOPE_ANSWERS.join(', ')})`);
  }
  // Fail-loud coherence (issue #24 req 4/5, PR3): scopeAnswer and riskTable/changedLines must
  // agree BEFORE dispatch, not merely be individually well-shaped. Both arms only evaluate once
  // riskTable/changedLines/scopeAnswer are themselves well-formed (the errors pushed above would
  // otherwise be duplicated by a garbage-in eligibility computation here).
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
  // policy.provider drives which arm of registry.js's model resolution runs (full-ID pin
  // vs bare alias — see POLICY_PROVIDERS above for why a typo must not resolve silently).
  // Only the provider field is shape-checked here: tier/subagentModel predate this guard
  // and keep their existing tolerance. Absent provider means 'firstParty' (older waists).
  if (isPlainObject(args.policy)) validateArgsFields(args.policy, 'policy', errors, 'policy');
  // Type-check the consumed by-value fields (absence is already a REQUIRED error above).
  validateArgsFields(args, 'changed', errors);
  // Optional shared-context file size, measured by the skill immediately after it writes
  // {output_dir}/code-gauntlet-context-{head_sha_short}.md. The workflow has no disk, so
  // this is the only path for it. contextReadPlan (contextPlan.js) turns the pair into the
  // exact Read calls the discovery/validate/summarize prompts enumerate — issue #48,
  // where an unmeasurable file left the agent to guess whether it had read all of it.
  //
  // OPTIONAL and independently so. Not for old callers: SKILL.md is the only producer of
  // this waist (bench invokes the skill rather than assembling args) and it ships with the
  // bundle, so there is no real skew window. It is optional because Phase 2 is
  // MODEL-EXECUTED and can skip the measurement step in a live run, and hard-failing there
  // would trade a partial read for a dead review. runWith emits a context_unmeasured gap so
  // the degradation is disclosed rather than silent. Absent contextChars just means the
  // line cap binds the chunk size alone. Both are shape-checked when present, because a
  // zero/negative/fractional value would produce a plan that either misses the file's
  // tail or names a nonsense offset — a silent under-read is exactly what this exists to
  // prevent, so a malformed value fails loud at the waist instead.
  // The MAX bounds are memory safety, not taste. contextReadPlan allocates one entry per
  // chunk; handed Number.MAX_SAFE_INTEGER it OOM-kills the node process with a V8 fatal
  // error — uncatchable, so runWith's top-level catch never runs, no gap is recorded and
  // nothing is dispatched. contextReadPlan carries its own chunk ceiling as the last line
  // of defence; this is the fail-loud one, at the waist, where a nonsense measurement is
  // still attributable to the producer that stamped it.
  validateArgsFields(args, 'context', errors);
  if (args.contextChars !== undefined && args.contextLines === undefined) {
    errors.push('contextChars requires contextLines — chars alone cannot size a line-offset read plan');
  }
  // Optional reviewConfig (the parsed REVIEW.md shape, see parseReviewMd in
  // reviewConfig.js). Thresholds and ignore patterns may carry subtree scopes; the
  // Filter stage resolves the matching layer per finding. Validate every layer here so a
  // malformed pre-parsed scope cannot fail after paid stages have already dispatched.
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
            const extra = Object.keys(scope).filter((key) => !ARGS_SHAPES.scopeLayerFields.includes(key)).sort();
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
  // Optional exclusionPatterns (the parsed exclusion-pattern list threaded alongside
  // reviewConfig). It feeds the same escapeRegExp path as reviewConfig.ignore — both are
  // concatenated in applyFilterPipeline (filterFindings.js) before the Filter stage builds
  // its regexes — so it is exposed to the same crash class (same live-run L2) and gets the
  // identical present-then-shape-checked treatment: absent is fine, malformed fails loud.
  if (args.exclusionPatterns !== undefined) validateFlatPatterns(args.exclusionPatterns, 'exclusionPatterns', errors);
  // Optional reviewMd (issue #24 PR2): the raw, ORDERED discovery of REVIEW.md files
  // as [{path, text}]; layer construction owns depth ordering. This is the RAW-text
  // counterpart to the pre-parsed `reviewConfig` above: resolveReviewConfig (below) builds
  // a root layer and optional subtree layers by calling parseReviewMd per entry. An empty
  // array is a legal, authoritative "discovery ran and found
  // nothing" signal — distinct from the field being absent entirely (see resolveReviewConfig's
  // reviewConfigSource). `path` is repo-relative (must NOT start with '/', the opposite
  // convention from repoRoot/outputDir above, which must) and reuses PATH_CONTROL_RE below via
  // a local copy since this guard runs before that regex is declared in file order... — see
  // REVIEW_MD_PATH_CONTROL_RE just below, same pattern.
  if (args.reviewMd !== undefined) {
    if (!Array.isArray(args.reviewMd)) {
      errors.push('reviewMd must be an array of {path, text} objects when present');
    } else {
      for (let i = 0; i < args.reviewMd.length; i++) {
        const entry = args.reviewMd[i];
        if (!isPlainObject(entry)) {
          errors.push(`reviewMd[${i}] must be an object of the form {path, text}`);
          continue;
        }
        const keys = Object.keys(entry).sort();
        const extra = keys.filter((k) => !ARGS_SHAPES.reviewMdFields.includes(k));
        if (extra.length > 0) {
          errors.push(`reviewMd[${i}] has unexpected key(s): ${extra.join(', ')} (only path and text are allowed)`);
        }
        if (typeof entry.path !== 'string' || entry.path === '') {
          errors.push(`reviewMd[${i}].path must be a non-empty string`);
        } else {
          errors.push(...validatePathSegments(entry.path, `reviewMd[${i}].path`, true));
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
  // Optional exclusionsText (issue #24 PR2): the raw text of whatever exclusions source the
  // skill discovered (e.g. .reviewignore), consumed by loadExclusions (reviewConfig.js) in
  // resolveReviewConfig below. Empty string is legal (an exclusions file that exists but is
  // empty).
  validateArgsFields(args, 'exclusions', errors);
  // Single-authority guards (issue #24 req 4 analog): a caller must pass EITHER the raw
  // discovery (reviewMd/exclusionsText) OR the pre-parsed shape (reviewConfig/
  // exclusionPatterns), never both for the same axis — two authorities for one piece of
  // config is exactly the silent-substitution hazard this waist exists to refuse loudly.
  if (args.reviewMd !== undefined && args.reviewConfig !== undefined) {
    errors.push('reviewMd and reviewConfig are both present — pass raw reviewMd or pre-parsed reviewConfig, not both (single authority)');
  }
  if (args.exclusionsText !== undefined && args.exclusionPatterns !== undefined) {
    errors.push('exclusionsText and exclusionPatterns are both present — pass raw exclusionsText or ' +
      'pre-parsed exclusionPatterns, not both (single authority)');
  }
  // Optional delivery selector. Absence is fine; when present it must be an object, and a
  // present tier must be a known value — an unknown tier would otherwise fall through to the
  // 'all' default in selectDelivery, silently ignoring an operator's narrowing intent.
  if (args.delivery !== undefined) {
    if (!isPlainObject(args.delivery)) {
      errors.push('delivery must be an object of the form { tier } when present');
    } else {
      if (args.delivery.tier !== undefined && !DELIVERY_TIERS.includes(args.delivery.tier)) {
        errors.push(`invalid delivery.tier: ${args.delivery.tier} (expected one of ${DELIVERY_TIERS.join(', ')})`);
      }
      // Optional PR identity (live-run L3): when present, the artifact-writer persists the
      // post_review-ready wrapper { owner, repo, pr_number, sha, platform, review_body, findings }
      // instead of the bare findings array — Phase 8 consumes it without hand-assembly.
      // ABSENT for local-diff reviews (the waist stays target-agnostic).
      const id = args.delivery.prIdentity;
      if (id !== undefined) validatePrIdentity(id, errors);
    }
  }
  // Optional persist waist (issue #38, D3.4): { assembleScriptPath }. When present, the
  // artifact-writer persists only the UNIQUE content (findings.json, report.md, the
  // persist plan) and the executor runs that script to DERIVE the post-review and
  // checkpoint artifacts on disk. When ABSENT, writeArtifacts takes the legacy full
  // by-value path and records no gap — a clean, documented degradation for older callers
  // (bench included). Same present-then-shape-checked pattern as `delivery`: a malformed
  // object fails loud at the waist rather than mid-persist, after every paid stage.
  // (A literal `persist: null` never reaches here — it is on NULLABLE_TOP_LEVEL and is
  // dropped, and disclosed, by stripNullOptionalsReport. The `=== null` arm below is the
  // guard for a caller that skipped normalization entirely.)
  if (args.persist !== undefined) {
    if (!isPlainObject(args.persist)) {
      errors.push('persist must be an object of the form { assembleScriptPath, returnPrimaries } when present');
    } else {
      validateArgsFields(args.persist, 'persist', errors);
    }
  }

  // pluginRoot is the trust boundary for every script path the workflow delegates back to
  // Phase 8. A path outside this root would make the receipt identify one plugin while the
  // executor ran another plugin's script.
  errors.push(...validateScriptPaths(args));
  // limits (REQUIRED — see REQUIRED above; the skill always stamps {} at worst, and
  // normalizeArgs fills the LIMIT_DEFAULTS keys before validateArgs ever runs on a
  // real waist). Every OTHER key is a hard error: issue #24's motivating incident was a
  // silent typo (`verifySclieSize: 50`) that never reached the stage it was meant to size
  // and just as silently fell back to a different default — this closes that class by
  // refusing any key this waist does not know about, rather than ignoring it.
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

  // Review scope is stamped by the prior-review gate and is deliberately required even for
  // local/branch targets: those targets stamp the explicit full form rather than leaving the
  // renderer to infer provenance. The detector facts are copied from the detector output;
  // the renderer derives any fallback explanation from those facts instead of accepting
  // orchestrator-authored prose.
  if (args.reviewScope !== undefined) {
    if (!isPlainObject(args.reviewScope)) {
      errors.push('reviewScope must be { requested, kind, since, commits, detector }');
    } else {
      const scopeKeys = Object.keys(args.reviewScope);
      const scopeKeysExpected = ARGS_SHAPES.scopeFields;
      const scopeExtra = scopeKeys.filter((key) => !scopeKeysExpected.includes(key));
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
          const detectorExtra = Object.keys(detector).filter((key) => !detectorKeys.includes(key));
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

  // A few coherence rules are not row equality: they enforce presence or effective values
  // that the generic registry pass intentionally skips. `reported` prevents those special
  // refusals from also producing the generic waist-path mismatch below.
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

  // This is the registry-driven lockstep authority. Keep it after the retained special arms
  // so a malformed receipt or a special absence rule produces only its focused refusal.
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

// resolveReviewConfig(A) -> { reviewConfig, exclusionPatterns, reviewConfigSource,
// exclusionsSource, reviewMdEntryCount } (issue #24 PR2 / #80).
//
// A strict SUPERSET of today's behavior: when A.reviewMd/A.exclusionsText are absent this
// resolves to exactly A.reviewConfig/A.exclusionPatterns as before (req 8 backward compat —
// bench children and older invocation shapes are unaffected). validateArgs above already
// refuses a waist that stamps BOTH the raw and pre-parsed form for the same axis, so at most
// one of each pair is ever present here.
//
// The reviewMd branch delegates layer construction to buildReviewConfig. The root layer is the
// default; a matching subtree layer overrides present thresholds and appends ignore entries.
// The builder owns deterministic depth ordering and never fills absent keys.
//
// reviewConfigSource and exclusionsSource are two INDEPENDENT provenance signals, one per
// axis — 'reviewMd' when A.reviewMd was present (even an empty array: discovery ran and found
// nothing is still authoritative) / 'exclusionsText' when A.exclusionsText was present;
// 'preParsed' when the axis's pre-parsed field (A.reviewConfig / A.exclusionPatterns) was
// present instead; 'none' when neither form of that axis was supplied. A mixed waist (one axis
// raw, the other pre-parsed) is legal transition back-compat and reports one label per axis
// accordingly — the two never need to agree.
export function resolveReviewConfig(A) {
  const a = A || {};
  const reviewConfigSource = a.reviewMd !== undefined ? 'reviewMd'
    : (a.reviewConfig !== undefined ? 'preParsed' : 'none');
  const exclusionsSource = a.exclusionsText !== undefined ? 'exclusionsText'
    : (a.exclusionPatterns !== undefined ? 'preParsed' : 'none');

  let reviewConfig;
  let reviewMdEntryCount = 0;
  if (a.reviewMd !== undefined) {
    reviewConfig = buildReviewConfig(a.reviewMd);
    reviewMdEntryCount = a.reviewMd.length;
  } else {
    reviewConfig = a.reviewConfig || {};
  }

  const exclusionPatterns = a.exclusionsText !== undefined ? loadExclusions(a.exclusionsText)
    : (a.exclusionPatterns || []);

  return { reviewConfig, exclusionPatterns, reviewConfigSource, exclusionsSource, reviewMdEntryCount };
}
