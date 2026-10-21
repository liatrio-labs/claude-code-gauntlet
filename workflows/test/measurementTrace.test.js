import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runWith } from '../src/run.js';
import { measurementScenario, persistedScenario } from './helpers/measurementScenario.js';
import { createMeasurementTrace, stampDiscovery, recordStage, appendMeasurement, traceDecision, TRACE_REASONS, TRACE_STAGES } from '../src/measurementTrace.js';
import { validArgs } from './helpers/argsFixtures.js';
import { makeCtx, makeFinding } from './helpers/pipelineMock.js';

for (const mode of ['legacy', 'derived', 'return']) test(`trace opt-in preserves every prompt, schema and user artifact: ${mode}`, async () => {
  const plain = measurementScenario(mode);
  const measured = measurementScenario(mode, { measurementTrace: true });
  const off = await runWith(plain.ctx, plain.args);
  const on = await runWith(measured.ctx, measured.args);
  assert.equal(off.ok, true);
  assert.equal(on.ok, true);
  assert.equal(Object.hasOwn(off, 'measurement'), false);
  assert.deepEqual(measured.ctx.calls, plain.ctx.calls);
  assert.deepEqual(persistedScenario(on, measured.state), persistedScenario(off, plain.state));
  assert.deepEqual(on.persistReturn, off.persistReturn);
  const { measurement, ...terminal } = on;
  assert.deepEqual(terminal, off);
  for (const call of measured.ctx.calls) {
    assert.equal(/trace_id|discovery_dimension|snapshots/.test(call.prompt), false);
    assert.equal(/trace_id|discovery_dimension|snapshots/.test(JSON.stringify(call.schema)), false);
  }
  const trace = JSON.parse(on.measurement.text);
  assert.deepEqual(trace.snapshots.map((s) => s.stage), TRACE_STAGES.slice(0, -1));
  assert.equal(trace.schema_version, 1);
  assert.equal(trace.complete, true);
  const first = trace.versions.find((v) => v.finding.id === 'FILTER');
  assert.equal(first.finding.confidence, 65);
  assert.ok(trace.versions.some((v) => v.trace_id === first.trace_id && v.finding.confidence === 45));
  const decisions = trace.snapshots.flatMap((s) => s.decisions);
  for (const code of ['duplicate_id', 'verify_eliminated', 'threshold', 'challenge_removed', 'challenge_cap', 'validation_skipped', 'delivery_cap']) {
    assert.ok(decisions.some((d) => d.reason_code === code), code);
  }
  assert.ok(decisions.every((d) => TRACE_REASONS.includes(d.reason_code)));
  assert.ok(trace.snapshots.some((s) => s.cap_hit));
});

test('large optional trace preserves primary recovery and native Unicode bytes', () => {
  const trace = createMeasurementTrace('n');
  const f = stampDiscovery(trace, { id: 'A', description: '\u00e9'.repeat(1100000), dimension: 'forged', trace_id: 'forged' }, { agentType: 'a', dimensions: [] }, 0, 0);
  assert.equal(f.trace_id, 'n:0:0');
  assert.equal(f.discovery_dimension, 'unknown');
  recordStage(trace, 'discover', [f]);
  const envelope = { ok: false, checkpoints: { phases: { discover: {} }, checksum: 'held' } };
  const out = appendMeasurement(envelope, trace);
  assert.deepEqual(out.checkpoints, envelope.checkpoints);
  assert.equal(out.measurement.status, 'unavailable');
  assert.equal(Object.hasOwn(out, 'persistReturn'), false);
  const small = createMeasurementTrace('n');
  recordStage(small, 'discover', [stampDiscovery(small, { description: '\u00e9' }, { agentType: 'a', dimensions: [] }, 0, 0)]);
  assert.ok(appendMeasurement(envelope, small).measurement.text.includes('\u00e9'));
});

test('replay coverage is partial and malformed trace flags refuse dispatch', async () => {
  const first = measurementScenario('return', { measurementTrace: true });
  const out = await runWith(first.ctx, first.args);
  const replay = measurementScenario('return', { measurementTrace: true, checkpoints: persistedScenario(out, first.state).checkpoints.phases });
  const resumed = await runWith(replay.ctx, replay.args);
  const trace = JSON.parse(resumed.measurement.text);
  assert.equal(trace.snapshots.find((s) => s.stage === 'challenge').coverage, 'partial');
  const malformed = measurementScenario('return', { measurementTrace: 'yes' });
  assert.equal((await runWith(malformed.ctx, malformed.args)).ok, false);
  assert.equal(malformed.ctx.calls.length, 0);
});

test('malformed discovery keeps the same review failure with tracing on and off', async () => {
  const captured = [];
  for (const measurementTrace of [false, true]) {
    const { args, ctx } = measurementScenario('return', { measurementTrace });
    const original = ctx.agent;
    ctx.agent = (prompt, dispatch) => {
      if (dispatch.agentType !== 'code-gauntlet:bug-detector') return original(prompt, dispatch);
      ctx.calls.push({ prompt, ...dispatch });
      return Promise.resolve({ findings: [null], complete: true, total_seen: 1 });
    };
    const out = await runWith(ctx, args);
    assert.equal(out.ok, false);
    delete out.measurement;
    captured.push({ out, calls: ctx.calls });
  }
  assert.deepEqual(captured[0], captured[1]);
});

test('main_only tier exclusions do not count as a delivery cap hit', async () => {
  const args = validArgs({ measurementTrace: true, delivery: { tier: 'main_only' },
    limits: { deliveryCap: 25 }, persist: { returnPrimaries: true } });
  const ctx = makeCtx(args, { findings: [makeFinding('MAIN'),
    makeFinding('SUGGESTION', { dimension: 'comment_accuracy' })] });
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  const trace = JSON.parse(out.measurement.text);
  const selected = trace.snapshots.find((s) => s.stage === 'select');
  assert.equal(selected.finding_version_ids.length, 1);
  assert.equal(selected.cap_hit, false);
  assert.deepEqual(selected.decisions.map((d) => d.reason_code), ['delivery_tier']);
});

test('mid-pipeline failure checkpoints are identical with tracing on and off', async () => {
  const captured = [];
  for (const measurementTrace of [false, true]) {
    const args = validArgs({ measurementTrace });
    const ctx = makeCtx(args);
    const parallel = ctx.parallel;
    let dispatches = 0;
    ctx.parallel = (thunks) => {
      if (++dispatches === 2) throw new Error('synthetic mid-pipeline failure');
      return parallel(thunks);
    };
    const out = await runWith(ctx, args);
    assert.equal(out.ok, false);
    assert.equal(out.phaseReached, 'verify');
    assert.equal(out.failingPhase, 'validate');
    assert.ok(out.checkpoints.phases.discover.findings.length > 0);
    if (measurementTrace) {
      const trace = JSON.parse(out.measurement.text);
      assert.equal(trace.complete, false);
      assert.ok(trace.versions.some((v) => v.finding.trace_id));
    }
    delete out.measurement;
    assert.equal(/trace_id|discovery_agent|discovery_dimension/.test(JSON.stringify(out)), false);
    captured.push({ out, calls: ctx.calls });
  }
  assert.deepEqual(captured[0], captured[1]);
});

test('an unlisted reason code is recorded as unknown', () => {
  assert.equal(traceDecision({ trace_id: 't1' }, 'threshold').reason_code, 'threshold');
  assert.equal(traceDecision({ trace_id: 't1' }, 'not-a-listed-code').reason_code, 'unknown');
});
