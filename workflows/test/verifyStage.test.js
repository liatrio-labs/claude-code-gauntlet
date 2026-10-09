import { test } from 'node:test';
import assert from 'node:assert/strict';
import { verifyStage } from '../src/verifyStage.js';
import { planVerifySlices } from '../src/capacity.js';
import { projectVerifySliceFinding, sliceInputChecksum } from '../src/verifyWire.js';
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
  { name: 'plain receipt preserves agent and dispatches only one executor', findings: findingsFor(2) },
  { name: 'unsafe numbers remain trusted with an unprovable content proof',
    findings: BASE.map((f) => ({ ...f, line_start: Number.MAX_SAFE_INTEGER + 10 })),
    receipt: { input_checksum: null }, ledger: { ...ZERO, slices: 1, unprovable: 1 } },
  { name: 'reordered deltas preserve dispatched finding order', findings: findingsFor(3),
    options: (f) => ({ deltas: deltasFor(f).reverse() }) },
  { name: 'stamped elimination removes only the eliminated finding', findings: findingsFor(3),
    options: () => ({ overrides: { F1: { verified: false, elimination_reason: ELIMINATION_STAMP } } }), ids: ['F0', 'F2'] },
  { name: 'whitespace-padded ids remain distinct through trust and join',
    findings: [{ ...findingsFor(2)[0], id: 'F1' }, { ...findingsFor(2)[1], id: ' F1 ' }],
    options: () => ({ overrides: { ' F1 ': { verified: false, elimination_reason: ELIMINATION_STAMP } } }), ids: ['F1'] },
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
      Object.assign(env.receipt, row.receipt);
      return env;
    });
    const out = await verifyStage(ctx, verifyInput(row.findings));
    assert.equal(out.verified, true);
    const expected = row.findings.filter((f) => !row.ids || row.ids.includes(f.id)).map((f) => ({ ...f, ...row.changes }));
    assert.deepEqual(out.findings, expected);
    assert.deepEqual(out.gaps, []);
    assert.deepEqual(out.inputProof, row.ledger || { ...ZERO, slices: 1, proven: 1 });
    assert.deepEqual(ctx.calls.map((call) => [call.label, call.agentType]), [['verify-slice-0', 'code-gauntlet:executor']]);
    for (const f of out.findings) assert.equal(f.agent, 'bug-detector');
    if (row.changes) assert.equal(out.findings[0].hidden_errors, 'AttributeError on the API-key path');
  });
}

const UNTRUSTED_ROWS = [
  { name: 'omitted id keeps the uncovered finding', count: 3,
    options: (f) => ({ deltas: deltasFor(f).slice(0, 2), ids: ['F0', 'F1'] }),
    gap: 'verify: UNVERIFIED \u2014 slice 0: delta does not cover 1 of 3 dispatched finding(s) (first: F2) \u2014 retried once after the first attempt failed (delta does not cover 1 of 3 dispatched finding(s) (first: F2)); 3 of 3 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'undispatched id rejects a sibling answer',
    options: (f) => ({ deltas: [...deltasFor(f), { id: 'STRANGER', verified: true, origin: 'new' }] }),
    gap: 'verify: UNVERIFIED \u2014 slice 0: delta names a finding this slice did not dispatch (STRANGER) \u2014 retried once after the first attempt failed (delta names a finding this slice did not dispatch (STRANGER)); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'repeated delta id rejects the slice', options: (f) => ({ deltas: [...deltasFor(f), deltasFor(f)[0]] }),
    gap: 'verify: UNVERIFIED \u2014 slice 0: delta repeats a finding id (F0) \u2014 retried once after the first attempt failed (delta repeats a finding id (F0)); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'origin drift rejects the original script proof',
    options: (f) => ({ deltas: deltasFor(f, { F0: { origin: 'surfaced' } }), checksum: 'fnv1a32:0xc35f3d60' }),
    gap: 'verify: UNVERIFIED \u2014 slice 0: delta content proof mismatch (receipt fnv1a32:0xc35f3d60, recomputed fnv1a32:0x151174e3) \u2014 the echoed values are not the ones the script wrote \u2014 retried once after the first attempt failed (delta content proof mismatch (receipt fnv1a32:0xc35f3d60, recomputed fnv1a32:0x151174e3) \u2014 the echoed values are not the ones the script wrote); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'confidence drift rejects the original script proof', count: 1,
    options: (f) => ({ deltas: deltasFor(f, { F0: { confidence: 75 } }), checksum: 'fnv1a32:0x3c40d9ff' }),
    gap: 'verify: UNVERIFIED \u2014 slice 0: delta content proof mismatch (receipt fnv1a32:0x3c40d9ff, recomputed fnv1a32:0x6db20037) \u2014 the echoed values are not the ones the script wrote \u2014 retried once after the first attempt failed (delta content proof mismatch (receipt fnv1a32:0x3c40d9ff, recomputed fnv1a32:0x6db20037) \u2014 the echoed values are not the ones the script wrote); 1 of 1 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'absent delta proof is untrusted', mutate: (env) => { delete env.receipt.deltas_checksum; },
    gap: 'verify: UNVERIFIED \u2014 slice 0: receipt carries no deltas_checksum (content proof missing) \u2014 retried once after the first attempt failed (receipt carries no deltas_checksum (content proof missing)); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'fabricated elimination keeps the claimed eliminated finding',
    options: () => ({ overrides: { F1: { verified: false } } }),
    gap: 'verify: UNVERIFIED \u2014 slice 0: delta F1: eliminated without the elimination_reason stamp (fabricated elimination \u2014 the verify script always stamps a real one) \u2014 retried once after the first attempt failed (delta F1: eliminated without the elimination_reason stamp (fabricated elimination \u2014 the verify script always stamps a real one)); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'verified delta rejects an elimination stamp',
    options: () => ({ overrides: { F1: { elimination_reason: ELIMINATION_STAMP } } }),
    gap: 'verify: UNVERIFIED \u2014 slice 0: delta F1: verified finding carries an elimination_reason stamp \u2014 retried once after the first attempt failed (delta F1: verified finding carries an elimination_reason stamp); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'fractional delta confidence is untrusted', count: 1,
    options: () => ({ deltas: [{ id: 'F0', verified: true, origin: 'new', confidence: 82.5 }] }),
    gap: 'verify: UNVERIFIED \u2014 slice 0: delta F0: confidence is not an integer \u2014 retried once after the first attempt failed (delta F0: confidence is not an integer); 1 of 1 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'non-object delta is untrusted', count: 1,
    options: () => ({ deltas: ['not-an-object'], checksum: 'fnv1a32:0xdeadbeef' }), gap: 'verify: UNVERIFIED \u2014 slice 0: delta entry is not an object \u2014 retried once after the first attempt failed (delta entry is not an object); 1 of 1 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'blank delta id is untrusted', count: 1,
    options: () => ({ deltas: [{ id: '   ', verified: true, origin: 'new' }], checksum: 'fnv1a32:0xdeadbeef' }),
    gap: 'verify: UNVERIFIED \u2014 slice 0: delta entry has no id \u2014 retried once after the first attempt failed (delta entry has no id); 1 of 1 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'string verified flag is untrusted', count: 1,
    options: () => ({ deltas: [{ id: 'F0', verified: 'true', origin: 'new', severity: 'high', confidence: 80 }], checksum: 'fnv1a32:0xdeadbeef' }),
    gap: 'verify: UNVERIFIED \u2014 slice 0: delta F0 has no boolean verified flag \u2014 retried once after the first attempt failed (delta F0 has no boolean verified flag); 1 of 1 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'missing delta array is untrusted', mutate: (env) => { env.result = {}; }, gap: 'verify: UNVERIFIED \u2014 slice 0: result missing deltas array \u2014 retried once after the first attempt failed (result missing deltas array); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'failed executor preserves original agent and findings', envelope: () => ({ status: 'failed', exitCode: 1, stderr: 'boom' }),
    gap: 'verify: UNVERIFIED \u2014 slice 0: status=failed (boom) \u2014 retried once after the first attempt failed (status=failed (boom)); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'wrong receipt sha is untrusted', options: () => ({ sha: 'wrong' }), gap: 'verify: UNVERIFIED \u2014 slice 0: receipt sha mismatch (got wrong) \u2014 retried once after the first attempt failed (receipt sha mismatch (got wrong)); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'wrong receipt input count is untrusted', options: () => ({ n_in: 1 }), gap: 'verify: UNVERIFIED \u2014 slice 0: receipt n_in mismatch (got 1, expected 2) \u2014 retried once after the first attempt failed (receipt n_in mismatch (got 1, expected 2)); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'executor throw is retried and disclosed', envelope: () => { throw new Error('boom'); }, gap: 'verify: UNVERIFIED \u2014 slice 0: executor threw (boom) \u2014 retried once after the first attempt failed (executor threw (boom)); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'null envelope is retried and disclosed', envelope: () => null, gap: 'verify: UNVERIFIED \u2014 slice 0: executor returned no envelope \u2014 retried once after the first attempt failed (executor returned no envelope); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'non-string origin is untrusted', options: () => ({ overrides: { F0: { origin: 7 } } }), gap: 'verify: UNVERIFIED \u2014 slice 0: delta F0: origin is not a string \u2014 retried once after the first attempt failed (delta F0: origin is not a string); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'JSON string input preserves findings on failure', json: true, envelope: () => ({ status: 'failed', stderr: 'boom' }), gap: 'verify: UNVERIFIED \u2014 slice 0: status=failed (boom) \u2014 retried once after the first attempt failed (status=failed (boom)); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'wrong nonce degrades only its slice and retries once', count: 5, over: { limits: { verifySliceSize: 2 } },
    envelope: (findings, index) => deltaEnvelope(findings.slice(index * 2, index * 2 + 2), { nonce: ['n-1.0', 'WRONG', 'n-1.2'][index] }),
    origins: ['new', 'new', 'unknown', 'unknown', 'new'], ledger: { ...ZERO, slices: 3, proven: 2 },
    labels: ['verify-slice-0', 'verify-slice-1', 'verify-slice-1-retry', 'verify-slice-2'],
    nonces: ['n-1.0', 'n-1.1', 'n-1.1.r1', 'n-1.2'],
    gap: 'verify: UNVERIFIED \u2014 slice 1: receipt nonce mismatch (got WRONG, expected n-1.1.r1) \u2014 retried once after the first attempt failed (receipt nonce mismatch (got WRONG, expected n-1.1)); 2 of 5 finding(s) marked origin=unknown, surfaced-classification skipped' },
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
    const out = await verifyStage(ctx, row.json ? JSON.stringify(input) : input);
    assert.equal(out.verified, false);
    assert.deepEqual(out.findings, findings.map((f, i) => ({ ...f, origin: row.origins?.[i] || 'unknown' })));
    assert.deepEqual(ctx.calls.map((call) => call.label), row.labels || ['verify-slice-0', 'verify-slice-0-retry']);
    assert.deepEqual(out.inputProof, row.ledger || { ...ZERO, slices: 1 });
    if (row.nonces) assert.deepEqual(ctx.calls.map((call) => { const argv = argvOf(call); return argv[argv.indexOf('--nonce') + 1]; }), row.nonces);
    assert.deepEqual(out.gaps, [row.gap]);
  });
}

const NOT_DISPATCHED_ROWS = [
  { name: 'missing id', findings: [findingsFor(2)[0], { ...findingsFor(2)[1], id: undefined }],
    gap: 'verify: UNVERIFIED \u2014 slice 0: a dispatched finding has no usable id \u2014 the delta echo is keyed by id, so this slice cannot be verified; 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'whitespace-only id', findings: [findingsFor(2)[0], { ...findingsFor(2)[1], id: '   ' }],
    gap: 'verify: UNVERIFIED \u2014 slice 0: a dispatched finding has no usable id \u2014 the delta echo is keyed by id, so this slice cannot be verified; 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'duplicate ids', findings: [findingsFor(2)[0], { ...findingsFor(2)[1], id: 'F0' }],
    gap: 'verify: UNVERIFIED \u2014 slice 0: duplicate finding id in the slice (F0) \u2014 the delta echo is keyed by id, so this slice cannot be verified; 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'null finding', findings: [null], gap: 'verify: UNVERIFIED \u2014 slice 0: a dispatched finding has no usable id \u2014 the delta echo is keyed by id, so this slice cannot be verified; 1 of 1 finding(s) marked origin=unknown, surfaced-classification skipped' },
];
for (const row of NOT_DISPATCHED_ROWS) {
  test(`not dispatched: ${row.name}`, async () => {
    const ctx = verifyCtx(() => { throw new Error('must not dispatch'); });
    const out = await verifyStage(ctx, verifyInput(row.findings));
    assert.equal(out.verified, false);
    assert.deepEqual(ctx.calls, []);
    assert.deepEqual(out.findings, row.findings.map((f) => ({ ...f, origin: 'unknown' })));
    assert.deepEqual(out.inputProof, { ...ZERO, slices: 1 });
    assert.deepEqual(out.gaps, [row.gap]);
  });
}

const BAD_PROOF = 'fnv1a32:0xdeadbeef';
// Key order is part of the copied document contract, so regenerate the hostile proof
// through the production function: sorting both sides must make this row fail.
const transposedProof = () => sliceInputChecksum({ base_branch: 'main', findings: BASE.map(projectVerifySliceFinding) });
const LEDGER_ROWS = [
  { name: 'missing content proof degrades after the fresh retry', receipt: () => ({ input_checksum: null }), missing: 1,
    gap: 'verify: UNVERIFIED \u2014 slice 0: input content proof missing from receipt \u2014 retried once after the first attempt failed (input content proof missing from receipt); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'wrong content proof recovers on a fresh attempt', receipt: (attempt) => attempt === 1 ? { input_checksum: BAD_PROOF } : {}, trusted: true, retriedMismatch: 1,
    gap: 'verify-slice-retry: slice 0\'s first executor dispatch was untrusted (slice-input content proof mismatch (receipt fnv1a32:0xdeadbeef, dispatched fnv1a32:0x693ddb36) \u2014 the document the script decoded is not the document this stage dispatched); a second dispatch was trusted and this slice\'s verified findings are from that attempt' },
  { name: 'missing content proof recovers on a fresh attempt', receipt: (attempt) => attempt === 1 ? { input_checksum: null } : {}, trusted: true, retriedMissing: 1,
    gap: 'verify-slice-retry: slice 0\'s first executor dispatch was untrusted (input content proof missing from receipt); a second dispatch was trusted and this slice\'s verified findings are from that attempt' },
  { name: 'wrong token proof precedes a wrong content proof', receipt: () => ({ inline_checksum: BAD_PROOF, input_checksum: BAD_PROOF }), mismatched: 1,
    gap: 'verify: UNVERIFIED \u2014 slice 0: slice-input token proof mismatch (receipt fnv1a32:0xdeadbeef, dispatched fnv1a32:0xc69f8ec8) \u2014 the token the script decoded is not the token this stage dispatched \u2014 retried once after the first attempt failed (slice-input token proof mismatch (receipt fnv1a32:0xdeadbeef, dispatched fnv1a32:0xc69f8ec8) \u2014 the token the script decoded is not the token this stage dispatched); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'missing token proof precedes a wrong content proof', receipt: () => ({ inline_checksum: null, input_checksum: BAD_PROOF }), missing: 1,
    gap: 'verify: UNVERIFIED \u2014 slice 0: inline token proof missing from receipt \u2014 retried once after the first attempt failed (inline token proof missing from receipt); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'missing token proof recovers on a fresh attempt', receipt: (attempt) => attempt === 1 ? { inline_checksum: null } : {}, trusted: true, retriedMissing: 1,
    gap: 'verify-slice-retry: slice 0\'s first executor dispatch was untrusted (inline token proof missing from receipt); a second dispatch was trusted and this slice\'s verified findings are from that attempt' },
  { name: 'transposed keys fail both attempts', receipt: () => ({ input_checksum: transposedProof() }), mismatched: 1,
    gap: 'verify: UNVERIFIED \u2014 slice 0: slice-input content proof mismatch (receipt fnv1a32:0x3c2b753e, dispatched fnv1a32:0x693ddb36) \u2014 the document the script decoded is not the document this stage dispatched \u2014 retried once after the first attempt failed (slice-input content proof mismatch (receipt fnv1a32:0x3c2b753e, dispatched fnv1a32:0x693ddb36) \u2014 the document the script decoded is not the document this stage dispatched); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'first input fault survives a differently failing retry', receipt: () => ({ inline_checksum: BAD_PROOF }), failRetry: true, mismatched: 1,
    gap: 'verify: UNVERIFIED \u2014 slice 0: status=failed (boom) \u2014 retried once after the first attempt failed (slice-input token proof mismatch (receipt fnv1a32:0xdeadbeef, dispatched fnv1a32:0xc69f8ec8) \u2014 the token the script decoded is not the token this stage dispatched); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
  { name: 'empty input emits the complete zero ledger', empty: true },
  { name: 'wrong delta proof recovers with literal retry nonce', deltaFault: true, trusted: true,
    gap: 'verify-slice-retry: slice 0\'s first executor dispatch was untrusted (delta content proof mismatch (receipt fnv1a32:0xdeadbeef, recomputed fnv1a32:0xdca84eaa) \u2014 the echoed values are not the ones the script wrote); a second dispatch was trusted and this slice\'s verified findings are from that attempt' },
  { name: 'wrong content proof under a good token degrades', receipt: () => ({ input_checksum: BAD_PROOF }), mismatched: 1,
    gap: 'verify: UNVERIFIED \u2014 slice 0: slice-input content proof mismatch (receipt fnv1a32:0xdeadbeef, dispatched fnv1a32:0x693ddb36) \u2014 the document the script decoded is not the document this stage dispatched \u2014 retried once after the first attempt failed (slice-input content proof mismatch (receipt fnv1a32:0xdeadbeef, dispatched fnv1a32:0x693ddb36) \u2014 the document the script decoded is not the document this stage dispatched); 2 of 2 finding(s) marked origin=unknown, surfaced-classification skipped' },
];
for (const row of LEDGER_ROWS) {
  test(`input-proof ledger: ${row.name}`, async () => {
    const ctx = verifyCtx((_index, attempt) => {
      if (row.empty) throw new Error('must not dispatch');
      if (row.failRetry && attempt === 2) return { status: 'failed', exitCode: 1, stderr: 'boom' };
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
    assert.deepEqual(out.gaps, row.empty ? [] : [row.gap]);
  });
}

const BIG = { id: 'BIG', origin: 'new', description: 'x'.repeat(50000) };
const countFindings = Array.from({ length: 130 }, (_, i) => ({
  id: `COUNT${i}`, file: `f${i}.js`, line_start: 1, line_end: 1, description: 'x'.repeat(800),
  evidence: 'e', severity: 'high', confidence: 90, cross_file_refs: [], origin: 'new',
}));
const fatFindings = Array.from({ length: 60 }, (_, i) => ({
  id: `FAT${i}`, file: `f${i}.js`, line_start: 1, line_end: 1, description: 'x'.repeat(4100),
  evidence: 'e', severity: 'high', confidence: 90, cross_file_refs: [], origin: 'new',
}));
const FANOUT_ROWS = [
  { name: 'count bound advises raising slice size', findings: countFindings,
    gap: 'verify_fanout: effective verifySliceSize=25 splits 130 finding(s) into 6 slices (above the 5-slice disclosure threshold) \u2014 up to 12 executor dispatches at 2 attempts per slice. Raise verifySliceSize to reduce fan-out.' },
  { name: 'budget bound advises that raising slice size will not help', findings: fatFindings,
    gap: 'verify_fanout: effective verifySliceSize=25 splits 60 finding(s) into 6 slices (above the 5-slice disclosure threshold) \u2014 up to 12 executor dispatches at 2 attempts per slice. The inline character budget bound this split; raising verifySliceSize will not reduce this fan-out.' },
  { name: 'oversize boundary takes precedence over count', findings: [
    ...Array.from({ length: 24 }, (_, i) => ({ id: `A${i}`, origin: 'new' })), BIG,
    ...Array.from({ length: 125 }, (_, i) => ({ id: `B${i}`, origin: 'new' })),
  ], reasons: ['oversize', 'count', 'count', 'count', 'count', null], oversize: 1,
    gap: 'verify_fanout: effective verifySliceSize=25 splits 150 finding(s) into 6 slices (above the 5-slice disclosure threshold) \u2014 up to 12 executor dispatches at 2 attempts per slice. An oversize finding forced this split; raising verifySliceSize will not reduce this fan-out.' },
  { name: 'budget boundary takes precedence over oversize', findings: [
    ...Array.from({ length: 60 }, (_, i) => ({ id: `FAT${i}`, origin: 'new', description: 'x'.repeat(4100) })), BIG, { id: 'TAIL', origin: 'new' },
  ], reasons: ['budget', 'budget', 'budget', 'budget', 'oversize', null], oversize: 1,
    gap: 'verify_fanout: effective verifySliceSize=25 splits 62 finding(s) into 6 slices (above the 5-slice disclosure threshold) \u2014 up to 12 executor dispatches at 2 attempts per slice. The inline character budget bound this split; raising verifySliceSize will not reduce this fan-out.' },
  { name: 'last oversize closure does not change count advice', findings: [
    ...Array.from({ length: 150 }, (_, i) => ({ id: `C${i}`, origin: 'new' })), BIG,
  ], reasons: ['count', 'count', 'count', 'count', 'count', 'oversize'], oversize: 1,
    gap: 'verify_fanout: effective verifySliceSize=25 splits 151 finding(s) into 6 slices (above the 5-slice disclosure threshold) \u2014 up to 12 executor dispatches at 2 attempts per slice. Raise verifySliceSize to reduce fan-out.' },
];
for (const row of FANOUT_ROWS) {
  test(`fan-out: ${row.name}`, async () => {
    const ctx = verifyCtx((index, _attempt, call) => deltaEnvelope(JSON.parse(inlineOf(call)).findings, { nonce: ['n-1.0', 'n-1.1', 'n-1.2', 'n-1.3', 'n-1.4', 'n-1.5'][index] }));
    const out = await verifyStage(ctx, verifyInput(row.findings, { limits: { verifySliceSize: 25 } }));
    assert.deepEqual(out.gaps.filter((gap) => gap.startsWith('verify_fanout:')), [row.gap]);
    assert.equal(out.inputProof.oversize, row.oversize || 0);
    if (row.reasons) assert.deepEqual(planVerifySlices(row.findings, 25, 50000, 'main').closeReasons, row.reasons);
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
  assert.deepEqual(out.gaps, ['verify: UNVERIFIED \u2014 slice 0: delta names a finding this slice did not dispatch (F2) \u2014 retried once after the first attempt failed (status=failed (transient)); 2 of 4 finding(s) marked origin=unknown, surfaced-classification skipped']);
});

test('command carries the literal encoded document as one shell word', async () => {
  const ctx = verifyCtx(() => deltaEnvelope(BASE, { nonce: 'n-1.0' }));
  await verifyStage(ctx, verifyInput(BASE));
  const call = ctx.calls[0];
  assert.equal(inlineOf(call), '{"findings":[{"id":"F1","file":"a.js","line_start":1,"cross_file_refs":[],"origin":"new"},{"id":"F2","file":"b.js","line_start":2,"cross_file_refs":["c.js:9"],"origin":"new"}],"base_branch":"main"}');
  assert.deepEqual(argvOf(call), [
    'python3', '/plugin/scripts/verify_findings.py', '--input', '/out/phase4-input-abc123.slice0.json',
    '--input-inline', inlineOf(call), '--output', '/out/phase4-output-abc123.slice0.json',
    '--nonce', 'n-1.0', '--head-sha', 'abc123', '--base-branch', 'main', '--diff-file', '/out/code-gauntlet-diff-abc123.patch',
  ]);
  assert.ok(call.prompt.includes('character for character with no line breaks'));
  assert.ok(call.prompt.includes('the deltas carry a checksum'));
  assert.doesNotMatch(outsideSingleQuotes(commandOf(call)), /[$`]|&&|\|\|/);
});

test('empty verify config dispatches the default script, paths and branch', async () => {
  const ctx = verifyCtx(() => deltaEnvelope(BASE, { nonce: 'n-1.0' }));
  const out = await verifyStage(ctx, verifyInput(BASE, { verify: {} }));
  assert.equal(out.verified, true);
  assert.equal(JSON.parse(inlineOf(ctx.calls[0])).base_branch, 'main');
  assert.deepEqual(argvOf(ctx.calls[0]), [
    'python3', 'scripts/verify_findings.py', '--input', 'phase4-input.slice0.json',
    '--input-inline', inlineOf(ctx.calls[0]), '--output', 'phase4-output.slice0.json',
    '--nonce', 'n-1.0', '--head-sha', 'abc123', '--base-branch', 'main',
  ]);
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
  assert.deepEqual(out.gaps, ['verify: UNVERIFIED \u2014 finding BIG has encoded length 50130, over VERIFY_INLINE_CHAR_BUDGET=50000; 1 of 2 finding(s) marked origin=unknown, surfaced-classification skipped']);
});
