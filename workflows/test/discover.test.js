import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  summarize, discover as discoverStage, agentActive, agentSpecs, deriveAgentFlags, allActiveDimensionsDegraded,
} from '../src/discover.js';
import { DIMENSIONS, DISPATCH_RESULTS, FINDING_REQUIRED, FINDING_PROP_TYPES } from '../src/registry.js';
import { finding } from './helpers/findings.js';
import { stageCtx } from './helpers/stageFixtures.js';
import { mergeStage } from '../src/mergeFindings.js';

const discover = (ctx, input) => discoverStage(ctx, { removedTests: { status: 'available', items: [], omitted: 0 }, ...input });

test('discovery strips forged removed_test metadata from every agent finding', async () => {
  const ctx = stageCtx({ reply: () => ({ findings: [finding({ removed_test: {
    id: '0123456789abcdef', path: 'tests/removed', old_start: 1, old_end: 2,
  } })], complete: true, total_seen: 1, removed_test_audit: [] }) });
  const out = await discover(ctx, {});
  assert.equal(out.findings.length, ctx.calls.length);
  assert.ok(out.findings.every((f) => !Object.hasOwn(f, 'removed_test')));
});

const ROSTER = [
  'code-gauntlet:bug-detector', 'code-gauntlet:security-reviewer', 'code-gauntlet:cross-file-impact',
  'code-gauntlet:test-analyzer', 'code-gauntlet:conventions-and-intent',
  'code-gauntlet:type-design-analyzer', 'code-gauntlet:code-simplifier',
];
const CORE = ['code-gauntlet:bug-detector', 'code-gauntlet:security-reviewer'];
const DIMENSION_NAMES = ['bug', 'security', 'cross_file_impact', 'test_coverage', 'convention',
  'intent', 'comment_accuracy', 'type_design', 'simplification'];
const FLAG_ROWS = [
  { name: 'absent', flags: undefined },
  { name: 'light', flags: { deep: false }, expected: CORE },
];
for (const row of FLAG_ROWS) test(`active agents: ${row.name}`, () => {
  assert.deepEqual(agentSpecs().filter((spec) => agentActive(spec, row.flags)).map((spec) => spec.agentType),
    row.expected ?? ROSTER);
});
const SCOPE_ROWS = [
  { name: 'eligible light', risks: [{ path: 'a.js', risk: 'low' }], lines: 10, answer: 'light', expected: { deep: false } },
  { name: 'eligible full', risks: [{ path: 'a.js', risk: 'low' }], lines: 10, answer: 'full' },
  { name: 'mixed risk', risks: [{ path: 'a.js', risk: 'low' }, { path: 'b.js', risk: 'medium' }], lines: 10, answer: 'light' },
  { name: 'line threshold', risks: [{ path: 'a.js', risk: 'low' }], lines: 50, answer: 'light' },
];
for (const row of SCOPE_ROWS) test(`derived scope: ${row.name}`, () => {
  assert.deepEqual(deriveAgentFlags(row.risks, row.lines, row.answer), row.expected ?? {});
});
const DEGRADED_ROWS = [
  { name: 'one healthy single-dimension agent', dispatched: ROSTER,
    degraded: ['security', 'cross_file_impact', 'test_coverage', 'convention', 'intent',
      'comment_accuracy', 'type_design', 'simplification'], expected: false },
  { name: 'all active', dispatched: ROSTER, degraded: DIMENSION_NAMES, expected: true },
  { name: 'empty healthy', dispatched: [], degraded: [], expected: false },
  { name: 'empty failed', dispatched: [], degraded: ['bug', 'security'], expected: true },
  { name: 'light relative', dispatched: CORE, degraded: ['bug', 'security'], expected: true },
];
for (const row of DEGRADED_ROWS) test(`degradation: ${row.name}`, () => {
  assert.equal(allActiveDimensionsDegraded(row.dispatched, row.degraded), row.expected);
});

const CANONICAL_PROPS = {
  id: { type: 'string' }, file: { type: 'string' }, line_start: { type: 'number' }, line_end: { type: 'number' },
  title: { type: 'string' }, description: { type: 'string' }, severity: { type: 'string' }, confidence: { type: 'number' },
  dimension: { type: 'string' }, origin: { type: 'string' }, evidence: { type: 'string' },
  anchor: { type: 'string', minLength: 1, description: 'Exact text, copied from the source at the PR head, of the 1 to 4 contiguous lines in file this finding is about; at most 400 characters.' },
  suggestion: { type: 'string' }, claude_md_rule: { type: 'string' }, suggested_fix_code: { type: 'string' },
  cross_file_refs: { type: 'array', items: { type: 'string' } },
};
const REQUIRED = ['id', 'file', 'line_start', 'title', 'description', 'severity', 'confidence', 'dimension'];
const CONDITIONAL = [
  { if: { properties: { dimension: { const: 'convention' } }, required: ['dimension'] }, then: { required: ['claude_md_rule'] } },
  { if: { properties: { dimension: { const: 'intent' } }, required: ['dimension'] }, then: { required: ['spec_text'] } },
];
const SCHEMA_ROWS = [
  { agent: 'code-gauntlet:bug-detector', extra: { hidden_errors: { type: 'string' } } },
  { agent: 'code-gauntlet:security-reviewer',
    extra: { attack_vector: { type: 'string' } }, required: ['attack_vector'] },
  { agent: 'code-gauntlet:cross-file-impact',
    extra: { affected_consumers: { type: 'array', items: { type: 'string' } } }, required: ['affected_consumers'] },
  { agent: 'code-gauntlet:test-analyzer',
    extra: { criticality: { type: 'number', minimum: 1, maximum: 10 }, failure_scenario: { type: 'string' } },
    required: ['criticality', 'failure_scenario'] },
  { agent: 'code-gauntlet:conventions-and-intent',
    extra: { rule_source: { type: 'string' }, spec_text: { type: 'string' } },
    allOf: CONDITIONAL },
  { agent: 'code-gauntlet:type-design-analyzer',
    extra: { invalid_state_example: { type: 'string' } } },
  { agent: 'code-gauntlet:code-simplifier',
    extra: { behavior_preserved: { type: 'string' } }, required: ['behavior_preserved'] },
];
test('finding schemas: one dispatch supplies the roster', async (t) => {
  const ctx = stageCtx();
  await discover(ctx, { policy: {} });
  assert.deepEqual(ctx.calls.map((call) => call.label), ROSTER);
  assert.deepEqual(FINDING_REQUIRED, REQUIRED);
  assert.deepEqual(ctx.violations, []);
  for (const row of SCHEMA_ROWS) await t.test(row.agent, () => {
    const schema = ctx.calls.find((call) => call.label === row.agent).schema;
    assert.deepEqual(schema, { type: 'object', properties: {
      findings: { type: 'array', items: { type: 'object', properties: { ...CANONICAL_PROPS, ...row.extra },
        required: [...REQUIRED, 'anchor', ...(row.required ?? [])], additionalProperties: false,
        ...(row.allOf ? { allOf: row.allOf } : {}) } },
      complete: { type: 'boolean' }, total_seen: { type: 'number' },
      ...(DISPATCH_RESULTS[row.agent] || {}),
    }, required: ['findings', 'complete', 'total_seen', ...Object.keys(DISPATCH_RESULTS[row.agent] || {})] });
  });
});
const POLICY_ROWS = [
  { name: 'bedrock', policy: { provider: 'bedrock' } },
  { name: 'first party gateway', policy: { provider: 'firstParty', gateway: true } },
];
for (const row of POLICY_ROWS) test(`conditional schema policy: ${row.name}`, async () => {
  const ctx = stageCtx();
  await discover(ctx, { policy: row.policy });
  for (const call of ctx.calls) {
    const item = call.schema.properties.findings.items;
    assert.equal(item.allOf, undefined);
    assert.deepEqual(item.properties.dimension, { type: 'string' });
  }
});

const NULL_SECURITY = 'code-gauntlet:security-reviewer: agent returned null (dispatch failed) '
  + '\u2014 dimensions security not covered';
const NULL_CONVENTIONS = 'code-gauntlet:conventions-and-intent: agent returned null (dispatch failed) '
  + '\u2014 dimensions convention/intent/comment_accuracy not covered';
const DISCOVER_GOLDEN = { findings: [], gaps: [], degraded: [], dispatched: ROSTER, removedTestAudit: { status: 'ran-clean' } };
const OUTCOME_ROWS = [
  { name: 'host can mutate schema copies without changing registry fragments', mutateSchema: true },
  { name: 'light empty', input: { agentFlags: { deep: false } }, expected: { ...DISCOVER_GOLDEN, dispatched: CORE } },
  { name: 'null security preserves sibling and short agent name', nulls: ['code-gauntlet:security-reviewer'],
    replies: { 'code-gauntlet:bug-detector': { findings: [{ id: 'F1' }], complete: true, total_seen: 1 } },
    expected: { ...DISCOVER_GOLDEN, findings: [{ id: 'F1', agent: 'bug-detector' }],
      gaps: [NULL_SECURITY], degraded: ['security'] } },
  { name: 'active multidimension null', nulls: ['code-gauntlet:conventions-and-intent'], expected: { ...DISCOVER_GOLDEN,
    gaps: [NULL_CONVENTIONS + ' (dispatch carried the conditional per-dimension schema)'],
    degraded: ['convention', 'intent', 'comment_accuracy'] } },
  { name: 'inactive multidimension null', input: { policy: { provider: 'bedrock' } },
    nulls: ['code-gauntlet:conventions-and-intent'], expected: { ...DISCOVER_GOLDEN,
      gaps: [NULL_CONVENTIONS], degraded: ['convention', 'intent', 'comment_accuracy'] } },
  { name: 'incomplete is soft', replies: {
    'code-gauntlet:bug-detector': { findings: [], complete: false, total_seen: 999 },
  }, expected: { ...DISCOVER_GOLDEN, gaps: [
    'code-gauntlet:bug-detector: possibly incomplete (complete=false, total_seen=999) \u2014 dimensions bug',
  ] } },
  { name: 'malformed is degraded', replies: { 'code-gauntlet:bug-detector': { findings: {} } },
    expected: { ...DISCOVER_GOLDEN, gaps: ['code-gauntlet:bug-detector: malformed result (no findings array)'], degraded: ['bug'] } },
  { name: 'total seen reaches cap', input: { limits: { discoveryCap: 2 } },
    replies: { 'code-gauntlet:bug-detector': { findings: [], complete: true, total_seen: 2 } },
    expected: { ...DISCOVER_GOLDEN, gaps: [
      'code-gauntlet:bug-detector: possibly incomplete (complete=true, total_seen=2) \u2014 dimensions bug',
    ] } },
];
for (const row of OUTCOME_ROWS) test(`discover outcome: ${row.name}`, async () => {
  const registrySnapshot = structuredClone({ DIMENSIONS, DISPATCH_RESULTS, FINDING_REQUIRED, FINDING_PROP_TYPES });
  const ctx = stageCtx({ replies: structuredClone(row.replies ?? {}), nulls: row.nulls,
    reply: row.mutateSchema ? (_, { schema }) => {
      const item = schema.properties.findings.items;
      item.required.push('host_added');
      item.properties.cross_file_refs.items.type = 'number';
      if (item.properties.affected_consumers) item.properties.affected_consumers.items.type = 'number';
      if (item.properties.criticality) item.properties.criticality.minimum = 0;
      for (const condition of item.allOf ?? []) condition.then.required.push('host_added');
      return { findings: [], complete: true, total_seen: 0, removed_test_audit: [] };
    } : undefined });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {}, ...row.input });
  assert.deepEqual(out, row.expected ?? DISCOVER_GOLDEN);
  assert.deepEqual(ctx.calls.map((call) => call.label), (row.expected ?? DISCOVER_GOLDEN).dispatched);
  assert.deepEqual(ctx.violations, []);
  if (row.mutateSchema) assert.deepEqual({ DIMENSIONS, DISPATCH_RESULTS, FINDING_REQUIRED, FINDING_PROP_TYPES }, registrySnapshot);
});

const SPAN_GAP = 'discover: 1 finding(s) had an implausible line_end (non-integer, before line_start, '
  + 'or spanning more than maxLineSpan=100 lines) \u2014 line_end dropped, finding kept anchored at line_start';
const RANGE_GOLDEN = { line_start: 68, end: false, patch: false, gaps: [] };
const RANGE_ROWS = [
  { name: 'implausible with patch', fields: { line_end: 940, suggested_fix_code: 'const x = 1;' },
    expected: { ...RANGE_GOLDEN, gaps: [SPAN_GAP
      + ' \u2014 1 of those also carried suggested_fix_code: patch field(s) dropped along with the range'] } },
  { name: 'reversed range with patch', fields: { line_end: 60, suggested_fix_code: 'const x = 1;' },
    expected: { ...RANGE_GOLDEN, gaps: [SPAN_GAP
      + ' \u2014 1 of those also carried suggested_fix_code: patch field(s) dropped along with the range'] } },
  { name: 'kept patch', fields: { line_end: 78, suggested_fix_code: 'const x = 1;' },
    expected: { ...RANGE_GOLDEN, end: true, line_end: 78, patch: true, suggested_fix_code: 'const x = 1;' } },
  { name: 'missing range drops patch', fields: { line_end: undefined, suggested_fix_code: 'const x = 1;' },
    expected: { ...RANGE_GOLDEN, gaps: [
      'discover: 1 finding(s) emitted suggested_fix_code without line_end \u2014 field dropped (a patch must state its range)',
    ] } },
];
for (const row of RANGE_ROWS) test(`discovery range: ${row.name}`, async () => {
  const ctx = stageCtx({ replies: { 'code-gauntlet:bug-detector': {
    findings: [finding({ file: 'a.js', line_start: 68, ...row.fields })], complete: true, total_seen: 1,
  } } });
  const out = await discover(ctx, { limits: row.limits });
  const expected = row.expected ?? RANGE_GOLDEN;
  assert.equal(out.findings.length, 1);
  const f = out.findings[0];
  assert.equal(f.line_start, expected.line_start);
  assert.equal(Object.hasOwn(f, 'line_end'), expected.end);
  assert.equal(f.line_end, expected.line_end);
  assert.equal(Object.hasOwn(f, 'suggested_fix_code'), expected.patch);
  assert.equal(f.suggested_fix_code, expected.suggested_fix_code);
  assert.deepEqual(out.gaps, expected.gaps);
});

const PROMPT_ROWS = [
  { agent: 'code-gauntlet:bug-detector', dims: 'bug' },
  { agent: 'code-gauntlet:security-reviewer', dims: 'security' },
  { agent: 'code-gauntlet:cross-file-impact', dims: 'cross_file_impact' },
  { agent: 'code-gauntlet:test-analyzer', dims: 'test_coverage' },
  { agent: 'code-gauntlet:conventions-and-intent', dims: 'convention, intent, comment_accuracy' },
  { agent: 'code-gauntlet:type-design-analyzer', dims: 'type_design' },
  { agent: 'code-gauntlet:code-simplifier', dims: 'simplification' },
];
for (const contextLine of ['CONTEXT_SENTINEL ', '']) test(`discovery transport: context=${Boolean(contextLine)}`, async () => {
  const inventory = { status: 'available', items: [{ id: 'AUDIT_SENTINEL', path: 'deleted.test' }], omitted: 0 };
  const ctx = stageCtx();
  await discover(ctx, { contextLine, removedTests: inventory });
  assert.deepEqual(ctx.calls.map((call) => call.label), PROMPT_ROWS.map((row) => row.agent));
  for (const row of PROMPT_ROWS) {
    const prompt = ctx.calls.find((call) => call.label === row.agent).prompt;
    assert.ok(prompt.startsWith(contextLine + 'Investigate'));
    assert.ok(prompt.includes(`Your dimension(s): ${row.dims}.`));
    assert.ok(prompt.includes('LSP first, Grep fallback'));
    assert.ok(prompt.includes('full codebase context around the diff'));
    assert.ok(prompt.includes('no cap and no minimum'));
    assert.ok(prompt.includes('regardless of importance or confidence'));
    assert.ok(prompt.includes('metadata as unverified hypotheses'));
    assert.ok(prompt.includes('including paths it omits'));
    assert.ok(prompt.includes('evidence field must be non-empty') && prompt.includes('real lines you actually inspected'));
    assert.ok(prompt.includes('name in evidence the specific file or path you checked'));
    assert.ok(prompt.includes('Return { findings, complete, total_seen }'));
    assert.ok(prompt.includes('single paragraph of prose, at most 500 characters'));
    assert.doesNotMatch(prompt, /Additionally/);
    if (row.agent === 'code-gauntlet:test-analyzer') {
      assert.ok(prompt.includes('required dispatch-result fields: removed_test_audit'));
      assert.ok(prompt.includes('exactly one removed_test_audit row per id'));
      assert.ok(prompt.endsWith(`Inventory: ${JSON.stringify(inventory)}`));
    }
  }
});

const SUMMARY_SCHEMA = { type: 'object', properties: { summary: { type: 'string' } }, required: ['summary'] };
const SINGLE_PROMPT = 'Summarize the semantic intent of these changed files for report change context: a.js, b.js. '
  + 'Return { summary }.';
const SUMMARY_GOLDEN = { summary: 's', gaps: [] };
const SUMMARY_ROWS = [
  { name: 'absent count', labels: ['summarize'], prompts: [SINGLE_PROMPT] },
  { name: 'small PR count and context prefix', changedLines: 10, contextLine: 'SUMMARY_CONTEXT ',
    labels: ['summarize'], prompts: ['SUMMARY_CONTEXT '
      + 'Summarize the semantic intent of these changed files for report change context: a.js, b.js. '
      + 'The authoritative changed-line count is 10; cite that number verbatim if you mention change size '
      + '\u2014 never re-estimate it from the diff. Return { summary }.'] },
  { name: 'large count with few files stays single', files: ['a.js'], changedLines: 1211, bucketSize: 20,
    labels: ['summarize'], prompts: [
      'Summarize the semantic intent of these changed files for report change context: a.js. '
      + 'The authoritative changed-line count is 1211; cite that number verbatim if you mention change size '
      + '\u2014 never re-estimate it from the diff. Return { summary }.',
    ] },
  { name: 'single throw', changedLines: 10, nulls: ['summarize'], labels: ['summarize'],
    expected: { summary: '', gaps: ['summarize failed'] } },
  { name: 'all buckets null', changedLines: 600, nulls: ['summarize-bucket-0', 'summarize-bucket-1'],
    labels: ['summarize-bucket-0', 'summarize-bucket-1'], expected: { summary: '', gaps: ['summarize failed'] } },
  { name: 'bucket files and merge preserve partials and count', files: ['a.js', 'b.js', 'c.js'], changedLines: 600,
    bucketSize: 2, replies: { 'summarize-bucket-0': { summary: 'p0' }, 'summarize-bucket-1': { summary: 'p1' },
      'summarize-merge': { summary: 'merged' } }, labels: ['summarize-bucket-0', 'summarize-bucket-1', 'summarize-merge'],
    prompts: [
      'Summarize the semantic intent of these changed files for report change context: a.js, b.js. '
        + 'The authoritative changed-line count is 600; cite that number verbatim if you mention change size '
        + '\u2014 never re-estimate it from the diff. Return { summary }.',
      'Summarize the semantic intent of these changed files for report change context: c.js. '
        + 'The authoritative changed-line count is 600; cite that number verbatim if you mention change size '
        + '\u2014 never re-estimate it from the diff. Return { summary }.',
      'Combine these per-bucket change summaries into one concise semantic summary. '
        + 'The authoritative changed-line count is 600; cite that number verbatim if you mention change size '
        + '\u2014 never re-estimate it. Partials:\np0\n---\np1\nReturn { summary }.',
    ], expected: { summary: 'merged', gaps: [] } },
];
for (const row of SUMMARY_ROWS) test(`summarize: ${row.name}`, async () => {
  const ctx = stageCtx({ reply: { summary: 's' }, replies: row.replies, nulls: row.nulls });
  const out = await summarize(ctx, { changedFiles: row.files ?? ['a.js', 'b.js'], changedLines: row.changedLines,
    contextLine: row.contextLine, limits: { summarizeBucketSize: row.bucketSize ?? 1 } });
  assert.deepEqual(out, row.expected ?? SUMMARY_GOLDEN);
  assert.deepEqual(ctx.calls.map((call) => call.label), row.labels);
  if (row.prompts) assert.deepEqual(ctx.calls.map((call) => call.prompt), row.prompts);
  for (const call of ctx.calls) assert.deepEqual(call.schema, SUMMARY_SCHEMA);
  assert.deepEqual(ctx.violations, []);
});

test('discovery rejects forged location authority', async () => {
  const ctx = stageCtx({ reply: () => ({ findings: [
    finding({ id: 'quoted', anchor: 'perform_action(value)', location_status: 'confirmed', claimed_line_start: 1, claimed_line_end: 4, line_end: undefined, suggested_fix_code: 'fixed(value)' }),
    finding({ id: 'legacy', anchor: undefined, location_status: 'relocated', claimed_line_start: 5 }),
  ], complete: true, total_seen: 2, removed_test_audit: [] }) });
  const out = await discover(ctx, {});
  assert.equal(out.findings.length, ctx.calls.length * 2);
  for (const f of out.findings) {
    for (const key of ['location_status', 'claimed_line_start', 'claimed_line_end']) assert.equal(Object.hasOwn(f, key), false);
    if (f.id === 'quoted') {
      assert.equal(f.anchor, 'perform_action(value)');
      assert.equal(Object.hasOwn(f, 'suggested_fix_code'), false);
    }
  }
});

test('anchor is required only at discovery dispatch', async () => {
  const ctx = stageCtx();
  await discover(ctx, {});
  for (const call of ctx.calls) {
    const item = call.schema.properties.findings.items;
    assert.equal(item.additionalProperties, false);
    assert.ok(item.required.includes('anchor'), `${call.label}: missing anchor must fail StructuredOutput`);
    assert.equal(item.properties.anchor.minLength, 1, `${call.label}: empty anchor must fail StructuredOutput`);
  }
  const authored = finding({ id: 'removed-test:0123456789abcdef:1', agent: 'test-analyzer',
    dimension: 'test_coverage', anchor: undefined, location_status: 'unconfirmed',
    removed_test: { id: '0123456789abcdef', path: 'tests/removed', old_start: 1, old_end: 2 } });
  const merged = mergeStage({ findings: [authored], dispatched: ['code-gauntlet:test-analyzer'] }, {});
  assert.deepEqual(merged.findings, [authored]);
  assert.deepEqual(merged.methodology.validation_warnings, []);
});
