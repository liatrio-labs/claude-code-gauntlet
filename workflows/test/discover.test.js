import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  summarize, discover, agentActive, agentSpecs, intersectRequiredExtra, deriveAgentFlags, allActiveDimensionsDegraded,
} from '../src/discover.js';
import { DIMENSIONS, FINDING_REQUIRED } from '../src/registry.js';
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
  { name: 'absent', flags: undefined }, { name: 'empty', flags: {} },
  { name: 'unrelated flag', flags: { someOtherFlag: false } },
  { name: 'light', flags: { deep: false }, expected: CORE },
  { name: 'hostile core flags', flags: { deep: false, bug: false, security: false }, expected: CORE },
  { name: 'true', flags: { deep: true } }, { name: 'zero', flags: { deep: 0 } },
  { name: 'null', flags: { deep: null } }, { name: 'string false', flags: { deep: 'false' } },
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
  { name: 'ineligible absent answer', risks: [{ path: 'a.js', risk: 'medium' }], lines: 10 },
];
for (const row of SCOPE_ROWS) test(`derived scope: ${row.name}`, () => {
  assert.deepEqual(deriveAgentFlags(row.risks, row.lines, row.answer), row.expected ?? {});
});
const DEGRADED_ROWS = [
  { name: 'one healthy single-dimension agent', dispatched: ROSTER,
    degraded: ['security', 'cross_file_impact', 'test_coverage', 'convention', 'intent',
      'comment_accuracy', 'type_design', 'simplification'], expected: false },
  { name: 'all active', dispatched: ROSTER, degraded: DIMENSION_NAMES, expected: true },
  { name: 'partial multidimension failure', dispatched: ROSTER,
    degraded: ['convention', 'intent', 'comment_accuracy'], expected: false },
  { name: 'empty healthy', dispatched: [], degraded: [], expected: false },
  { name: 'empty failed', dispatched: [], degraded: ['bug', 'security'], expected: true },
  { name: 'unresolvable failed', dispatched: ['some:renamed-agent'], degraded: ['bug'], expected: true },
  { name: 'light relative', dispatched: CORE, degraded: ['bug', 'security'], expected: true },
];
for (const row of DEGRADED_ROWS) test(`degradation: ${row.name}`, () => {
  assert.equal(allActiveDimensionsDegraded(row.dispatched, row.degraded), row.expected);
});

const INTERSECTION_ROWS = [
  { name: 'one sibling requires', rows: [{ requiredExtra: ['x'] }, { requiredExtra: [] }], expected: [] },
  { name: 'all siblings require', rows: [{ requiredExtra: ['x'] }, { requiredExtra: ['x'] }], expected: ['x'] },
  { name: 'empty', rows: [], expected: [] },
];
for (const row of INTERSECTION_ROWS) test(`required intersection: ${row.name}`, () => {
  assert.deepEqual(intersectRequiredExtra(row.rows), row.expected);
});
const SPEC_GOLDEN = [{ agentType: 'synthetic:agent', dimensions: ['dim_a', 'dim_b'], schemaExtra: {},
  conditionalFlags: [null, null], promptExtra: null, requiredExtra: [], conditionalRequired: [] }];
const SPEC_ROWS = [
  { name: 'unshared requirement removed', dims: [
    { agentType: 'synthetic:agent', dimension: 'dim_a', conditionalFlag: null, requiredExtra: ['x'] },
    { agentType: 'synthetic:agent', dimension: 'dim_b', conditionalFlag: null, requiredExtra: [] },
  ] },
  { name: 'shared requirement retained', dims: [
    { agentType: 'synthetic:agent', dimension: 'dim_a', conditionalFlag: null, requiredExtra: ['x'] },
    { agentType: 'synthetic:agent', dimension: 'dim_b', conditionalFlag: null, requiredExtra: ['x'] },
  ], expected: [{ ...SPEC_GOLDEN[0], requiredExtra: ['x'] }] },
  { name: 'alternate registry with missing conditional key', dims: [
    { agentType: 'synthetic:only-here', dimension: 'dim_z', conditionalFlag: null, schemaExtra: {}, requiredExtra: [] },
  ], expected: [{ ...SPEC_GOLDEN[0], agentType: 'synthetic:only-here', dimensions: ['dim_z'], conditionalFlags: [null] }] },
  { name: 'conditional requirements sorted independently of dimensions', dims: [
    { agentType: 'synthetic:agent', dimension: 'dim_b', conditionalFlag: null,
      schemaExtra: { y: 'string' }, requiredExtra: [], requiredWhenDimension: ['y'] },
    { agentType: 'synthetic:agent', dimension: 'dim_a', conditionalFlag: null,
      schemaExtra: { x: 'string' }, requiredExtra: [], requiredWhenDimension: ['x'] },
  ], expected: [{ ...SPEC_GOLDEN[0], dimensions: ['dim_b', 'dim_a'], schemaExtra: { y: 'string', x: 'string' },
    conditionalRequired: [{ dimension: 'dim_a', required: ['x'] }, { dimension: 'dim_b', required: ['y'] }] }] },
];
for (const row of SPEC_ROWS) test(`agent specs: ${row.name}`, () => {
  assert.deepEqual(agentSpecs(row.dims), row.expected ?? SPEC_GOLDEN);
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
  { agent: 'code-gauntlet:bug-detector', dimensions: ['bug'], flags: [null], extra: { hidden_errors: { type: 'string' } } },
  { agent: 'code-gauntlet:security-reviewer', dimensions: ['security'], flags: [null],
    extra: { attack_vector: { type: 'string' } }, required: ['attack_vector'] },
  { agent: 'code-gauntlet:cross-file-impact', dimensions: ['cross_file_impact'], flags: ['deep'],
    extra: { affected_consumers: { type: 'array', items: { type: 'string' } } }, required: ['affected_consumers'] },
  { agent: 'code-gauntlet:test-analyzer', dimensions: ['test_coverage'], flags: ['deep'],
    extra: { criticality: { type: 'number', minimum: 1, maximum: 10 }, failure_scenario: { type: 'string' } },
    required: ['criticality', 'failure_scenario'] },
  { agent: 'code-gauntlet:conventions-and-intent', dimensions: ['convention', 'intent', 'comment_accuracy'],
    flags: ['deep', 'deep', 'deep'], extra: { rule_source: { type: 'string' }, spec_text: { type: 'string' } },
    conditional: [{ dimension: 'convention', required: ['claude_md_rule'] }, { dimension: 'intent', required: ['spec_text'] }],
    allOf: CONDITIONAL },
  { agent: 'code-gauntlet:type-design-analyzer', dimensions: ['type_design'], flags: ['deep'],
    extra: { invalid_state_example: { type: 'string' } } },
  { agent: 'code-gauntlet:code-simplifier', dimensions: ['simplification'], flags: ['deep'],
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
    const spec = agentSpecs().find((value) => value.agentType === row.agent);
    assert.deepEqual(spec.dimensions, row.dimensions);
    assert.deepEqual(spec.conditionalFlags, row.flags);
    assert.deepEqual(spec.requiredExtra, row.required ?? []);
    assert.deepEqual(spec.conditionalRequired, row.conditional ?? []);
    assert.equal(Object.hasOwn(spec, 'rows'), false);
  });
});
const POLICY_ROWS = [
  { name: 'default', policy: {}, allOf: CONDITIONAL },
  { name: 'first party', policy: { provider: 'firstParty' }, allOf: CONDITIONAL },
  { name: 'null provider', policy: { provider: null }, allOf: CONDITIONAL },
  { name: 'undefined provider no gateway', policy: { provider: undefined, gateway: false }, allOf: CONDITIONAL },
  { name: 'bedrock', policy: { provider: 'bedrock' } }, { name: 'vertex', policy: { provider: 'vertex' } },
  { name: 'foundry', policy: { provider: 'foundry' } },
  { name: 'first party gateway', policy: { provider: 'firstParty', gateway: true } },
  { name: 'default gateway', policy: { gateway: true } },
];
for (const row of POLICY_ROWS) test(`conditional schema policy: ${row.name}`, async () => {
  const ctx = stageCtx();
  await discover(ctx, { policy: row.policy });
  for (const call of ctx.calls) {
    const item = call.schema.properties.findings.items;
    assert.deepEqual(item.allOf, call.label === 'code-gauntlet:conventions-and-intent' ? row.allOf : undefined);
    assert.deepEqual(item.properties.dimension, { type: 'string' });
  }
});

const NULL_SECURITY = 'code-gauntlet:security-reviewer: agent returned null (dispatch failed) '
  + '\u2014 dimensions security not covered';
const NULL_CONVENTIONS = 'code-gauntlet:conventions-and-intent: agent returned null (dispatch failed) '
  + '\u2014 dimensions convention/intent/comment_accuracy not covered';
const DISCOVER_GOLDEN = { findings: [], gaps: [], degraded: [], dispatched: ROSTER };
const OUTCOME_ROWS = [
  { name: 'full empty' },
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
  { name: 'finding count reaches cap independently', input: { limits: { discoveryCap: 1 } },
    replies: { 'code-gauntlet:bug-detector': { findings: [{ id: 'F1' }], complete: true, total_seen: 0 } },
    expected: { ...DISCOVER_GOLDEN, findings: [{ id: 'F1', agent: 'bug-detector' }], gaps: [
      'code-gauntlet:bug-detector: possibly incomplete (complete=true, total_seen=0) \u2014 dimensions bug',
    ] } },
  { name: 'remaining single-dimension agents fail', nulls: [
    'code-gauntlet:bug-detector', 'code-gauntlet:cross-file-impact', 'code-gauntlet:test-analyzer',
    'code-gauntlet:type-design-analyzer', 'code-gauntlet:code-simplifier',
  ], expected: { ...DISCOVER_GOLDEN,
    degraded: ['bug', 'cross_file_impact', 'test_coverage', 'type_design', 'simplification'], gaps: [
    'code-gauntlet:bug-detector: agent returned null (dispatch failed) \u2014 dimensions bug not covered',
    'code-gauntlet:cross-file-impact: agent returned null (dispatch failed) \u2014 dimensions cross_file_impact not covered',
    'code-gauntlet:test-analyzer: agent returned null (dispatch failed) \u2014 dimensions test_coverage not covered',
    'code-gauntlet:type-design-analyzer: agent returned null (dispatch failed) \u2014 dimensions type_design not covered',
    'code-gauntlet:code-simplifier: agent returned null (dispatch failed) \u2014 dimensions simplification not covered',
  ] } },
];
for (const row of OUTCOME_ROWS) test(`discover outcome: ${row.name}`, async () => {
  const ctx = stageCtx({ replies: structuredClone(row.replies ?? {}), nulls: row.nulls });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {}, ...row.input });
  assert.deepEqual(out, row.expected ?? DISCOVER_GOLDEN);
  assert.deepEqual(ctx.calls.map((call) => call.label), (row.expected ?? DISCOVER_GOLDEN).dispatched);
  assert.deepEqual(ctx.violations, []);
});

const SPAN_GAP = 'discover: 1 finding(s) had an implausible line_end (non-integer, before line_start, '
  + 'or spanning more than maxLineSpan=100 lines) \u2014 line_end dropped, finding kept anchored at line_start';
const RANGE_GOLDEN = { line_start: 68, end: false, patch: false, gaps: [] };
const RANGE_ROWS = [
  { name: 'implausible with patch', fields: { line_end: 940, suggested_fix_code: 'const x = 1;' },
    expected: { ...RANGE_GOLDEN, gaps: [SPAN_GAP
      + ' \u2014 1 of those also carried suggested_fix_code: patch field(s) dropped along with the range'] } },
  { name: 'implausible without patch', fields: { line_end: 940 }, expected: { ...RANGE_GOLDEN, gaps: [SPAN_GAP] } },
  { name: 'kept patch', fields: { line_end: 78, suggested_fix_code: 'const x = 1;' },
    expected: { ...RANGE_GOLDEN, end: true, line_end: 78, patch: true, suggested_fix_code: 'const x = 1;' } },
  { name: 'reverse range', fields: { line_start: 100, line_end: 50 },
    expected: { ...RANGE_GOLDEN, line_start: 100, gaps: [SPAN_GAP] } },
  { name: 'fractional end', fields: { line_end: 70.5 }, expected: { ...RANGE_GOLDEN, gaps: [SPAN_GAP] } },
  { name: 'single line', fields: { line_end: 68 }, expected: { ...RANGE_GOLDEN, end: true, line_end: 68 } },
  { name: 'span at ceiling', fields: { line_end: 168 }, expected: { ...RANGE_GOLDEN, end: true, line_end: 168 } },
  { name: 'no range no patch', fields: {} },
  { name: 'missing range drops patch', fields: { suggested_fix_code: 'const x = 1;' },
    expected: { ...RANGE_GOLDEN, gaps: [
      'discover: 1 finding(s) emitted suggested_fix_code without line_end \u2014 field dropped (a patch must state its range)',
    ] } },
  { name: 'narrowed span', fields: { line_end: 78 }, limits: { maxLineSpan: 5 }, expected: { ...RANGE_GOLDEN,
    gaps: ['discover: 1 finding(s) had an implausible line_end (non-integer, before line_start, '
      + 'or spanning more than maxLineSpan=5 lines) \u2014 line_end dropped, finding kept anchored at line_start'] } },
  { name: 'widened span', fields: { line_end: 940 }, limits: { maxLineSpan: 1000 },
    expected: { ...RANGE_GOLDEN, end: true, line_end: 940 } },
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
  const spec = agentSpecs().find((value) => value.agentType === row.agent);
  assert.equal(spec.promptExtra, row.extra ? row.extra.slice(1) : null);
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
  { name: 'zero count', changedLines: 0, labels: ['summarize'], prompts: [SINGLE_PROMPT] },
  { name: 'negative count', changedLines: -1, labels: ['summarize'], prompts: [SINGLE_PROMPT] },
  { name: 'string count buckets but carries no count pin', changedLines: '601', labels: [
    'summarize-bucket-0', 'summarize-bucket-1', 'summarize-merge',
  ], prompts: [
    'Summarize the semantic intent of these changed files for downstream reviewers: a.js. Return { summary }.',
    'Summarize the semantic intent of these changed files for downstream reviewers: b.js. Return { summary }.',
    'Combine these per-bucket change summaries into one concise semantic summary. Partials:\ns\n---\ns\nReturn { summary }.',
  ] },
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
];
for (const row of SUMMARY_ROWS) test(`summarize: ${row.name}`, async () => {
  const ctx = stageCtx({ reply: { summary: 's' }, nulls: row.nulls });
  const out = await summarize(ctx, { changedFiles: row.files ?? ['a.js', 'b.js'], changedLines: row.changedLines,
    contextLine: row.contextLine, limits: { summarizeBucketSize: row.bucketSize ?? 1 } });
  assert.deepEqual(out, row.expected ?? SUMMARY_GOLDEN);
  assert.deepEqual(ctx.calls.map((call) => call.label), row.labels);
  if (row.prompts) assert.deepEqual(ctx.calls.map((call) => call.prompt), row.prompts);
  for (const call of ctx.calls) assert.deepEqual(call.schema, SUMMARY_SCHEMA);
  assert.deepEqual(ctx.violations, []);
});
test('registry scope: literal core and deep dimensions', () => {
  assert.deepEqual(DIMENSIONS.filter((d) => d.conditionalFlag === null).map((d) => d.dimension).sort(), ['bug', 'security']);
  assert.deepEqual(DIMENSIONS.filter((d) => d.conditionalFlag === 'deep').map((d) => d.dimension).sort(), [
    'comment_accuracy', 'convention', 'cross_file_impact', 'intent', 'simplification', 'test_coverage', 'type_design',
  ]);
});
