import { test } from 'node:test';
import assert from 'node:assert/strict';
import { prIdentity } from './helpers/argsFixtures.js';
import { DIMENSIONS, AGENTS, AGENT_LABELS, FINDING_PROP_TYPES, FINDING_PATH_ARRAY_FIELDS, FINDING_REQUIRED } from '../src/registry.js';
import { STAGE_DEFAULTS, RULE_SOURCE_LABELS, RULE_SOURCE_LABEL_FALLBACK } from '../src/registry.js';
import { PR_IDENTITY_FIELDS, PERMALINK_TEMPLATES } from '../src/registry.js';
import { SHA_FULL_RE, WEB_ORIGIN_RE, resolvePolicy, conditionalSchemaActive, modelFor, identityCheck } from '../src/registry.js';

const POLICY_ROWS = [
  { name: 'discovery default', agent: 'code-gauntlet:bug-detector', input: undefined, expected: { model: 'claude-sonnet-5-5' } },
  { name: 'security override', agent: 'code-gauntlet:security-reviewer', input: {}, expected: { model: 'claude-opus-5-5' } },
  { name: 'stage default ignores legacy frontier', agent: 'code-gauntlet:challenger',
    input: { frontier: true, frontierModelId: 'claude-fable-5' }, expected: { model: 'claude-sonnet-5-5' } },
  { name: 'unknown agent', agent: 'code-gauntlet:unknown', input: {}, expected: { model: 'claude-sonnet-5-5' } },
  { name: 'explicit model', agent: 'code-gauntlet:security-reviewer', input: { subagentModelEnv: 'claude-haiku-4-5' },
    expected: { model: 'claude-haiku-4-5' } },
  { name: 'alias override', agent: 'code-gauntlet:bug-detector', input: { subagentModelEnv: 'sonnet' },
    expected: { model: 'claude-sonnet-5-5' } },
  { name: 'third-party default', agent: 'code-gauntlet:bug-detector', input: { provider: 'bedrock' }, expected: { model: 'sonnet' } },
  { name: 'third-party override', agent: 'code-gauntlet:security-reviewer', input: { provider: 'bedrock' },
    expected: { model: 'opus' } },
  { name: 'third-party deployment', agent: 'code-gauntlet:bug-detector',
    input: { provider: 'bedrock', subagentModelEnv: 'us.anthropic.claude-sonnet-4-6' },
    expected: { model: 'us.anthropic.claude-sonnet-4-6' } },
  { name: 'third-party alias override', agent: 'code-gauntlet:bug-detector',
    input: { provider: 'bedrock', subagentModelEnv: 'sonnet' }, expected: { model: 'sonnet' } },
  { name: 'vertex stage default', agent: 'code-gauntlet:executor', input: { provider: 'vertex' },
    expected: { model: 'sonnet' } },
  { name: 'foundry stage default', agent: 'code-gauntlet:executor', input: { provider: 'foundry' },
    expected: { model: 'sonnet' } },
  { name: 'unknown provider stage default', agent: 'code-gauntlet:executor', input: { provider: 'someday-provider' },
    expected: { model: 'sonnet' } },
  { name: 'null provider', agent: 'code-gauntlet:bug-detector', input: { provider: null }, expected: { model: 'claude-sonnet-5-5' } },
  { name: 'first-party', agent: 'code-gauntlet:bug-detector', input: { provider: 'firstParty' },
    expected: { model: 'claude-sonnet-5-5' } },
  { name: 'copied dimension', agent: 'code-gauntlet:bug-detector', input: {},
    table: DIMENSIONS.map((row) => ({ ...row, modelOverride: row.dimension === 'bug' ? 'haiku' : row.modelOverride })),
    expected: { model: 'claude-haiku-4-5-20251001' } },
];
for (const { name, agent, input, table, expected } of POLICY_ROWS) {
  test(`policy: ${name}`, () => {
    assert.deepEqual(resolvePolicy(agent, input, table), expected);
    assert.equal(modelFor(agent, { ...input, subagentModel: input?.subagentModelEnv }, table), expected.model);
  });
}

const SCHEMA_ROWS = [
  { name: 'absent', input: undefined, expected: true },
  { name: 'null provider', input: { provider: null }, expected: true },
  { name: 'first-party', input: { provider: 'firstParty' }, expected: true },
  { name: 'bedrock', input: { provider: 'bedrock' }, expected: false },
  { name: 'gateway', input: { provider: 'firstParty', gateway: true }, expected: false },
  { name: 'direct', input: { gateway: false }, expected: true },
];
for (const { name, input, expected } of SCHEMA_ROWS) {
  test(`conditional schema: ${name}`, () => assert.equal(conditionalSchemaActive(input), expected));
}

const DATA_ROWS = [
  { name: 'SHA pattern', input: String(SHA_FULL_RE), expected: '/^[0-9a-f]{40}$/' },
  { name: 'dimension order', input: DIMENSIONS.map((row) => row.dimension), expected: [
    'bug', 'security', 'cross_file_impact', 'test_coverage', 'convention', 'intent', 'comment_accuracy', 'type_design', 'simplification',
  ] },
  { name: 'stages', input: STAGE_DEFAULTS,
    expected: { validator: 'sonnet', challenger: 'sonnet', executor: 'sonnet', 'artifact-writer': 'sonnet' } },
  { name: 'labels', input: AGENT_LABELS, expected: {
    'code-gauntlet:bug-detector': 'Correctness & Error Handling', 'code-gauntlet:security-reviewer': 'Security',
    'code-gauntlet:cross-file-impact': 'Cross-file Impact', 'code-gauntlet:test-analyzer': 'Test Coverage',
    'code-gauntlet:conventions-and-intent': 'Conventions & Intent', 'code-gauntlet:type-design-analyzer': 'Type Design',
    'code-gauntlet:code-simplifier': 'Code Simplification',
  } },
  { name: 'agent order', input: AGENTS, expected: [
    'code-gauntlet:bug-detector', 'code-gauntlet:security-reviewer', 'code-gauntlet:cross-file-impact',
    'code-gauntlet:test-analyzer', 'code-gauntlet:conventions-and-intent', 'code-gauntlet:type-design-analyzer',
    'code-gauntlet:code-simplifier',
  ] },
  { name: 'identity fields', input: PR_IDENTITY_FIELDS.map(({ name, required, describe }) => ({ name, required, describe })),
    expected: [
      { name: 'owner', required: true, describe: 'a non-empty string' },
      { name: 'repo', required: true, describe: 'a non-empty string' },
      { name: 'pr_number', required: true, describe: 'a positive safe integer' },
      { name: 'sha_full', required: true, describe: 'a 40-character lowercase hex commit id' },
      { name: 'platform', required: true, describe: 'one of github, gitlab' },
      { name: 'web_origin', required: true, describe: 'an http(s) origin: scheme, host and optional port only' },
      { name: 'title', required: false, describe: 'a non-empty string when present' },
    ] },
  { name: 'permalink bytes', input: PERMALINK_TEMPLATES, expected: {
    github: { blob: '{origin}/{owner}/{repo}/blob/{sha}/{path}', line: '#L{start}', range: '#L{start}-L{end}',
      ref: '#{number}', refUrl: '{origin}/{owner}/{repo}/pull/{number}' },
    gitlab: { blob: '{origin}/{owner}/{repo}/-/blob/{sha}/{path}', line: '#L{start}', range: '#L{start}-{end}',
      ref: '!{number}', refUrl: '{origin}/{owner}/{repo}/-/merge_requests/{number}' },
  } },
  { name: 'rule labels', input: RULE_SOURCE_LABELS, expected: {
    documented_rule: 'Cited rule', code_comment: 'Cited comment', repo_precedent: 'Repo precedent', self_inconsistency: 'Inconsistency',
  } },
  { name: 'rule fallback', input: RULE_SOURCE_LABEL_FALLBACK, expected: 'Cited rule' },
  { name: 'required finding fields', input: FINDING_REQUIRED,
    expected: ['id', 'file', 'line_start', 'title', 'description', 'severity', 'confidence', 'dimension'] },
  { name: 'path array fields', input: FINDING_PATH_ARRAY_FIELDS, expected: ['cross_file_refs', 'affected_consumers'] },
  { name: 'canonical schema', input: FINDING_PROP_TYPES, expected: {
    id: 'string', file: 'string', line_start: 'number', line_end: 'number', title: 'string', description: 'string', severity: 'string',
    confidence: 'number', dimension: 'string', origin: 'string', evidence: 'string', suggestion: 'string', claude_md_rule: 'string',
    suggested_fix_code: 'string', cross_file_refs: { type: 'array', items: { type: 'string' } },
  } },
];
for (const { name, input, expected } of DATA_ROWS) {
  test(`registry data: ${name}`, () => assert.deepEqual(input, expected));
}

const DIMENSION_ROWS = [
  { name: 'bug', expected: ['code-gauntlet:bug-detector', null, { hidden_errors: 'string' }, [], [], null] },
  { name: 'security', expected: ['code-gauntlet:security-reviewer', null, { attack_vector: 'string' }, ['attack_vector'], [], 'opus'] },
  { name: 'cross_file_impact', expected: ['code-gauntlet:cross-file-impact', 'deep',
    { affected_consumers: { type: 'array', items: { type: 'string' } } }, ['affected_consumers'], [], null] },
  { name: 'test_coverage', expected: ['code-gauntlet:test-analyzer', 'deep',
    { criticality: { type: 'number', minimum: 1, maximum: 10 }, failure_scenario: 'string' },
    ['criticality', 'failure_scenario'], [], null] },
  { name: 'convention', expected: ['code-gauntlet:conventions-and-intent', 'deep', { rule_source: 'string' },
    [], ['claude_md_rule'], null] },
  { name: 'intent', expected: ['code-gauntlet:conventions-and-intent', 'deep', { spec_text: 'string' }, [], ['spec_text'], null] },
  { name: 'comment_accuracy', expected: ['code-gauntlet:conventions-and-intent', 'deep', {}, [], [], null] },
  { name: 'type_design', expected: ['code-gauntlet:type-design-analyzer', 'deep', { invalid_state_example: 'string' }, [], [], null] },
  { name: 'simplification', expected: ['code-gauntlet:code-simplifier', 'deep',
    { behavior_preserved: 'string' }, ['behavior_preserved'], [], null] },
];
for (const { name, expected } of DIMENSION_ROWS) {
  test(`dimension: ${name}`, () => {
    const row = DIMENSIONS.find((d) => d.dimension === name);
    assert.deepEqual([row.agentType, row.conditionalFlag, row.schemaExtra, row.requiredExtra,
      row.requiredWhenDimension, row.modelOverride], expected);
    assert.ok(Object.isFrozen(row));
    assert.ok(Object.isFrozen(row.schemaExtra));
    assert.ok(Object.isFrozen(row.requiredExtra));
  });
}

const PATTERN_ROWS = [
  { name: 'SHA lowercase', pattern: SHA_FULL_RE, input: 'dddddddddddddddddddddddddddddddddddddddd', expected: true },
  { name: 'SHA uppercase', pattern: SHA_FULL_RE, input: 'D'.repeat(40), expected: false },
  { name: 'SHA length', pattern: SHA_FULL_RE, input: 'd'.repeat(39), expected: false },
  { name: 'SHA excess length', pattern: SHA_FULL_RE, input: 'd'.repeat(41), expected: false },
  { name: 'origin IPv6', pattern: WEB_ORIGIN_RE, input: 'http://[::1]:8443', expected: true },
  { name: 'origin port bound', pattern: WEB_ORIGIN_RE, input: 'https://git.example:65535', expected: true },
  { name: 'origin port overflow', pattern: WEB_ORIGIN_RE, input: 'https://git.example:65536', expected: false },
];
for (const { name, pattern, input, expected } of PATTERN_ROWS) {
  test(`pattern: ${name}`, () => assert.equal(pattern.test(input), expected));
}

const IDENTITY_ROWS = [
  { name: 'unknown field', input: {}, key: 'unknown', expected: false },
  { name: 'copied field', input: { owner: 'long' }, key: 'owner',
    table: PR_IDENTITY_FIELDS.map((row) => ({ ...row, check: row.name === 'owner' ? (value) => value === 'short' : row.check })),
    expected: false },
  { name: 'origin type', input: prIdentity({ web_origin: 1 }), key: 'web_origin', expected: false },
  { name: 'origin total length', input: { web_origin: `https://${'a'.repeat(63)}.${'a'.repeat(63)}.${'a'.repeat(63)}.${'a'.repeat(63)}` },
    key: 'web_origin', expected: false },
];
for (const { name, input, key, table, expected } of IDENTITY_ROWS) {
  test(`identity check: ${name}`, () => assert.equal(identityCheck(input, key, table), expected));
}

const PROMPT_ROWS = [
  { name: 'security', input: ['security'], expected:
    'Additionally sweep explicitly for: SSRF and unvalidated-URL fetches (user-influenced URLs ' +
    'reaching http/request/fetch clients without allowlist validation); frame and embedding ' +
    'policy gaps (missing X-Frame-Options or frame-ancestors, clickjacking exposure); ' +
    'postMessage handlers that do not validate event.origin or check it with weak substring ' +
    'matching; and string-matching bypass patterns where a security decision uses containment ' +
    'checks (indexOf/includes/startsWith/contains) on a host, origin, path, or scheme instead of ' +
    'exact parsing \u2014 these are bypassable (e.g. a host "evil.com/trusted.com" still contains ' +
    '"trusted.com").' },
  { name: 'typo', input: ['bug', 'convention', 'intent', 'comment_accuracy'], expected:
    'Additionally run an explicit typo and naming sweep: identifier misspellings; typos in ' +
    'user-facing strings, messages, and log output; case-sensitivity mistakes in string ' +
    'comparisons (comparing mixed-case values without normalizing case); and copy-paste ' +
    'plural/singular or off-by-one naming mismatches (a field, key, or variable named for one ' +
    'thing but holding another).' },
  { name: 'none', input: ['cross_file_impact', 'test_coverage', 'type_design', 'simplification'], expected: null },
];
for (const { name, input, expected } of PROMPT_ROWS) {
  test(`prompt: ${name}`, () => {
    for (const dimension of input) assert.equal(DIMENSIONS.find((d) => d.dimension === dimension).promptExtra, expected);
  });
}
