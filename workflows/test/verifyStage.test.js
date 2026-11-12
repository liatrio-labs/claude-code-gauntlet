import { spawnSync } from 'node:child_process';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { verifyStage } from '../src/verifyStage.js';
import { deltaContentProof, encodeSliceInline, projectVerifySliceFinding } from '../src/verifyWire.js';
import { coarsenLimits, verifyDispatchCount, worstCaseAgentCount } from '../src/capacity.js';
import { ANCHOR_LIMITS } from '../src/registry.js';
import { deltaEnvelope, deltasFor, ELIMINATION_STAMP, verifyCtx, verifyInput, dispatchedFindings, sliceInputRecorder } from './helpers/verifyDelta.js';
import { outsideSingleQuotes, shellSplit } from './helpers/shellWords.js';

const BASE = [
  { id: 'F1', file: 'a.js', line_start: 1, line_end: 1, anchor: 'return process(value);', origin: 'new', dimension: 'bug', cross_file_refs: [], agent: 'bug-detector' },
  { id: 'F2', file: 'b.js', line_start: 2, line_end: 2, anchor: 'return process(value);', origin: 'new', dimension: 'security', cross_file_refs: ['c.js:9'], agent: 'bug-detector' },
];
const findingsFor = (n) => Array.from({ length: n }, (_, k) => ({
  anchor: 'return process(value);', id: `F${k}`, file: `f${k}.js`, line_start: k + 1, line_end: k + 1,
  title: `t${k}`, description: `d${k}`, severity: 'high', confidence: 80,
  dimension: 'bug', origin: 'new', evidence: `e${k}`, cross_file_refs: [], agent: 'bug-detector',
}));
// Expected location views describe the held claim and the executor's proven range.
const located = (f) => {
  if (!Number.isSafeInteger(f.line_start) || !f.anchor) return unresolved(f);
  const out = { ...f, location_status: 'confirmed', line_end: f.line_end ?? f.line_start,
    line: f.line_start, end_line: f.line_end ?? f.line_start,
    claimed_line_start: f.line_start, ...(Object.hasOwn(f, 'line_end') ? { claimed_line_end: f.line_end } : {}) };
  return out;
};
const unresolved = (f) => {
  const { line_start, line_end, line: _line, end_line: _end, ...rest } = f;
  return { ...rest, location_status: 'unconfirmed',
    ...(Object.hasOwn(f, 'line_start') ? { claimed_line_start: line_start } : {}),
    ...(Object.hasOwn(f, 'line_end') ? { claimed_line_end: line_end } : {}) };
};
const ZERO = {
  slices: 0, proven: 0, mismatched: 0, missing: 0, unprovable: 0, oversize: 0,
  retried: 0, retriedMismatch: 0, retriedMissing: 0,
  localised: 0, retriedFindings: 0, degradedFindings: 0,
};
const commandOf = (call) => call.prompt.split('\n').pop();
const argvOf = (call) => shellSplit(commandOf(call));
const inlineOf = (call) => {
  const argv = argvOf(call);
  return argv[argv.indexOf('--input-inline') + 1];
};

test('Verify refuses missing or unsupported target kinds before dispatch', async () => {
  for (const verify of [undefined, null, {}, ...[undefined, null, '', 'LOCAL', 'commit', 1, {}, ['local']]
    .map((targetKind) => ({ targetKind }))]) {
    const ctx = verifyCtx(() => deltaEnvelope(BASE, { nonce: 'n-1.0' }));
    await assert.rejects(verifyStage(ctx, { ...verifyInput(BASE), verify }),
      /verify\.targetKind must be one of pr, mr, branch, local/);
    assert.equal(ctx.calls.length, 0);
  }
});

test('Verify dispatches each explicit supported target kind', async () => {
  for (const targetKind of ['pr', 'mr', 'branch', 'local']) {
    const ctx = verifyCtx(() => deltaEnvelope(BASE, { nonce: 'n-1.0' }));
    const out = await verifyStage(ctx, verifyInput(BASE, { verify: { targetKind } }));
    assert.equal(out.verified, true);
    const argv = argvOf(ctx.calls[0]);
    assert.equal(argv[argv.indexOf('--target-kind') + 1], targetKind);
  }
});

const TRUSTED_ROWS = [
  ...[['python', ['python']], ['py -3', ['py', '-3']]].map(([python, argv]) => ({
    name: `drive interpreter ${python}`, findings: BASE,
    input: { python, pluginRoot: 'D:/plug in', verify: { scriptPath: '/untrusted.py' } },
    commandPrefix: [...argv, 'D:/plug in/scripts/verify_findings.py'],
  })),
  { name: 'dispatched slice document carries pinned numerics and no undeclared field',
    findings: [{ id: 'F1', file: 'a.js', line_start: 3.5, confidence: 90.2, origin: 'new', title: 'undeclared', suggestion: 'undeclared' }],
    changes: { line_start: 4, confidence: 90 },
    inline: '{"findings":[{"id":"F1","file":"a.js","line_start":4,"confidence":90}],"base_branch":"main"}' },
  { name: 'reordered deltas preserve dispatched finding order', findings: findingsFor(3),
    options: (f) => ({ deltas: deltasFor(f).reverse() }) },
  { name: 'git diff source is trusted when no file was dispatched', findings: BASE,
    input: { verify: { baseBranch: 'main' } } },
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
    const expected = row.findings.filter((f) => !row.ids || row.ids.includes(f.id)).map((f) => located({ ...f, ...row.changes }));
    const out = await verifyStage(ctx, verifyInput(row.findings, row.input));
    assert.equal(out.verified, true);
    assert.deepEqual(out.findings, expected);
    assert.deepEqual(out.gaps, []);
    assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, proven: 1 });
    assert.deepEqual(ctx.calls.map((call) => [call.label, call.agentType]), [['verify-slice-0', 'code-gauntlet:executor']]);
    if (row.commandPrefix) assert.deepEqual(argvOf(ctx.calls[0]).slice(0, row.commandPrefix.length), row.commandPrefix);
    if (row.inline) assert.equal(inlineOf(ctx.calls[0]), row.inline);
  });
}

const degradedGap = (groups, slice, total, firstReason) =>
  `verify: UNVERIFIED \u2014 slice ${slice}: `
  + groups.map(([reason, ids]) => `${reason} [ids: ${ids.map((id) => JSON.stringify(id)).join(', ')}]`).join('; ')
  + (firstReason === undefined ? '' : `; first attempt: ${firstReason}`)
  + `; ${groups.reduce((count, [, ids]) => count + ids.length, 0)} of ${total} finding(s) in stage marked origin=unknown, surfaced-classification skipped, held back to the report`;

const UNTRUSTED_ROWS = [
  { name: 'origin drift rejects the original script proof',
    retryReason: 'receipt nonce mismatch (got n-1.0.r1, expected n-1.0.r2.0)',
    options: (f) => ({ deltas: deltasFor(f, { F0: { origin: 'surfaced' } }), checksum: 'fnv1a32:0xc35f3d60' }),
    reason: 'delta content proof mismatch (receipt fnv1a32:0xc35f3d60, recomputed fnv1a32:0xb75055cd) \u2014 the echoed values are not the ones the script wrote',
  },
  { name: 'absent delta proof is untrusted',
    retryReason: 'receipt nonce mismatch (got n-1.0.r1, expected n-1.0.r2.0)',
    mutate: (env) => { delete env.receipt.deltas_checksum; },
    reason: 'receipt carries no deltas_checksum (content proof missing)',
  },
  { name: 'fabricated elimination keeps the claimed eliminated finding',
    retryReason: 'receipt nonce mismatch (got n-1.0.r1, expected n-1.0.r2.0)',
    options: () => ({ overrides: { F1: { verified: false } } }),
    reason: 'delta F1: eliminated without the elimination_reason stamp (fabricated elimination \u2014 the verify script always stamps a real one)',
  },
  { name: 'missing delta array is untrusted',
    retryReason: 'receipt nonce mismatch (got n-1.0.r1, expected n-1.0.r2.0)',
    mutate: (env) => { env.result = {}; },
    reason: 'result missing deltas array',
  },
  { name: 'failed executor preserves original agent and findings',
    envelope: () => ({ status: 'failed', exitCode: 1, stderr: 'boom' }),
    reason: 'status=failed (boom)',
  },
  { name: 'null envelope is retried and disclosed', envelope: () => null, reason: 'executor returned no envelope' },
  { name: 'wrong receipt sha is untrusted',
    retryReason: 'receipt nonce mismatch (got n-1.0.r1, expected n-1.0.r2.0)',
    options: () => ({ sha: 'wrong' }),
    reason: 'receipt sha mismatch (got wrong)',
  },
  { name: 'executor throw is retried and disclosed',
    envelope: () => { throw new Error('boom'); },
    reason: 'executor threw (boom)',
  },
  { name: 'wrong nonce degrades only its slice after bounded retries', count: 5, over: { limits: { verifySliceSize: 2 } },
    envelope: (findings, index) => deltaEnvelope(findings.slice(index * 2, index * 2 + 2), { nonce: ['n-1.0', 'WRONG', 'n-1.2'][index] }),
    origins: ['new', 'new', 'unknown', 'unknown', 'new'], ledger: { ...ZERO, slices: 3, proven: 2, retriedFindings: 2, degradedFindings: 2 },
    labels: ['verify-slice-0', 'verify-slice-1', 'verify-slice-1-retry', 'verify-slice-1-retry-0', 'verify-slice-2'],
    nonces: ['n-1.0', 'n-1.1', 'n-1.1.r1', 'n-1.1.r2.0', 'n-1.2'],
    inputs: [
      '/out/phase4-input-abc123.slice0.json', '/out/phase4-input-abc123.slice1.json',
      '/out/phase4-input-abc123.slice1.r1.json', '/out/phase4-input-abc123.slice1.r2.0.json', '/out/phase4-input-abc123.slice2.json',
    ],
    outputs: [
      '/out/phase4-output-abc123.slice0.json', '/out/phase4-output-abc123.slice1.json',
      '/out/phase4-output-abc123.slice1.r1.json', '/out/phase4-output-abc123.slice1.r2.0.json', '/out/phase4-output-abc123.slice2.json',
    ],
    reason: 'receipt nonce mismatch (got WRONG, expected n-1.1)',
    retryReason: 'receipt nonce mismatch (got WRONG, expected n-1.1.r2.0)',
    degradedIds: ['F2', 'F3'],
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
    assert.deepEqual(out.findings, findings.map((f, i) => row.origins?.[i] === 'new' ? located(f) : unresolved({ ...f, origin: 'unknown' })));
    assert.deepEqual(ctx.calls.map((call) => call.label), row.labels || ['verify-slice-0', 'verify-slice-0-retry', 'verify-slice-0-retry-0']);
    assert.deepEqual(out.inputProof, row.ledger || { ...ZERO, slices: 1, retriedFindings: findings.length, degradedFindings: findings.length });
    if (row.nonces) assert.deepEqual(ctx.calls.map((call) => { const argv = argvOf(call); return argv[argv.indexOf('--nonce') + 1]; }),
      row.nonces);
    if (row.inputs) assert.deepEqual(ctx.calls.map((call) => { const argv = argvOf(call); return argv[argv.indexOf('--input') + 1]; }),
      row.inputs);
    if (row.outputs) {
      const outputs = ctx.calls.map((call) => { const argv = argvOf(call); return argv[argv.indexOf('--output') + 1]; });
      assert.deepEqual(outputs, row.outputs);
      assert.notEqual(outputs[2], outputs[1], 'retry preserves the first attempt output path');
    }
    const [singletonId, cappedId] = row.degradedIds || ['F0', 'F1'];
    assert.deepEqual(out.gaps, [degradedGap([
      [row.retryReason || row.reason, [singletonId]],
      ['singleton retry cap=1 reached after batched retry', [cappedId]],
    ], row.slice || 0, findings.length, row.retryReason ? row.reason : undefined)]);
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
  assert.deepEqual(out.findings, findings.map((f) => unresolved({ ...f, origin: 'unknown' })));
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, degradedFindings: 2 });
  assert.deepEqual(out.gaps, [
    'verify: UNVERIFIED \u2014 slice 0: a dispatched finding has no usable id \u2014 the delta echo is keyed by '
      + 'id, so this slice cannot be verified; 2 of 2 finding(s) in stage marked origin=unknown, surfaced-classification skipped, held back to the report'
  ]);
});

const BAD_PROOF = 'fnv1a32:0xdeadbeef';
const envelopeForCall = (call) => sliceInputRecorder(deltaEnvelope(dispatchedFindings(call.prompt), {
  nonce: argvOf(call)[argvOf(call).indexOf('--nonce') + 1],
}), call.prompt);

test('received record order binds the delta proof while held order and exact ids survive', async () => {
  const findings = findingsFor(3);
  findings[1].id = ' F0 ';
  const ctx = verifyCtx((_index, attempt, call) => {
    const env = envelopeForCall(call, attempt);
    env.receipt.record_proofs.reverse();
    env.receipt.deltas_checksum = deltaContentProof(env.receipt.record_proofs.map((p) => p.id), env.result.deltas);
    env.receipt.inline_checksum = BAD_PROOF;
    env.receipt.input_checksum = BAD_PROOF;
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.equal(ctx.calls.length, 1);
  assert.equal(out.verified, true);
  assert.deepEqual(out.findings, findings.map(located));
});

const LOCAL_ROWS = [
  { name: 'mixed proof repairs only the mistyped record', bad: ['F1'], repaired: true },
  { name: 'failed retry degrades only the mistyped record', bad: ['F1'], failed: true },
  { name: 'retry partly succeeds and preserves original order', bad: ['F1', 'F2'], retryBad: ['F2'] },
  { name: 'mistyped input cannot eliminate its finding', bad: ['F1'], eliminate: true, failed: true },
  { name: 'missing record row and delta localise an input omission', bad: ['F1'], omit: true, missing: true },
  { name: 'single foreign id is ignored and its expected id is retried', bad: ['F1'], foreign: true, missing: true },
  { name: 'trusted elimination survives a subset retry', bad: ['F1'], acceptedElimination: true, repaired: true },
];
for (const row of LOCAL_ROWS) test(`per-record recovery: ${row.name}`, async () => {
  const findings = findingsFor(3);
  const ctx = verifyCtx((_index, attempt, call) => {
    if (attempt === 2 && row.failed) return { status: 'failed', stderr: 'retry failed' };
    const env = envelopeForCall(call, attempt);
    const bad = attempt === 1 ? row.bad : (row.retryBad || []);
    for (const proof of env.receipt.record_proofs) if (bad.includes(proof.id)) proof.checksum = BAD_PROOF;
    if (attempt === 1) {
      if (row.omit) {
        env.receipt.record_proofs = env.receipt.record_proofs.filter((p) => !bad.includes(p.id));
        env.result.deltas = env.result.deltas.filter((d) => !bad.includes(d.id));
        env.receipt.n_in -= bad.length;
      }
      if (row.foreign) {
        env.receipt.record_proofs.find((p) => p.id === 'F1').id = 'FOREIGN';
        env.result.deltas.find((d) => d.id === 'F1').id = 'FOREIGN';
      }
      if (row.eliminate || row.acceptedElimination) {
        const d = env.result.deltas.find((d) => d.id === (row.eliminate ? 'F1' : 'F0'));
        Object.assign(d, { verified: false, elimination_reason: ELIMINATION_STAMP });
      }
    }
    env.receipt.deltas_checksum = deltaContentProof(env.receipt.record_proofs.map((p) => p.id), env.result.deltas);
    env.result.deltas.reverse();
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.equal(ctx.calls.length, 2 + (row.retryBad || []).length);
  assert.deepEqual(ctx.calls.slice(1).map((call) => dispatchedFindings(call.prompt).map((f) => f.id)), [row.bad, ...(row.retryBad || []).map((id) => [id])]);
  assert.ok(inlineOf(ctx.calls[1]).length < inlineOf(ctx.calls[0]).length);
  assert.equal(argvOf(ctx.calls[1])[argvOf(ctx.calls[1]).indexOf('--nonce') + 1], 'n-1.0.r1');
  const degraded = row.failed ? row.bad : (row.retryBad || []);
  assert.equal(out.verified, degraded.length === 0);
  assert.deepEqual(out.findings, findings.filter((f) => !(row.acceptedElimination && f.id === 'F0'))
    .map((f) => degraded.includes(f.id) ? unresolved({ ...f, origin: 'unknown' }) : located(f)));
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, localised: 3 - row.bad.length,
    retriedFindings: row.bad.length, degradedFindings: degraded.length,
    ...(degraded.length ? { mismatched: 1 } : { retried: 1, [row.missing ? 'retriedMissing' : 'retriedMismatch']: 1 }),
  });
  if (degraded.length) assert.match(out.gaps[0], new RegExp(`${degraded.length} of 3 finding`));
});

for (const field of ['deltas_checksum', 'record_proofs']) test(`whole-attempt recovery: missing ${field} retries the full slice`, async () => {
  const findings = findingsFor(2);
  const ctx = verifyCtx((_index, attempt, call) => {
    const env = envelopeForCall(call, attempt);
    if (attempt === 1) env.receipt[field] = undefined;
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.equal(out.verified, true);
  assert.deepEqual(out.findings, findings.map(located));
  assert.deepEqual(ctx.calls.map((call) => dispatchedFindings(call.prompt).map((f) => f.id)), [['F0', 'F1'], ['F0', 'F1']]);
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, proven: 1, retried: 1, retriedFindings: 2,
    retriedMissing: field === 'record_proofs' ? 1 : 0 });
});

for (const [name, mutate] of [
  ['context mismatch with matching record proofs', (env) => { env.receipt.base_branch = 'other'; }],
  ['diff path mismatch with matching record proofs', (env) => { env.receipt.diff_source.path = '/other.patch'; }],
  ['diff unreadable with matching record proofs', (env) => { env.receipt.diff_source.read = false; }],
  ['diff source missing with matching record proofs', (env) => { env.receipt.diff_source = undefined; }],
  ['missing record_proofs', (env) => { env.receipt.record_proofs = undefined; }],
  ['malformed record_proofs', (env) => { env.receipt.record_proofs = [{ id: 'F0' }]; }],
  ['duplicate record proof', (env) => { env.receipt.record_proofs.push(env.receipt.record_proofs[0]); }],
  ['duplicate delta', (env) => { env.result.deltas.push(env.result.deltas[0]); }],
  ['proof and delta id sets differ', (env) => { env.receipt.record_proofs.pop(); }],
  ['two foreign ids', (env) => {
    for (let i = 0; i < 2; i += 1) {
      env.receipt.record_proofs[i].id = `OTHER${i}`;
      env.result.deltas[i].id = `OTHER${i}`;
    }
    env.receipt.deltas_checksum = deltaContentProof(env.receipt.record_proofs.map((p) => p.id), env.result.deltas);
  }],
]) test(`whole-attempt gate: ${name}`, async () => {
  const findings = findingsFor(3);
  const ctx = verifyCtx((_index, attempt, call) => {
    const env = envelopeForCall(call, attempt);
    mutate(env);
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.equal(out.verified, false);
  assert.deepEqual(ctx.calls.map((call) => dispatchedFindings(call.prompt).map((f) => f.id)), [
    ['F0', 'F1', 'F2'], ['F0', 'F1', 'F2'], ['F0'], ['F1'],
  ]);
  if (name === 'two foreign ids') assert.match(out.gaps[0], /receipt names multiple findings this slice did not dispatch/);
  assert.deepEqual(out.findings, findings.map((f) => unresolved({ ...f, origin: 'unknown' })));
});

test('unprovable expected record falls back to the whole-slice token gate', async () => {
  const findings = findingsFor(2);
  findings[0].line_start = Number.MAX_SAFE_INTEGER + 1;
  const ctx = verifyCtx((_index, attempt, call) => {
    const env = envelopeForCall(call, attempt);
    env.receipt.inline_checksum = BAD_PROOF;
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.equal(out.verified, false);
  assert.deepEqual(out.findings.map((f) => f.origin), ['unknown', 'unknown']);
});

test('unprovable expected record trusts valid whole-slice token and delta proofs', async () => {
  const findings = findingsFor(2);
  findings[0].line_start = Number.MAX_SAFE_INTEGER + 1;
  const ctx = verifyCtx((_index, attempt, call) => envelopeForCall(call, attempt));
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.equal(out.verified, true);
  assert.deepEqual(out.findings, findings.map(located));
  assert.deepEqual(out.gaps, []);
  assert.equal(ctx.calls.length, 1);
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, unprovable: 1, proven: 0 });
});

for (const [name, receipt, fault] of [
  ['missing content proof', { input_checksum: null }, 'missing'],
  ['wrong content proof', { input_checksum: BAD_PROOF }, 'mismatched'],
  ['wrong token proof', { inline_checksum: BAD_PROOF, input_checksum: BAD_PROOF }, 'mismatched'],
  ['wrong count', { n_in: 1 }, 'mismatched'],
]) test(`aggregate diagnostic: ${name} accepts matching records without retry`, async () => {
  const ctx = verifyCtx(() => {
    const env = deltaEnvelope(BASE, { nonce: 'n-1.0' });
    Object.assign(env.receipt, receipt);
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(BASE));
  assert.equal(out.verified, true);
  assert.deepEqual(out.findings, BASE.map(located));
  assert.equal(ctx.calls.length, 1);
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, [fault]: 1 });
});
test('empty input emits the complete zero ledger', async () => {
  const ctx = verifyCtx(() => { throw new Error('must not dispatch'); });
  assert.deepEqual(await verifyStage(ctx, verifyInput([])), { findings: [], eliminated: [], verified: true, gaps: [], inputProof: ZERO });
});

const bulk = (prefix, length, chars) => Array.from({ length }, (_, i) => ({
  id: `${prefix}${i}`, file: 'x'.repeat(chars), line_start: 1, line_end: 1,
  evidence: 'e', severity: 'high', confidence: 90, cross_file_refs: [], origin: 'new',
}));
const FANOUT_ROWS = [
  { name: 'count bound advises raising slice size', findings: bulk('COUNT', 130, 10),
    gap: ('verify_fanout: effective verifySliceSize=25 splits 130 finding(s) into 6 slices (above the 5-slice '
      + 'disclosure threshold) \u2014 up to 30 executor dispatches (12 initial or r1 calls and up to 18 singleton retries). Raise verifySliceSize to '
      + 'reduce fan-out.') },
  { name: 'budget bound advises that raising slice size will not help', findings: bulk('FAT', 60, 4100),
    gap: ('verify_fanout: effective verifySliceSize=25 splits 60 finding(s) into 60 slices (above the 5-slice '
      + 'disclosure threshold) \u2014 up to 120 executor dispatches (120 initial or r1 calls and up to 0 singleton retries). The inline character '
      + 'budget bound this split; raising verifySliceSize will not reduce this fan-out.') },
];
for (const row of FANOUT_ROWS) {
  test(`fan-out: ${row.name}`, async () => {
    const ctx = verifyCtx((index, _attempt, call) => deltaEnvelope(JSON.parse(inlineOf(call)).findings, { nonce: `n-1.${index}` }));
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
  assert.deepEqual(out.gaps, [degradedGap([
    ['receipt nonce mismatch (got n-1.0.r1, expected n-1.0.r2.0)', ['F0']],
    ['singleton retry cap=1 reached after batched retry', ['F1']],
  ], 0, 4, 'status=failed (transient)')]);
});

test('shell quoting preserves paths with spaces, apostrophes and special branch names', async () => {
  const ctx = verifyCtx(() => deltaEnvelope(BASE, { nonce: 'n-1.0' }));
  await verifyStage(ctx, verifyInput(BASE, { pluginRoot: '/plug in', verify: {
    scriptPath: '/plug in/scripts/verify_findings.py', inputPathBase: '/My Documents/out/phase4-input-abc123',
    outputPathBase: '/My Documents/out/phase4-output-abc123', baseBranch: 'feature/$x-`y`',
    diffPath: "/Users/o'brien/out/code-gauntlet diff.patch",
  } }));
  assert.deepEqual(argvOf(ctx.calls[0]), [
    'python3', '/plug in/scripts/verify_findings.py', '--input', '/My Documents/out/phase4-input-abc123.slice0.json',
    '--input-inline', inlineOf(ctx.calls[0]), '--output', '/My Documents/out/phase4-output-abc123.slice0.json',
    '--nonce', 'n-1.0', '--head-sha', 'abc123', '--base-branch', 'feature/$x-`y`',
    '--diff-file', "/Users/o'brien/out/code-gauntlet diff.patch",
    '--target-kind', 'branch',
  ]);
  assert.doesNotMatch(outsideSingleQuotes(commandOf(ctx.calls[0])), /[$`]/);
});

test('many small findings dispatch tokens under the copy budget with the exact-copy instruction', async () => {
  const findings = Array.from({ length: 100 }, (_, i) => ({ id: `F${i}`, file: 'x'.repeat(200) }));
  const ctx = verifyCtx((index, _attempt, call) => deltaEnvelope(dispatchedFindings(call.prompt), { nonce: `n-1.${index}` }));
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.equal(out.verified, true);
  assert.deepEqual(ctx.calls.map((call) => dispatchedFindings(call.prompt).length), [22, 22, 22, 22, 12]);
  assert.deepEqual(out.findings.map((f) => f.id), findings.map((f) => f.id));
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 5, proven: 5 });
  for (const call of ctx.calls) {
    assert.ok(inlineOf(call).length <= 5000);
    assert.doesNotMatch(inlineOf(call), /[\r\n]/);
    assert.equal(call.prompt.split('\n')[0], 'Reproduce the quoted --input-inline token character for character with no line breaks.');
    assert.equal(call.prompt.split('\n').length, 2);
  }
});

for (const hostPlatform of ['posix', 'windows']) test(`oversize copy gets its own verified slice and retains retry recovery: ${hostPlatform}`, async () => {
  const findings = findingsFor(3);
  findings[1].file = 'x'.repeat(6000);
  const ctx = verifyCtx((index, attempt, call) => {
    if (index === 1 && attempt === 1) return { status: 'failed', stderr: 'transient' };
    return deltaEnvelope(dispatchedFindings(call.prompt), { nonce: `n-1.${index}${attempt === 2 ? '.r1' : ''}` });
  });
  const out = await verifyStage(ctx, verifyInput(findings, { repoRoot: hostPlatform === 'windows' ? 'D:/repo' : '/repo' }));
  assert.equal(out.verified, true);
  assert.deepEqual(out.findings, findings.map(located));
  assert.deepEqual(ctx.calls.map((call) => dispatchedFindings(call.prompt).map((f) => f.id)), [['F0'], ['F1'], ['F1'], ['F2']]);
  assert.deepEqual(ctx.calls.map((call) => call.label), ['verify-slice-0', 'verify-slice-1', 'verify-slice-1-retry', 'verify-slice-2']);
  assert.ok(inlineOf(ctx.calls[1]).length > 5000);
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 3, proven: 3, retried: 1, retriedFindings: 1 });
  for (const call of ctx.calls) assert.ok(call.prompt.startsWith('Reproduce the quoted --input-inline token character for character with no line breaks.\n'));
});

test('finding over the Windows native command limit is disclosed without dispatch and leaves the next slice nonce intact', async () => {
  const findings = [
    { id: 'BIG', file: 'x'.repeat(31000), line_start: 1, origin: 'new', cross_file_refs: [] },
    { id: 'OK', file: 'b.js', line_start: 2, line_end: 2, anchor: 'return process(value);', origin: 'new', cross_file_refs: [] },
  ];
  const ctx = verifyCtx(() => deltaEnvelope([findings[1]], { nonce: 'n-1.0', n_in: 1 }));
  const out = await verifyStage(ctx, verifyInput(findings, { repoRoot: 'D:/repo', limits: { verifySliceSize: 2 } }));
  assert.equal(out.verified, false);
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, proven: 1, oversize: 1, degradedFindings: 1 });
  assert.deepEqual(ctx.calls.map((call) => call.label), ['verify-slice-0']);
  assert.deepEqual(out.findings.map((f) => [f.id, f.origin]), [['BIG', 'unknown'], ['OK', 'new']]);
  assert.deepEqual(out.gaps, ['verify: UNVERIFIED \u2014 finding BIG has inline cost 31089, over hard cap=30385 (copy target=5000); 1 of 2 finding(s) in stage marked origin=unknown, surfaced-classification skipped, held back to the report']);
});

for (const hostPlatform of ['posix', 'windows']) test(`finding over 50,000 characters degrades between neighbors without dispatch: ${hostPlatform}`, async () => {
  const findings = findingsFor(3);
  findings[1].file = 'x'.repeat(50000);
  const ctx = verifyCtx((index, _attempt, call) => deltaEnvelope(dispatchedFindings(call.prompt), { nonce: `n-1.${index}` }));
  const out = await verifyStage(ctx, verifyInput(findings, {
    repoRoot: hostPlatform === 'windows' ? 'D:/repo' : '/repo', limits: { verifySliceSize: 3 },
  }));
  assert.equal(out.verified, false);
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 2, proven: 2, oversize: 1, degradedFindings: 1 });
  assert.deepEqual(ctx.calls.map((call) => dispatchedFindings(call.prompt).map((f) => f.id)), [['F0'], ['F2']]);
  assert.deepEqual(ctx.calls.map((call) => call.label), ['verify-slice-0', 'verify-slice-1']);
  assert.deepEqual(out.findings.map((f) => [f.id, f.origin]), [['F0', 'new'], ['F1', 'unknown'], ['F2', 'new']]);
  assert.equal(out.gaps.length, 1);
  assert.match(out.gaps[0], /finding F1 has inline cost \d+, over hard cap=\d+ \(copy target=5000\); 1 of 3 finding\(s\)/);
  if (hostPlatform === 'posix') assert.match(out.gaps[0], /hard cap=50000/);
});

test('finding exactly at the POSIX hard cap dispatches in its own slice', async () => {
  const finding = { id: 'EXACT', file: '' };
  const emptyLength = encodeSliceInline({ findings: [projectVerifySliceFinding(finding)], base_branch: 'main' }).length;
  const findings = [{ ...finding, file: 'x'.repeat(50000 - emptyLength) }];
  const ctx = verifyCtx(() => deltaEnvelope(findings, { nonce: 'n-1.0' }));
  const out = await verifyStage(ctx, verifyInput(findings, { repoRoot: '/repo' }));
  assert.equal(out.verified, true);
  assert.equal(ctx.calls.length, 1);
  assert.equal(inlineOf(ctx.calls[0]).length, 50000);
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, proven: 1 });
});

test('location deltas require shape and content proofs', async () => {
  const rows = [
    { location_status: undefined }, { location_status: 'forged' },
    { line_start: 0 }, { line_start: 1.5 }, { line_end: Number.MAX_SAFE_INTEGER + 1 },
    { line_start: 4, line_end: 2 }, { line_end: ANCHOR_LIMITS.lines + 1 },
    { location_status: 'unconfirmed', line_start: 1, line_end: 1 },
  ];
  for (const fields of rows) {
    const findings = findingsFor(2);
    const ctx = verifyCtx((_index, _attempt, call) => {
      const dispatched = dispatchedFindings(call.prompt);
      return deltaEnvelope(dispatched, { nonce: shellSplit(call.prompt.split('\n').pop())[shellSplit(call.prompt.split('\n').pop()).indexOf('--nonce') + 1],
        overrides: { F0: fields } });
    });
    const out = await verifyStage(ctx, verifyInput(findings));
    assert.equal(out.findings[0].location_status, 'unconfirmed');
    assert.equal(Object.hasOwn(out.findings[0], 'line_start'), false);
    assert.equal(out.findings[1].location_status, 'confirmed');
    assert.deepEqual(dispatchedFindings(ctx.calls[1].prompt).map((f) => f.id), ['F0']);
  }
  for (const fields of [{ line_start: 2 }, { line_end: 2 }, { location_status: 'relocated' }]) {
    const findings = findingsFor(1);
    const ctx = verifyCtx((_index, _attempt, call) => {
      const env = envelopeForCall(call);
      Object.assign(env.result.deltas[0], fields);
      return env;
    });
    const out = await verifyStage(ctx, verifyInput(findings));
    assert.equal(out.findings[0].location_status, 'unconfirmed');
    assert.ok(out.gaps.some((g) => g.includes('delta content proof mismatch')));
  }
});

for (const [name, damage, groups, firstReason] of [
  ['sha', (env) => { env.receipt.sha = 'wrong'; }, [
    ['receipt sha mismatch (got wrong)', ['F0', 'F1', 'F2']],
  ]],
  ['nonce', (env) => { env.receipt.nonce = 'wrong'; }, [
    ['receipt nonce mismatch (got wrong, expected n-1.0.r2.0)', ['F0']],
    ['receipt nonce mismatch (got wrong, expected n-1.0.r2.1)', ['F1']],
    ['receipt nonce mismatch (got wrong, expected n-1.0.r2.2)', ['F2']],
  ], 'receipt nonce mismatch (got wrong, expected n-1.0)'],
  ['diff source', (env) => { env.receipt.diff_source.read = false; }, [
    ['receipt diff_source mismatch or unreadable', ['F0', 'F1', 'F2']],
  ]],
  ['failed executor', () => ({ status: 'failed', stderr: 'inline slice-input rejected' }), [
    ['status=failed (inline slice-input rejected)', ['F0', 'F1', 'F2']],
  ]],
  ['delta proof', (env) => { env.receipt.deltas_checksum = BAD_PROOF; }, [
    ['delta content proof mismatch (receipt fnv1a32:0xdeadbeef, recomputed fnv1a32:0x639396a7) \u2014 the echoed values are not the ones the script wrote', ['F0']],
    ['delta content proof mismatch (receipt fnv1a32:0xdeadbeef, recomputed fnv1a32:0xa8cb9e84) \u2014 the echoed values are not the ones the script wrote', ['F1']],
    ['delta content proof mismatch (receipt fnv1a32:0xdeadbeef, recomputed fnv1a32:0xb74ffce5) \u2014 the echoed values are not the ones the script wrote', ['F2']],
  ], 'delta content proof mismatch (receipt fnv1a32:0xdeadbeef, recomputed fnv1a32:0x34f57d6b) \u2014 the echoed values are not the ones the script wrote'],
]) test(`bounded whole-attempt recovery caps singleton failures: ${name}`, async () => {
  const findings = findingsFor(25);
  const ctx = verifyCtx((_index, _attempt, call) => {
    const env = envelopeForCall(call);
    return damage(env) || env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.deepEqual(ctx.calls.map((call) => dispatchedFindings(call.prompt).map((f) => f.id)), [
    findings.map((f) => f.id), findings.map((f) => f.id), ['F0'], ['F1'], ['F2'],
  ]);
  assert.deepEqual(ctx.parallelCalls, [3]);
  assert.equal(out.verified, false);
  assert.equal(out.inputProof.degradedFindings, 25);
  assert.deepEqual(out.gaps, [degradedGap([
    ...groups,
    ['singleton retry cap=3 reached after batched retry', findings.slice(3).map((f) => f.id)],
  ], 0, 25, firstReason)]);
});

test('bounded local recovery batches first, caps parallel singletons, and preserves evidence and decisions', async () => {
  const findings = findingsFor(8);
  let active = 0;
  let peak = 0;
  const ctx = verifyCtx(async (_index, attempt, call) => {
    const env = envelopeForCall(call);
    if (attempt <= 2) {
      for (const proof of env.receipt.record_proofs) {
        if (Number(proof.id.slice(1)) >= (attempt === 1 ? 2 : 3)) proof.checksum = BAD_PROOF;
      }
      if (attempt === 1) {
        Object.assign(env.result.deltas[1], { verified: false, elimination_reason: ELIMINATION_STAMP });
      } else {
        env.receipt.record_proofs = env.receipt.record_proofs.filter((proof) => proof.id !== 'F3');
        env.result.deltas = env.result.deltas.filter((delta) => delta.id !== 'F3');
        env.receipt.n_in -= 1;
      }
      env.receipt.deltas_checksum = deltaContentProof(env.receipt.record_proofs.map((p) => p.id), env.result.deltas);
    } else {
      active += 1;
      peak = Math.max(peak, active);
      await Promise.resolve();
      active -= 1;
      const [record] = dispatchedFindings(call.prompt);
      if (record.id === 'F4') env.receipt.deltas_checksum = BAD_PROOF;
      if (record.id === 'F5') throw new Error('copy failed');
    }
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.deepEqual(ctx.calls.map((call) => dispatchedFindings(call.prompt).map((f) => f.id)), [
    findings.map((f) => f.id), ['F2', 'F3', 'F4', 'F5', 'F6', 'F7'], ['F3'], ['F4'], ['F5'],
  ]);
  assert.equal(peak, 3);
  assert.deepEqual(ctx.parallelCalls, [3]);
  assert.deepEqual(ctx.calls.map((call) => call.label), [
    'verify-slice-0', 'verify-slice-0-retry', 'verify-slice-0-retry-3', 'verify-slice-0-retry-4', 'verify-slice-0-retry-5',
  ]);
  const suffixes = ['slice0', 'slice0.r1', 'slice0.r2.3', 'slice0.r2.4', 'slice0.r2.5'];
  for (const [flag, base] of [['--input', 'phase4-input'], ['--output', 'phase4-output']]) {
    assert.deepEqual(ctx.calls.map((call) => argvOf(call)[argvOf(call).indexOf(flag) + 1]),
      suffixes.map((suffix) => `/out/${base}-abc123.${suffix}.json`));
  }
  assert.deepEqual(ctx.calls.map((call) => argvOf(call)[argvOf(call).indexOf('--nonce') + 1]),
    ['n-1.0', 'n-1.0.r1', 'n-1.0.r2.3', 'n-1.0.r2.4', 'n-1.0.r2.5']);
  assert.deepEqual(out.eliminated.map((f) => f.id), ['F1']);
  assert.deepEqual(out.findings, [located(findings[0]), located(findings[2]), located(findings[3]),
    ...findings.slice(4).map((f) => unresolved({ ...f, origin: 'unknown' }))]);
  assert.equal(out.inputProof.mismatched, 1);
  assert.equal(out.inputProof.retriedFindings, 6);
  assert.equal(out.inputProof.degradedFindings, 4);
  assert.match(out.gaps[0], /delta content proof mismatch.*\[ids: "F4"\]/);
  assert.match(out.gaps[0], /executor threw \(copy failed\) \[ids: "F5"\]/);
  assert.match(out.gaps[0], /first attempt: record proof missing or mismatched for 6 of 8 finding/);
  assert.match(out.gaps[0], /singleton retry cap=3 reached after batched retry \[ids: "F6", "F7"\]/);
  assert.equal(out.gaps[0].split('singleton retry cap=').length, 2);
});

test('bounded recovery preserves the agent guard and useful limits at the stage boundary', async () => {
  const findings = Array.from({ length: 1000 }, (_, i) => ({ id: `F${i}`, file: 'source' }));
  const limits = coarsenLimits({ verifySliceSize: 200, validateBatch: 25, challengeCap: 400 }, 0, findings);
  assert.ok(worstCaseAgentCount(limits, 0, findings) < 900);
  assert.equal(limits.challengeCap, 400);
  assert.equal(limits.validateBatch, 25);
  const ctx = verifyCtx((_index, _attempt, call) => envelopeForCall(call));
  const out = await verifyStage(ctx, verifyInput(findings, { limits }));
  assert.equal(out.verified, true);
  assert.equal(out.findings.length, findings.length);
  assert.equal(out.inputProof.degradedFindings, 0);
});

for (const fault of ['throw', 'null']) test(`bounded local recovery discloses parallel host failure: ${fault}`, async () => {
  const findings = findingsFor(5);
  const ctx = verifyCtx((_index, _attempt, call) => {
    const env = envelopeForCall(call);
    for (const proof of env.receipt.record_proofs) if (proof.id !== 'F0') proof.checksum = BAD_PROOF;
    return env;
  });
  ctx.parallel = async (thunks) => {
    assert.equal(thunks.length, 3);
    if (fault === 'throw') throw new Error('host unavailable');
    return null;
  };
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.equal(ctx.calls.length, 2);
  assert.deepEqual(out.findings, [located(findings[0]), ...findings.slice(1).map((f) => unresolved({ ...f, origin: 'unknown' }))]);
  assert.equal(out.inputProof.mismatched, 1);
  assert.equal(out.inputProof.degradedFindings, 4);
  assert.match(out.gaps[0], fault === 'throw'
    ? /singleton dispatch failed \(host unavailable\) \[ids: "F1", "F2", "F3"\]/
    : /singleton retry returned no result \[ids: "F1", "F2", "F3"\]/);
  assert.match(out.gaps[0], /singleton retry cap=3 reached after batched retry \[ids: "F4"\]/);
});

test('a garbled record that garbles again stops after singleton r1', async () => {
  const findings = findingsFor(3);
  const ctx = verifyCtx((_index, attempt, call) => {
    const env = envelopeForCall(call);
    if (attempt <= 2) env.receipt.record_proofs.find((proof) => proof.id === 'F1').checksum = BAD_PROOF;
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.deepEqual(ctx.calls.map((call) => dispatchedFindings(call.prompt).map((f) => f.id)), [
    ['F0', 'F1', 'F2'], ['F1'],
  ]);
  assert.deepEqual(ctx.parallelCalls, []);
  assert.equal(out.verified, false);
  assert.deepEqual(out.findings, [located(findings[0]), unresolved({ ...findings[1], origin: 'unknown' }), located(findings[2])]);
  assert.equal(out.inputProof.retriedFindings, 1);
  assert.equal(out.inputProof.degradedFindings, 1);
  assert.match(out.gaps[0], /\[ids: "F1"\]/);
});

test('two-record recovery fits its per-slice budget and counts cap-reached records as retried', async () => {
  const findings = findingsFor(2);
  const ctx = verifyCtx((_index, attempt, call) => {
    const env = envelopeForCall(call);
    if (attempt <= 2) for (const proof of env.receipt.record_proofs) proof.checksum = BAD_PROOF;
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.deepEqual(ctx.calls.map((call) => dispatchedFindings(call.prompt).map((f) => f.id)), [
    ['F0', 'F1'], ['F0', 'F1'], ['F0'],
  ]);
  assert.equal(verifyDispatchCount([findings]), 3);
  assert.equal(ctx.calls.length, verifyDispatchCount([findings]));
  assert.equal(out.inputProof.retriedFindings, 2);
  assert.equal(out.inputProof.degradedFindings, 1);
  assert.deepEqual(out.findings, [located(findings[0]), unresolved({ ...findings[1], origin: 'unknown' })]);
  assert.match(out.gaps[0], /singleton retry cap=1 reached after batched retry \[ids: "F1"\]/);
});

test('matched record proofs with malformed locations degrade after batched r1 without singleton retries', async () => {
  const findings = findingsFor(4);
  const ctx = verifyCtx((_index, _attempt, call) => {
    const env = envelopeForCall(call);
    for (const delta of env.result.deltas) if (delta.id !== 'F0') delta.line_end = 0;
    env.receipt.deltas_checksum = deltaContentProof(env.receipt.record_proofs.map((proof) => proof.id), env.result.deltas);
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.deepEqual(ctx.calls.map((call) => dispatchedFindings(call.prompt).map((f) => f.id)), [
    ['F0', 'F1', 'F2', 'F3'], ['F1', 'F2', 'F3'],
  ]);
  assert.deepEqual(ctx.parallelCalls, []);
  assert.equal(out.verified, false);
  assert.deepEqual(out.findings, [located(findings[0]), ...findings.slice(1).map((f) => unresolved({ ...f, origin: 'unknown' }))]);
  assert.equal(out.inputProof.retriedFindings, 3);
  assert.equal(out.inputProof.degradedFindings, 3);
});

// These successes pass through the host copier, unlike initial and batched dispatches.
test('singleton recovery retains record coverage across the JSON host boundary', async () => {
  const findings = findingsFor(4);
  const ctx = verifyCtx((_index, attempt, call) => {
    const env = envelopeForCall(call);
    if (attempt < 3) for (const proof of env.receipt.record_proofs) {
      if (proof.id !== 'F0') proof.checksum = BAD_PROOF;
    }
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.deepEqual(ctx.parallelCalls, [3]);
  assert.equal(out.verified, true);
  assert.deepEqual(out.findings, findings.map(located));
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, localised: 1,
    retried: 1, retriedMismatch: 1, retriedFindings: 3 });
});

for (const [name, tokenProof] of [
  ['wrong', BAD_PROOF], ['null', null], ['missing', undefined],
]) test(`token spelling alone does not taint proven content: ${name}`, async () => {
  const ctx = verifyCtx((_index, _attempt, call) => {
    const env = envelopeForCall(call);
    env.receipt.inline_checksum = tokenProof;
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(BASE));
  assert.equal(ctx.calls.length, 1);
  assert.equal(out.verified, true);
  assert.deepEqual(out.findings, BASE.map(located));
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, proven: 1 });
  assert.deepEqual(out.gaps, []);
});

test('whole batched transcription failures recover singletons and isolate one bad record', async () => {
  const findings = findingsFor(4);
  const ctx = verifyCtx((_index, attempt, call) => {
    if (attempt < 3) return { status: 'failed', stderr: 'inline slice-input rejected' };
    const env = envelopeForCall(call);
    if (dispatchedFindings(call.prompt)[0].id === 'F1') env.receipt.record_proofs[0].checksum = BAD_PROOF;
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.deepEqual(ctx.calls.map((call) => dispatchedFindings(call.prompt).map((f) => f.id)), [
    ['F0', 'F1', 'F2', 'F3'], ['F0', 'F1', 'F2', 'F3'], ['F0'], ['F1'], ['F2'],
  ]);
  assert.deepEqual(ctx.parallelCalls, [3]);
  assert.deepEqual(out.findings, [located(findings[0]), unresolved({ ...findings[1], origin: 'unknown' }),
    located(findings[2]), unresolved({ ...findings[3], origin: 'unknown' })]);
  assert.equal(out.verified, false);
  assert.deepEqual(out.inputProof, { ...ZERO, slices: 1, retriedFindings: 4, degradedFindings: 2, mismatched: 1 });
  assert.deepEqual(out.gaps, [degradedGap([
    ['record proof missing or mismatched for 1 of 1 finding(s)', ['F1']],
    ['singleton retry cap=3 reached after batched retry', ['F3']],
  ], 0, 4, 'status=failed (inline slice-input rejected)')]);
});

for (const [name, respell, altered] of [
  ['raw printable', (token) => token.replaceAll('%7C', '|'), false],
  ['safe percent escape', (token) => token.replace('main', '%6Dain'), false],
  ['JSON escape', (token) => token.replaceAll('%7C', String.raw`\u007c`), false],
  ['percent decoded prematurely', (token) => token.replaceAll('%257C', '%7C'), true],
]) test(`Python decoded proofs govern JS retry: ${name}`, async () => {
  const findings = findingsFor(2);
  findings[1].anchor = 'pipe | literal %7C';
  const ctx = verifyCtx((_index, attempt, call) => {
    const env = envelopeForCall(call);
    const token = attempt === 1 ? respell(inlineOf(call)) : inlineOf(call);
    const result = spawnSync(process.env.PYTHON || 'python3', ['-c', `
import json, sys
sys.path.insert(0, 'scripts')
from gauntlet.verify.wire import decode_inline_slice
from gauntlet.jsjson import checksum_or_none, fnv1a32
token = sys.stdin.read()
doc = decode_inline_slice(token)
print(json.dumps(dict(inline_checksum=fnv1a32(token), input_checksum=checksum_or_none(doc),
    record_proofs=[dict(id=f['id'], checksum=checksum_or_none(f)) for f in doc['findings']])))
`], { input: token, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    Object.assign(env.receipt, JSON.parse(result.stdout));
    return env;
  });
  const out = await verifyStage(ctx, verifyInput(findings));
  assert.equal(out.verified, true);
  assert.deepEqual(out.findings, findings.map(located));
  assert.deepEqual(ctx.calls.map((call) => dispatchedFindings(call.prompt).map((f) => f.id)),
    altered ? [['F0', 'F1'], ['F1']] : [['F0', 'F1']]);
  assert.deepEqual(out.inputProof, altered
    ? { ...ZERO, slices: 1, localised: 1, retried: 1, retriedMismatch: 1, retriedFindings: 1 }
    : { ...ZERO, slices: 1, proven: 1 });
});
