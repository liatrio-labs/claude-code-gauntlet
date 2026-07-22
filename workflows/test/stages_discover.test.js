// stages_discover.test.js — orchestration-contract tests for stages 1-3
// (Summarize, Discover, Merge) + the agent-count coarsening formula.
// ctx is injected {agent, parallel}; the mock ctx is the testability seam.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { summarize, discover, mergeStage, worstCaseAgentCount, coarsenLimits } from '../src/stages.js';
import { AGENTS } from '../src/registry.js';
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

test('null member becomes a gap + degrades its dimension, siblings survive', async () => {
  const ctx = fakeCtx({ nulls: ['deep-review:security-reviewer'],
    byAgent: { 'deep-review:bug-detector': { findings: [{ id: 'F1' }], complete: true, total_seen: 1 } } });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  assert.ok(out.gaps.some((g) => /security-reviewer/.test(g)));
  assert.equal(out.findings.length, 1);
  // A null result is terminal degradation: the failed agent's dimension is marked immediately.
  assert.ok(out.degraded.includes('security'));
});

// Bug 3 regression: discover must inject the SHORT agent name ('bug-detector'), not the
// full dispatch agentType ('deep-review:bug-detector') — filterFindings and mergeStage
// both match/regroup on the short name, and the prefix silently broke both live.
test('discover injects the SHORT agent name onto findings, not the dispatch agentType', async () => {
  const ctx = fakeCtx({
    byAgent: { 'deep-review:bug-detector': { findings: [{ id: 'F1' }], complete: true, total_seen: 1 } },
  });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  const f1 = out.findings.find((f) => f.id === 'F1');
  assert.equal(f1.agent, 'bug-detector');
});

test('a nulled multi-dimension agent degrades every dimension it covers', async () => {
  const ctx = fakeCtx({ nulls: ['deep-review:conventions-and-intent'] });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  assert.deepEqual([...out.degraded].sort(), ['comment_accuracy', 'convention', 'intent']);
});

test('complete=false is a SOFT possibly-incomplete gap, not degradation', async () => {
  const ctx = fakeCtx({ byAgent: { 'deep-review:bug-detector': { findings: [], complete: false, total_seen: 999 } } });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  assert.ok(out.gaps.some((g) => /possibly incomplete|bug-detector/.test(g)));
  assert.equal(out.degraded.length, 0); // a live-but-incomplete agent did not fail -> not degraded
});

test('discover returns `dispatched`: every active agentType, regardless of outcome', async () => {
  const ctx = fakeCtx({ nulls: ['deep-review:security-reviewer'] });
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
  const bugPrompt = ctx.prompts['deep-review:bug-detector'];
  assert.match(bugPrompt, /Read the shared context at \/abs\/ctx\.md first/);
  assert.match(bugPrompt, /no cap and no minimum/);
  assert.match(bugPrompt, /genuine post-investigation absence/);
  assert.match(bugPrompt, /\bbug\b/); // names its own dimension
  assert.match(bugPrompt, /canonical schema/);
  assert.match(bugPrompt, /single paragraph/);
  // Multi-dimension agent names ALL of its dimensions, not just one.
  const conventionsPrompt = ctx.prompts['deep-review:conventions-and-intent'];
  assert.match(conventionsPrompt, /convention/);
  assert.match(conventionsPrompt, /intent/);
  assert.match(conventionsPrompt, /comment_accuracy/);
});

test('discoverPrompt: no contextPath -> no dangling context-file instruction', async () => {
  const ctx = fakeCtx();
  await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  assert.doesNotMatch(ctx.prompts['deep-review:bug-detector'], /Read the shared context/);
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

  const security = ctx.prompts['deep-review:security-reviewer'];
  assert.match(security, /SSRF and unvalidated-URL fetches/);
  assert.match(security, /postMessage handlers that do not validate event\.origin/);
  assert.match(security, /string-matching bypass patterns/);
  assert.doesNotMatch(security, /typo and naming sweep/);

  for (const agentType of ['deep-review:bug-detector', 'deep-review:conventions-and-intent']) {
    const p = ctx.prompts[agentType];
    assert.match(p, /explicit typo and naming sweep/, `${agentType} missing typo/naming sweep`);
    assert.match(p, /case-sensitivity mistakes in string comparisons/, `${agentType} missing case-sensitivity clause`);
    assert.doesNotMatch(p, /SSRF and unvalidated-URL fetches/, `${agentType} should not carry the security sweep`);
  }

  // An agent with no promptExtra carries neither sweep.
  const crossFile = ctx.prompts['deep-review:cross-file-impact'];
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
      { id: 'F1', file: 'a.js', line_start: 1, title: 't1', description: 'd1', severity: 'high', confidence: 'high', dimension: 'bug', agent: 'deep-review:bug-detector' },
      { id: 'F2', file: 'b.js', line_start: 2, title: 't2', description: 'd2', severity: 'low', confidence: 'low', dimension: 'security', agent: 'deep-review:security-reviewer' },
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
  assert.ok(env.methodology.agents_dispatched.includes('deep-review:bug-detector'));
  // agent field survives the round-trip (re-injected by merge()).
  assert.ok(env.findings.every((f) => typeof f.agent === 'string'));
});

test('mergeStage: agents_dispatched counts a zero-finding agent, distinct from never-dispatched', () => {
  // deep-review:code-simplifier was dispatched (discover() attempted it) but produced
  // zero findings; deep-review:type-design-analyzer was never in `dispatched` at all
  // (e.g. disabled via agentFlags). agents_dispatched must include the former, not the latter.
  // `dispatched` mirrors discover()'s real output (full 'deep-review:' agentType strings,
  // unaffected by FIX 1); mergeStage normalizes it to the SHORT form to match findings'
  // own (short, post-FIX-1) .agent field, so agents_dispatched comes out short too.
  const discoverOut = {
    findings: [
      { id: 'F1', file: 'a.js', line_start: 1, title: 't1', description: 'd1', severity: 'high', confidence: 'high', dimension: 'bug', agent: 'bug-detector' },
    ],
    gaps: [],
    degraded: [],
    dispatched: ['deep-review:bug-detector', 'deep-review:code-simplifier'],
  };
  const meta = { base_branch: 'main', head_sha: 'abc123', pr_number: 7, owner: 'o', repo: 'r' };
  const env = mergeStage(discoverOut, meta);
  assert.deepEqual([...env.methodology.agents_dispatched].sort(), ['bug-detector', 'code-simplifier']);
  assert.ok(!env.methodology.agents_dispatched.includes('type-design-analyzer'));
});
