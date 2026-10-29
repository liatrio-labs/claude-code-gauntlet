// Shared contract rows belong here; knob descriptors, injection patterns, and stage orchestration belong in their modules.
import { deepFreeze } from './wire.js';
const SECURITY_SWEEP_PROMPT_EXTRA = 'Additionally sweep explicitly for: SSRF and unvalidated-URL fetches (user-influenced URLs ' +
  'reaching http/request/fetch clients without allowlist validation); frame and embedding ' +
  'policy gaps (missing X-Frame-Options or frame-ancestors, clickjacking exposure); ' +
  'postMessage handlers that do not validate event.origin or check it with weak substring ' +
  'matching; and string-matching bypass patterns where a security decision uses containment ' +
  'checks (indexOf/includes/startsWith/contains) on a host, origin, path, or scheme instead of ' +
  'exact parsing — these are bypassable (e.g. a host "evil.com/trusted.com" still contains ' +
  '"trusted.com").';
const TYPO_NAMING_SWEEP_PROMPT_EXTRA = 'Additionally run an explicit typo and naming sweep: identifier misspellings; typos in ' +
  'user-facing strings, messages, and log output; case-sensitivity mistakes in string ' +
  'comparisons (comparing mixed-case values without normalizing case); and copy-paste ' +
  'plural/singular or off-by-one naming mismatches (a field, key, or variable named for one ' +
  'thing but holding another).';

// Scope gating is opt-out so absent flags preserve full review; null flags keep core agents ungateable.
const DEEP = 'deep';

export const AGENT_TYPE_TEST_ANALYZER = 'code-gauntlet:test-analyzer';

// Share ordered identity checks with validation, rendering, and generated references so their contracts cannot drift.
export const SHA_FULL_RE = /^[0-9a-f]{40}$/;
export const WEB_ORIGIN_RE = new RegExp(
  "^https?:\\/\\/((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\\.(?:" +
  "[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*|\\[[0-9A-Fa-f:.]{2,45}" +
  "\\])(?::(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}" +
  "|655[0-2][0-9]|6553[0-5]))?$"
);

export const PERMALINK_TEMPLATES = deepFreeze({
  github: {
    blob: '{origin}/{owner}/{repo}/blob/{sha}/{path}',
    line: '#L{start}',
    range: '#L{start}-L{end}',
    ref: '#{number}',
    refUrl: '{origin}/{owner}/{repo}/pull/{number}',
  },
  gitlab: {
    blob: '{origin}/{owner}/{repo}/-/blob/{sha}/{path}',
    line: '#L{start}',
    range: '#L{start}-{end}',
    ref: '!{number}',
    refUrl: '{origin}/{owner}/{repo}/-/merge_requests/{number}',
  },
});

const validWebOrigin = (value) => {
  if (typeof value !== 'string') return false;
  const match = WEB_ORIGIN_RE.exec(value);
  if (!match) return false;
  return match[1].startsWith('[') || match[1].length <= 253;
};

export const PR_IDENTITY_FIELDS = deepFreeze([
  { name: 'owner', required: true, check: (value) => typeof value === 'string' && value.length > 0, describe: 'a non-empty string' },
  { name: 'repo', required: true, check: (value) => typeof value === 'string' && value.length > 0, describe: 'a non-empty string',
    extraChecks: [{ check: (value) => !value.includes('/'), message: 'must not contain "/"' }] },
  { name: 'pr_number', required: true, check: (value) => Number.isSafeInteger(value) && value > 0, describe: 'a positive safe integer' },
  { name: 'sha_full', required: true, check: (value) => typeof value === 'string' && SHA_FULL_RE.test(value),
    describe: 'a 40-character lowercase hex commit id' },
  { name: 'platform', required: true, check: (value) => Object.hasOwn(PERMALINK_TEMPLATES, value), describe: 'one of github, gitlab' },
  { name: 'web_origin', required: true, check: validWebOrigin, describe: 'an http(s) origin: scheme, host and optional port only' },
  { name: 'title', required: false, check: (value) => typeof value === 'string' && value.trim().length > 0,
    describe: 'a non-empty string when present' },
]);

export const CODE_OWNED_HEADINGS = deepFreeze([
  '## Summary',
  '## Change Context',
  '## Findings',
  '## Unverified / pipeline-degraded findings',
  '## Eliminated findings',
  '## Review Dimensions Summary',
  '## Review Methodology',
]);

// Agents are asked for 1 to 4 lines; a longer exact quote is more specific, and chars bounds its cost.
export const ANCHOR_LIMITS = deepFreeze({ lines: 8, chars: 400 });

export const LOCATION_RIGHTS = deepFreeze({
  confirmed: { body: true, sourceLink: true, inline: true, fence: true, patch: true, fixTask: true },
  relocated: { body: true, sourceLink: true, inline: true, fence: true, patch: true, fixTask: true },
  unconfirmed: { body: true, sourceLink: false, inline: false, fence: false, patch: false, fixTask: false },
});
export const LOCATION_STATUSES = deepFreeze(Object.keys(LOCATION_RIGHTS));

export const DISPOSITION_RIGHTS = deepFreeze({
  deliver: { inline: true, body: true, report: 'finding', fixTask: true, patch: true, fence: true, sourceLink: true },
  body: { inline: false, body: true, report: 'finding', fixTask: false, patch: false, fence: false, sourceLink: true },
  held_back: { inline: false, body: false, report: 'unverified', fixTask: false, patch: false, fence: false, sourceLink: false },
  report_only: { inline: false, body: false, report: 'finding', fixTask: false, patch: false, fence: false, sourceLink: false },
  eliminate: { inline: false, body: false, report: 'eliminated', fixTask: false, patch: false, fence: false, sourceLink: false },
});
export const DISPOSITIONS = deepFreeze(Object.keys(DISPOSITION_RIGHTS));

// Use full schema fragments for arrays because the host requires items; scalar fields can use type shorthand.
// Verification stamps origin, so discovery does not require it.
export const FINDING_PROP_TYPES = deepFreeze({
  // Numeric confidence prevents consensus boosts from concatenating strings; resumed findings still need coercion.
  id: 'string', file: 'string', line_start: 'number', line_end: 'number',
  title: 'string', description: 'string', severity: 'string', confidence: 'number',
  dimension: 'string', origin: 'string', evidence: 'string',
  anchor: { type: 'string', description: 'Exact text, copied from the source at the PR head, of the 1 to 4 contiguous lines in file this finding is about; at most 400 characters.' },
  // Optional prose fields must be omitted when inapplicable; emitting null would violate their schema types.
  suggestion: 'string', claude_md_rule: 'string',
  // Delivery applies deterministic fix checks before rendering a committable fence.
  // The field alone does not guarantee that a patch will ship.
  suggested_fix_code: 'string',
  cross_file_refs: { type: 'array', items: { type: 'string' } },
});

// Structural references need the same confinement as file before reaching artifacts.
export const FINDING_PATH_ARRAY_FIELDS = deepFreeze(['cross_file_refs', 'affected_consumers']);

// Identifiers must not collide; structural paths and suggestion code retain their own semantics.
export const FINDING_REWRITE_EXEMPT_FIELDS = deepFreeze(['id', 'consolidation_key', 'consolidation_primary', 'file', ...FINDING_PATH_ARRAY_FIELDS, 'suggested_fix_code', 'removed_test', 'anchor', 'claimed_line_start', 'claimed_line_end']);

// Flat required fields must be unconditional for every dimension in a dispatch to avoid forcing fabricated sibling fields.
export const FINDING_REQUIRED = deepFreeze(['id', 'file', 'line_start', 'title', 'description', 'severity', 'confidence', 'dimension']);

export const SEVERITY_ORDER = deepFreeze(['critical', 'high', 'medium', 'low']);

const closedDispatchRow = (properties, optional = []) => ({
  type: 'object', properties, required: Object.keys(properties).filter((key) => !optional.includes(key)), additionalProperties: false,
});

export const DISPATCH_RESULTS = deepFreeze({
  [AGENT_TYPE_TEST_ANALYZER]: {
    removed_test_audit: {
      type: 'array', items: closedDispatchRow({
        id: { type: 'string', minLength: 1 },
        status: { type: 'string', minLength: 1, enum: ['removed_with_code', 'covered_elsewhere', 'lost'] },
        covered_by: { type: 'array', items: closedDispatchRow({
          file: { type: 'string', minLength: 1 }, line: { type: 'integer', minimum: 1 },
          input: { type: 'string', minLength: 1 }, assertion: { type: 'string', minLength: 1 },
        }) },
        losses: { type: 'array', items: closedDispatchRow({
          file: { type: 'string', minLength: 1 }, line: { type: 'integer', minimum: 1 },
          title: { type: 'string', minLength: 1 }, description: { type: 'string', minLength: 1 },
          severity: { type: 'string', minLength: 1, enum: SEVERITY_ORDER },
          confidence: { type: 'number', minimum: 0, maximum: 100 },
          criticality: { type: 'number', minimum: 1, maximum: 10 },
          failure_scenario: { type: 'string', minLength: 1 },
          anchor: FINDING_PROP_TYPES.anchor,
        }, ['anchor']) },
      }),
    },
  },
});

// `requiredWhenDimension` names fields the owning contract emits
// within one dimension; sibling dimensions omit them, so a flat dispatch requirement
// would force fabricated fields. Conditional enforcement is provider-gated.
export const DIMENSIONS = deepFreeze([
  { dimension: 'bug', agentType: 'code-gauntlet:bug-detector', conditionalFlag: null, schemaExtra: { hidden_errors: 'string' },
    requiredExtra: [], requiredWhenDimension: [], modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'security', agentType: 'code-gauntlet:security-reviewer', conditionalFlag: null, schemaExtra: { attack_vector: 'string' },
    requiredExtra: ['attack_vector'], requiredWhenDimension: [], modelOverride: 'opus', promptExtra: SECURITY_SWEEP_PROMPT_EXTRA },
  { dimension: 'cross_file_impact', agentType: 'code-gauntlet:cross-file-impact', conditionalFlag: DEEP,
    schemaExtra: { affected_consumers: { type: 'array', items: { type: 'string' } } }, requiredExtra: ['affected_consumers'],
      requiredWhenDimension: [], modelOverride: null, promptExtra: null },
  { dimension: 'test_coverage', agentType: AGENT_TYPE_TEST_ANALYZER, conditionalFlag: DEEP,
    // Bound the impact scale at dispatch; confidence remains unbounded here because runtime validators adjust it.
    schemaExtra: { criticality: { type: 'number', minimum: 1, maximum: 10 }, failure_scenario: 'string' },
    requiredExtra: ['criticality', 'failure_scenario'], requiredWhenDimension: [], modelOverride: null, promptExtra: null },
  { dimension: 'convention', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP,
    schemaExtra: { rule_source: 'string' }, requiredExtra: [], requiredWhenDimension: ['claude_md_rule'], modelOverride: null,
    promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'intent', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP,
    schemaExtra: { spec_text: 'string' }, requiredExtra: [], requiredWhenDimension: ['spec_text'], modelOverride: null,
      promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'comment_accuracy', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP, schemaExtra: {},
    requiredExtra: [], requiredWhenDimension: [], modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'type_design', agentType: 'code-gauntlet:type-design-analyzer', conditionalFlag: DEEP,
    schemaExtra: { invalid_state_example: 'string' }, requiredExtra: [], requiredWhenDimension: [], modelOverride: null,
      promptExtra: null },
  { dimension: 'simplification', agentType: 'code-gauntlet:code-simplifier', conditionalFlag: DEEP,
    schemaExtra: { behavior_preserved: 'string' }, requiredExtra: ['behavior_preserved'], requiredWhenDimension: [], modelOverride: null,
      promptExtra: null },
]);

// Keep pipeline stamp names code-owned so disclosure can use a safe other fallback for unknown fields.
const PIPELINE_FINDING_FIELDS = deepFreeze([
  'trace_id', 'discovery_agent', 'discovery_dimension',
  'removed_test',
  'location_status', 'claimed_line_start', 'claimed_line_end',
  'agent', 'body', 'line', 'end_line', 'verified', 'validation', 'challenge',
  'validation_justification', 'challenge_justification', 'elimination_reason',
  'original_confidence', 'validator_confidence', 'reachability', 'challenge_score',
  'challenge_contested', 'severity_downgraded', 'original_severity',
  'report_destination', 'report_tag', 'eliminated_by', 'replay_belt',
  'contested', 'contestation_drop', 'contestation_reason', 'demoted_by', 'demotion_reason',
  'consensus_count', 'consensus_boost', 'corroborated_by', 'corroborations',
  'singleton_penalty', 'contradiction', 'security_escalation', 'escalation_note',
  'consolidation_key', 'consolidation_primary', 'routed_by', 'promoted_from', 'promotion_reason',
  ...['suggestion', 'claude_md_rule', 'spec_text', 'suggested_fix_code'].flatMap((field) =>
    [`${field}_removed_by`, `${field}_removal_reason`]),
]);

export const FINDING_TEXT_FIELD_NAMES = deepFreeze([...new Set([
  ...Object.keys(FINDING_PROP_TYPES),
  ...DIMENSIONS.flatMap((dimension) => Object.keys(dimension.schemaExtra || {})),
  ...PIPELINE_FINDING_FIELDS,
])]);

export const AGENTS = deepFreeze([...new Set(DIMENSIONS.map((d) => d.agentType))]);

// Key display labels by agent because a shared dispatch contributes one aggregate row, not one row per dimension.
export const AGENT_LABELS = deepFreeze({
  'code-gauntlet:bug-detector': 'Correctness & Error Handling',
  'code-gauntlet:security-reviewer': 'Security',
  'code-gauntlet:cross-file-impact': 'Cross-file Impact',
  [AGENT_TYPE_TEST_ANALYZER]: 'Test Coverage',
  'code-gauntlet:conventions-and-intent': 'Conventions & Intent',
  'code-gauntlet:type-design-analyzer': 'Type Design',
  'code-gauntlet:code-simplifier': 'Code Simplification',
});

// Presentation branding is separate from the machine-parsed product slug, so changing display text cannot rename the wire protocol.
export const BRAND_MARK = '\u2694\uFE0F';
export const BRAND_NAME = 'Code Gauntlet';
export const SEVERITY_EMOJI = deepFreeze({
  critical: '\u{1F534}', high: '\u{1F7E0}', medium: '\u{1F7E1}', low: '\u{1F4A1}',
});
export const SEVERITY_EMOJI_FALLBACK = SEVERITY_EMOJI.low;

export const RULE_SOURCE_LABELS = deepFreeze({
  documented_rule: 'Cited rule',
  code_comment: 'Cited comment',
  repo_precedent: 'Repo precedent',
  self_inconsistency: 'Inconsistency',
});
export const RULE_SOURCE_LABEL_FALLBACK = 'Cited rule';

export const STAGE_DEFAULTS = deepFreeze({
  validator: 'sonnet', challenger: 'sonnet', executor: 'sonnet',
  'artifact-writer': 'sonnet',
});

// Pin first-party IDs so the session's model variant cannot cascade into agents.
const MODEL_IDS = deepFreeze({ sonnet: 'claude-sonnet-5-5', opus: 'claude-opus-5-5', haiku: 'claude-haiku-4-5-20251001' });
export const MODEL_ALIASES = deepFreeze(Object.keys(MODEL_IDS));
export const pinsModelIds = (provider) => provider === undefined || provider === null || provider === 'firstParty';
// Claude Code must resolve user mappings itself, including suffixes, overrides and allowlists.
const toModelId = (m, provider, mappedAliases) =>
  (pinsModelIds(provider) && !mappedAliases?.includes(m) ? (MODEL_IDS[m] || m) : m);

// A gateway may preserve first-party model names while forwarding schemas to another backend.
// Gate conditional schemas separately from model pinning for that host constraint.
export const conditionalSchemaActive = (policy) => {
  const p = policy || {};
  return pinsModelIds(p.provider) && !p.gateway;
};

export function resolvePolicy(agentType, opts = {}, dimensions = DIMENSIONS) {
  if (opts.subagentModelEnv) {
    // Preserve explicit third-party deployment IDs so the override remains an escape hatch from alias mapping.
    return { model: toModelId(opts.subagentModelEnv, opts.provider, opts.mappedAliases) };
  }
  const dim = dimensions.find((d) => d.agentType === agentType);
  const model = toModelId(dim?.modelOverride || STAGE_DEFAULTS[agentType.split(':').pop()] || 'sonnet', opts.provider, opts.mappedAliases);
  return { model };
}

export function modelFor(agentType, policy, dimensions = DIMENSIONS) {
  return resolvePolicy(agentType, {
    subagentModelEnv: policy.subagentModel, provider: policy.provider, mappedAliases: policy.mappedAliases,
  }, dimensions).model;
}

export function resolvedDispatchModels(policy) {
  const types = [...AGENTS, 'code-gauntlet:change-summarizer', ...Object.keys(STAGE_DEFAULTS).map((name) => `code-gauntlet:${name}`)];
  return Object.fromEntries(types.map((type) => [type, modelFor(type, policy)]));
}

export const REQUIRED = deepFreeze(['mode', 'repoRoot', 'outputDir', 'headShaShort', 'nonce', 'generatedAt', 'diffPath', 'changedFiles',
  'changedLines', 'riskTable', 'policy', 'limits', 'configEcho', 'pluginRoot', 'reviewScope']);

export const DELIVERY_TIERS = deepFreeze(['all', 'main_only']);

export const SCOPE_ANSWERS = deepFreeze(['light', 'full']);

// Provider and gateway answer different questions: model naming and whether conditional schemas reach another backend.
export const POLICY_PROVIDERS = deepFreeze(['firstParty', 'bedrock', 'vertex', 'foundry']);

export const CONTROL_RE = /[\u0000-\u001F\u007F]/;

// Absent delivery and discovery caps mean no ceiling; defaulting them would silently narrow a review.
export const LIMIT_DEFAULTS = deepFreeze({
  summarizeBucketSize: 20,
  validateBatch: 25,
  challengeCap: 40,
  // Small verify slices limit the blast radius of a bad inline transcription.
  verifySliceSize: 25,
  maxLineSpan: 100,
});

export const LIGHT_SCOPE_MAX_CHANGED_LINES = 50;

export const TRUSTED_SCRIPT_BASENAMES = deepFreeze({
  assemble: 'assemble_artifacts.py',
  verify: 'verify_findings.py',
});

export const FIELD_RENAMES = deepFreeze({ body: 'description', line: 'line_start', blame_tag: 'origin' });

export const REVIEW_SETTING_KEYS = deepFreeze([
  'confidence_threshold',
  'security_min_confidence',
  'severity_threshold',
]);

export const INJECTION_STRIPPED_PROSE_FIELDS = deepFreeze(['suggestion', 'claude_md_rule', 'spec_text']);

export const AGENT_BUG_DETECTOR = 'bug-detector';

export const AGENT_TEST_ANALYZER = 'test-analyzer';

export const AGENT_SECURITY_REVIEWER = 'security-reviewer';

export const AGENT_CONVENTIONS = 'conventions-and-intent';

// Display limits do not bound the whole report or return payload; findings.json retains unfolded values and cardinality.
export const REPORT_FOLD_LIMITS = deepFreeze({
  proseChars: 4000,
  summaryChars: 12000,
  summaryIndexChars: 12000,
  evidenceLines: 40,
  evidenceChars: 8000,
  inlineChars: 512,
});

export const KNOB_VOCAB = deepFreeze({
  modes: ['headless', 'interactive'],
  headless: ['headless'],
  interactive: ['interactive'],
  envDefault: ['env', 'default'],
  fixed: ['fixed'],
  envReviewMdDefault: ['env', 'review_md', 'default'],
  discovery: ['discovery'],
  modelTiers: ['optimized'],
  deliveryChannels: ['chat', 'pr_comments', 'markdown'],
  postModes: ['dry-run', 'live'],
  draftPolicies: ['review', 'skip'],
  reviewedPolicies: ['incremental', 'full', 'skip'],
  prNotFoundPolicies: ['local', 'error'],
  reviewMdValues: ['present', 'absent'],
  receiptEntryKeys: ['value', 'source'],
});

export function identityCheck(identity, name, fields = PR_IDENTITY_FIELDS) {
  const field = fields.find((entry) => entry.name === name);
  return Boolean(field && field.check(identity[name]));
}

export const CORE_DIMENSIONS = deepFreeze(['bug', 'security', 'cross_file_impact', 'intent']);

export const SUGGESTION_DIMENSIONS = deepFreeze(['comment_accuracy', 'comment-accuracy']);

export const MAIN_REPORT_AGENTS = deepFreeze([AGENT_BUG_DETECTOR, AGENT_SECURITY_REVIEWER, 'cross-file-impact', 'type-design-analyzer']);

export const SUGGESTION_AGENTS = deepFreeze([AGENT_TEST_ANALYZER, 'code-simplifier']);

export const COMMENT_ACCURACY_DIMENSIONS = deepFreeze(['comment-accuracy', 'documentation', 'doc-accuracy']);

export const REPORT_PLACED_FIELDS = deepFreeze([
  'id', 'file', 'line_start', 'line_end', 'title', 'description', 'severity',
  'confidence', 'dimension', 'origin', 'evidence', 'suggestion',
  'claude_md_rule', 'rule_source', 'spec_text', 'cross_file_refs',
]);
