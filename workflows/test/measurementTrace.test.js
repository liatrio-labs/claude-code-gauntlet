import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { runWith } from '../src/run.js';
import { makeFinding } from './helpers/pipelineMock.js';
import { measurementScenario, persistedScenario } from './helpers/measurementScenario.js';
import { createMeasurementTrace, stampDiscovery, recordStage, measurementText, measurementHash, appendMeasurement } from '../src/measurementTrace.js';
import { emitAgentPrompts } from './tools/emit_agent_prompts.mjs';

const referenceSha = '94ea095a89bfc2430814ff4e65db4ba790654fc7';
const bundle = new URL('../pipeline.js', import.meta.url);
const hash = (text) => createHash('sha256').update(text).digest('hex');

for (const mode of ['legacy', 'derived', 'return']) test(`runWith separate trace preserves immutable intake and primary checkpoints: ${mode}`, async () => {
  const { args, ctx, state } = measurementScenario(mode);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  const entry = out.persistReturn.entries.find((e) => e.path === out.measurement.path);
  assert.equal(entry.sha256, hash(entry.text));
  const trace = JSON.parse(entry.text);
  assert.deepEqual(trace.snapshots.map((s) => s.stage), ['discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'holdback', 'select', 'deliver']);
  const first = trace.versions.find((v) => v.finding?.id === 'FILTER');
  assert.equal(first.finding.confidence, 65);
  assert.ok(trace.versions.some((v) => v.trace_id === first.trace_id && v.set_fields.confidence === 45));
  const decisions = trace.snapshots.flatMap((s) => s.decisions);
  for (const code of ['duplicate_id', 'verify_eliminated', 'threshold', 'challenge_removed', 'challenge_cap', 'validation_skipped', 'delivery_cap']) {
    assert.ok(decisions.some((d) => d.reason_code === code), code);
  }
  const payload = persistedScenario(out, state);
  assert.deepEqual(Object.keys(payload.checkpoints), ['phases', 'completed', 'phaseReached', 'counts', 'checksum']);
  assert.deepEqual(Object.keys(payload.checkpoints.phases), ['challenge']);
  assert.equal(JSON.stringify(payload.checkpoints).includes('versions'), false);
  assert.equal(JSON.stringify(payload.checkpoints).includes('snapshots'), false);
  const selected = payload.postReview.findings;
  assert.deepEqual(new Set(selected.map((f) => f.discovery_dimension)), new Set(['bug', 'intent']));
  assert.equal(ctx.calls.some((c) => c.label.includes('measurement')), false);
  for (const c of ctx.calls.filter((c) => !['artifact-writer', 'assemble-artifacts'].includes(c.label))) {
    assert.equal(c.prompt.includes('trace_id'), false);
    assert.equal(JSON.stringify(c.schema).includes('discovery_dimension'), false);
  }
});

test('baseline arm independently preserves review prompts, schemas and delivery inputs', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'measurement-reference-'));
  const referencePath = join(dir, 'reference.js');
  const reference = execFileSync('git', ['show', `${referenceSha}:workflows/pipeline.js`], { cwd: new URL('../..', import.meta.url), encoding: 'utf8' });
  writeFileSync(referencePath, reference);
  const current = readFileSync(bundle, 'utf8');
  assert.notEqual(hash(reference), hash(current), 'reference must be the independent pre-instrumentation bundle');
  const before = await emitAgentPrompts(referencePath);
  const after = await emitAgentPrompts(bundle);
  assert.deepEqual(after.prompts, before.prompts);
  assert.deepEqual(after.schemas, before.schemas);
  assert.deepEqual(after.labels, before.labels);
  const project = (payload) => JSON.parse(JSON.stringify(payload.postReview), (key, value) =>
    ['trace_id', 'discovery_agent', 'discovery_dimension'].includes(key) ? undefined : value);
  assert.deepEqual(project(after.payload), before.payload.postReview);
  assert.deepEqual(Object.keys(after.payload.checkpoints), Object.keys(before.payload.checkpoints));
  assert.deepEqual(Object.keys(after.payload.checkpoints.phases), Object.keys(before.payload.checkpoints.phases));
});

test('oversized separate trace preserves the recovery envelope and primary budget priority', async () => {
  const trace = createMeasurementTrace('n');
  const f = stampDiscovery(trace, { id: 'A', description: 'x'.repeat(1100000) }, { agentType: 'a', dimensions: [] }, 0, 0);
  recordStage(trace, 'discover', [f]);
  const envelope = { ok: false, checkpoints: { phases: { discover: { findings: [{ id: 'A' }] } }, checksum: 'held' } };
  const out = appendMeasurement(envelope, trace, { outputDir: '/tmp/fixture', headShaShort: 'abcdef', nonce: 'n' });
  assert.equal(out.measurement.status, 'unavailable');
  assert.deepEqual(out.checkpoints, envelope.checkpoints);
  assert.equal(Object.hasOwn(out, 'persistReturn'), false);
});

test('dispatch overwrites forged provenance and retains unknown raw dimensions', () => {
  const trace = createMeasurementTrace('nonce');
  const f = stampDiscovery(trace, { id: 'A', dimension: 'forged', trace_id: 'forged', discovery_agent: 'forged', discovery_dimension: 'security' },
    { agentType: 'code-gauntlet:conventions-and-intent', dimensions: ['intent', 'convention', 'comment_accuracy'] }, 4, 2);
  recordStage(trace, 'discover', [f]);
  assert.equal(f.trace_id, 'nonce:4:2');
  assert.equal(f.discovery_agent, 'conventions-and-intent');
  assert.equal(f.discovery_dimension, 'unknown');
  f.id = 'changed';
  recordStage(trace, 'merge', [f]);
  assert.equal(trace.versions[0].finding.id, 'A');
  assert.equal(trace.versions[1].set_fields.id, 'changed');
  const text = measurementText(trace);
  assert.equal(measurementHash(text), hash(text));
});

test('separate compatible replay evidence is retained while old replay remains partial', async () => {
  const first = measurementScenario('return');
  const out = await runWith(first.ctx, first.args);
  const trace = JSON.parse(out.persistReturn.entries.find((e) => e.path === out.measurement.path).text);
  const checkpoint = persistedScenario(out, first.state).checkpoints;
  const phases = checkpoint.phases;
  for (const prior of [trace, undefined]) {
    const replay = measurementScenario('return', { checkpoints: phases });
    replay.ctx.measurementTrace = prior;
    const resumed = await runWith(replay.ctx, replay.args);
    assert.equal(resumed.ok, true);
    const evidence = JSON.parse(resumed.persistReturn.entries.find((e) => e.path === resumed.measurement.path).text);
    const challenge = evidence.snapshots.find((s) => s.stage === 'challenge');
    assert.equal(challenge.replayed, true);
    assert.equal(challenge.coverage, prior ? trace.snapshots.find((s) => s.stage === 'challenge').coverage : 'partial');
    if (prior) assert.deepEqual(evidence.snapshots[0], trace.snapshots[0]);
  }
});

test('oversized run trace preserves persistence dispatch and failure recovery against reference', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'measurement-oversize-'));
  const referencePath = join(dir, 'reference.js');
  writeFileSync(referencePath, execFileSync('git', ['show', `${referenceSha}:workflows/pipeline.js`], { cwd: new URL('../..', import.meta.url) }));
  const before = await emitAgentPrompts(referencePath, 'legacy', {}, { extraBytes: 1100000 });
  const after = await emitAgentPrompts(bundle, 'legacy', {}, { extraBytes: 1100000 });
  assert.equal(after.out.measurement.status, 'unavailable');
  assert.deepEqual(after.labels, before.labels);
  assert.deepEqual(Object.keys(after.payload.checkpoints), Object.keys(before.payload.checkpoints));
  assert.deepEqual(Object.keys(after.payload.checkpoints.phases), Object.keys(before.payload.checkpoints.phases));
  const scenario = measurementScenario('legacy', {}, { extraBytes: 1100000 });
  const parallel = scenario.ctx.parallel;
  let phases = 0;
  scenario.ctx.parallel = (thunks) => {
    phases += 1;
    if (phases === 2) throw new Error('synthetic post-discovery failure');
    return parallel(thunks);
  };
  const failed = await runWith(scenario.ctx, scenario.args);
  assert.equal(failed.ok, false);
  assert.equal(failed.checkpoints.truncated, true);
  assert.equal(Object.hasOwn(failed.checkpoints, 'measurement'), false);
  assert.equal(failed.measurement.status, 'unavailable');
});


test('discovery trace retains rejected paths and shapes outside sanitized checkpoint findings', async () => {
  const scenario = measurementScenario('return');
  const agent = scenario.ctx.agent;
  scenario.ctx.agent = (prompt, dispatch) => dispatch.agentType === 'code-gauntlet:bug-detector'
    ? Promise.resolve({ findings: [
      makeFinding('BADPATH', { file: '../outside.js', dimension: 'security', trace_id: 'forged', discovery_agent: 'forged', discovery_dimension: 'security' }),
      { id: 'MISSING', file: 'allowed.js', line_start: 10 }, null,
    ], complete: true, total_seen: 3 }) : agent(prompt, dispatch);
  const out = await runWith(scenario.ctx, scenario.args);
  assert.equal(out.ok, true);
  const trace = JSON.parse(out.persistReturn.entries.find((e) => e.path === out.measurement.path).text);
  const rejected = trace.versions.find((v) => v.model_finding_id === 'BADPATH').finding;
  assert.equal(rejected.file, '../outside.js');
  assert.equal(rejected.trace_id, 'nonce-xyz:0:0');
  assert.equal(rejected.discovery_agent, 'bug-detector');
  assert.equal(rejected.discovery_dimension, 'unknown');
  assert.equal(rejected.dimension, 'security');
  const decisions = trace.snapshots.flatMap((s) => s.decisions);
  assert.ok(decisions.some((d) => d.trace_id === rejected.trace_id && d.reason_code === 'invalid_path'));
  assert.ok(decisions.some((d) => d.reason_code === 'invalid_shape'));
  assert.ok(trace.versions.some((v) => v.finding && Object.hasOwn(v.finding, 'invalid_value')));
  const payload = persistedScenario(out, scenario.state);
  assert.equal(JSON.stringify(payload.checkpoints).includes('../outside.js'), false);
});
