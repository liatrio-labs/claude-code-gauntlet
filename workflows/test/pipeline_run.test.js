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
  coarsenLimits, plannedArtifactPaths, deriveAgentFlags,
} from '../src/stages.js';
import { makeFinding, makeFindings, validArgs, makeCtx } from './helpers/pipelineMock.js';
import { AGENTS, DIMENSIONS } from '../src/registry.js';

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
  // The slice-input proof ledger (issue #69 / #25 req 4-6) must actually be surfaced on
  // run()'s stats, not just computed and dropped — nothing else in this file touches
  // `stats.inputProof`, so deleting the `inputProof: verifyOut.inputProof` line from
  // run()'s stats block would otherwise pass the whole suite.
  assert.deepEqual(out.stats.inputProof, { slices: 1, proven: 1, recovered: 0, mismatched: 0, missing: 0, unprovable: 0 });
});

test('partially-degraded verify: one failed slice keeps origin=unknown; healthy slices and downstream stages survive', async () => {
  // End-to-end cover for issue #54's per-slice degradation: unit tests in stages_verify
  // already pin the stage contract, but nothing else drove runWith with a mixed
  // origin='new'/origin='unknown' array through Validate → Filter → Challenge → report →
  // persist. Four findings, verifySliceSize 2 → two slices; fail slice 0 on both attempts.
  const findings = [
    makeFinding('F0'), makeFinding('F1'), makeFinding('F2'), makeFinding('F3'),
  ];
  const args = validArgs({
    limits: { validateBatch: 25, verifySliceSize: 2, challengeCap: 40, summarizeBucketSize: 20 },
  });
  let persisted = null;
  const ctx = makeCtx(args, {
    findings,
    verifySliceFailIndex: 0,
    onPersist: (payload) => { persisted = payload; },
  });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true, `pipeline must complete on a mixed-origin verify; gaps: ${out.gaps}`);
  assert.equal(out.phaseReached, 'report');
  assert.equal(out.stats.verified, false);
  assert.ok(out.gaps.some((g) => /UNVERIFIED/.test(g) && /slice 0/.test(g)), `expected per-slice UNVERIFIED gap, got: ${out.gaps}`);
  assert.ok(out.gaps.some((g) => /2 of 4/.test(g)), `gap must state the blast radius, got: ${out.gaps}`);
  // Retry fired for the failed slice; the healthy slice was never re-dispatched.
  assert.deepEqual(
    ctx.calls.filter((c) => /^verify-slice-0(-retry)?$/.test(c.label || '')).map((c) => c.label),
    ['verify-slice-0', 'verify-slice-0-retry'],
  );
  assert.deepEqual(
    ctx.calls.filter((c) => /^verify-slice-1(-retry)?$/.test(c.label || '')).map((c) => c.label),
    ['verify-slice-1'],
  );
  // Downstream stages still ran on the mixed-origin array.
  assert.ok(ctx.calls.some((c) => (c.label || '').startsWith('validate-batch-')), 'validate ran');
  assert.ok(ctx.calls.some((c) => (c.label || '').startsWith('challenge-')), 'challenge ran');
  assert.ok(ctx.calls.some((c) => c.label === 'report-writer'), 'report ran');
  assert.ok(persisted, 'artifact-writer received a payload');
  const byId = Object.fromEntries((persisted.findings || []).map((f) => [f.id, f]));
  for (const id of ['F0', 'F1']) {
    assert.equal(byId[id].origin, 'unknown', `${id} (failed slice) must stay origin=unknown through persist`);
  }
  for (const id of ['F2', 'F3']) {
    assert.ok(byId[id], `${id} (healthy slice) must reach persist`);
    assert.notEqual(byId[id].origin, 'unknown', `${id} must not be degraded by the sibling slice`);
  }
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

test('sandbox parity: the shared-context read plan builds with node-only globals removed', async () => {
  // The test above runs a waist with NO contextLines, so it never reaches contextReadPlan or
  // the plan-enumerating branch of sharedContextLine (issue #48). Without this case the whole
  // read-plan path would be sandbox-UNTESTED — exactly the shape of the structuredClone crash:
  // green under node:test, `X is not defined` on the first live dispatch. Drive it explicitly.
  const args = validArgs({ contextLines: 2028, contextChars: 94784 });
  const ctx = makeCtx(args);
  const out = await withSandboxGlobals(() => runWith(ctx, args));
  assert.equal(out.ok, true, `read-plan path must not depend on node-only globals; gaps: ${out.gaps}`);
  assert.equal(out.phaseReached, 'report');
  // Prove the plan actually reached the agents under those conditions, rather than the run
  // merely surviving because the branch was skipped again.
  const planned = ctx.calls.filter((c) => /Read\(offset=1924, limit=105\)/.test(c.prompt || ''));
  assert.ok(planned.length >= 9, `expected summarize + 7 discovery + validate to carry the plan, got ${planned.length}`);
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
  // Regression for the mid-pipeline description strip, across two fixes now history:
  //   - Original bug: verifyStage collected the executor's result.verified BY VALUE as THE
  //     findings for every later stage, but VERIFY_SCHEMA declared those items as
  //     { type:'object', properties:{} }. StructuredOutput leaves an empty-properties object
  //     unconstrained, so the executor dropped the (largest) `description` field when echoing
  //     the --output file "verbatim". Emptied descriptions then flowed through validate/filter,
  //     where the injection guard (short description + high confidence) false-fired and
  //     eliminated high-confidence findings (golden matches).
  //   - Intermediate fix: VERIFY_SCHEMA declared the FULL finding item on verified/eliminated
  //     (description present, confidence typed number), so StructuredOutput preserved the
  //     field instead of letting the executor drop it — but the finding itself still crossed
  //     the trust boundary a second time, so the item schema was the only thing standing
  //     between the executor and another silent drop.
  //   - Current state (issue #25 PR2): the finding never crosses the boundary a second time at
  //     all. The executor's answer is a per-id DELTA — {id, verified, origin, severity,
  //     confidence, elimination_reason} — never the finding, so there is no properties:{} (or
  //     any other item schema) for a description to be dropped BY. verifyStage rebuilds the
  //     verified findings by joining the delta onto the SAME finding object this stage already
  //     holds by value (joinVerifyDeltas): the description-strip class is structurally
  //     impossible now, not merely schema-prevented.
  // Two guarantees are asserted here:
  //   (1) the DATA FLOW — no stage strips a description that verify's join reattaches; and
  //   (2) the SCHEMA SHAPE — the verify dispatch schema declares NO finding-shaped array at
  //       all (no result.properties.verified / .eliminated to declare description on), and its
  //       one array (result.properties.deltas) declares exactly the six delta keys — so a
  //       finding-shaped item schema can't regress back into existence unnoticed.
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

  // (2) Schema shape: the verify dispatch must carry NO finding-shaped array at all — the
  // verified/eliminated arrays that used to need a `description` property don't exist to
  // declare one on — and its only result array (deltas) must declare exactly the six delta
  // keys, with `id`/`verified` required. A revert that reintroduces a findings-shaped verify
  // result array (with or without `description`) fails here.
  const verifyCall = ctx.calls.find((c) => (c.label || '').startsWith('verify-slice-'));
  assert.ok(verifyCall && verifyCall.schema, 'a verify-slice was dispatched with a schema');
  const resultProps = verifyCall.schema.properties.result.properties;
  assert.ok(!('verified' in resultProps), 'verify result no longer declares a verified findings array');
  assert.ok(!('eliminated' in resultProps), 'verify result no longer declares an eliminated findings array');
  assert.ok(resultProps.deltas, 'verify result declares a deltas array');
  const deltaItemProps = resultProps.deltas.items.properties;
  assert.deepEqual(
    Object.keys(deltaItemProps).sort(),
    ['confidence', 'elimination_reason', 'id', 'origin', 'severity', 'verified'],
    'the delta item declares exactly the six delta keys — no description, no room to drop one',
  );
  assert.deepEqual(
    (resultProps.deltas.items.required || []).slice().sort(),
    ['id', 'verified'],
    'id and verified are the only required delta keys',
  );
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

test('the fresh-run empty_report wording is unchanged by the union guard (output-preserving)', async () => {
  // The guard now fires on the UNION of postFilterCount and the delivered challenge count.
  // On a FRESH run challenge only removes findings, so postFilterCount >= deliveredCount and
  // the union must reduce to exactly the old condition AND the old message, byte for byte.
  // Whitespace-only (not ''): a falsy report is caught by reportStage's own minimal-report
  // fallback, so the guard under test is exercised with a truthy-but-blank report.
  const args = validArgs();
  const ctx = makeCtx(args, { reportText: '   ' });
  const out = await runWith(ctx, args);
  const gap = out.gaps.find((g) => /empty_report/.test(g));
  assert.ok(gap, `expected an empty_report gap, got: ${out.gaps}`);
  assert.equal(
    gap,
    'empty_report: report stage produced no report while 2 finding(s) survived the filter — refusing to ship a silent empty report',
  );
});

test('a non-empty report on a run with findings records NO empty_report gap (guard stays narrow)', async () => {
  const args = validArgs();
  const out = await runWith(makeCtx(args), args);
  assert.ok(!out.gaps.some((g) => /empty_report/.test(g)), `got: ${out.gaps}`);
  assert.equal(typeof out.artifactPaths.report, 'string');
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

// --- All-degraded discovery (issue #178) ------------------------------------
//
// Live Bedrock incident 2026-08-11: an invalid model ID null-isolated EVERY discovery
// agent, and the pre-fix pipeline sailed on through merge/verify/validate/filter/challenge/
// report/persist to an ok:true envelope with an empty "clean" report. These tests pin the
// fix (allActiveDimensionsDegraded's guard in runWith): total degradation fails loud,
// partial degradation stays exactly as before (degraded-but-disclosed, ok:true).

test('issue #178 regression: every discovery agent null -> ok:false, no downstream dispatch', async () => {
  // policy is driven through args (not left default) and resolvedPolicy asserted by exact
  // shape — a stray/dropped field on the envelope would previously pass under key-presence
  // checks alone (finding 4, #178 follow-up). 'bedrock' is a valid POLICY_PROVIDERS entry.
  const args = validArgs({ policy: { subagentModel: 'claude-x', provider: 'bedrock' } });
  const ctx = makeCtx(args, { nullAgentLabels: [...AGENTS] });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, false);
  assert.equal(out.error, 'all-degraded: every active discovery dimension degraded — no review was performed');
  assert.equal(out.failingPhase, 'discover');
  assert.equal(out.phaseReached, 'discover');
  assert.deepEqual(out.artifactPaths, {});
  assert.deepEqual([...out.stats.degraded].sort(), DIMENSIONS.map((d) => d.dimension).sort());
  assert.deepEqual(out.resolvedPolicy, { subagentModel: 'claude-x', provider: 'bedrock' });

  // The 7 per-agent gaps (one per nulled discovery dispatch) plus EXACTLY one all-degraded gap.
  const perAgentGaps = out.gaps.filter((g) => /agent returned null/.test(g));
  assert.equal(perAgentGaps.length, AGENTS.length, `expected ${AGENTS.length} per-agent gaps, got: ${out.gaps}`);
  const allDegradedGaps = out.gaps.filter((g) => g.startsWith('all-degraded:'));
  assert.equal(allDegradedGaps.length, 1, `expected exactly one all-degraded gap, got: ${out.gaps}`);

  // The resume checkpoints do NOT carry a discover phase entry — a retry must re-dispatch
  // discovery, never replay the degraded-to-nothing output.
  assert.ok(out.checkpoints && out.checkpoints.phases, 'in-memory phases map present');
  assert.ok('summarize' in out.checkpoints.phases, 'summarize (which completed) is still resumable');
  assert.ok(!('discover' in out.checkpoints.phases), 'discover is dropped from the resume checkpoints');

  // Nothing downstream of discover was ever dispatched.
  assert.ok(!ctx.calls.some((c) => c.label.startsWith('validate-batch-')), 'validate was not dispatched');
  assert.ok(!ctx.calls.some((c) => c.label.startsWith('challenge-')), 'challenge was not dispatched');
  assert.ok(!ctx.calls.some((c) => c.label === 'report-writer' || c.label.startsWith('report-writer-')), 'report-writer was not dispatched');
  assert.ok(!ctx.calls.some((c) => c.label === 'artifact-writer'), 'the persist writer was not dispatched');
});

test('issue #178 partial-degradation pin: one discovery agent null stays degraded-but-disclosed (ok:true)', async () => {
  const args = validArgs();
  const ctx = makeCtx(args, { nullAgentLabels: ['code-gauntlet:security-reviewer'] });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.equal(out.phaseReached, 'report');
  assert.deepEqual(out.stats.degraded, ['security'], 'only the nulled agent\'s own dimension(s) degrade');
  assert.ok(out.gaps.some((g) => /security-reviewer/.test(g) && /agent returned null/.test(g)));
  assert.ok(!out.gaps.some((g) => g.startsWith('all-degraded:')), 'partial degradation must not trip the all-degraded guard');
  // Report/persist behavior unchanged: a real report is produced and persisted.
  assert.equal(typeof out.artifactPaths.report, 'string');
  assert.ok(ctx.calls.some((c) => c.label === 'report-writer'));
  assert.ok(ctx.calls.some((c) => c.label === 'artifact-writer'));
});

test('issue #178 checkpoint-replay defense: an all-degraded discover checkpoint still fails loud on resume', async () => {
  // Simulates a stale checkpoint from a prior all-degraded run being fed back through the
  // args waist — the guard must fire on the REPLAYED discoverOut, not only a fresh dispatch.
  const allDims = DIMENSIONS.map((d) => d.dimension);
  const discoverCheckpoint = {
    findings: [],
    degraded: allDims,
    dispatched: [...AGENTS],
    gaps: AGENTS.map((a) => `${a}: agent returned null (dispatch failed) — dimensions ? not covered`),
  };
  const args = validArgs({ checkpoints: { discover: discoverCheckpoint } });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, false);
  assert.equal(out.error, 'all-degraded: every active discovery dimension degraded — no review was performed');
  assert.equal(out.failingPhase, 'discover');
  // Discovery was REPLAYED from the checkpoint, not re-dispatched.
  assert.ok(!ctx.calls.some((c) => c.label.startsWith('code-gauntlet:')), 'no discovery agent was dispatched (replayed instead)');
});

// Inverse boundary (finding 5): every discovery agent is HEALTHY but genuinely finds
// nothing (a real clean review), fresh dispatch, no checkpoints in play. This must sail
// through exactly as before the guard existed — ok:true, a real report, and no
// 'all-degraded:' gap. `opts.findings: []` drives every agent (including bug-detector,
// the only one the mock seeds by default) to return zero findings.
test('issue #178 inverse boundary: all agents healthy with zero findings each stays a clean ok:true review', async () => {
  const args = validArgs();
  const ctx = makeCtx(args, { findings: [] });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.equal(out.phaseReached, 'report');
  assert.ok(!out.gaps.some((g) => g.startsWith('all-degraded:')), `a genuine zero-finding clean review must not trip the guard; gaps: ${out.gaps}`);
  assert.equal(typeof out.artifactPaths.report, 'string');
});

// Finding 1 (#178 follow-up): resume delivery must survive a totally re-degraded
// re-discovery when a replayable challenge checkpoint (PERSISTED_RESUME_PHASES) is in
// hand — aborting here would drop a prior attempt's already-delivered review.
test('issue #178 finding 1: resume with a replayable challenge checkpoint survives a fresh all-degraded re-discovery', async () => {
  // Run 1: a real end-to-end pass produces (and we capture) the persisted slim checkpoint,
  // which carries `challenge` in full — this is the genuine fixture shape, not a hand-rolled
  // guess at challengeStage's output.
  const args1 = validArgs();
  let persistedCheckpoints = null;
  const ctx1 = makeCtx(args1, { onPersist: (payload) => { persistedCheckpoints = payload.checkpoints; } });
  const out1 = await runWith(ctx1, args1);
  assert.equal(out1.ok, true);
  assert.ok(persistedCheckpoints.phases.challenge.findings.length > 0, 'run 1 delivered at least one finding');

  // Run 2: resumes from that slim checkpoint (challenge replayable), but discovery is
  // dispatched fresh and EVERY agent fails — simulating the exact scenario finding 1 names:
  // a totally-degraded fresh re-discovery on a resume that already has a delivered review.
  const args2 = validArgs({ checkpoints: persistedCheckpoints });
  const ctx2 = makeCtx(args2, { nullAgentLabels: [...AGENTS] });
  const out2 = await runWith(ctx2, args2);

  assert.equal(out2.ok, true, `must not abort when a replayable challenge checkpoint is in hand; got: ${JSON.stringify(out2)}`);
  assert.equal(out2.phaseReached, 'report');
  assert.notDeepEqual(out2.artifactPaths, {}, 'artifactPaths must be populated, not the abort-path {}');
  // Per-agent degradation is still disclosed even though the run did not abort.
  assert.ok(out2.gaps.some((g) => /agent returned null/.test(g)), 'per-agent null gaps still present');
  assert.ok(!out2.gaps.some((g) => g.startsWith('all-degraded:')), 'the guard must not fire on this resume path');
  // The delivered set is the REPLAYED challenge output, not a fresh (empty) one.
  assert.equal(out2.stats.highConfidence, persistedCheckpoints.phases.challenge.findings.length, 'the prior delivered set survives the resume');
});

// Finding 2 (#178 follow-up): a version-skew replayed discover checkpoint (an agentType
// that no longer resolves via agentSpecs()) whose findings are NON-empty must still
// deliver — the helper's fail-closed arm returns true, but the guard's findings-empty
// conjunct holds it back because there is real work product to deliver.
test('issue #178 finding 2: version-skew partial replay WITH findings keeps delivering (guard held back by findings-empty conjunct)', async () => {
  const discoverCheckpoint = {
    findings: [makeFinding('F1')],
    gaps: [],
    degraded: ['bug'],
    dispatched: ['code-gauntlet:renamed-away'], // unresolvable: not a real agentType in the current registry
  };
  const args = validArgs({ checkpoints: { discover: discoverCheckpoint } });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true, `must not abort a version-skew replay that carries real findings; got: ${JSON.stringify(out)}`);
  assert.equal(out.phaseReached, 'report');
  assert.ok(!out.gaps.some((g) => g.startsWith('all-degraded:')), 'the guard must not fire while findings are non-empty');
  assert.ok(!ctx.calls.some((c) => c.label.startsWith('code-gauntlet:')), 'discovery was replayed, not re-dispatched');
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

// --- Report JSON-wrapper unwrap (measured: ~15 of 25 runs since 2026-07-22) --
//
// The report-writer intermittently returns its markdown ALREADY WRAPPED as a JSON
// document: the string in its `report` field is literally `{"report": "# Code
// Gauntlet..."}`. The artifact-writer then persists that wrapper verbatim (correctly —
// its contract is to write the text as given), so every non-Phase-8 consumer of
// report.md gets JSON where markdown belongs. reportStage unwraps at the point the
// string is first received, so the persisted artifact is markdown.

test('reportStage unwraps a report the writer returned JSON-wrapped', async () => {
  const md = '# Code Gauntlet Report\n\nOne finding.\n';
  const ctx = {
    agent: async () => ({ report: JSON.stringify({ report: md }) }),
    parallel: async () => [],
  };
  const out = await reportStage(ctx, { findings: [makeFinding('F1')], stats: {} });
  assert.equal(out.report, md, 'the persisted report must be the markdown, not the wrapper');
  assert.deepEqual(out.gaps, []);
});

test('reportStage unwraps a pretty-printed / whitespace-padded wrapper too', async () => {
  const md = '# Code Gauntlet Report\n\n- [HIGH] something (a.js:1)\n';
  const ctx = {
    agent: async () => ({ report: `\n  ${JSON.stringify({ report: md }, null, 2)}\n` }),
    parallel: async () => [],
  };
  const out = await reportStage(ctx, { findings: [makeFinding('F1')], stats: {} });
  assert.equal(out.report, md);
});

test('reportStage leaves a legitimate markdown report starting with a brace ALONE', async () => {
  // Conservative by construction: unwrapping requires a successful JSON.parse AND an
  // object AND a string `report` member. Markdown that merely opens with `{` is not JSON.
  const md = '{ this is prose } \n\n# Code Gauntlet Report\n';
  const ctx = {
    agent: async () => ({ report: md }),
    parallel: async () => [],
  };
  const out = await reportStage(ctx, { findings: [makeFinding('F1')], stats: {} });
  assert.equal(out.report, md);
});

test('reportStage does not unwrap JSON that is not a {report:string} wrapper', async () => {
  const cases = [
    JSON.stringify({ report: { markdown: '# nope' } }),   // report is not a string
    JSON.stringify({ summary: '# nope' }),                 // no report member
    JSON.stringify(['# nope']),                            // array, not object
    JSON.stringify('# nope'),                              // bare JSON string
    JSON.stringify({ report: '# md', findings: [1, 2] }),  // extra MEANINGFUL content
  ];
  for (const raw of cases) {
    const ctx = { agent: async () => ({ report: raw }), parallel: async () => [] };
    const out = await reportStage(ctx, { findings: [makeFinding('F1')], stats: {} });
    assert.equal(out.report, raw, `must be left verbatim: ${raw}`);
  }
});

test('reportStage unwraps on the SEGMENTED path as well', async () => {
  const big = [];
  for (let i = 0; i < 80; i += 1) big.push(makeFinding(`F${i}`, { description: 'x'.repeat(2000) }));
  const ctx = {
    agent: async (prompt, opts) => ({ report: JSON.stringify({ report: `body for ${opts.label}` }) }),
    parallel: async (thunks) => Promise.all(thunks.map((t) => t())),
  };
  const out = await reportStage(ctx, { findings: big, unverified: [], stats: {} });
  assert.ok(!/\{"report"/.test(out.report), `segments must be unwrapped: ${out.report.slice(0, 200)}`);
  assert.match(out.report, /body for report-writer-0/);
});

test('a wrapper whose report member is EMPTY degrades to the minimal report', async () => {
  const ctx = {
    agent: async () => ({ report: JSON.stringify({ report: '' }) }),
    parallel: async () => [],
  };
  const out = await reportStage(ctx, { findings: [makeFinding('F1')], stats: {} });
  assert.match(out.report, /minimal report/i);
  assert.ok(out.gaps.some((g) => /report/i.test(g)), out.gaps);
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
  const out = await writeArtifacts(ctx, { findings: [makeFinding('F1')], report: '# r', checkpoints: {}, outputDir: '/repo/.code-gauntlet', headShaShort: 'abc1234' });
  assert.ok(out.gaps.some((g) => /partial|artifact/i.test(g)));
  assert.equal(out.artifactPaths.findings, null);
});

// Item 5: the artifact-writer echo must ACCOUNT for all four planned paths — a schema-valid
// but degenerate echo (empty {} or partial) is no write proof and degrades to partial.
test('writeArtifacts: an empty {} echo (no write proof) degrades to a partial-artifacts gap', async () => {
  const wIn = { findings: [makeFinding('F1')], postReview: [], report: '# r', checkpoints: {}, outputDir: '/repo/.code-gauntlet', headShaShort: 'abc1234' };
  const ctx = { agent: async () => ({}), parallel: async () => [] }; // schema-valid, accounts for nothing
  const out = await writeArtifacts(ctx, wIn);
  assert.equal(out.partial, true);
  assert.equal(out.artifactPaths.findings, null);
  assert.ok(out.gaps.some((g) => /write proof|partial|artifact/i.test(g)));
});

test('writeArtifacts: a partial echo (missing one planned path) also degrades to partial-artifacts', async () => {
  const wIn = { findings: [makeFinding('F1')], postReview: [], report: '# r', checkpoints: {}, outputDir: '/repo/.code-gauntlet', headShaShort: 'abc1234' };
  const paths = plannedArtifactPaths(wIn.outputDir, wIn.headShaShort);
  const partialEcho = { ...paths }; delete partialEcho.checkpoints; // three of four
  const ctx = { agent: async () => ({ artifactPaths: partialEcho }), parallel: async () => [] };
  const out = await writeArtifacts(ctx, wIn);
  assert.equal(out.partial, true);
  assert.equal(out.artifactPaths.report, null);
});

test('writeArtifacts: a faithful echo of all four planned paths persists (partial:false)', async () => {
  const wIn = { findings: [makeFinding('F1')], postReview: [], report: '# r', checkpoints: {}, outputDir: '/repo/.code-gauntlet', headShaShort: 'abc1234' };
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

// --- Args-waist null tolerance is DISCLOSED (issue #38, L5-2/L3-1) -----------

test('a stamped reviewConfig:null runs, but the config substitution is DISCLOSED as a gap', async () => {
  // Tolerating the null is what removes the 21.3s reject round trip. Doing it SILENTLY
  // would review under the Filter stage's built-in 55/70 instead of the operator's
  // REVIEW.md thresholds — a silent delivered-findings change. Degraded-but-disclosed.
  const args = validArgs({ reviewConfig: null });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true, `a stamped null must not reject the run; gaps: ${out.gaps}`);
  const gap = out.gaps.find((g) => /^null_arg: /.test(g));
  assert.ok(gap, `expected a null_arg gap naming reviewConfig, got: ${out.gaps}`);
  assert.match(gap, /reviewConfig/);
  assert.match(gap, /ABSENT/);
  assert.match(gap, /Omit/);
  // The caller's args object is not mutated by the strip.
  assert.equal(args.reviewConfig, null);
});

test('a stamped exclusionPatterns/delivery null each gets its own disclosure gap', async () => {
  const args = validArgs({ exclusionPatterns: null, delivery: null });
  const out = await runWith(makeCtx(args), args);
  assert.equal(out.ok, true);
  const named = out.gaps.filter((g) => /^null_arg: /.test(g));
  assert.equal(named.length, 2, `one gap per dropped key, got: ${named}`);
  for (const k of ['exclusionPatterns', 'delivery']) {
    assert.ok(named.some((g) => g.includes(k)), `a gap names ${k}`);
  }
});

test('F4-3: checkpoints:null discloses NOTHING — validateArgs never rejected it', async () => {
  // The disclosure exists because tolerating a stamped null REMOVES a fail-loud guard. There
  // has never been a checkpoints shape check, so `checkpoints: null` was always equivalent to
  // absence: nothing was tolerated, nothing was lost, and the run was previously valid and
  // silent. Announcing a degradation there is noise in the one channel this pipeline uses to
  // stay honest about the degradations that are real.
  const args = validArgs({ checkpoints: null });
  const out = await runWith(makeCtx(args), args);
  assert.equal(out.ok, true);
  assert.deepEqual(out.gaps.filter((g) => /^null_arg: /.test(g)), []);

  // ...while a null that WOULD have been rejected still discloses, from the same run.
  const args2 = validArgs({ checkpoints: null, reviewConfig: null });
  const out2 = await runWith(makeCtx(args2), args2);
  const named = out2.gaps.filter((g) => /^null_arg: /.test(g));
  assert.equal(named.length, 1, `only the load-bearing drop is disclosed, got: ${named}`);
  assert.match(named[0], /reviewConfig/);
});

test('F4-3: every OTHER nullable key still discloses (the filter is not a blanket mute)', async () => {
  // delivery.tier / delivery.prIdentity are nested drops; each is rejected by validateArgs as
  // a stamped null, so each keeps its disclosure.
  const args = validArgs({ delivery: { tier: null, prIdentity: null } });
  const out = await runWith(makeCtx(args), args);
  assert.equal(out.ok, true);
  const named = out.gaps.filter((g) => /^null_arg: /.test(g));
  assert.equal(named.length, 2, `got: ${named}`);
  assert.ok(named.some((g) => g.includes('delivery.tier')));
  assert.ok(named.some((g) => g.includes('delivery.prIdentity')));
});

test('a clean waist records NO null_arg gap (disclosure is not noise)', async () => {
  const args = validArgs();
  const out = await runWith(makeCtx(args), args);
  assert.equal(out.ok, true);
  assert.deepEqual(out.gaps.filter((g) => /null_arg/.test(g)), []);
});

test('L3-1: a stamped persist:null runs instead of hard-rejecting the whole run, and is disclosed', async () => {
  // persist was added to the waist but left OFF the null-tolerance allowlist the same diff
  // introduced for its siblings, so `persist: null` rejected the run outright.
  const args = validArgs({ persist: null });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true, `persist:null must be treated as absent, not rejected; error: ${out.error}`);
  assert.equal(out.phaseReached, 'report');
  assert.ok(out.gaps.some((g) => /^null_arg: /.test(g) && g.includes('persist')), `expected a persist disclosure, got: ${out.gaps}`);
});

test('the null_arg disclosure also rides the invalid-args early return', async () => {
  // A run rejected for an unrelated reason must still tell the operator the null was there.
  const args = validArgs({ reviewConfig: null, argsVersion: 2 });
  const out = await runWith(makeCtx(args), args);
  assert.equal(out.ok, false);
  assert.ok(out.gaps.some((g) => /^null_arg: /.test(g) && g.includes('reviewConfig')));
  assert.ok(out.gaps.some((g) => /argsVersion/.test(g)), 'the validation errors are still carried');
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

test('checkpoint round-trip: persisted checkpoint is SLIM — only the resume-consumed phase carries full output', async () => {
  // Run 1 produces the checkpoint artifact the writer persists; capture it via onPersist.
  const args1 = validArgs();
  let persistedCheckpoints = null;
  const ctx1 = makeCtx(args1, { onPersist: (payload) => { persistedCheckpoints = payload.checkpoints; } });
  const out1 = await runWith(ctx1, args1);
  assert.equal(out1.ok, true);
  assert.ok(persistedCheckpoints, 'writer received a checkpoints payload');

  // The SLIM persisted checkpoint keeps FULL output only for the ONE resume-consumed phase:
  // challenge carries the delivered findings. `filter` is a pure agent-free JS function, so it
  // simply re-runs on a resume at zero dispatch cost (issue #38, P1) rather than being persisted.
  assert.deepEqual(
    Object.keys(persistedCheckpoints.phases).sort(), ['challenge'],
    'only challenge keeps full output in the persisted checkpoint',
  );
  // Every completed phase is still accounted for by NAME + a bare count (observability without
  // the by-value findings bulk the OLD full-map checkpoint duplicated).
  for (const phase of ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report']) {
    assert.ok(persistedCheckpoints.completed.includes(phase), `completed lists phase '${phase}'`);
    assert.equal(typeof persistedCheckpoints.counts[phase], 'number', `counts has a number for '${phase}'`);
  }
  // The upstream phases' full findings arrays are GONE from the persisted checkpoint.
  for (const phase of ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'report']) {
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

test('slimPersistedCheckpoints keeps only challenge full, counts every phase, and the writer omits unverified', async () => {
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
  // P1 (issue #38): filter is NOT persisted — it is a pure JS function that re-runs free.
  assert.ok(!('filter' in persisted.checkpoints.phases), 'filter output is not persisted');
  assert.equal(typeof persisted.checkpoints.counts.filter, 'number', 'the filter COUNT is still recorded');
  assert.ok(persisted.checkpoints.counts, 'per-phase counts present');
});

// --- Report segmentation (oversized findings payload) -----------------------

test('reportStage segments an oversized findings payload into >1 dispatch and joins sections', async () => {
  // ~80 findings x ~2000-char description >> PROMPT_SEGMENT_CHAR_BUDGET (100k).
  const big = [];
  for (let i = 0; i < 80; i += 1) big.push(makeFinding(`F${i}`, { description: 'x'.repeat(2000) }));

  const calls = [];
  const ctx = {
    agent: async (prompt, opts) => { calls.push(opts); return { report: `segment body for ${opts.label}` }; },
    // Segment dispatches now fan out through parallel() (issue #38, S2); it preserves input
    // order and nulls a failed member in place.
    parallel: async (thunks) => Promise.all(thunks.map(async (t) => {
      try { return await t(); } catch { return null; }
    })),
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
  // A phases map whose serialized size blows past the 1,000,000-char RETURN budget.
  const huge = { findings: Array.from({ length: 2000 }, (_, i) => makeFinding(`F${i}`, { description: 'y'.repeat(300) })) };
  const cp = buildResumeCheckpoints({ discover: huge, verify: huge });
  assert.ok(!('phases' in cp), 'oversized phases map is dropped from the compact return');
  assert.equal(cp.truncated, true);
  assert.deepEqual(cp.completed, ['discover', 'verify']);
});

// The budget split (PROMPT_SEGMENT_CHAR_BUDGET vs RETURN_CHAR_BUDGET). This map is well
// over the 100,000-char PROMPT budget the resume state used to be graded against by
// analogy, and far under the return channel's own measured limit — so it must now survive.
// Three recorded runs threw exactly this state away.
test('buildResumeCheckpoints carries a phases map that only the PROMPT budget would have rejected', () => {
  const big = { findings: Array.from({ length: 400 }, (_, i) => makeFinding(`F${i}`, { description: 'y'.repeat(300) })) };
  const phaseOutputs = { discover: big, verify: big };
  const size = JSON.stringify({ phases: phaseOutputs, completed: ['discover', 'verify'] }).length;
  assert.ok(size > 100000, `fixture must exceed the prompt budget to be meaningful (got ${size})`);
  assert.ok(size < 1000000, `fixture must stay under the return budget (got ${size})`);
  const cp = buildResumeCheckpoints(phaseOutputs);
  assert.deepEqual(cp.phases, phaseOutputs, 'resume state survives the return channel');
  assert.ok(!cp.truncated);
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

// policy.provider must thread waist -> modelFor -> every dispatch: the registry test alone
// cannot catch modelFor dropping the field (it would silently re-pin first-party full IDs
// on Bedrock — the exact live failure this exists to prevent).
test('policy.provider threads through modelFor: bedrock dispatches the bare alias', async () => {
  const args = validArgs({ policy: { tier: 'optimized', subagentModel: null, provider: 'bedrock' } });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  const models = [...new Set(ctx.calls.map((c) => c.model))].sort();
  // Discovery + stage agents on the sonnet alias, security-reviewer on the opus alias —
  // and no first-party full ID anywhere in the run.
  assert.deepEqual(models, ['opus', 'sonnet']);
  assert.equal(out.resolvedPolicy.provider, 'bedrock');
});
test('absent policy.provider keeps first-party full-ID pins and reports firstParty', async () => {
  const args = validArgs();
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.ok(ctx.calls.every((c) => /^claude-/.test(c.model)), 'every dispatch pins a full first-party ID');
  assert.equal(out.resolvedPolicy.provider, 'firstParty');
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

  // Segmented report (parallel per-chunk dispatches).
  const big = Array.from({ length: 80 }, (_, i) => makeFinding(`F${i}`, { description: 'x'.repeat(2000) }));
  const rctx = makeCtx(args);
  await reportStage(rctx, { findings: big, unverified: [], stats: {}, policy: {} });
  assert.deepEqual(rctx.violations, [], `report violations: ${rctx.violations.join('; ')}`);
  assert.ok(rctx.calls.filter((c) => c.label.startsWith('report-writer-')).length > 1);
});

// Issue #89: discover()'s own dispatched/degraded lists flow through reportInput
// (stages.js ~3366) into the dimensionsSummaryTable dispatched to the report-writer.
// makeCtx's default fixture dispatches every one of the 7 AGENTS (agentFlags={}), only
// bug-detector returns findings (the 2-element makeFindings() set — both survive to
// challengeOut.findings, confirmed by the happy-path test's `highConfidence === 2`), and
// no discovery agent throws — so the expected end state is dispatched=AGENTS, degraded=[].
test('happy path: discoverOut.dispatched/degraded reach the final Review Dimensions Summary table via reportInput', async () => {
  const args = validArgs();
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);

  const reportCall = ctx.calls.find((c) => c.label === 'report-writer');
  assert.ok(reportCall, 'the tiny fixture must dispatch a single unsegmented report-writer call');
  const m = /Results JSON:\n([\s\S]*)\nReturn \{ report \}/.exec(reportCall.prompt);
  assert.ok(m, 'the report-writer prompt carries a Results JSON body');
  const body = JSON.parse(m[1]);
  assert.ok(body.dimensionsTable, 'dimensionsTable is present on the segment-0 dispatch');

  const rows = body.dimensionsTable.split('\n').slice(2)
    .map((line) => line.split('|').slice(1, -1).map((c) => c.trim()));
  assert.equal(rows.length, AGENTS.length);
  const bugRow = rows.find((r) => r[1] === 'bug-detector');
  assert.equal(bugRow[2], '2', 'bug-detector reports the 2 seeded findings that survived to delivery');
  const otherRows = rows.filter((r) => r[1] !== 'bug-detector');
  assert.ok(
    otherRows.every((r) => r[2] === '0' && r[3] === 'Clean — no findings returned'),
    `every other dispatched agent must read Clean/0: ${JSON.stringify(otherRows)}`,
  );
});

// --- Issue #24 req 1/3/4/5 (PR3): deterministic agentFlags derivation ------------------

// deriveAgentFlags itself (stages.js): the ONE call site that turns riskTable/changedLines/
// scopeAnswer into the scope-gating map agentActive consumes.
test('deriveAgentFlags: light-eligible + scopeAnswer "light" -> { deep: false }', () => {
  assert.deepEqual(
    deriveAgentFlags([{ path: 'a.js', risk: 'low' }], 10, 'light'),
    { deep: false },
  );
});
test('deriveAgentFlags: light-eligible + scopeAnswer "full" -> {}', () => {
  assert.deepEqual(deriveAgentFlags([{ path: 'a.js', risk: 'low' }], 10, 'full'), {});
});
test('deriveAgentFlags: any medium/high entry -> {} regardless of scopeAnswer', () => {
  assert.deepEqual(
    deriveAgentFlags([{ path: 'a.js', risk: 'low' }, { path: 'b.js', risk: 'medium' }], 10, 'light'),
    {},
  );
});
test('deriveAgentFlags: changedLines >= 50 -> {} even with an all-low table and "light" answer', () => {
  assert.deepEqual(deriveAgentFlags([{ path: 'a.js', risk: 'low' }], 50, 'light'), {});
});
test('deriveAgentFlags: not eligible + no scopeAnswer -> {} (the common full-scope case)', () => {
  assert.deepEqual(deriveAgentFlags([{ path: 'a.js', risk: 'medium' }], 10, undefined), {});
});

// runWith-level: the derived flags actually gate discover()'s dispatch, and the resolved
// decision is echoed in stats.scope.
test('runWith: a light-eligible waist with scopeAnswer "light" dispatches only bug-detector + security-reviewer', async () => {
  const args = validArgs({
    riskTable: [{ path: 'a.js', risk: 'low' }],
    changedLines: 10,
    scopeAnswer: 'light',
  });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.deepEqual(out.stats.scope, { lightEligible: true, scopeAnswer: 'light', deep: false });
  // Exactly 2 discovery agents dispatched (the two UNGATEABLE core dimensions).
  const dispatchedAgentLabels = new Set(ctx.calls.map((c) => c.label).filter((l) => AGENTS.includes(l)));
  assert.deepEqual([...dispatchedAgentLabels].sort(), ['code-gauntlet:bug-detector', 'code-gauntlet:security-reviewer']);
});
test('runWith: a full-scope waist (not eligible) dispatches all 7 discovery agents and echoes stats.scope', async () => {
  const args = validArgs(); // default fixture riskTable is 'medium' -> not eligible
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.deepEqual(out.stats.scope, { lightEligible: false, scopeAnswer: null, deep: true });
  const dispatchedAgentLabels = new Set(ctx.calls.map((c) => c.label).filter((l) => AGENTS.includes(l)));
  assert.deepEqual([...dispatchedAgentLabels].sort(), [...AGENTS].sort());
});
test('runWith: an eligible waist answered "full" still runs all 7 agents and echoes lightEligible true', async () => {
  const args = validArgs({
    riskTable: [{ path: 'a.js', risk: 'low' }],
    changedLines: 10,
    scopeAnswer: 'full',
  });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.deepEqual(out.stats.scope, { lightEligible: true, scopeAnswer: 'full', deep: true });
});
test('runWith: a stale caller-stamped agentFlags is refused loud, never silently honoured or ignored', async () => {
  const args = { ...validArgs(), agentFlags: { deep: false } };
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, false);
  assert.match(out.error, /agentFlags is no longer accepted/);
});
