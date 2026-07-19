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

// A canonical discovery finding carrying every REQUIRED_FIELD (merge validates
// against these) plus the fields the downstream filter/challenge stages read.
// Built fresh per call so stage mutation (confidence boosts, origin, tags) never
// leaks across dispatches or tests.
function makeFinding(id, over = {}) {
  return {
    id,
    file: `${id}.js`,
    line_start: 10,
    line_end: 10,
    title: `finding ${id}`,
    description: `a genuine correctness problem in ${id} that is described in enough words to clear the injection and threshold filters`,
    severity: 'high',
    confidence: 90,
    dimension: 'bug',
    origin: 'new',
    evidence: '',
    cross_file_refs: [],
    code: `const ${id} = broken();`,
    ...over,
  };
}

function makeFindings() {
  return [makeFinding('F1'), makeFinding('F2')];
}

// A fully valid args waist (every REQUIRED field from args.js). `over` patches it.
function validArgs(over = {}) {
  return {
    argsVersion: 1,
    mode: 'headless',
    repoRoot: '/repo',
    outputDir: '.deep-review',
    headShaShort: 'abc1234',
    nonce: 'nonce-xyz',
    generatedAt: '2026-07-18T00:00:00Z',
    diffPath: '/repo/.deep-review/diff.patch',
    changedFilesPath: '/repo/.deep-review/changed.txt',
    agentFlags: {},
    policy: {},
    limits: { validateBatch: 10, verifySliceSize: 100, challengeCap: 40, summarizeBucketSize: 20 },
    ...over,
  };
}

// Mock ctx. Records every dispatched task on `calls`. Each agent()/parallel()
// member returns a coherent per-stage envelope so the happy path threads all the
// way to report. Options:
//   - agentThrowLabel: agent() throws when task.label === this (report/writer tests)
//   - throwOnDiscover: parallel() throws when handed discovery tasks (top-level catch test)
function makeCtx(args, opts = {}) {
  const calls = [];
  const A = args;

  const agent = async (task) => {
    calls.push(task);
    const label = task.label || '';
    if (opts.agentThrowLabel && label === opts.agentThrowLabel) {
      throw new Error(`injected agent throw on ${label}`);
    }
    if (label === 'summarize' || label === 'summarize-merge') return { summary: 'the PR changes X' };
    if (label.startsWith('verify-slice-')) {
      // Echo a receipt the verify stage will TRUST: same head sha, the per-slice
      // derived nonce `${nonce}.0` (one slice because verifySliceSize > nFindings),
      // and n_in === the slice length. verified+eliminated must account for n_in.
      const verified = makeFindings().map((f) => ({ ...f, origin: 'new' }));
      return {
        status: 'ok',
        receipt: { sha: A.headShaShort, n_in: verified.length, nonce: `${A.nonce}.0` },
        result: { verified, eliminated: [], batches: [], stats: {} },
      };
    }
    if (label === 'report-writer') return { report: '# Deep Review\n\nHigh-confidence findings: 2' };
    if (label === 'artifact-writer') return { artifactPaths: {} };
    return null;
  };

  const parallel = async (tasks) => {
    const isDiscover = tasks.some((t) => Array.isArray(t.dimensions));
    if (isDiscover && opts.throwOnDiscover) throw new Error('injected parallel throw in discover');
    return Promise.all(tasks.map(async (t) => {
      calls.push(t);
      const label = t.label || '';
      if (label.startsWith('summarize-bucket-')) return { summary: 'partial' };
      if (label.startsWith('validate-batch-')) return []; // no confidence adjustments
      if (label.startsWith('challenge-')) return { score: 80, justification: 'claim holds' };
      // discovery task: label IS the agentType. Only bug-detector yields findings.
      if (Array.isArray(t.dimensions)) {
        if (t.agentType === 'deep-review:bug-detector') return { findings: makeFindings(), complete: true, total_seen: 2 };
        return { findings: [], complete: true, total_seen: 0 };
      }
      return null;
    }));
  };

  return { calls, agent, parallel };
}

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
