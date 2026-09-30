import { test } from 'node:test';
import assert from 'node:assert/strict';
import { merge, validateFindings } from '../src/mergeFindings.js';
import { dedupById } from '../src/findingDedup.js';
import { finding } from './helpers/findings.js';

const META = { agents: ['bug-detector'], base_branch: 'main', head_sha: 'abc123full', pr_number: 42, owner: 'org', repo: 'myrepo' };
function mergeStructured(byAgent, agents = ['bug-detector'], options = {}) {
  const ndjson = Object.fromEntries(Object.entries(byAgent).map(([agent, rows]) => [agent, rows.map((f) => JSON.stringify(f)).join('\n')]));
  return merge(ndjson, {}, { ...META, ...options, agents });
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

test('merge boundary: absolute finding paths are rewritten under repoRoot and outside paths are rejected', () => {
  const repoRoot = '/private/tmp/wt-410';
  const out = mergeStructured({
    'bug-detector': [
      finding({ id: 'UNDER_ROOT', file: `${repoRoot}/scripts/gauntlet/proc.py` }),
      finding({ id: 'OUTSIDE_ROOT', file: '/private/tmp/wt-411/scripts/gauntlet/proc.py' }),
    ],
  }, ['bug-detector'], { repoRoot });

  assert.deepEqual(out.findings.map((item) => [item.id, item.file]), [
    ['UNDER_ROOT', 'scripts/gauntlet/proc.py'],
  ]);
  assert.ok(out.methodology.validation_warnings.some((warning) =>
    warning.includes('[UNDER_ROOT]') && warning.includes('rewritten')));
  assert.ok(out.methodology.validation_warnings.some((warning) =>
    warning.includes('[OUTSIDE_ROOT]') && warning.includes('outside repoRoot') && warning.includes('rejected')));
});
