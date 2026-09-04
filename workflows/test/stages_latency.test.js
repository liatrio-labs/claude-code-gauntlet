// stages_latency.test.js — issue #38 stream D2: the four output-preserving latency
// changes to workflows/src/stages.js.
//
//  D2.1  the persisted checkpoint drops `filter` (pure, agent-free, zero-cost re-run)
//  D2.2  summarize runs CONCURRENTLY with discover (no data dependency)
//  D2.3  verify slice inputs now travel inline with the executor command (covered in
//        stages_verify.test.js; this file retains the latency/checkpoint contracts)
//
// The load-bearing property for D2.2/D2.3 is that NOTHING observable changes: checkpoint
// semantics, phaseOutputs/completed ORDER, error attribution (failingPhase), degradation
// MESSAGES and output ORDER must all stay byte-identical. Every test here pins one of those.
//
// Concurrency is proven with a GATE, not a clock: dispatch A blocks on a promise that only
// dispatch B can resolve. A sequential implementation deadlocks; a safety valve releases the
// gate after draining the microtask queue and the test asserts the valve never fired.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { runWith, slimPersistedCheckpoints } from '../src/stages.js';
import { makeFinding, validArgs, makeCtx } from './helpers/pipelineMock.js';

const STAGES_SOURCE = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'stages.js'), 'utf8');

// Drain the microtask queue far enough that a SEQUENTIAL implementation has provably
// parked on the gate (it can make no further progress without it).
async function drainMicrotasks(n = 100) {
  for (let i = 0; i < n; i += 1) await Promise.resolve();
}

test('report replay comments contain no retired empty-report flow terminology', () => {
  const retiredTerms = ['postFilter' + 'Count', 'findings' + 'AtRisk', 'empty_' + 'report', 'empty-' + 'report guard'];
  for (const term of retiredTerms) {
    assert.ok(!STAGES_SOURCE.includes(term), `stages.js must not mention retired ${term}`);
  }
});

// --- D2.1: the persisted checkpoint drops `filter` --------------------------

test('D2.1: slimPersistedCheckpoints persists challenge only — no phases.filter, counts for every phase', () => {
  const phaseOutputs = {
    summarize: { summary: 's' },
    discover: { findings: [makeFinding('F1'), makeFinding('F2')] },
    merge: { findings: [makeFinding('F1')] },
    verify: { findings: [makeFinding('F1')] },
    validate: { findings: [makeFinding('F1')] },
    filter: { filtered: [makeFinding('F1')], stats: {} },
    challenge: { findings: [makeFinding('F1')], unverified: [] },
    report: { report: '# r' },
  };
  const completed = Object.keys(phaseOutputs);
  const cp = slimPersistedCheckpoints(phaseOutputs, completed, 'report');

  assert.deepEqual(Object.keys(cp.phases), ['challenge'], 'only challenge keeps full output');
  assert.ok(!('filter' in cp.phases), 'filter full output is NOT persisted (pure JS, re-runs free)');
  // Every phase — filter included — still records a bare count.
  for (const name of completed) {
    assert.equal(typeof cp.counts[name], 'number', `counts has a number for '${name}'`);
  }
  assert.equal(cp.counts.filter, 1, 'the filter count is still recorded (from .filtered)');
  assert.equal(cp.counts.challenge, 1);
  assert.deepEqual(cp.completed, completed);
  assert.equal(cp.phaseReached, 'report');
});

test('D2.1: a resume with ONLY challenge persisted still delivers the replayed challenge findings', async () => {
  // Run 1 -> capture the persisted checkpoint artifact.
  const args1 = validArgs();
  let persisted = null;
  const ctx1 = makeCtx(args1, { onPersist: (p) => { persisted = p.checkpoints; } });
  const out1 = await runWith(ctx1, args1);
  assert.equal(out1.ok, true);
  assert.deepEqual(Object.keys(persisted.phases), ['challenge']);

  // Run 2 -> feed it straight back. filter RE-RUNS (free), challenge is REPLAYED verbatim.
  const args2 = validArgs({ checkpoints: persisted });
  const ctx2 = makeCtx(args2);
  const out2 = await runWith(ctx2, args2);
  assert.equal(out2.ok, true);
  assert.equal(out2.phaseReached, 'report');
  assert.ok(!ctx2.calls.some((c) => (c.label || '').startsWith('challenge-')), 'challenge replayed, not re-dispatched');
  assert.equal(out2.stats.highConfidence, 2, 'the replayed challenge findings are delivered unchanged');
  // The replayed challenge output is delivered, and the pure renderer rebuilds the report
  // from that output on every resume.
  assert.equal(typeof out2.artifactPaths.report, 'string', 'a real report persisted');
});

// --- D2.1 replay hole: an empty report checkpoint is regenerated ------------

async function persistedCheckpointFromAFullRun() {
  const args = validArgs();
  let persisted = null;
  const ctx = makeCtx(args, { onPersist: (payload) => { persisted = payload.checkpoints; } });
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.deepEqual(Object.keys(persisted.phases), ['challenge']);
  return persisted;
}

test('D2.1 resume hole: a replayed EMPTY report checkpoint is re-run even when the fresh filter set is empty', async () => {
  const persisted = await persistedCheckpointFromAFullRun();
  const args = validArgs({
    checkpoints: {
      phases: { challenge: persisted.phases.challenge, report: { report: '', gaps: [] } },
      completed: persisted.completed,
    },
  });
  let persistedReport = null;
  const ctx = makeCtx(args, {
    findings: [],
    onPersist: (payload) => { persistedReport = payload.report; },
  });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.ok(persistedReport.startsWith('# \u2694\uFE0F Code Gauntlet:'), 'the empty checkpoint was re-rendered before persistence');
  assert.equal(typeof out.artifactPaths.report, 'string', 'a real report persisted');
  assert.equal(out.stats.highConfidence, 2, 'the replayed delivered set is unchanged by the recovery');
});

// --- D2.2: summarize runs concurrently with discover ------------------------

test('D2.2: summarize and the discovery parallel() are both issued before either resolves', async () => {
  const args = validArgs();
  const base = makeCtx(args);
  const events = [];
  let releaseSummarize = null;
  const summarizeGate = new Promise((r) => { releaseSummarize = r; });

  const ctx = {
    calls: base.calls,
    violations: base.violations,
    agent: async (prompt, opts) => {
      if ((opts || {}).label === 'summarize') {
        events.push('summarize:dispatched');
        await summarizeGate; // only the discovery fan-out can unblock this
      }
      return base.agent(prompt, opts);
    },
    parallel: async (thunks) => {
      if (releaseSummarize) {
        events.push('discover:parallel-dispatched');
        const r = releaseSummarize; releaseSummarize = null; r();
      }
      return base.parallel(thunks);
    },
  };

  const runP = runWith(ctx, args);
  await drainMicrotasks();
  let deadlocked = false;
  if (releaseSummarize) { deadlocked = true; const r = releaseSummarize; releaseSummarize = null; r(); }
  const out = await runP;

  assert.equal(deadlocked, false, 'discover must dispatch while summarize is still in flight (sequential = deadlock)');
  assert.deepEqual(events, ['summarize:dispatched', 'discover:parallel-dispatched']);
  assert.equal(out.ok, true);
  assert.equal(out.phaseReached, 'report');
});

test('D2.2: a checkpointed summarize does NOT dispatch, while discover still runs', async () => {
  const args = validArgs({ checkpoints: { summarize: { summary: 'replayed summary', gaps: [] } } });
  let persistedReport = null;
  const ctx = makeCtx(args, { onPersist: (payload) => { persistedReport = payload.report; } });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.ok(!ctx.calls.some((c) => c.label === 'summarize'), 'summarize was replayed, never dispatched');
  assert.ok(ctx.calls.some((c) => c.label === 'code-gauntlet:bug-detector'), 'discover still dispatched');
  assert.ok(persistedReport.includes('## Summary\n\nreplayed summary'), 'the checkpointed summary flows into the persisted report');
});

test('D2.2: a checkpointed discover does NOT dispatch, while summarize still runs', async () => {
  const discoverCheckpoint = {
    findings: [makeFinding('F1'), makeFinding('F2')].map((f) => ({ ...f, agent: 'bug-detector' })),
    gaps: [], degraded: [], dispatched: ['code-gauntlet:bug-detector'],
  };
  const args = validArgs({ checkpoints: { discover: discoverCheckpoint } });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.ok(!ctx.calls.some((c) => (c.label || '').startsWith('code-gauntlet:bug-detector')), 'discover was replayed, never dispatched');
  assert.ok(ctx.calls.some((c) => c.label === 'summarize'), 'summarize still dispatched');
});

test('D2.2: completed / phaseOutputs order is unchanged — summarize is recorded before discover', async () => {
  const args = validArgs();
  let persisted = null;
  const ctx = makeCtx(args, { onPersist: (p) => { persisted = p.checkpoints; } });
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.deepEqual(
    persisted.completed,
    ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report'],
  );
  // counts is insertion-ordered off phaseOutputs, so it pins the RECORD order too.
  assert.deepEqual(
    Object.keys(persisted.counts),
    ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report'],
  );
  assert.deepEqual(out.checkpoints.completed, persisted.completed);
});

test('D2.2: a discover throw while summarize is still pending -> failingPhase discover, no unhandled rejection', async () => {
  const rejections = [];
  const onUnhandled = (reason) => rejections.push(reason);
  process.on('unhandledRejection', onUnhandled);
  try {
    const args = validArgs();
    const base = makeCtx(args);
    let releaseSummarize = null;
    const summarizeGate = new Promise((r) => { releaseSummarize = r; });

    const ctx = {
      calls: base.calls,
      violations: base.violations,
      agent: async (prompt, opts) => {
        if ((opts || {}).label === 'summarize') await summarizeGate;
        return base.agent(prompt, opts);
      },
      // Discover's fan-out fails IMMEDIATELY; summarize only resolves several microtask
      // turns later, so the discover rejection is floating in between. Without a
      // settle-capturing wrapper the runtime sees an unhandled rejection there.
      parallel: async () => {
        if (releaseSummarize) {
          const r = releaseSummarize; releaseSummarize = null;
          Promise.resolve().then(() => {}).then(() => {}).then(() => {}).then(() => r());
        }
        throw new Error('simulated platform failure: parallel() unavailable');
      },
    };

    const runP = runWith(ctx, args);
    await drainMicrotasks();
    // Safety valve: a SEQUENTIAL implementation never reaches parallel(), so nothing would
    // ever release the gate. Release it here rather than hanging the suite.
    let deadlocked = false;
    if (releaseSummarize) { deadlocked = true; const r = releaseSummarize; releaseSummarize = null; r(); }
    const out = await runP;
    await new Promise((r) => setImmediate(r)); // let node surface any unhandled rejection

    assert.equal(deadlocked, false, 'discover must dispatch while summarize is still in flight');
    assert.equal(out.ok, false);
    assert.match(out.error, /parallel/);
    assert.equal(out.phaseReached, 'summarize', 'summarize completed before discover blew up');
    assert.equal(out.failingPhase, 'discover', 'error attribution is unchanged');
    assert.ok(out.checkpoints.phases && 'summarize' in out.checkpoints.phases);
    assert.ok(!('discover' in out.checkpoints.phases), 'the phase that threw is not recorded');
    assert.deepEqual(rejections, [], 'the eagerly-started discover promise must never float unhandled');
  } finally {
    process.removeListener('unhandledRejection', onUnhandled);
  }
});
