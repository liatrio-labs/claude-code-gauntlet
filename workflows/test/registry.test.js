import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DIMENSIONS } from '../src/registry.js';
import { WEB_ORIGIN_RE, resolvePolicy, modelFor } from '../src/registry.js';
import { ANCHOR_LIMITS, SKEPTICAL_RETURN_SCHEMAS, SKEPTICAL_CONTRACTS, CITATION_REQUEST_SCHEMA, EXCLUSION_CATEGORIES, EXCLUSION_NEW_FACTS, PIPELINE_FINDING_FIELDS, SKEPTICAL_FINDING_PROPERTIES, FINDING_PATH_DESCRIPTORS, FINDING_REWRITE_EXEMPT_FIELDS, acceptSkepticalReturn } from '../src/registry.js';
import { agentSpecs, findingSchema, discover } from '../src/discover.js';
import { normalizeFindingPaths } from '../src/mergeFindings.js';

test('skeptical descriptors derive verdict and scope variants', async () => {
  const request = (stage, extra = {}) => ({ finding_id: 'F', stage, request_id: 'C', role: 'claim',
    snapshot_id: 'snapshot', file: 'src/service', side: 'head', line_start: 1, line_end: 1, quote: 'return value', ...extra });
  for (const stage of ['validator', 'challenger']) {
    const schema = SKEPTICAL_RETURN_SCHEMAS[stage];
    assert.deepEqual(schema.properties.verdict.enum, ['real', 'refuted', 'needs_evidence']);
    const validation = stage === 'validator' ? { confidence: 91, policy_subject: 'ordinary',
      admission_assessments: [{ predicate: 'present_defect', status: 'confirmed', references: ['primary'] }] } : {};
    const entry = { finding_id: 'F', stage, verdict: 'real', scope_assessments: [], citations: [request(stage)], ...validation };
    const accept = (extra) => SKEPTICAL_CONTRACTS[stage].accept({ ...entry, ...extra }, 'F');
    for (const verdict of ['real', 'refuted']) {
      assert.equal(accept({ verdict }).status, 'complete');
      const withoutProof = { ...entry, verdict };
      delete withoutProof.citations;
      assert.equal(acceptSkepticalReturn(withoutProof, 'F', stage).status, 'incomplete');
    }
    const needs = { ...entry, verdict: 'needs_evidence', missing_evidence: 'Caller input' };
    delete needs.citations;
    assert.equal(acceptSkepticalReturn(needs, 'F', stage).status, 'complete');
    delete needs.missing_evidence;
    assert.equal(acceptSkepticalReturn(needs, 'F', stage).status, 'incomplete');
    for (const extra of [{ verdict: null }, { verdict: 'invented' }, { scope_assessments: null }, { scope_assessments: undefined },
      { finding_id: 'foreign' }, { stage: 'foreign' }, { citations: [] }, { citations: null }]) {
      const result = accept(extra);
      assert.equal(result.status, 'incomplete', JSON.stringify(extra));
      assert.deepEqual(result.raw, { ...entry, ...extra });
      assert.equal(result.finding_id, 'F');
    }
    for (const extra of [{ file: '../escape' }, { file: '/escape' }, { file: 'C:/escape' }, { file: 'a\\b' },
      { snapshot_id: '' }, { side: 'other' }, { line_start: 0 }, { line_start: 1.5 }, { line_end: Number.MAX_SAFE_INTEGER + 1 },
      { line_start: 2, line_end: 1 }, { line_end: 9 }, { quote: 'x'.repeat(401) }, { quote: 'x\n'.repeat(8) + 'x' },
      { role: 'rule' }, { stage: 'foreign' }, { finding_id: 'foreign' }]) {
      assert.equal(accept({ citations: [request(stage, extra)] }).status, 'incomplete', JSON.stringify(extra));
    }
    assert.equal(accept({ citations: [request(stage, { line_end: 8, quote: 'x'.repeat(400) })] }).status, 'complete');
    assert.equal(accept({ citations: [request(stage), request(stage)] }).status, 'incomplete');
    for (const row of EXCLUSION_CATEGORIES) {
      const scope = { finding_id: 'F', stage, category: row.category, applicability: 'applicable',
        citations: row.citation_roles.map((role) => request(stage, { request_id: role, role })) };
      assert.equal(accept({ scope_assessments: [scope] }).status,
        row.consumers.includes(stage) ? 'complete' : 'incomplete', `${stage}: ${row.category}`);
      if (row.citation_roles.length) assert.equal(accept({ scope_assessments: [{ ...scope, citations: [] }] }).status, 'incomplete');
    }
    assert.equal(accept({ scope_assessments: [{ category: 'unknown' }] }).status, 'incomplete');
    if (stage === 'validator') {
      for (const [key, value] of [['confidence', null], ['policy_subject', 'unknown'], ['admission_assessments', [{}]]]) {
        const result = accept({ [key]: value });
        assert.equal(result.status, 'incomplete');
        assert.equal(result.raw[key], value);
      }
    }
  }
  for (const value of [null, [], 'invalid', {}]) assert.equal(acceptSkepticalReturn(value, 'F', 'validator').status, 'incomplete');
  assert.equal(acceptSkepticalReturn({}, 'F', 'unknown').status, 'incomplete');
  assert.equal(CITATION_REQUEST_SCHEMA.properties.quote.maxLength, ANCHOR_LIMITS.chars);
  const owned = Object.keys(SKEPTICAL_FINDING_PROPERTIES);
  for (const name of owned) {
    assert.ok(PIPELINE_FINDING_FIELDS.includes(name));
    assert.ok(FINDING_REWRITE_EXEMPT_FIELDS.includes(name));
    for (const spec of agentSpecs()) {
      const schema = findingSchema(spec, {}).properties.findings.items;
      assert.equal(schema.additionalProperties, false);
      assert.equal(Object.hasOwn(schema.properties, name), false);
    }
  }
  for (const name of ['skeptical_assessments', 'cite_results']) assert.ok(FINDING_PATH_DESCRIPTORS[name]);
  const proof = { finding_id: 'F', stage: 'validator', verdict: 'real', scope_assessments: [], citations: [request('validator')] };
  const normalized = normalizeFindingPaths([{ id: 'F', file: 'src/service', skeptical_assessments: [proof] }], '/repo').valid[0];
  assert.deepEqual(normalized.skeptical_assessments, [proof]);
  const unsafe = normalizeFindingPaths([{ id: 'F', file: 'src/service', skeptical_assessments: [{ ...proof,
    citations: [request('validator', { file: '../escape' })] }] }], '/repo');
  assert.equal(unsafe.valid[0].skeptical_assessments[0].citations[0].file, '../escape');
  assert.ok(unsafe.warnings.length);
  const forged = Object.fromEntries(owned.map((key) => [key, 'forged']));
  const discovered = await discover({ parallel: async (thunks) => Promise.all(thunks.map((fn) => fn())),
    agent: async () => ({ findings: [{ ...forged, id: 'F', file: 'src/service', line_start: 1, title: 'Defect',
      description: 'A real defect', severity: 'high', confidence: 90, dimension: 'bug', anchor: 'return value' }], complete: true, total_seen: 1 }) }, {});
  assert.ok(discovered.findings.length);
  for (const finding of discovered.findings) for (const key of owned) assert.equal(Object.hasOwn(finding, key), false);
});

test('scope schemas bind category enums and variants to their consumers', () => {
  const schemas = [...Object.entries(SKEPTICAL_RETURN_SCHEMAS).map(([stage, schema]) =>
    [[stage], schema.properties.scope_assessments.items]),
  [['early', 'post_validation'], SKEPTICAL_FINDING_PROPERTIES.scope_decisions.items]];
  for (const [stages, schema] of schemas) {
    const rows = EXCLUSION_CATEGORIES.filter((row) => row.consumers.some((stage) => stages.includes(stage)));
    assert.deepEqual(schema.properties.category.enum, rows.map((row) => row.category));
    assert.deepEqual(schema.anyOf.map((variant) => [variant.properties.stage.const, variant.properties.category.const]),
      rows.flatMap((row) => stages.filter((stage) => row.consumers.includes(stage)).map((stage) => [stage, row.category])));
  }
});

test('exclusion rows use verified blame facts and defer latent placement until after validation', () => {
  const row = (category) => EXCLUSION_CATEGORIES.find((item) => item.category === category);
  assert.equal(row('latent').stage, 'post_validation');
  assert.deepEqual(row('latent').consumers, ['post_validation']);
  assert.deepEqual(row('pre_existing').facts, ['blame_class', 'blame_source', 'cross_file_refs']);
  assert.ok(EXCLUSION_NEW_FACTS.includes('in_diff'));
});

for (const [name, before, after, error] of [
  ['fact', "facts: ['in_diff']", "facts: ['in_dif']", 'Unknown exclusion fact: in_dif'],
  ['outcome', "outcome: 'body'", "outcome: 'bodi'", 'Unknown exclusion outcome: bodi'],
  ['stage', "stage: 'post_validation'", "stage: 'post_validaton'", 'Unknown exclusion stage: post_validaton'],
  ['proof', "proof: 'verified_fact'", "proof: 'verified_fat'", 'Unknown exclusion proof: verified_fat'],
]) {
  test(`module load rejects an unknown exclusion ${name}`, async () => {
    const original = readFileSync(new URL('../src/registry.js', import.meta.url), 'utf8');
    assert.ok(original.includes(before));
    const source = original.replace(before, after).replace(/from '(\.\/[^']+)'/g,
      (_, path) => `from '${new URL(path, new URL('../src/registry.js', import.meta.url)).href}'`);
    assert.notEqual(source, original);
    await assert.rejects(import(`data:text/javascript,${encodeURIComponent(source)}`), { message: error });
  });
}

const POLICY_ROWS = [
  { name: 'third-party alias override', agent: 'code-gauntlet:bug-detector',
    input: { provider: 'bedrock', subagentModelEnv: 'sonnet' }, expected: { model: 'sonnet' } },
  { name: 'vertex stage default', agent: 'code-gauntlet:executor', input: { provider: 'vertex' },
    expected: { model: 'sonnet' } },
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

const PATTERN_ROWS = [
  { name: 'origin port bound', pattern: WEB_ORIGIN_RE, input: 'https://git.example:65535', expected: true },
  { name: 'origin port overflow', pattern: WEB_ORIGIN_RE, input: 'https://git.example:65536', expected: false },
];
for (const { name, pattern, input, expected } of PATTERN_ROWS) {
  test(`pattern: ${name}`, () => assert.equal(pattern.test(input), expected));
}
