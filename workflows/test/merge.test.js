import { test } from 'node:test';
import assert from 'node:assert/strict';
import { merge, validateFindings, normalizeFindingPaths } from '../src/mergeFindings.js';
import { dedupById } from '../src/findingDedup.js';
import { finding } from './helpers/findings.js';

const META = { agents: ['bug-detector'], base_branch: 'main', head_sha: 'abc123full', pr_number: 42, owner: 'org', repo: 'myrepo' };
function mergeStructured(byAgent, agents = ['bug-detector']) {
  const ndjson = Object.fromEntries(Object.entries(byAgent).map(([agent, rows]) => [agent, rows.map((f) => JSON.stringify(f)).join('\n')]));
  return merge(ndjson, {}, { ...META, agents });
}

const VALIDATION_CASES = [
  ...['bug', 'security', 'cross_file_impact', 'test_coverage', 'convention', 'intent', 'comment_accuracy', 'type_design', 'simplification'].map((dimension) => ({ name: `known ${dimension} dimension kept`, input: finding({ dimension }), kept: 1, warning: null })),
  { name: 'missing dimension warns and stays', input: (() => { const f = finding(); delete f.dimension; return f; })(), kept: 1, warning: "Missing 'dimension'" },
  { name: 'unknown dimension warns and stays', input: finding({ dimension: 'new_dimension' }), kept: 1, warning: "Unknown dimension 'new_dimension'" },
  ...['file', 'line_start', 'description', 'confidence'].map((field) => ({ name: `missing ${field} rejects`, input: (() => { const f = finding(); delete f[field]; return f; })(), kept: 0, warning: `Missing required field '${field}'` })),
];
for (const c of VALIDATION_CASES) test(`merge validation: ${c.name}`, () => {
  const out = validateFindings([c.input]);
  assert.equal(out.valid.length, c.kept);
  assert.equal(out.warnings.length, c.warning === null ? 0 : 1);
  if (c.warning) assert.ok(out.warnings[0].includes(c.warning));
});

const DEDUP_CASES = [
  { name: 'idless structured finding dropped', rows: [{ file: 'x.py', title: 'no id' }, finding({ id: 'B' })], expected: ['B'], dropped: 1 },
  { name: 'duplicate id resolves once', rows: [finding({ id: 'B' }), finding({ id: 'B', title: 'second' })], expected: ['B'], dropped: 0, duplicates: 1 },
];
for (const c of DEDUP_CASES) test(`merge id dedup: ${c.name}`, () => {
  const out = dedupById({ 'bug-detector': c.rows }, {});
  assert.deepEqual(out.merged.map((f) => f.id), c.expected);
  assert.equal(out.droppedNoId, c.dropped);
  assert.equal(out.duplicatesResolved, c.duplicates ?? 0);
});

const MERGE_CASES = [
  { name: 'three structured findings keep order and count', input: { 'bug-detector': [finding({ id: 'bug-0' }), finding({ id: 'bug-1' }), finding({ id: 'bug-2' })] }, agents: ['bug-detector'], ids: ['bug-0', 'bug-1', 'bug-2'], ndjson: 3, dropped: 0, warnings: 0 },
  { name: 'empty structured run has valid envelope', input: {}, agents: ['bug-detector'], ids: [], ndjson: 0, dropped: 0, warnings: 0 },
  { name: 'structured finding stamps its dispatched agent', input: { 'bug-detector': [finding({ id: 'bug-1', agent: 'wrong-agent' })] }, agents: ['bug-detector'], ids: ['bug-1'], ndjson: 1, dropped: 0, warnings: 0, stamped: ['bug-detector'] },
  { name: 'multiple structured agents combine', input: { 'bug-detector': [finding({ id: 'b' })], 'security-reviewer': [finding({ id: 's', dimension: 'security' })] }, agents: ['bug-detector', 'security-reviewer'], ids: ['b', 's'], ndjson: 2, dropped: 0, warnings: 0, stamped: ['bug-detector', 'security-reviewer'] },
  { name: 'unknown dimension warns but keeps finding', input: { 'bug-detector': [finding({ dimension: 'new_dimension' })] }, agents: ['bug-detector'], ids: ['f1'], ndjson: 1, dropped: 0, warnings: 1 },
  { name: 'idless structured finding counted as dropped', input: { 'bug-detector': [finding({ id: undefined })] }, agents: ['bug-detector'], ids: [], ndjson: 0, dropped: 1, warnings: 1 },
  { name: 'extra fields survive structured merge', input: { 'bug-detector': [finding({ risk_level: 8, custom_receipt: { x: 2 } })] }, agents: ['bug-detector'], ids: ['f1'], ndjson: 1, dropped: 0, warnings: 0, pass: { risk_level: 8, custom_receipt: { x: 2 } } },
];
for (const c of MERGE_CASES) test(`merge structured: ${c.name}`, () => {
  const out = mergeStructured(c.input, c.agents);
  assert.deepEqual(Object.keys(out).sort(), ['base_branch', 'findings', 'head_sha', 'methodology', 'owner', 'pr_number', 'repo']);
  assert.deepEqual(out.findings.map((f) => f.id), c.ids);
  assert.deepEqual([out.base_branch, out.head_sha, out.pr_number, out.owner, out.repo], ['main', 'abc123full', 42, 'org', 'myrepo']);
  assert.deepEqual(out.methodology.agents_dispatched, c.agents);
  assert.deepEqual(out.methodology.findings_per_channel, { ndjson: c.ndjson, text_fallback: 0 });
  assert.equal(out.methodology.duplicates_resolved, 0);
  assert.equal(out.methodology.dropped_no_id, c.dropped);
  assert.equal(out.methodology.validation_warnings.length, c.warnings);
  assert.deepEqual(out.methodology.truncation_warnings, []);
  if (c.stamped) assert.deepEqual(out.findings.map((f) => f.agent), c.stamped);
  if (c.pass) for (const [key, value] of Object.entries(c.pass)) assert.deepEqual(out.findings[0][key], value);
});

test('normalizeFindingPaths: absolute finding paths are rewritten under repoRoot and outside paths are rejected', () => {
  const repoRoot = '/private/tmp/wt-410';
  const out = normalizeFindingPaths([
    finding({ id: 'UNDER_ROOT', file: `${repoRoot}/scripts/gauntlet/proc.py` }),
    finding({ id: 'OUTSIDE_ROOT', file: '/private/tmp/wt-411/scripts/gauntlet/proc.py' }),
  ], repoRoot);

  assert.deepEqual(out.valid.map((item) => [item.id, item.file]), [
    ['UNDER_ROOT', 'scripts/gauntlet/proc.py'],
  ]);
  assert.equal(out.path_rewrites, 1);
  assert.ok(!out.warnings.some((warning) => warning.includes('rewritten')));
  assert.ok(out.warnings.some((warning) =>
    warning.includes('[OUTSIDE_ROOT]') && warning.includes('outside repoRoot') && warning.includes('rejected')));
});

test('normalizeFindingPaths: cross_file_refs normalize paths, preserve line suffixes and disclose rejected references', () => {
  const out = normalizeFindingPaths([finding({
    id: 'REFS', file: '/app/app/models/user.rb',
    cross_file_refs: [
      '/app/app/[id]/page.tsx:12', '/app/src/a:b.js:12-20',
      '/app/src/index.js', './src/./index.js:4', 'app/models/user.rb:5',
      '/outside/host-secret.js:9', '../host-secret.js:1-3', 42,
    ],
  })], '/app');
  assert.equal(out.valid[0].file, 'app/models/user.rb');
  assert.deepEqual(out.valid[0].cross_file_refs, [
    'app/[id]/page.tsx:12', 'src/a:b.js:12-20',
    'src/index.js', 'src/index.js:4', 'app/models/user.rb:5',
  ]);
  assert.equal(out.path_rewrites, 5);
  assert.deepEqual(out.warnings, [
    '[REFS] Invalid cross_file_refs path: absolute file path is outside repoRoot - reference dropped',
    '[REFS] Invalid cross_file_refs path: file path contains a .. segment - reference dropped',
    '[REFS] Invalid cross_file_refs path: file must be a non-empty string - reference dropped',
  ]);
  const snapshot = JSON.parse(JSON.stringify(out.valid));
  const twice = normalizeFindingPaths(out.valid, '/app');
  assert.deepEqual(twice.valid, snapshot);
  assert.deepEqual(twice.warnings, []);
  assert.equal(twice.path_rewrites, 0);
  const merged = mergeStructured({ 'bug-detector': twice.valid });
  assert.deepEqual(merged.findings[0].cross_file_refs, snapshot[0].cross_file_refs);
  assert.deepEqual(merged.methodology.validation_warnings, []);
});

for (const field of ['cross_file_refs', 'affected_consumers']) {
  for (const [label, value] of [['string', '/repo/src/a.js'], ['object', { path: '/repo/src/a.js' }], ['number', 42], ['null', null], ['undefined', undefined]]) {
    test(`normalizeFindingPaths: ${field} ${label} value is removed with a warning`, () => {
      const input = finding({ id: 'BAD_ARRAY' });
      input[field] = value;
      const out = normalizeFindingPaths([input], '/repo');
      assert.equal(out.valid.length, 1);
      assert.equal(Object.hasOwn(out.valid[0], field), false);
      assert.deepEqual(out.warnings, [`[BAD_ARRAY] Invalid ${field} path: expected an array - field dropped`]);
    });
  }
  test(`normalizeFindingPaths: ${field} preserves every location suffix`, () => {
    const refs = ['README.md:10', 'README.md:10-20', 'README.md:10:5', 'README.md:L10', 'src/a.js:L3-L9', 'README.md:x'];
    const out = normalizeFindingPaths([finding({ [field]: refs.map((ref) => `/repo/${ref}`) })], '/repo');
    assert.deepEqual(out.valid[0][field], refs);
    assert.deepEqual(out.warnings, []);
  });
}

test('normalizeFindingPaths: affected_consumers drops outside paths and keeps normalized under-root paths', () => {
  const out = normalizeFindingPaths([finding({
    id: 'CONSUMERS', dimension: 'cross_file_impact',
    affected_consumers: ['/outside/consumer.js:10:5', '/repo/src/consumer.js:L3-L9', './/src/other.js'],
  })], '/repo');
  assert.deepEqual(out.valid[0].affected_consumers, ['src/consumer.js:L3-L9', 'src/other.js']);
  assert.deepEqual(out.warnings, ['[CONSUMERS] Invalid affected_consumers path: absolute file path is outside repoRoot - reference dropped']);
  const twice = normalizeFindingPaths(out.valid, '/repo');
  assert.deepEqual(twice.valid[0].affected_consumers, ['src/consumer.js:L3-L9', 'src/other.js']);
  assert.deepEqual(twice.warnings, []);
  assert.equal(twice.path_rewrites, 0);
});

test('normalizeFindingPaths: affected_consumers remains an empty array when every entry is rejected', () => {
  const out = normalizeFindingPaths([finding({
    id: 'EMPTY_CONSUMERS', dimension: 'cross_file_impact',
    affected_consumers: ['/outside/consumer.js', 42],
  })], '/repo');
  assert.deepEqual(out.valid[0].affected_consumers, []);
  assert.deepEqual(out.warnings, [
    '[EMPTY_CONSUMERS] Invalid affected_consumers path: absolute file path is outside repoRoot - reference dropped',
    '[EMPTY_CONSUMERS] Invalid affected_consumers path: file must be a non-empty string - reference dropped',
  ]);
});

test('normalizeFindingPaths: file and reference warnings share the missing-id fallback', () => {
  for (const id of [undefined, null]) {
    const out = normalizeFindingPaths([
      { id, file: '/outside/a.js' },
      { id, file: 'src/a.js', cross_file_refs: ['/outside/b.js'] },
    ], '/repo');
    assert.deepEqual(out.warnings, [
      '[<no id>] Invalid file path: absolute file path is outside repoRoot - finding rejected',
      '[<no id>] Invalid cross_file_refs path: absolute file path is outside repoRoot - reference dropped',
    ]);
  }
});

for (const [key, expected] of [
  ['/repo/src/a.js:0', 'src/a.js:0'],
  ['/repo/src/a.js-extra:0', '/repo/src/a.js-extra:0'],
  [42, 42],
]) {
  test(`normalizeFindingPaths: consolidation key ${JSON.stringify(key)} tracks only its own rewritten file`, () => {
    const out = normalizeFindingPaths([finding({ file: '/repo/src/a.js', consolidation_key: key })], '/repo');
    assert.equal(out.valid[0].file, 'src/a.js');
    assert.equal(out.valid[0].consolidation_key, expected);
  });
}
