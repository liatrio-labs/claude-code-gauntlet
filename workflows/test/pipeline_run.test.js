// pipeline_run.test.js — orchestration-contract tests for the full run() waist
// (Task 13). runWith(ctx, args) is the testable seam: it validates args, threads
// every stage summarize->...->report inside ONE top-level try/catch, then persists
// via writeArtifacts, and returns the compact envelope. A fully mocked ctx drives
// every agent()/parallel() dispatch (the runtime globals do not exist under
// node:test), so these tests assert the GLUE, not the individual stages.
//
// Dispatch contract (Phase 0-verified + Workflow docs): agent(promptString, opts);
// parallel(thunks) where each thunk calls agent(...). The mock ctx (helpers/pipelineMock)
// asserts this on every dispatch, so these tests can never mask an object-vs-prompt bug.
//
// Degradation contract under test (Phase 0 throw contract):
//  - A catastrophic platform failure (parallel() itself throwing) inside a core stage
//    (discover) bubbles to the top-level catch -> { ok:false, error, phaseReached }.
//    run() NEVER throws. (Agent member failures null-isolate; single-dispatch stages
//    catch their own throws — so a parallel() failure is the realistic catch trigger.)
//  - reportStage / writeArtifacts each wrap their own agent() in try/catch, so an
//    agent throw there degrades to a minimal report / partial-artifacts gap with
//    ok:true — report/persistence failure is non-fatal.
//  - argsVersion mismatch is rejected up front with an ok:false envelope.
//  - A present checkpoint for a phase skips that phase's dispatch entirely.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  runWith, summarize, reportStage, writeArtifacts, checkpointPath, readCheckpoints, buildResumeCheckpoints,
} from '../src/stages.js';
import { makeFinding, makeFindings, validArgs, makeCtx } from './helpers/pipelineMock.js';

// Node/browser host globals the workflow runtime SANDBOX does not provide (but node:test
// does). Deleting them makes the pipeline run under sandbox-parity conditions, so a
// reintroduced dependency (the structuredClone crash) fails a test instead of only prod.
const SANDBOX_ABSENT_GLOBALS = ['structuredClone', 'Buffer', 'TextEncoder', 'TextDecoder', 'setTimeout', 'queueMicrotask'];
async function withSandboxGlobals(fn) {
  const saved = {};
  for (const name of SANDBOX_ABSENT_GLOBALS) { saved[name] = globalThis[name]; delete globalThis[name]; }
  try { return await fn(); } finally { for (const name of SANDBOX_ABSENT_GLOBALS) globalThis[name] = saved[name]; }
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
  // On persist SUCCESS the return stays compact: phase names only, no findings-bearing
  // phases map (that lives in the persisted artifact at artifactPaths.checkpoints).
  assert.ok(Array.isArray(out.checkpoints.completed));
  assert.ok(!('phases' in out.checkpoints), 'success return must not carry the phases bulk');
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

test('sandbox parity: full pipeline runs ok with node-only globals (structuredClone etc.) removed', async () => {
  // Regression guard for the live-smoke crash: applyChallenges used structuredClone, a
  // global the runtime sandbox lacks. With it deleted, the OLD code threw in the challenge
  // stage -> top-level catch -> ok:false. deepClone (JSON round-trip) removes that dependency.
  const args = validArgs();
  const out = await withSandboxGlobals(() => runWith(makeCtx(args), args));
  assert.equal(out.ok, true, `pipeline must not depend on node-only globals; gaps: ${out.gaps}`);
  assert.equal(out.phaseReached, 'report');
  assert.equal(out.stats.highConfidence, 2);
});

// --- Validate dispatch schema is object-rooted (Messages API contract) -------

test('every stage dispatch uses an object-rooted schema (no array-rooted 400)', async () => {
  // The Messages API rejects an array-rooted tool input_schema (the VALIDATE_SCHEMA 400
  // the live smoke run hit). The mock asserts this per-dispatch; here we also check the
  // recorded calls directly, including the validate batch specifically.
  const args = validArgs();
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.deepEqual(ctx.violations, [], `object-root violations: ${ctx.violations.join('; ')}`);
  for (const c of ctx.calls) {
    assert.equal(c.schema.type, 'object', `dispatch ${c.label} must be object-rooted, got ${c.schema.type}`);
  }
  const validate = ctx.calls.find((c) => c.label.startsWith('validate-batch-'));
  assert.ok(validate, 'a validate batch dispatched');
  assert.equal(validate.schema.type, 'object');
  assert.ok(validate.schema.properties.validations, 'validate schema wraps the array under { validations }');
});

// --- Empty-report false-negative guard --------------------------------------

test('empty report while findings survive the filter -> ok:true, empty_report gap, report path nulled', async () => {
  // A report-writer that returns whitespace-only content is a false negative: the pipeline
  // must NOT ship it silently. ok stays true, findings still persist, but the report path
  // is nulled and an explicit empty_report gap is recorded.
  const args = validArgs();
  const ctx = makeCtx(args, { reportText: '   ' });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.equal(out.stats.highConfidence, 2, 'findings survived the filter/challenge');
  assert.ok(out.gaps.some((g) => /empty_report/.test(g)), `expected an empty_report gap, got: ${out.gaps}`);
  assert.equal(out.artifactPaths.report, null, 'the empty report path is nulled');
  assert.equal(typeof out.artifactPaths.findings, 'string', 'findings are still persisted');
});

test('resume replaying an empty report checkpoint re-runs report+persist (not skipped)', async () => {
  // The crashed-persist false negative: a prior run left an empty report in its checkpoint.
  // On resume, runPhase would normally REUSE it — the guard must instead re-run report from
  // scratch so a real report persists, rather than shipping the degenerate empty stub.
  const args = validArgs({ checkpoints: { report: { report: '', gaps: [] } } });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.ok(ctx.calls.some((c) => c.label === 'report-writer'), 'report was re-dispatched, not skipped');
  assert.equal(typeof out.artifactPaths.report, 'string', 'a real report persisted');
  assert.ok(!out.gaps.some((g) => /empty_report/.test(g)), 'recovered cleanly — no empty_report gap remains');
});

// --- Top-level catch --------------------------------------------------------

test('a throw in a core stage (discover) is caught by the top-level try/catch', async () => {
  const args = validArgs();
  const ctx = makeCtx(args, { parallelThrows: true });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, false);
  assert.equal(typeof out.error, 'string');
  assert.match(out.error, /parallel/);
  // summarize (single agent) completed; discover's parallel() throw was caught.
  assert.equal(out.phaseReached, 'summarize');
  // The failure envelope still carries the compact keys.
  assert.ok('artifactPaths' in out);
  assert.ok('stats' in out);
  // Nothing was persisted on the throw path, so the resume state rides in the return:
  // the completed phase (summarize) is recoverable.
  assert.ok(out.checkpoints && out.checkpoints.phases, 'catch path carries the in-memory phases map');
  assert.ok('summarize' in out.checkpoints.phases);
  assert.ok(!('discover' in out.checkpoints.phases), 'the phase that threw is not recorded');
});

test('run never throws out of runWith even when a stage throws', async () => {
  const args = validArgs();
  const ctx = makeCtx(args, { parallelThrows: true });
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
  // Persistence failed, so the resume state rides in the compact return instead: every
  // phase (through report) is recoverable without re-running.
  assert.ok(out.checkpoints && out.checkpoints.phases, 'writer-failure carries the in-memory phases map');
  assert.ok('report' in out.checkpoints.phases);
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
  // No discovery dispatch (its label IS a 'deep-review:<agent>' agentType) ever happened.
  assert.ok(!ctx.calls.some((t) => t.label.startsWith('deep-review:')), 'discover dispatch was skipped');
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
    agent: async (prompt, opts) => { calls.push(opts); return { report: `segment body for ${opts.label}` }; },
    parallel: async () => [],
  };
  const out = await reportStage(ctx, { findings: big, unverified: [], stats: {}, generatedAt: '2026-07-18T00:00:00Z' });

  assert.ok(calls.length > 1, `expected >1 report-writer dispatch, got ${calls.length}`);
  assert.ok(calls.every((t) => t.agentType === 'deep-review:report-writer'));
  assert.match(out.report, /Report segment 1 of/);
  assert.match(out.report, new RegExp(`Report segment ${calls.length} of ${calls.length}`));
  assert.equal(out.gaps.length, 0, 'all segments rendered cleanly');
});

// --- buildResumeCheckpoints truncation (compact-return principle) ------------

test('buildResumeCheckpoints carries the phases map when it fits the budget', () => {
  const phaseOutputs = { summarize: { summary: 's' }, discover: { findings: [{ id: 'F1' }] } };
  const cp = buildResumeCheckpoints(phaseOutputs);
  assert.deepEqual(cp.phases, phaseOutputs);
  assert.deepEqual(cp.completed, ['summarize', 'discover']);
  assert.ok(!cp.truncated);
});

test('buildResumeCheckpoints truncates to names-only when the phases map exceeds the budget', () => {
  // A single phase whose serialized output blows past the 100k char budget.
  const huge = { findings: Array.from({ length: 400 }, (_, i) => makeFinding(`F${i}`, { description: 'y'.repeat(300) })) };
  const cp = buildResumeCheckpoints({ discover: huge, verify: huge });
  assert.ok(!('phases' in cp), 'oversized phases map is dropped from the compact return');
  assert.equal(cp.truncated, true);
  assert.deepEqual(cp.completed, ['discover', 'verify']);
});

test('reportStage under the budget stays a single dispatch', async () => {
  const calls = [];
  const ctx = {
    agent: async (prompt, opts) => { calls.push(opts); return { report: 'one report' }; },
    parallel: async () => [],
  };
  const out = await reportStage(ctx, { findings: makeFindings(), unverified: [], stats: {} });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].label, 'report-writer');
  assert.equal(out.report, 'one report');
});

// --- Structural dispatch-contract sweep (masking-proof) ---------------------

test('sweep: runWith drives every stage with STRING prompts + valid JSON Schemas', async () => {
  const args = validArgs();
  const ctx = makeCtx(args); // its agent()/parallel() assert the platform contract
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.deepEqual(ctx.violations, [], `dispatch-contract violations: ${ctx.violations.join('; ')}`);
  // Every dispatch label family was exercised (so the assertions actually ran on each).
  const seen = ctx.calls.map((c) => c.label);
  for (const family of ['summarize', 'deep-review:bug-detector', 'verify-input-writer', 'verify-slice-', 'validate-batch-', 'challenge-', 'report-writer', 'artifact-writer']) {
    assert.ok(seen.some((l) => l === family || l.startsWith(family)), `swept dispatch family: ${family}`);
  }
  // And every recorded dispatch carried a string prompt + an object schema.
  for (const c of ctx.calls) {
    assert.equal(typeof c.prompt, 'string');
    assert.equal(typeof c.schema, 'object');
    assert.equal(typeof c.schema.type, 'string');
  }
});

test('sweep: bucketed summarize + segmented report emit only contract-valid dispatches', async () => {
  const args = validArgs();
  // Bucketed summarize (parallel thunks + merge): >500 lines AND more files than the bucket.
  const files = Array.from({ length: 50 }, (_, i) => `f${i}.js`);
  const sctx = makeCtx(args);
  await summarize(sctx, { changedFiles: files, changedLines: 600, limits: { summarizeBucketSize: 20 }, policy: {} });
  assert.deepEqual(sctx.violations, [], `summarize violations: ${sctx.violations.join('; ')}`);
  assert.ok(sctx.calls.some((c) => c.label.startsWith('summarize-bucket-')));
  assert.ok(sctx.calls.some((c) => c.label === 'summarize-merge'));

  // Segmented report (sequential per-chunk dispatches).
  const big = Array.from({ length: 80 }, (_, i) => makeFinding(`F${i}`, { description: 'x'.repeat(2000) }));
  const rctx = makeCtx(args);
  await reportStage(rctx, { findings: big, unverified: [], stats: {}, policy: {} });
  assert.deepEqual(rctx.violations, [], `report violations: ${rctx.violations.join('; ')}`);
  assert.ok(rctx.calls.filter((c) => c.label.startsWith('report-writer-')).length > 1);
});
