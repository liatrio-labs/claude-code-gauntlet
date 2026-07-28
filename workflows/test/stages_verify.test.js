// stages_verify.test.js — orchestration-contract tests for the Verify stage.
// verifyStage dispatches the `executor` agent (one call per verifySliceSize slice),
// SEQUENTIALLY, and trusts a slice's result ONLY when status==='ok' and the receipt
// echoes the dispatched nonce, head sha, and slice finding-count (trustSlice).
// DEGRADATION IS PER SLICE (issue #54, issue #25 requirement 3): an untrusted slice
// degrades ONLY ITS OWN findings (origin='unknown', surfaced-classification skipped) and
// the loop keeps going — slices that verify cleanly keep their verified output. Each slice
// gets exactly ONE deterministic retry before degrading (VERIFY_ATTEMPTS_PER_SLICE=2):
// attempt 1 is labeled `verify-slice-${i}` with nonce `${nonce}.${i}`, the retry is
// `verify-slice-${i}-retry` with the DISTINCT nonce `${nonce}.${i}.r1` (so a replay of
// attempt 1's receipt cannot satisfy the retry). `verified` is true iff ZERO slices
// degraded. Findings are never dropped and success is never fabricated, at slice
// granularity; output stays in strict slice-index order.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { verifyStage, parseWriterPayload, VERIFY_ATTEMPTS_PER_SLICE } from '../src/stages.js';
import { assertPrompt, assertValidSchema } from './helpers/pipelineMock.js';

// Platform contract: agent(promptString, opts). The EXECUTOR slice loop is deliberately
// SEQUENTIAL bare agent() calls (never parallel() — the order pairs receipts to slices),
// so the mock throws if a 'verify-slice-' dispatch is ever issued from inside parallel().
// The slice-input WRITER group fan-out DOES go through parallel() (issue #38, S2), so
// parallel() is a faithful null-isolating implementation rather than a hard throw.
// Before the executor loop, verifyStage dispatches artifact-writer 'verify-input-writer-*'
// calls to materialize the slice inputs; those are handled separately (succeeding by
// default, or via cfg.sliceWriter) so `agentImpl` sees only executor dispatches.
//
// agentImpl(call, sliceIndex, { attempt }) — sliceIndex and attempt (1 or 2) are derived
// from the dispatch LABEL (`verify-slice-${i}` / `verify-slice-${i}-retry`), not from a
// monotonic call counter: with per-slice retries a counter no longer equals the slice
// index (issue #54 harness fix). Existing tests that only read the second positional arg
// as "the slice index" keep working unchanged — that is still exactly what it is.
function verifyCtx(agentImpl, cfg = {}) {
  const calls = [];
  let inParallel = 0;
  const agent = async (prompt, opts = {}) => {
    assertPrompt(prompt);
    assertValidSchema(opts.schema);
    const call = { prompt, ...opts };
    calls.push(call);
    const label = opts.label || '';
    if (label.startsWith('verify-input-writer')) {
      if (cfg.sliceWriter) return cfg.sliceWriter(call);
      // Faithful default: echo the exact slice-input paths so the write-proof gate passes.
      const entries = parseWriterPayload(prompt) || [];
      return { written: entries.map((e) => e.path) };
    }
    if (label.startsWith('verify-slice-')) {
      if (inParallel > 0) {
        throw new Error('verifyStage must use agent() per slice, not parallel()');
      }
      const m = /^verify-slice-(\d+)(-retry)?$/.exec(label);
      const sliceIndex = m ? Number(m[1]) : -1;
      const attempt = m && m[2] ? 2 : 1;
      return agentImpl(call, sliceIndex, { attempt });
    }
    return agentImpl(call, -1, { attempt: 1 });
  };
  return {
    calls,
    execCalls: () => calls.filter((t) => (t.label || '').startsWith('verify-slice-')),
    // Every dispatch (attempt 1 and, if present, the retry) for one slice index, in
    // dispatch order. Handy for asserting "exactly one retry" / "never re-dispatched".
    execCallsFor: (i) => calls.filter((t) => new RegExp(`^verify-slice-${i}(-retry)?$`).test(t.label || '')),
    agent,
    // parallel() nulls a failed member IN PLACE (Phase 0 contract), preserving input order.
    parallel: async (thunks) => {
      inParallel += 1;
      try {
        return await Promise.all(thunks.map(async (t) => {
          try { return await t(); } catch { return null; }
        }));
      } finally { inParallel -= 1; }
    },
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
      diffPath: '/out/code-gauntlet-diff-abc123.patch',
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

test('(h) one bad slice among several -> ONLY that slice degrades, per-slice (issue #54)', async () => {
  const findings = Array.from({ length: 5 }, (_, i) => ({ id: `F${i}`, origin: 'new' }));
  const input = baseInput({ findings, limits: { verifySliceSize: 2 } });
  const slices = [];
  for (let k = 0; k < findings.length; k += 2) slices.push(findings.slice(k, k + 2));
  // Slice 1 (F2, F3) fails deterministically on EVERY attempt (both the first dispatch and
  // the retry return the same untrusted shape); slices 0 and 2 are trusted on their first try.
  const ctx = verifyCtx((_t, i) => {
    if (i === 1) return { status: 'failed', exitCode: 1, stderr: 'boom' };
    return okEnvelope(slices[i], { nonce: `n-1.${i}`, n_in: slices[i].length });
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false); // one degraded slice still marks the run as untrusted
  assert.equal(out.findings.length, 5); // never dropped
  const byId = Object.fromEntries(out.findings.map((f) => [f.id, f]));
  for (const id of ['F0', 'F1', 'F4']) assert.notEqual(byId[id].origin, 'unknown', `${id} must stay verified`);
  for (const id of ['F2', 'F3']) assert.equal(byId[id].origin, 'unknown', `${id} must degrade`);
  // Output stays in strict slice-index order regardless of which slice degraded.
  assert.deepEqual(out.findings.map((f) => f.id), ['F0', 'F1', 'F2', 'F3', 'F4']);
  assert.equal(out.gaps.length, 1, 'exactly one gap, for the one degraded slice');
  assert.match(out.gaps[0], /slice 1/);
  assert.match(out.gaps[0], /2 of 5/);
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
  assert.equal(ctx.calls[0].agentType, 'code-gauntlet:artifact-writer');
  const firstExecIdx = ctx.calls.findIndex((t) => (t.label || '').startsWith('verify-slice-'));
  const writerIdx = ctx.calls.findIndex((t) => (t.label || '').startsWith('verify-input-writer'));
  assert.ok(writerIdx >= 0 && writerIdx < firstExecIdx, 'writer dispatched before executors');
  // The writer prompt carries the sliced findings by value and their target paths.
  assert.match(ctx.calls[0].prompt, /phase4-input-abc123\.slice0\.json/);
  assert.match(ctx.calls[0].prompt, /"id":"F1"/);
});

test('(k) slice-input writer failure -> that slice UNVERIFIED (here: the only slice), no executor dispatched', async () => {
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

test('(l) slice-input writer THROW -> that slice UNVERIFIED (here: the only slice), never fabricate', async () => {
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

// --- Item 2: echo content-fidelity gate -------------------------------------
// trustSlice must reject a slice whose eliminated[] entries lack the elimination_reason
// stamp run_verification() ALWAYS writes on a real elimination. Observed live: the script
// disk had 10 verified/0 eliminated, but the echo claimed 7 verified/3 eliminated with a
// valid receipt and a passing count-sum — the 3 fabricated eliminations carried no stamp.
function stampedEliminated(f) {
  return { ...f, elimination_reason: 'evidence does not match file content' };
}

test('(m1) stamped eliminations -> slice TRUSTED: verified findings threaded, verified===true', async () => {
  const input = baseInput(); // F1, F2; one slice
  const ctx = verifyCtx((_t, i) => ({
    status: 'ok',
    receipt: { sha: 'abc123', nonce: `n-1.${i}`, n_in: 2 },
    result: {
      verified: [{ ...input.findings[0], origin: 'new' }],
      eliminated: [stampedEliminated(input.findings[1])], // script-stamped real elimination
      batches: [], stats: {},
    },
  }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.equal(out.findings.length, 1); // only the verified finding is threaded onward
  assert.equal(out.findings[0].id, 'F1');
  assert.ok(out.findings.every((f) => f.origin !== 'unknown'));
  assert.equal(out.gaps.length, 0);
});

test('(m2) an UNSTAMPED elimination (fabricated verified->eliminated move) -> that slice UNVERIFIED, both attempts', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => ({
    status: 'ok',
    receipt: { sha: 'abc123', nonce: `n-1.${i}`, n_in: 2 }, // receipt + count-sum both PASS
    result: {
      verified: [{ ...input.findings[0], origin: 'new' }],
      eliminated: [{ ...input.findings[1] }], // NO elimination_reason — the script never omits it
      batches: [], stats: {},
    },
  }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 2); // conservative: BOTH originals kept, never dropped
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.ok(out.gaps.some((g) => /elimination_reason|fabricated/.test(g)));
});

test('(m3) a blank-string elimination_reason is also rejected (not a real stamp)', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => ({
    status: 'ok',
    receipt: { sha: 'abc123', nonce: `n-1.${i}`, n_in: 2 },
    result: {
      verified: [{ ...input.findings[0], origin: 'new' }],
      eliminated: [{ ...input.findings[1], elimination_reason: '   ' }],
      batches: [], stats: {},
    },
  }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
});

// --- Item 4: verify echo item schema declares agent + reconciled extras -------

test('(m4) verify echo item schema declares reconciled per-dimension extras (array types) + elimination_reason — and NOT agent', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  await verifyStage(ctx, input);
  const schema = ctx.execCalls()[0].schema;
  for (const arr of ['verified', 'eliminated']) {
    const props = schema.properties.result.properties[arr].items.properties;
    // agent is INTENTIONALLY not declared (item 4 reverted after mini-subset A): a
    // deterministic agent echo activated proximity-keyed cross-agent dedup and cost
    // -7 same-6 goldens. Re-lands only with the #17/D20 consolidation redesign.
    assert.ok(!('agent' in props), 'agent must not be declared until the D20 redesign');
    assert.equal(props.confidence.type, 'number');
    // elimination_reason must be declarable so an honest script stamp survives transcription
    // (else the item-2 fidelity gate would false-fire on real eliminations).
    assert.equal(props.elimination_reason.type, 'string');
    // Reconciled per-dimension extras (union across all agents), matching the .md contracts:
    assert.equal(props.hidden_errors.type, 'string', 'bug -> hidden_errors');
    assert.equal(props.attack_vector.type, 'string', 'security -> attack_vector');
    assert.equal(props.invalid_state_example.type, 'string', 'type_design -> invalid_state_example');
    assert.equal(props.behavior_preserved.type, 'string', 'simplification -> behavior_preserved');
    // cross_file_impact -> affected_consumers is an ARRAY of strings (array support).
    assert.equal(props.affected_consumers.type, 'array');
    assert.equal(props.affected_consumers.items.type, 'string');
    // The pre-reconciliation phantom fields (never emitted, never consumed) are gone.
    for (const ghost of ['encapsulation', 'invariants', 'enforcement', 'usefulness', 'before', 'after']) {
      assert.ok(!(ghost in props), `phantom field ${ghost} must not be declared`);
    }
  }
});

// With `agent` undeclared in the echo schema (item-4 revert), survival is stochastic in
// production; this pins the PASS-THROUGH: when the executor does echo it, the stage
// threads it onward untouched (detectDisagreement's input when present).
test('(m5) an echoed agent field is threaded through the verify stage untouched', async () => {
  const findings = [
    { id: 'F1', file: 'a.js', line_start: 1, origin: 'new', dimension: 'bug', agent: 'bug-detector', cross_file_refs: [] },
    { id: 'F2', file: 'a.js', line_start: 2, origin: 'new', dimension: 'convention', agent: 'conventions-and-intent', cross_file_refs: [] },
  ];
  const input = baseInput({ findings });
  const ctx = verifyCtx((_t, i) => okEnvelope(findings.map((f) => ({ ...f })), { nonce: `n-1.${i}` }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.deepEqual(out.findings.map((f) => f.agent), ['bug-detector', 'conventions-and-intent']);
});

// --- Item 5: slice-input writer write-proof ---------------------------------

test('(k2) slice-input writer echo that omits a dispatched path -> that slice UNVERIFIED (no write proof)', async () => {
  const input = baseInput();
  const ctx = verifyCtx(
    () => { throw new Error('executor must not run when slice inputs were not proven written'); },
    { sliceWriter: () => ({ written: ['/unrelated/path.json'] }) }, // does not cover the dispatched slice path
  );
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 2); // preserved, never dropped
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.ok(out.gaps.some((g) => /write proof|cover/i.test(g)));
  assert.equal(ctx.execCalls().length, 0, 'no executor ran without write proof');
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
  assert.equal(t.agentType, 'code-gauntlet:executor');
});

// --- Issue #54: per-slice degradation + the single deterministic retry ------

test('(n1) a slice that fails attempt 1 but succeeds on the retry recovers cleanly: verified===true, zero findings degraded, disclosure gap without UNVERIFIED', async () => {
  const findings = Array.from({ length: 5 }, (_, i) => ({ id: `F${i}`, origin: 'new' }));
  const input = baseInput({ findings, limits: { verifySliceSize: 2 } });
  const slices = [];
  for (let k = 0; k < findings.length; k += 2) slices.push(findings.slice(k, k + 2));
  // Slice 1's FIRST dispatch fails; its retry succeeds (with the retry's distinct nonce).
  // Slices 0 and 2 are trusted on their one and only dispatch.
  const ctx = verifyCtx((_t, i, { attempt }) => {
    if (i === 1 && attempt === 1) return { status: 'failed', exitCode: 1, stderr: 'transient' };
    const nonce = i === 1 && attempt === 2 ? `n-1.${i}.r1` : `n-1.${i}`;
    return okEnvelope(slices[i], { nonce, n_in: slices[i].length });
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.equal(out.findings.length, 5);
  assert.ok(out.findings.every((f) => f.origin !== 'unknown'), 'a recovered slice degrades nothing');
  assert.equal(ctx.execCallsFor(1).length, 2, 'slice 1: first attempt + retry');
  assert.deepEqual(ctx.execCallsFor(1).map((t) => t.label), ['verify-slice-1', 'verify-slice-1-retry']);
  assert.equal(ctx.execCallsFor(0).length, 1, 'slice 0: never retried');
  assert.equal(ctx.execCallsFor(2).length, 1, 'slice 2: never retried');
  assert.equal(out.gaps.length, 1);
  assert.match(out.gaps[0], /verify-slice-retry/);
  assert.doesNotMatch(out.gaps[0], /UNVERIFIED/, 'a recovered slice discloses, it does not degrade');
});

test('(n2) a slice failing every attempt gets EXACTLY VERIFY_ATTEMPTS_PER_SLICE (2) dispatches, never a second retry', async () => {
  const findings = Array.from({ length: 5 }, (_, i) => ({ id: `F${i}`, origin: 'new' }));
  const input = baseInput({ findings, limits: { verifySliceSize: 2 } });
  const slices = [];
  for (let k = 0; k < findings.length; k += 2) slices.push(findings.slice(k, k + 2));
  const ctx = verifyCtx((_t, i) => {
    if (i === 1) return { status: 'failed', exitCode: 1, stderr: 'boom' }; // fails on every attempt
    return okEnvelope(slices[i], { nonce: `n-1.${i}`, n_in: slices[i].length });
  });
  await verifyStage(ctx, input);
  const slice1Calls = ctx.execCallsFor(1);
  // Asserted against the EXPORTED constant, not a copied literal: raising
  // VERIFY_ATTEMPTS_PER_SLICE without teaching verifySliceWithRetry to use it fails here
  // rather than silently leaving worstCaseAgentCount over-counting the real fan-out.
  assert.equal(VERIFY_ATTEMPTS_PER_SLICE, 2, 'the retry budget this file pins its labels to');
  assert.equal(slice1Calls.length, VERIFY_ATTEMPTS_PER_SLICE);
  assert.deepEqual(slice1Calls.map((t) => t.label), ['verify-slice-1', 'verify-slice-1-retry']);
});

test('(n3) the retry nonce is distinct: a replay of attempt 1\'s receipt does not satisfy the retry', async () => {
  const findings = Array.from({ length: 4 }, (_, i) => ({ id: `F${i}`, origin: 'new' }));
  const input = baseInput({ findings, limits: { verifySliceSize: 2 } });
  const slices = [findings.slice(0, 2), findings.slice(2, 4)];
  const commands = [];
  const ctx = verifyCtx((t, i, { attempt }) => {
    if (i === 0) return okEnvelope(slices[0], { nonce: 'n-1.0', n_in: 2 });
    commands.push(t.prompt); // slice 1's commands, in dispatch order: [attempt 1, retry]
    if (attempt === 1) return { status: 'failed', exitCode: 1, stderr: 'boom' };
    // The retry: a confused/replaying executor echoes attempt 1's nonce (n-1.1) instead of
    // the expected retry nonce (n-1.1.r1). trustSlice must reject this, not accept it.
    return okEnvelope(slices[1], { nonce: 'n-1.1', n_in: 2 });
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false); // slice 1's retry rejected -> slice 1 degrades
  const byId = Object.fromEntries(out.findings.map((f) => [f.id, f]));
  assert.notEqual(byId.F0.origin, 'unknown');
  assert.equal(byId.F2.origin, 'unknown');
  assert.match(commands[0], /--nonce n-1\.1(\s|$)/, 'attempt 1 embeds the slice nonce');
  assert.match(commands[1], /--nonce n-1\.1\.r1(\s|$)/, 'the retry embeds a DISTINCT nonce');
});

// (n4) "a trusted slice is never re-dispatched" is already covered by test (g) above:
// every slice there is trusted on its first answer and execCalls().length===slices.length
// (no extra retry dispatches). Not duplicated here.

test('(n5) the loop CONTINUES past a failed slice: later slices are still dispatched', async () => {
  const findings = Array.from({ length: 5 }, (_, i) => ({ id: `F${i}`, origin: 'new' }));
  const input = baseInput({ findings, limits: { verifySliceSize: 2 } });
  const slices = [];
  for (let k = 0; k < findings.length; k += 2) slices.push(findings.slice(k, k + 2));
  const ctx = verifyCtx((_t, i) => {
    if (i === 0) return { status: 'failed', exitCode: 1, stderr: 'boom' }; // fails on every attempt
    return okEnvelope(slices[i], { nonce: `n-1.${i}`, n_in: slices[i].length });
  });
  await verifyStage(ctx, input);
  const labels = ctx.calls.map((c) => c.label);
  assert.ok(labels.includes('verify-slice-1'), 'slice 1 still dispatched after slice 0 failed');
  assert.ok(labels.includes('verify-slice-2'), 'slice 2 still dispatched after slice 0 failed');
});

test('(n6) an executor THROW on one slice degrades only that slice; the gap names both attempts', async () => {
  const findings = Array.from({ length: 5 }, (_, i) => ({ id: `F${i}`, origin: 'new' }));
  const input = baseInput({ findings, limits: { verifySliceSize: 2 } });
  const slices = [];
  for (let k = 0; k < findings.length; k += 2) slices.push(findings.slice(k, k + 2));
  const ctx = verifyCtx((_t, i) => {
    if (i === 2) throw new Error('schema-retry exhausted'); // throws on every attempt
    return okEnvelope(slices[i], { nonce: `n-1.${i}`, n_in: slices[i].length });
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  const byId = Object.fromEntries(out.findings.map((f) => [f.id, f]));
  assert.notEqual(byId.F0.origin, 'unknown'); // slice 0, unaffected
  assert.notEqual(byId.F2.origin, 'unknown'); // slice 1, unaffected
  assert.equal(byId.F4.origin, 'unknown'); // slice 2, the one that threw
  assert.equal(out.gaps.length, 1);
  assert.match(out.gaps[0], /retried once after the first attempt failed/);
});

// Force slice 0's --input entry into its OWN writer group: a description well over the
// 100_000-char SEGMENT_CHAR_BUDGET on its own means chunkBySerializedSize (greedy packer)
// closes group 0 right after slice 0 and starts group 1 with every other slice — the same
// technique stages_latency.test.js's bigVerifyFindings uses to force >1 writer group.
function verifyFindingsWithOversizedFirst(n) {
  const first = {
    id: 'F0', file: 'a.js', line_start: 1, origin: 'new', dimension: 'bug', cross_file_refs: [],
    description: 'd'.repeat(150000),
  };
  const rest = Array.from({ length: n - 1 }, (_, i) => ({
    id: `F${i + 1}`, file: 'a.js', line_start: i + 2, origin: 'new', dimension: 'bug', cross_file_refs: [],
  }));
  return [first, ...rest];
}

test('(n7) a slice-input writer GROUP failure degrades only the slices IT carried', async () => {
  const findings = verifyFindingsWithOversizedFirst(6);
  const input = baseInput({ findings, limits: { verifySliceSize: 1 } });
  const writerLabels = [];
  const ctx = verifyCtx(
    (_t, i) => okEnvelope([findings[i]], { nonce: `n-1.${i}`, n_in: 1 }),
    {
      sliceWriter: (call) => {
        writerLabels.push(call.label);
        const entries = parseWriterPayload(call.prompt) || [];
        const paths = entries.map((e) => e.path);
        // Fail only the group carrying slice 0's input path; echo every other group whole.
        if (paths.some((p) => p.endsWith('.slice0.json'))) return null;
        return { written: paths };
      },
    },
  );
  const out = await verifyStage(ctx, input);
  assert.ok(writerLabels.length > 1, 'the payload must chunk into more than one writer group');
  const byId = Object.fromEntries(out.findings.map((f) => [f.id, f]));
  assert.equal(byId.F0.origin, 'unknown');
  for (let i = 1; i < 6; i += 1) assert.notEqual(byId[`F${i}`].origin, 'unknown', `F${i} unaffected by group 0's failure`);
  assert.equal(ctx.execCallsFor(0).length, 0, 'slice 0 never reached the executor (no write proof)');
  assert.equal(out.gaps.length, 1);
  assert.match(out.gaps[0], /slice 0 \(slice-input group \d+\)/);
});

test('(n8) every slice degraded -> every finding origin=unknown, one gap PER slice (the old whole-set outcome is still reachable, just no longer the only one)', async () => {
  const findings = Array.from({ length: 5 }, (_, i) => ({ id: `F${i}`, origin: 'new' }));
  const input = baseInput({ findings, limits: { verifySliceSize: 2 } });
  const ctx = verifyCtx(() => ({ status: 'failed', exitCode: 1, stderr: 'boom' })); // every slice, every attempt
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 5);
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.equal(out.gaps.length, 3); // ceil(5/2) = 3 slices -> 3 gaps, one per slice
  // Gap ORDER is slice-index order, not arrival order — the whole loop is sequential, so
  // an out-of-order gap list would mean a future refactor had started collecting failures
  // off the dispatch order and made the run's own degradation record nondeterministic.
  assert.match(out.gaps[0], /slice 0:/);
  assert.match(out.gaps[1], /slice 1:/);
  assert.match(out.gaps[2], /slice 2:/);
  // Blast radius is per slice: 2 + 2 + 1 of 5, never "all 5" three times over.
  assert.deepEqual(out.gaps.map((g) => (g.match(/(\d+) of (\d+) finding/) || []).slice(1, 3)), [['2', '5'], ['2', '5'], ['1', '5']]);
});
