// stages_discover.test.js — orchestration-contract tests for stages 1-3
// (Summarize, Discover, Merge) + the agent-count coarsening formula.
// ctx is injected {agent, parallel}; the mock ctx is the testability seam.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { summarize, discover, mergeStage, worstCaseAgentCount, coarsenLimits } from '../src/stages.js';
import { AGENTS } from '../src/registry.js';

function fakeCtx({ nulls = [], byAgent = {} } = {}) {
  const calls = [];
  return {
    calls,
    parallel: async (tasks) => Promise.all(tasks.map(async (t, i) => {
      calls.push(t.label);
      if (nulls.includes(t.label)) return null; // parallel() null-isolation
      return byAgent[t.label] ?? { findings: [], complete: true, total_seen: 0 };
    })),
    agent: async () => { throw new Error('discover must use parallel(), not bare agent()'); },
  };
}

test('discover dispatches once per AGENT (7)', async () => {
  const ctx = fakeCtx();
  await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: { schemaFailureLimit: 3 }, policy: {} });
  assert.equal(new Set(ctx.calls).size, AGENTS.length);
});

test('null member becomes a gap, siblings survive', async () => {
  const ctx = fakeCtx({ nulls: ['deep-review:security-reviewer'],
    byAgent: { 'deep-review:bug-detector': { findings: [{ id: 'F1' }], complete: true, total_seen: 1 } } });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: { schemaFailureLimit: 3 }, policy: {} });
  assert.ok(out.gaps.some((g) => /security-reviewer/.test(g)));
  assert.equal(out.findings.length, 1);
});

test('complete=false is surfaced as possibly-incomplete', async () => {
  const ctx = fakeCtx({ byAgent: { 'deep-review:bug-detector': { findings: [], complete: false, total_seen: 999 } } });
  const out = await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: { schemaFailureLimit: 3 }, policy: {} });
  assert.ok(out.gaps.some((g) => /possibly incomplete|bug-detector/.test(g)));
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
  return {
    calls,
    agent: agentImpl || (async (t) => { calls.push(t.label); return { summary: `S:${t.label}` }; }),
    parallel: parallelImpl || (async (tasks) => Promise.all(tasks.map(async (t) => {
      calls.push(t.label);
      return { summary: `part:${t.label}` };
    }))),
  };
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
