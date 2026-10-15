import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  summarize, discover, agentActive, agentSpecs, deriveAgentFlags, allActiveDimensionsDegraded,
} from '../src/discover.js';
import { DIMENSIONS, FINDING_REQUIRED, FINDING_PROP_TYPES } from '../src/registry.js';
import { finding } from './helpers/findings.js';
import { stageCtx } from './helpers/stageFixtures.js';

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
        required: [...REQUIRED, ...(row.required ?? [])], additionalProperties: false,
        ...(row.allOf ? { allOf: row.allOf } : {}) } },
      complete: { type: 'boolean' }, total_seen: { type: 'number' },
    }, required: ['findings', 'complete', 'total_seen'] });
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
const DISCOVER_GOLDEN = { findings: [], gaps: [], degraded: [], dispatched: ROSTER };
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
  const registrySnapshot = structuredClone({ DIMENSIONS, FINDING_REQUIRED, FINDING_PROP_TYPES });
  const ctx = stageCtx({ replies: structuredClone(row.replies ?? {}), nulls: row.nulls,
    reply: row.mutateSchema ? (_, { schema }) => {
      const item = schema.properties.findings.items;
      item.required.push('host_added');
      item.properties.cross_file_refs.items.type = 'number';
      if (item.properties.affected_consumers) item.properties.affected_consumers.items.type = 'number';
      if (item.properties.criticality) item.properties.criticality.minimum = 0;
      for (const condition of item.allOf ?? []) condition.then.required.push('host_added');
      return { findings: [], complete: true, total_seen: 0 };
    } : undefined });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {}, ...row.input });
  assert.deepEqual(out, row.expected ?? DISCOVER_GOLDEN);
  assert.deepEqual(ctx.calls.map((call) => call.label), (row.expected ?? DISCOVER_GOLDEN).dispatched);
  assert.deepEqual(ctx.violations, []);
  if (row.mutateSchema) assert.deepEqual({ DIMENSIONS, FINDING_REQUIRED, FINDING_PROP_TYPES }, registrySnapshot);
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
  { name: 'missing range drops patch', fields: { suggested_fix_code: 'const x = 1;' },
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

const PROMPT_HEAD = 'This is a code gauntlet built for thoroughness, not speed: investigate using your own methodology '
  + 'and tools (LSP first, Grep fallback) as defined for your role, across the full codebase context around the diff '
  + '\u2014 not just the changed lines. Your dimension(s): ';
const PROMPT_TAIL = '. Report EVERY genuine finding for these dimension(s): there is no cap and no minimum. '
  + 'An empty findings list must reflect a genuine post-investigation absence of issues, never brevity or a quota. '
  + 'Every finding MUST cite concrete evidence: the evidence field must be non-empty and reference real lines '
  + 'you actually inspected (in the diff or in a file you opened) \u2014 a finding you cannot ground in inspected '
  + 'code is noise, do not emit it. For any absence or "missing" claim (e.g. a test-coverage negative asserting no test '
  + 'exists), name in evidence the specific file or path you checked; an unproven absence is not a finding. '
  + 'Return { findings, complete, total_seen }; each finding must match the canonical schema, with description as '
  + 'a single paragraph of prose, at most 500 characters \u2014 no code blocks or bullet lists; put code references '
  + 'in evidence and cross_file_refs instead.';
const TYPO_SWEEP = ' Additionally run an explicit typo and naming sweep: identifier misspellings; typos in user-facing '
  + 'strings, messages, and log output; case-sensitivity mistakes in string comparisons (comparing mixed-case values '
  + 'without normalizing case); and copy-paste plural/singular or off-by-one naming mismatches (a field, key, or '
  + 'variable named for one thing but holding another).';
const SECURITY_SWEEP = ' Additionally sweep explicitly for: SSRF and unvalidated-URL fetches (user-influenced URLs '
  + 'reaching http/request/fetch clients without allowlist validation); frame and embedding policy gaps '
  + '(missing X-Frame-Options or frame-ancestors, clickjacking exposure); postMessage handlers that do not validate '
  + 'event.origin or check it with weak substring matching; and string-matching bypass patterns where a security '
  + 'decision uses containment checks (indexOf/includes/startsWith/contains) on a host, origin, path, or scheme '
  + 'instead of exact parsing \u2014 these are bypassable (e.g. a host "evil.com/trusted.com" still contains "trusted.com").';
const PROMPT_ROWS = [
  { agent: 'code-gauntlet:bug-detector', dims: 'bug', extra: TYPO_SWEEP },
  { agent: 'code-gauntlet:security-reviewer', dims: 'security', extra: SECURITY_SWEEP },
  { agent: 'code-gauntlet:cross-file-impact', dims: 'cross_file_impact' },
  { agent: 'code-gauntlet:test-analyzer', dims: 'test_coverage' },
  { agent: 'code-gauntlet:conventions-and-intent', dims: 'convention, intent, comment_accuracy', extra: TYPO_SWEEP },
  { agent: 'code-gauntlet:type-design-analyzer', dims: 'type_design' },
  { agent: 'code-gauntlet:code-simplifier', dims: 'simplification' },
];
for (const row of PROMPT_ROWS) test(`discovery prompt: ${row.agent}`, async () => {
  const ctx = stageCtx();
  await discover(ctx, { contextLine: 'CONTEXT_SENTINEL ' });
  assert.equal(ctx.calls.find((call) => call.label === row.agent).prompt,
    'CONTEXT_SENTINEL ' + PROMPT_HEAD + row.dims + PROMPT_TAIL + (row.extra ?? ''));
});

test('discovery prompt: absent context has no prefix', async () => {
  const ctx = stageCtx();
  await discover(ctx, {});
  assert.equal(ctx.calls.find((call) => call.label === 'code-gauntlet:bug-detector').prompt,
    PROMPT_HEAD + 'bug' + PROMPT_TAIL + TYPO_SWEEP);
});

const SUMMARY_SCHEMA = { type: 'object', properties: { summary: { type: 'string' } }, required: ['summary'] };
const SINGLE_PROMPT = 'Summarize the semantic intent of these changed files for downstream reviewers: a.js, b.js. '
  + 'Return { summary }.';
const SUMMARY_GOLDEN = { summary: 's', gaps: [] };
const SUMMARY_ROWS = [
  { name: 'absent count', labels: ['summarize'], prompts: [SINGLE_PROMPT] },
  { name: 'small PR count and context prefix', changedLines: 10, contextLine: 'SUMMARY_CONTEXT ',
    labels: ['summarize'], prompts: ['SUMMARY_CONTEXT '
      + 'Summarize the semantic intent of these changed files for downstream reviewers: a.js, b.js. '
      + 'The authoritative changed-line count is 10; cite that number verbatim if you mention change size '
      + '\u2014 never re-estimate it from the diff. Return { summary }.'] },
  { name: 'large count with few files stays single', files: ['a.js'], changedLines: 1211, bucketSize: 20,
    labels: ['summarize'], prompts: [
      'Summarize the semantic intent of these changed files for downstream reviewers: a.js. '
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
      'Summarize the semantic intent of these changed files for downstream reviewers: a.js, b.js. '
        + 'The authoritative changed-line count is 600; cite that number verbatim if you mention change size '
        + '\u2014 never re-estimate it from the diff. Return { summary }.',
      'Summarize the semantic intent of these changed files for downstream reviewers: c.js. '
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
