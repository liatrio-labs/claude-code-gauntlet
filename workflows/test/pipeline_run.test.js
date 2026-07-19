// pipeline_run.test.js — orchestration-contract tests for the full run() waist
// (Task 13). runWith(ctx, args) is the testable seam: it validates args, threads
// every stage summarize->...->report inside ONE top-level try/catch, then persists
// via writeArtifacts, and returns the compact envelope. A fully mocked ctx drives
// every agent()/parallel() dispatch (the runtime globals do not exist under
// node:test), so these tests assert the GLUE, not the individual stages.
//
// Degradation contract under test (Phase 0 throw contract):
//  - A throw inside a CORE stage (discover uses parallel(), so we make parallel()
//    itself throw) bubbles to the top-level catch -> { ok:false, error, phaseReached }.
//    run() NEVER throws.
//  - reportStage / writeArtifacts each wrap their own agent() in try/catch, so an
//    agent throw there degrades to a minimal report / partial-artifacts gap with
//    ok:true — report/persistence failure is non-fatal.
//  - argsVersion mismatch is rejected up front with an ok:false envelope.
//  - A present checkpoint for a phase skips that phase's dispatch entirely.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  runWith, reportStage, writeArtifacts, checkpointPath, readCheckpoints,
} from '../src/stages.js';
import { makeFinding, makeFindings, validArgs, makeCtx } from './helpers/pipelineMock.js';

// --- Happy path -------------------------------------------------------------

test('happy path: full pipeline returns ok:true, phaseReached=report, artifact paths', async () => {
  const args = validArgs();
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.equal(out.phaseReached, 'report');
  assert.ok(out.artifactPaths, 'artifactPaths present');
  assert.equal(typeof out.artifactPaths.findings, 'string');
  assert.equal(typeof out.artifactPaths.report, 'string');
  assert.equal(typeof out.artifactPaths.checkpoints, 'string');
  // Compact return: counts + paths + gaps, never the raw findings bulk.
  assert.ok(out.stats, 'stats present');
  assert.equal(out.stats.highConfidence, 2);
  assert.ok(Array.isArray(out.gaps));
  assert.ok(!('findings' in out), 'compact return must not carry raw findings');
  // Every stage dispatched: summarize + 7 discover + verify + validate + challenge + report + writer.
  assert.ok(ctx.calls.some((t) => t.label === 'report-writer'));
  assert.ok(ctx.calls.some((t) => t.label === 'artifact-writer'));
});

test('happy path: verify is trusted end-to-end (no UNVERIFIED gap, verified=true)', async () => {
  const args = validArgs();
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.stats.verified, true);
  assert.ok(!out.gaps.some((g) => /UNVERIFIED/.test(g)), `no verify degradation, got: ${out.gaps}`);
});

// --- Top-level catch --------------------------------------------------------

test('a throw in a core stage (discover) is caught by the top-level try/catch', async () => {
  const args = validArgs();
  const ctx = makeCtx(args, { throwOnDiscover: true });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, false);
  assert.equal(typeof out.error, 'string');
  assert.match(out.error, /discover/);
  // summarize completed before discover threw.
  assert.equal(out.phaseReached, 'summarize');
  // The failure envelope still carries the compact keys.
  assert.ok('artifactPaths' in out);
  assert.ok('stats' in out);
});

test('run never throws out of runWith even when a stage throws', async () => {
  const args = validArgs();
  const ctx = makeCtx(args, { throwOnDiscover: true });
  // Must resolve, not reject.
  await assert.doesNotReject(() => runWith(ctx, args));
});

// --- Report degradation (non-fatal) -----------------------------------------

test('a reportStage agent() throw degrades to ok:true with a minimal report + gap', async () => {
  const args = validArgs();
  const ctx = makeCtx(args, { agentThrowLabel: 'report-writer' });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true, 'report failure is non-fatal');
  assert.equal(out.phaseReached, 'report');
  assert.ok(out.gaps.some((g) => /report/i.test(g)), `expected a report gap, got: ${out.gaps}`);
  // Persistence still ran after the degraded report.
  assert.equal(typeof out.artifactPaths.report, 'string');
});

test('reportStage in isolation: a bare agent() throw yields a minimal report + gap', async () => {
  const ctx = {
    agent: async () => { throw new Error('boom'); },
    parallel: async () => [],
  };
  const out = await reportStage(ctx, { findings: [makeFinding('F1')], stats: {}, generatedAt: '2026-07-18T00:00:00Z' });
  assert.equal(typeof out.report, 'string');
  assert.ok(out.report.length > 0, 'minimal report assembled from stats');
  assert.ok(out.gaps.some((g) => /report/i.test(g)));
});

// --- writeArtifacts degradation (non-fatal) ---------------------------------

test('a writeArtifacts agent() throw yields ok:true with a partial-artifacts gap', async () => {
  const args = validArgs();
  const ctx = makeCtx(args, { agentThrowLabel: 'artifact-writer' });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true, 'persistence failure is non-fatal');
  assert.equal(out.phaseReached, 'report');
  assert.ok(out.gaps.some((g) => /partial|artifact/i.test(g)), `expected a partial-artifacts gap, got: ${out.gaps}`);
  // On failure the persisted paths are null (nothing was written).
  assert.equal(out.artifactPaths.findings, null);
});

test('writeArtifacts in isolation: a bare agent() throw yields a partial-artifacts gap', async () => {
  const ctx = {
    agent: async () => { throw new Error('disk on fire'); },
    parallel: async () => [],
  };
  const out = await writeArtifacts(ctx, { findings: [makeFinding('F1')], report: '# r', checkpoints: {}, outputDir: '.deep-review', headShaShort: 'abc1234' });
  assert.ok(out.gaps.some((g) => /partial|artifact/i.test(g)));
  assert.equal(out.artifactPaths.findings, null);
});

// --- Args rejection ---------------------------------------------------------

test('argsVersion mismatch is rejected immediately with an ok:false envelope', async () => {
  const args = validArgs({ argsVersion: 2 });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, false);
  assert.match(out.error, /argsVersion/);
  // No stage was dispatched — validation short-circuits before orchestration.
  assert.equal(ctx.calls.length, 0);
});

// --- Checkpoint resume ------------------------------------------------------

test('a present checkpoint for a phase skips that phase\'s dispatch', async () => {
  // Inject a checkpoint for discover: its output is reused, and NO discovery agent
  // is dispatched. The rest of the pipeline still runs on the checkpointed findings.
  const discoverCheckpoint = { findings: makeFindings().map((f) => ({ ...f, agent: 'deep-review:bug-detector' })), gaps: [], degraded: [] };
  const args = validArgs({ checkpoints: { discover: discoverCheckpoint } });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.equal(out.phaseReached, 'report');
  // No discovery task (a task carrying `dimensions`) was ever dispatched.
  assert.ok(!ctx.calls.some((t) => Array.isArray(t.dimensions)), 'discover dispatch was skipped');
});

test('readCheckpoints reads the resume map injected through the args waist', () => {
  const cp = { discover: { findings: [] } };
  assert.deepEqual(readCheckpoints(null, { checkpoints: cp }), cp);
  assert.deepEqual(readCheckpoints({ checkpoints: cp }, {}), cp);
  assert.deepEqual(readCheckpoints(null, {}), {});
});

test('checkpointPath is phase-keyed and sha-scoped', () => {
  const p = checkpointPath('discover', 'abc1234');
  assert.match(p, /discover/);
  assert.match(p, /abc1234/);
});

// --- Checkpoint round-trip (the pipeline's OWN persist output resumes it) ----

test('checkpoint round-trip: persisted per-phase map, fed back, skips every phase', async () => {
  // Run 1 produces the checkpoint artifact the writer persists; capture it via onPersist.
  const args1 = validArgs();
  let persistedCheckpoints = null;
  const ctx1 = makeCtx(args1, { onPersist: (payload) => { persistedCheckpoints = payload.checkpoints; } });
  const out1 = await runWith(ctx1, args1);
  assert.equal(out1.ok, true);
  assert.ok(persistedCheckpoints, 'writer received a checkpoints payload');
  assert.ok(persistedCheckpoints.phases, 'checkpoint artifact carries the per-phase outputs map');
  for (const phase of ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report']) {
    assert.ok(phase in persistedCheckpoints.phases, `checkpoint map has phase '${phase}'`);
  }

  // Run 2 feeds the persisted artifact straight back through the args waist. Every
  // phase's output is present, so NO stage dispatches — only the writer runs.
  const args2 = validArgs({ checkpoints: persistedCheckpoints });
  const ctx2 = makeCtx(args2);
  const out2 = await runWith(ctx2, args2);
  assert.equal(out2.ok, true);
  assert.equal(out2.phaseReached, 'report');
  assert.equal(ctx2.calls.length, 1, 'only the artifact-writer dispatched on full resume');
  assert.equal(ctx2.calls[0].label, 'artifact-writer');
});

// --- Report segmentation (oversized findings payload) -----------------------

test('reportStage segments an oversized findings payload into >1 dispatch and joins sections', async () => {
  // ~80 findings x ~2000-char description >> REPORT_SEGMENT_CHAR_BUDGET (100k).
  const big = [];
  for (let i = 0; i < 80; i += 1) big.push(makeFinding(`F${i}`, { description: 'x'.repeat(2000) }));

  const calls = [];
  const ctx = {
    agent: async (t) => { calls.push(t); return { report: `segment body for ${t.label}` }; },
    parallel: async () => [],
  };
  const out = await reportStage(ctx, { findings: big, unverified: [], stats: {}, generatedAt: '2026-07-18T00:00:00Z' });

  assert.ok(calls.length > 1, `expected >1 report-writer dispatch, got ${calls.length}`);
  assert.ok(calls.every((t) => t.agentType === 'deep-review:report-writer'));
  assert.match(out.report, /Report segment 1 of/);
  assert.match(out.report, new RegExp(`Report segment ${calls.length} of ${calls.length}`));
  assert.equal(out.gaps.length, 0, 'all segments rendered cleanly');
});

test('reportStage under the budget stays a single dispatch', async () => {
  const calls = [];
  const ctx = {
    agent: async (t) => { calls.push(t); return { report: 'one report' }; },
    parallel: async () => [],
  };
  const out = await reportStage(ctx, { findings: makeFindings(), unverified: [], stats: {} });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].label, 'report-writer');
  assert.equal(out.report, 'one report');
});
