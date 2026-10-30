import { test } from 'node:test';
import assert from 'node:assert/strict';
import { merge, mergeStage, validateFindings, normalizeFindingPaths, dedupById } from '../src/mergeFindings.js';
import { finding } from './helpers/findings.js';
import { discover } from '../src/discover.js';
import { stageCtx } from './helpers/stageFixtures.js';
import { normalizePhaseFindings } from '../src/checkpoints.js';

const META = { agents: ['bug-detector'], base_branch: 'main', head_sha: 'abc123full', pr_number: 42, owner: 'org', repo: 'myrepo' };
const KNOWN_DIMENSIONS = ['bug', 'security', 'cross_file_impact', 'test_coverage', 'convention',
  'intent', 'comment_accuracy', 'type_design', 'simplification'];
for (const dimension of KNOWN_DIMENSIONS) test(`known dimension: ${dimension}`, () => {
  const out = validateFindings([finding({ dimension })]);
  assert.equal(out.valid.length, 1);
  assert.deepEqual(out.warnings, []);
});
const VALIDATION_ROWS = [
  { name: 'unknown dimension stays with its own label', fields: { dimension: 'new_dimension' }, kept: 1,
    warning: "[f1] Unknown dimension 'new_dimension' \u2014 finding kept with warning" },
  { name: 'missing dimension stays', fields: { dimension: undefined }, kept: 1,
    warning: "[f1] Missing 'dimension' field \u2014 finding kept with warning" },
  { name: 'missing file rejects', fields: { file: undefined }, kept: 0,
    warning: "[f1] Missing required field 'file' \u2014 finding rejected" },
  { name: 'missing line_start rejects', fields: { line_start: undefined }, kept: 0,
    warning: "[f1] Missing required field 'line_start' \u2014 finding rejected" },
  { name: 'missing description rejects', fields: { description: undefined }, kept: 0,
    warning: "[f1] Missing required field 'description' \u2014 finding rejected" },
  { name: 'missing confidence rejects', fields: { confidence: undefined }, kept: 0,
    warning: "[f1] Missing required field 'confidence' \u2014 finding rejected" },
];
for (const row of VALIDATION_ROWS) test(`finding validation: ${row.name}`, () => {
  const out = validateFindings([finding(row.fields)]);
  assert.equal(out.valid.length, row.kept);
  assert.deepEqual(out.warnings, [row.warning]);
});
const DEDUP_GOLDEN = { ids: ['B'], dropped: 0, duplicates: 1 };
const DEDUP_ROWS = [
  { name: 'idless dropped', input: [{ file: 'x.py', title: 'no id' }, finding({ id: 'B' })],
    expected: { ids: ['B'], dropped: 1, duplicates: 0 } },
  { name: 'duplicate retains first', input: [finding({ id: 'B' }), finding({ id: 'B', title: 'second' })] },
];
for (const row of DEDUP_ROWS) test(`id dedup: ${row.name}`, () => {
  const out = dedupById(row.input);
  assert.deepEqual({ ids: out.merged.map((f) => f.id), dropped: out.droppedNoId, duplicates: out.duplicatesResolved },
    row.expected ?? DEDUP_GOLDEN);
});

const MERGE_GOLDEN = { ids: ['f1'], count: 1, dropped: 0, duplicates: 0, warnings: [] };
const MERGE_ROWS = [
  { name: 'three structured findings keep order and count', input: { 'bug-detector': [
    finding({ id: 'bug-0' }), finding({ id: 'bug-1' }), finding({ id: 'bug-2' }),
  ] }, expected: { ...MERGE_GOLDEN, ids: ['bug-0', 'bug-1', 'bug-2'], count: 3 } },
  { name: 'empty structured run has valid envelope', input: {}, expected: { ...MERGE_GOLDEN, ids: [], count: 0 } },
  { name: 'structured finding stamps its dispatched agent', input: { 'bug-detector': [finding({ id: 'bug-1', agent: 'wrong-agent' })] },
    expected: { ...MERGE_GOLDEN, ids: ['bug-1'] }, stamped: ['bug-detector'] },
  { name: 'multiple structured agents combine', agents: ['bug-detector', 'security-reviewer'], input: {
    'bug-detector': [finding({ id: 'b' })], 'security-reviewer': [finding({ id: 's', dimension: 'security' })],
  }, expected: { ...MERGE_GOLDEN, ids: ['b', 's'], count: 2 }, stamped: ['bug-detector', 'security-reviewer'] },
  { name: 'idless structured finding counted as dropped', input: { 'bug-detector': [finding({ id: undefined })] },
    expected: { ...MERGE_GOLDEN, ids: [], count: 0, dropped: 1,
      warnings: ["[<no id>] Missing required field 'id' \u2014 finding rejected"] } },
  { name: 'extra fields survive structured merge', input: { 'bug-detector': [finding({ risk_level: 8, custom_receipt: { x: 2 } })] },
    pass: { risk_level: 8, custom_receipt: { x: 2 } } },
  { name: 'missing title rejects with the literal warning', input: { 'bug-detector': [finding({ title: undefined })] },
    expected: { ...MERGE_GOLDEN, ids: [], count: 0,
      warnings: ["[f1] Missing required field 'title' \u2014 finding rejected"] } },
  { name: 'earlier invalid duplicate loses to later valid duplicate', input: { 'bug-detector': [
    finding({ id: 'F1', title: undefined }), finding({ id: 'F1', title: 'valid winner' }),
  ] }, expected: { ...MERGE_GOLDEN, ids: ['F1'], warnings: ["[F1] Missing required field 'title' \u2014 finding rejected"] },
    pass: { title: 'valid winner' } },
  { name: 'cross-agent duplicate follows roster order', agents: ['bug-detector', 'security-reviewer'], input: {
    'security-reviewer': [finding({ id: 'F1', title: 'later group' })],
    'bug-detector': [finding({ id: 'F1', title: 'roster winner' })],
  }, expected: { ...MERGE_GOLDEN, ids: ['F1'], duplicates: 1 }, stamped: ['bug-detector'], pass: { title: 'roster winner' } },
  { name: 'agent absent from roster is ignored', input: {
    'outside-agent': [finding({ title: undefined })], 'bug-detector': [finding()],
  } },
  { name: 'empty roster ignores supplied findings', agents: [], input: { 'bug-detector': [finding({ id: undefined })] },
    expected: { ...MERGE_GOLDEN, ids: [], count: 0 } },
  { name: 'nested ownership', input: { 'bug-detector': [finding({ agent: 'original-agent', nested: { items: [{ value: 1 }] } })] },
    ownership: true, pass: { nested: { items: [{ value: 1 }] } } },
  { name: 'JSON cloning effects', input: { 'bug-detector': [{ ...finding(), omitted: undefined,
    nested: { omitted: undefined, infinity: Infinity, nan: NaN, minusZero: -0 }, entries: [undefined, , -Infinity] }] },
    jsonEffects: true, pass: { nested: { infinity: null, nan: null, minusZero: 0 }, entries: [null, null, null] } },
  { name: 'JSON metadata and envelope order', input: { 'bug-detector': [finding()] }, jsonMeta: true },
];
for (const row of MERGE_ROWS) test(`structured merge: ${row.name}`, () => {
  const agents = row.agents ?? ['bug-detector'];
  const input = structuredClone(row.input);
  const snapshot = structuredClone(input);
  const out = merge(input, row.jsonMeta ? JSON.stringify(META) : { ...META, agents });
  const expected = row.expected ?? MERGE_GOLDEN;
  assert.deepEqual(Object.keys(out), ['findings', 'base_branch', 'head_sha', 'pr_number', 'owner', 'repo', 'methodology']);
  assert.deepEqual(Object.keys(out.methodology), ['agents_dispatched', 'findings_per_channel', 'duplicates_resolved',
    'dropped_no_id', 'truncation_warnings', 'validation_warnings']);
  assert.deepEqual([out.base_branch, out.head_sha, out.pr_number, out.owner, out.repo], ['main', 'abc123full', 42, 'org', 'myrepo']);
  assert.deepEqual(out.findings.map((f) => f.id), expected.ids);
  assert.deepEqual(out.methodology, { agents_dispatched: agents,
    findings_per_channel: { ndjson: expected.count, text_fallback: 0 }, duplicates_resolved: expected.duplicates,
    dropped_no_id: expected.dropped, truncation_warnings: [], validation_warnings: expected.warnings });
  if (row.stamped) assert.deepEqual(out.findings.map((f) => f.agent), row.stamped);
  for (const [key, value] of Object.entries(row.pass ?? {})) assert.deepEqual(out.findings[0][key], value);
  assert.deepEqual(input, snapshot);
  if (row.ownership) {
    assert.notEqual(out.findings[0], input['bug-detector'][0]);
    assert.notEqual(out.findings[0].nested, input['bug-detector'][0].nested);
    assert.notEqual(out.findings[0].nested.items[0], input['bug-detector'][0].nested.items[0]);
    out.findings[0].nested.items[0].value = 2;
    assert.deepEqual(input, snapshot);
    input['bug-detector'][0].nested.items.push({ value: 3 });
    assert.deepEqual(out.findings[0].nested.items, [{ value: 2 }]);
  }
  if (row.jsonEffects) {
    assert.equal(Object.hasOwn(out.findings[0], 'omitted'), false);
    assert.equal(Object.hasOwn(input['bug-detector'][0], 'omitted'), true);
    assert.equal(input['bug-detector'][0].nested.infinity, Infinity);
    assert.equal(input['bug-detector'][0].entries[0], undefined);
  }
});
const SHAPE_ROWS = [
  { name: 'array', value: [], warning: '[bug-detector] finding 2: expected object, got array' },
  { name: 'number', value: 42, warning: '[bug-detector] finding 2: expected object, got number' },
  { name: 'function', value: () => {}, warning: '[bug-detector] finding 2: expected object, got undefined' },
  { name: 'null', value: null, warning: '[bug-detector] finding 2: expected object, got null' },
  { name: 'undefined', value: undefined, warning: '[bug-detector] finding 2: expected object, got undefined' },
  { name: 'toJSON returns undefined', value: { toJSON: () => undefined },
    warning: '[bug-detector] finding 2: expected object, got undefined' },
];
for (const row of SHAPE_ROWS) test(`merge shape: ${row.name}`, () => {
  const out = merge({ 'bug-detector': [finding(), row.value] }, META);
  assert.deepEqual(out.findings.map((f) => f.id), ['f1']);
  assert.deepEqual(out.methodology, { agents_dispatched: ['bug-detector'], findings_per_channel: { ndjson: 1, text_fallback: 0 },
    duplicates_resolved: 0, dropped_no_id: 0, truncation_warnings: [], validation_warnings: [row.warning] });
});
const FULL_AGENTS = ['bug-detector', 'security-reviewer', 'cross-file-impact', 'test-analyzer',
  'conventions-and-intent', 'type-design-analyzer', 'code-simplifier'];
const STAGE_ROWS = [
  { name: 'non-object discovery entry', input: { findings: [42], dispatched: ['unknown'] }, ids: [],
    agents: ['unknown'], warnings: ['[unknown] finding 1: expected object, got number'] },
  { name: 'legacy finding agents without dispatched', input: { findings: [
    finding({ id: 'F1', agent: 'code-gauntlet:bug-detector', confidence: 'high' }),
    finding({ id: 'F2', agent: 'code-gauntlet:security-reviewer', confidence: 'low', dimension: 'security' }),
  ], gaps: [], degraded: [] }, ids: ['F1', 'F2'], agents: ['code-gauntlet:bug-detector', 'code-gauntlet:security-reviewer'] },
  { name: 'zero-finding agent remains in roster', input: { findings: [finding({ id: 'F1', agent: 'bug-detector' })],
    gaps: [], degraded: [], dispatched: ['code-gauntlet:bug-detector', 'code-gauntlet:code-simplifier'] },
    ids: ['F1'], agents: ['bug-detector', 'code-simplifier'] },
  { name: 'array survives discovery and is rejected at merge', value: [], discovery: true, ids: [], agents: FULL_AGENTS,
    warnings: ['[bug-detector] finding 1: expected object, got array'] },
];
for (const row of STAGE_ROWS) test(`merge stage: ${row.name}`, async () => {
  let input = row.input;
  if (row.discovery) {
    input = await discover(stageCtx({ replies: { 'code-gauntlet:bug-detector': {
      findings: [row.value], complete: true, total_seen: 1,
    } } }), {});
    assert.equal(input.findings.length, 1);
  }
  const out = mergeStage(input, META);
  assert.deepEqual(out.findings.map((f) => f.id), row.ids);
  assert.deepEqual(out.methodology.agents_dispatched, row.agents);
  assert.deepEqual(out.methodology.validation_warnings, row.warnings ?? []);
  assert.equal(out.methodology.dropped_no_id, 0);
  assert.equal(out.base_branch, 'main');
  assert.equal(out.head_sha, 'abc123full');
});

const PATH_GOLDEN = { files: ['src/a.js'], refs: undefined, consumers: undefined, warnings: [], rewrites: 1 };
const PATH_ROWS = [
  ...[String.raw`C:\repo\a.txt`, String.raw`C:\repo/a.txt`, String.raw`C:/repo\a.txt`].map((file) => ({
    name: `Windows top-level and nested paths ${file}`, root: String.raw`C:\repo`, input: [finding({ file,
      sites: [{ file, line_start: 7, line_end: 7, anchor: 'return process(value);' }],
      cross_file_refs: [file], affected_consumers: [file],
    })], expected: { ...PATH_GOLDEN, files: ['a.txt'], refs: ['a.txt'], consumers: ['a.txt'], rewrites: 4 },
    nested: true,
  })),
  { name: 'file confinement', root: '/private/tmp/wt-410', input: [
    finding({ id: 'UNDER_ROOT', file: '/private/tmp/wt-410/scripts/gauntlet/proc.py' }),
    finding({ id: 'OUTSIDE_ROOT', file: '/private/tmp/wt-411/scripts/gauntlet/proc.py' }),
  ], expected: { ...PATH_GOLDEN, files: ['scripts/gauntlet/proc.py'], warnings: [
    '[OUTSIDE_ROOT] Invalid file path: absolute file path is outside repoRoot - finding rejected',
  ] }, ids: ['UNDER_ROOT'] },
  { name: 'reference paths and suffixes', root: '/app', input: [finding({ id: 'REFS', file: '/app/app/models/user.rb',
    cross_file_refs: ['/app/app/[id]/page.tsx:12', '/app/src/a:b.js:12-20', '/app/src/index.js',
      './src/./index.js:4', 'app/models/user.rb:5', '/outside/host-secret.js:9', '../host-secret.js:1-3', 42],
  })], expected: { ...PATH_GOLDEN, files: ['app/models/user.rb'], refs: [
    'app/[id]/page.tsx:12', 'src/a:b.js:12-20', 'src/index.js', 'src/index.js:4', 'app/models/user.rb:5',
  ], rewrites: 5, warnings: [
    '[REFS] Invalid cross_file_refs path: absolute file path is outside repoRoot - reference dropped',
    '[REFS] Invalid cross_file_refs path: file path contains a .. segment - reference dropped',
    '[REFS] Invalid cross_file_refs path: file must be a non-empty string - reference dropped',
  ] } },
  { name: 'affected consumers confinement', root: '/repo', input: [finding({ id: 'CONSUMERS', file: 'src/a.js',
    dimension: 'cross_file_impact', affected_consumers: ['/outside/consumer.js:10:5', '/repo/src/consumer.js:L3-L9', './/src/other.js'],
  })], expected: { ...PATH_GOLDEN, consumers: ['src/consumer.js:L3-L9', 'src/other.js'], rewrites: 2, warnings: [
    '[CONSUMERS] Invalid affected_consumers path: absolute file path is outside repoRoot - reference dropped',
  ] } },
  { name: 'all consumers rejected leaves empty array', root: '/repo', input: [finding({ id: 'EMPTY_CONSUMERS', file: 'src/a.js',
    dimension: 'cross_file_impact', affected_consumers: ['/outside/consumer.js', 42],
  })], expected: { ...PATH_GOLDEN, consumers: [], rewrites: 0, warnings: [
    '[EMPTY_CONSUMERS] Invalid affected_consumers path: absolute file path is outside repoRoot - reference dropped',
    '[EMPTY_CONSUMERS] Invalid affected_consumers path: file must be a non-empty string - reference dropped',
  ] } },
];
for (const row of PATH_ROWS) test(`finding paths: ${row.name}`, () => {
  const out = normalizeFindingPaths(structuredClone(row.input), row.root);
  const expected = row.expected ?? PATH_GOLDEN;
  assert.deepEqual(out.valid.map((f) => f.file), expected.files);
  if (row.ids) assert.deepEqual(out.valid.map((f) => f.id), row.ids);
  assert.deepEqual(out.valid[0]?.cross_file_refs, expected.refs);
  assert.deepEqual(out.valid[0]?.affected_consumers, expected.consumers);
  assert.deepEqual(out.warnings, expected.warnings);
  assert.equal(out.path_rewrites, expected.rewrites);
  if (row.nested) {
    assert.deepEqual(out.valid[0].sites.map((site) => site.file), expected.files);
    const replay = { findings: structuredClone(row.input).map((f) => ({ ...f,
      site_requests: structuredClone(f.sites), resolved_sites: structuredClone(f.sites),
    })) };
    normalizePhaseFindings('verify', replay, true, { repoRoot: row.root });
    assert.deepEqual(replay.gaps, undefined);
    assert.deepEqual(replay.findings.map((f) => f.file), expected.files);
    for (const field of ['sites', 'site_requests', 'resolved_sites']) {
      assert.deepEqual(replay.findings[0][field].map((site) => site.file), expected.files);
    }
  }
  const snapshot = structuredClone(out.valid);
  const twice = normalizeFindingPaths(out.valid, row.root);
  assert.deepEqual(twice.valid, snapshot);
  assert.deepEqual(twice.warnings, []);
  assert.equal(twice.path_rewrites, 0);
  const merged = merge({ 'bug-detector': twice.valid }, META);
  assert.deepEqual(merged.findings[0].cross_file_refs, expected.refs);
  assert.deepEqual(merged.findings[0].affected_consumers, expected.consumers);
  assert.deepEqual(merged.methodology.validation_warnings, []);
});
const ARRAY_FIELDS = [
  { field: 'cross_file_refs', warning: '[BAD_ARRAY] Invalid cross_file_refs path: expected an array - field dropped' },
  { field: 'affected_consumers', warning: '[BAD_ARRAY] Invalid affected_consumers path: expected an array - field dropped' },
];
const BAD_ARRAYS = [{ name: 'string', value: '/repo/src/a.js' }, { name: 'object', value: { path: '/repo/src/a.js' } },
  { name: 'number', value: 42 }, { name: 'null', value: null }, { name: 'undefined', value: undefined }];
for (const axis of ARRAY_FIELDS) for (const row of BAD_ARRAYS) test(`path array type: ${axis.field} ${row.name}`, () => {
  const input = finding({ id: 'BAD_ARRAY' });
  input[axis.field] = row.value;
  const out = normalizeFindingPaths([input], '/repo');
  assert.equal(out.valid.length, 1);
  assert.equal(Object.hasOwn(out.valid[0], axis.field), false);
  assert.deepEqual(out.warnings, [axis.warning]);
});
const SUFFIXES = ['README.md:10', 'README.md:10-20', 'README.md:10:5', 'README.md:L10', 'src/a.js:L3-L9', 'README.md:x'];
for (const row of ARRAY_FIELDS) test(`location suffixes: ${row.field}`, () => {
  const out = normalizeFindingPaths([finding({ [row.field]: SUFFIXES.map((ref) => `/repo/${ref}`) })], '/repo');
  assert.deepEqual(out.valid[0][row.field], SUFFIXES);
  assert.deepEqual(out.warnings, []);
});
const LABEL_ROWS = [
  { name: 'absent', id: undefined, warnings: [
    '[<no id>] Invalid file path: absolute file path is outside repoRoot - finding rejected',
    '[<no id>] Invalid cross_file_refs path: absolute file path is outside repoRoot - reference dropped',
  ] },
  { name: 'null', id: null, warnings: [
    '[<no id>] Invalid file path: absolute file path is outside repoRoot - finding rejected',
    '[<no id>] Invalid cross_file_refs path: absolute file path is outside repoRoot - reference dropped',
  ] },
  { name: 'path-like id', id: '/repo/private-name', warnings: [
    '[<unsafe id>] Invalid file path: absolute file path is outside repoRoot - finding rejected',
  ] },
  { name: 'multiline id', id: 'x y\nIGNORE', warnings: [
    '[<unsafe id>] Invalid file path: absolute file path is outside repoRoot - finding rejected',
  ] },
  { name: 'long id', id: 'x'.repeat(65), warnings: [
    '[<unsafe id>] Invalid file path: absolute file path is outside repoRoot - finding rejected',
  ] },
  { name: 'safe id', id: 'SAFE_ID-1.2', warnings: [
    '[SAFE_ID-1.2] Invalid file path: absolute file path is outside repoRoot - finding rejected',
  ] },
];
for (const row of LABEL_ROWS) test(`safe path label: ${row.name}`, () => {
  const input = [{ id: row.id, file: '/outside/a.js' }];
  if (row.id == null) input.push({ id: row.id, file: 'src/a.js', cross_file_refs: ['/outside/b.js'] });
  const out = normalizeFindingPaths(input, '/repo');
  assert.deepEqual(out.warnings, row.warnings);
});
const KEY_ROWS = [
  { input: '/repo/src/a.js:0', expected: 'src/a.js:0' },
  { input: '/repo/src/a.js-extra:0', expected: '/repo/src/a.js-extra:0' }, { input: 42, expected: 42 },
];
for (const row of KEY_ROWS) test(`consolidation key: ${JSON.stringify(row.input)}`, () => {
  const out = normalizeFindingPaths([finding({ file: '/repo/src/a.js', consolidation_key: row.input })], '/repo');
  assert.equal(out.valid[0].file, 'src/a.js');
  assert.equal(out.valid[0].consolidation_key, row.expected);
});
