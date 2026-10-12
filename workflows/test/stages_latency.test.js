// A promise gate proves concurrent dispatch without relying on wall-clock timing.
// Output order and failure attribution still follow phase order.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runWith } from '../src/run.js';
import { makeFinding, validArgs, makeCtx } from './helpers/pipelineMock.js';

// Drain the microtask queue far enough that a SEQUENTIAL implementation has provably
// parked on the gate (it can make no further progress without it).
async function drainMicrotasks(n = 100) {
  for (let i = 0; i < n; i += 1) await Promise.resolve();
}

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
  assert.ok(persistedReport.includes('## Change Context\n\nThis is the change summary the review agents shared as context.\n\nreplayed summary'), 'the checkpointed summary flows into Change Context');
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
