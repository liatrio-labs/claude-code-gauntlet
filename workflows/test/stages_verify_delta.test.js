// stages_verify_delta.test.js — the guards issue #25 requirement 2 added at the verify
// boundary, and the requirement-1 constraint the join must preserve.
//
// stages_verify.test.js owns the stage's ORCHESTRATION contract (per-slice degradation,
// the single retry, slice-index ordering, receipt checks). This file owns what the delta
// echo itself made possible:
//
//   1. DELTA-ID COVERAGE. The echo names ids, and those ids must be the ones this slice
//      dispatched. This is the guard that closes the silent-drop hole reproduced during
//      the #54 review and re-published as a runnable PoC on issue #25 — a sibling slice's
//      answer used to satisfy every check while the real slice's findings vanished from
//      the run under `verified: true`.
//   2. CONTENT PROOF. A shape that passes (1) can still carry drifted VALUES — a flipped
//      origin, a shifted confidence, a plausible elimination. The script checksums its own
//      deltas and the workflow recomputes; an executor that transcribes cannot recompute.
//   3. THE JOIN. The findings on the trusted path are the DISPATCHED ones, enriched. Which
//      means: nothing the script did not touch can be lost in transcription, and `agent`
//      is stripped exactly once, here (issue #25 requirement 1 — deterministic agent
//      identity past verify is the measured dedup recall-collapse mechanism, and it
//      re-lands only with the cross-dimension consolidation redesign, issue #22).
//
// The harness is deliberately local rather than shared with stages_verify.test.js: these
// tests drive MALFORMED envelopes, and a helper that made malformed answers convenient
// would make the honest ones less obvious in the file that tests them.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { verifyStage, joinVerifyDeltas, deltaContentProof } from '../src/stages.js';
import { deltaEnvelope, deltasFor, ELIMINATION_STAMP, sliceInputRecorder } from './helpers/verifyDelta.js';

function makeFindings(n, over = {}) {
  return Array.from({ length: n }, (_, k) => ({
    id: `F${k}`,
    file: `f${k}.js`,
    line_start: k + 1,
    line_end: k + 1,
    title: `t${k}`,
    description: `d${k}`,
    severity: 'high',
    confidence: 80,
    dimension: 'bug',
    origin: 'new',
    evidence: `e${k}`,
    cross_file_refs: [],
    agent: 'bug-detector',
    ...over,
  }));
}

function baseInput(findings, over = {}) {
  return {
    findings,
    nonce: 'n-1',
    headShaShort: 'abc123',
    limits: { verifySliceSize: findings.length },
    policy: {},
    verify: {
      scriptPath: '/plugin/scripts/verify_findings.py',
      inputPathBase: '/out/phase4-input-abc123',
      outputPathBase: '/out/phase4-output-abc123',
      baseBranch: 'main',
    },
    ...over,
  };
}

// executorImpl(sliceIndex, attempt, sliceNonce) -> the envelope that dispatch returns.
// The slice-input writer always succeeds (its failure path is stages_verify.test.js's
// subject), so every dispatch this harness sees is an executor dispatch.
function ctxFor(executorImpl) {
  const calls = [];
  const rec = sliceInputRecorder();
  const agent = async (prompt, opts = {}) => {
    calls.push({ prompt, ...opts });
    const label = opts.label || '';
    if (label.startsWith('verify-input-writer')) {
      return rec.write(prompt);
    }
    const m = /^verify-slice-(\d+)(-retry)?$/.exec(label);
    const i = m ? Number(m[1]) : -1;
    const attempt = m && m[2] ? 2 : 1;
    const nonce = (prompt.match(/--nonce (\S+)/) || [])[1];
    return rec.stamp(await executorImpl(i, attempt, nonce), i);
  };
  return {
    calls,
    execLabels: () => calls.map((c) => c.label).filter((l) => (l || '').startsWith('verify-slice-')),
    agent,
    parallel: async (thunks) => Promise.all(thunks.map(async (t) => {
      try { return await t(); } catch { return null; }
    })),
  };
}

const unverifiedGap = (gaps) => (gaps || []).find((g) => g.includes('verify: UNVERIFIED'));

// --- 1. Delta-id coverage ----------------------------------------------------

// The PoC published on issue #25 (poc_verify_content_swap.mjs), expressed in the delta
// shape and asserted the way it must now behave. On main and on #71 this printed
// `ids out: F2,F3,F2,F3 / F0 present: false / verified: true` — two findings gone, two
// duplicated, and the run positively asserting trustworthy classification. The
// substitution is now visible because the answer names the ids it is answering about.
test('the sibling-slice substitution PoC: slice 0 keeps its own findings and degrades honestly', async () => {
  const findings = makeFindings(4);
  const input = baseInput(findings, { limits: { verifySliceSize: 2 } });
  const sliceOne = findings.slice(2, 4);

  const ctx = ctxFor((i, attempt, nonce) => {
    // Slice 0's first attempt fails HONESTLY; its retry answers with slice 1's content —
    // the exact shape of the original PoC.
    if (i === 0 && attempt === 1) return { status: 'failed', exitCode: 1, stderr: 'transient' };
    return deltaEnvelope(sliceOne, { nonce, n_in: 2 });
  });

  const out = await verifyStage(ctx, input);

  // Never-drop, now structurally: every dispatched finding leaves the stage exactly once.
  assert.deepEqual(out.findings.map((f) => f.id), ['F0', 'F1', 'F2', 'F3']);
  // Slice 0 could not be verified, and says so rather than reporting success.
  assert.equal(out.verified, false);
  assert.equal(out.findings.find((f) => f.id === 'F0').origin, 'unknown');
  assert.equal(out.findings.find((f) => f.id === 'F1').origin, 'unknown');
  // Slice 1 answered for itself and keeps its classification — per-slice, as PR1 built it.
  assert.equal(out.findings.find((f) => f.id === 'F2').origin, 'new');
  const gap = unverifiedGap(out.gaps);
  assert.ok(gap, `a loud gap names the degraded slice, got: ${JSON.stringify(out.gaps)}`);
  assert.match(gap, /slice 0/);
  assert.match(gap, /2 of 4 finding\(s\)/);
});

test('a delta that omits one dispatched id degrades the slice (coverage, not a count)', async () => {
  const findings = makeFindings(3);
  const short = deltasFor(findings).slice(0, 2);
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, {
    nonce, deltas: short, ids: findings.slice(0, 2).map((f) => f.id),
  }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 3, 'the uncovered finding is kept, never dropped');
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.match(unverifiedGap(out.gaps), /does not cover 1 of 3/);
});

test('a delta naming an id this slice never dispatched degrades the slice', async () => {
  const findings = makeFindings(2);
  const deltas = deltasFor(findings).concat([{ id: 'STRANGER', verified: true, origin: 'new' }]);
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, { nonce, deltas }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.match(unverifiedGap(out.gaps), /did not dispatch \(STRANGER\)/);
});

test('a delta repeating an id degrades the slice', async () => {
  const findings = makeFindings(2);
  const deltas = deltasFor(findings).concat([deltasFor(findings)[0]]);
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, { nonce, deltas }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.match(unverifiedGap(out.gaps), /repeats a finding id \(F0\)/);
});

// --- 2. Content proof --------------------------------------------------------

// The failure the checksum exists for: a shape that satisfies every structural guard while
// carrying values the script never wrote. Measured precedent — the by-value artifact
// writer's transcription of findings.json diverged from the payload it was handed on 3 of
// 3 runs (16 chars, 8 chars, and one document broken outright).
test('a flipped origin under the script\'s original checksum degrades the slice', async () => {
  const findings = makeFindings(2);
  const honest = deltasFor(findings);
  const drifted = honest.map((d, k) => (k === 0 ? { ...d, origin: 'surfaced' } : d));
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, {
    nonce,
    deltas: drifted,
    // The receipt still carries the proof the SCRIPT computed, over the honest deltas.
    checksum: deltaContentProof(findings.map((f) => f.id), honest),
  }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.ok(out.findings.every((f) => f.origin === 'unknown'), 'the drifted classification is not delivered');
  assert.match(unverifiedGap(out.gaps), /content proof mismatch/);
});

test('a shifted confidence under the original checksum degrades the slice', async () => {
  const findings = makeFindings(1);
  const honest = deltasFor(findings);
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, {
    nonce,
    deltas: [{ ...honest[0], confidence: 75 }],
    checksum: deltaContentProof(['F0'], honest),
  }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.match(unverifiedGap(out.gaps), /content proof mismatch/);
});

test('a receipt with no deltas_checksum is untrusted (an absent proof is not a passing one)', async () => {
  const findings = makeFindings(2);
  const ctx = ctxFor((i, attempt, nonce) => {
    const env = deltaEnvelope(findings, { nonce });
    delete env.receipt.deltas_checksum;
    return env;
  });
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.match(unverifiedGap(out.gaps), /no deltas_checksum/);
});

// The echo's own ARRAY ORDER is not part of the proof: the canonical form is rebuilt from
// the dispatched id order, so an executor that reorders its answer is tolerated while one
// that changes a value is not. Without this property, a harmless reordering would cost a
// slice its verification on every run that happened to hit one.
test('a reordered delta array with the same values stays trusted', async () => {
  const findings = makeFindings(3);
  const reversed = deltasFor(findings).slice().reverse();
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, { nonce, deltas: reversed }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, true);
  assert.deepEqual(out.findings.map((f) => f.id), ['F0', 'F1', 'F2'], 'output order follows the DISPATCH, not the echo');
  assert.equal(out.gaps.length, 0);
});

// --- 3. Elimination stamp and delta shape ------------------------------------

test('an eliminated delta without the script\'s stamp degrades the slice (fabricated elimination)', async () => {
  const findings = makeFindings(2);
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, {
    nonce, overrides: { F1: { verified: false } },
  }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 2, 'a claimed elimination cannot be used to drop a real finding');
  assert.match(unverifiedGap(out.gaps), /elimination_reason stamp/);
});

test('a verified delta carrying an elimination stamp degrades the slice', async () => {
  const findings = makeFindings(2);
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, {
    nonce, overrides: { F1: { elimination_reason: ELIMINATION_STAMP } },
  }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.match(unverifiedGap(out.gaps), /verified finding carries an elimination_reason/);
});

test('a stamped elimination is honoured: the finding is absent, the rest are verified', async () => {
  const findings = makeFindings(3);
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, {
    nonce, overrides: { F1: { verified: false, elimination_reason: ELIMINATION_STAMP } },
  }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, true);
  assert.deepEqual(out.findings.map((f) => f.id), ['F0', 'F2']);
  assert.equal(out.gaps.length, 0);
});

test('a non-integer confidence in a delta degrades the slice', async () => {
  const findings = makeFindings(1);
  const deltas = [{ id: 'F0', verified: true, origin: 'new', confidence: 82.5 }];
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, { nonce, deltas }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.match(unverifiedGap(out.gaps), /confidence is not an integer/);
});

test('a delta entry that is not an object degrades the slice', async () => {
  const findings = makeFindings(1);
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, {
    nonce, deltas: ['not-an-object'], checksum: 'fnv1a32:0xdeadbeef',
  }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.match(unverifiedGap(out.gaps), /not an object/);
});

test('a delta entry with a blank id degrades the slice', async () => {
  const findings = makeFindings(1);
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, {
    nonce, deltas: [{ id: '   ', verified: true, origin: 'new' }], checksum: 'fnv1a32:0xdeadbeef',
  }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.match(unverifiedGap(out.gaps), /has no id/);
});

test('a delta whose verified flag is a string degrades the slice', async () => {
  const findings = makeFindings(1);
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, {
    nonce,
    deltas: [{ id: 'F0', verified: 'true', origin: 'new', severity: 'high', confidence: 80 }],
    checksum: 'fnv1a32:0xdeadbeef',
  }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.match(unverifiedGap(out.gaps), /no boolean verified flag/);
});

test('a missing deltas array degrades the slice', async () => {
  const findings = makeFindings(2);
  const ctx = ctxFor((i, attempt, nonce) => ({
    status: 'ok', receipt: { sha: 'abc123', nonce, n_in: 2, deltas_checksum: 'fnv1a32:0x00000000' }, result: {},
  }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.match(unverifiedGap(out.gaps), /missing deltas array/);
});

// --- 4. Ids the join cannot use ----------------------------------------------

// Post-merge findings always carry ids (mergeFindings drops id-less ones), so this is a
// guard rather than a routine path — but it must degrade-and-disclose rather than reject,
// or a merge-side regression would silently cost every slice its verification.
test('a slice with an unusable id set is never dispatched and degrades with a reason', async () => {
  const findings = makeFindings(2);
  delete findings[1].id;
  const ctx = ctxFor(() => {
    throw new Error('the executor must not be dispatched for an unjoinable slice');
  });
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 2, 'findings are kept');
  assert.deepEqual(ctx.execLabels(), [], 'no executor was spent on an answer that could not be used');
  assert.match(unverifiedGap(out.gaps), /no usable id/);
  assert.match(unverifiedGap(out.gaps), /keyed by id/);
});

// Ids are matched EXACTLY at every step. An earlier draft matched on the trimmed form,
// which collided two ids differing only by surrounding whitespace into a false
// whole-slice degrade — and bought nothing, because the content proof compares the id
// text the script wrote, so a whitespace-altered echo failed there anyway. Found by the
// adversarial pass on this branch; pinned so the tolerant form cannot come back.
//
// The DIFFERENT delta decisions matter: when both were identical, a trim collision still
// looked like a successful lookup (one entry's values stood in for both). Cross-runtime
// proof that the checksum itself agrees with Python is the
// tests/fixtures/parity/verify_deltas/whitespace_padded_ids golden — this test owns the
// trust/join behaviour for the same shape.
test('ids differing only by surrounding whitespace are distinct, not a duplicate', async () => {
  const findings = makeFindings(2);
  findings[0].id = 'F1';
  findings[1].id = ' F1 ';
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, {
    nonce,
    overrides: { ' F1 ': { verified: false, elimination_reason: ELIMINATION_STAMP } },
  }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, true, 'two distinct ids must not read as a duplicate');
  assert.deepEqual(out.findings.map((f) => f.id), ['F1'], 'only the verified bare-token id survives');
  assert.equal(out.gaps.length, 0);
});

// Direct unit pin for the trim/no-trim defect: deltaContentProof must key by the raw id
// so a whitespace-padded dispatched id associates with its own delta. The Python-produced
// checksum in the whitespace_padded_ids golden is the cross-runtime half of this guard.
test('deltaContentProof keys by exact id text, not trimmed', () => {
  const ids = ['ws-1', ' ws-1 '];
  const deltas = [
    { id: 'ws-1', verified: true, origin: 'new', severity: 'high', confidence: 80 },
    {
      id: ' ws-1 ', verified: false, origin: 'new', severity: 'medium', confidence: 0,
      elimination_reason: ELIMINATION_STAMP,
    },
  ];
  // The golden recorded by verify_findings.build_deltas/deltas_checksum for this shape.
  assert.equal(deltaContentProof(ids, deltas), 'fnv1a32:0x336f631c');
});

test('an id that is only whitespace is unusable and degrades without dispatching', async () => {
  const findings = makeFindings(2);
  findings[1].id = '   ';
  const ctx = ctxFor(() => { throw new Error('must not dispatch'); });
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.deepEqual(ctx.execLabels(), []);
  assert.match(unverifiedGap(out.gaps), /no usable id/);
});

test('a slice with duplicate ids degrades without dispatching', async () => {
  const findings = makeFindings(2);
  findings[1].id = findings[0].id;
  const ctx = ctxFor(() => { throw new Error('must not dispatch'); });
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.deepEqual(ctx.execLabels(), []);
  assert.match(unverifiedGap(out.gaps), /duplicate finding id/);
});

// --- 5. The join: equivalence, and the agent-withholding constraint ----------

// Issue #25 requirement 1's constraint, stated as a test because a regression here is
// invisible in every other signal: deterministic merge-injected `agent` reaching the
// filter is the MEASURED recall-collapse mechanism (mini-subset A: dedupCrossAgent
// eliminations 7 -> 33, same-6 recall 20/30 -> 13/30). It re-lands only together with the
// cross-dimension consolidation redesign (#22) — they are one design problem.
test('the join strips `agent` from every finding it emits', async () => {
  const findings = makeFindings(3);
  assert.ok(findings.every((f) => f.agent), 'the fixture dispatches findings that DO carry agent');
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, { nonce }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, true);
  for (const f of out.findings) {
    assert.equal('agent' in f, false, `finding ${f.id} must not carry a filter-visible agent identity`);
  }
});

// The DEGRADED path is deliberately untouched by #25 PR2: it re-emits the stage's own
// input, `agent` included, exactly as PR1 left it. Stripping there would be a
// findings-content change on a path this PR does not measure — recorded as an adjacent
// inconsistency rather than fixed in passing.
test('the degraded path is unchanged — it re-emits the dispatched findings as they were', async () => {
  const findings = makeFindings(2);
  const ctx = ctxFor((i, attempt, nonce) => ({ status: 'failed', exitCode: 1, stderr: 'boom' }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, false);
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.ok(out.findings.every((f) => f.agent === 'bug-detector'), 'degraded findings are the input, unmodified beyond origin');
});

test('everything the script does not touch survives the join by construction', async () => {
  const [f] = makeFindings(1);
  f.suggestion = 'guard the null case';
  f.claude_md_rule = 'stdlib-only';
  f.hidden_errors = 'AttributeError on the API-key path';
  f.cross_file_refs = ['other.js:9'];
  f.description = 'x'.repeat(480);
  const findings = [f];

  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, {
    nonce, overrides: { F0: { origin: 'surfaced', severity: 'medium', confidence: 55 } },
  }));
  const out = await verifyStage(ctx, baseInput(findings));

  assert.equal(out.verified, true);
  const [got] = out.findings;
  // The script's decisions are applied...
  assert.equal(got.origin, 'surfaced');
  assert.equal(got.severity, 'medium');
  assert.equal(got.confidence, 55);
  // ...and everything else is the value this stage already held. Under the by-value echo
  // each of these depended on a sampled agent transcribing it correctly; `description` was
  // observed emptied live, which false-fired the filter's injection guard.
  assert.equal(got.description, f.description);
  assert.equal(got.suggestion, 'guard the null case');
  assert.equal(got.claude_md_rule, 'stdlib-only');
  assert.equal(got.hidden_errors, 'AttributeError on the API-key path');
  assert.deepEqual(got.cross_file_refs, ['other.js:9']);
});

// pinNumericFields on the joined path, for the same reason it is on the degraded path: a
// confidence that reaches the filter as the string "85" makes the consensus boost
// concatenate ("85" + 10 -> "8510"). The script coerces at its --input boundary, so the
// trusted output has always carried real numbers; the join must reproduce that.
test('string-typed numerics on a dispatched finding are pinned by the join', async () => {
  const findings = makeFindings(1);
  findings[0].line_start = '42';
  findings[0].line_end = '44';
  findings[0].confidence = '85';
  // The delta carries no confidence here (the script left it alone), so the pin is what
  // decides the joined value.
  const deltas = [{ id: 'F0', verified: true, origin: 'new', severity: 'high' }];
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, { nonce, deltas }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, true);
  assert.equal(out.findings[0].line_start, 42);
  assert.equal(out.findings[0].line_end, 44);
  assert.equal(out.findings[0].confidence, 85);
});

// Line fields are not in the delta (the script does not re-decide them). Without half-up
// rounding in pinNumericFields, the join would keep a fractional dispatched line_start
// while verification ran against the value _coerce_numeric_fields rounded at the Python
// input boundary — the silent divergence Bugbot flagged on this PR.
test('fractional line fields on a dispatched finding are half-up rounded by the join', async () => {
  const findings = makeFindings(1);
  findings[0].line_start = 4.6;
  findings[0].line_end = 9.2;
  findings[0].confidence = 82.5;
  // Delta carries no confidence/line fields — the pin alone decides the joined values.
  const deltas = [{ id: 'F0', verified: true, origin: 'new', severity: 'high' }];
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, { nonce, deltas }));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, true);
  assert.equal(out.findings[0].line_start, 5);
  assert.equal(out.findings[0].line_end, 9);
  assert.equal(out.findings[0].confidence, 83);
});

test('joinVerifyDeltas is pure: it never mutates the findings it is given', () => {
  const findings = makeFindings(2);
  const before = JSON.parse(JSON.stringify(findings));
  joinVerifyDeltas(findings, deltasFor(findings, { F1: { verified: false, elimination_reason: ELIMINATION_STAMP } }));
  assert.deepEqual(findings, before);
});

// --- 6. The dispatch schema no longer carries findings -----------------------

// The structural half of the withholding and of the description-strip fix: not "the schema
// declares every finding field correctly" (which it had to, when findings crossed the
// boundary) but "no finding crosses the boundary at all".
test('the verify dispatch schema declares deltas and no finding arrays', async () => {
  const findings = makeFindings(2);
  const ctx = ctxFor((i, attempt, nonce) => deltaEnvelope(findings, { nonce }));
  await verifyStage(ctx, baseInput(findings));
  const call = ctx.calls.find((c) => (c.label || '').startsWith('verify-slice-'));
  const result = call.schema.properties.result.properties;
  assert.ok(result.deltas, 'result declares deltas');
  assert.equal(result.verified, undefined, 'result must not declare a verified finding array');
  assert.equal(result.eliminated, undefined, 'result must not declare an eliminated finding array');
  assert.deepEqual(
    Object.keys(result.deltas.items.properties).sort(),
    ['confidence', 'elimination_reason', 'id', 'origin', 'severity', 'verified'],
    'the delta item declares exactly the six keys verify_findings.py emits',
  );
  assert.ok(call.schema.properties.receipt.properties.deltas_checksum, 'the receipt declares the content proof');
});

// --- 7. The retry still works on the new failure classes ---------------------

test('a first attempt that fails the content proof is retried once, and a clean retry is trusted', async () => {
  const findings = makeFindings(2);
  const ctx = ctxFor((i, attempt, nonce) => (attempt === 1
    ? deltaEnvelope(findings, { nonce, checksum: 'fnv1a32:0xdeadbeef' })
    : deltaEnvelope(findings, { nonce })));
  const out = await verifyStage(ctx, baseInput(findings));
  assert.equal(out.verified, true, 'a recovered slice is not a degraded run');
  assert.deepEqual(ctx.execLabels(), ['verify-slice-0', 'verify-slice-0-retry']);
  const gap = out.gaps[0];
  assert.match(gap, /verify-slice-retry/);
  assert.equal(gap.includes('UNVERIFIED'), false, 'a recovered slice discloses, it does not degrade');
  assert.ok(out.findings.every((f) => f.origin === 'new'));
});
