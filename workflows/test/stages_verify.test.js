// stages_verify.test.js — orchestration-contract tests for the Verify stage.
// verifyStage dispatches the `executor` agent (one call per verifySliceSize slice),
// and trusts a slice's result ONLY when status==='ok' and the receipt echoes the
// dispatched nonce, head sha, and slice finding-count. ANY untrusted slice (wrong
// receipt, status:'failed', or agent throw) degrades the WHOLE set to the UNVERIFIED
// path: every finding origin='unknown', surfaced-classification skipped, a loud gap,
// verified=false — findings are never dropped and success is never fabricated.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { verifyStage } from '../src/stages.js';
import { assertPrompt, assertValidSchema } from './helpers/pipelineMock.js';

// Platform contract: agent(promptString, opts). verify uses SEQUENTIAL agent() calls
// (never parallel() — the order pairs receipts to slices), so parallel() throws here.
// Before the executor loop, verifyStage dispatches artifact-writer 'verify-input-writer-*'
// calls to materialize the slice inputs; those are handled separately (succeeding by
// default, or via cfg.sliceWriter) so `agentImpl` sees only executor dispatches with a
// clean 0-based executor index. Each recorded call is { prompt, ...opts } so tests can
// read the embedded command from the prompt. Every dispatch asserts the contract.
function verifyCtx(agentImpl, cfg = {}) {
  const calls = [];
  let execIdx = -1;
  const agent = async (prompt, opts = {}) => {
    assertPrompt(prompt);
    assertValidSchema(opts.schema);
    const call = { prompt, ...opts };
    calls.push(call);
    if ((opts.label || '').startsWith('verify-input-writer')) {
      return cfg.sliceWriter ? cfg.sliceWriter(call) : { written: [] };
    }
    execIdx += 1;
    return agentImpl(call, execIdx);
  };
  return {
    calls,
    execCalls: () => calls.filter((t) => (t.label || '').startsWith('verify-slice-')),
    agent,
    parallel: async () => { throw new Error('verifyStage must use agent() per slice, not parallel()'); },
  };
}

function baseInput(overrides = {}) {
  return {
    findings: [
      { id: 'F1', file: 'a.js', line_start: 1, origin: 'new', dimension: 'bug', cross_file_refs: [] },
      { id: 'F2', file: 'b.js', line_start: 2, origin: 'new', dimension: 'security', cross_file_refs: ['c.js:9'] },
    ],
    nonce: 'n-1',
    headShaShort: 'abc123',
    limits: { verifySliceSize: 200 },
    policy: {},
    verify: {
      scriptPath: '/plugin/scripts/verify_findings.py',
      inputPathBase: '/out/phase4-input-abc123',
      outputPathBase: '/out/phase4-output-abc123',
      baseBranch: 'main',
      diffPath: '/out/deep-review-diff-abc123.patch',
    },
    ...overrides,
  };
}

function okEnvelope(findings, { sha = 'abc123', nonce = 'n-1', n_in = findings.length } = {}) {
  return {
    status: 'ok',
    receipt: { sha, nonce, n_in },
    result: { verified: findings, eliminated: [], batches: [], stats: {} },
  };
}

test('(a) valid ok envelope with matching receipt -> findings verified, verified===true', async () => {
  const input = baseInput();
  const verifiedFindings = input.findings.map((f) => ({ ...f, origin: 'new' }));
  // Per-slice nonce: slice i must echo `${nonce}.${i}` (here slice 0 -> n-1.0).
  const ctx = verifyCtx((_t, i) => okEnvelope(verifiedFindings, { nonce: `n-1.${i}` }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.equal(out.findings.length, 2);
  assert.equal(out.gaps.length, 0);
  assert.ok(out.findings.every((f) => f.origin !== 'unknown'));
  // cross_file_refs survives verbatim (surfaced-classification depends on it downstream).
  assert.deepEqual(out.findings.find((f) => f.id === 'F2').cross_file_refs, ['c.js:9']);
});

test('(b) wrong nonce -> UNVERIFIED: every origin unknown, verified false, loud gap', async () => {
  const input = baseInput();
  const ctx = verifyCtx(() => okEnvelope(input.findings, { nonce: 'WRONG' }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 2); // preserved, never dropped
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.ok(out.gaps.length > 0);
});

test('(c) status:failed -> UNVERIFIED path, findings preserved (never dropped)', async () => {
  const input = baseInput();
  const ctx = verifyCtx(() => ({ status: 'failed', exitCode: 1, stderr: 'boom' }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 2);
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.ok(out.gaps.some((g) => /unverified|verify/i.test(g)));
});

test('(c2) UNVERIFIED path pins numeric-string fields — confidence "85" leaves as 85, never fuel for downstream string concatenation', async () => {
  const input = baseInput();
  // Discovery-shaped findings: the by-value schema declares confidence as a string,
  // so StructuredOutput renders the agents' numeric score as "85". line_start gets the
  // same treatment. Non-numeric values must pass through untouched.
  input.findings = [
    { id: 'F1', file: 'a.js', line_start: '3', confidence: '85', origin: 'new', dimension: 'bug', cross_file_refs: [] },
    { id: 'F2', file: 'a.js', line_start: 4, confidence: 90, origin: 'new', dimension: 'bug', cross_file_refs: [] },
    { id: 'F3', file: 'b.js', line_start: 5, confidence: null, origin: 'new', dimension: 'bug', cross_file_refs: [] },
  ];
  const ctx = verifyCtx(() => ({ status: 'failed', exitCode: 1, stderr: 'boom' }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  const byId = Object.fromEntries(out.findings.map((f) => [f.id, f]));
  assert.strictEqual(byId.F1.confidence, 85);
  assert.strictEqual(byId.F1.line_start, 3);
  assert.strictEqual(byId.F2.confidence, 90);
  assert.strictEqual(byId.F3.confidence, null);
});

test('(d) receipt sha mismatch -> UNVERIFIED', async () => {
  const input = baseInput();
  // Correct per-slice nonce so the sha check is the one that trips.
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}`, sha: 'deadbeef' }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
});

test('(e) receipt n_in mismatch (dispatched count) -> UNVERIFIED', async () => {
  const input = baseInput();
  // Receipt claims 1 input finding but we dispatched 2 — the count guard.
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}`, n_in: 1 }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
});

test('(e2) result completeness: verified+eliminated != n_in -> UNVERIFIED (transport truncation)', async () => {
  const input = baseInput();
  // nonce/sha/n_in all match, but the result arrays were truncated in transport:
  // verified(1)+eliminated(0) != n_in(2). Without this guard a finding silently vanishes.
  const ctx = verifyCtx((_t, i) => ({
    status: 'ok',
    receipt: { sha: 'abc123', nonce: `n-1.${i}`, n_in: input.findings.length },
    result: { verified: [input.findings[0]], eliminated: [], batches: [], stats: {} },
  }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, input.findings.length); // originals preserved
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.ok(out.gaps.some((g) => /incomplete|truncat/i.test(g)));
});

test('(f) agent() throw -> UNVERIFIED, findings preserved', async () => {
  const input = baseInput();
  const ctx = verifyCtx(() => { throw new Error('schema-retry exhausted'); });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 2);
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.ok(out.gaps.length > 0);
});

test('(g) large set slices into ceil(n/verifySliceSize) executor calls; all trusted -> verified', async () => {
  const findings = Array.from({ length: 5 }, (_, i) => ({ id: `F${i}`, origin: 'new', cross_file_refs: [] }));
  const input = baseInput({ findings, limits: { verifySliceSize: 2 } });
  const slices = [];
  for (let k = 0; k < findings.length; k += 2) slices.push(findings.slice(k, k + 2));
  // Each executor call answers for exactly its slice: per-slice nonce + n_in === slice length.
  const ctx = verifyCtx((_t, i) => okEnvelope(slices[i], { nonce: `n-1.${i}`, n_in: slices[i].length }));
  const out = await verifyStage(ctx, input);
  assert.equal(ctx.execCalls().length, slices.length); // 3 = ceil(5/2) executor calls
  assert.equal(out.verified, true);
  assert.equal(out.findings.length, 5); // verified findings from every slice, concatenated
  assert.equal(out.gaps.length, 0);
});

test('(h) one bad slice among several -> the whole set is UNVERIFIED (all-or-nothing)', async () => {
  const findings = Array.from({ length: 5 }, (_, i) => ({ id: `F${i}`, origin: 'new' }));
  const input = baseInput({ findings, limits: { verifySliceSize: 2 } });
  const slices = [];
  for (let k = 0; k < findings.length; k += 2) slices.push(findings.slice(k, k + 2));
  const ctx = verifyCtx((_t, i) => okEnvelope(slices[i], { nonce: `n-1.${i}`, n_in: i === 1 ? 999 : slices[i].length }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 5);
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
});

test('(h2) equal-length slices cannot satisfy each other: per-slice nonces are distinct', async () => {
  const findings = Array.from({ length: 4 }, (_, i) => ({ id: `F${i}`, origin: 'new' }));
  const input = baseInput({ findings, limits: { verifySliceSize: 2 } });
  const commands = [];
  // Answer every slice with slice 0's nonce (n-1.0). Only slice 0 should be trusted;
  // slice 1 (also length 2) must NOT accept n-1.0 -> whole set UNVERIFIED.
  const ctx = verifyCtx((t, i) => {
    commands.push(t.prompt); // the pinned command is embedded in the executor prompt
    return okEnvelope(findings.slice(0, 2), { nonce: 'n-1.0', n_in: 2 });
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false); // slice 1's receipt nonce n-1.0 != expected n-1.1
  // The prompts prove distinct per-slice nonces were dispatched.
  assert.match(commands[0], /--nonce n-1\.0(\s|$)/);
  assert.match(commands[1], /--nonce n-1\.1(\s|$)/);
});

test('(j) slice inputs are materialized by the artifact-writer BEFORE any executor runs', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  await verifyStage(ctx, input);
  // The first dispatch is the slice-input writer; the executor(s) come after.
  assert.match(ctx.calls[0].label, /^verify-input-writer/);
  assert.equal(ctx.calls[0].agentType, 'deep-review:artifact-writer');
  const firstExecIdx = ctx.calls.findIndex((t) => (t.label || '').startsWith('verify-slice-'));
  const writerIdx = ctx.calls.findIndex((t) => (t.label || '').startsWith('verify-input-writer'));
  assert.ok(writerIdx >= 0 && writerIdx < firstExecIdx, 'writer dispatched before executors');
  // The writer prompt carries the sliced findings by value and their target paths.
  assert.match(ctx.calls[0].prompt, /phase4-input-abc123\.slice0\.json/);
  assert.match(ctx.calls[0].prompt, /"id":"F1"/);
});

test('(k) slice-input writer failure -> whole set UNVERIFIED, no executor dispatched', async () => {
  const input = baseInput();
  const ctx = verifyCtx(
    () => { throw new Error('executor should never run when slice inputs were not written'); },
    { sliceWriter: () => null }, // writer returns null -> materialization failed
  );
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 2); // preserved, never dropped
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.ok(out.gaps.some((g) => /UNVERIFIED/.test(g) && /writer/i.test(g)));
  assert.equal(ctx.execCalls().length, 0, 'no executor ran after the write failure');
});

test('(l) slice-input writer THROW -> whole set UNVERIFIED (never fabricate)', async () => {
  const input = baseInput();
  const ctx = verifyCtx(
    () => okEnvelope(input.findings),
    { sliceWriter: () => { throw new Error('disk on fire'); } },
  );
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.equal(ctx.execCalls().length, 0);
});

test('(i) empty finding set -> trivially verified, no executor calls', async () => {
  const input = baseInput({ findings: [] });
  const ctx = verifyCtx(() => { throw new Error('should not dispatch for an empty set'); });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.equal(out.findings.length, 0);
  assert.equal(ctx.calls.length, 0);
});

test('the executor command is a single AST-safe python3 word-token invocation', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  await verifyStage(ctx, input);
  const t = ctx.execCalls()[0];
  const cmd = t.command || t.prompt || '';
  assert.match(cmd, /python3 \S*verify_findings\.py/);
  assert.match(cmd, /--input /);
  assert.match(cmd, /--output /);
  assert.match(cmd, /--nonce n-1\.0(\s|$)/); // per-slice derived nonce
  assert.match(cmd, /--head-sha abc123/);
  // No shell substitution / heredocs / env-prefix (CLAUDE.md AST-safe emission).
  assert.doesNotMatch(cmd, /\$\(|`|<<|\$\{|&&|\|\|/);
  assert.equal(t.agentType, 'deep-review:executor');
});
