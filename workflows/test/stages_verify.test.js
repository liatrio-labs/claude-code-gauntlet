// stages_verify.test.js — orchestration-contract tests for the Verify stage.
// verifyStage dispatches the `executor` agent (one call per verifySliceSize slice),
// SEQUENTIALLY, and trusts a slice's result ONLY when status==='ok', the receipt echoes
// the dispatched nonce/head-sha/n_in, the echoed `result.deltas` cover EXACTLY the ids
// this slice dispatched (no missing, no duplicate, no stranger), and their content proof
// (fnv1a32 over the canonical rebuild) matches receipt.deltas_checksum (trustSlice).
// Under the delta echo (issue #25 PR2) the executor no longer echoes findings at all —
// only a per-id DELTA of what verify_findings.py decided (origin/severity/confidence/
// elimination_reason) — so the verified findings are rebuilt HERE, by joining that delta
// onto the findings this stage already dispatched (joinVerifyDeltas), which also
// unconditionally strips `agent` from every joined finding.
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
import { verifyStage, parseWriterPayload, VERIFY_ATTEMPTS_PER_SLICE, VERIFY_SLICE_FIELDS } from '../src/stages.js';
import { assertPrompt, assertValidSchema } from './helpers/pipelineMock.js';
import { deltaEnvelope, deltasFor, ELIMINATION_STAMP, sliceInputRecorder } from './helpers/verifyDelta.js';
import { outsideSingleQuotes, shellSplit } from './helpers/shellWords.js';
import { FINDING_PROP_TYPES } from '../src/registry.js';

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
  const rec = sliceInputRecorder();
  const agent = async (prompt, opts = {}) => {
    assertPrompt(prompt);
    assertValidSchema(opts.schema);
    const call = { prompt, ...opts };
    calls.push(call);
    const label = opts.label || '';
    if (label.startsWith('verify-input-writer')) {
      // The recorder always learns the dispatched content from the PROMPT — that is what
      // materializeVerifySlices itself checksums against, independent of what the writer
      // echoes back — so a stamped executor envelope is provable even when cfg.sliceWriter
      // overrides the write-proof RESPONSE (a test simulating a lying/failing writer whose
      // healthy sibling groups still reach the executor, e.g. (n7)).
      const faithful = rec.write(prompt);
      return cfg.sliceWriter ? cfg.sliceWriter(call) : faithful;
    }
    if (label.startsWith('verify-slice-')) {
      if (inParallel > 0) {
        throw new Error('verifyStage must use agent() per slice, not parallel()');
      }
      const m = /^verify-slice-(\d+)(-retry)?$/.exec(label);
      const sliceIndex = m ? Number(m[1]) : -1;
      const attempt = m && m[2] ? 2 : 1;
      return rec.stamp(await agentImpl(call, sliceIndex, { attempt }), sliceIndex);
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

// okEnvelope(findings, opts) — thin same-signature wrapper around the shared
// deltaEnvelope helper (verifyDelta.js). Kept under this name because ~30 call sites in
// this file already read `okEnvelope(...)`; it MUST delegate rather than re-derive the
// checksum itself — deltaEnvelope's own rationale is that a second copy of the
// canonicalisation would agree with a broken implementation just as happily as a correct
// one. By default this builds one delta per finding, each echoing that finding's own
// origin/severity/confidence (verified:true) — the shape a well-behaved script produces
// for a slice it changed nothing about. Callers pass `deltas`/`overrides`/`checksum`/`ids`
// (see deltaEnvelope's own doc comment) to drive the trust-boundary tests below.
function okEnvelope(findings, opts = {}) {
  return deltaEnvelope(findings, opts);
}

test('(a) valid ok envelope with matching receipt -> findings verified, verified===true', async () => {
  const input = baseInput();
  // Per-slice nonce: slice i must echo `${nonce}.${i}` (here slice 0 -> n-1.0). The
  // default delta (okEnvelope/deltaEnvelope) echoes each finding's own origin, so this is
  // exactly the shape a script produces when it changes nothing about the slice.
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.equal(out.findings.length, 2);
  assert.equal(out.gaps.length, 0);
  assert.ok(out.findings.every((f) => f.origin !== 'unknown'));
  // cross_file_refs survives verbatim (surfaced-classification depends on it downstream).
  // This used to test that the executor's echo carried it through faithfully; now it is
  // guaranteed BY CONSTRUCTION rather than by transcription — joinVerifyDeltas enriches the
  // finding this stage already dispatched, and the delta echo has no cross_file_refs field
  // at all for an executor to drop or mangle.
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
  // Discovery-shaped findings: the schema now declares confidence a NUMBER, so "85"
  // no longer arrives from a live dispatch, but the pin is defence-in-depth for
  // legacy/checkpoint-resume findings that predate that schema pin. line_start gets
  // the same treatment. Non-numeric values must pass through untouched.
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

test('(e2) delta coverage: an echo missing one dispatched id\'s delta -> UNVERIFIED (replaces the old verified+eliminated count-sum guard)', async () => {
  const input = baseInput();
  // nonce/sha/n_in all match, but the deltas array covers only F1 — F2's delta never
  // arrived. The old count guard (verified.length + eliminated.length === n_in) cannot
  // exist any more now that findings themselves are never echoed; #25 req 2's replacement
  // is an exact id-coverage check (trustSlice), which is what this pins. Without it a
  // finding whose delta never arrived would silently vanish.
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, {
    nonce: `n-1.${i}`,
    deltas: deltasFor([input.findings[0]]), // F2's delta is missing entirely
  }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, input.findings.length); // originals preserved
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.ok(out.gaps.some((g) => /does not cover/i.test(g)));
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

// Issue #72: verifySliceSize is not floored (a tiny slice mitigates a transcription-
// fidelity failure), so a caller who sets it small on a large finding set gets a
// disclosure instead of a silent, expensive fan-out.
test('verify_fanout gap fires once, above threshold, naming size/slices/dispatch-ceiling; absent below threshold', async () => {
  const under = Array.from({ length: 5 }, (_, i) => ({ id: `F${i}`, origin: 'new', cross_file_refs: [] }));
  const underInput = baseInput({ findings: under, limits: { verifySliceSize: 1 } }); // 5 slices == threshold, not above
  const underCtx = verifyCtx((_t, i) => okEnvelope([under[i]], { nonce: `n-1.${i}`, n_in: 1 }));
  const underOut = await verifyStage(underCtx, underInput);
  assert.ok(!underOut.gaps.some((g) => g.startsWith('verify_fanout:')), 'exactly-at-threshold does not disclose');

  const over = Array.from({ length: 6 }, (_, i) => ({ id: `F${i}`, origin: 'new', cross_file_refs: [] }));
  const overInput = baseInput({ findings: over, limits: { verifySliceSize: 1 } }); // 6 slices > threshold
  const overCtx = verifyCtx((_t, i) => okEnvelope([over[i]], { nonce: `n-1.${i}`, n_in: 1 }));
  const overOut = await verifyStage(overCtx, overInput);
  const fanoutGaps = overOut.gaps.filter((g) => g.startsWith('verify_fanout:'));
  assert.equal(fanoutGaps.length, 1, 'fires exactly once per run, not once per slice');
  assert.match(fanoutGaps[0], /verifySliceSize=1/);
  assert.match(fanoutGaps[0], /splits 6 finding\(s\) into 6 slices/);
  assert.match(fanoutGaps[0], /up to 12 executor dispatches/); // 6 slices * VERIFY_ATTEMPTS_PER_SLICE(2)
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

test('(h1) a null finding must not crash materializeVerifySlices — it degrades honestly via dispatchableIds, never throws (issue #50b regression)', async () => {
  const input = baseInput({ findings: [null] });
  // The executor must never be dispatched: dispatchableIds degrades the slice (no usable
  // id on a null finding) before verifySliceWithRetry is ever called.
  const ctx = verifyCtx(() => {
    throw new Error('executor must never be dispatched for a slice with no usable id');
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 1); // preserved, never dropped
  assert.equal(out.findings[0].origin, 'unknown');
  assert.equal(ctx.execCalls().length, 0);
  assert.ok(out.gaps.some((g) => /no usable id/.test(g)));
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

// (j2)-(j4): the slice-input projection (issue #50b). The slice-input file is narrowed to
// VERIFY_SLICE_FIELDS — every other finding field is dropped before the artifact-writer
// ever sees it. Nothing downstream loses it: joinVerifyDeltas rebuilds every verified
// finding from the workflow's OWN in-memory copy of the dispatched finding, never from
// this projection (see workflows/AGENTS.md, "The verify boundary").
test('(j2) the slice-input projection drops every field outside VERIFY_SLICE_FIELDS, keeping the rest in list order', async () => {
  const input = baseInput();
  input.findings = [{
    id: 'F1', file: 'a.js', line_start: 1, line_end: 3, description: 'desc',
    evidence: 'ev', severity: 'high', confidence: 90, cross_file_refs: ['b.js:9'],
    origin: 'new',
    // Fields VERIFY_SLICE_FIELDS does not carry — verify_findings.py never reads these.
    agent: 'security', title: 'title text', dimension: 'security',
    suggestion: 'do X instead', claude_md_rule: 'rule-1', criticality: 8,
  }];
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  await verifyStage(ctx, input);
  const entries = parseWriterPayload(ctx.calls[0].prompt);
  const written = entries[0].content.findings[0];
  assert.deepEqual(Object.keys(written), VERIFY_SLICE_FIELDS, 'exactly the projected keys, in VERIFY_SLICE_FIELDS order');
  for (const dropped of ['agent', 'title', 'dimension', 'suggestion', 'claude_md_rule', 'criticality']) {
    assert.ok(!Object.hasOwn(written, dropped), `${dropped} must not reach the slice-input file`);
  }
});

test('(j3) a field absent from the dispatched finding stays absent in the slice input — never written as null', async () => {
  const input = baseInput();
  // No evidence, no cross_file_refs, no line_end: a discovery finding before verification
  // fills those in is a realistic shape, not a contrived one.
  input.findings = [{ id: 'F1', file: 'a.js', line_start: 1, description: 'd', severity: 'high', confidence: 50, origin: 'new' }];
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  await verifyStage(ctx, input);
  const entries = parseWriterPayload(ctx.calls[0].prompt);
  const written = entries[0].content.findings[0];
  for (const absent of ['evidence', 'cross_file_refs', 'line_end']) {
    assert.ok(!Object.hasOwn(written, absent), `${absent} was never dispatched, so it must not appear at all`);
    assert.notEqual(written[absent], null, `${absent} must be omitted, not written as null`);
  }
  assert.deepEqual(Object.keys(written), ['id', 'file', 'line_start', 'description', 'severity', 'confidence', 'origin']);
});

test('(j4) VERIFY_SLICE_FIELDS is a subset of the closed finding schema (registry.js FINDING_PROP_TYPES)', () => {
  // Every field verify_findings.py reads must be a real, declared finding field — a typo
  // or a stale entry here would silently project nothing for that key on every run.
  for (const field of VERIFY_SLICE_FIELDS) {
    assert.ok(Object.hasOwn(FINDING_PROP_TYPES, field), `${field} is not declared in FINDING_PROP_TYPES`);
  }
});

test('(j5) the projection still applies pinNumericFields: a fractional line_start/confidence is half-up rounded before it reaches the slice-input file', async () => {
  const input = baseInput();
  input.findings = [{ id: 'F1', file: 'a.js', line_start: 3.5, description: 'd', severity: 'high', confidence: 90.2, origin: 'new' }];
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  await verifyStage(ctx, input);
  const entries = parseWriterPayload(ctx.calls[0].prompt);
  const written = entries[0].content.findings[0];
  assert.equal(written.line_start, 4, 'Math.floor(3.5 + 0.5) === 4, matching pinNumericFields elsewhere');
  assert.equal(written.confidence, 90, 'Math.floor(90.2 + 0.5) === 90');
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
// trustSlice must reject a slice whose delta carries verified:false without the
// elimination_reason stamp run_verification() ALWAYS writes on a real elimination.
// Observed live (same underlying script behavior the by-value design also had to guard):
// the script disk had 10 verified/0 eliminated, but the echo claimed 7 verified/3
// eliminated with a valid receipt and a passing count check — the 3 fabricated
// eliminations carried no stamp. Under the delta echo the identical fault is expressed as
// a verified:false delta with no stamp; ELIMINATION_STAMP (verifyDelta.js) is the exact
// string the script writes, so fixtures here stay honest about what it actually emits.

test('(m1) a verified:false delta carrying the elimination stamp -> slice TRUSTED: only the verified finding threaded, verified===true', async () => {
  const input = baseInput(); // F1, F2; one slice
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, {
    nonce: `n-1.${i}`,
    overrides: { F2: { verified: false, elimination_reason: ELIMINATION_STAMP } }, // script-stamped real elimination
  }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.equal(out.findings.length, 1); // only the verified finding is threaded onward
  assert.equal(out.findings[0].id, 'F1');
  assert.ok(out.findings.every((f) => f.origin !== 'unknown'));
  assert.equal(out.gaps.length, 0);
});

test('(m2) a verified:false delta with NO elimination_reason stamp (fabricated elimination) -> that slice UNVERIFIED, both attempts', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, {
    nonce: `n-1.${i}`, // receipt + id-coverage both PASS
    overrides: { F2: { verified: false } }, // NO elimination_reason — the script never omits it
  }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 2); // conservative: BOTH originals kept, never dropped
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.ok(out.gaps.some((g) => /elimination_reason|fabricated/.test(g)));
});

test('(m3) a blank-string elimination_reason is also rejected (not a real stamp)', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, {
    nonce: `n-1.${i}`,
    overrides: { F2: { verified: false, elimination_reason: '   ' } },
  }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
});

// --- Item 4: verify echo item schema declares agent + reconciled extras -------
// (superseded by the #25 PR2 delta echo) The by-value item schema used to union every
// per-dimension extra across all agents so a verified finding's shape survived
// transcription. The delta echo removes that need entirely: no finding is echoed at all
// any more, so VERIFY_SCHEMA's result.deltas item is deliberately just the six flat
// DELTA_KEYS scalars and nothing else — there is no per-dimension extra left to declare
// or drop. This test now pins that narrowness plus the still-live `agent` withholding
// (item 4's original point, now structural rather than a schema omission).

test('(m4) verify echo item schema is exactly the six delta scalars — no per-dimension extras, and NOT agent', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  await verifyStage(ctx, input);
  const schema = ctx.execCalls()[0].schema;
  const props = schema.properties.result.properties.deltas.items.properties;
  // agent is INTENTIONALLY not declared — no finding is echoed any more, only a per-id
  // decision, so there is nothing left for a deterministic agent echo to ride on. That
  // used to matter as a schema omission (item 4 reverted after mini-subset A: a
  // deterministic agent echo activated proximity-keyed cross-agent dedup and cost -7
  // same-6 goldens); it is now unreachable by construction. Re-lands only with the
  // #17/D20 consolidation redesign.
  assert.ok(!('agent' in props), 'agent must not be declared until the D20 redesign');
  assert.equal(props.id.type, 'string');
  assert.equal(props.verified.type, 'boolean');
  assert.equal(props.origin.type, 'string');
  assert.equal(props.severity.type, 'string');
  assert.equal(props.confidence.type, 'number');
  // elimination_reason must be declarable so an honest script stamp survives transcription
  // (else the item-2 fidelity gate would false-fire on real eliminations).
  assert.equal(props.elimination_reason.type, 'string');
  // The per-dimension extras (union across all agents, e.g. hidden_errors/attack_vector)
  // and the pre-reconciliation phantom fields are BOTH gone: the delta item has nothing to
  // reconstruct a finding from, by construction, not by a field-by-field allowlist.
  for (const ghost of [
    'hidden_errors', 'attack_vector', 'affected_consumers', 'invalid_state_example', 'behavior_preserved',
    'encapsulation', 'invariants', 'enforcement', 'usefulness', 'before', 'after',
  ]) {
    assert.ok(!(ghost in props), `field ${ghost} must not be declared on the delta item`);
  }
});

// The by-value design left `agent` undeclared in the echo schema and observed its
// survival as stochastic (item 4's original PASS-THROUGH pin). Under the delta echo the
// executor never receives or returns a finding at all — only a per-id decision — so
// joinVerifyDeltas is the one place `agent` can be threaded onward, and it unconditionally
// deletes it (issue #25 requirement 1: a filter-visible `agent` past verify is a defect,
// not a stochastic outcome). This test now pins the opposite of what it used to: `agent`
// is stripped, always, regardless of what (if anything) the delta says about that id.
test('(m5) `agent` never survives verify: a dispatched finding carrying it comes out stripped', async () => {
  const findings = [
    { id: 'F1', file: 'a.js', line_start: 1, origin: 'new', dimension: 'bug', agent: 'bug-detector', cross_file_refs: [] },
    { id: 'F2', file: 'a.js', line_start: 2, origin: 'new', dimension: 'convention', agent: 'conventions-and-intent', cross_file_refs: [] },
  ];
  const input = baseInput({ findings });
  const ctx = verifyCtx((_t, i) => okEnvelope(findings, { nonce: `n-1.${i}` }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.ok(out.findings.every((f) => !('agent' in f)), 'agent must be stripped from every joined finding');
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

// --- Issue #75: the pinned command's tokens are shell WORDS, not space-joined ----------
//
// verifyCommand builds argv and hands it to the executor as one string that Bash splits.
// A path holding a space used to split into two arguments there — `--diff-file /My
// Documents/d.patch` reached verify_findings.py as `--diff-file /My`, and the slice
// degraded on an input the caller spelled correctly. Each token is now shellWord-quoted,
// so these tests assert on the ARGV a shell would build (shellSplit), never on substrings
// of the command: a substring match cannot tell one word from two.

// The command dispatched for slice `i` is the last line of the executor prompt.
const commandOf = (call) => call.prompt.split('\n').pop();

async function dispatchedCommand(input) {
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  await verifyStage(ctx, input);
  return commandOf(ctx.execCalls()[0]);
}

test('(#75) ordinary paths stay BYTE-IDENTICAL to the unquoted join — quoting costs nothing by default', async () => {
  // Every real fixture ({output_dir} paths, hex nonces, --flags, short SHAs, ordinary
  // branch names) matches the bare charset, so the shipped command must not change at
  // all. Pinned as an exact string: a regex would not notice a stray pair of quotes.
  assert.equal(
    await dispatchedCommand(baseInput()),
    'python3 /plugin/scripts/verify_findings.py'
    + ' --input /out/phase4-input-abc123.slice0.json'
    + ' --output /out/phase4-output-abc123.slice0.json'
    + ' --nonce n-1.0 --head-sha abc123 --base-branch main'
    + ' --diff-file /out/code-gauntlet-diff-abc123.patch',
  );
});

test('(#75) a space in ANY path field still names ONE file in the executor argv', async () => {
  const verify = {
    scriptPath: '/plug in/scripts/verify_findings.py',
    inputPathBase: '/My Documents/out/phase4-input-abc123',
    outputPathBase: '/My Documents/out/phase4-output-abc123',
    baseBranch: 'main',
    diffPath: '/My Documents/out/code-gauntlet diff.patch',
  };
  const cmd = await dispatchedCommand(baseInput({ verify }));
  assert.deepEqual(shellSplit(cmd), [
    'python3', verify.scriptPath,
    '--input', '/My Documents/out/phase4-input-abc123.slice0.json',
    '--output', '/My Documents/out/phase4-output-abc123.slice0.json',
    '--nonce', 'n-1.0',
    '--head-sha', 'abc123',
    '--base-branch', 'main',
    '--diff-file', verify.diffPath,
  ]);
});

test("(#75) a single quote inside a path round-trips through the '\\'' escape", async () => {
  const diffPath = "/Users/o'brien/out/code-gauntlet diff.patch";
  const cmd = await dispatchedCommand(baseInput({ verify: { ...baseInput().verify, diffPath } }));
  const argv = shellSplit(cmd);
  assert.equal(argv[argv.length - 1], diffPath);
  assert.equal(argv[argv.length - 2], '--diff-file');
});

test('(#75) a $ or backtick in baseBranch is quoted, never live shell syntax', async () => {
  // git refnames forbid a space but PERMIT `$` and a backtick, so `feature/$x` is a legal
  // branch that must reach the script literally — and must not reach the shell at all.
  const baseBranch = 'feature/$x-`y`';
  const cmd = await dispatchedCommand(baseInput({ verify: { ...baseInput().verify, baseBranch } }));
  assert.deepEqual(shellSplit(cmd).slice(-4, -2), ['--base-branch', baseBranch]);
  assert.doesNotMatch(outsideSingleQuotes(cmd), /[$`]/, `expansion escaped its quotes: ${cmd}`);
});

test('(#75) an apostrophe path and a $ branch in the SAME command: neither escape leaks live syntax', async () => {
  // The two escapes interact: the `'\''` a quoted path emits ends and reopens quoted runs,
  // so a scanner that mis-pairs quotes reads the branch's `$` as live (or throws) even
  // though both tokens are correctly quoted. Realistic together — /Users/o'brien plus a
  // legal `feature/$x` refname.
  const verify = {
    ...baseInput().verify,
    diffPath: "/Users/o'brien/out/code-gauntlet diff.patch",
    baseBranch: 'feature/$x-`y`',
  };
  const cmd = await dispatchedCommand(baseInput({ verify }));
  assert.deepEqual(shellSplit(cmd).slice(-4), [
    '--base-branch', verify.baseBranch, '--diff-file', verify.diffPath,
  ]);
  assert.doesNotMatch(outsideSingleQuotes(cmd), /[$`]/, `expansion escaped its quotes: ${cmd}`);
});

test('(#75) an absent head SHA contributes an empty token, never the word "undefined"', async () => {
  // Array.join stringifies null/undefined to '' — shellWord must keep that exact
  // semantics, or a missing optional field starts passing a literal `undefined` to argv.
  for (const headShaShort of [undefined, null, '']) {
    const cmd = await dispatchedCommand(baseInput({ headShaShort }));
    assert.ok(cmd.includes('--head-sha  --base-branch main'), `${headShaShort}: ${cmd}`);
    assert.ok(!cmd.includes('undefined') && !cmd.includes('null'), cmd);
  }
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
  // 6 findings at verifySliceSize:1 -> 6 slices, above VERIFY_FANOUT_DISCLOSE_THRESHOLD
  // (issue #72), so a fan-out disclosure gap leads the one per-slice degrade gap.
  assert.equal(out.gaps.length, 2);
  assert.match(out.gaps[0], /^verify_fanout: verifySliceSize=1 splits 6 finding\(s\) into 6 slices/);
  assert.match(out.gaps[1], /slice 0 \(slice-input group \d+\)/);
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

// --- issue #69 / #25 req 4-6: the slice-input content proof --------------------

test('(p1) a receipt whose input_checksum matches the dispatched content -> TRUSTED, counted proven', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.deepEqual(out.inputProof, { slices: 1, proven: 1, recovered: 0, mismatched: 0, missing: 0, unprovable: 0 });
});

test('(p2) a receipt with NO input_checksum -> UNVERIFIED after the retry, counted missing', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i, { attempt }) => {
    const env = okEnvelope(input.findings, { nonce: attempt === 2 ? `n-1.${i}.r1` : `n-1.${i}` });
    env.receipt.input_checksum = null; // declared, so the harness stamp leaves it alone
    return env;
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.ok(out.gaps.some((g) => g.includes('input content proof missing from receipt')));
  assert.equal(out.inputProof.missing, 1);
  // A dropped field is a sampled-agent transcription failure, so it IS retried.
  assert.equal(ctx.execCallsFor(0).length, VERIFY_ATTEMPTS_PER_SLICE);
});

test('(p2b) a receipt missing input_checksum on attempt 1 but valid (with input_recovery) on the retry -> TRUSTED, verified true, recovery forwarded, counted recovered', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i, { attempt }) => {
    const env = okEnvelope(input.findings, { nonce: attempt === 2 ? `n-1.${i}.r1` : `n-1.${i}` });
    if (attempt === 1) {
      // Attempt 1 drops the checksum entirely -- the retryable "missing" fault, not the
      // deterministic "mismatch" fault probed by (p3).
      env.receipt.input_checksum = null;
    } else {
      // Attempt 2 is a fresh, valid sample: leave input_checksum undeclared so the
      // recorder's rec.stamp fills in the checksum of what was actually dispatched, and
      // additionally carry input_recovery to prove that field is forwarded on a
      // retry-success path too, not just on a first-attempt trusted slice (p4).
      env.input_recovery = { trailing_bytes: '}\n' };
    }
    return env;
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true, 'the retry recovers cleanly -- a missing checksum on attempt 1 does not sink the slice');
  assert.equal(ctx.execCallsFor(0).length, VERIFY_ATTEMPTS_PER_SLICE, 'missing input_checksum is retryable, so the retry is spent');
  assert.ok(out.findings.every((f) => f.origin !== 'unknown'), 'the recovered slice is trusted, not degraded');
  const gap = out.gaps.find((g) => g.startsWith('verify: RECOVERED —'));
  assert.ok(gap, 'the retry-success recovery is disclosed exactly like a first-attempt recovery');
  assert.deepEqual(out.inputProof, { slices: 1, proven: 0, recovered: 1, mismatched: 0, missing: 0, unprovable: 0 });
});

test('(p3) a receipt whose input_checksum disagrees -> UNVERIFIED, NOT retried (deterministic), counted mismatched', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => {
    const env = okEnvelope(input.findings, { nonce: `n-1.${i}` });
    env.receipt.input_checksum = 'fnv1a32:0xdeadbeef';
    return env;
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  const gap = out.gaps.find((g) => g.includes('slice-input content proof mismatch'));
  assert.ok(gap, 'the gap names the fault');
  assert.ok(gap.includes('fnv1a32:0xdeadbeef'), 'the gap names the receipt value');
  assert.equal(out.inputProof.mismatched, 1);
  // The file on disk is what it is: a second dispatch re-reads the same bytes, so the
  // retry cannot change the answer and is not spent.
  assert.equal(ctx.execCallsFor(0).length, 1);
});

test('(p4) a TRUSTED slice whose envelope carries input_recovery -> RECOVERED disclosure, findings kept verified', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => {
    const env = okEnvelope(input.findings, { nonce: `n-1.${i}` });
    env.input_recovery = { trailing_bytes: '}\n' };
    return env;
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true, 'a recovered input does not degrade the slice');
  const gap = out.gaps.find((g) => g.startsWith('verify: RECOVERED —'));
  assert.ok(gap, 'the recovery is disclosed');
  assert.ok(gap.includes(JSON.stringify('}\n')), 'the exact trailing bytes are named');
  assert.ok(gap.includes('proven against dispatch'), 'a proven slice may claim the proof');
  // The bench checker's degrade sentinels must NOT match a disclosure that degraded
  // nothing (bench/runner/check.py _DEGRADE_RE), and neither must the UNVERIFIED token.
  for (const token of ['no write proof', 'partial-artifacts', 'path-escape', 'UNVERIFIED']) {
    assert.ok(!gap.includes(token), `RECOVERED gap must not contain ${token}`);
  }
  assert.deepEqual(out.inputProof, { slices: 1, proven: 0, recovered: 1, mismatched: 0, missing: 0, unprovable: 0 });
});

test('(p5) a slice carrying a number Python cannot spell has no computable proof -> trusted, counted unprovable', async () => {
  // line_start IS in VERIFY_NUMERIC_FIELDS, so pinNumericFields (inside
  // projectVerifySliceFinding) does touch it — but it only ROUNDS a finite,
  // non-integer value; a value that is already integral is left exactly as large as it
  // arrived, even outside JS's safe integer range. js_stringify_pretty REFUSES such a
  // number (assemble_artifacts.assert_js_reproducible), so no cross-runtime proof
  // exists. Skipping the check keeps every other guard and costs nothing; degrading
  // here would repeat the exact bug #69 exists to close.
  const input = baseInput();
  input.findings = input.findings.map((f) => ({ ...f, line_start: Number.MAX_SAFE_INTEGER + 10 }));
  const ctx = verifyCtx((_t, i) => {
    const env = okEnvelope(input.findings, { nonce: `n-1.${i}` });
    env.receipt.input_checksum = null;
    return env;
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.deepEqual(out.inputProof, { slices: 1, proven: 0, recovered: 0, mismatched: 0, missing: 0, unprovable: 1 });
});

test('(p6) the empty finding set still reports zero-populated inputProof counters', async () => {
  const ctx = verifyCtx(() => { throw new Error('no executor should run'); });
  const out = await verifyStage(ctx, { ...baseInput(), findings: [] });
  assert.deepEqual(out.inputProof, { slices: 0, proven: 0, recovered: 0, mismatched: 0, missing: 0, unprovable: 0 });
});

test('(p8) a slice that is both unprovable and recovered does not claim a proof it never had', async () => {
  // The RECOVERED disclosure's closing clause states only what was established. On an
  // unprovable slice (no cross-runtime checksum exists) claiming "proven against
  // dispatch" would be a false measurement in a delivered disclosure — the exact
  // overstatement class this repo rejects in report prose.
  const input = baseInput();
  input.findings = input.findings.map((f) => ({ ...f, line_start: Number.MAX_SAFE_INTEGER + 10 }));
  const ctx = verifyCtx((_t, i) => {
    const env = okEnvelope(input.findings, { nonce: `n-1.${i}` });
    env.receipt.input_checksum = null; // declared: the harness stamp leaves it alone
    env.input_recovery = { trailing_bytes: '}\n' };
    return env;
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  const gap = out.gaps.find((g) => g.startsWith('verify: RECOVERED —'));
  assert.ok(gap, 'the recovery is still disclosed');
  assert.ok(!gap.includes('proven against dispatch'), 'no proof claim without a proof');
  assert.ok(gap.includes('no cross-runtime proof was computable'), 'the honest clause is stated');
  assert.deepEqual(out.inputProof, { slices: 1, proven: 0, recovered: 0, mismatched: 0, missing: 0, unprovable: 1 });
});

test('(p7) the echo schema and the executor prompt both name input_checksum and input_recovery', async () => {
  // A stale prompt that does not name a field silently drops it on EVERY slice, and a
  // schema that does not declare it drops it at the StructuredOutput boundary. Both
  // halves are pinned here because either alone is a silent total loss of the proof.
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  await verifyStage(ctx, input);
  const call = ctx.execCalls()[0];
  assert.ok(call.prompt.includes('input_checksum'), 'the prompt asks for input_checksum');
  assert.ok(call.prompt.includes('input_recovery'), 'the prompt asks for input_recovery');
  assert.ok(call.schema.properties.receipt.properties.input_checksum, 'schema declares receipt.input_checksum');
  assert.ok(call.schema.properties.input_recovery, 'schema declares input_recovery');
  // Optional in the schema so an absent recovery is a legal answer, mandatory-in-logic
  // for the checksum (trustSlice), exactly like deltas_checksum.
  assert.deepEqual(call.schema.required, ['status']);
  assert.ok(!('required' in call.schema.properties.receipt), 'receipt declares no required list');
  // The object's PRESENCE is optional (no top-level `required`), but once it is present,
  // `trailing_bytes` is not — a schema-legal `{}` must not be an answerable shape.
  assert.deepEqual(call.schema.properties.input_recovery.required, ['trailing_bytes']);
});

// --- code review fix round 1: input_recovery shape + ledger disjointness -------

test('(p9) a schema-legal empty input_recovery ({}) is not a recovery: no RECOVERED gap, counted proven, no "undefined" ever appears', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => {
    const env = okEnvelope(input.findings, { nonce: `n-1.${i}` });
    env.input_recovery = {}; // schema-legal (no top-level `required`), but unusable
    return env;
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.equal(out.gaps.length, 0, 'an unusable recovery is silently not a disclosure');
  assert.ok(!out.gaps.some((g) => g.includes('undefined')), 'the literal word "undefined" never appears in a gap');
  assert.deepEqual(out.inputProof, { slices: 1, proven: 1, recovered: 0, mismatched: 0, missing: 0, unprovable: 0 });
});

test('(p10) an input_recovery with a non-string trailing_bytes is also not a recovery: same outcome as {}', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_t, i) => {
    const env = okEnvelope(input.findings, { nonce: `n-1.${i}` });
    env.input_recovery = { trailing_bytes: 42 }; // wrong type: not a usable value
    return env;
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.equal(out.gaps.length, 0);
  assert.ok(!out.gaps.some((g) => g.includes('undefined')));
  assert.deepEqual(out.inputProof, { slices: 1, proven: 1, recovered: 0, mismatched: 0, missing: 0, unprovable: 0 });
});

test('(p11) a slice degraded for a NON-input reason (both attempts wrong nonce) leaves the input-proof ledger untouched: mismatched===0, missing===0', async () => {
  // The ledger's disjointness invariant (stated in verifyStage's own comment: "a slice
  // degraded for any other reason is counted in `slices` and nowhere else") has to be
  // pinned by a test, not just by reading the code — a mutation that widens
  // `else if (attempt.inputFault === 'missing')` to a bare `else` would otherwise count
  // THIS slice's ordinary nonce-mismatch degrade as an input-proof failure and the whole
  // suite would stay green.
  const input = baseInput();
  const ctx = verifyCtx(() => okEnvelope(input.findings, { nonce: 'WRONG' })); // fails both attempts, never an input-proof fault
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.deepEqual(out.inputProof, { slices: 1, proven: 0, recovered: 0, mismatched: 0, missing: 0, unprovable: 0 });
});
