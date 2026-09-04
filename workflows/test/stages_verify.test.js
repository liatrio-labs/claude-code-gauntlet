// Verify-stage contracts: the slice document crosses the executor boundary as one
// percent-encoded argv token. The executor echoes only receipt/delta data; the stage
// joins trusted deltas onto its own findings and degrades only an untrusted slice.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  encodeInlineString,
  encodeSliceInline,
  planVerifySlices,
  projectVerifySliceFinding,
  verifyStage,
  VERIFY_ATTEMPTS_PER_SLICE,
  VERIFY_INLINE_SAFE,
  VERIFY_INLINE_CHAR_BUDGET,
  VERIFY_SLICE_FIELDS,
} from '../src/stages.js';
import { assertPrompt, assertValidSchema } from './helpers/pipelineMock.js';
import { deltaEnvelope, ELIMINATION_STAMP, sliceInputRecorder } from './helpers/verifyDelta.js';
import { outsideSingleQuotes, shellSplit } from './helpers/shellWords.js';
import { FINDING_PROP_TYPES } from '../src/registry.js';

function verifyCtx(agentImpl) {
  const calls = [];
  let inParallel = 0;
  const rec = sliceInputRecorder();
  const agent = async (prompt, opts = {}) => {
    assertPrompt(prompt);
    assertValidSchema(opts.schema);
    const call = { prompt, ...opts };
    calls.push(call);
    const label = opts.label || '';
    if (label.startsWith('verify-slice-')) {
      if (inParallel > 0) throw new Error('verifyStage must not use parallel()');
      const match = /^verify-slice-(\d+)(-retry)?$/.exec(label);
      const index = match ? Number(match[1]) : -1;
      const attempt = match && match[2] ? 2 : 1;
      return rec.stamp(await agentImpl(call, index, { attempt }), index, prompt);
    }
    return agentImpl(call, -1, { attempt: 1 });
  };
  return {
    calls,
    execCalls: () => calls.filter((call) => String(call.label).startsWith('verify-slice-')),
    execCallsFor: (i) => calls.filter((call) => new RegExp(`^verify-slice-${i}(-retry)?$`).test(call.label || '')),
    agent,
    parallel: async (thunks) => {
      inParallel += 1;
      try {
        return await Promise.all(thunks.map(async (thunk) => {
          try { return await thunk(); } catch { return null; }
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

function okEnvelope(findings, opts = {}) {
  return deltaEnvelope(findings, opts);
}

const commandOf = (call) => call.prompt.split('\n').pop();
const argvOf = (call) => shellSplit(commandOf(call));
const inlineOf = (call) => {
  const argv = argvOf(call);
  return argv[argv.indexOf('--input-inline') + 1];
};
const parsedInlineOf = (call) => JSON.parse(inlineOf(call));

test('inline encoder passes the safe alphabet and encodes every unsafe class', () => {
  const safe = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,:/_-';
  assert.equal(VERIFY_INLINE_SAFE, safe);
  assert.equal(encodeInlineString(safe), safe);
  assert.equal(encodeInlineString(''), '');
  assert.equal(encodeInlineString("'\\\"`%\n\r\t\0"), '%27%5C%22%60%25%0A%0D%09%00');
  assert.equal(encodeInlineString('—'), '%E2%80%94');
  assert.equal(encodeInlineString('😀'), '%F0%9F%98%80');
  assert.equal(encodeInlineString(String.fromCharCode(0xD800)), '%uD800');
  assert.equal(encodeInlineString(String.fromCharCode(0xDC00)), '%uDC00');

  const forbidden = [
    '$', '`', ';', '(', ')', '&', '|', '<', '>', '#', '*', '?', '!', '~', '=', '+',
    "'", '"', '\\', '%', '\n', '\r', '\t',
  ];
  for (const char of forbidden) {
    const hex = char.charCodeAt(0).toString(16).toUpperCase().padStart(2, '0');
    assert.equal(encodeInlineString(char), `%${hex}`, JSON.stringify(char));
  }
});

test('inline encoder deep-walks values and keys, preserves primitives, and is shell-safe', () => {
  const content = { 'clé': { '💥': "a'b\\c\n" }, list: [null, true, 7], text: '%41' };
  const encoded = encodeSliceInline(content);
  assert.match(encoded, /^[\x20-\x26\x28-\x7E]*$/);
  assert.doesNotMatch(encoded, /'/);
  assert.deepEqual(JSON.parse(encoded), {
    'cl%C3%A9': { '%F0%9F%92%A5': 'a%27b%5Cc%0A' }, list: [null, true, 7], text: '%2541',
  });
});

test('inline encoder postcondition rejects a custom toJSON result containing an apostrophe', () => {
  const value = { toJSON: () => "'" };
  assert.throws(() => encodeSliceInline({ value }), /non-printable or quoted payload/);
});

test('valid receipt -> findings verified, and no writer dispatch exists', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_call, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.equal(out.findings.length, 2);
  assert.ok(ctx.calls.every((call) => call.agentType !== 'code-gauntlet:artifact-writer'));
  assert.ok(ctx.calls.every((call) => !String(call.label).startsWith('verify-input-writer-')));
  assert.equal(ctx.execCalls().length, 1);
});

test('inline command carries the exact encoded document as one shell word', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_call, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  await verifyStage(ctx, input);
  const call = ctx.execCalls()[0];
  const content = {
    findings: [
      { id: 'F1', file: 'a.js', line_start: 1, cross_file_refs: [], origin: 'new' },
      { id: 'F2', file: 'b.js', line_start: 2, cross_file_refs: ['c.js:9'], origin: 'new' },
    ],
    base_branch: 'main',
  };
  assert.equal(inlineOf(call), encodeSliceInline(content));
  assert.deepEqual(parsedInlineOf(call), content);
  assert.match(call.prompt, /--input-inline/);
  assert.match(call.prompt, /character for character with no line breaks/);
  assert.match(call.prompt, /the deltas carry a checksum/);
  assert.doesNotMatch(outsideSingleQuotes(commandOf(call)), /[$`]|&&|\|\|/);
});

test('projection drops undeclared fields, omits absent fields, and pins numbers', async () => {
  const finding = {
    id: 'F1', file: 'a.js', line_start: 3.5, line_end: 3, description: 'd', evidence: 'e',
    severity: 'high', confidence: 90.2, cross_file_refs: [], origin: 'new',
    agent: 'security', dimension: 'security', title: 'title', suggestion: 'fix',
  };
  const input = baseInput({ findings: [finding] });
  const ctx = verifyCtx((_call, i) => okEnvelope([finding], { nonce: `n-1.${i}` }));
  await verifyStage(ctx, input);
  const projected = parsedInlineOf(ctx.execCalls()[0]).findings[0];
  assert.deepEqual(Object.keys(projected), VERIFY_SLICE_FIELDS);
  assert.equal(projected.line_start, 4);
  assert.equal(projected.confidence, 90);
  for (const key of ['agent', 'dimension', 'title', 'suggestion']) assert.ok(!Object.hasOwn(projected, key));
  const absentInput = baseInput({ findings: [{ id: 'F1', file: 'a.js', line_start: 1, origin: 'new' }] });
  const absentCtx = verifyCtx((_call, i) => okEnvelope(absentInput.findings, { nonce: `n-1.${i}` }));
  await verifyStage(absentCtx, absentInput);
  const absent = parsedInlineOf(absentCtx.execCalls()[0]).findings[0];
  for (const key of ['line_end', 'description', 'evidence', 'cross_file_refs']) assert.ok(!Object.hasOwn(absent, key));
});

test('VERIFY_SLICE_FIELDS stays inside the closed finding schema', () => {
  for (const field of VERIFY_SLICE_FIELDS) assert.ok(Object.hasOwn(FINDING_PROP_TYPES, field));
});

test('wrong receipt nonce degrades only the affected slice and retries once', async () => {
  const findings = Array.from({ length: 5 }, (_, i) => ({ id: `F${i}`, origin: 'new' }));
  const input = baseInput({ findings, limits: { verifySliceSize: 2 } });
  const slices = [findings.slice(0, 2), findings.slice(2, 4), findings.slice(4)];
  const ctx = verifyCtx((_call, i) => {
    if (i === 1) return okEnvelope(slices[i], { nonce: 'WRONG', n_in: slices[i].length });
    return okEnvelope(slices[i], { nonce: `n-1.${i}`, n_in: slices[i].length });
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(ctx.execCallsFor(1).length, VERIFY_ATTEMPTS_PER_SLICE);
  assert.equal(out.findings.length, 5);
  assert.equal(out.findings[2].origin, 'unknown');
  assert.notEqual(out.findings[0].origin, 'unknown');
});

test('missing receipt checksum retries and then degrades with missing ledger count', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_call, i, { attempt }) => {
    const env = okEnvelope(input.findings, { nonce: attempt === 2 ? `n-1.${i}.r1` : `n-1.${i}` });
    env.receipt.input_checksum = null;
    return env;
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(ctx.execCallsFor(0).length, 2);
  assert.equal(out.inputProof.missing, 1);
  assert.equal(out.inputProof.retried, 0);
});

test('input checksum mismatch is retried and can succeed on the fresh attempt', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_call, i, { attempt }) => {
    if (attempt === 1) {
      const env = okEnvelope(input.findings, { nonce: `n-1.${i}` });
      env.receipt.input_checksum = 'fnv1a32:0xdeadbeef';
      return env;
    }
    return okEnvelope(input.findings, { nonce: `n-1.${i}.r1` });
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.equal(ctx.execCallsFor(0).length, 2);
  assert.equal(out.inputProof.mismatched, 0);
  assert.equal(out.inputProof.retried, 1);
  assert.equal(out.inputProof.retriedMismatch, 1);
  assert.equal(out.inputProof.retriedMissing, 0);
  assert.equal(out.gaps.length, 1);
  assert.match(out.gaps[0], /verify-slice-retry/);
});

test('missing first-attempt proof is counted on a trusted retry', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_call, i, { attempt }) => {
    const env = okEnvelope(input.findings, { nonce: `n-1.${i}${attempt === 2 ? '.r1' : ''}` });
    if (attempt === 1) env.receipt.input_checksum = null;
    return env;
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.equal(out.inputProof.missing, 0);
  assert.equal(out.inputProof.mismatched, 0);
  assert.equal(out.inputProof.retried, 1);
  assert.equal(out.inputProof.retriedMismatch, 0);
  assert.equal(out.inputProof.retriedMissing, 1);
});

test('two input checksum mismatches degrade after exactly the fresh retry', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_call, i, { attempt }) => {
    const env = okEnvelope(input.findings, { nonce: attempt === 2 ? `n-1.${i}.r1` : `n-1.${i}` });
    env.receipt.input_checksum = 'fnv1a32:0xdeadbeef';
    return env;
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, false);
  assert.equal(ctx.execCallsFor(0).length, 2);
  assert.equal(out.inputProof.mismatched, 1);
  assert.ok(out.findings.every((finding) => finding.origin === 'unknown'));
});

test('oversize inline finding is degraded alone, disclosed, and never dispatched', async () => {
  const findings = [
    { id: 'BIG', file: 'a.js', line_start: 1, origin: 'new', cross_file_refs: [], description: 'x'.repeat(50000) },
    { id: 'OK', file: 'b.js', line_start: 2, origin: 'new', cross_file_refs: [] },
  ];
  const input = baseInput({ findings, limits: { verifySliceSize: 2 } });
  const ctx = verifyCtx((_call, i) => okEnvelope([findings[1]], { nonce: `n-1.${i}`, n_in: 1 }));
  const out = await verifyStage(ctx, input);
  const planned = planVerifySlices(findings, input.limits.verifySliceSize, VERIFY_INLINE_CHAR_BUDGET, input.verify.baseBranch);
  assert.equal(out.inputProof.oversize, 1);
  assert.equal(out.inputProof.slices, planned.slices.length);
  assert.equal(ctx.execCalls().length, 1);
  assert.equal(ctx.execCalls()[0].label, 'verify-slice-0');
  assert.equal(out.findings[0].origin, 'unknown');
  assert.notEqual(out.findings[1].origin, 'unknown');
  assert.ok(out.gaps.some((gap) => /encoded length .*VERIFY_INLINE_CHAR_BUDGET=50000/.test(gap)));
});

test('unprovable numeric values stay trusted and are counted unprovable', async () => {
  const input = baseInput({ findings: baseInput().findings.map((finding) => ({ ...finding, line_start: Number.MAX_SAFE_INTEGER + 10 })) });
  const ctx = verifyCtx((_call, i) => {
    const env = okEnvelope(input.findings, { nonce: `n-1.${i}` });
    env.receipt.input_checksum = null;
    return env;
  });
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true);
  assert.equal(out.inputProof.unprovable, 1);
});

test('empty inputProof is the complete zero-populated ledger', async () => {
  const out = await verifyStage(verifyCtx(() => { throw new Error('must not dispatch'); }), { ...baseInput(), findings: [] });
  assert.deepEqual(out.inputProof, {
    slices: 0, proven: 0, mismatched: 0, missing: 0, unprovable: 0, oversize: 0,
    retried: 0, retriedMismatch: 0, retriedMissing: 0,
  });
});

test('verify fan-out disclosure names the bound that actually split the slices', async () => {
  const countFindings = Array.from({ length: 130 }, (_, i) => ({
    id: `COUNT${i}`, file: `f${i}.js`, line_start: 1, line_end: 1,
    description: 'x'.repeat(800), evidence: 'e', severity: 'high', confidence: 90,
    cross_file_refs: [], origin: 'new',
  }));
  const fatFindings = Array.from({ length: 60 }, (_, i) => ({
    id: `FAT${i}`, file: `f${i}.js`, line_start: 1, line_end: 1,
    description: 'x'.repeat(4000), evidence: 'e', severity: 'high', confidence: 90,
    cross_file_refs: [], origin: 'new',
  }));
  const run = async (findings) => {
    const input = baseInput({ findings, limits: { verifySliceSize: 25 } });
    const ctx = verifyCtx((call, i) => {
      const dispatched = parsedInlineOf(call).findings;
      return okEnvelope(dispatched, { nonce: `n-1.${i}`, n_in: dispatched.length });
    });
    return verifyStage(ctx, input);
  };
  const countGap = (await run(countFindings)).gaps.find((gap) => gap.startsWith('verify_fanout:'));
  const budgetGap = (await run(fatFindings)).gaps.find((gap) => gap.startsWith('verify_fanout:'));
  assert.match(countGap, /effective verifySliceSize=25/);
  assert.match(countGap, /Raise verifySliceSize to reduce fan-out/);
  assert.match(budgetGap, /effective verifySliceSize=25/);
  assert.match(budgetGap, /inline character budget bound/);
  assert.doesNotMatch(budgetGap, /Raise verifySliceSize to reduce fan-out/);
});

test('VERIFY_SCHEMA has no input_recovery and extra executor input_recovery is ignored', async () => {
  const input = baseInput();
  const ctx = verifyCtx((_call, i) => ({ ...okEnvelope(input.findings, { nonce: `n-1.${i}` }), input_recovery: { trailing_bytes: '}\n' } }));
  const out = await verifyStage(ctx, input);
  const call = ctx.execCalls()[0];
  assert.ok(!Object.hasOwn(call.schema.properties, 'input_recovery'));
  assert.ok(!out.gaps.some((gap) => /RECOVERED|input_recovery/.test(gap)));
  assert.equal(out.verified, true);
});

test('verified:false deltas need the real elimination stamp', async () => {
  const input = baseInput();
  const bad = verifyCtx((_call, i) => okEnvelope(input.findings, {
    nonce: `n-1.${i}`, overrides: { F2: { verified: false } },
  }));
  const out = await verifyStage(bad, input);
  assert.equal(out.verified, false);
  assert.ok(out.gaps.some((gap) => /elimination_reason|fabricated/.test(gap)));
  const good = verifyCtx((_call, i) => okEnvelope(input.findings, {
    nonce: `n-1.${i}`, overrides: { F2: { verified: false, elimination_reason: ELIMINATION_STAMP } },
  }));
  const trusted = await verifyStage(good, input);
  assert.equal(trusted.verified, true);
  assert.deepEqual(trusted.findings.map((finding) => finding.id), ['F1']);
});

test('status failure and null finding degrade without dropping the original', async () => {
  const input = baseInput();
  const failed = await verifyStage(verifyCtx(() => ({ status: 'failed', stderr: 'boom' })), input);
  assert.equal(failed.findings.length, 2);
  assert.ok(failed.findings.every((finding) => finding.origin === 'unknown'));
  const nullInput = baseInput({ findings: [null] });
  const nullCtx = verifyCtx(() => { throw new Error('must not dispatch null finding'); });
  const degraded = await verifyStage(nullCtx, nullInput);
  assert.equal(degraded.findings.length, 1);
  assert.equal(degraded.findings[0].origin, 'unknown');
  assert.equal(nullCtx.execCalls().length, 0);
});

test('shell quoting preserves paths with spaces and special branch names', async () => {
  const verify = {
    scriptPath: '/plug in/scripts/verify_findings.py',
    inputPathBase: '/My Documents/out/phase4-input-abc123',
    outputPathBase: '/My Documents/out/phase4-output-abc123',
    baseBranch: 'feature/$x-`y`',
    diffPath: "/Users/o'brien/out/code-gauntlet diff.patch",
  };
  const input = baseInput({ verify });
  const ctx = verifyCtx((_call, i) => okEnvelope(input.findings, { nonce: `n-1.${i}` }));
  await verifyStage(ctx, input);
  const argv = argvOf(ctx.execCalls()[0]);
  assert.equal(argv[1], verify.scriptPath);
  assert.equal(argv[argv.indexOf('--input') + 1], `${verify.inputPathBase}.slice0.json`);
  assert.equal(argv[argv.indexOf('--output') + 1], `${verify.outputPathBase}.slice0.json`);
  assert.equal(argv[argv.indexOf('--base-branch') + 1], verify.baseBranch);
  assert.equal(argv[argv.indexOf('--diff-file') + 1], verify.diffPath);
  assert.doesNotMatch(outsideSingleQuotes(commandOf(ctx.execCalls()[0])), /[$`]/);
});

test('planner is pure, count-bounded, budget-bounded, and isolates oversize findings', () => {
  const findings = [
    { id: 'A', file: 'a.js', line_start: 1, origin: 'new', cross_file_refs: [] },
    { id: 'B', file: 'b.js', line_start: 2, origin: 'new', cross_file_refs: [] },
    { id: 'C', file: 'c.js', line_start: 3, origin: 'new', cross_file_refs: [], description: 'z'.repeat(100) },
  ];
  const copy = JSON.parse(JSON.stringify(findings));
  const plan = planVerifySlices(findings, 2, 100000);
  assert.deepEqual(findings, copy);
  assert.deepEqual(plan.oversize, []);
  assert.deepEqual(plan.slices.map((slice) => slice.map((finding) => finding.id)), [['A', 'B'], ['C']]);
  const oversized = planVerifySlices([findings[0]], 2, 10);
  assert.deepEqual(oversized.slices, []);
  assert.deepEqual(oversized.oversize.map((finding) => finding.id), ['A']);
  for (const slice of plan.slices) {
    assert.ok(encodeSliceInline({ findings: slice.map(projectVerifySliceFinding), base_branch: 'main' }).length <= 100000);
  }
  assert.ok(VERIFY_INLINE_CHAR_BUDGET > 0);
});

test('planner flushes a preceding slice before isolating an oversize finding', () => {
  const findings = [
    { id: 'A', origin: 'new', cross_file_refs: [] },
    { id: 'BIG', origin: 'new', cross_file_refs: [], description: 'x'.repeat(50000) },
    { id: 'C', origin: 'new', cross_file_refs: [] },
  ];
  const plan = planVerifySlices(findings, 2, VERIFY_INLINE_CHAR_BUDGET);
  assert.deepEqual(plan.slices.map((slice) => slice.map((finding) => finding.id)), [['A'], ['C']]);
  assert.deepEqual(plan.oversize.map((finding) => finding.id), ['BIG']);
});
