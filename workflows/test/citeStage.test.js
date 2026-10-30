import { test } from 'node:test';
import assert from 'node:assert/strict';
import { citeStage } from '../src/citeStage.js';
import { CITE_ROLES_PER_STAGE, citeChecksum, citeSource, collectCitations, planCiteBatches } from '../src/citeWire.js';
import { buildResumeCheckpoints, checkpointShapeErrors, normalizePhaseFindings, slimPersistedCheckpoints } from '../src/checkpoints.js';
import { fnv1a32 } from '../src/wire.js';
import { stageCtx } from './helpers/stageFixtures.js';

export function citeInput() {
  const capture = { version: 1, nonce: 'run', head: 'a'.repeat(40), base: 'b'.repeat(40),
    diff_sha256: 'c'.repeat(64), manifest_sha256: 'd'.repeat(64), target_kind: 'branch',
    copied_files: 0, copied_bytes: 0, failures: [] };
  return { nonce: 'run', headShaShort: 'aaaaaaaa', outputDir: '/out', limits: { validateBatch: 10 },
    verify: { targetKind: 'branch', sourceCapture: capture, diffPath: '/out/diff', baseBranch: 'main' },
    findings: ['F', 'G'].map((id) => ({ id, file: 'source', title: 'Defect', description: 'Source evidence',
      severity: 'high', confidence: 90, skeptical_assessments: [{ finding_id: id, stage: 'validator', verdict: 'refuted',
        citations: [{ finding_id: id, stage: 'validator', request_id: 'claim', role: 'claim', snapshot_id: capture.manifest_sha256,
          file: 'source', side: 'head', line_start: 1, line_end: 1, quote: 'return "café 😀 value";' }], scope_assessments: [] }] })) };
}

export function citeEnvelope(prompt, inp, status = () => 'matched') {
  const token = prompt.match(/--input-inline '([^']*)'/)[1];
  const decode = (value) => typeof value === 'string' ? decodeURIComponent(value)
    : Array.isArray(value) ? value.map(decode) : value && typeof value === 'object'
      ? Object.fromEntries(Object.entries(value).map(([k, v]) => [decodeURIComponent(k), decode(v)])) : value;
  const document = decode(JSON.parse(token));
  const nonce = prompt.match(/--nonce (\S+)/)[1];
  const batch = prompt.match(/--cite-batch (\S+)/)[1];
  const attempt = Number(prompt.match(/--cite-attempt (\d+)/)[1]);
  const rows = document.requests.map((r) => ({ id: r.id, request: r.request, status: status(r),
    detail: 'explicit_source_range', source: citeSource(inp.verify.sourceCapture) }));
  return { status: 'ok', receipt: { nonce, sha: inp.headShaShort, base_branch: 'main',
    diff_source: { path: inp.verify.diffPath, read: true }, batch_id: batch, attempt,
    n_in: document.requests.length, inline_checksum: fnv1a32(token), input_checksum: citeChecksum(document),
    deltas_checksum: citeChecksum(rows), record_proofs: document.requests.map((r) => ({ id: r.id, checksum: citeChecksum(r) })) },
  result: { citations: rows } };
}

test('cite successes persist and infrastructure retries are bounded across resumes', async () => {
  const inp = citeInput();
  inp.limits.validateBatch = 1;
  let reserved;
  const ctx = stageCtx({ reply: (prompt) => {
    const batch = inp.cite_ledger.batches[0];
    assert.equal(batch.state, 'uncertain');
    assert.equal(batch.attempts, ctx.calls.length);
    reserved = JSON.parse(JSON.stringify(inp));
    return citeEnvelope(prompt, inp, (r) => r.finding_id === 'F' ? 'matched' : 'unavailable');
  } });
  const first = await citeStage(ctx, inp);
  assert.equal(ctx.calls.length, 2);
  assert.deepEqual(ctx.violations, []);
  assert.deepEqual(first.findings.map((f) => f.cite_results[0].status), ['matched', 'unavailable']);
  assert.equal(first.stats.cite_planned, 2);
  assert.equal(first.stats.cite_attempts, 2);
  const signed = slimPersistedCheckpoints({ challenge: { ...inp, validation_audit: { bounded: true } }, cite: first }, ['challenge', 'cite'], 'cite');
  assert.equal(checkpointShapeErrors(signed.phases).length, 0);
  assert.deepEqual(signed.phases.cite.validation_audit, { bounded: true });
  assert.deepEqual(signed.phases.cite.cite_ledger, first.cite_ledger);
  const fail = { agent: () => { assert.fail('exhausted or successful citation dispatched again'); } };
  const replay = await citeStage(fail, JSON.parse(JSON.stringify(signed.phases.cite)));
  assert.deepEqual(replay.findings, first.findings);
  assert.equal(replay.stats.cite_attempts, 0);
  // The reservation captured before a lost response still consumes that attempt.
  assert.equal((await citeStage(fail, reserved)).cite_ledger.batches[0].attempts, 2);
  const changed = JSON.parse(JSON.stringify(first));
  changed.findings[1].skeptical_assessments[0].citations[0].quote = 'return changed_value;';
  const repairedCtx = stageCtx({ reply: (p) => citeEnvelope(p, changed) });
  const repaired = await citeStage(repairedCtx, changed);
  assert.equal(repairedCtx.calls.length, 1);
  assert.ok(repairedCtx.calls[0].prompt.includes('changed_value'));
  assert.ok(repaired.gaps.includes('cite_identity_changed: dependent citation results invalidated'));
  assert.deepEqual(repaired.findings.map((f) => f.cite_results[0].status), ['matched', 'matched']);
  const retryable = citeInput();
  retryable.cite_ledger = JSON.parse(JSON.stringify(first.cite_ledger));
  retryable.cite_ledger.batches[0].attempts = 1;
  const retryCtx = stageCtx({ reply: (p) => citeEnvelope(p, retryable) });
  assert.equal((await citeStage(retryCtx, retryable)).stats.cite_attempts, 1);
});

const RECEIPT_FAULTS = {
  nonce: (e) => { e.receipt.nonce = 'wrong'; },
  head: (e) => { e.receipt.sha = 'wrong'; },
  base: (e) => { e.receipt.base_branch = 'wrong'; },
  diff: (e) => { e.receipt.diff_source.read = false; },
  content: (e) => { e.receipt.deltas_checksum = 'wrong'; },
  records: (e) => { e.receipt.record_proofs[0].checksum = 'wrong'; },
  duplicate: (e) => { e.result.citations.push(e.result.citations[0]); e.receipt.deltas_checksum = citeChecksum(e.result.citations); },
  batch: (e) => { e.receipt.batch_id = 'wrong'; },
  attempt: (e) => { e.receipt.attempt = 0; },
  source: (e) => { e.result.citations[0].source.head = 'wrong'; e.receipt.deltas_checksum = citeChecksum(e.result.citations); },
  request: (e) => { e.result.citations[0].request = { ...e.result.citations[0].request, file: 'other' }; e.receipt.deltas_checksum = citeChecksum(e.result.citations); },
};
for (const [name, corrupt] of Object.entries(RECEIPT_FAULTS)) test(`cite receipts reject ${name} substitution without losing trusted siblings`, async () => {
  const inp = citeInput();
  const ctx = stageCtx({ reply: (prompt) => { const e = citeEnvelope(prompt, inp); corrupt(e); return e; } });
  const out = await citeStage(ctx, inp);
  assert.equal(out.findings[0].cite_results[0].status, 'unavailable');
  const local = ['records', 'duplicate', 'source', 'request'].includes(name);
  assert.equal(out.findings[1].cite_results[0].status, local ? 'matched' : 'unavailable');
  assert.ok(out.gaps.some((g) => g.startsWith('cite_receipt:')));
});

for (const field of ['input_checksum', 'inline_checksum', 'n_in']) test(`cite receipt ${field} faults localize by attributable record proofs`, async () => {
  const inp = citeInput();
  const ctx = stageCtx({ reply: (p) => { const e = citeEnvelope(p, inp); delete e.receipt[field]; return e; } });
  const out = await citeStage(ctx, inp);
  assert.ok(out.findings.every((f) => f.cite_results[0].status === 'matched'));
  assert.ok(out.gaps.some((g) => g.startsWith('cite_receipt:')));
});

test('cite preserves rejected audit paths, legacy packets and upstream records', async () => {
  const inp = citeInput();
  inp.findings[0].skeptical_assessments[0].citations[0].file = '../source';
  inp.unverified = [inp.findings.pop()];
  inp.eliminated = [{ ...inp.unverified[0], id: 'threshold', eliminated_by: 'filter' }];
  const ctx = stageCtx({ reply: (p) => citeEnvelope(p, inp) });
  const out = await citeStage(ctx, inp);
  assert.equal(ctx.calls.length, 1);
  assert.equal(out.findings[0].cite_results[0].status, 'rejected');
  assert.equal(out.unverified[0].cite_results[0].status, 'matched');
  assert.deepEqual(out.eliminated, inp.eliminated);
  assert.equal(out.cite_ledger.entries.some((e) => JSON.parse(e.key).record.finding_id === 'threshold'), false);
  const cp = JSON.parse(JSON.stringify(buildResumeCheckpoints({ cite: out, challenge: inp })));
  normalizePhaseFindings('cite', cp.phases.cite, true, { repoRoot: '/repo', verify: inp.verify });
  assert.equal(cp.phases.cite.findings[0].skeptical_assessments[0].citations[0].file, '../source');
  assert.equal(cp.phases.cite.findings[0].cite_results[0].request.file, '../source');
  cp.phases.cite.unverified[0].cite_results[0].source.head = 'other';
  normalizePhaseFindings('cite', cp.phases.cite, true, { repoRoot: '/repo', verify: inp.verify });
  assert.equal(cp.phases.cite.unverified[0].cite_results[0].status, 'unavailable');
  assert.equal(cp.phases.cite.cite_audit[0].result.source.head, 'other');
  const legacy = citeInput(); delete legacy.findings[0].skeptical_assessments; delete legacy.findings[1].skeptical_assessments;
  assert.deepEqual(collectCitations((await citeStage({}, legacy)).findings), []);
  for (const ledger of [null, {}, { version: 1, entries: [], batches: [{ attempts: 0 }] }]) {
    const corrupt = { ...citeInput(), cite_ledger: ledger };
    const held = await citeStage({ agent: () => assert.fail('corrupt ledger dispatched') }, corrupt);
    assert.ok(held.findings.every((f) => f.cite_results[0].status === 'unavailable'));
  }
  const noLedger = JSON.parse(JSON.stringify(out)); delete noLedger.cite_ledger;
  assert.equal((await citeStage({}, noLedger)).findings[0].cite_results[0].status, 'unavailable');
  assert.equal(planCiteBatches(collectCitations(citeInput().findings), 1, 'main').oversize.length, 2);
});

test('cite terminal requests reuse proof while oversize and changed snapshots hold only dependents', async () => {
  const inp = citeInput();
  const ctx = stageCtx({ reply: (p) => citeEnvelope(p, inp, (r) => r.finding_id === 'F' ? 'unmatched' : 'rejected') });
  const first = await citeStage(ctx, inp);
  assert.equal(ctx.calls.length, 1);
  const cached = await citeStage({ agent: () => assert.fail('terminal result retried') }, JSON.parse(JSON.stringify(first)));
  assert.deepEqual(cached.findings.map((f) => f.cite_results[0].status), ['unmatched', 'rejected']);
  const huge = citeInput();
  huge.findings[0].skeptical_assessments[0].citations[0].file = 'é'.repeat(10000);
  const smallCtx = stageCtx({ reply: (p) => citeEnvelope(p, huge) });
  const partial = await citeStage(smallCtx, huge);
  assert.equal(smallCtx.calls.length, 1);
  assert.deepEqual(partial.findings.map((f) => f.cite_results[0].status), ['unavailable', 'matched']);
  assert.equal(partial.stats.cite_oversize, 1);
  const moved = JSON.parse(JSON.stringify(first));
  moved.verify.sourceCapture.manifest_sha256 = 'e'.repeat(64);
  const movedCtx = stageCtx({ reply: (p) => citeEnvelope(p, moved, () => 'rejected') });
  const changed = await citeStage(movedCtx, moved);
  assert.equal(movedCtx.calls.length, 1);
  assert.ok(changed.gaps.some((g) => g.startsWith('cite_identity_changed:')));
  assert.equal(changed.cite_ledger.entries.length, 4);
});

test('cite preserves assessment identities and every requested proof role', async () => {
  const inp = citeInput();
  const validator = inp.findings[0].skeptical_assessments[0];
  const claim = validator.citations[0];
  validator.scope_assessments = [{ stage: 'validator', category: 'intentional', citations: [
    { ...claim, request_id: 'rule', role: 'rule' },
    { ...claim, request_id: 'applies', role: 'applicability' },
    { ...claim, request_id: 'exception', role: 'exception' },
  ] }];
  inp.findings[0].skeptical_assessments.push({ ...validator, stage: 'challenger', scope_assessments: [],
    citations: [{ ...claim, stage: 'challenger' }] });
  const ctx = stageCtx({ reply: (p) => citeEnvelope(p, inp, (r) => r.request.role === 'applicability' ? 'unmatched' : 'matched') });
  const out = await citeStage(ctx, inp);
  assert.equal(ctx.calls.length, 1);
  assert.deepEqual(out.findings[0].cite_results.map((r) => [r.request.stage, r.request.role, r.status]), [
    ['validator', 'claim', 'matched'], ['validator', 'rule', 'matched'], ['validator', 'applicability', 'unmatched'],
    ['validator', 'exception', 'matched'], ['challenger', 'claim', 'matched'],
  ]);
});

test('cite holds bounded or corrupt resume dependents while retaining successful siblings', async () => {
  const inp = citeInput();
  const ctx = stageCtx({ reply: (p) => citeEnvelope(p, inp, (r) => r.finding_id === 'F' ? 'matched' : 'unavailable') });
  const first = await citeStage(ctx, inp);
  for (const change of [
    (p) => { p.cite_ledger.batches[0].attempts = -1; },
  ]) {
    const resumed = JSON.parse(JSON.stringify(first)); change(resumed);
    const noDispatch = stageCtx();
    const held = await citeStage(noDispatch, resumed);
    assert.equal(noDispatch.calls.length, 0);
    assert.deepEqual(held.findings.map((f) => f.cite_results[0].status), ['matched', 'unavailable']);
    assert.equal(held.findings[1].cite_results[0].detail, 'corrupt_attempt_identity');
  }
  const capped = citeInput();
  const q = capped.findings[0].skeptical_assessments[0].citations[0];
  capped.findings[0].skeptical_assessments[0].citations = Array.from({ length: CITE_ROLES_PER_STAGE + 1 }, (_, i) => ({ ...q, request_id: String(i) }));
  const valid = stageCtx({ reply: (p) => citeEnvelope(p, capped) });
  const out = await citeStage(valid, capped);
  assert.equal(valid.calls.length, 1);
  assert.ok(out.findings[0].cite_results.every((r) => r.status === 'unavailable' && r.detail === 'request_cap'));
  assert.equal(out.findings[1].cite_results[0].status, 'matched');
});

test('cite malformed proof entries hold only their owners', async () => {
  const inp = citeInput();
  inp.findings[0].skeptical_assessments.push(null);
  const ctx = stageCtx({ reply: (p) => {
    const env = citeEnvelope(p, inp);
    delete env.result.citations[0].source;
    env.receipt.deltas_checksum = citeChecksum(env.result.citations);
    return env;
  } });
  const out = await citeStage(ctx, inp);
  assert.deepEqual(out.findings.map((f) => f.cite_results[0].status), ['unavailable', 'matched']);
  out.findings[0].cite_results = [null, { status: 'matched', request: inp.findings[0].skeptical_assessments[0].citations[0] }];
  out.unverified = [null];
  normalizePhaseFindings('cite', out, true, { repoRoot: '/repo', verify: inp.verify });
  assert.equal(out.findings[0].cite_results[0], null);
  assert.equal(out.findings[0].cite_results[1].status, 'unavailable');
  assert.equal(out.findings[1].cite_results[0].status, 'matched');
  assert.ok(checkpointShapeErrors({ cite: { findings: [], cite_ledger: [] } }).length);
});
