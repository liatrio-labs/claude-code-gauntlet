// stages_discover.test.js — orchestration-contract tests for stages 1-3
// (Summarize, Discover, Merge) + the agent-count coarsening formula.
// ctx is injected {agent, parallel}; the mock ctx is the testability seam.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { summarize, discover, mergeStage, worstCaseAgentCount, coarsenLimits, agentActive, agentSpecs } from '../src/stages.js';
import { AGENTS, DIMENSIONS } from '../src/registry.js';
import { assertPrompt, assertValidSchema } from './helpers/pipelineMock.js';

// Platform contract: agent(promptString, opts); parallel(thunks). The mock asserts it.
// Discovery's per-agent result is keyed on opts.label (which IS the agentType); a
// `nulls` label makes its thunk throw so parallel() null-isolates it (Phase 0).
// `prompts` records the exact prompt string dispatched per label, for elicitation tests.
function fakeCtx({ nulls = [], byAgent = {} } = {}) {
  const calls = [];
  const prompts = {};
  const agent = async (prompt, opts = {}) => {
    assertPrompt(prompt);
    assertValidSchema(opts.schema);
    calls.push(opts.label);
    prompts[opts.label] = prompt;
    if (nulls.includes(opts.label)) throw new Error(`injected failure for ${opts.label}`);
    return byAgent[opts.label] ?? { findings: [], complete: true, total_seen: 0 };
  };
  const parallel = async (thunks) => Promise.all(thunks.map(async (thunk) => {
    if (typeof thunk !== 'function') throw new Error('parallel() members must be zero-arg functions');
    try { return await thunk(); } catch { return null; }
  }));
  return { calls, prompts, agent, parallel };
}

test('discover dispatches once per AGENT (7)', async () => {
  const ctx = fakeCtx();
  await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  assert.equal(new Set(ctx.calls).size, AGENTS.length);
});

// --- Light scope (item 7): agentFlags gating -------------------------------
// The two CORE dimensions stay on in light scope; the seven extended dimensions
// (conditionalFlag 'deep') drop out. bug + security -> bug-detector + security-reviewer.
const CORE_AGENTS = ['code-gauntlet:bug-detector', 'code-gauntlet:security-reviewer'];

// BYTE-IDENTICAL guarantee (fingerprint over EVERY spec): with agentFlags absent, empty,
// or an unrelated key, agentActive returns exactly what it returned before flags existed —
// TRUE for every agent. Any spec silently gated off here would be silent under-delivery on
// the full path. Both the absent (undefined) and empty ({}) forms must match.
test('agentActive: full-scope path is byte-identical — every spec active when flags absent/empty', () => {
  for (const spec of agentSpecs()) {
    assert.equal(agentActive(spec, undefined), true, `${spec.agentType} inactive with agentFlags undefined`);
    assert.equal(agentActive(spec, {}), true, `${spec.agentType} inactive with agentFlags {}`);
    // An unrelated/unknown flag key must not disable anything either (opt-out: missing key = on).
    assert.equal(agentActive(spec, { someOtherFlag: false }), true, `${spec.agentType} inactive with an unrelated flag`);
  }
});

// Light scope stamps { deep: false }: exactly the two core agents survive.
test('agentActive: light scope { deep:false } keeps ONLY bug + security agents active', () => {
  for (const spec of agentSpecs()) {
    const expected = CORE_AGENTS.includes(spec.agentType);
    assert.equal(agentActive(spec, { deep: false }), expected, `${spec.agentType} light-scope active=${!expected}`);
  }
});

// Core dimensions are UNGATEABLE (conditionalFlag null): even a hostile flag map that tries
// to name them cannot disable bug/security, and a stray non-`false` deep value keeps all on.
test('agentActive: core (bug/security) cannot be disabled; only literal false gates a deep dim', () => {
  const byType = new Map(agentSpecs().map((s) => [s.agentType, s]));
  // Hostile map: bug/security have null conditionalFlag, so no agentFlags key reaches them.
  assert.equal(agentActive(byType.get('code-gauntlet:bug-detector'), { deep: false, bug: false, security: false }), true);
  assert.equal(agentActive(byType.get('code-gauntlet:security-reviewer'), { deep: false }), true);
  // A non-`false` deep value (truthy, null, 0, or the string 'false') leaves deep dims ON —
  // only the literal boolean false gates. This is why the args waist boolean-checks the map.
  const crossFile = byType.get('code-gauntlet:cross-file-impact');
  assert.equal(agentActive(crossFile, { deep: true }), true);
  assert.equal(agentActive(crossFile, { deep: 0 }), true);
  assert.equal(agentActive(crossFile, { deep: null }), true);
  assert.equal(agentActive(crossFile, { deep: 'false' }), true);
  assert.equal(agentActive(crossFile, { deep: false }), false);
});

// End-to-end through discover(): light scope fans out to exactly the two core agents.
test('discover: light scope { deep:false } dispatches ONLY bug-detector + security-reviewer', async () => {
  const ctx = fakeCtx();
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: { deep: false }, limits: {}, policy: {} });
  assert.deepEqual([...new Set(ctx.calls)].sort(), [...CORE_AGENTS].sort());
  assert.deepEqual([...out.dispatched].sort(), [...CORE_AGENTS].sort());
});

// The disabled dimensions are NOT reported as gaps/degradation — a scoped-out dimension is
// intentionally uncovered, distinct from a dispatched-but-failed one (the report gap section
// only surfaces dispatched agents, so light scope produces no false "uncovered" noise).
test('discover: light scope reports no gaps/degradation for the scoped-out dimensions', async () => {
  const ctx = fakeCtx();
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: { deep: false }, limits: {}, policy: {} });
  assert.equal(out.gaps.length, 0);
  assert.equal(out.degraded.length, 0);
});

// The DEEP flag covers precisely the non-core dimensions (registry ⇄ scope invariant): the
// set of dimensions gated by 'deep' must be exactly {all dimensions} minus {bug, security}.
test('registry: the deep flag gates exactly the non-core dimensions', () => {
  const gated = DIMENSIONS.filter((d) => d.conditionalFlag === 'deep').map((d) => d.dimension).sort();
  const core = DIMENSIONS.filter((d) => d.conditionalFlag === null).map((d) => d.dimension).sort();
  assert.deepEqual(core, ['bug', 'security']);
  assert.deepEqual(gated, DIMENSIONS.map((d) => d.dimension).filter((d) => !['bug', 'security'].includes(d)).sort());
});

test('null member becomes a gap + degrades its dimension, siblings survive', async () => {
  const ctx = fakeCtx({ nulls: ['code-gauntlet:security-reviewer'],
    byAgent: { 'code-gauntlet:bug-detector': { findings: [{ id: 'F1' }], complete: true, total_seen: 1 } } });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  assert.ok(out.gaps.some((g) => /security-reviewer/.test(g)));
  assert.equal(out.findings.length, 1);
  // A null result is terminal degradation: the failed agent's dimension is marked immediately.
  assert.ok(out.degraded.includes('security'));
});

// Bug 3 regression: discover must inject the SHORT agent name ('bug-detector'), not the
// full dispatch agentType ('code-gauntlet:bug-detector') — filterFindings and mergeStage
// both match/regroup on the short name, and the prefix silently broke both live.
test('discover injects the SHORT agent name onto findings, not the dispatch agentType', async () => {
  const ctx = fakeCtx({
    byAgent: { 'code-gauntlet:bug-detector': { findings: [{ id: 'F1' }], complete: true, total_seen: 1 } },
  });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  const f1 = out.findings.find((f) => f.id === 'F1');
  assert.equal(f1.agent, 'bug-detector');
});

test('a nulled multi-dimension agent degrades every dimension it covers', async () => {
  const ctx = fakeCtx({ nulls: ['code-gauntlet:conventions-and-intent'] });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  assert.deepEqual([...out.degraded].sort(), ['comment_accuracy', 'convention', 'intent']);
});

test('complete=false is a SOFT possibly-incomplete gap, not degradation', async () => {
  const ctx = fakeCtx({ byAgent: { 'code-gauntlet:bug-detector': { findings: [], complete: false, total_seen: 999 } } });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  assert.ok(out.gaps.some((g) => /possibly incomplete|bug-detector/.test(g)));
  assert.equal(out.degraded.length, 0); // a live-but-incomplete agent did not fail -> not degraded
});

test('discover returns `dispatched`: every active agentType, regardless of outcome', async () => {
  const ctx = fakeCtx({ nulls: ['code-gauntlet:security-reviewer'] });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  // Includes the nulled (failed) agent too — a dispatch attempt happened even though it failed.
  assert.deepEqual([...out.dispatched].sort(), [...AGENTS].sort());
});

// v2-grade elicitation frame (hill-climb iter 1): the terse one-liner dropped raw
// discovery yield ~40%. Assert the markers that replace it are present in every
// dispatched prompt — context-file-first instruction, explicit no-cap/no-minimum
// framing, and the agent's own dimension names (not a generic "your dimensions").
test('discoverPrompt: elicitation markers present (context-file-first, no-cap, dimension naming)', async () => {
  const ctx = fakeCtx();
  await discover(ctx, {
    changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {}, contextPath: '/abs/ctx.md',
  });
  const bugPrompt = ctx.prompts['code-gauntlet:bug-detector'];
  assert.match(bugPrompt, /Read the shared context at \/abs\/ctx\.md first/);
  assert.match(bugPrompt, /no cap and no minimum/);
  assert.match(bugPrompt, /genuine post-investigation absence/);
  assert.match(bugPrompt, /\bbug\b/); // names its own dimension
  assert.match(bugPrompt, /canonical schema/);
  assert.match(bugPrompt, /single paragraph/);
  // Multi-dimension agent names ALL of its dimensions, not just one.
  const conventionsPrompt = ctx.prompts['code-gauntlet:conventions-and-intent'];
  assert.match(conventionsPrompt, /convention/);
  assert.match(conventionsPrompt, /intent/);
  assert.match(conventionsPrompt, /comment_accuracy/);
});

test('discoverPrompt: no contextPath -> no dangling context-file instruction', async () => {
  const ctx = fakeCtx();
  await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  assert.doesNotMatch(ctx.prompts['code-gauntlet:bug-detector'], /Read the shared context/);
});

// Hill-climb iter 5 (evidence discipline): the base discoverPrompt now demands concrete,
// inspected evidence on every finding and names-the-path proof for any absence claim.
// Dimension-agnostic -> present in EVERY dispatched agent prompt.
test('discoverPrompt: evidence-discipline clause present in every agent prompt', async () => {
  const ctx = fakeCtx();
  await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  for (const agentType of AGENTS) {
    const p = ctx.prompts[agentType];
    assert.match(p, /evidence field must be non-empty/, `${agentType} missing evidence clause`);
    assert.match(p, /absence or "missing" claim/, `${agentType} missing absence clause`);
    assert.match(p, /name in evidence the specific file or path/, `${agentType} missing name-the-path clause`);
  }
});

// Hill-climb iter 5 (discovery breadth): per-agent promptExtra sweeps, scoped via
// registry.js. security-reviewer carries the security sweep; bug-detector and
// conventions-and-intent carry the typo/naming sweep; the other agents carry neither.
test('discoverPrompt: per-agent sweeps are scoped (security vs typo/naming vs none)', async () => {
  const ctx = fakeCtx();
  await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });

  const security = ctx.prompts['code-gauntlet:security-reviewer'];
  assert.match(security, /SSRF and unvalidated-URL fetches/);
  assert.match(security, /postMessage handlers that do not validate event\.origin/);
  assert.match(security, /string-matching bypass patterns/);
  assert.doesNotMatch(security, /typo and naming sweep/);

  for (const agentType of ['code-gauntlet:bug-detector', 'code-gauntlet:conventions-and-intent']) {
    const p = ctx.prompts[agentType];
    assert.match(p, /explicit typo and naming sweep/, `${agentType} missing typo/naming sweep`);
    assert.match(p, /case-sensitivity mistakes in string comparisons/, `${agentType} missing case-sensitivity clause`);
    assert.doesNotMatch(p, /SSRF and unvalidated-URL fetches/, `${agentType} should not carry the security sweep`);
  }

  // An agent with no promptExtra carries neither sweep.
  const crossFile = ctx.prompts['code-gauntlet:cross-file-impact'];
  assert.doesNotMatch(crossFile, /SSRF and unvalidated-URL fetches/);
  assert.doesNotMatch(crossFile, /explicit typo and naming sweep/);
});

test('worst-case agent count stays under 1000; coarsening lowers challengeCap and raises batch sizes', () => {
  const limits = { summarizeBucketSize: 20, validateBatch: 10, challengeCap: 40, verifySliceSize: 200 };
  assert.ok(worstCaseAgentCount(limits, 500, 3000) < 1000);
  // A deliberately huge shape that WOULD blow the cap without coarsening.
  const huge = { summarizeBucketSize: 20, validateBatch: 2, challengeCap: 900, verifySliceSize: 5 };
  const coarse = coarsenLimits(huge, 500, 5000);
  assert.ok(coarse.validateBatch >= huge.validateBatch);     // bigger batches -> fewer validators
  assert.ok(coarse.verifySliceSize >= huge.verifySliceSize); // bigger slices -> fewer executor reads
  assert.ok(coarse.challengeCap <= huge.challengeCap);       // LOWER cap -> fewer challengers (min(n,cap))
  assert.ok(worstCaseAgentCount(coarse, 500, 5000) < 900);   // loop terminated below the guard
});

test('coarsenLimits raises summarizeBucketSize so a pathological file count still converges', () => {
  const limits = { summarizeBucketSize: 20, validateBatch: 10, challengeCap: 40, verifySliceSize: 200 };
  // ~20k changed files: the summarize term ceil(20000/20)+1 = 1001 ALONE exceeds the guard,
  // so no validate/challenge coarsening can converge unless summarizeBucketSize also widens.
  const coarse = coarsenLimits(limits, 20000, 3000);
  assert.ok(coarse.summarizeBucketSize > limits.summarizeBucketSize); // wider bucket -> fewer summarize calls
  assert.ok(worstCaseAgentCount(coarse, 20000, 3000) < 900);          // converges despite the file count
});

test('absent challengeCap counts as challenge-every-finding (mirrors challengeStage), never as zero', () => {
  // challengeStage defaults a missing/null cap to findings.length; the guard must
  // count the same fan-out or it can skip coarsening the stage actually needs.
  const noCap = { summarizeBucketSize: 20, validateBatch: 25, verifySliceSize: 200 };
  const withCap = { ...noCap, challengeCap: 1000 };
  assert.equal(worstCaseAgentCount(noCap, 0, 1000), worstCaseAgentCount(withCap, 0, 1000));
  // 0 is a REAL cap of zero (also mirrors the stage), distinct from absent.
  assert.ok(worstCaseAgentCount({ ...noCap, challengeCap: 0 }, 0, 1000) < worstCaseAgentCount(noCap, 0, 1000));
  // With the cap absent and a pathological finding count, coarsening must fire and
  // converge — under the old `|| 0` semantics the challenge term was invisible here.
  const coarse = coarsenLimits(noCap, 0, 5000);
  assert.ok(coarse.challengeCap != null, 'coarsening pins a concrete cap');
  assert.ok(worstCaseAgentCount(coarse, 0, 5000) < 900);
  // Present-cap behavior is byte-identical to the pre-fix semantics (benchmark shape).
  const bench = { summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 200 };
  assert.deepEqual(coarsenLimits(bench, 23, 40), { ...bench });
});

test('absent size limits mirror stage defaults — the guard never goes NaN-silent', () => {
  // Math.max(1, undefined) is NaN; a NaN worst case made `NaN >= 900` false and
  // silently disabled coarsening. The guard now mirrors each stage's own default:
  // summarize bucket 20, verify/validate ONE slice/batch over all findings.
  const n = worstCaseAgentCount({}, 20000, 1000);
  assert.ok(Number.isFinite(n), 'worst case must be a real number');
  assert.equal(n, (1000 + 1) + 7 + 1 + 1 + 1000 + 2); // 20000/20 buckets +merge, 7 discovery, 1 slice, 1 batch, challenge-all, report+writer
  // Coarsening fires and converges from fully-absent limits.
  const coarse = coarsenLimits({}, 20000, 5000);
  assert.ok(worstCaseAgentCount(coarse, 20000, 5000) < 900);
  // 0-valued sizes mirror the stages too (summarize treats 0 as 20; verify/validate as one slice/batch).
  assert.equal(
    worstCaseAgentCount({ summarizeBucketSize: 0, verifySliceSize: 0, validateBatch: 0, challengeCap: 40 }, 200, 100),
    worstCaseAgentCount({ verifySliceSize: 100, validateBatch: 100, challengeCap: 40 }, 200, 100),
  );
});

// Item 3: confidence is declared NUMBER end-to-end — the discovery finding schema must
// declare confidence:number so the string form ("85") the filter's consensus boost could
// string-concatenate never exists. Item 4 (array support): a schemaExtra with an array-valued
// fragment (cross-file-impact -> affected_consumers) reaches the per-agent item schema intact.
test('discovery finding schema declares confidence NUMBER + reconciled schemaExtra (array support)', async () => {
  const schemas = {};
  const ctx = {
    calls: [],
    agent: async (prompt, opts = {}) => {
      assertPrompt(prompt);
      assertValidSchema(opts.schema); // recursively validates the array fragment declares items
      schemas[opts.label] = opts.schema;
      return { findings: [], complete: true, total_seen: 0 };
    },
    parallel: async (thunks) => Promise.all(thunks.map((thunk) => thunk())),
  };
  await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });

  const bugItem = schemas['code-gauntlet:bug-detector'].properties.findings.items.properties;
  assert.equal(bugItem.confidence.type, 'number', 'confidence declared NUMBER at discovery (no string form)');
  assert.equal(bugItem.hidden_errors.type, 'string', "bug-detector's reconciled extra");

  const cfItem = schemas['code-gauntlet:cross-file-impact'].properties.findings.items.properties;
  assert.equal(cfItem.affected_consumers.type, 'array', 'array-valued schemaExtra survives to the item schema');
  assert.equal(cfItem.affected_consumers.items.type, 'string');
});

// --- Supplementary: summarize (single vs bucketed) + mergeStage envelope ------

function summarizeCtx({ agentImpl, parallelImpl } = {}) {
  const calls = [];
  const agent = agentImpl || (async (prompt, opts = {}) => {
    assertPrompt(prompt);
    assertValidSchema(opts.schema);
    calls.push(opts.label);
    return { summary: `S:${opts.label}` };
  });
  // Default parallel CALLS each thunk (which invokes agent -> records the label).
  const parallel = parallelImpl || (async (thunks) => Promise.all(thunks.map((thunk) => thunk())));
  return { calls, agent, parallel };
}

test('summarize: small PR uses a single agent() call, no gaps', async () => {
  const ctx = summarizeCtx();
  const out = await summarize(ctx, { changedFiles: ['a.js', 'b.js'], changedLines: 10, limits: { summarizeBucketSize: 20 }, policy: {} });
  assert.equal(ctx.calls.length, 1);
  assert.equal(out.gaps.length, 0);
  assert.ok(out.summary.length > 0);
});

// L7: the prompt single-sources the change-size number — the summarizer must cite the
// waist's changedLines verbatim, never re-estimate from the diff (live run: 1211 vs ~1218).
test('summarize: the prompt carries the authoritative changedLines count verbatim', async () => {
  const prompts = [];
  const ctx = summarizeCtx({ agentImpl: async (prompt) => { prompts.push(prompt); return { summary: 's' }; } });
  await summarize(ctx, { changedFiles: ['a.js'], changedLines: 1211, limits: { summarizeBucketSize: 20 }, policy: {} });
  assert.match(prompts[0], /authoritative changed-line count is 1211/);
  assert.match(prompts[0], /never re-estimate/);
});

test('summarize: an agent() throw degrades to empty summary + a gap', async () => {
  const ctx = summarizeCtx({ agentImpl: async () => { throw new Error('schema exhausted'); } });
  const out = await summarize(ctx, { changedFiles: ['a.js'], changedLines: 10, limits: { summarizeBucketSize: 20 }, policy: {} });
  assert.equal(out.summary, '');
  assert.ok(out.gaps.some((g) => /summarize failed/.test(g)));
});

test('summarize: a large PR buckets per-file (parallel) then a single merge call', async () => {
  const files = Array.from({ length: 50 }, (_, i) => `f${i}.js`);
  const ctx = summarizeCtx();
  const out = await summarize(ctx, { changedFiles: files, changedLines: 600, limits: { summarizeBucketSize: 20 }, policy: {} });
  // 3 buckets (ceil(50/20)) fan out through parallel, then exactly 1 merge agent() call.
  const mergeCalls = ctx.calls.filter((l) => /merge/.test(l));
  assert.equal(mergeCalls.length, 1);
  assert.ok(out.summary.length > 0);
  assert.equal(out.gaps.length, 0);
});

test('summarize: bucketed PR with every bucket nulled degrades to a gap', async () => {
  const files = Array.from({ length: 50 }, (_, i) => `f${i}.js`);
  const ctx = summarizeCtx({ parallelImpl: async (tasks) => tasks.map(() => null) });
  const out = await summarize(ctx, { changedFiles: files, changedLines: 600, limits: { summarizeBucketSize: 20 }, policy: {} });
  assert.equal(out.summary, '');
  assert.ok(out.gaps.some((g) => /summarize failed/.test(g)));
});

test('mergeStage: consumes merge() as-is and produces the Phase-4 envelope', () => {
  const discoverOut = {
    findings: [
      { id: 'F1', file: 'a.js', line_start: 1, title: 't1', description: 'd1', severity: 'high', confidence: 'high', dimension: 'bug', agent: 'code-gauntlet:bug-detector' },
      { id: 'F2', file: 'b.js', line_start: 2, title: 't2', description: 'd2', severity: 'low', confidence: 'low', dimension: 'security', agent: 'code-gauntlet:security-reviewer' },
    ],
    gaps: [],
    degraded: [],
  };
  const meta = { base_branch: 'main', head_sha: 'abc123', pr_number: 7, owner: 'o', repo: 'r' };
  const env = mergeStage(discoverOut, meta);
  assert.equal(env.findings.length, 2);
  assert.equal(env.base_branch, 'main');
  assert.equal(env.head_sha, 'abc123');
  assert.ok(env.methodology);
  assert.ok(env.methodology.agents_dispatched.includes('code-gauntlet:bug-detector'));
  // agent field survives the round-trip (re-injected by merge()).
  assert.ok(env.findings.every((f) => typeof f.agent === 'string'));
});

test('mergeStage: agents_dispatched counts a zero-finding agent, distinct from never-dispatched', () => {
  // code-gauntlet:code-simplifier was dispatched (discover() attempted it) but produced
  // zero findings; code-gauntlet:type-design-analyzer was never in `dispatched` at all
  // (e.g. disabled via agentFlags). agents_dispatched must include the former, not the latter.
  // `dispatched` mirrors discover()'s real output (full 'code-gauntlet:' agentType strings,
  // unaffected by FIX 1); mergeStage normalizes it to the SHORT form to match findings'
  // own (short, post-FIX-1) .agent field, so agents_dispatched comes out short too.
  const discoverOut = {
    findings: [
      { id: 'F1', file: 'a.js', line_start: 1, title: 't1', description: 'd1', severity: 'high', confidence: 'high', dimension: 'bug', agent: 'bug-detector' },
    ],
    gaps: [],
    degraded: [],
    dispatched: ['code-gauntlet:bug-detector', 'code-gauntlet:code-simplifier'],
  };
  const meta = { base_branch: 'main', head_sha: 'abc123', pr_number: 7, owner: 'o', repo: 'r' };
  const env = mergeStage(discoverOut, meta);
  assert.deepEqual([...env.methodology.agents_dispatched].sort(), ['bug-detector', 'code-simplifier']);
  assert.ok(!env.methodology.agents_dispatched.includes('type-design-analyzer'));
});
