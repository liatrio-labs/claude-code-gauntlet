import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fnv1a32 } from '../src/wire.js';
import { resolveRemovedTestsArgs } from '../src/args.js';
import { discover } from '../src/discover.js';
import { dimensionsSummaryTable } from '../src/renderReport.js';
import { runWith } from '../src/run.js';
import { stageCtx } from './helpers/stageFixtures.js';
import { validArgs, checkpointArgs } from './helpers/argsFixtures.js';
import { makeCtx, makeFinding } from './helpers/pipelineMock.js';
import { shellSplit } from './helpers/shellWords.js';
import { runAndCapture } from './helpers/persistFixtures.js';
import { projectVerifySliceFinding } from '../src/verifyWire.js';

const item = { id: '0123456789abcdef', path: 'tests/removed', old_start: 3, old_end: 7, kind: 'deleted_file' };
const body = { status: 'available', items: [item], omitted: 0 };
const signed = (value = body) => ({ ...value, checksum: fnv1a32(JSON.stringify(value, null, 2)) });
const loss = {
  file: 'source', line: 1, title: 'Coverage lost for override precedence',
  description: 'Override precedence still controls selection, but no surviving assertion exercises competing values.',
  severity: 'high', confidence: 95, criticality: 8,
  searched_paths: ['tests'], candidate_tests: [],
  failure_scenario: 'Reversing selection precedence silently chooses the wrong value.',
};
const lost = { id: item.id, status: 'lost', covered_by: [], losses: [loss] };
const cleanReply = (rows) => ({ findings: [], complete: true, total_seen: 0, removed_test_audit: rows });
const auditCtx = (rows) => stageCtx({ replies: { 'code-gauntlet:test-analyzer': cleanReply(rows) } });

test('audit dispatch converts losses and reserves removed_test for code', async () => {
  const ctx = auditCtx([lost]);
  const out = await discover(ctx, { removedTests: body });
  assert.deepEqual(out.removedTestAudit, { status: 'ran-clean' });
  assert.equal(out.findings.length, 1);
  assert.deepEqual(out.findings[0], {
    id: `removed-test:${item.id}:1`, agent: 'test-analyzer', dimension: 'test_coverage',
    test_kind: 'critical_gap', searched_paths: loss.searched_paths, candidate_tests: loss.candidate_tests,
    file: loss.file, line_start: loss.line, location_status: 'unconfirmed', title: loss.title, description: loss.description,
    severity: loss.severity, confidence: loss.confidence, criticality: loss.criticality,
    failure_scenario: loss.failure_scenario,
    removed_test: { id: item.id, path: item.path, old_start: 3, old_end: 7 },
  });
  const dispatch = ctx.calls.find((call) => call.agentType === 'code-gauntlet:test-analyzer');
  assert.match(dispatch.prompt, /tests\/removed/);
  assert.ok(dispatch.schema.required.includes('removed_test_audit'));
  const auditRow = dispatch.schema.properties.removed_test_audit.items;
  assert.equal(auditRow.additionalProperties, false);
  assert.equal(auditRow.properties.id.minLength, 1);
  assert.equal(auditRow.properties.status.minLength, 1);
  const citation = auditRow.properties.covered_by.items.properties;
  assert.equal(citation.file.minLength, 1);
  assert.equal(citation.line.minimum, 1);
  assert.equal(citation.input.minLength, 1);
  assert.equal(citation.assertion.minLength, 1);
  const lossSchema = auditRow.properties.losses.items.properties;
  assert.equal(lossSchema.file.minLength, 1);
  assert.equal(lossSchema.line.minimum, 1);
  assert.equal(lossSchema.title.minLength, 1);
  assert.equal(lossSchema.description.minLength, 1);
  assert.equal(lossSchema.severity.minLength, 1);
  assert.equal(lossSchema.failure_scenario.minLength, 1);
  for (const call of ctx.calls) {
    const schema = call.schema.properties.findings.items;
    assert.equal(schema.additionalProperties, false);
    assert.equal(Object.hasOwn(schema.properties, 'removed_test'), false, 'forged metadata fails the closed finding schema');
  }
});

test('audit anchor optionality follows required keys', async () => {
  for (const fields of [{}, { anchor: '' }, { anchor: 'return selected_value;' }, { anchor: ' ' }]) {
    const out = await discover(auditCtx([{ ...lost, losses: [{ ...loss, ...fields }] }]), { removedTests: body });
    assert.deepEqual(out.removedTestAudit, { status: 'ran-clean' });
    assert.equal(out.findings.length, 1);
  }
  const invalidLosses = [
    ...Object.keys(loss).map((key) => Object.fromEntries(Object.entries(loss).filter(([field]) => field !== key))),
    ...['file', 'title', 'description', 'failure_scenario'].map((key) => ({ ...loss, [key]: '' })),
    { ...loss, title: '  ' },
    { ...loss, anchor: null }, { ...loss, anchor: 42 },
    { ...loss, unexpected: 'unknown' },
  ];
  const invalidRows = [
    ...invalidLosses.map((value) => ({ ...lost, losses: [value] })),
    { ...lost, id: '' },
    { ...lost, unexpected: 'unknown' },
    { ...lost, status: 'covered_elsewhere', losses: [], covered_by: [
      { file: 'tests/new', line: 1, input: 'competing values', assertion: 'override wins', unexpected: 'unknown' },
    ] },
  ];
  for (const row of invalidRows) {
    const out = await discover(auditCtx([row]), { removedTests: body });
    assert.equal(out.removedTestAudit.status, 'gap');
    assert.deepEqual(out.findings, []);
  }
});

test('audit anchors reach Verify without numeric suppression', async () => {
  const anchor = { ...loss, file: '/repo/bin/seat', line: 545, anchor: 'return selected_value;' };
  const ordinary = (id, fields = {}) => makeFinding(id, {
    file: 'bin/seat', line_start: 545, line_end: undefined, dimension: 'test_coverage',
    test_kind: 'defective_test', criticality: 8, failure_scenario: 'Broken assertion',
    title: anchor.title, ...fields,
  });
  const ctx = stageCtx({ replies: {
    'code-gauntlet:bug-detector': { ...cleanReply([]), findings: [ordinary('other-agent')] },
    'code-gauntlet:test-analyzer': {
      ...cleanReply([{ ...lost, losses: [anchor] }]), total_seen: 7,
      findings: [
        ordinary('range-start', { file: './bin/seat', line_end: 566 }),
        ordinary('range-middle', { file: '/repo/bin/seat', line_start: 540, line_end: 550 }),
        ordinary('range-end', { line_start: 540, line_end: 545 }),
        ordinary('single-line'),
        ordinary('before', { line_start: 540, line_end: 544 }),
        ordinary('after', { line_start: 546 }),
        ordinary('other-file', { file: 'bin/other' }),
      ],
    },
  } });
  const out = await discover(ctx, { repoRoot: '/repo', removedTests: body });
  assert.deepEqual(out.findings.map((finding) => finding.id), [
    'other-agent', `removed-test:${item.id}:1`, 'range-start', 'range-middle', 'range-end', 'single-line', 'before', 'after', 'other-file',
  ]);
  assert.equal(out.findings[0].agent, 'bug-detector');
  assert.equal(out.findings[1].file, 'bin/seat');
  assert.equal(out.findings[1].anchor, anchor.anchor);
  assert.equal(out.findings[1].location_status, 'unconfirmed');
  assert.equal(projectVerifySliceFinding(out.findings[1]).anchor, anchor.anchor);
  assert.deepEqual(out.gaps, []);
  assert.equal(out.removedTestAudit.duplicates_dropped, undefined);
  const row = dimensionsSummaryTable(out).split('\n').find((line) => line.includes('| test-analyzer |'));
  assert.doesNotMatch(row, /repeated an audit loss/);
});

const badRows = [
  ['missing id', []], ['unknown id', [{ ...lost, id: 'unknown' }]],
  ['duplicate id', [lost, lost]],
  ['missing citation', [{ ...lost, status: 'covered_elsewhere', losses: [] }]],
  ['incomplete citation', [{ ...lost, status: 'covered_elsewhere', losses: [], covered_by: [{ file: 'tests/new', line: 1, input: '', assertion: 'equals override' }] }]],
  ['lost without loss', [{ ...lost, losses: [] }]],
  ['non-lost losses', [{ ...lost, status: 'removed_with_code' }]],
  ['confidence range', [{ ...lost, losses: [{ ...loss, confidence: 101 }] }]],
  ['severity domain', [{ ...lost, losses: [{ ...loss, severity: 'blocker' }] }]],
  ['escaped anchor', [{ ...lost, losses: [{ ...loss, file: '../source' }] }]],
  ['deleted anchor', [{ ...lost, losses: [{ ...loss, file: item.path }] }]],
];
for (const [name, rows] of badRows) test(`audit gap: ${name}`, async () => {
  const out = await discover(auditCtx(rows), { removedTests: body });
  assert.equal(out.removedTestAudit.status, 'gap');
  assert.equal(out.findings.length, 0);
  assert.ok(out.degraded.includes('test_coverage'));
  assert.ok(out.gaps.some((gap) => gap.includes('removed-test audit: gap')));
});

test('audit normalizes agent anchors and surviving citations within repoRoot', async () => {
  for (const file of ['/repo/source', './source']) {
    const out = await discover(auditCtx([{ ...lost, losses: [{ ...loss, file }] }]), {
      repoRoot: '/repo', removedTests: body,
    });
    assert.deepEqual(out.removedTestAudit, { status: 'ran-clean' });
    assert.equal(out.findings[0].file, 'source');
  }
  for (const file of ['/repo/tests/new', './tests/new', '/outside/tests/new', '../tests/new']) {
    const out = await discover(auditCtx([{ ...lost, status: 'covered_elsewhere', losses: [],
      covered_by: [{ file, line: 2, input: 'competing values', assertion: 'override wins' }] }]), {
      repoRoot: '/repo', removedTests: body,
    });
    assert.equal(out.removedTestAudit.status, file.startsWith('/outside') || file.startsWith('../') ? 'gap' : 'ran-clean');
  }
});

test('audit retains attributable losses with deleted paths in prose despite other row failures', async () => {
  const other = { ...item, id: 'fedcba9876543210', path: 'tests/other', kind: 'hunk' };
  const valid = { ...lost, losses: [{ ...loss, title: `Loss of ${item.path}`,
    description: `Removing ${item.path} loses the assertion.`, failure_scenario: `${item.path} no longer catches this.` }] };
  for (const invalid of [
    { ...lost, id: other.id, status: 'covered_elsewhere', losses: [] },
    { ...lost, id: other.id, status: 'removed_with_code' },
    { ...lost, id: other.id, losses: [loss, { ...loss, file: '../escape' }] },
    { ...lost, id: other.id, losses: [{ ...loss, confidence: 101 }] },
    null,
    { ...lost, id: 'unknown' },
  ]) {
    for (const rows of [[valid, invalid], [invalid, valid]]) {
      const out = await discover(auditCtx(rows), { removedTests: { ...body, items: [item, other] } });
      assert.equal(out.removedTestAudit.status, 'gap');
      assert.ok(out.removedTestAudit.reason);
      assert.deepEqual(out.findings.map((f) => [f.removed_test.id, f.title]), [[item.id, valid.losses[0].title]]);
    }
  }
  for (const rows of [[valid], [valid, { ...lost, id: other.id }, { ...lost, id: other.id }]]) {
    const out = await discover(auditCtx(rows), { removedTests: { ...body, items: [item, other] } });
    assert.equal(out.removedTestAudit.status, 'gap');
    assert.deepEqual(out.findings.map((f) => f.removed_test.id), [item.id]);
  }
});

test('audit rejects normalized citations and anchors to any deleted inventory file per row', async () => {
  const other = { ...item, id: 'fedcba9876543210', path: 'tests/other', kind: 'hunk' };
  for (const file of [item.path, `./${item.path}`, `/repo/${item.path}`]) {
    for (const invalid of [
      { ...lost, id: other.id, losses: [{ ...loss, file }] },
      { id: other.id, status: 'covered_elsewhere', losses: [],
        covered_by: [{ file, line: 3, input: 'competing values', assertion: 'override wins' }] },
    ]) {
      const out = await discover(auditCtx([lost, invalid]), { repoRoot: '/repo',
        removedTests: { ...body, items: [item, other] } });
      assert.equal(out.removedTestAudit.status, 'gap');
      assert.deepEqual(out.findings.map((f) => f.removed_test.id), [item.id]);
    }
  }
  const out = await discover(auditCtx([lost, { ...lost, id: other.id, losses: [{ ...loss, file: other.path }] }]), {
    removedTests: { ...body, items: [item, other] },
  });
  assert.equal(out.removedTestAudit.status, 'ran-clean');
  assert.equal(out.findings.length, 2, 'a hunk leaves its file available as an anchor');
});

for (const row of [
  { ...lost, status: 'removed_with_code', losses: [] },
  { ...lost, status: 'covered_elsewhere', losses: [], covered_by: [{ file: 'tests/new', line: 2, input: 'competing values', assertion: 'override wins' }] },
]) test(`audit accepts ${row.status}`, async () => {
  const out = await discover(auditCtx([row]), { removedTests: body });
  assert.deepEqual(out.removedTestAudit, { status: 'ran-clean' });
  assert.deepEqual(out.findings, []);
});

for (const [name, value] of [
  ['checksum mismatch', { ...signed(), checksum: 'wrong' }],
  ['bad shape', { ...signed(), items: 'bad' }],
  ['null', null], ['missing', undefined],
  ['unsafe inventory path', signed({ ...body, items: [{ ...item, path: '../tests/removed' }] })],
  ['numeric inventory id', signed({ ...body, items: [{ ...item, id: 1234567890123456 }] })],
]) test(`inventory ${name} degrades without refusing review`, async () => {
  const args = validArgs({ removedTests: value });
  assert.equal(resolveRemovedTestsArgs(args).status, 'unavailable');
  const out = await runWith(makeCtx(args, { findings: [] }), args);
  assert.equal(out.ok, true);
  assert.ok(out.gaps.some((gap) => gap.includes('removed-test audit: unavailable')));
  assert.ok(out.stats.degraded.includes('test_coverage'));
});

for (const [status, input] of [
  ['gap', { removedTests: body }],
  ['overflow', { removedTests: { ...body, omitted: 2 } }],
  ['unavailable', { removedTests: { status: 'unavailable', items: [], omitted: 0, reason: 'malformed patch' } }],
  ['skipped_light_scope', { removedTests: body, agentFlags: { deep: false } }],
]) test(`report discloses ${status} without a Clean test dimension`, async () => {
  const out = await discover(auditCtx(status === 'gap' ? [] : [lost]), input);
  assert.equal(out.removedTestAudit.status, status);
  const table = dimensionsSummaryTable({ ...out, removedTestAudit: out.removedTestAudit });
  const row = table.split('\n').find((line) => line.includes('| test-analyzer |'));
  assert.match(row, /Degraded - removed-test audit:/);
  assert.ok(row.includes(status));
  assert.doesNotMatch(row, /Clean/);
  if (status === 'overflow' || status === 'unavailable') assert.ok(!out.gaps.some((gap) => gap.includes('possibly incomplete')));
});

test('removed coverage shares admission and capped body delivery with real Verify', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'removed-tests-'));
  assert.equal(spawnSync('git', ['init', '-q'], { cwd: dir }).status, 0);
  const outputDir = join(dir, 'out');
  mkdirSync(outputDir);
  mkdirSync(join(dir, 'tests'));
  writeFileSync(join(dir, 'source'), 'return override\n');
  const bugs = Array.from({ length: 6 }, (_, index) => makeFinding(`bug${index}`, {
    file: `bug${index}`, line_start: 1, line_end: 1, anchor: 'return broken', severity: 'medium',
  }));
  for (const bug of bugs) writeFileSync(join(dir, bug.file), 'return broken\n');
  const diffPath = join(outputDir, 'code-gauntlet-diff-abc1234.patch');
  writeFileSync(diffPath, 'diff --git a/tests/removed b/tests/removed\n--- a/tests/removed\n+++ /dev/null\n@@ -3,5 +0,0 @@\n-assert override\n-assert selection\n-assert precedence\n-assert competing\n-assert values\n'
    + bugs.map((bug) => `diff --git a/${bug.file} b/${bug.file}\n--- a/${bug.file}\n+++ b/${bug.file}\n@@ -0,0 +1 @@\n+return broken\n`).join(''));
  writeFileSync(join(outputDir, 'code-gauntlet-project-rules-abc1234.md'), 'project rules\n');
  const produced = spawnSync('python3', [resolve('scripts/write_shared_context.py'), '--output-dir', outputDir, '--head-sha', 'abc1234'], {
    cwd: dir, input: 'Risk: medium\nAI-generated code: none', encoding: 'utf8',
  });
  assert.equal(produced.status, 0, produced.stderr);
  const receipt = JSON.parse(produced.stdout);
  assert.equal(receipt.removedTests.items.length, 1);
  const auditRow = { ...lost, id: receipt.removedTests.items[0].id, losses: [{ ...loss, file: join(dir, 'source'),
    production_element: { file: 'source', line_start: 1, line_end: 1, anchor: 'return override' } }] };
  let discoverOutput;
  for (const tier of ['all', 'main_only']) {
    let payload;
    const args = validArgs({
      repoRoot: dir, outputDir, diffPath, pluginRoot: resolve('.'),
      verify: { targetKind: 'local' },
      ...receipt, delivery: { tier }, limits: { deliveryCap: 6 },
      ...(tier === 'main_only' ? checkpointArgs({ discover: discoverOutput }) : {}),
    });
    const ctx = makeCtx(args, { findings: bugs, onPersist: (value) => { payload = value; } });
    const agent = ctx.agent;
    ctx.agent = async (prompt, dispatch) => {
      if (dispatch.label === 'code-gauntlet:test-analyzer') return cleanReply([auditRow]);
      if (dispatch.label.startsWith('verify-slice-')) {
        const argv = shellSplit(prompt.split('\n').pop());
        const result = spawnSync(argv[0], argv.slice(1), { cwd: dir, encoding: 'utf8' });
        assert.equal(result.status, 0, result.stderr);
        return JSON.parse(readFileSync(resolve(dir, argv[argv.indexOf('--output') + 1]), 'utf8'));
      }
      if (dispatch.label.startsWith('validate-batch-')) {
        const result = await agent(prompt, dispatch);
        for (const validation of result.validations) if (validation.finding_id.startsWith('removed-test:')) {
          validation.admission_assessments = [
            { predicate: 'resolved_production_element', status: 'confirmed', references: ['production_element'] },
            { predicate: 'complete_searches', status: 'confirmed', references: ['searched_paths:0'] },
            { predicate: 'complete_behavioral_absence', status: 'confirmed', references: ['production_element', 'searched_paths:0'] },
          ];
        }
        return result;
      }
      const result = await agent(prompt, dispatch);
      // A partial persistence return carries the discovery phase for resume.
      if (tier === 'all' && dispatch.label === 'artifact-writer') throw new Error('capture resumable phases');
      return result;
    };
    const out = await runWith(ctx, args);
    assert.equal(out.ok, true, JSON.stringify(out));
    assert.equal(out.stats.verified, true, JSON.stringify(out.gaps));
    assert.equal(payload.postReview.length, 6, JSON.stringify(payload.findings));
    assert.ok(payload.postReview.filter((finding) => !finding.removed_test).every((finding) => finding.disposition === 'deliver'));
    assert.ok(payload.postReview.some((finding) => finding.removed_test));
    const admitted = payload.findings.find((finding) => finding.removed_test);
    assert.equal(admitted.disposition, 'body');
    assert.equal(admitted.admission.status, 'admitted');
    assert.equal(admitted.diff_validation.in_diff, false);
    assert.equal(admitted.anchor, 'return override');
    assert.equal(admitted.origin, 'new');
    assert.equal(admitted.removed_test.path, item.path);
    const challenge = ctx.calls.find((call) => call.label.startsWith('challenge-') && call.prompt.includes(loss.title));
    assert.ok(challenge.prompt.includes(diffPath));
    assert.ok(challenge.prompt.includes(`${JSON.stringify(item.path)}, old lines 3-7`));
    assert.doesNotMatch(challenge.prompt, /failure_scenario|criticality|covered_by/);
    assert.match(payload.report, /\| Test Coverage \| test-analyzer \| 1 \|/);
    if (tier === 'all') {
      discoverOutput = out.checkpoints.phases.discover;
      assert.deepEqual(discoverOutput.removedTestAudit, { status: 'ran-clean' });
    } else assert.ok(!ctx.calls.some((call) => call.label === 'code-gauntlet:test-analyzer'));
  }
});

test('resolved audit overlap keeps existing same-start policy', async () => {
  const synthetic = makeFinding(`removed-test:${item.id}:1`, { agent: 'test-analyzer', file: 'source', line_start: 10, line_end: 10,
    removed_test: { id: item.id, path: item.path, old_start: 3, old_end: 7 } });
  const ordinary = [
    makeFinding('duplicate', { agent: 'test-analyzer', file: 'source' }),
    makeFinding('other-agent', { agent: 'bug-detector', file: 'source' }),
    makeFinding('other-start', { agent: 'test-analyzer', file: 'source', line_start: 9, line_end: 11 }),
    makeFinding('unconfirmed', { agent: 'test-analyzer', file: 'source', location_status: 'unconfirmed' }),
    makeFinding('other-file', { agent: 'test-analyzer', file: 'other' }),
  ];
  for (const location_status of ['confirmed', 'unconfirmed']) {
    const findings = [{ ...synthetic, location_status }, ...ordinary];
    const args = validArgs({ checkpoints: {
      summarize: { summary: 's', gaps: [] },
      discover: { findings, gaps: [], dispatched: ['code-gauntlet:test-analyzer'], degraded: [], removedTestAudit: { status: 'ran-clean', duplicates_dropped: 99 } },
      verify: { findings: structuredClone(findings), gaps: [], verified: true },
    } });
    const failed = await runWith(makeCtx(args, { parallelThrows: true }), args);
    assert.equal(failed.ok, false);
    assert.equal(failed.failingPhase, 'validate');
    const { out, ctx, payload } = await runAndCapture({ ...args, checkpoints: failed.checkpoints }, { findings: [] });
    assert.equal(out.ok, true);
    const row = payload.report.split('\n').find((line) => line.includes('| test-analyzer |'));
    if (location_status === 'confirmed') assert.match(row, /1 repeated an audit loss/);
    else assert.doesNotMatch(row, /repeated an audit loss/);
    const validate = ctx.calls.find((call) => call.label.startsWith('validate-')).prompt;
    assert.equal(validate.includes('- duplicate ['), location_status === 'unconfirmed');
    for (const f of ordinary.slice(1)) assert.ok(validate.includes(`- ${f.id} [`));
  }
});
