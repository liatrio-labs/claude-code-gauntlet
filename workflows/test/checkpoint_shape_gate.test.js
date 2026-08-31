// checkpoint_shape_gate.test.js — #248 + #250: the RESOLVED replayed checkpoint map is
// shape-gated, pre-dispatch, before runWith trusts any phase's checkpointed value.
//
// checkpointShapeErrors(resolvedCheckpoints) is a pure function: it takes the OUTPUT of
// readCheckpoints (already `.phases`-unwrapped, already defaulted to {} for a non-object
// top-level `checkpoints`) and returns a string[] of `checkpoint-shape:`-prefixed
// violations, one per malformed spot. runWith calls it immediately after
// `readCheckpoints`, pre-dispatch and outside the top-level try, and on ANY violation
// returns a dedicated refusal envelope instead of running the pipeline at all.
//
// Table under test (issues #248/#250) — one row per phase runPhase() names, ALL EIGHT:
//   every phase                 : gaps (array, elements NOT checked — container-only)
//   discover                    : additionally dispatched, degraded (array, container-only)
//   discover/merge/verify/validate: findings (array, elements plain objects, REQUIRED
//                                  whenever the phase key is present)
//   filter                       : filtered (array, elements plain objects, REQUIRED)
//   challenge                    : findings (array, elements plain objects, strict,
//                                  REQUIRED); unverified (array, elements TOLERATED,
//                                  optional); eliminated is WHOLLY ungated (not in the
//                                  table at all)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  checkpointShapeErrors, runWith, readCheckpoints, CHECKPOINT_PHASE_SHAPE_TABLE, buildResumeCheckpoints,
  slimPersistedCheckpoints,
} from '../src/stages.js';
import { makeFinding, validArgs, makeCtx } from './helpers/pipelineMock.js';

// The strict rows of the exported shape table, derived here (not in stages.js, where the
// derived list would ship as dead code in the bundle). 'strict' is the table's sentinel.
const CHECKPOINT_STRICT_FIELDS = Object.entries(CHECKPOINT_PHASE_SHAPE_TABLE).flatMap(
  ([phase, fields]) => Object.entries(fields)
    .filter(([, kind]) => kind === 'strict')
    .map(([field]) => [phase, field]),
);

// A well-formed value for every array-bearing phase field, keyed by phase name, so the
// "everything else stays well-formed" pattern below never accidentally trips a SECOND
// violation while probing one spot. Every phase carries `gaps: []`; discover also carries
// `dispatched`/`degraded`; every REQUIRED content field is present — this is a full,
// real-shaped checkpoint, which the acceptance pins below run straight off.
function wellFormedCheckpoints() {
  return {
    summarize: { summary: 'x', gaps: [] },
    discover: {
      findings: [makeFinding('D1')], gaps: [], degraded: [], dispatched: ['code-gauntlet:bug-detector'],
    },
    merge: { findings: [makeFinding('M1')], gaps: [] },
    verify: { findings: [makeFinding('V1')], gaps: [] },
    validate: { findings: [makeFinding('VA1')], gaps: [] },
    filter: { filtered: [makeFinding('F1')], gaps: [] },
    challenge: {
      findings: [makeFinding('C1')], unverified: [], eliminated: [], gaps: [], stats: {},
    },
    report: { report: '# r', gaps: [] },
  };
}

const ALL_PHASES = ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report'];
const NON_OBJECT_SAMPLES = [
  ['null', null],
  ['an array', []],
  ['a string', 'not-an-object'],
  ['a number', 42],
  ['a boolean', true],
];

// --- checkpointShapeErrors: container check, ALL EIGHT phases ---------------

for (const phase of ALL_PHASES) {
  for (const [label, badValue] of NON_OBJECT_SAMPLES) {
    test(`checkpointShapeErrors: phases.${phase} as ${label} is refused (row: ${phase})`, () => {
      const cp = { ...wellFormedCheckpoints(), [phase]: badValue };
      const errors = checkpointShapeErrors(cp);
      assert.ok(errors.length >= 1, `expected at least one violation, got none for ${phase}=${label}`);
      assert.ok(
        errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes(`phases.${phase}`)),
        `expected a checkpoint-shape: violation naming phases.${phase}, got: ${JSON.stringify(errors)}`,
      );
    });
  }
}

// --- checkpointShapeErrors: array-field check --------------------------------

test('checkpointShapeErrors: discover.findings non-array is refused', () => {
  const cp = { ...wellFormedCheckpoints(), discover: { findings: 'not-an-array', gaps: [] } };
  const errors = checkpointShapeErrors(cp);
  assert.ok(errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes('discover.findings')), JSON.stringify(errors));
});

test('checkpointShapeErrors: merge.findings non-array is refused', () => {
  const cp = { ...wellFormedCheckpoints(), merge: { findings: 'not-an-array' } };
  const errors = checkpointShapeErrors(cp);
  assert.ok(errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes('merge.findings')), JSON.stringify(errors));
});

test('checkpointShapeErrors: verify.findings non-array is refused', () => {
  const cp = { ...wellFormedCheckpoints(), verify: { findings: {} } };
  const errors = checkpointShapeErrors(cp);
  assert.ok(errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes('verify.findings')), JSON.stringify(errors));
});

test('checkpointShapeErrors: validate.findings non-array is refused', () => {
  const cp = { ...wellFormedCheckpoints(), validate: { findings: 7 } };
  const errors = checkpointShapeErrors(cp);
  assert.ok(errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes('validate.findings')), JSON.stringify(errors));
});

test('checkpointShapeErrors: filter.filtered non-array is refused', () => {
  const cp = { ...wellFormedCheckpoints(), filter: { filtered: 'not-an-array' } };
  const errors = checkpointShapeErrors(cp);
  assert.ok(errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes('filter.filtered')), JSON.stringify(errors));
});

test('checkpointShapeErrors: challenge.findings non-array is refused', () => {
  const cp = { ...wellFormedCheckpoints(), challenge: { ...wellFormedCheckpoints().challenge, findings: 'not-an-array' } };
  const errors = checkpointShapeErrors(cp);
  assert.ok(errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes('challenge.findings')), JSON.stringify(errors));
});

test('checkpointShapeErrors: challenge.unverified non-array is refused (container-gated)', () => {
  const cp = { ...wellFormedCheckpoints(), challenge: { ...wellFormedCheckpoints().challenge, unverified: 'not-an-array' } };
  const errors = checkpointShapeErrors(cp);
  assert.ok(errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes('challenge.unverified')), JSON.stringify(errors));
});

// --- checkpointShapeErrors: container-only fields (gaps/dispatched/degraded) -------------
//
// out.gaps is spread unconditionally (`...(out.gaps || [])`) on every phase, so ABSENT
// passes but a truthy non-array raw-TypeErrors or silently iterates per-character garbage.
// discover.dispatched/degraded are unconditionally .join-ed/iterated the same way.

for (const [phase, badValue] of [
  ['summarize', 'not-an-array'],
  ['discover', 5],
  ['merge', {}],
  ['verify', 'abc'],
  ['validate', 42],
  ['filter', {}],
  ['challenge', 'not-an-array'],
  ['report', 7],
]) {
  test(`checkpointShapeErrors: ${phase}.gaps non-array is refused`, () => {
    const base = wellFormedCheckpoints();
    const cp = { ...base, [phase]: { ...base[phase], gaps: badValue } };
    const errors = checkpointShapeErrors(cp);
    assert.ok(errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes(`${phase}.gaps`)), JSON.stringify(errors));
  });
}

test('checkpointShapeErrors: gaps absent is accepted on every phase', () => {
  const base = wellFormedCheckpoints();
  const cp = Object.fromEntries(Object.entries(base).map(([phase, value]) => {
    const { gaps, ...rest } = value;
    return [phase, rest];
  }));
  assert.deepEqual(checkpointShapeErrors(cp), []);
});

test('checkpointShapeErrors: discover.dispatched non-array is refused', () => {
  const cp = { ...wellFormedCheckpoints(), discover: { ...wellFormedCheckpoints().discover, dispatched: 'not-an-array' } };
  const errors = checkpointShapeErrors(cp);
  assert.ok(errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes('discover.dispatched')), JSON.stringify(errors));
});

test('checkpointShapeErrors: discover.degraded non-array is refused', () => {
  const cp = { ...wellFormedCheckpoints(), discover: { ...wellFormedCheckpoints().discover, degraded: 42 } };
  const errors = checkpointShapeErrors(cp);
  assert.ok(errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes('discover.degraded')), JSON.stringify(errors));
});

// --- checkpointShapeErrors: element check, the three PREVIOUSLY-UNPINNED rows -----------
//
// HARD-CODED, independent of CHECKPOINT_PHASE_SHAPE_TABLE — deliberately NOT sourced from
// CHECKPOINT_STRICT_FIELDS below. A loop derived from the table cannot catch a regression
// IN the table: flipping verify.findings from strict to tolerant also removes it from
// CHECKPOINT_STRICT_FIELDS, so a table-driven loop silently stops testing the row it broke
// instead of failing red (mutation-verified: without these three hard-coded tests,
// flipping verify.findings/validate.findings/filter.filtered to tolerant leaves the suite
// green).
for (const [phase, field] of [
  ['verify', 'findings'],
  ['validate', 'findings'],
  ['filter', 'filtered'],
]) {
  test(`checkpointShapeErrors: a null element in ${phase}.${field} is refused (pinned independent of the table)`, () => {
    const base = wellFormedCheckpoints();
    const cp = { ...base, [phase]: { ...base[phase], [field]: [...base[phase][field], null] } };
    const errors = checkpointShapeErrors(cp);
    assert.ok(
      errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes(`${phase}.${field}`)),
      `expected a null-element violation for ${phase}.${field}, got: ${JSON.stringify(errors)}`,
    );
  });
}

// --- checkpointShapeErrors: element check, ALL STRICT ROWS (forward coverage) -----------
//
// Looped over CHECKPOINT_STRICT_FIELDS (exported by stages.js): a table row later ADDED as
// strict is covered here with no second edit. This loop is forward-looking only — it
// cannot pin an EXISTING row against being flipped away from strict (see the hard-coded
// block above for that).
for (const [phase, field] of CHECKPOINT_STRICT_FIELDS) {
  test(`checkpointShapeErrors: a null element in ${phase}.${field} is refused`, () => {
    const base = wellFormedCheckpoints();
    const cp = { ...base, [phase]: { ...base[phase], [field]: [...base[phase][field], null] } };
    const errors = checkpointShapeErrors(cp);
    assert.ok(
      errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes(`${phase}.${field}`)),
      `expected a null-element violation for ${phase}.${field}, got: ${JSON.stringify(errors)}`,
    );
  });

  test(`checkpointShapeErrors: a primitive element in ${phase}.${field} is refused`, () => {
    const base = wellFormedCheckpoints();
    const cp = { ...base, [phase]: { ...base[phase], [field]: [...base[phase][field], 'not-an-object'] } };
    const errors = checkpointShapeErrors(cp);
    assert.ok(
      errors.some((e) => e.startsWith('checkpoint-shape:') && e.includes(`${phase}.${field}`)),
      `expected a primitive-element violation for ${phase}.${field}, got: ${JSON.stringify(errors)}`,
    );
  });
}

// --- checkpointShapeErrors: acceptance pins ----------------------------------

test('checkpointShapeErrors: challenge.unverified [null, real] is ACCEPTED (element tolerance, #213 degradation)', () => {
  const cp = {
    ...wellFormedCheckpoints(),
    challenge: { ...wellFormedCheckpoints().challenge, unverified: [null, makeFinding('U1')] },
  };
  assert.deepEqual(checkpointShapeErrors(cp), []);
});

test('checkpointShapeErrors: challenge.eliminated "not-an-array" is ACCEPTED (wholly ungated)', () => {
  const cp = {
    ...wellFormedCheckpoints(),
    challenge: { ...wellFormedCheckpoints().challenge, eliminated: 'not-an-array' },
  };
  assert.deepEqual(checkpointShapeErrors(cp), []);
});

test('checkpointShapeErrors: unknown checkpoint keys are inert', () => {
  const cp = { ...wellFormedCheckpoints(), someFuturePhase: 'garbage', notAPhase: 123 };
  assert.deepEqual(checkpointShapeErrors(cp), []);
});

test('checkpointShapeErrors: an absent OPTIONAL field always passes (version-delta replay)', () => {
  // A phase present with its REQUIRED content field but none of its (version-newer)
  // optional array fields (gaps/dispatched/degraded/unverified) stamped at all.
  const cp = { discover: { findings: [makeFinding('D1')] }, challenge: { findings: [makeFinding('C1')] } };
  assert.deepEqual(checkpointShapeErrors(cp), []);
});

// --- checkpointShapeErrors: required content fields (#248 silent-empty) -----------------

for (const [phase, field] of [
  ['discover', 'findings'],
  ['merge', 'findings'],
  ['verify', 'findings'],
  ['validate', 'findings'],
  ['filter', 'filtered'],
  ['challenge', 'findings'],
]) {
  test(`checkpointShapeErrors: ${phase}:{} is refused (missing required field ${field})`, () => {
    const cp = { ...wellFormedCheckpoints(), [phase]: {} };
    const errors = checkpointShapeErrors(cp);
    assert.ok(
      errors.includes(`checkpoint-shape: phases.${phase} is missing required field ${field}`),
      `expected the missing-required-field violation for ${phase}, got: ${JSON.stringify(errors)}`,
    );
  });
}

test('checkpointShapeErrors: summarize:{} and report:{} are accepted (no required content field)', () => {
  const cp = { ...wellFormedCheckpoints(), summarize: {}, report: {} };
  assert.deepEqual(checkpointShapeErrors(cp), []);
});

test('checkpointShapeErrors: a non-object/array top-level argument is treated as inert (defensive)', () => {
  assert.deepEqual(checkpointShapeErrors(null), []);
  assert.deepEqual(checkpointShapeErrors('garbage'), []);
  assert.deepEqual(checkpointShapeErrors([]), []);
  assert.deepEqual(checkpointShapeErrors(undefined), []);
});

test('checkpointShapeErrors: a well-formed full checkpoint (all eight phases) is accepted', () => {
  assert.deepEqual(checkpointShapeErrors(wellFormedCheckpoints()), []);
});

test('checkpointShapeErrors: an empty map is accepted', () => {
  assert.deepEqual(checkpointShapeErrors({}), []);
});

// --- runWith wiring: readCheckpoints resolves top-level garbage to {} (inert) ------------

test('runWith: checkpoints: "garbage" at top level resolves inert through readCheckpoints, no refusal', async () => {
  const args = validArgs({ checkpoints: 'garbage' });
  assert.deepEqual(readCheckpoints(null, args), {});
  const out = await runWith(makeCtx(args), args);
  assert.equal(out.ok, true);
  // #268: the discard is no longer silent. checkpoint-discarded: names the failing
  // POSITION (`checkpoints` — the top level itself, here) and its shape.
  const discardGap = out.gaps.find((g) => g.startsWith('checkpoint-discarded:'));
  assert.ok(discardGap, `expected a checkpoint-discarded: gap, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(discardGap.includes('checkpoints must be a plain object'), discardGap);
  assert.ok(discardGap.includes('got string'), discardGap);
});

// --- readCheckpoints: array-shaped checkpoints are garbage, uniformly (round-2 fix) -----
//
// `typeof [] === 'object'` used to let an array slip past the unwrap's object check as a
// truthy "resolved map". The gate then sanitized it to `{}` (no violations to report), but
// runWith indexes the RESOLVED VALUE by phase name -- and `[]['filter'] ===
// Array.prototype.filter`, so a replayed `checkpoints: []` handed runPhase('filter') a
// FUNCTION as the phase's prior output, which it reused as-is with no dispatch. Every
// array-shaped input must resolve to `{}`, exactly like the already-pinned
// `checkpoints: 'garbage'` case above.
test('readCheckpoints: {checkpoints: []} resolves to {} (array top level is garbage)', () => {
  assert.deepEqual(readCheckpoints(null, { checkpoints: [] }), {});
});

test('readCheckpoints: {checkpoints: {phases: []}} resolves to {} (array .phases is garbage)', () => {
  assert.deepEqual(readCheckpoints(null, { checkpoints: { phases: [] } }), {});
});

test('readCheckpoints: {checkpoints: [1,2,3]} resolves to {} (non-empty array top level is garbage)', () => {
  assert.deepEqual(readCheckpoints(null, { checkpoints: [1, 2, 3] }), {});
});

// The regression oracle for the phantom Array.prototype.filter replay: a well-formed run
// with `checkpoints: []` must dispatch the FULL fresh pipeline, including a challenge-stage
// dispatch, and complete ok:true -- not silently skip the filter phase by "replaying" the
// array's own `filter` method as its output.
test('runWith: checkpoints: [] dispatches the full fresh pipeline including challenge (regression oracle for the Array.prototype.filter replay)', async () => {
  const args = validArgs({ checkpoints: [] });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  // #268: the discard is no longer silent. checkpoint-discarded: names the failing
  // POSITION (`checkpoints`, the top level) and its shape (`array`, this time) — the
  // disclosure does not change what dispatched, only what is disclosed.
  const discardGap = out.gaps.find((g) => g.startsWith('checkpoint-discarded:'));
  assert.ok(discardGap, `expected a checkpoint-discarded: gap, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(discardGap.includes('checkpoints must be a plain object'), discardGap);
  assert.ok(discardGap.includes('got array'), discardGap);
  assert.ok(
    ctx.calls.some((c) => (c.label || '').startsWith('challenge-')),
    `expected a challenge-labeled dispatch, got labels: ${JSON.stringify(ctx.calls.map((c) => c.label))}`,
  );
});

test('checkpointShapeErrors: an array top-level argument is still treated as inert (defensive, pinned)', () => {
  assert.deepEqual(checkpointShapeErrors([]), []);
});

// --- checkpoint-discarded: disclosure (#268) ---------------------------------
//
// readCheckpoints's fallback to {} on an unusable args.checkpoints is correct behavior
// (never abort a resume-free review over a bad resume input) but was previously silent.
// checkpoint-discarded: discloses it — computed from the TOP-LEVEL args.checkpoints value
// the operator actually stamped, naming the failing POSITION (`checkpoints` itself, or
// `checkpoints.phases` for a malformed wrapper) and its shape. It never fires for
// `checkpoints` absent, nor for a stamped `checkpoints: null` (dropped to absent by
// normalizeArgsReport upstream, on the NULLABLE_TOP_LEVEL allowlist — the same reasoning
// F4-3 pins for null_arg gaps in pipeline_run.test.js), because in both cases nothing real
// was tolerated or lost.

test('runWith: checkpoints: {phases: "not-an-object"} discloses checkpoint-discarded: naming checkpoints.phases, not the wrapper', async () => {
  const args = validArgs({ checkpoints: { phases: 'not-an-object' } });
  const out = await runWith(makeCtx(args), args);

  assert.equal(out.ok, true);
  const discardGap = out.gaps.find((g) => g.startsWith('checkpoint-discarded:'));
  assert.ok(discardGap, `expected a checkpoint-discarded: gap, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(discardGap.includes('checkpoints.phases must be a plain object'), discardGap);
  assert.ok(discardGap.includes('got string'), discardGap);
  // The wrapper ITSELF is a plain object — describing IT would print the useless
  // "(got object)"; the message must name the offending `.phases` value instead.
  assert.ok(!discardGap.includes('got object'), discardGap);
});

test('runWith: checkpoints: {phases: []} discloses checkpoint-discarded: naming checkpoints.phases (array .phases)', async () => {
  const args = validArgs({ checkpoints: { phases: [] } });
  const out = await runWith(makeCtx(args), args);

  assert.equal(out.ok, true);
  const discardGap = out.gaps.find((g) => g.startsWith('checkpoint-discarded:'));
  assert.ok(discardGap, `expected a checkpoint-discarded: gap, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(discardGap.includes('checkpoints.phases must be a plain object'), discardGap);
  assert.ok(discardGap.includes('got array'), discardGap);
});

test('runWith: checkpoint-discarded: gap text is exit-neutral -- it never claims every phase re-runs', async () => {
  // This same gap text is also threaded into makeCheckpointShapeRejectEnvelope's
  // `gaps` (a hard-refusal exit where NO phase runs at all), so it must not
  // assert what happened on the continuation path it does not always ride --
  // only that nothing was resumed.
  const args = validArgs({ checkpoints: { phases: 'not-an-object' } });
  const out = await runWith(makeCtx(args), args);
  const discardGap = out.gaps.find((g) => g.startsWith('checkpoint-discarded:'));
  assert.ok(discardGap, `expected a checkpoint-discarded: gap, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(discardGap.includes('nothing was resumed'), discardGap);
  assert.ok(!discardGap.includes('every phase re-runs'), discardGap);
});

test('runWith: checkpoints: null discloses NO checkpoint-discarded: gap (equivalent to absent, same F4-3 reasoning)', async () => {
  const args = validArgs({ checkpoints: null });
  const out = await runWith(makeCtx(args), args);
  assert.equal(out.ok, true);
  assert.deepEqual(out.gaps.filter((g) => g.startsWith('checkpoint-discarded:')), []);
});

test('runWith: checkpoints absent (never stamped) discloses NO checkpoint-discarded: gap', async () => {
  const args = validArgs();
  const out = await runWith(makeCtx(args), args);
  assert.equal(out.ok, true);
  assert.deepEqual(out.gaps.filter((g) => g.startsWith('checkpoint-discarded:')), []);
});

test('runWith: a discarded top-level checkpoints AND a checkpoint-shape violation surfaced via the ctx fallback ride the SAME reject-envelope gaps array', async () => {
  // Contrived seam — readCheckpoints's ctx-borne fallback is a test seam, not a live input
  // path in production. args.checkpoints is garbage (discarded outright), so readCheckpoints
  // falls through to ctx.checkpoints: itself a real, malformed value carrying its own shape
  // violation. Exercises makeCheckpointShapeRejectEnvelope's fourth `discardGap` parameter,
  // otherwise unreachable in production — a garbage args.checkpoints ALONE resolves inert
  // with zero shape violations, so the reject envelope is never even built on that path by
  // itself.
  const args = validArgs({ checkpoints: 'garbage' });
  const ctx = { ...makeCtx(args), checkpoints: { challenge: 'not-an-object' } };
  const out = await runWith(ctx, args);

  assert.equal(out.ok, false);
  assert.equal(out.failingPhase, 'checkpoints');
  const discardGap = out.gaps.find((g) => g.startsWith('checkpoint-discarded:'));
  const shapeGap = out.gaps.find((g) => g.startsWith('checkpoint-shape:'));
  assert.ok(discardGap, `expected a checkpoint-discarded: gap, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(shapeGap, `expected a checkpoint-shape: violation, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(discardGap.includes('got string'), discardGap);
});

// --- runWith wiring: refusal envelope ----------------------------------------

test('runWith: a malformed replayed checkpoint phase value is refused pre-dispatch (#248)', async () => {
  const args = validArgs({ checkpoints: { challenge: 'not-an-object' } });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, false);
  assert.equal(out.failingPhase, 'checkpoints');
  assert.equal(out.phaseReached, 'checkpoints');
  assert.deepEqual(out.artifactPaths, {});
  assert.deepEqual(out.stats, {});
  assert.deepEqual(out.checkpoints, { completed: [] }, 'no .phases map, so headless auto-resume cannot fire');
  assert.ok(out.error.startsWith('checkpoint-shape:'), `error must carry the prefix, got: ${out.error}`);
  // The recovery sentence is the operator-actionable half of the message -- pin a
  // distinctive substring so deleting it (measured to leave the rest of this suite green)
  // is caught here.
  assert.ok(out.error.includes('re-run without'), `error must carry the recovery sentence, got: ${out.error}`);
  assert.ok(out.gaps.length >= 1);
  // At least one gap (not necessarily every gap — see the combined-gaps test below, where
  // a tolerated-null disclosure rides alongside the checkpoint-shape violation) must carry
  // the prefix; the checkpoint-shape subset itself must be non-empty.
  assert.ok(
    out.gaps.some((g) => g.startsWith('checkpoint-shape:')),
    `expected at least one checkpoint-shape gap, got: ${JSON.stringify(out.gaps)}`,
  );
  assert.equal(ctx.calls.length, 0, 'nothing was dispatched — the gate runs pre-dispatch');
});

test('runWith: a tolerated null_arg disclosure rides alongside a checkpoint-shape refusal (both survive on the SAME gaps array)', async () => {
  const args = validArgs({ reviewConfig: null, checkpoints: { challenge: 'not-an-object' } });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, false);
  assert.equal(out.failingPhase, 'checkpoints');
  const nullGap = out.gaps.find((g) => g.startsWith('null_arg:') && g.includes('reviewConfig'));
  const shapeGap = out.gaps.find((g) => g.startsWith('checkpoint-shape:'));
  assert.ok(nullGap, `expected a null_arg disclosure for reviewConfig, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(shapeGap, `expected a checkpoint-shape violation, got: ${JSON.stringify(out.gaps)}`);
  assert.equal(ctx.calls.length, 0, 'nothing was dispatched — the gate runs pre-dispatch');
});

// contextSizeGap is computed ABOVE the checkpoint-shape gate (both derive from `A` before
// any phase is attempted), but the gate's own reject envelope used to build its `gaps` from
// only `[...nullArgGaps, ...violations]` -- dropping contextSizeGap on the floor on this one
// exit, even though the success exit and the args-reject exit both carry it. A run whose
// context read plan could not be computed AND whose checkpoint is malformed must disclose
// BOTH on the SAME gaps array, exactly like the null_arg case above.
test('runWith: a context_unmeasured disclosure rides alongside a checkpoint-shape refusal (both survive on the SAME gaps array)', async () => {
  // validArgs() never stamps contextLines, so contextSizeGap is unconditionally non-empty
  // (context_unmeasured) here — no override needed to reach that branch.
  const args = validArgs({ checkpoints: { challenge: 'not-an-object' } });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, false);
  assert.equal(out.failingPhase, 'checkpoints');
  const contextGap = out.gaps.find((g) => g.startsWith('context_unmeasured:'));
  const shapeGap = out.gaps.find((g) => g.startsWith('checkpoint-shape:'));
  assert.ok(contextGap, `expected a context_unmeasured disclosure, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(shapeGap, `expected a checkpoint-shape violation, got: ${JSON.stringify(out.gaps)}`);
  assert.equal(ctx.calls.length, 0, 'nothing was dispatched — the gate runs pre-dispatch');
});

test('runWith: a null element in a replayed challenge.findings is refused pre-dispatch (#250)', async () => {
  const checkpoint = {
    findings: [makeFinding('M1'), null],
    unverified: [],
    eliminated: [],
    gaps: [],
    stats: {},
    generated_at: '2026-07-18T00:00:00Z',
  };
  const args = validArgs({ checkpoints: { challenge: checkpoint } });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, false);
  assert.equal(out.failingPhase, 'checkpoints');
  assert.ok(out.gaps.some((g) => g.startsWith('checkpoint-shape:') && g.includes('challenge.findings')));
  assert.equal(ctx.calls.length, 0, 'nothing was dispatched — rankKey never sees the null element');
});

test('runWith: ALL violations are collected, not just the first', async () => {
  const args = validArgs({
    checkpoints: {
      merge: 'not-an-object',
      verify: { findings: 'not-an-array' },
    },
  });
  const out = await runWith(makeCtx(args), args);

  assert.equal(out.ok, false);
  const mergeGap = out.gaps.find((g) => g.includes('merge'));
  const verifyGap = out.gaps.find((g) => g.includes('verify.findings'));
  assert.ok(mergeGap, `expected a merge violation, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(verifyGap, `expected a verify.findings violation, got: ${JSON.stringify(out.gaps)}`);
  assert.notEqual(mergeGap, verifyGap);
});

test('runWith: the error message names the first violation and the total count', async () => {
  const args = validArgs({
    // Stamped (valid, tiny) so contextSizeGap is empty here — this test isolates the
    // violation-count wording, not the unrelated context_unmeasured disclosure.
    contextLines: 1,
    contextChars: 1,
    checkpoints: {
      merge: 'not-an-object',
      verify: { findings: 'not-an-array' },
    },
  });
  const out = await runWith(makeCtx(args), args);
  assert.equal(out.ok, false);
  assert.equal(out.gaps.length, 2);
  assert.ok(out.error.includes(out.gaps[0]), 'error leads with the first violation verbatim');
  assert.match(out.error, /1 more/, 'error discloses the remaining violation count');
});

test('runWith: a well-formed replayed checkpoint set is never refused by the shape gate', async () => {
  const args = validArgs({ checkpoints: wellFormedCheckpoints() });
  const out = await runWith(makeCtx(args), args);
  assert.notEqual(out.failingPhase, 'checkpoints');
  assert.ok(!(out.gaps || []).some((g) => g.startsWith('checkpoint-shape:')));
  // #268: a well-formed, USABLE checkpoints value is not a discard either — the disclosure
  // fires only when the value is thrown away, never merely because a resume happened.
  assert.ok(!(out.gaps || []).some((g) => g.startsWith('checkpoint-discarded:')));
});

// --- checkpoint-discarded: no-op-resume disclosure (#270) --------------------
//
// #268 caught only unwrapCheckpointMap FAILING. A fed-back `{completed, truncated: true}`
// return -- the shape SKILL.md documents as a real failure-path return, and the one a
// headless auto-resume plausibly re-feeds straight back in -- unwraps SUCCESSFULLY (its
// keys are not `.phases`, so the direct-return branch fires) to an inert bare map: none of
// its keys is a recognized phase name, so every phase re-runs with zero disclosure. Arm A
// below catches that: unwrapCheckpointMap succeeded, the resolved map is non-empty, and
// none of its own keys is one of the eight recognized phase names (derived from
// CHECKPOINT_PHASE_SHAPE_TABLE). Arm B catches a sibling masking bug: `{phases: {}, ...}`
// with a recognized phase name sitting unused at the WRAPPER's top level, right next to the
// `.phases` key the unwrap actually reads.

test('runWith: {completed: [], truncated: true} discloses the truncated-return sub-case, ok:true, challenge dispatched, exactly one gap', async () => {
  const args = validArgs({ checkpoints: { completed: [], truncated: true } });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  const discardGaps = out.gaps.filter((g) => g.startsWith('checkpoint-discarded:'));
  assert.equal(discardGaps.length, 1, `expected exactly one checkpoint-discarded: gap, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(discardGaps[0].includes('truncated'), discardGaps[0]);
  assert.ok(
    ctx.calls.some((c) => (c.label || '').startsWith('challenge-')),
    `expected a challenge-labeled dispatch, got labels: ${JSON.stringify(ctx.calls.map((c) => c.label))}`,
  );
});

test('runWith: {completed: ["summarize"]} (no truncated) discloses the compact-return sub-case', async () => {
  const args = validArgs({ checkpoints: { completed: ['summarize'] } });
  const out = await runWith(makeCtx(args), args);

  assert.equal(out.ok, true);
  const discardGaps = out.gaps.filter((g) => g.startsWith('checkpoint-discarded:'));
  assert.equal(discardGaps.length, 1, `expected exactly one checkpoint-discarded: gap, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(!discardGaps[0].includes('truncated'), discardGaps[0]);
  assert.ok(discardGaps[0].includes('artifactPaths.checkpoints'), discardGaps[0]);
});

// {completed: []} is the OTHER producer of a bare `completed` key: makeCheckpointShapeRejectEnvelope's
// own refusal envelope (`{completed: []}`, `artifactPaths: {}`) fed back as the next run's
// `checkpoints` argument. Nothing was ever persisted for it, so the on-disk pointer sentence
// the non-empty case above gets would send the operator to a path that was never written --
// this sub-case gets fresh-run advice instead, and must NOT mention artifactPaths.checkpoints.
test('runWith: {completed: []} (empty completed) discloses the return-envelope sub-case, NOT the on-disk pointer', async () => {
  const args = validArgs({ checkpoints: { completed: [] } });
  const out = await runWith(makeCtx(args), args);

  assert.equal(out.ok, true);
  const discardGaps = out.gaps.filter((g) => g.startsWith('checkpoint-discarded:'));
  assert.equal(discardGaps.length, 1, `expected exactly one checkpoint-discarded: gap, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(!discardGaps[0].includes('truncated'), discardGaps[0]);
  assert.ok(!discardGaps[0].includes('artifactPaths.checkpoints'), discardGaps[0]);
  assert.ok(discardGaps[0].includes('re-run without'), discardGaps[0]);
});

// Truthiness, not key-presence: `truncated: false` sitting right next to `completed` must NOT
// be misread as the truncated sub-case just because the key exists on the resolved map.
test('runWith: {completed: [], truncated: false} takes the empty-completed sub-case, not the truncated one (truthiness, not key-presence)', async () => {
  const args = validArgs({ checkpoints: { completed: [], truncated: false } });
  const out = await runWith(makeCtx(args), args);

  assert.equal(out.ok, true);
  const discardGaps = out.gaps.filter((g) => g.startsWith('checkpoint-discarded:'));
  assert.equal(discardGaps.length, 1, `expected exactly one checkpoint-discarded: gap, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(!discardGaps[0].includes('truncated'), discardGaps[0]);
  assert.ok(!discardGaps[0].includes('artifactPaths.checkpoints'), discardGaps[0]);
});

test('runWith: {foo: 1} discloses the eight-recognized-names sub-case', async () => {
  const args = validArgs({ checkpoints: { foo: 1 } });
  const out = await runWith(makeCtx(args), args);

  assert.equal(out.ok, true);
  const discardGaps = out.gaps.filter((g) => g.startsWith('checkpoint-discarded:'));
  assert.equal(discardGaps.length, 1, `expected exactly one checkpoint-discarded: gap, got: ${JSON.stringify(out.gaps)}`);
  for (const phase of ALL_PHASES) assert.ok(discardGaps[0].includes(phase), `expected ${phase} named in: ${discardGaps[0]}`);
  // Never echo the caller's own key byte-for-byte -- the message is fixed vocabulary.
  assert.ok(!discardGaps[0].includes('foo'), discardGaps[0]);
});

test('runWith: {phases: {}, challenge: <well-formed challenge output>} discloses the empty-phases-masking sub-case (arm B), ok:true, challenge re-dispatched', async () => {
  const args = validArgs({
    checkpoints: { phases: {}, challenge: wellFormedCheckpoints().challenge },
  });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  const discardGaps = out.gaps.filter((g) => g.startsWith('checkpoint-discarded:'));
  assert.equal(discardGaps.length, 1, `expected exactly one checkpoint-discarded: gap, got: ${JSON.stringify(out.gaps)}`);
  assert.ok(discardGaps[0].includes('checkpoints.phases'), discardGaps[0]);
  assert.ok(discardGaps[0].includes('empty'), discardGaps[0]);
  // The top-level `challenge` value was ignored, not replayed -- the phase RE-DISPATCHES.
  assert.ok(
    ctx.calls.some((c) => (c.label || '').startsWith('challenge-')),
    `expected a challenge-labeled dispatch (re-run, not replay), got labels: ${JSON.stringify(ctx.calls.map((c) => c.label))}`,
  );
});

// --- #270 silence: every case that must stay gap-free ------------------------

test('runWith: silence loop over the hard-coded ALL_PHASES const -- a single recognized phase key never discloses checkpoint-discarded:', async () => {
  for (const phase of ALL_PHASES) {
    const args = validArgs({ checkpoints: { [phase]: wellFormedCheckpoints()[phase] } });
    const out = await runWith(makeCtx(args), args);
    assert.deepEqual(
      out.gaps.filter((g) => g.startsWith('checkpoint-discarded:')),
      [],
      `phase ${phase} unexpectedly disclosed: ${JSON.stringify(out.gaps)}`,
    );
  }
});

test('runWith: {} discloses no checkpoint-discarded: gap', async () => {
  const args = validArgs({ checkpoints: {} });
  const out = await runWith(makeCtx(args), args);
  assert.deepEqual(out.gaps.filter((g) => g.startsWith('checkpoint-discarded:')), []);
});

test('runWith: {phases: {}} (no top-level phase names) discloses no checkpoint-discarded: gap', async () => {
  const args = validArgs({ checkpoints: { phases: {} } });
  const out = await runWith(makeCtx(args), args);
  assert.deepEqual(out.gaps.filter((g) => g.startsWith('checkpoint-discarded:')), []);
});

test('runWith: {phases: {}, completed: []} (buildResumeCheckpoints({}), the real first-phase-crash resume) discloses no checkpoint-discarded: gap', () => {
  const resumed = buildResumeCheckpoints({});
  assert.deepEqual(resumed, { phases: {}, completed: [] });
  return (async () => {
    const args = validArgs({ checkpoints: resumed });
    const out = await runWith(makeCtx(args), args);
    assert.deepEqual(out.gaps.filter((g) => g.startsWith('checkpoint-discarded:')), []);
  })();
});

test('runWith: checkpoints: null (alongside the untouched F4-3 test) discloses no checkpoint-discarded: gap', async () => {
  const args = validArgs({ checkpoints: null });
  const out = await runWith(makeCtx(args), args);
  assert.deepEqual(out.gaps.filter((g) => g.startsWith('checkpoint-discarded:')), []);
});

test('runWith: a map with one real phase plus one unknown key discloses no checkpoint-discarded: gap', async () => {
  const args = validArgs({ checkpoints: { summarize: wellFormedCheckpoints().summarize, foo: 1 } });
  const out = await runWith(makeCtx(args), args);
  assert.deepEqual(out.gaps.filter((g) => g.startsWith('checkpoint-discarded:')), []);
});

// The PRIMARY legitimate resume this whole gate exists to leave alone: the actual shape
// slimPersistedCheckpoints produces and the skill feeds straight back in on a real resume
// (`{phases, completed, phaseReached, counts}`). Arm A's "recognized key" check must scan
// the RESOLVED `.phases` map, not the wrapper's own top-level keys (which are 'phases',
// 'completed', 'phaseReached', 'counts' -- none of them a phase name) -- getting that wrong
// would misfire #270 on every real resume.
test('runWith: a slimPersistedCheckpoints-shaped resume (the actual legitimate producer) discloses no checkpoint-discarded: gap and REPLAYS challenge (no re-dispatch)', async () => {
  const resumed = slimPersistedCheckpoints(
    { challenge: wellFormedCheckpoints().challenge },
    ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge'],
    'challenge',
  );
  assert.ok(Object.keys(resumed.phases).includes('challenge'), 'fixture sanity: slimPersistedCheckpoints kept .phases.challenge');
  const args = validArgs({ checkpoints: resumed });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.deepEqual(out.gaps.filter((g) => g.startsWith('checkpoint-discarded:')), []);
  assert.ok(
    !ctx.calls.some((c) => (c.label || '').startsWith('challenge-')),
    `expected challenge to be REPLAYED (no dispatch), got labels: ${JSON.stringify(ctx.calls.map((c) => c.label))}`,
  );
});

// Partial masking (a non-empty `.phases` resume sitting next to an ignored top-level
// sibling) is a deliberate NON-GOAL of arm B: there is no legitimate producer of this shape,
// and the phase inside `.phases` really is being resumed, not silently dropped.
test('runWith: a real .phases resume plus an ignored top-level sibling discloses no checkpoint-discarded: gap (partial masking is a non-goal)', async () => {
  const args = validArgs({
    checkpoints: { phases: { challenge: wellFormedCheckpoints().challenge }, summarize: { gaps: [] } },
  });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.deepEqual(out.gaps.filter((g) => g.startsWith('checkpoint-discarded:')), []);
  assert.ok(
    !ctx.calls.some((c) => (c.label || '').startsWith('challenge-')),
    `expected challenge to be REPLAYED from .phases (no dispatch), got labels: ${JSON.stringify(ctx.calls.map((c) => c.label))}`,
  );
});

// --- #270 exactly-once / exclusivity -----------------------------------------

test('runWith: checkpoints: [] still discloses exactly one gap (the unwrap-fail message, not a #270 arm)', async () => {
  const args = validArgs({ checkpoints: [] });
  const out = await runWith(makeCtx(args), args);
  const discardGaps = out.gaps.filter((g) => g.startsWith('checkpoint-discarded:'));
  assert.equal(discardGaps.length, 1, JSON.stringify(out.gaps));
  assert.ok(discardGaps[0].includes('must be a plain object'), discardGaps[0]);
});

test('runWith: args.checkpoints "garbage" + a ctx.checkpoints fallback of {completed: []} discloses exactly one gap, the got-string unwrap-fail message', async () => {
  const args = validArgs({ checkpoints: 'garbage' });
  const ctx = { ...makeCtx(args), checkpoints: { completed: [] } };
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  const discardGaps = out.gaps.filter((g) => g.startsWith('checkpoint-discarded:'));
  assert.equal(discardGaps.length, 1, JSON.stringify(out.gaps));
  assert.ok(discardGaps[0].includes('got string'), discardGaps[0]);
});

test('runWith: {challenge: "x", completed: []} is a checkpoint-shape refusal with NO zero-recognized (#270) gap', async () => {
  const args = validArgs({ checkpoints: { challenge: 'x', completed: [] } });
  const out = await runWith(makeCtx(args), args);

  assert.equal(out.ok, false);
  assert.equal(out.failingPhase, 'checkpoints');
  assert.ok(out.gaps.some((g) => g.startsWith('checkpoint-shape:') && g.includes('challenge')));
  assert.deepEqual(out.gaps.filter((g) => g.startsWith('checkpoint-discarded:')), []);
});

// --- #270 G3-sentinel guard ---------------------------------------------------
//
// Same reasoning as the existing `checkpoint-discarded:`/`checkpoint-shape:` rows in
// docs/machine-parsed-strings.md: bench/runner/check.py's G3 `_DEGRADE_RE` matches on
// these exact two substrings, case-insensitively, to detect a degraded-but-undisclosed
// run. All #270 message text is FIXED pipeline-authored vocabulary (never the caller's own
// key bytes), so neither forbidden substring can ever appear -- but a future edit could
// still introduce one, so pin it directly against every #270 message text this suite drives.
test('runWith: no #270 checkpoint-discarded: gap ever contains a G3 sentinel substring', async () => {
  const scenarios = [
    { completed: [], truncated: true },
    { completed: ['summarize'] },
    { foo: 1 },
    { phases: {}, challenge: wellFormedCheckpoints().challenge },
  ];
  const seen = [];
  for (const checkpoints of scenarios) {
    const args = validArgs({ checkpoints });
    const out = await runWith(makeCtx(args), args);
    const discardGaps = out.gaps.filter((g) => g.startsWith('checkpoint-discarded:'));
    assert.equal(discardGaps.length, 1, `scenario ${JSON.stringify(checkpoints)} produced: ${JSON.stringify(out.gaps)}`);
    seen.push(...discardGaps);
  }
  assert.ok(seen.length >= scenarios.length);
  for (const gap of seen) {
    assert.doesNotMatch(gap.toLowerCase(), /no write proof|partial-artifacts/, `forbidden substring in: ${gap}`);
  }
});
