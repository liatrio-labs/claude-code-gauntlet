import { test } from 'node:test';
import assert from 'node:assert/strict';
import { verifyStage } from '../src/verifyStage.js';
import { deltaEnvelope, deltasFor, ELIMINATION_STAMP, verifyCtx, verifyInput } from './helpers/verifyDelta.js';
import { outsideSingleQuotes, shellSplit } from './helpers/shellWords.js';

const BASE = [
  { id: 'F1', file: 'a.js', line_start: 1, origin: 'new', dimension: 'bug', cross_file_refs: [], agent: 'bug-detector' },
  { id: 'F2', file: 'b.js', line_start: 2, origin: 'new', dimension: 'security', cross_file_refs: ['c.js:9'], agent: 'bug-detector' },
];
const findingsFor = (n) => Array.from({ length: n }, (_, k) => ({
  id: `F${k}`, file: `f${k}.js`, line_start: k + 1, line_end: k + 1,
  title: `t${k}`, description: `d${k}`, severity: 'high', confidence: 80,
  dimension: 'bug', origin: 'new', evidence: `e${k}`, cross_file_refs: [], agent: 'bug-detector',
}));
const ZERO = {
  slices: 0, proven: 0, mismatched: 0, missing: 0, unprovable: 0, oversize: 0,
  retried: 0, retriedMismatch: 0, retriedMissing: 0,
};
const commandOf = (call) => call.prompt.split('\n').pop();
const argvOf = (call) => shellSplit(commandOf(call));
const inlineOf = (call) => {
  const argv = argvOf(call);
  return argv[argv.indexOf('--input-inline') + 1];
};

const TRUSTED_ROWS = [
  { name: 'dispatched slice document carries pinned numerics and no undeclared field',
    findings: [{ id: 'F1', file: 'a.js', line_start: 3.5, confidence: 90.2, origin: 'new', title: 'undeclared', suggestion: 'undeclared' }],
    changes: { line_start: 4, confidence: 90 },
    inline: '{"findings":[{"id":"F1","file":"a.js","line_start":4,"confidence":90,"origin":"new"}],"base_branch":"main"}' },
  { name: 'reordered deltas preserve dispatched finding order', findings: findingsFor(3),
    options: (f) => ({ deltas: deltasFor(f).reverse() }) },
  { name: 'stamped elimination removes only the eliminated finding', findings: findingsFor(3),
    options: () => ({ overrides: { F1: { verified: false, elimination_reason: ELIMINATION_STAMP } } }), ids: ['F0', 'F2'] },
  { name: 'hidden_errors and untouched extras survive script decisions', findings: [{ ...findingsFor(1)[0],
    suggestion: 'guard the null case', claude_md_rule: 'stdlib-only',
    hidden_errors: 'AttributeError on the API-key path', cross_file_refs: ['other.js:9'], description: 'x'.repeat(480) }],
    options: () => ({ overrides: { F0: { origin: 'surfaced', severity: 'medium', confidence: 55 } } }),
    changes: { origin: 'surfaced', severity: 'medium', confidence: 55 } },
];
for (const row of TRUSTED_ROWS) {
  test(`trusted: ${row.name}`, async () => {
    const ctx = verifyCtx(() => {
      const env = deltaEnvelope(row.findings, { nonce: 'n-1.0', ...row.options?.(row.findings) });
      return env;
    });
    const expected = row.findings.filter((f) => !row.ids || row.ids.includes(f.id)).map((f) => ({ ...f, ...row.changes }));
    const out = await verifyStage(ctx, verifyInput(row.findings));
    assert.equal(out.verified, true);
    assert.deepEqual(out.findings, expected);
    assert.deepEqual(out.gaps, []);
    assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, proven: 1 });
    assert.deepEqual(ctx.calls.map((call) => [call.label, call.agentType]), [['verify-slice-0', 'code-gauntlet:executor']]);
    if (row.inline) assert.equal(inlineOf(ctx.calls[0]), row.inline);
  });
}

const degradedGap = (reason, first = reason, slice, affected, total) =>
  `verify: UNVERIFIED \u2014 slice ${slice}: ${reason} \u2014 retried once after the first attempt failed (${first}); `
  + `${affected} of ${total} finding(s) marked origin=unknown, surfaced-classification skipped`;

const UNTRUSTED_ROWS = [
  { name: 'omitted id keeps the uncovered finding', count: 3,
    options: (f) => ({ deltas: deltasFor(f).slice(0, 2), ids: ['F0', 'F1'] }),
    reason: 'delta does not cover 1 of 3 dispatched finding(s) (first: F2)',
  },
  { name: 'origin drift rejects the original script proof',
    options: (f) => ({ deltas: deltasFor(f, { F0: { origin: 'surfaced' } }), checksum: 'fnv1a32:0xc35f3d60' }),
    reason: 'delta content proof mismatch (receipt fnv1a32:0xc35f3d60, recomputed fnv1a32:0x151174e3) \u2014 the echoed values are not the ones the script wrote',
  },
  { name: 'absent delta proof is untrusted',
    mutate: (env) => { delete env.receipt.deltas_checksum; },
    reason: 'receipt carries no deltas_checksum (content proof missing)',
  },
  { name: 'fabricated elimination keeps the claimed eliminated finding',
    options: () => ({ overrides: { F1: { verified: false } } }),
    reason: 'delta F1: eliminated without the elimination_reason stamp (fabricated elimination \u2014 the verify script always stamps a real one)',
  },
  { name: 'missing delta array is untrusted',
    mutate: (env) => { env.result = {}; },
    reason: 'result missing deltas array',
  },
  { name: 'failed executor preserves original agent and findings',
    envelope: () => ({ status: 'failed', exitCode: 1, stderr: 'boom' }),
    reason: 'status=failed (boom)',
  },
  { name: 'null envelope is retried and disclosed', envelope: () => null, reason: 'executor returned no envelope' },
  { name: 'wrong receipt sha is untrusted',
    options: () => ({ sha: 'wrong' }),
    reason: 'receipt sha mismatch (got wrong)',
  },
  { name: 'wrong receipt input count is untrusted',
    options: () => ({ n_in: 1 }),
    reason: 'receipt n_in mismatch (got 1, expected 2)',
  },
  { name: 'executor throw is retried and disclosed',
    envelope: () => { throw new Error('boom'); },
    reason: 'executor threw (boom)',
  },
  { name: 'wrong nonce degrades only its slice and retries once', count: 5, over: { limits: { verifySliceSize: 2 } },
    envelope: (findings, index) => deltaEnvelope(findings.slice(index * 2, index * 2 + 2), { nonce: ['n-1.0', 'WRONG', 'n-1.2'][index] }),
    origins: ['new', 'new', 'unknown', 'unknown', 'new'], ledger: { ...ZERO, slices: 3, proven: 2 },
    labels: ['verify-slice-0', 'verify-slice-1', 'verify-slice-1-retry', 'verify-slice-2'],
    nonces: ['n-1.0', 'n-1.1', 'n-1.1.r1', 'n-1.2'],
    inputs: [
      '/out/phase4-input-abc123.slice0.json', '/out/phase4-input-abc123.slice1.json',
      '/out/phase4-input-abc123.slice1.json', '/out/phase4-input-abc123.slice2.json',
    ],
    outputs: [
      '/out/phase4-output-abc123.slice0.json', '/out/phase4-output-abc123.slice1.json',
      '/out/phase4-output-abc123.slice1.json', '/out/phase4-output-abc123.slice2.json',
    ],
    reason: 'receipt nonce mismatch (got WRONG, expected n-1.1.r1)',
    firstReason: 'receipt nonce mismatch (got WRONG, expected n-1.1)',
    slice: 1,
  },
];
for (const row of UNTRUSTED_ROWS) {
  test(`untrusted: ${row.name}`, async () => {
    const findings = findingsFor(row.count || 2);
    const ctx = verifyCtx((index, attempt) => {
      if (row.envelope) return row.envelope(findings, index, attempt);
      const env = deltaEnvelope(findings, { nonce: attempt === 1 ? 'n-1.0' : 'n-1.0.r1', ...row.options?.(findings) });
      row.mutate?.(env);
      return env;
    });
    const input = verifyInput(findings, row.over);
    const out = await verifyStage(ctx, input);
    assert.equal(out.verified, false);
    assert.deepEqual(out.findings, findings.map((f, i) => ({ ...f, origin: row.origins?.[i] || 'unknown' })));
    assert.deepEqual(ctx.calls.map((call) => call.label), row.labels || ['verify-slice-0', 'verify-slice-0-retry']);
    assert.deepEqual(out.inputProof, row.ledger || { ...ZERO, slices: 1 });
    if (row.nonces) assert.deepEqual(ctx.calls.map((call) => { const argv = argvOf(call); return argv[argv.indexOf('--nonce') + 1]; }),
      row.nonces);
    if (row.inputs) assert.deepEqual(ctx.calls.map((call) => { const argv = argvOf(call); return argv[argv.indexOf('--input') + 1]; }),
      row.inputs);
    if (row.outputs) {
      const outputs = ctx.calls.map((call) => { const argv = argvOf(call); return argv[argv.indexOf('--output') + 1]; });
      assert.deepEqual(outputs, row.outputs);
      assert.equal(outputs[2], outputs[1], 'retry reuses the first attempt output path');
    }
    assert.deepEqual(out.gaps, [degradedGap(row.reason, row.firstReason, row.slice || 0, row.origins ? 2 : findings.length, findings.length)]);
  });
}

test('missing id degrades without dispatch', async () => {
  const findings = [
    findingsFor(2)[0],
    { ...findingsFor(2)[1], id: undefined },
  ];
  const ctx = verifyCtx(() => { throw new Error('must not dispatch'); });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.equal(out.verified, false);
  assert.deepEqual(ctx.calls, []);
  assert.deepEqual(out.findings, findings.map((f) => ({ ...f, origin: 'unknown' })));
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 1 });
  assert.deepEqual(out.gaps, [
    'verify: UNVERIFIED \u2014 slice 0: a dispatched finding has no usable id \u2014 the delta echo is keyed by '
      + 'id, so this slice cannot be verified; 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped'
  ]);
});

const BAD_PROOF = 'fnv1a32:0xdeadbeef';
const LEDGER_ROWS = [
  { name: 'missing content proof degrades after the fresh retry',
    receipt: () => ({ input_checksum: null }),
    missing: 1,
    reason: 'input content proof missing from receipt',
  },
  { name: 'wrong content proof recovers on a fresh attempt',
    receipt: (attempt) => attempt === 1 ? { input_checksum: BAD_PROOF } : {},
    trusted: true,
    retriedMismatch: 1,
    reason: 'slice-input content proof mismatch (receipt fnv1a32:0xdeadbeef, dispatched fnv1a32:0x693ddb36) \u2014 the document the script decoded is not the document this stage dispatched',
  },
  { name: 'wrong token proof precedes a wrong content proof',
    receipt: () => ({ inline_checksum: BAD_PROOF, input_checksum: BAD_PROOF }),
    mismatched: 1,
    reason: 'slice-input token proof mismatch (receipt fnv1a32:0xdeadbeef, dispatched fnv1a32:0xc69f8ec8) \u2014 the token the script decoded is not the token this stage dispatched',
  },
  { name: 'missing token proof recovers on a fresh attempt',
    receipt: (attempt) => attempt === 1 ? { inline_checksum: null } : {},
    trusted: true,
    retriedMissing: 1,
    reason: 'inline token proof missing from receipt',
  },
  { name: 'empty input emits the complete zero ledger', empty: true },
  { name: 'wrong delta proof recovers with literal retry nonce',
    deltaFault: true,
    trusted: true,
    reason: 'delta content proof mismatch (receipt fnv1a32:0xdeadbeef, recomputed fnv1a32:0xdca84eaa) \u2014 the echoed values are not the ones the script wrote',
  },
];
for (const row of LEDGER_ROWS) {
  test(`input-proof ledger: ${row.name}`, async () => {
    const ctx = verifyCtx((_index, attempt) => {
      if (row.empty) throw new Error('must not dispatch');
      const env = deltaEnvelope(BASE, { nonce: attempt === 1 ? 'n-1.0' : 'n-1.0.r1',
        ...(row.deltaFault && attempt === 1 ? { checksum: BAD_PROOF } : {}) });
      Object.assign(env.receipt, row.receipt?.(attempt));
      return env;
    });
    const out = await verifyStage(ctx, verifyInput(row.empty ? [] : BASE));
    assert.equal(out.verified, !!row.trusted || !!row.empty);
    assert.deepEqual(out.findings, row.empty ? [] : BASE.map((f) => ({ ...f, origin: row.trusted ? 'new' : 'unknown' })));
    assert.deepEqual(out.inputProof, row.empty ? ZERO : {
      ...ZERO, slices: 1, proven: row.trusted ? 1 : 0,
      missing: row.missing || 0, mismatched: row.mismatched || 0, retried: row.trusted ? 1 : 0,
      retriedMissing: row.retriedMissing || 0, retriedMismatch: row.retriedMismatch || 0,
    });
    assert.deepEqual(ctx.calls.map((call) => call.label), row.empty ? [] : ['verify-slice-0', 'verify-slice-0-retry']);
    assert.deepEqual(out.gaps, row.empty ? [] : [row.trusted
      ? `verify-slice-retry: slice 0's first executor dispatch was untrusted (${row.reason}); a second dispatch was trusted and this slice's verified findings are from that attempt`
      : degradedGap(row.reason, undefined, 0, 2, 2)]);
  });
}
const bulk = (prefix, length, chars) => Array.from({ length }, (_, i) => ({
  id: `${prefix}${i}`, file: `f${i}.js`, line_start: 1, line_end: 1, description: 'x'.repeat(chars),
  evidence: 'e', severity: 'high', confidence: 90, cross_file_refs: [], origin: 'new',
}));
const FANOUT_ROWS = [
  { name: 'count bound advises raising slice size', findings: bulk('COUNT', 130, 800),
    gap: ('verify_fanout: effective verifySliceSize=25 splits 130 finding(s) into 6 slices (above the 5-slice '
      + 'disclosure threshold) \u2014 up to 12 executor dispatches at 2 attempts per slice. Raise verifySliceSize to '
      + 'reduce fan-out.') },
  { name: 'budget bound advises that raising slice size will not help', findings: bulk('FAT', 60, 4100),
    gap: ('verify_fanout: effective verifySliceSize=25 splits 60 finding(s) into 6 slices (above the 5-slice '
      + 'disclosure threshold) \u2014 up to 12 executor dispatches at 2 attempts per slice. The inline character '
      + 'budget bound this split; raising verifySliceSize will not reduce this fan-out.') },
];
for (const row of FANOUT_ROWS) {
  test(`fan-out: ${row.name}`, async () => {
    const ctx = verifyCtx((index, _attempt, call) => deltaEnvelope(JSON.parse(inlineOf(call)).findings, { nonce: ['n-1.0', 'n-1.1',
      'n-1.2', 'n-1.3', 'n-1.4', 'n-1.5'][index] }));
    const out = await verifyStage(ctx, verifyInput(row.findings, { limits: { verifySliceSize: 25 } }));
    assert.deepEqual(out.gaps.filter((gap) => gap.startsWith('verify_fanout:')), [row.gap]);
    assert.equal(out.inputProof.oversize, 0);
  });
}

test('sibling-slice substitution keeps every finding and degrades only the substituted slice', async () => {
  const findings = findingsFor(4);
  const ctx = verifyCtx((index, attempt) => {
    if (index === 0 && attempt === 1) return { status: 'failed', exitCode: 1, stderr: 'transient' };
    return deltaEnvelope(findings.slice(2, 4), { nonce: index === 0 ? 'n-1.0.r1' : 'n-1.1', n_in: 2 });
  });
  const out = await verifyStage(ctx, verifyInput(findings, { limits: { verifySliceSize: 2 } }));
  assert.equal(out.verified, false);
  assert.deepEqual(out.findings.map((f) => [f.id, f.origin]), [['F0', 'unknown'], ['F1', 'unknown'], ['F2', 'new'], ['F3', 'new']]);
  assert.deepEqual(out.gaps, [('verify: UNVERIFIED \u2014 slice 0: delta names a finding this slice did not dispatch '
    + '(F2) \u2014 retried once after the first attempt failed (status=failed (transient)); 2 of 4 finding(s) marked '
    + 'origin=unknown, surfaced-classification skipped')]);
});

test('shell quoting preserves paths with spaces, apostrophes and special branch names', async () => {
  const ctx = verifyCtx(() => deltaEnvelope(BASE, { nonce: 'n-1.0' }));
  await verifyStage(ctx, verifyInput(BASE, { verify: {
    scriptPath: '/plug in/scripts/verify_findings.py', inputPathBase: '/My Documents/out/phase4-input-abc123',
    outputPathBase: '/My Documents/out/phase4-output-abc123', baseBranch: 'feature/$x-`y`',
    diffPath: "/Users/o'brien/out/code-gauntlet diff.patch",
  } }));
  assert.deepEqual(argvOf(ctx.calls[0]), [
    'python3', '/plug in/scripts/verify_findings.py', '--input', '/My Documents/out/phase4-input-abc123.slice0.json',
    '--input-inline', inlineOf(ctx.calls[0]), '--output', '/My Documents/out/phase4-output-abc123.slice0.json',
    '--nonce', 'n-1.0', '--head-sha', 'abc123', '--base-branch', 'feature/$x-`y`',
    '--diff-file', "/Users/o'brien/out/code-gauntlet diff.patch",
  ]);
  assert.doesNotMatch(outsideSingleQuotes(commandOf(ctx.calls[0])), /[$`]/);
});

test('oversize finding is disclosed without dispatch and leaves the next slice nonce intact', async () => {
  const findings = [
    { id: 'BIG', file: 'a.js', line_start: 1, origin: 'new', cross_file_refs: [], description: 'x'.repeat(50000) },
    { id: 'OK', file: 'b.js', line_start: 2, origin: 'new', cross_file_refs: [] },
  ];
  const ctx = verifyCtx(() => deltaEnvelope([findings[1]], { nonce: 'n-1.0', n_in: 1 }));
  const out = await verifyStage(ctx, verifyInput(findings, { limits: { verifySliceSize: 2 } }));
  assert.equal(out.verified, false);
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, proven: 1, oversize: 1 });
  assert.deepEqual(ctx.calls.map((call) => call.label), ['verify-slice-0']);
  assert.deepEqual(out.findings.map((f) => [f.id, f.origin]), [['BIG', 'unknown'], ['OK', 'new']]);
  assert.deepEqual(out.gaps, [('verify: UNVERIFIED \u2014 finding BIG has encoded length 50130, over '
    + 'VERIFY_INLINE_CHAR_BUDGET=50000; 1 of 2 finding(s) marked origin=unknown, surfaced-classification skipped')]);
});
