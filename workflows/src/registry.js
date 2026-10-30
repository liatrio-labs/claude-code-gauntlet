// Shared contract rows belong here; knob descriptors, injection patterns, and stage orchestration belong in their modules.
import { deepFreeze } from './wire.js';
import { repoRelativeFindingPath } from './paths.js';
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
  '## Findings held back by admission',
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
  body: { inline: false, body: true, report: 'finding', fixTask: true, patch: false, fence: false, sourceLink: true },
  held_back: { inline: false, body: false, report: 'unverified', fixTask: false, patch: false, fence: false, sourceLink: false },
  report_only: { inline: false, body: false, report: 'finding', fixTask: false, patch: false, fence: false, sourceLink: false },
  eliminate: { inline: false, body: false, report: 'eliminated', fixTask: false, patch: false, fence: false, sourceLink: false },
});
export const DISPOSITIONS = deepFreeze(Object.keys(DISPOSITION_RIGHTS));
export const RIGHT_ELIGIBILITY = deepFreeze({ body: { fixTask: ['current_defect', 'current_reachability', 'confirmed_location'] } });
export const STAGE_COSTS = deepFreeze({ verify: { attempts: 2 }, validate: { retryBatchDivisor: 5 }, cite: { attempts: 2 }, writer: { attempts: 2 } });

const SOURCE_REFERENCE = {
  type: 'object', $comment: 'source-reference', properties: {
    file: { type: 'string', $comment: 'repo-path' },
    line_start: { type: 'integer', minimum: 1 }, line_end: { type: 'integer', minimum: 1 },
    anchor: { type: 'string', minLength: 1 },
  }, required: ['file', 'line_start', 'line_end', 'anchor'], additionalProperties: false,
};

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
  cross_file_refs: { type: 'array', items: { type: 'string', $comment: 'repo-path' } },
  sites: { discovery: true, type: 'array', description: 'Additional locations of this issue.', items: {
    type: 'object', properties: {
      file: { type: 'string', $comment: 'repo-path' },
      line_start: { type: 'integer', minimum: 1 }, line_end: { type: 'integer', minimum: 1 },
      anchor: { type: 'string' },
    }, required: ['file', 'line_start', 'line_end', 'anchor'], additionalProperties: false,
  } },
});

// Stage discovery fields independently of the downstream verification and delivery wire.
export const DISCOVERY_FINDING_PROP_TYPES = deepFreeze(Object.fromEntries(Object.entries(FINDING_PROP_TYPES)
  .filter(([, schema]) => schema.discovery !== false)
  .map(([name, schema]) => {
    if (typeof schema === 'string') return [name, schema];
    const { discovery, ...wireSchema } = schema;
    return [name, wireSchema];
  })));

const RECURRENCE_RETURN_PROPERTIES = {
  site_confirmations: { type: 'array', items: {
    type: 'object', properties: {
      file: { type: 'string' }, line_start: { type: 'integer', minimum: 1 },
      line_end: { type: 'integer', minimum: 1 }, confirmed: { type: 'boolean' },
    }, required: ['file', 'line_start', 'line_end', 'confirmed'], additionalProperties: false,
  } },
};

export const RESOLVED_SITE_SCHEMA = deepFreeze({ type: 'array', items: {
  ...FINDING_PROP_TYPES.sites.items,
  properties: { ...FINDING_PROP_TYPES.sites.items.properties,
    location_status: { type: 'string', enum: LOCATION_STATUSES },
    diff_validation: { type: 'object', properties: { in_diff: { type: ['boolean', 'null'] } }, required: ['in_diff'], additionalProperties: false },
  }, required: [...FINDING_PROP_TYPES.sites.items.required, 'location_status', 'diff_validation'],
} });

export const DELIVERY_GROUP_SCHEMA = deepFreeze({ type: 'object', properties: {
  issue_key: { type: 'string' }, lead_id: { type: 'string' }, finding_ids: { type: 'array', items: { type: 'string' } },
}, required: ['issue_key', 'lead_id', 'finding_ids'], additionalProperties: false });

// Flat required fields must be unconditional for every dimension in a dispatch to avoid forcing fabricated sibling fields.
export const FINDING_REQUIRED = deepFreeze(['id', 'file', 'line_start', 'title', 'description', 'severity', 'confidence', 'dimension']);

export const SEVERITY_ORDER = deepFreeze(['critical', 'high', 'medium', 'low']);

const closedDispatchRow = (properties, optional = []) => ({
  type: 'object', properties, required: Object.keys(properties).filter((key) => !optional.includes(key)), additionalProperties: false,
});

const POLICY_SUBJECTS = {
  test_defect: { dimension: 'test_coverage', kind: 'defective_test', stamp: 'effective_test_kind' },
  coverage: { dimension: 'test_coverage', kind: 'critical_gap' },
  ordinary: { original: true }, unresolved: { defaultDisposition: 'held_back' },
};

const QUALITY_ADMISSION = {
  field: 'quality_basis', promotion: { predicate: 'present_defect', kind: 'current_correctness' }, kinds: {
    current_correctness: { description: 'A demonstrated present production defect.', required: [], predicates: ['present_defect'], phase: 'semantic', core: true, tier: 'main' },
    duplication: { description: 'Real duplicated behavior identified by duplicate_of; show the redundancy and how consolidation preserves behavior.', required: ['duplicate_of'], predicates: ['resolved_duplicate', 'confirmed_duplication'], phase: 'semantic', tier: 'suggestion' },
    simpler_equivalent: { description: 'A concrete simpler replacement with evidence of reduced complexity and preserved return values, side effects and error paths.', required: ['behavior_preserved'], anyOf: ['suggestion', 'suggested_fix_code'], predicates: ['concrete_equivalent', 'behavior_preserved'], phase: 'semantic', tier: 'suggestion' },
    stack_idiom: { description: 'A behavior-preserving replacement supported by applicable existing repository precedent; identify the precedent and the cost it removes.', required: ['precedent'], predicates: ['resolved_precedent', 'precedent_not_added', 'applicable'], phase: 'semantic', tier: 'suggestion' },
    other: { description: 'No demonstrated defect, redundancy, simpler equivalent or applicable precedent; unsupported preferences stay report_only.', required: [], predicates: [], phase: 'deterministic', defaultDisposition: 'report_only', tier: 'suggestion' },
  },
};

// Shared dispatches must not require fields from irrelevant sibling dimensions.
export const DIMENSIONS = deepFreeze([
  { dimension: 'bug', core: true, agentType: 'code-gauntlet:bug-detector', conditionalFlag: null, schemaExtra: { hidden_errors: { type: 'string', description: 'How an error is hidden or silently swallowed.' } },
    requiredExtra: [], requiredWhenDimension: [], modelOverride: null, promptExtra: null },
  { dimension: 'security', core: true, agentType: 'code-gauntlet:security-reviewer', conditionalFlag: null, schemaExtra: { attack_vector: { type: 'string', description: 'Concrete attack path and its preconditions.' } },
    requiredExtra: ['attack_vector'], requiredWhenDimension: [], modelOverride: 'opus', promptExtra: null },
  { dimension: 'cross_file_impact', core: true, agentType: 'code-gauntlet:cross-file-impact', conditionalFlag: DEEP,
    schemaExtra: { affected_consumers: { type: 'array', description: 'Repository paths of consumers affected by the change.', items: { type: 'string', $comment: 'repo-path' } } }, requiredExtra: ['affected_consumers'],
      requiredWhenDimension: [], modelOverride: null, promptExtra: null },
  { dimension: 'test_coverage', agentType: AGENT_TYPE_TEST_ANALYZER, conditionalFlag: DEEP,
    defaultDisposition: 'held_back', defaultTier: 'main', admission: { field: 'test_kind', promotion: { predicate: 'present_defect', kind: 'production_defect', stamp: 'effective_test_kind' }, kinds: {
      defective_test: { required: [], predicates: ['pr_owned_test', 'confirmed_test_defect', 'test_diff_site_for_inline'], phase: 'semantic', tier: 'main' },
      critical_gap: { required: ['criticality', 'failure_scenario', 'searched_paths', 'candidate_tests'], optional: ['production_element'], predicates: ['criticality_at_least_8', 'resolved_production_element', 'complete_searches', 'complete_behavioral_absence'], phase: 'semantic', tier: 'main' },
      production_defect: { required: [], predicates: ['present_defect'], phase: 'semantic', dimension: 'bug', core: true, tier: 'main' },
      additional_case: { required: [], predicates: [], phase: 'deterministic', defaultDisposition: 'report_only', tier: 'suggestion' },
    } },
    // Bound the impact scale at dispatch; confidence remains unbounded here because runtime validators adjust it.
    schemaExtra: {
      criticality: { type: 'number', minimum: 1, maximum: 10, description: 'Impact of the untested behavior on a 1 to 10 scale.' },
      failure_scenario: { type: 'string', description: 'Concrete input or condition and the failure it exposes.' },
      searched_paths: { type: 'array', $comment: 'search-paths', description: 'Repository files or directories searched for coverage.', items: { type: 'string', $comment: 'repo-path' } },
      candidate_tests: { type: 'array', description: 'Inspected tests, including their input and assertion.', items: {
        ...SOURCE_REFERENCE, properties: { ...SOURCE_REFERENCE.properties, input: { type: 'string' }, assertion: { type: 'string' } },
        required: [...SOURCE_REFERENCE.required, 'input', 'assertion'],
      } },
      production_element: { ...SOURCE_REFERENCE, description: 'Source reference to the production behavior whose coverage is assessed.' },
    },
    requiredExtra: ['criticality', 'failure_scenario'], requiredWhenDimension: [], modelOverride: null, promptExtra: null },
  { dimension: 'convention', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP, functionalRouting: true, agentFallback: true,
    schemaExtra: { rule_source: { type: 'string', description: 'Basis for the cited rule: documented_rule, code_comment, repo_precedent, or self_inconsistency.' } }, requiredExtra: [], requiredWhenDimension: ['claude_md_rule'], modelOverride: null,
    promptExtra: null },
  { dimension: 'intent', core: true, agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP,
    schemaExtra: { spec_text: { type: 'string', description: 'Quoted specification or requirement contradicted by the implementation.' } }, requiredExtra: [], requiredWhenDimension: ['spec_text'], modelOverride: null,
      promptExtra: null },
  { dimension: 'comment_accuracy', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP, defaultTier: 'suggestion', aliases: ['comment-accuracy', 'documentation', 'doc-accuracy'], schemaExtra: {},
    requiredExtra: [], requiredWhenDimension: [], modelOverride: null, promptExtra: null },
  { dimension: 'type_design', agentType: 'code-gauntlet:type-design-analyzer', conditionalFlag: DEEP,
    defaultDisposition: 'held_back', defaultTier: 'suggestion', admission: QUALITY_ADMISSION,
    schemaExtra: { invalid_state_example: { type: 'string', description: 'Concrete invalid state permitted by the type design.' } }, requiredExtra: [], requiredWhenDimension: [], modelOverride: null,
      promptExtra: null },
  { dimension: 'simplification', agentType: 'code-gauntlet:code-simplifier', conditionalFlag: DEEP,
    defaultDisposition: 'held_back', defaultTier: 'suggestion', admission: QUALITY_ADMISSION,
    schemaExtra: { behavior_preserved: { type: 'string', description: 'How the proposed simplification preserves observable behavior.' } }, requiredExtra: [], requiredWhenDimension: [], modelOverride: null,
      promptExtra: null },
].map((row) => ({
  core: false,
  defaultDisposition: 'deliver', defaultTier: 'main', aliases: [], admission: null,
  ...row, policySubjects: POLICY_SUBJECTS,
  schemaExtra: { ...row.schemaExtra, ...(row.admission ? {
    [row.admission.field]: { type: 'string', enum: Object.keys(row.admission.kinds), description: row.admission === QUALITY_ADMISSION
      ? 'Select the demonstrated quality basis. Behavior-preserving improvements do not require a production defect. '
        + Object.entries(row.admission.kinds).map(([kind, rule]) => `${kind}: ${rule.description}`).join(' ')
      : 'Test issue kind used to assess admission and production-defect promotion.' },
    ...(row.admission === QUALITY_ADMISSION ? {
      duplicate_of: { ...SOURCE_REFERENCE, description: 'Source reference to the existing implementation duplicated here.' },
      precedent: { ...SOURCE_REFERENCE, description: 'Source reference to an existing repository idiom supporting the change.' },
      behavior_preserved: { type: 'string', description: 'Why the simpler equivalent preserves observable behavior.' },
    } : {}),
    ...row.schemaExtra,
  } : {}) },
  requiredWhenDimension: [...row.requiredWhenDimension, ...(row.admission ? [row.admission.field] : [])],
})));

export const VERIFY_REFERENCE_FIELDS = deepFreeze(Object.keys(SOURCE_REFERENCE.properties));
export const VERIFY_REFERENCE_DESCRIPTORS = deepFreeze(Object.fromEntries(Object.entries(Object.assign({}, ...DIMENSIONS.map((row) => row.schemaExtra)))
  .filter(([, schema]) => schema.$comment === 'source-reference' || schema.items?.$comment === 'source-reference' || schema.$comment === 'search-paths')
  .map(([key, schema]) => [key, schema.$comment === 'search-paths' ? 'search_paths' : schema.type === 'array' ? 'reference_list' : 'reference'])));

const PATH_ROLE_SCHEMA = { type: 'string', enum: ['source', 'test', 'non_source', 'unknown'] };
const DIFF_FACT_SCHEMA = { type: 'object', properties: { in_diff: { type: ['boolean', 'null'] } }, required: ['in_diff'], additionalProperties: false };
const RESOLVED_REFERENCE_SCHEMA = { type: 'object', properties: {
  ...Object.fromEntries(Object.entries(SOURCE_REFERENCE.properties).filter(([key]) => key !== 'anchor')), path_role: PATH_ROLE_SCHEMA, file_added: { type: ['boolean', 'null'] },
  location_status: { type: 'string', enum: LOCATION_STATUSES }, diff_validation: DIFF_FACT_SCHEMA,
}, required: ['file', 'path_role', 'file_added', 'location_status', 'diff_validation'], additionalProperties: false };
const SEARCH_FACT_SCHEMA = { type: 'object', properties: {
  path: { type: 'string' }, available: { type: 'boolean' }, kind: { type: 'string', enum: ['file', 'directory', 'unknown'] },
}, required: ['path', 'available', 'kind'], additionalProperties: false };

export const VERIFY_FACT_PROPERTIES = deepFreeze({
  path_role: PATH_ROLE_SCHEMA, file_added: { type: ['boolean', 'null'] },
  blame_class: { type: 'string', enum: ['new', 'pre_existing', 'surfaced', 'unknown'] },
  blame_source: { type: 'string', enum: ['git_blame', 'local_diff', 'cross_file_refs', 'off_diff', 'removed_loss', 'unavailable'] },
  resolved_references: { type: 'object', properties: Object.fromEntries(Object.entries(VERIFY_REFERENCE_DESCRIPTORS).map(([key, kind]) => {
    const schema = kind === 'search_paths' ? SEARCH_FACT_SCHEMA : RESOLVED_REFERENCE_SCHEMA;
    return [key, kind !== 'reference' ? { type: 'array', items: schema } : schema];
  })), additionalProperties: false },
});

export const ADMISSION_PREDICATES = deepFreeze({
  pr_owned_test: { fact: 'pr_owned_test' },
  confirmed_test_defect: { references: ['primary'], description: 'Actual incorrect assertion, ineffective mock, missing wait, isolation failure or other broken/noisy changed test.' },
  test_diff_site_for_inline: { placement: true },
  criticality_at_least_8: { fact: 'criticality' },
  resolved_production_element: { references: ['production_element'], description: 'The resolved element is production behavior; directory names do not establish this.' },
  complete_searches: { fact: 'searches', references: ['searched_paths', 'candidate_tests'], description: 'Completed coverage investigation inspecting inputs, assertions, helpers and indirect integration coverage, including searches with no candidates.' },
  complete_behavioral_absence: { references: ['production_element', 'searched_paths', 'candidate_tests'], description: 'The entire named behavior has no effective coverage; an extra branch/case or indirect surviving coverage rejects absence.' },
  present_defect: { references: ['primary'], description: 'A present production correctness failure.' },
  resolved_duplicate: { fact: 'duplicate_of' },
  confirmed_duplication: { references: ['primary', 'duplicate_of'], description: 'The resolved source duplicates the same behavior.' },
  concrete_equivalent: { fact: 'equivalent', references: ['primary'], description: 'A concrete equivalent fix exists.' },
  behavior_preserved: { references: ['primary'], description: 'Equivalent return values, side effects and error paths.' },
  resolved_precedent: { fact: 'precedent' },
  precedent_not_added: { fact: 'precedent_not_added' },
  applicable: { references: ['primary', 'precedent'], description: 'The existing repository precedent applies here.' },
  current_defect: { references: ['primary'], description: 'A current code defect, independently of coverage absence or quality preference.' },
  current_reachability: { fact: 'reachability' },
  confirmed_location: { fact: 'location' },
});
const ASSESSMENT = { type: 'object', properties: {
  predicate: { type: 'string', enum: Object.keys(ADMISSION_PREDICATES).filter((key) => ADMISSION_PREDICATES[key].references) },
  status: { type: 'string', enum: ['confirmed', 'rejected', 'unresolved'] },
  references: { type: 'array', items: { type: 'string' } },
}, required: ['predicate', 'status', 'references'], additionalProperties: false };
export const VALIDATION_RETURN_PROPERTIES = deepFreeze({ ...RECURRENCE_RETURN_PROPERTIES,
  policy_subject: { type: 'string', enum: Object.keys(POLICY_SUBJECTS) },
  admission_assessments: { type: 'array', items: ASSESSMENT },
});
export const ADMISSION_STAMP_PROPERTIES = deepFreeze({
  policy_subject: VALIDATION_RETURN_PROPERTIES.policy_subject,
  admission_assessments: VALIDATION_RETURN_PROPERTIES.admission_assessments,
  admission: { type: 'object', properties: {
    version: { type: 'integer', enum: [1] }, input_proof: { type: 'string' },
    input_fields: { type: 'array', items: { type: 'string' } },
    status: { type: 'string', enum: ['admitted', 'report_only', 'held_back'] },
    reasons: { type: 'array', items: { type: 'object', properties: {
      predicate: { type: 'string' }, status: { type: 'string', enum: ['report_only', 'held_back'] },
    }, required: ['predicate', 'status'], additionalProperties: false } },
    kind: { type: 'string' }, core: { type: 'boolean' }, tier: { type: 'string', enum: ['main', 'suggestion'] },
    inline_eligible: { type: 'boolean' },
  }, required: ['version', 'input_proof', 'input_fields', 'status', 'reasons', 'kind', 'core', 'tier', 'inline_eligible'], additionalProperties: false },
  body_fix_task_eligible: { type: 'boolean' },
});
export const VERIFY_FACT_KEYS = deepFreeze(Object.keys(VERIFY_FACT_PROPERTIES));
export const REACHABILITY_VALUES = deepFreeze(['current', 'future_change_only', 'uncertain']);
export const EXCLUSION_STAGES = deepFreeze(['early', 'admission', 'post_validation', 'skeptical']);
export const EXCLUSION_PROOFS = deepFreeze(['verified_fact', 'admission', 'citation']);
export const EXCLUSION_NEW_FACTS = deepFreeze([
  'cross_file_refs', 'in_diff', 'linguist-generated', 'linguist-vendored', 'generated_marker',
  'linguist-documentation', 'all_changed_files_documentation', 'lockfile_basename', 'mechanical_change',
  'admission', 'reachability', 'conventions_confirmation',
]);

export const EXCLUSION_CATEGORIES = deepFreeze([
  { category: 'pre_existing', stage: 'early', predicate: 'blame_pre_existing', facts: ['blame_class', 'blame_source', 'cross_file_refs'],
    applicability: 'Actual git_blame pre-existence with no affected cross-file references.', exceptions: ['cross_file_effect_created_by_pr'],
    proof: 'verified_fact', outcome: 'report_only', reference: 'Origin surfaced, off-diff placement and descriptions do not prove pre-existence.' },
  { category: 'untouched_line', stage: 'early', predicate: 'off_diff', facts: ['in_diff'],
    applicability: 'Verified in_diff=false; unknown is not false.', exceptions: [],
    proof: 'verified_fact', outcome: 'body', reference: 'Placement only after admission and vetting; still competes for challenge capacity.' },
  { category: 'generated_vendored', stage: 'early', predicate: 'generated_or_vendored', facts: ['linguist-generated', 'linguist-vendored', 'generated_marker'],
    applicability: 'git check-attr at captured head or generated markers in the first source lines.', exceptions: ['security_dependency_vulnerability_claim'],
    proof: 'verified_fact', outcome: 'eliminate', reference: 'A conventions word is not classification; the security exception requires no confirmed vulnerable version.' },
  { category: 'docs_only', stage: 'early', predicate: 'documentation_only_code_claim', facts: ['linguist-documentation', 'all_changed_files_documentation', 'path_role'],
    applicability: 'Finding file has a source role and every changed file is documentation, verified at captured head by git check-attr.',
    exceptions: ['documentation_or_comment_claim', 'test_applicable_documented_convention'], proof: 'verified_fact', outcome: 'eliminate',
    reference: 'Extensions and mixed PRs prove neither whole-PR documentation scope nor irrelevance; unknown stays unknown.' },
  { category: 'lockfile', stage: 'early', predicate: 'mechanical_lockfile', facts: ['lockfile_basename', 'mechanical_change'],
    basenames: ['package-lock.json', 'npm-shrinkwrap.json', 'yarn.lock', 'pnpm-lock.yaml', 'Cargo.lock', 'Gemfile.lock', 'poetry.lock', 'Pipfile.lock', 'uv.lock', 'composer.lock', 'packages.lock.json'],
    applicability: 'Closed basename list and verified mechanical scope.', exceptions: ['security_dependency_vulnerability_claim'],
    proof: 'verified_fact', outcome: 'eliminate', reference: 'The security exception is claim-based and needs no prior vulnerable-version confirmation.' },
  { category: 'general_quality', stage: 'admission', predicate: 'canonical_admission', facts: ['admission'],
    applicability: 'Consume canonical quality admission and its present_defect promotion.', exceptions: ['named_in_repo_convention', 'confirmed_present_defect'],
    proof: 'admission', outcome: 'admission', reference: 'A convention finding grounded in an in-repo precedent names the files that establish the pattern; a preference with no named precedent stays excluded.' },
  { category: 'latent', stage: 'post_validation', predicate: 'future_change_only', facts: ['reachability'],
    applicability: 'Existing future-change-only reachability assessment appropriate to the actual claim.', exceptions: ['present_static_defect'],
    proof: 'verified_fact', outcome: 'body', reference: 'Placement after admission and vetting; no invented runtime trigger or early challenge exclusion.' },
  { category: 'test_only_pattern', stage: 'skeptical', predicate: 'harmless_test_pattern', facts: ['path_role'],
    applicability: 'Cited harmless test-role pattern and its applicability; test path alone is insufficient.', exceptions: ['test_coverage_origin', 'test_applicable_convention', 'genuine_test_defect'],
    proof: 'citation', outcome: 'eliminate', reference: 'Wrong assertions, mocks and waits remain defects.' },
  { category: 'lint_catchable', stage: 'skeptical', predicate: 'tool_covers_claim', facts: [],
    applicability: 'Cite applicable repository tool, rule or configuration and coverage of the actual claim.', exceptions: [],
    proof: 'citation', outcome: 'eliminate', reference: 'Familiar syntax and assumed CI tools are insufficient.' },
  { category: 'suppressed', stage: 'skeptical', predicate: 'suppression_covers_claim', facts: [],
    applicability: 'Cited suppression location, range and rule cover this issue.', exceptions: [],
    proof: 'citation', outcome: 'eliminate', reference: 'A nearby suppression does not establish applicability.' },
  { category: 'opted_out', stage: 'skeptical', predicate: 'opt_out_covers_claim', facts: [],
    applicability: 'Cite repository opt-out mechanism and applicable file or rule opt-out.', exceptions: [],
    proof: 'citation', outcome: 'eliminate', reference: 'An author assertion is not an opt-out mechanism.' },
  { category: 'intentional', stage: 'skeptical', predicate: 'confirmed_intent', facts: ['conventions_confirmation'],
    applicability: 'Structured conventions side-record identity in the same file and 10-line bucket, plus independent rule/comment citation.',
    exceptions: ['proven_incorrect_or_dangerous_behavior_including_security'], proof: 'citation', outcome: 'eliminate',
    reference: 'The side record is only a gating fact, never a finding or proof; description and intentional wording are insufficient.' },
].map((row) => ({ ...row, consumers: row.stage === 'skeptical' ? ['validator', 'challenger'] : [row.stage],
  citation_roles: row.proof === 'citation' ? ['rule', 'applicability'] : [] })));
for (const row of EXCLUSION_CATEGORIES) {
  if (![...DISPOSITIONS, 'admission'].includes(row.outcome)) throw new Error(`Unknown exclusion outcome: ${row.outcome}`);
  if (!EXCLUSION_STAGES.includes(row.stage)) throw new Error(`Unknown exclusion stage: ${row.stage}`);
  if (!EXCLUSION_PROOFS.includes(row.proof)) throw new Error(`Unknown exclusion proof: ${row.proof}`);
  for (const fact of row.facts) {
    if (![...VERIFY_FACT_KEYS, ...EXCLUSION_NEW_FACTS].includes(fact)) throw new Error(`Unknown exclusion fact: ${fact}`);
  }
}
export const EXCLUSION_BY_CATEGORY = deepFreeze(Object.fromEntries(EXCLUSION_CATEGORIES.map((row) => [row.category, row])));

const SKEPTICAL_IDENTITY = {
  finding_id: { type: 'string', minLength: 1 }, stage: { type: 'string', enum: ['validator', 'challenger'] },
};
export const CITATION_REQUEST_PROPERTIES = deepFreeze({
  ...SKEPTICAL_IDENTITY, request_id: { type: 'string', minLength: 1 },
  role: { type: 'string', enum: ['claim', 'rule', 'applicability', 'exception'] },
  snapshot_id: { type: 'string', minLength: 1 }, file: { type: 'string', minLength: 1, $comment: 'repo-path' },
  side: { type: 'string', enum: ['head', 'old'] },
  line_start: { type: 'integer', minimum: 1, maximum: Number.MAX_SAFE_INTEGER },
  line_end: { type: 'integer', minimum: 1, maximum: Number.MAX_SAFE_INTEGER },
  quote: { type: 'string', minLength: 1, maxLength: ANCHOR_LIMITS.chars },
});
export const CITATION_REQUEST_SCHEMA = deepFreeze(closedDispatchRow(CITATION_REQUEST_PROPERTIES));
const citationRequests = (roles) => ({ type: 'array', minItems: roles.length,
  maxItems: CITATION_REQUEST_PROPERTIES.role.enum.length, items: CITATION_REQUEST_SCHEMA,
  allOf: roles.map((role) => ({ contains: { type: 'object', properties: { role: { const: role } }, required: ['role'] } })),
});
function scopeAssessmentSchema(stages) {
  const rows = EXCLUSION_CATEGORIES.filter((row) => row.consumers.some((stage) => stages.includes(stage)));
  return { ...closedDispatchRow({
    ...SKEPTICAL_IDENTITY, stage: { type: 'string', enum: stages },
    category: { type: 'string', enum: rows.map((row) => row.category) },
    applicability: { type: 'string', enum: ['applicable', 'inapplicable', 'contradicted'] },
    citations: citationRequests([]),
  }), anyOf: rows.flatMap((row) => stages.filter((stage) => row.consumers.includes(stage)).map((stage) => ({
    properties: { stage: { const: stage }, category: { const: row.category }, citations: citationRequests(row.citation_roles) },
  }))) };
}
export const SCOPE_ASSESSMENT_SCHEMA = deepFreeze(scopeAssessmentSchema(SKEPTICAL_IDENTITY.stage.enum));
export const VERDICT_RETURN_PROPERTIES = deepFreeze({
  ...SKEPTICAL_IDENTITY, verdict: { type: 'string', enum: ['real', 'refuted', 'needs_evidence'] },
  citations: citationRequests(['claim']), missing_evidence: { type: 'string', minLength: 1 },
  scope_assessments: { type: 'array', items: SCOPE_ASSESSMENT_SCHEMA },
});
const VERDICT_VARIANTS = VERDICT_RETURN_PROPERTIES.verdict.enum.map((verdict) => ({
  properties: { verdict: { const: verdict } },
  required: [verdict === 'needs_evidence' ? 'missing_evidence' : 'citations'],
}));
export const SKEPTICAL_RETURN_SCHEMAS = deepFreeze(Object.fromEntries(SKEPTICAL_IDENTITY.stage.enum.map((stage) => {
  const properties = { ...VERDICT_RETURN_PROPERTIES, stage: { type: 'string', enum: [stage] },
    scope_assessments: { type: 'array', items: scopeAssessmentSchema([stage]) },
    ...(stage === 'validator' ? { confidence: { type: 'number' }, justification: { type: 'string' },
      reachability: { type: 'string', enum: REACHABILITY_VALUES }, ...VALIDATION_RETURN_PROPERTIES } : {}) };
  return [stage, { ...closedDispatchRow(properties,
    ['citations', 'missing_evidence', 'justification', 'reachability', 'admission_assessments', 'site_confirmations']), anyOf: VERDICT_VARIANTS }];
})));
export const SKEPTICAL_FINDING_PROPERTIES = deepFreeze({
  skeptical_assessments: { type: 'array', items: closedDispatchRow(VERDICT_RETURN_PROPERTIES, ['citations', 'missing_evidence']) },
  cite_results: { type: 'array', items: closedDispatchRow({
    request: CITATION_REQUEST_SCHEMA, status: { type: 'string', enum: ['matched', 'unmatched', 'rejected', 'unavailable'] },
    detail: { type: 'string' },
  }) },
  scope_facts: { type: 'array', items: closedDispatchRow({
    category: { type: 'string', enum: Object.keys(EXCLUSION_BY_CATEGORY) }, predicate: { type: 'string' },
    value: { type: ['boolean', 'null'] }, snapshot_id: CITATION_REQUEST_PROPERTIES.snapshot_id,
  }) },
  scope_decisions: { type: 'array', items: SCOPE_ASSESSMENT_SCHEMA },
  reduction_provenance: closedDispatchRow({
    finding_id: SKEPTICAL_IDENTITY.finding_id, disposition: { type: 'string', enum: DISPOSITIONS },
    reason: { type: 'string', enum: ['report_only', 'held_back', 'body_routed', 'refuted', 'out_of_scope', 'scope_contradicted', 'cite_unmatched', 'cite_unavailable'] },
    category: { type: 'string', enum: Object.keys(EXCLUSION_BY_CATEGORY) }, stage: SKEPTICAL_IDENTITY.stage,
    request_ids: { type: 'array', items: { type: 'string' } },
  }, ['category', 'stage', 'request_ids']),
});

// Provider schema enforcement can fail; retain the raw entry and bind diagnostics to the dispatched identity.
export function acceptSkepticalReturn(entry, findingId, stage) {
  const errors = [];
  const check = (value, schema, path) => {
    const fail = () => errors.push(path);
    if (schema.const !== undefined && value !== schema.const) fail();
    if (schema.enum && !schema.enum.includes(value)) fail();
    const kind = value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
    const types = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (schema.type && !types.includes(kind) && !(types.includes('integer') && Number.isSafeInteger(value))) { fail(); return; }
    if (kind === 'object') {
      for (const key of schema.required || []) if (!Object.hasOwn(value, key)) errors.push(`${path}.${key}`);
      for (const [key, child] of Object.entries(schema.properties || {})) if (Object.hasOwn(value, key)) check(value[key], child, `${path}.${key}`);
      if (schema.additionalProperties === false) for (const key of Object.keys(value)) if (!Object.hasOwn(schema.properties, key)) errors.push(`${path}.${key}`);
    }
    if (kind === 'array') {
      if (value.length < (schema.minItems || 0) || value.length > (schema.maxItems ?? Infinity)) fail();
      if (schema.items) value.forEach((item, index) => check(item, schema.items, `${path}[${index}]`));
      if (schema.contains && !value.some((item) => matches(item, schema.contains))) fail();
    }
    if (kind === 'string') {
      if (value.trim().length < (schema.minLength || 0) || value.length > (schema.maxLength ?? Infinity)) fail();
      if (schema.$comment === 'repo-path' && (repoRelativeFindingPath('', value).file !== value
        || /\\|^[A-Za-z]:/.test(value))) fail();
    }
    if (kind === 'number' && (!Number.isFinite(value) || value < (schema.minimum ?? -Infinity) || value > (schema.maximum ?? Infinity))) fail();
    for (const clause of schema.allOf || []) check(value, clause, path);
    if (schema.anyOf && !schema.anyOf.some((clause) => matches(value, clause))) fail();
  };
  const matches = (value, schema) => {
    const start = errors.length;
    check(value, schema, '$');
    return errors.splice(start).length === 0;
  };
  const schema = SKEPTICAL_RETURN_SCHEMAS[stage];
  if (schema) check(entry, schema, '$');
  else errors.push('stage');
  if (entry?.finding_id !== findingId) errors.push('finding_id');
  const requests = [...(Array.isArray(entry?.citations) ? entry.citations : []),
    ...(Array.isArray(entry?.scope_assessments) ? entry.scope_assessments.flatMap((scope) => {
      if (scope?.finding_id !== findingId || scope?.stage !== stage) errors.push('scope_identity');
      return Array.isArray(scope?.citations) ? scope.citations : [];
    }) : [])];
  const ids = new Set();
  for (const request of requests) {
    if (!request || request.finding_id !== findingId || request.stage !== stage) errors.push('citation_identity');
    if (ids.has(request?.request_id)) errors.push('request_id');
    ids.add(request?.request_id);
    if (!Number.isSafeInteger(request?.line_start) || !Number.isSafeInteger(request?.line_end)
      || request.line_end < request.line_start || request.line_end - request.line_start >= ANCHOR_LIMITS.lines
      || typeof request.quote !== 'string' || request.quote.split(/\r\n|\r|\n/).length > ANCHOR_LIMITS.lines) errors.push('citation_range');
  }
  return { finding_id: findingId, stage, status: errors.length ? 'incomplete' : 'complete', errors: [...new Set(errors)], raw: entry,
    accepted: Object.fromEntries(Object.entries(entry || {}).filter(([key, value]) => {
      const field = schema?.properties[key];
      return ['confidence', 'policy_subject', 'admission_assessments', 'site_confirmations', 'reachability'].includes(key) && field && matches(value, field);
    })) };
}

export const SKEPTICAL_CONTRACTS = deepFreeze(Object.fromEntries(Object.entries(SKEPTICAL_RETURN_SCHEMAS)
  .map(([stage, schema]) => [stage, { schema, accept: (entry, findingId) => acceptSkepticalReturn(entry, findingId, stage) }])));

export function dimensionRow(dimension) {
  return DIMENSIONS.find((row) => row.dimension === dimension || row.aliases.includes(dimension));
}

const LOSS_ADMISSION = dimensionRow('test_coverage').admission.kinds.critical_gap;
export const REMOVED_LOSS_FIELDS = deepFreeze([...LOSS_ADMISSION.required, ...LOSS_ADMISSION.optional]);
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
          ...Object.fromEntries(REMOVED_LOSS_FIELDS.map((key) => {
            const schema = dimensionRow('test_coverage').schemaExtra[key] || FINDING_PROP_TYPES[key];
            const typed = typeof schema === 'string' ? { type: schema } : schema;
            return [key, typed.type === 'string' ? { ...typed, minLength: 1 } : typed];
          })),
          anchor: FINDING_PROP_TYPES.anchor,
        }, ['anchor', ...LOSS_ADMISSION.optional]) },
      }),
    },
  },
});

export function producerRow(finding) {
  const rows = DIMENSIONS.filter((row) => row.agentType.split(':').pop() === finding.agent);
  return rows.length === 1 && rows[0].admission ? rows[0] : dimensionRow(finding.dimension);
}

export function findingPolicy(finding, effective = true) {
  const original = dimensionRow(finding.origin_dimension);
  const promotion = original?.admission?.kinds[finding[original.admission.field]]?.dimension
    || original?.admission?.kinds[original.admission.promotion?.kind]?.dimension;
  const producer = DIMENSIONS.filter((candidate) => candidate.agentType.split(':').pop() === finding.agent);
  const typedProducer = producer.length === 1 && producer[0].admission ? producer[0] : null;
  const row = typedProducer
    || DIMENSIONS.find((candidate) => typeof finding.dimension === 'string' && candidate.admission?.kinds[finding[candidate.admission.field]]?.dimension === finding.dimension)
    || (original && (original.dimension === finding.dimension || promotion === finding.dimension)
      ? original : null)
    || dimensionRow(finding.dimension) || producer.find((candidate) => candidate.agentFallback) || producer[0];
  const subject = row?.policySubjects[finding.policy_subject];
  const stampedKind = effective && finding.admission?.status === 'admitted' ? finding.admission.kind : undefined;
  const selected = stampedKind && stampedKind === row?.admission?.promotion?.kind ? row
    : subject?.dimension ? dimensionRow(subject.dimension) : row;
  const kind = selected?.admission?.kinds[stampedKind || subject?.kind || finding[selected.admission.field]];
  return { row: selected, kind, core: kind?.core ?? selected?.core ?? false,
    tier: kind?.tier ?? selected?.defaultTier, defaultDisposition: kind?.defaultDisposition ?? selected?.defaultDisposition };
}

// Both the return schema and runtime acceptance derive semantic requirements from the same rows.
export function admissionPolicy(finding, policySubject, promoted = false) {
  const original = findingPolicy({ ...finding, policy_subject: undefined }, false);
  const promotion = original.row?.admission?.promotion;
  const terminal = !promoted && original.kind?.defaultDisposition === 'report_only';
  const policyFinding = promoted && promotion
    ? { ...finding, policy_subject: 'ordinary', [original.row.admission.field]: promotion.kind }
    : { ...finding, policy_subject: policySubject };
  return { original, promotion, terminal, policyFinding,
    ...(terminal ? original : findingPolicy(policyFinding, false)) };
}

export function requiredAssessments(finding, policySubject, promoted = false) {
  const { promotion, kind } = admissionPolicy(finding, policySubject, promoted);
  return [...new Set([promotion?.predicate, ...(kind?.predicates || [])])]
    .filter((predicate) => ADMISSION_PREDICATES[predicate]?.references);
}

export function normalizePipelineStamps(finding) {
  const { row } = findingPolicy({ ...finding, policy_subject: undefined });
  if (finding.origin_dimension !== row?.dimension) delete finding.origin_dimension;
  const subject = row?.policySubjects[finding.policy_subject];
  const promotion = row?.admission?.promotion;
  if ((subject?.stamp !== 'effective_test_kind' || subject.kind !== finding.effective_test_kind)
    && !(promotion?.stamp === 'effective_test_kind' && promotion.kind === finding.effective_test_kind
      && finding.admission?.kind === promotion.kind && finding.admission.status === 'admitted')) {
    delete finding.effective_test_kind;
  }
}

export const FINDING_PATH_DESCRIPTORS = deepFreeze(Object.fromEntries(Object.entries({
  ...FINDING_PROP_TYPES, ...Object.assign({}, ...DIMENSIONS.map((row) => row.schemaExtra)), ...SKEPTICAL_FINDING_PROPERTIES,
}).filter(([, schema]) => JSON.stringify(schema).includes('repo-path'))));
export const FINDING_PATH_ARRAY_FIELDS = deepFreeze(Object.entries(FINDING_PATH_DESCRIPTORS)
  .filter(([, schema]) => schema.type === 'array' && schema.items.$comment === 'repo-path').map(([key]) => key));

// Identifiers must not collide; structural paths and suggestion code retain their own semantics.
export const FINDING_REWRITE_EXEMPT_FIELDS = deepFreeze(['id', 'consolidation_key', 'consolidation_primary', 'delivery_group', 'file', ...Object.keys(FINDING_PATH_DESCRIPTORS), ...Object.keys(ADMISSION_STAMP_PROPERTIES), ...Object.keys(SKEPTICAL_FINDING_PROPERTIES), 'resolved_references', 'site_requests', 'resolved_sites', 'suggested_fix_code', 'removed_test', 'anchor', 'claimed_line_start', 'claimed_line_end']);

// Keep pipeline stamp names code-owned so disclosure can use a safe other fallback for unknown fields.
export const PIPELINE_FINDING_FIELDS = deepFreeze([
  ...Object.keys(ADMISSION_STAMP_PROPERTIES),
  ...Object.keys(SKEPTICAL_FINDING_PROPERTIES),
  'origin_dimension', 'effective_test_kind', 'reference_diagnostics',
  ...VERIFY_FACT_KEYS,
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

export const REPORT_PLACED_FIELDS = deepFreeze([
  'sites',
  'id', 'file', 'line_start', 'line_end', 'title', 'description', 'severity',
  'confidence', 'dimension', 'origin', 'evidence', 'suggestion',
  'claude_md_rule', 'rule_source', 'spec_text', 'cross_file_refs',
]);
