// stages_latency.test.js — issue #38 stream D2: the four output-preserving latency
// changes to workflows/src/stages.js.
//
//  D2.1  the persisted checkpoint drops `filter` (pure, agent-free, zero-cost re-run)
//  D2.2  summarize runs CONCURRENTLY with discover (no data dependency)
//  D2.3  the two remaining sequential fan-out loops (verify slice-input writer groups,
//        report segments) go through parallel()
//  D2.4  the report-writer is no longer handed the shared context path
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
import { runWith, reportStage, verifyStage, slimPersistedCheckpoints, parseWriterPayload } from '../src/stages.js';
import { makeFinding, validArgs, makeCtx } from './helpers/pipelineMock.js';
import { deltaEnvelope } from './helpers/verifyDelta.js';

// Drain the microtask queue far enough that a SEQUENTIAL implementation has provably
// parked on the gate (it can make no further progress without it).
async function drainMicrotasks(n = 100) {
  for (let i = 0; i < n; i += 1) await Promise.resolve();
}

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
  // The empty-report guard's postFilterCount comes from the freshly re-derived filter set.
  assert.ok(!out2.gaps.some((g) => /empty_report/.test(g)), `no empty_report gap, got: ${out2.gaps}`);
  assert.equal(typeof out2.artifactPaths.report, 'string', 'a real report persisted');
});

// --- D2.1 resume hole: the empty-report guard must see the REPLAYED set ------
//
// Dropping `filter` from the persisted checkpoint means that on a resume postFilterCount is
// a FRESHLY recomputed number from a re-run discover/verify/validate/filter, while the
// delivered set is the REPLAYED challenge output. A resume that rediscovers nothing has
// postFilterCount 0, so a guard keyed on postFilterCount alone goes blind to a real
// delivered set sitting behind an empty report (issue #38, L2-1/L5-3). The guard fires on
// the UNION of the two counts — a strict superset of the old condition.

// Helper: run once with real findings and hand back the persisted slim checkpoint.
async function persistedCheckpointFromAFullRun() {
  const args = validArgs();
  let persisted = null;
  const ctx = makeCtx(args, { onPersist: (p) => { persisted = p.checkpoints; } });
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.deepEqual(Object.keys(persisted.phases), ['challenge']);
  return persisted;
}

test('D2.1 resume hole: an empty report with a REPLAYED challenge trips the guard even when the fresh filter set is empty', async () => {
  const persisted = await persistedCheckpointFromAFullRun();

  // Resume against a tree that now discovers NOTHING (postFilterCount === 0) with a
  // report-writer that returns a blank report. The replayed challenge still carries the
  // delivered findings, so an empty report here is a false negative and must never ship
  // silently. (Whitespace-only, not '': a falsy report is caught by reportStage's own
  // minimal-report fallback before this guard ever sees it.)
  const args2 = validArgs({ checkpoints: persisted });
  const ctx2 = makeCtx(args2, { findings: [], reportText: '   ' });
  const out2 = await runWith(ctx2, args2);

  assert.equal(out2.ok, true);
  assert.equal(out2.stats.highConfidence, 2, 'the replayed challenge findings ARE the delivered set');
  assert.equal(out2.stats.filter.kept ?? 0, 0, 'the freshly re-run filter kept nothing (postFilterCount === 0)');
  const gap = out2.gaps.find((g) => /empty_report/.test(g));
  assert.ok(gap, `the guard must fire on the replayed set, got: ${out2.gaps}`);
  assert.match(gap, /replayed/, 'the wording names the replayed set, not "0 survived the filter"');
  assert.equal(out2.artifactPaths.report, null, 'the empty report path is nulled');
  assert.equal(typeof out2.artifactPaths.findings, 'string', 'the delivered findings still persist');
});

test('D2.1 resume hole: a replayed EMPTY report checkpoint is re-run even when the fresh filter set is empty', async () => {
  const persisted = await persistedCheckpointFromAFullRun();

  // Same resume, but the crashed run also left a degenerate empty report in its checkpoint.
  // Re-running report is what the guard exists for; keyed on postFilterCount alone it would
  // skip past the stub and ship it.
  const args2 = validArgs({
    checkpoints: {
      phases: { challenge: persisted.phases.challenge, report: { report: '', gaps: [] } },
      completed: persisted.completed,
    },
  });
  const ctx2 = makeCtx(args2, { findings: [] });
  const out2 = await runWith(ctx2, args2);

  assert.equal(out2.ok, true);
  assert.ok(ctx2.calls.some((c) => (c.label || '') === 'report-writer'), 'report was re-dispatched, not skipped past');
  assert.equal(typeof out2.artifactPaths.report, 'string', 'a real report persisted');
  assert.ok(!out2.gaps.some((g) => /empty_report/.test(g)), `recovered cleanly, got: ${out2.gaps}`);
  assert.equal(out2.stats.highConfidence, 2, 'the replayed delivered set is unchanged by the recovery');
});

test('F2-1: an empty report with ONLY a replayed UNVERIFIED bucket still trips the guard', async () => {
  // The third hole in the same guard. challengeOut.unverified is the challenge-skipped /
  // cap-overflow bucket the report is CONTRACTUALLY required to render in its secondary
  // section. A resume can replay a challenge whose findings ALL landed there: postFilterCount
  // is 0 (the fresh filter discovered nothing) and deliveredCount is 0 (nothing survived to
  // delivery), yet the report still has real content to lose. Keyed on those two counts alone
  // the guard stays silent and a whitespace-only report ships with real content unreported.
  const args = validArgs({
    checkpoints: {
      phases: {
        challenge: {
          findings: [],
          unverified: [{ ...makeFinding('U1'), challenge: 'skipped' }],
          eliminated: [],
          gaps: ['challenge: 1 finding(s) over challengeCap=0 left unchallenged'],
          stats: { total_input: 1, dispatched: 0, completed: 0, skipped: 1, final_count: 0 },
          generated_at: '2026-07-18T00:00:00Z',
        },
      },
      completed: ['challenge'],
    },
  });
  const ctx = makeCtx(args, { findings: [], reportText: '   ' });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.equal(out.stats.highConfidence, 0, 'nothing was delivered');
  assert.equal(out.stats.unverified, 1, 'but the unverified bucket carries a real finding');
  const gap = out.gaps.find((g) => /empty_report/.test(g));
  assert.ok(gap, `the guard must fire on the replayed unverified bucket, got: ${out.gaps}`);
  // The bucket is named by what it IS (issue #25 req 8): "not blind-challenged", never
  // "unverified/pipeline-degraded", which was a whole-pipeline health claim this bucket is
  // in no position to make.
  assert.match(gap, /not blind-challenged/, 'the wording names the bucket that is actually at risk');
  assert.equal(out.artifactPaths.report, null, 'the empty report path is nulled');
});

test('F2-1: the resume wording names BOTH replayed buckets by count', async () => {
  const persisted = await persistedCheckpointFromAFullRun();
  // Same replayed challenge as the D2.1 resume-hole test (2 delivered, 0 unverified) — the
  // resume string must account for both buckets, not just the delivered one.
  const args2 = validArgs({ checkpoints: persisted });
  const ctx2 = makeCtx(args2, { findings: [], reportText: '   ' });
  const out2 = await runWith(ctx2, args2);
  assert.equal(
    out2.gaps.find((g) => /empty_report/.test(g)),
    'empty_report: report stage produced no report while 2 finding(s) replayed from the resumed '
    + 'challenge checkpoint would be delivered and 0 would be reported as not blind-challenged '
    + '— refusing to ship a silent empty report',
  );
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
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.ok(!ctx.calls.some((c) => c.label === 'summarize'), 'summarize was replayed, never dispatched');
  assert.ok(ctx.calls.some((c) => c.label === 'code-gauntlet:bug-detector'), 'discover still dispatched');
  // The replayed summary still reaches the report-writer by value.
  const reportCall = ctx.calls.find((c) => c.label === 'report-writer');
  assert.ok(reportCall.prompt.includes('replayed summary'), 'the checkpointed summary flows into the report input');
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

// --- D2.3: verify slice-input writer groups fan out through parallel() ------

// Findings big enough that the slice-input entries chunk into >1 writer GROUP
// (SEGMENT_CHAR_BUDGET is 100_000 serialized chars) with verifySliceSize=1.
function bigVerifyFindings(n = 60) {
  return Array.from({ length: n }, (_, i) => makeFinding(`V${i}`, { description: 'd'.repeat(3000) }));
}

test('D2.3: the verify slice-input writer groups dispatch through parallel(), not sequentially', async () => {
  const findings = bigVerifyFindings();
  const labels = [];
  let releaseFirst = null;
  const firstGate = new Promise((r) => { releaseFirst = r; });

  const ctx = {
    agent: async (prompt, opts) => {
      const label = (opts || {}).label || '';
      labels.push(label);
      if (label === 'verify-input-writer-0') await firstGate;
      if (label === 'verify-input-writer-1' && releaseFirst) { const r = releaseFirst; releaseFirst = null; r(); }
      return { written: [] }; // deliberately no write proof -> a deterministic degradation
    },
    parallel: async (thunks) => Promise.all(thunks.map(async (t) => {
      try { return await t(); } catch { return null; }
    })),
  };

  const runP = verifyStage(ctx, {
    findings, limits: { verifySliceSize: 1 }, policy: {}, nonce: 'n', headShaShort: 'abc1234',
    verify: { inputPathBase: 'p4', outputPathBase: 'p4o', baseBranch: 'main' },
  });
  await drainMicrotasks();
  let deadlocked = false;
  if (releaseFirst) { deadlocked = true; const r = releaseFirst; releaseFirst = null; r(); }
  const out = await runP;

  assert.equal(deadlocked, false, 'writer group 1 must dispatch while group 0 is still in flight');
  assert.ok(labels.filter((l) => l.startsWith('verify-input-writer-')).length > 1, 'more than one writer group dispatched');
  // Both groups return { written: [] } here, so the WHOLE set degrades — but that is now
  // incidental to this fixture (every group failed), not a first-failure-wins property.
  // Per-group / per-slice attribution is pinned by the two tests below, not here.
  assert.equal(out.verified, false, 'no write proof from either group -> every slice degrades to UNVERIFIED');
});

// Pulls the slice index back out of a materialized slice-input path
// (`${inputPathBase}.slice${i}.json`) so a test can attribute a writer group's
// dispatched entries to slice indices without hardcoding chunkBySerializedSize's
// packing boundaries.
function sliceIndexFromPath(path) {
  const m = /\.slice(\d+)\.json$/.exec(path);
  if (!m) throw new Error(`path has no slice index: ${path}`);
  return Number(m[1]);
}

test('D2.3: materializeVerifySlices surfaces EVERY failed group\'s own reason, attributed to its own slices, in slice-index order', async () => {
  // issue #54: this used to be "first failure wins, the rest of the run degrades under
  // that one message" — group 0 threw and its reason alone used to appear once. Now
  // every failed group degrades only the slices IT carried, each with ITS OWN reason,
  // and neither group's slices ever borrow the other's message.
  const findings = bigVerifyFindings(); // 60 -> exactly two writer groups under SEGMENT_CHAR_BUDGET
  const groupSliceIndices = {};
  const ctx = {
    agent: async (prompt, opts) => {
      const label = (opts || {}).label || '';
      if (label.startsWith('verify-input-writer-')) {
        groupSliceIndices[label] = parseWriterPayload(prompt).map((e) => sliceIndexFromPath(e.path));
      }
      if (label === 'verify-input-writer-0') throw new Error('group-0 boom');
      if (label === 'verify-input-writer-1') return null;
      return { written: [] };
    },
    parallel: async (thunks) => Promise.all(thunks.map(async (t) => {
      try { return await t(); } catch { return null; }
    })),
  };
  const out = await verifyStage(ctx, {
    findings, limits: { verifySliceSize: 1 }, policy: {}, nonce: 'n', headShaShort: 'abc1234',
    verify: { inputPathBase: 'p4', outputPathBase: 'p4o', baseBranch: 'main' },
  });

  const g0 = groupSliceIndices['verify-input-writer-0'];
  const g1 = groupSliceIndices['verify-input-writer-1'];
  assert.ok(g0 && g0.length > 0, 'writer group 0 dispatched with at least one slice');
  assert.ok(g1 && g1.length > 0, 'writer group 1 dispatched with at least one slice');
  assert.equal(g0.length + g1.length, findings.length, 'exactly two writer groups cover every slice (fixture assumption)');
  // Groups are contiguous chunks of the original slice order (issue #38's greedy
  // packer never reorders), so group 0 is the low contiguous range and group 1 picks
  // up immediately after it.
  assert.deepEqual(g0, Array.from({ length: g0.length }, (_, i) => i), 'group 0 is a contiguous low range starting at slice 0');
  assert.deepEqual(g1, Array.from({ length: g1.length }, (_, i) => g0.length + i), 'group 1 is the contiguous range immediately after group 0');

  // Both groups failed here, so every slice degrades -> one gap per slice (per-slice
  // granularity, not one gap per group), in strict slice-index order.
  assert.equal(out.verified, false);
  assert.equal(out.gaps.length, findings.length);
  for (let i = 0; i < findings.length; i += 1) {
    const gap = out.gaps[i];
    assert.match(gap, new RegExp(`^verify: UNVERIFIED — slice ${i} \\(slice-input group `), `gap ${i} is attributed to slice ${i}`);
    if (g0.includes(i)) {
      assert.match(gap, /slice-input group 0\): slice-input writer threw \(group-0 boom\)/, `slice ${i} (group 0) carries group 0's own reason`);
      assert.ok(!/returned null/.test(gap), `slice ${i} (group 0) must not borrow group 1's reason`);
    } else {
      assert.match(gap, /slice-input group 1\): slice-input writer returned null/, `slice ${i} (group 1) carries group 1's own reason`);
      assert.ok(!/threw/.test(gap), `slice ${i} (group 1) must not borrow group 0's reason`);
    }
  }
});

test('D2.3: a healthy writer group\'s slices reach the executor and stay verified while other groups fail (each with its own reason)', async () => {
  const findings = bigVerifyFindings(120); // >2 writer groups; group 0 is healthy
  const seen = [];
  const groupSliceIndices = {};
  const FAILED_LABELS = new Set(['verify-input-writer-1', 'verify-input-writer-2']);
  const ctx = {
    agent: async (prompt, opts) => {
      const label = (opts || {}).label || '';
      seen.push(label);
      if (label.startsWith('verify-input-writer-')) {
        const entries = parseWriterPayload(prompt);
        groupSliceIndices[label] = entries.map((e) => sliceIndexFromPath(e.path));
        if (label === 'verify-input-writer-1') return null;
        if (label === 'verify-input-writer-2') throw new Error('third-group boom');
        return { written: entries.map((e) => e.path) };
      }
      if (label.startsWith('verify-slice-')) {
        // A slice from a HEALTHY writer group reaches the executor. verifySliceSize is 1
        // here, so slice index i addresses findings[i] directly. Build a genuinely
        // TRUSTED delta envelope for that one finding (issue #25 PR2: the executor
        // answers with a per-id DECISION — verified/origin/severity/confidence — and can
        // no longer hand back a substitute finding at all). Retry-agnostic: whichever
        // attempt dispatches, its own nonce is what gets echoed into the receipt, so this
        // fixture proves reachability without asserting on which attempt landed.
        const sliceIndex = Number(/^verify-slice-(\d+)(?:-retry)?$/.exec(label)[1]);
        const nonce = (prompt.match(/--nonce (\S+)/) || [])[1];
        return deltaEnvelope([findings[sliceIndex]], { sha: 'abc1234', nonce, n_in: 1 });
      }
      throw new Error(`unexpected dispatch label: ${label}`);
    },
    parallel: async (thunks) => Promise.all(thunks.map(async (t) => {
      try { return await t(); } catch { return null; }
    })),
  };
  const out = await verifyStage(ctx, {
    findings, limits: { verifySliceSize: 1 }, policy: {}, nonce: 'n', headShaShort: 'abc1234',
    verify: { inputPathBase: 'p4', outputPathBase: 'p4o', baseBranch: 'main' },
  });

  const writerLabels = Object.keys(groupSliceIndices).sort();
  assert.ok(writerLabels.length > 2, 'at least three writer groups dispatched');
  assert.ok('verify-input-writer-0' in groupSliceIndices, 'group 0 dispatched');
  assert.ok('verify-input-writer-1' in groupSliceIndices, 'group 1 dispatched');
  assert.ok('verify-input-writer-2' in groupSliceIndices, 'group 2 dispatched');

  const failedIndices = [];
  const healthyIndices = [];
  for (const label of writerLabels) {
    const idxs = groupSliceIndices[label];
    (FAILED_LABELS.has(label) ? failedIndices : healthyIndices).push(...idxs);
  }
  failedIndices.sort((a, b) => a - b);
  healthyIndices.sort((a, b) => a - b);
  assert.equal(failedIndices.length + healthyIndices.length, findings.length, 'every slice belongs to exactly one writer group');
  // Groups 1 and 2 are adjacent in the (unreordered) chunking, so their failed slices
  // form one contiguous block.
  for (let k = 1; k < failedIndices.length; k += 1) {
    assert.equal(failedIndices[k], failedIndices[k - 1] + 1, 'failed-group slices form a contiguous block');
  }

  assert.equal(out.verified, false);
  assert.equal(out.findings.length, findings.length, 'no finding is ever dropped');

  // Slices from the healthy groups are NOT degraded and DID reach the executor. Under
  // the delta echo (issue #25 PR2) the executor can no longer substitute a different
  // finding for the one it was dispatched — it only returns a DECISION keyed by id — so
  // "reached the executor and was verified" can no longer be told apart from "degraded"
  // by a foreign `verified-${nonce}` id landing in the output; it shows up instead as the
  // slice's OWN dispatched finding surviving with a non-unknown origin.
  for (const i of healthyIndices) {
    assert.notEqual(out.findings[i].origin, 'unknown', `slice ${i} (healthy group) must not be degraded`);
    assert.equal(out.findings[i].id, findings[i].id, `slice ${i} carries its own dispatched finding — an executor cannot substitute one`);
    assert.ok(seen.some((l) => l === `verify-slice-${i}` || l === `verify-slice-${i}-retry`), `slice ${i} was dispatched to the executor`);
  }
  // Slices from the failed writer groups degrade to their ORIGINAL finding, origin=unknown,
  // and are NEVER dispatched to the executor.
  for (const i of failedIndices) {
    assert.equal(out.findings[i].origin, 'unknown', `slice ${i} (failed writer group) degrades to origin=unknown`);
    assert.equal(out.findings[i].id, findings[i].id, `slice ${i} keeps its original finding — nothing dropped`);
    assert.ok(!seen.some((l) => l === `verify-slice-${i}` || l === `verify-slice-${i}-retry`), `slice ${i} (writer failure) is never dispatched to the executor`);
  }

  // Gaps: one per degraded slice, in strict slice-index order, each with its OWN
  // group's reason — group 1's reason never appears on a group 2 slice and vice versa.
  assert.equal(out.gaps.length, failedIndices.length);
  const g1 = new Set(groupSliceIndices['verify-input-writer-1']);
  const g2 = new Set(groupSliceIndices['verify-input-writer-2']);
  for (let k = 0; k < failedIndices.length; k += 1) {
    const i = failedIndices[k];
    const gap = out.gaps[k];
    assert.match(gap, new RegExp(`^verify: UNVERIFIED — slice ${i} \\(slice-input group `), `gap ${k} is attributed to slice ${i}`);
    if (g1.has(i)) {
      assert.match(gap, /slice-input group 1\): slice-input writer returned null/, `slice ${i} (group 1) carries group 1's own reason`);
    } else {
      assert.ok(g2.has(i), `slice ${i} belongs to group 1 or group 2`);
      assert.match(gap, /slice-input group 2\): slice-input writer threw \(third-group boom\)/, `slice ${i} (group 2) carries group 2's own reason`);
    }
  }
});

// --- D2.3: report segments fan out through parallel() -----------------------

function bigReportFindings(n = 80) {
  return Array.from({ length: n }, (_, i) => makeFinding(`R${i}`, { description: 'x'.repeat(2000) }));
}

test('D2.3: report segments dispatch through parallel(), not sequentially', async () => {
  const labels = [];
  let releaseFirst = null;
  const firstGate = new Promise((r) => { releaseFirst = r; });
  const ctx = {
    agent: async (prompt, opts) => {
      const label = (opts || {}).label || '';
      labels.push(label);
      if (label === 'report-writer-0') await firstGate;
      if (label === 'report-writer-1' && releaseFirst) { const r = releaseFirst; releaseFirst = null; r(); }
      return { report: `body ${label}` };
    },
    parallel: async (thunks) => Promise.all(thunks.map(async (t) => {
      try { return await t(); } catch { return null; }
    })),
  };

  const runP = reportStage(ctx, { findings: bigReportFindings(), unverified: [], stats: {} });
  await drainMicrotasks();
  let deadlocked = false;
  if (releaseFirst) { deadlocked = true; const r = releaseFirst; releaseFirst = null; r(); }
  const out = await runP;

  assert.equal(deadlocked, false, 'report segment 1 must dispatch while segment 0 is still in flight');
  assert.ok(labels.length > 1, 'more than one report segment dispatched');
  assert.match(out.report, /## Report segment 1 of/);
});

test('D2.3: segmented report output ORDER stays byte-identical to the sequential concatenation', async () => {
  // Resolve OUT of dispatch order so a naive "push as they settle" would scramble the
  // sections. parallel() preserves INPUT order, so the concatenation must not move.
  const gates = new Map();
  const ctx = {
    agent: async (prompt, opts) => {
      const label = (opts || {}).label || '';
      const idx = Number(label.split('-').pop());
      // Segment 0 waits for every later segment to answer first.
      if (idx === 0) await Promise.all([...gates.values()]);
      else {
        let done; gates.set(idx, new Promise((r) => { done = r; }));
        await Promise.resolve();
        done();
      }
      return { report: `body ${label}` };
    },
    parallel: async (thunks) => Promise.all(thunks.map(async (t) => {
      try { return await t(); } catch { return null; }
    })),
  };
  const findings = bigReportFindings();
  const out = await reportStage(ctx, { findings, unverified: [], stats: {} });

  const n = out.report.split('## Report segment ').length - 1;
  assert.ok(n > 1, 'the payload segmented');
  const expected = Array.from({ length: n }, (_, i) => `## Report segment ${i + 1} of ${n}\n\nbody report-writer-${i}`).join('\n\n');
  assert.equal(out.report, expected);
  assert.deepEqual(out.gaps, []);
});

test('D2.3: a mid-list report segment failure degrades only that section, order + message unchanged', async () => {
  const ctx = {
    agent: async (prompt, opts) => {
      const label = (opts || {}).label || '';
      if (label === 'report-writer-1') throw new Error('segment boom');
      return { report: `body ${label}` };
    },
    parallel: async (thunks) => Promise.all(thunks.map(async (t) => {
      try { return await t(); } catch { return null; }
    })),
  };
  const out = await reportStage(ctx, { findings: bigReportFindings(), unverified: [], stats: {} });

  const n = out.report.split('## Report segment ').length - 1;
  assert.ok(n > 1);
  assert.match(out.report, /## Report segment 1 of/);
  assert.match(out.report, /## Report segment 2 of/);
  // Segment index 1 (heading "2 of n") degraded to the deterministic minimal section.
  assert.match(out.report, /# Code Gauntlet \(minimal report\)/);
  assert.equal(out.gaps.length, 1);
  assert.equal(
    out.gaps[0],
    'report segment 1: writer agent threw (segment boom) — assembled a minimal report from pipeline stats',
  );
  // The sections stay in index order: segment 1's body precedes the minimal section.
  assert.ok(out.report.indexOf('body report-writer-0') < out.report.indexOf('(minimal report)'));
});

// --- D2.4: the report-writer is no longer handed the shared context path ----

test('D2.4: reportPrompt carries no context path even when contextPath is supplied', async () => {
  let prompt = null;
  const ctx = {
    agent: async (p) => { prompt = p; return { report: '# r' }; },
    parallel: async (thunks) => Promise.all(thunks.map((t) => t())),
  };
  await reportStage(ctx, {
    findings: [makeFinding('F1')], unverified: [], stats: {},
    contextPath: '.code-gauntlet/code-gauntlet-context-abc1234.md',
  });
  assert.ok(prompt, 'the report-writer was dispatched');
  assert.ok(!/code-gauntlet-context-/.test(prompt), 'no context file path in the report prompt');
  assert.ok(!/Read the shared context/.test(prompt), 'no shared-context read instruction');
});

test('D2.4: runWith\'s reportInput carries no contextPath', async () => {
  const args = validArgs();
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  const reportCall = ctx.calls.find((c) => c.label === 'report-writer');
  assert.ok(reportCall, 'a report-writer dispatched');
  assert.ok(!/contextPath/.test(reportCall.prompt), 'reportInput does not carry a contextPath field');
  assert.ok(!/code-gauntlet-context-/.test(reportCall.prompt), 'no context file path reaches the report-writer');
  // Other stages STILL get the context path — this change is scoped to the report-writer.
  const discoverCall = ctx.calls.find((c) => c.label === 'code-gauntlet:bug-detector');
  assert.match(discoverCall.prompt, /code-gauntlet-context-abc1234\.md/);
});
