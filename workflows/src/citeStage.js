import { CITATION_REQUEST_SCHEMA, CITATION_RESULT_SCHEMA, CITE_VERIFIER_VERSION, STAGE_COSTS, matchesDispatchSchema, modelFor, dispatchReturnSchema } from './registry.js';
import { CITE_ROLES_PER_STAGE, citeChecksum, citeDocument, citeRecordKey, citeSource, collectCitations, planCiteBatches, reusableCiteResult } from './citeWire.js';
import { effectiveVerifyBaseBranch, verifyCommand, verifyInlineBudget } from './capacity.js';
import { encodeSliceInline } from './verifyWire.js';
import { firstUnsafeNumber, fnv1a32, isPlainObject } from './wire.js';
import { hostPlatformForRoots } from './paths.js';
import { VERIFY_RECEIPT_FIELDS, receiptIdentityError, receiptInputFault } from './receiptTrust.js';

const CITE_ENVELOPE_SCHEMA = { type: 'object', properties: {
  status: { type: 'string' }, stderr: { type: 'string' },
  receipt: { type: 'object', properties: {
    ...VERIFY_RECEIPT_FIELDS,
    batch_id: { type: 'string' }, attempt: { type: 'integer' },
  } },
  result: { type: 'object', properties: { citations: { type: 'array', items: {
    ...CITATION_RESULT_SCHEMA, properties: { id: { type: 'string' }, ...CITATION_RESULT_SCHEMA.properties },
    required: ['id', ...CITATION_RESULT_SCHEMA.required],
  } } } },
}, required: ['status'] };

function trustedCitations(env, inp, batch, nonce, attempt, document, token, gaps) {
  const reject = (reason) => { gaps.push(`cite_receipt: ${reason}`); return []; };
  const error = receiptIdentityError(env, { nonce, headShaShort: inp.headShaShort,
    baseBranch: document.base_branch, diffPath: inp.verify?.diffPath || null });
  if (error) return reject(error);
  const r = env.receipt;
  const rows = env.result?.citations;
  if (r.batch_id !== batch.id || r.attempt !== attempt || !Array.isArray(rows)
    || !Array.isArray(r.record_proofs) || r.deltas_checksum !== citeChecksum(rows)) return reject('batch, attempt, records or content proof mismatch');
  const fault = receiptInputFault(r, document.requests.length, citeChecksumToken(token), citeChecksum(document));
  if (fault) gaps.push(`cite_receipt: ${fault.reason}`);
  const proofs = (id) => r.record_proofs.filter((p) => p?.id === id);
  return document.requests.flatMap((record) => {
    const matches = rows.filter((row) => row?.id === record.id);
    const p = proofs(record.id);
    if (matches.length !== 1 || p.length !== 1 || p[0].checksum !== citeChecksum(record)) return reject(`${record.id}: row or record proof mismatch`);
    const row = matches[0];
    // Per-record proofs localize a substituted document without losing intact siblings.
    if (fault && firstUnsafeNumber(record, 'citation') !== null) return reject(`${record.id}: unsafe request number`);
    if (citeChecksum(row.request) !== citeChecksum(record.request)
      || citeChecksum(row.source) !== citeChecksum(batch.source)
      || !CITATION_RESULT_SCHEMA.properties.status.enum.includes(row.status)
      || typeof row.detail !== 'string') return reject(`${record.id}: request, source or result shape mismatch`);
    if (row.status === 'matched' && record.request?.snapshot_id !== batch.source.snapshot_id) return reject(`${record.id}: snapshot mismatch`);
    return [row];
  });
}

// Token proof hashes the encoded bytes, unlike a JSON content checksum.
const citeChecksumToken = fnv1a32;

function citeKeyIdentity(key) {
  try { return JSON.parse(key) || {}; } catch { return {}; }
}

export async function citeStage(ctx, input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : input;
  const findings = [...(inp.findings || []), ...(inp.unverified || [])];
  const records = collectCitations(findings);
  const source = citeSource(inp.verify?.sourceCapture);
  const gaps = [...(inp.gaps || [])];
  const hadLedger = Object.hasOwn(inp, 'cite_ledger');
  const ledger = hadLedger ? inp.cite_ledger : { version: 1, entries: [], batches: [] };
  const out = { ...inp, gaps, cite_ledger: ledger,
    stats: { ...(inp.stats || {}), cite_planned: 0, cite_attempts: 0, cite_oversize: 0 } };
  // Keep the reserved ledger reachable even when a host interrupts the dispatch.
  inp.cite_ledger = ledger;
  const validLedger = isPlainObject(ledger) && ledger.version === 1 && Array.isArray(ledger.entries) && Array.isArray(ledger.batches)
    && ledger.entries.every((e) => {
      if (!isPlainObject(e) || typeof e.key !== 'string') return false;
      const identity = citeKeyIdentity(e.key);
      return isPlainObject(identity.record) && isPlainObject(identity.source) && e.key === citeRecordKey(identity.record, identity.source);
    })
    && new Set(ledger.entries.map((e) => e.key)).size === ledger.entries.length
    && ledger.batches.every((b) => isPlainObject(b) && typeof b.id === 'string' && Array.isArray(b.keys) && isPlainObject(b.source))
    && new Set(ledger.batches.map((b) => b.id)).size === ledger.batches.length;
  const unavailable = (record, detail) => ({ id: record.id, request: record.request, status: 'unavailable', detail, source });
  const results = new Map();
  if (!validLedger || (!hadLedger && findings.some((f) => f.cite_results?.length))) {
    gaps.push('cite_unavailable: missing or corrupt attempt identity');
    for (const record of records) results.set(citeRecordKey(record, source), unavailable(record, 'corrupt_attempt_identity'));
  } else {
    const pending = [];
    for (const record of records) {
      const key = citeRecordKey(record, source);
      const entry = ledger.entries.find((e) => e.key === key);
      if (entry) {
        if (reusableCiteResult(entry, record, source)) results.set(key, entry.result);
        else if (!ledger.batches.some((b) => b.keys?.includes(key))) results.set(key, unavailable(record, 'missing_attempt_identity'));
        continue;
      }
      if (ledger.entries.some((e) => {
        const prior = citeKeyIdentity(e.key).record;
        return prior.finding_id === record.finding_id && prior.stage === record.stage
          && prior.request?.request_id === record.request?.request_id;
      })) {
        gaps.push('cite_identity_changed: dependent citation results invalidated');
      }
      ledger.entries.push({ key });
      if (records.filter((r) => r.finding_id === record.finding_id && r.stage === record.stage).length > CITE_ROLES_PER_STAGE) {
        const row = unavailable(record, 'request_cap');
        ledger.entries[ledger.entries.length - 1].result = row;
        results.set(key, row);
        gaps.push('cite_unavailable: assessment exceeds bounded citation roles');
        continue;
      }
      const identity = records.filter((r) => r.finding_id === record.finding_id && r.stage === record.stage
        && r.request?.request_id === record.request?.request_id);
      if (!matchesDispatchSchema(record.request, CITATION_REQUEST_SCHEMA) || identity.length !== 1
        || record.request.finding_id !== record.finding_id || record.request.stage !== record.stage
        || record.request.snapshot_id !== source.snapshot_id
        || firstUnsafeNumber(record, 'citation') !== null) {
        const row = { ...unavailable(record, 'malformed_or_ambiguous_request'), status: 'rejected' };
        ledger.entries[ledger.entries.length - 1].result = row;
        results.set(key, row);
        continue;
      }
      pending.push(record);
    }
    const branch = effectiveVerifyBaseBranch(inp.verify?.baseBranch);
    const plan = planCiteBatches(pending, verifyInlineBudget({ ...inp, citeMode: true }), branch, hostPlatformForRoots(inp));
    for (const record of plan.oversize) {
      const row = unavailable(record, 'oversize');
      ledger.entries.find((e) => e.key === citeRecordKey(record, source)).result = row;
      results.set(citeRecordKey(record, source), row);
      out.stats.cite_oversize += 1;
      gaps.push('cite_unavailable: encoded request exceeds inline budget');
    }
    for (const batchRecords of plan.batches) {
      const identity = { records: batchRecords, source, version: CITE_VERIFIER_VERSION };
      const id = citeChecksum(identity).slice(-8);
      if (ledger.batches.some((b) => b.id === id)) {
        for (const record of batchRecords) results.set(citeRecordKey(record, source), unavailable(record, 'ambiguous_batch_identity'));
        continue;
      }
      ledger.batches.push({ id, source, keys: batchRecords.map((r) => citeRecordKey(r, source)), attempts: 0 });
    }
    for (const batch of ledger.batches) {
      if (citeChecksum(batch.source) !== citeChecksum(source) || !Array.isArray(batch.keys)
        || !batch.keys.some((key) => records.some((r) => citeRecordKey(r, source) === key) && !results.has(key))) continue;
      const batchRecords = batch.keys.map((key) => citeKeyIdentity(key).record);
      const intact = Number.isSafeInteger(batch.attempts) && batch.attempts >= 0 && batch.attempts <= STAGE_COSTS.cite.attempts
        && batch.keys.every((key, i) => ledger.entries.some((e) => e.key === key) && key === citeRecordKey(batchRecords[i], source))
        && batch.id === citeChecksum({ records: batchRecords, source, version: CITE_VERIFIER_VERSION }).slice(-8);
      if (!intact) {
        for (const key of batch.keys) if (!results.has(key)) results.set(key, unavailable(citeKeyIdentity(key).record || {}, 'corrupt_attempt_identity'));
        gaps.push('cite_unavailable: corrupt batch identity');
        continue;
      }
      const document = citeDocument(batchRecords, branch);
      const token = encodeSliceInline(document);
      out.stats.cite_planned += STAGE_COSTS.cite.attempts - batch.attempts;
      while (batch.attempts < STAGE_COSTS.cite.attempts && batch.keys.some((key) => !results.has(key))) {
        // Reserve synchronously before handing control to the host or executor.
        batch.attempts += 1;
        batch.state = 'uncertain';
        const nonce = `${inp.nonce}.cite.${batch.id}.${batch.attempts}`;
        let env;
        out.stats.cite_attempts += 1;
        try {
          const command = verifyCommand({ ...inp, citeMode: true, citeAttempt: batch.attempts }, batch.id, nonce, token);
          env = await ctx.agent(`Run exactly this command; read --output and copy status, receipt and result.citations exactly.\n${command}`, {
            label: `cite-${batch.id}-${batch.attempts}`, agentType: 'code-gauntlet:executor',
            model: modelFor('code-gauntlet:executor', inp.policy || {}), schema: dispatchReturnSchema(CITE_ENVELOPE_SCHEMA),
          });
        } catch { env = null; }
        const accepted = trustedCitations(env, inp, batch, nonce, batch.attempts, document, token, gaps);
        for (const row of accepted) {
          const record = batchRecords.find((r) => r.id === row.id);
          const key = citeRecordKey(record, source);
          if (row.status !== 'unavailable') results.set(key, row);
          ledger.entries.find((e) => e.key === key).result = row;
        }
        batch.state = accepted.length === batchRecords.length ? 'complete' : 'uncertain';
      }
    }
  }
  const join = (f) => ({ ...f, cite_results: records.filter((r) => r.finding_id === f.id).map((r) => {
    const key = citeRecordKey(r, source);
    const row = results.get(key) || unavailable(r, 'exhausted_or_uncertain');
    const { id: _id, ...result } = row;
    return result;
  }) });
  out.findings = (inp.findings || []).map(join);
  if (inp.unverified) out.unverified = inp.unverified.map(join);
  if ([...out.findings, ...(out.unverified || [])].some((f) => f.cite_results.some((r) => r.status === 'unavailable'))) gaps.push('cite_unavailable: dependent proof could not be established');
  return out;
}
