import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { decodeMeasurement } from './helpers/decodeMeasurement.js';
import { runWith } from '../src/run.js';
import { measurementScenario, persistedScenario } from './helpers/measurementScenario.js';
import { createMeasurementTrace, stampDiscovery, recordStage, appendMeasurement, traceDecision, TRACE_REASONS } from '../src/measurementTrace.js';
import { validArgs } from './helpers/argsFixtures.js';
import { makeCtx, makeFinding } from './helpers/pipelineMock.js';
import { captureDeliveryReplay, appendDeliveryReplay } from '../src/measurementReplay.js';
import { largeMeasurement } from './helpers/largeMeasurement.js';
import { RETURN_CHAR_BUDGET } from '../src/wire.js';

for (const mode of ['legacy', 'derived', 'return']) test(`trace opt-in preserves every prompt, schema and user artifact: ${mode}`, async () => {
  const plain = measurementScenario(mode);
  const measured = measurementScenario(mode, { measurementTrace: true });
  const off = await runWith(plain.ctx, plain.args);
  const on = await runWith(measured.ctx, measured.args);
  assert.equal(off.ok, true);
  assert.equal(on.ok, true);
  assert.equal(on.stats.challenge.grouping, 'model');
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
  const replay = JSON.parse(on.measurement.replay.text);
  assert.equal(replay.state.phaseReached, 'challenge');
  assert.deepEqual(replay.state.completed, ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge']);
  assert.equal(Object.hasOwn(replay.state.phaseOutputs, 'report'), false);
  assert.equal(/trace_id|discovery_dimension/.test(JSON.stringify(replay.state)), false);
  assert.deepEqual(replay.persistence.map(({ label, agent_type, prompt_sha256, error }) => ({ label, agent_type, prompt_sha256, error })),
    measured.ctx.calls.filter(({ label }) => ['artifact-writer', 'assemble-artifacts'].includes(label))
      .map(({ label, agentType, prompt }) => ({ label, agent_type: agentType, prompt_sha256: createHash('sha256').update(prompt).digest('hex'), error: null })));
  assert.deepEqual(trace.snapshots.map((s) => s.stage), ['discover', 'merge', 'verify', 'validate', 'filter', 'admission', 'challenge', 'dispose', 'select']);
  assert.equal(trace.schema_version, 2);
  assert.equal(trace.complete, true);
  const first = trace.versions.find((v) => v.finding.id === 'FILTER');
  assert.equal(first.finding.confidence, 65);
  assert.ok(trace.versions.some((v) => v.trace_id === first.trace_id && v.finding.admission?.status === 'report_only'));
  const decisions = trace.snapshots.flatMap((s) => s.decisions);
  for (const code of ['duplicate_id', 'verify_eliminated', 'report_only', 'challenge_cap', 'delivery_cap']) {
    assert.ok(decisions.some((d) => d.reason_code === code), code);
  }
  assert.ok(decisions.every((d) => TRACE_REASONS.includes(d.reason_code)));
  const report = trace.versions.find((v) => v.finding.id === 'REPORT');
  const selection = trace.snapshots.find((s) => s.stage === 'select').decisions.find((d) => d.trace_id === report.trace_id);
  assert.equal(selection.reason_code, 'report_only');
  assert.deepEqual(selection.detail, { kind: 'ordinary' });
  assert.ok(trace.snapshots.some((s) => s.cap_hit));
});

for (const field of ['description', 'evidence']) test(`large optional ${field} preserves primary recovery and native Unicode bytes`, () => {
  const trace = createMeasurementTrace('n');
  const f = stampDiscovery(trace, { id: 'A', [field]: '\u00e9'.repeat(1100000), dimension: 'forged', trace_id: 'forged' }, { agentType: 'a', dimensions: [] }, 0, 0);
  assert.equal(f.trace_id, 'n:0:0');
  assert.equal(f.discovery_dimension, 'unknown');
  recordStage(trace, 'discover', [f]);
  const envelope = { ok: false, checkpoints: { phases: { discover: {} }, checksum: 'held' } };
  const out = appendMeasurement(envelope, trace);
  assert.deepEqual(out.checkpoints, envelope.checkpoints);
  assert.equal(out.measurement.status, 'available');
  const cut = decodeMeasurement(out.measurement);
  assert.equal(out.measurement.encoding, 'json-pool-lzw16-base64-v1');
  assert.deepEqual(cut, trace);
  assert.equal(Object.hasOwn(out, 'persistReturn'), false);
  const small = createMeasurementTrace('n');
  recordStage(small, 'discover', [stampDiscovery(small, { description: '\u00e9' }, { agentType: 'a', dimensions: [] }, 0, 0)]);
  assert.ok(appendMeasurement(envelope, small).measurement.text.includes('\u00e9'));
});

test('400 long claims retain discovery within the return budget and reuse versions across diagnostic stamps', () => {
  const { trace, findings, envelope, out } = largeMeasurement();
  assert.equal(out.measurement.status, 'available');
  assert.ok(JSON.stringify(out).length <= RETURN_CHAR_BUDGET);
  assert.equal(out.review, envelope.review);
  const saved = JSON.parse(out.measurement.text);
  assert.deepEqual(saved.dropped_stages, []);
  assert.equal(saved.snapshots[0].coverage, 'complete');
  assert.equal(saved.snapshots[0].finding_version_ids.length, 400);
  assert.equal(saved.versions.length, 400);
  for (const snapshot of saved.snapshots) assert.deepEqual(snapshot.finding_version_ids, saved.snapshots[0].finding_version_ids);
  assert.equal(saved.versions[0].finding.evidence, findings[0].evidence);
  assert.equal(Object.hasOwn(saved.versions[0].finding, 'reference_diagnostics'), false);
  const recorded = JSON.stringify(trace.versions[0].finding);
  findings[0].description = 'caller mutation after recording';
  findings[0].admission.kind = 'caller mutation of nested field';
  assert.equal(JSON.stringify(trace.versions[0].finding), recorded);
});

test('compression preserves history and budget fallback preserves discovery and decision references', () => {
  const trace = createMeasurementTrace('cut');
  const f = stampDiscovery(trace, { description: 'discovered defect' }, { agentType: 'a', dimensions: [] }, 0, 0);
  const invalid = stampDiscovery(trace, { description: 'invalid claim' }, { agentType: 'a', dimensions: [] }, 0, 1);
  recordStage(trace, 'discover', [f], { removed: [invalid], decisions: [traceDecision(invalid, 'invalid_shape', 'malformed', [f.trace_id])] });
  recordStage(trace, 'merge', [{ ...f, description: 'é'.repeat(600000) }], { decisions: [traceDecision(f, 'unknown', 'kept', [], 'kept')] });
  recordStage(trace, 'verify', [{ ...f, description: 'z'.repeat(600000) }]);
  const original = JSON.stringify(trace);
  const out = appendMeasurement({ ok: true }, trace);
  assert.ok(JSON.stringify(out).length <= RETURN_CHAR_BUDGET);
  const saved = decodeMeasurement(out.measurement);
  assert.deepEqual(saved, trace);
  const tighter = decodeMeasurement(appendMeasurement({ review: 'x'.repeat(RETURN_CHAR_BUDGET - 3000) }, saved).measurement);
  assert.deepEqual(tighter.dropped_stages, ['merge', 'verify']);
  assert.equal(tighter.snapshots[0].coverage, 'complete');
  assert.equal(tighter.snapshots[1].coverage, 'unavailable');
  assert.equal(tighter.snapshots[2].coverage, 'unavailable');
  assert.equal(tighter.versions.length, 2);
  assert.ok(tighter.versions.some((v) => v.trace_id === invalid.trace_id));
  assert.equal(JSON.stringify(trace), original);
  const noRoom = { review: 'x'.repeat(RETURN_CHAR_BUDGET) };
  assert.deepEqual(appendMeasurement(noRoom, trace), noRoom);
});

test('replaying old versions preserves snapshot IDs when ignored stamps collapse to one claim', () => {
  const original = createMeasurementTrace('old');
  const finding = stampDiscovery(original, { description: 'claim' }, { agentType: 'a', dimensions: [] }, 0, 0);
  recordStage(original, 'discover', [finding]);
  const first = original.versions[0];
  const previous = { schema_version: 1, versions: [first, { ...first, version_id: 'v1', finding: { ...finding, validation: 'stage stamp' } }] };
  const trace = createMeasurementTrace('old', previous);
  recordStage(trace, 'discover', [finding]);
  assert.deepEqual(trace.versions.map((v) => v.version_id), ['v0', 'v1']);
  assert.deepEqual(trace.snapshots[0].finding_version_ids, ['v1']);
  assert.deepEqual(trace.versions[0].finding, trace.versions[1].finding);
  recordStage(trace, 'merge', [{ ...finding, confidence: 75 }]);
  assert.deepEqual(trace.snapshots[1].finding_version_ids, ['v2']);
  const pruned = createMeasurementTrace('old', { versions: [trace.versions[0], trace.versions[2]] });
  recordStage(pruned, 'discover', [{ ...finding, confidence: 80 }]);
  assert.deepEqual(pruned.snapshots[0].finding_version_ids, ['v3']);
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

test('malformed discovery preserves sibling review with tracing on and off', async () => {
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
    assert.equal(out.ok, true);
    delete out.measurement;
    captured.push({ out, calls: ctx.calls });
  }
  assert.deepEqual(captured[0], captured[1]);
});

test('main_only tier exclusions do not count as a delivery cap hit', async () => {
  const args = validArgs({ measurementTrace: true, delivery: { tier: 'main_only' },
    limits: { deliveryCap: 25 }, persist: { returnPrimaries: true } });
  const ctx = makeCtx(args, { findings: [makeFinding('MAIN'), makeFinding('SUGGESTION')] });
  const agent = ctx.agent;
  ctx.agent = async (prompt, dispatch) => {
    const result = await agent(prompt, dispatch);
    if (dispatch.label.startsWith('validate-')) for (const row of result.validations) {
      if (row.finding_id === 'SUGGESTION') Object.assign(row, { consequence: 'latent', latent_condition: 'New caller' });
    }
    if (dispatch.label.startsWith('challenge-') && prompt.includes('finding SUGGESTION')) {
      Object.assign(result.verdict, { consequence: 'latent', latent_condition: 'New caller' });
    }
    return result;
  };
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

test('measured persistence records native UTF-8 hashes, results and thrown errors', async () => {
  const options = { label: 'artifact-writer', agentType: 'code-gauntlet:artifact-writer' };
  const result = { artifactPaths: { report: '/report' } };
  const { ctx, record } = captureDeliveryReplay({ pipelineVersion: null, agent: async (prompt) => {
    if (prompt === 'fail') throw new Error('write failed');
    if (prompt === 'undefined') return undefined;
    return result;
  } }, {}, {});
  const prompts = ['', 'a'.repeat(55), 'a'.repeat(56), 'a'.repeat(64), 'é🐦\ud800', 'undefined', 'fail'];
  for (const prompt of prompts) {
    if (prompt === 'fail') await assert.rejects(ctx.agent(prompt, options), /write failed/);
    else await ctx.agent(prompt, options);
  }
  result.artifactPaths.report = '/changed';
  assert.deepEqual(record.persistence.map((row) => row.prompt_sha256), prompts.map((prompt) => createHash('sha256').update(prompt).digest('hex')));
  assert.deepEqual(record.persistence[0].result, { artifactPaths: { report: '/report' } });
  assert.equal(record.persistence.at(-2).result, null);
  assert.equal(record.persistence.at(-1).error, 'write failed');
  const large = { ...record, args: { text: 'x'.repeat(1100000) } };
  const envelope = { ok: true, measurement: { status: 'available', text: '{}' } };
  const out = appendDeliveryReplay(envelope, large);
  assert.deepEqual(out.measurement.replay, { status: 'unavailable', reason: 'return_budget' });
  assert.equal(out.measurement.text, '{}');
  const full = { ...envelope, text: 'x'.repeat(1100000) };
  assert.equal(appendDeliveryReplay(full, large), full);
});

test('selection traces one reason for every member of an indivisible omitted group', async () => {
  const args = validArgs({ measurementTrace: true, limits: { deliveryCap: 1 }, delivery: { tier: 'main_only' } });
  const ctx = makeCtx(args, { findings: [makeFinding('MAIN'), makeFinding('CHILD'), makeFinding('SUGGESTION'), makeFinding('WIN', { severity: 'critical' })] });
  const original = ctx.agent;
  ctx.agent = async (prompt, opts) => {
    if (opts.label === 'group-findings') return { groups: [{ finding_ids: ['MAIN', 'CHILD'] }] };
    const result = await original(prompt, opts);
    if (opts.label.startsWith('validate-')) for (const v of result.validations) {
      if (['CHILD', 'SUGGESTION'].includes(v.finding_id)) Object.assign(v, { consequence: 'latent', latent_condition: 'Another caller' });
    }
    if (opts.label.startsWith('challenge-') && /finding (?:CHILD|SUGGESTION)/.test(prompt)) Object.assign(result.verdict, { consequence: 'latent', latent_condition: 'Another caller' });
    return result;
  };
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  const trace = JSON.parse(out.measurement.text);
  const ids = new Map(trace.versions.map((v) => [v.trace_id, v.finding.id]));
  const selected = trace.snapshots.find((s) => s.stage === 'select');
  const reasons = Object.fromEntries(selected.decisions.map((d) => [ids.get(d.trace_id), d.reason_code]));
  assert.equal(reasons.MAIN, 'delivery_cap');
  assert.equal(reasons.CHILD, 'delivery_cap');
  assert.equal(reasons.SUGGESTION, 'delivery_tier');
});
