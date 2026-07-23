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
  coarsenLimits, plannedArtifactPaths,
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

// --- Agent-count guard wiring (coarsenLimits applied by runWith) -------------

test('agent-count guard: benchmark-scale inputs leave the limits untouched (coarsening is inert)', async () => {
  const args = validArgs();
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  // 2 findings against validateBatch 25 / verifySliceSize 100 / challengeCap 40: the
  // dispatch counts are exactly what the RAW limits produce — the guard never fired.
  assert.equal(ctx.calls.filter((t) => (t.label || '').startsWith('verify-slice-')).length, 1);
  assert.equal(ctx.calls.filter((t) => (t.label || '').startsWith('validate-batch-')).length, 1);
  assert.equal(ctx.calls.filter((t) => (t.label || '').startsWith('challenge-')).length, 2);
});

test('agent-count guard: a pathological changed-file count coarsens the summarize bucket at entry', async () => {
  const changedFiles = Array.from({ length: 20000 }, (_, i) => `src/f${i}.js`);
  const args = validArgs({
    changedFiles,
    changedLines: 100000,
    limits: { validateBatch: 25, verifySliceSize: 100, challengeCap: 40, summarizeBucketSize: 1 },
  });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  const eff = coarsenLimits(args.limits, changedFiles.length, 0);
  assert.ok(eff.summarizeBucketSize > 1, 'guard must fire for this input');
  const bucketCalls = ctx.calls.filter((t) => (t.label || '').startsWith('summarize-bucket-')).length;
  assert.equal(bucketCalls, Math.ceil(changedFiles.length / eff.summarizeBucketSize));
  assert.ok(bucketCalls + 10 < 900, 'summarize fan-out stays under the platform guard');
});

test('agent-count guard: a pathological finding count coarsens validate batches after merge', async () => {
  // 1000 distinct findings, verifySliceSize large enough for ONE slice (the mock echo
  // only trusts single-slice runs), validateBatch 1 so the validate term (1000) alone
  // trips the guard. Post-merge coarsening must shrink the validate fan-out.
  const findings = Array.from({ length: 1000 }, (_, i) => makeFinding(`F${i}`));
  const args = validArgs({
    limits: { validateBatch: 1, verifySliceSize: 2000, challengeCap: 40, summarizeBucketSize: 20 },
  });
  const ctx = makeCtx(args, { findings });
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  const eff = coarsenLimits(args.limits, 0, findings.length);
  assert.ok(eff.validateBatch > 1, 'guard must fire for this input');
  const batchCalls = ctx.calls.filter((t) => (t.label || '').startsWith('validate-batch-')).length;
  assert.equal(batchCalls, Math.ceil(findings.length / eff.validateBatch));
  assert.ok(batchCalls < 900, 'validate fan-out stays under the platform guard');
});

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

// --- Description flows intact through every stage (regression) ---------------

test('a long description survives merge->verify->validate->filter->challenge->persist unchanged', async () => {
  // Regression for the mid-pipeline description strip: verifyStage collects the executor's
  // result.verified BY VALUE as THE findings for every later stage, but VERIFY_SCHEMA once
  // declared those items as { type:'object', properties:{} }. StructuredOutput leaves an
  // empty-properties object unconstrained, so the executor dropped the (largest) `description`
  // field when echoing the --output file "verbatim". Emptied descriptions then flowed through
  // validate/filter, where the injection guard (short description + high confidence) false-fired
  // and eliminated high-confidence findings (golden matches). Two guarantees are asserted here:
  //   (1) the DATA FLOW — no stage strips a description that verify returns; and
  //   (2) the SCHEMA SHAPE — the verify dispatch declares `description` on its verified/eliminated
  //       items (the actual root-cause fix; a mock alone can't catch a schema regression because
  //       it never applies StructuredOutput's field-dropping).
  const longDescription =
    'When authenticating via the API-key path, organization_context.member is None but line 42 '
    + 'dereferences member.role without a guard, so any API-key request that resolves to a '
    + 'membership-less principal raises AttributeError before the authorization check runs. The '
    + 'same principal on the session path is guarded, so the two auth paths disagree and the '
    + 'API-key path crashes instead of returning 403 — a security-relevant divergence that also '
    + 'blocks every downstream handler for that request. The fix is to check member for None on '
    + 'the API-key branch and return the same 403 the session branch returns, keeping the two '
    + 'authentication paths behaviourally identical so neither leaks an unhandled exception to the '
    + 'client or the logs, and so the authorization decision is reached on both paths.';
  assert.ok(longDescription.length > 500, 'fixture description must be long enough to exercise the strip');

  const finding = makeFinding('BUG1', { description: longDescription, confidence: 90 });
  const args = validArgs();
  let persisted = null;
  const ctx = makeCtx(args, { findings: [finding], onPersist: (payload) => { persisted = payload; } });
  const out = await runWith(ctx, args);

  // (1) Data flow: the finding survived to persistence with its description byte-for-byte intact
  // (and the v2 `body` alias mirrors it), NOT emptied or truncated by any stage.
  assert.equal(out.ok, true);
  assert.ok(persisted, 'artifact-writer received a payload');
  const survivor = (persisted.findings || []).find((f) => f.id === 'BUG1');
  assert.ok(survivor, 'the high-confidence finding survived the filter+challenge (its description was not emptied)');
  assert.equal(survivor.description, longDescription, 'description reaches persist unchanged');
  assert.equal(survivor.body, longDescription, 'the persisted v2 body alias mirrors the full description');

  // (2) Schema shape: the verify dispatch must declare the FULL finding item — `description`
  // present, and confidence typed NUMBER (verify_findings.py re-scores it) — so StructuredOutput
  // preserves description instead of letting the executor drop it. A revert to properties:{} fails here.
  const verifyCall = ctx.calls.find((c) => (c.label || '').startsWith('verify-slice-'));
  assert.ok(verifyCall && verifyCall.schema, 'a verify-slice was dispatched with a schema');
  for (const arr of ['verified', 'eliminated']) {
    const itemProps = verifyCall.schema.properties.result.properties[arr].items.properties;
    assert.ok(itemProps && itemProps.description, `verify ${arr} items must declare description (not properties:{})`);
    assert.equal(itemProps.description.type, 'string', `verify ${arr} description must be typed string`);
    assert.equal(itemProps.confidence.type, 'number', `verify ${arr} confidence must be typed number (post-verify numeric)`);
  }
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
  // L6: the envelope names the phase that actually THREW — narrating from phaseReached
  // misattributed a Filter crash as "failed during Validate" in the live run.
  assert.equal(out.failingPhase, 'discover');
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
  const out = await writeArtifacts(ctx, { findings: [makeFinding('F1')], report: '# r', checkpoints: {}, outputDir: '.code-gauntlet', headShaShort: 'abc1234' });
  assert.ok(out.gaps.some((g) => /partial|artifact/i.test(g)));
  assert.equal(out.artifactPaths.findings, null);
});

// Item 5: the artifact-writer echo must ACCOUNT for all four planned paths — a schema-valid
// but degenerate echo (empty {} or partial) is no write proof and degrades to partial.
test('writeArtifacts: an empty {} echo (no write proof) degrades to a partial-artifacts gap', async () => {
  const wIn = { findings: [makeFinding('F1')], postReview: [], report: '# r', checkpoints: {}, outputDir: '.code-gauntlet', headShaShort: 'abc1234' };
  const ctx = { agent: async () => ({}), parallel: async () => [] }; // schema-valid, accounts for nothing
  const out = await writeArtifacts(ctx, wIn);
  assert.equal(out.partial, true);
  assert.equal(out.artifactPaths.findings, null);
  assert.ok(out.gaps.some((g) => /write proof|partial|artifact/i.test(g)));
});

test('writeArtifacts: a partial echo (missing one planned path) also degrades to partial-artifacts', async () => {
  const wIn = { findings: [makeFinding('F1')], postReview: [], report: '# r', checkpoints: {}, outputDir: '.code-gauntlet', headShaShort: 'abc1234' };
  const paths = plannedArtifactPaths(wIn.outputDir, wIn.headShaShort);
  const partialEcho = { ...paths }; delete partialEcho.checkpoints; // three of four
  const ctx = { agent: async () => ({ artifactPaths: partialEcho }), parallel: async () => [] };
  const out = await writeArtifacts(ctx, wIn);
  assert.equal(out.partial, true);
  assert.equal(out.artifactPaths.report, null);
});

test('writeArtifacts: a faithful echo of all four planned paths persists (partial:false)', async () => {
  const wIn = { findings: [makeFinding('F1')], postReview: [], report: '# r', checkpoints: {}, outputDir: '.code-gauntlet', headShaShort: 'abc1234' };
  const paths = plannedArtifactPaths(wIn.outputDir, wIn.headShaShort);
  const ctx = { agent: async () => ({ artifactPaths: paths }), parallel: async () => [] };
  const out = await writeArtifacts(ctx, wIn);
  assert.equal(out.partial, false);
  assert.equal(out.artifactPaths.findings, paths.findings);
  assert.deepEqual(out.gaps, []);
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
  const discoverCheckpoint = { findings: makeFindings().map((f) => ({ ...f, agent: 'code-gauntlet:bug-detector' })), gaps: [], degraded: [] };
  const args = validArgs({ checkpoints: { discover: discoverCheckpoint } });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.equal(out.phaseReached, 'report');
  // No discovery dispatch (its label IS a 'code-gauntlet:<agent>' agentType) ever happened.
  assert.ok(!ctx.calls.some((t) => t.label.startsWith('code-gauntlet:')), 'discover dispatch was skipped');
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

test('checkpoint round-trip: persisted checkpoint is SLIM — only the resume-consumed phases carry full output', async () => {
  // Run 1 produces the checkpoint artifact the writer persists; capture it via onPersist.
  const args1 = validArgs();
  let persistedCheckpoints = null;
  const ctx1 = makeCtx(args1, { onPersist: (payload) => { persistedCheckpoints = payload.checkpoints; } });
  const out1 = await runWith(ctx1, args1);
  assert.equal(out1.ok, true);
  assert.ok(persistedCheckpoints, 'writer received a checkpoints payload');

  // The SLIM persisted checkpoint keeps FULL output only for the two resume-consumed phases
  // (filter carries the empty-report guard's count; challenge carries the delivered findings).
  assert.deepEqual(
    Object.keys(persistedCheckpoints.phases).sort(), ['challenge', 'filter'],
    'only filter + challenge keep full output in the persisted checkpoint',
  );
  // Every completed phase is still accounted for by NAME + a bare count (observability without
  // the by-value findings bulk the OLD full-map checkpoint duplicated).
  for (const phase of ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report']) {
    assert.ok(persistedCheckpoints.completed.includes(phase), `completed lists phase '${phase}'`);
    assert.equal(typeof persistedCheckpoints.counts[phase], 'number', `counts has a number for '${phase}'`);
  }
  // The upstream phases' full findings arrays are GONE from the persisted checkpoint.
  for (const phase of ['summarize', 'discover', 'merge', 'verify', 'validate', 'report']) {
    assert.ok(!(phase in persistedCheckpoints.phases), `phase '${phase}' full output dropped from the checkpoint`);
  }

  // Run 2 feeds the slim artifact straight back. The preserved phases are SKIPPED (challenge
  // is not re-dispatched — its delivered findings are reused verbatim) and the rest RE-RUN.
  const args2 = validArgs({ checkpoints: persistedCheckpoints });
  const ctx2 = makeCtx(args2);
  const out2 = await runWith(ctx2, args2);
  assert.equal(out2.ok, true);
  assert.equal(out2.phaseReached, 'report');
  assert.ok(!ctx2.calls.some((c) => c.label.startsWith('challenge-')), 'challenge reused from checkpoint (not re-dispatched)');
  assert.ok(ctx2.calls.some((c) => c.label.startsWith('code-gauntlet:bug-detector')), 'a non-preserved phase (discover) re-ran');
  assert.ok(ctx2.calls.some((c) => c.label === 'report-writer'), 'report re-ran');
  assert.ok(ctx2.calls.some((c) => c.label === 'artifact-writer'), 'the writer ran');
  // The delivered high-confidence set is reproduced exactly across the resume.
  assert.equal(out2.stats.highConfidence, 2, 'the preserved challenge findings are delivered unchanged');
});

test('slimPersistedCheckpoints keeps only filter+challenge full, counts every phase, and the writer omits unverified', async () => {
  const args = validArgs();
  let persisted = null;
  const ctx = makeCtx(args, { onPersist: (payload) => { persisted = payload; } });
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.ok(persisted, 'writer received a payload');
  // Change 3: the pipeline-degraded `unverified` bucket is no longer carried by value in the
  // writer prompt (it is persisted to no file; the report + checkpoint carry it).
  assert.ok(!('unverified' in persisted), 'writer payload no longer carries the unverified bucket');
  // Change 2: the checkpoint the writer persisted is the slim shape.
  assert.ok(persisted.checkpoints.phases.challenge, 'challenge output preserved for resume');
  assert.ok(persisted.checkpoints.phases.filter, 'filter output preserved for the empty-report guard count');
  assert.ok(persisted.checkpoints.counts, 'per-phase counts present');
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
  assert.ok(calls.every((t) => t.agentType === 'code-gauntlet:report-writer'));
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
  for (const family of ['summarize', 'code-gauntlet:bug-detector', 'verify-input-writer', 'verify-slice-', 'validate-batch-', 'challenge-', 'report-writer', 'artifact-writer']) {
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
