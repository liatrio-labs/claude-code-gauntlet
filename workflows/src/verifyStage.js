import { defineAgentCall } from './agentCalls.js';
import { canonicalRange, clearLocation } from './location.js';
import { hostPlatformForRoots } from './paths.js';
import { FINDING_PROP_TYPES, LOCATION_STATUSES, RESOLVED_SITE_SCHEMA, VERIFY_FACT_PROPERTIES, modelFor } from './registry.js';
import { firstUnsafeNumber } from './wire.js';
import { deltaContentProof, deltaHas, encodeSliceInline, joinVerifyDeltas, pinNumericFields, projectVerifySliceFinding, recordProof, sliceInputChecksum, sliceTokenChecksum, stripVerifyFacts } from './verifyWire.js';
import { VERIFY_ATTEMPTS_PER_SLICE, verifySingletonRetryBudget, verifyDispatchCount, verifyInlineBudgets, verifyInlineCost, verifyCommand, effectiveSliceSize, effectiveVerifyBaseBranch, planVerifySlices, predictVerifySliceInlineLength } from './capacity.js';
import { measurementDecisions, traceDecision } from './measurementTrace.js';

// Allow honest failures in the schema to avoid fabricated successes under retry
// pressure. Deltas avoid retranscribing finding fields.
const VERIFY_RECEIPT_FIELDS = {
  sha: { type: 'string' },
  n_in: { type: 'number' },
  nonce: { type: 'string' },
  deltas_checksum: { type: 'string' },
  input_checksum: { type: 'string' },
  inline_checksum: { type: 'string' },
  base_branch: { type: 'string' },
  diff_source: {
    type: 'object',
    properties: { path: { type: ['string', 'null'] }, read: { type: 'boolean' } },
    required: ['path', 'read'],
  },
  record_proofs: {
    type: 'array',
    items: {
      type: 'object',
      properties: { id: { type: 'string' }, checksum: { type: ['string', 'null'] } },
      required: ['id', 'checksum'],
    },
  },
};
const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    status: { type: 'string' },
    receipt: {
      type: 'object',
      properties: VERIFY_RECEIPT_FIELDS,
    },
    result: {
      type: 'object',
      properties: {
        deltas: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              id: { type: 'string' },
              verified: { type: 'boolean' },
              origin: { type: 'string' },
              severity: { type: 'string' },
              confidence: { type: 'number' },
              elimination_reason: { type: 'string' },
              line_start: { type: 'integer', minimum: 1 },
              line_end: { type: 'integer', minimum: 1 },
              location_status: { type: 'string', enum: LOCATION_STATUSES },
              diff_validation: { type: 'object', properties: { in_diff: { type: ['boolean', 'null'] } }, required: ['in_diff'], additionalProperties: false },
              resolved_sites: RESOLVED_SITE_SCHEMA,
              anchor: FINDING_PROP_TYPES.anchor,
              ...VERIFY_FACT_PROPERTIES,
            },
            required: ['id', 'verified', 'location_status'],
          },
        },
      },
    },
    exitCode: { type: 'number' },
    stderr: { type: 'string' },
  },
  required: ['status'],
};

const callVerify = defineAgentCall('verify', () => VERIFY_SCHEMA);

// Small slices can repair transcription failures; disclose fan-out instead of
// flooring the operator's chosen size.
const VERIFY_FANOUT_DISCLOSE_THRESHOLD = 5;

// Sequential dispatch pairs receipts to slices; held findings prevent echo substitution.
export async function verifyStage(ctx, input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const sliceSize = effectiveSliceSize(inp.limits || {}, findings.length);

  // A zero-filled ledger spares consumers telling "no slices" from a missing field.
  if (findings.length === 0) {
    return { findings: [], eliminated: [], verified: true, gaps: [], inputProof: emptyInputProof() };
  }

  // Source selection must be explicit before any executor can read HEAD or the worktree.
  if (!['pr', 'mr', 'branch', 'local'].includes(inp.verify?.targetKind)) {
    throw new Error('verify.targetKind must be one of pr, mr, branch, local');
  }

  const model = modelFor('code-gauntlet:executor', inp.policy || {});
  const baseBranch = effectiveVerifyBaseBranch((inp.verify || {}).baseBranch);
  const hostPlatform = hostPlatformForRoots(inp);
  const budgets = verifyInlineBudgets(inp);
  const plan = planVerifySlices(findings, sliceSize, budgets, baseBranch, hostPlatform);
  const slices = plan.slices;

  const gaps = [];
  if (slices.length > VERIFY_FANOUT_DISCLOSE_THRESHOLD) {
    // Planner tags survive skipped oversize findings, unlike inferred boundaries.
    const bounds = new Set(plan.closeReasons.slice(0, -1));
    const fanoutAdvice = [
      ['budget', 'The inline character budget bound this split; raising verifySliceSize will not reduce this fan-out.'],
      ['oversize', 'An oversize finding forced this split; raising verifySliceSize will not reduce this fan-out.'],
      ['count', 'Raise verifySliceSize to reduce fan-out.'],
    ].find(([reason]) => bounds.has(reason))[1];
    const dispatches = verifyDispatchCount(slices);
    const batched = slices.length * VERIFY_ATTEMPTS_PER_SLICE;
    gaps.push(`verify_fanout: effective verifySliceSize=${sliceSize} splits ${findings.length} finding(s) into ${slices.length} slices `
      + `(above the ${VERIFY_FANOUT_DISCLOSE_THRESHOLD}-slice disclosure threshold) — up to ${dispatches} `
      + `executor dispatches (${batched} initial or r1 calls and up to ${dispatches - batched} singleton retries). ${fanoutAdvice}`);
  }

  const out = [];
  const eliminated = [];
  let degradedSlices = 0;
  const inputProof = { ...emptyInputProof(), slices: slices.length };

  // Oversize findings consume no nonce, so later slices retain their identity.
  const units = [
    ...slices.map((slice, i) => ({ kind: 'slice', slice, index: i, position: findings.indexOf(slice[0]) })),
    ...plan.oversize.map((finding) => ({ kind: 'oversize', finding, position: findings.indexOf(finding) })),
  ].sort((a, b) => a.position - b.position);

  // Preserve input order because applyChallenges breaks ranking ties by position.
  for (const unit of units) {
    if (unit.kind === 'oversize') {
      const content = { findings: [projectVerifySliceFinding(unit.finding)], base_branch: baseBranch };
      const encodedLength = verifyInlineCost(encodeSliceInline(content), hostPlatform);
      inputProof.oversize += 1;
      inputProof.degradedFindings += 1;
      out.push(...degradedSlice([unit.finding]));
      gaps.push(verifyDegradeGap(
        `finding ${unit.finding && unit.finding.id !== undefined ? unit.finding.id : '(missing id)'} has inline cost ${encodedLength}, over hard cap=${budgets.hard} (copy target=${budgets.copy})`,
        1,
        findings.length,
      ));
      degradedSlices += 1;
      continue;
    }
    const { index: i, slice } = unit;
    const record = prepareVerifySlice(slice, baseBranch);
    const payload = record.inlinePayload;
    const predictedPayloadLength = predictVerifySliceInlineLength(slice, baseBranch);
    const commandCost = predictVerifySliceInlineLength(slice, baseBranch, hostPlatform);
    const payloadLengthMismatch = payload.length !== predictedPayloadLength;
    const exceedsHardCap = commandCost > budgets.hard;
    const exceedsCopyTarget = slice.length > 1 && commandCost > budgets.copy;
    if (payloadLengthMismatch || exceedsHardCap || exceedsCopyTarget) {
      const failed = [
        [payloadLengthMismatch, 'payload length mismatch'],
        [exceedsHardCap, 'hard cap exceeded'],
        [exceedsCopyTarget, 'multi-finding copy target exceeded'],
      ].filter(([fails]) => fails).map(([, name]) => name).join(', ');
      throw new Error(`verify inline planner invariant failed for slice ${i}: ${failed} `
        + `(payload length=${payload.length}, predicted=${predictedPayloadLength}, inline cost=${commandCost}, copy target=${budgets.copy}, hard cap=${budgets.hard})`);
    }
    const degrade = (detail) => {
      out.push(...degradedSlice(slice));
      gaps.push(verifyDegradeGap(detail, slice.length, findings.length));
      degradedSlices += 1;
      inputProof.degradedFindings += slice.length;
    };

    // Unusable ids cannot support a delta join, so avoid spending an executor call.
    const ids = dispatchableIds(slice);
    if (!ids.ok) {
      degrade(`slice ${i}: ${ids.reason} — the delta echo is keyed by id, so this slice cannot be verified`);
      continue;
    }

    const attempt = await verifySliceWithRetry(ctx, inp, i, slice, { ...record, model });
    if (attempt.proven) inputProof.proven += 1;
    if (!attempt.degraded && record.expectedInputChecksum == null) inputProof.unprovable += 1;
    inputProof.localised += attempt.localised;
    inputProof.retriedFindings += attempt.retriedFindings;
    inputProof.degradedFindings += attempt.degraded;
    if (attempt.retriedFindings && !attempt.degraded) {
      inputProof.retried += 1;
      if (attempt.inputFault === 'mismatch') inputProof.retriedMismatch += 1;
      else if (attempt.inputFault === 'missing') inputProof.retriedMissing += 1;
    } else {
      if (attempt.inputFault === 'mismatch') inputProof.mismatched += 1;
      else if (attempt.inputFault === 'missing') inputProof.missing += 1;
    }
    out.push(...attempt.verified);
    eliminated.push(...attempt.eliminated);
    if (attempt.degraded) {
      degradedSlices += 1;
      gaps.push(verifyDegradeGap(`slice ${i}: ${attempt.reason}`, attempt.degraded, findings.length));
    } else if (attempt.retriedFindings) {
      gaps.push(`verify-slice-retry: slice ${i}'s first executor dispatch left ${attempt.retriedFindings} of ${slice.length} finding(s) unresolved (${attempt.reason}); retries repaired them`);
    }
  }

  return measurementDecisions({ findings: out, eliminated, verified: degradedSlices === 0, gaps, inputProof },
    eliminated.filter((f) => f.trace_id).map((f) => traceDecision(f, 'verify_eliminated', f.elimination_reason)));
}

function degradedSlice(slice) {
  return slice.map((f) => stripVerifyFacts({ ...clearLocation(pinNumericFields(f)), ...(f.sites ? { sites: [], resolved_sites: [] } : {}), origin: 'unknown' }));
}

const emptyInputProof = () => ({
  slices: 0, proven: 0, mismatched: 0, missing: 0, unprovable: 0, oversize: 0,
  retried: 0, retriedMismatch: 0, retriedMissing: 0,
  localised: 0, retriedFindings: 0, degradedFindings: 0,
});

// Name the affected count so partial verification failures remain attributable.
function verifyDegradeGap(detail, k, n) {
  return `verify: UNVERIFIED — ${detail}; ${k} of ${n} finding(s) in stage marked origin=unknown, surfaced-classification skipped, held back to the report`;
}

function prepareVerifySlice(slice, baseBranch) {
  const content = { findings: slice.map(projectVerifySliceFinding), base_branch: baseBranch };
  const inlinePayload = encodeSliceInline(content);
  return {
    ids: slice.map((f) => f.id),
    expectedProofs: new Map(content.findings.map((f) => [f.id, recordProof(f)])),
    expectedInputChecksum: firstUnsafeNumber(content, 'slice') === null ? sliceInputChecksum(content) : null,
    expectedInlineChecksum: sliceTokenChecksum(inlinePayload),
    baseBranch, inlinePayload,
  };
}

// A fresh sample can repair transcription. A distinct retry nonce prevents replay
// of another dispatch's receipt from satisfying a retry.
async function verifySliceWithRetry(c, inp, i, slice, record) {
  const first = await dispatchVerifySlice(c, inp, i, slice, record, `${inp.nonce}.${i}`, `verify-slice-${i}`);
  const accepted = new Map();
  const matchedProofIds = new Set();
  const provenIds = new Set();
  let inputFault;
  const collect = (result) => {
    // A later success cannot erase a transcription fault from the ledger.
    if (inputFault !== 'mismatch') inputFault = result.inputFault ?? inputFault;
    for (const d of result.deltas || []) {
      accepted.set(d.id, d);
      if (result.proven) provenIds.add(d.id);
    }
    for (const id of result.matchedProofIds || []) matchedProofIds.add(id);
  };
  collect(first);
  const unresolved = slice.filter((f) => !accepted.has(f.id));
  const failures = new Map();
  if (unresolved.length && VERIFY_ATTEMPTS_PER_SLICE > 1) {
    const retryRecord = { ...prepareVerifySlice(unresolved, record.baseBranch), model: record.model };
    const batch = await dispatchVerifySlice(c, inp, i, unresolved, retryRecord, `${inp.nonce}.${i}.r1`, `verify-slice-${i}-retry`, 'r1');
    collect(batch);
    // Repeating an already isolated copy cannot narrow the fault any further.
    // A batch that failed as a whole names no ids, so each unresolved finding is retried alone.
    const eligible = new Set(unresolved.length > 1 ? (batch.retryIds || unresolved.map((f) => f.id)) : []);
    const pending = slice.map((finding, index) => ({ finding, index }))
      .filter(({ finding }) => !accepted.has(finding.id));
    const singletonBudget = verifySingletonRetryBudget(slice.length);
    const singletons = pending.filter(({ finding }) => eligible.has(finding.id)).slice(0, singletonBudget);
    for (const { finding } of pending) failures.set(finding.id, eligible.has(finding.id)
      ? `singleton retry cap=${singletonBudget} reached after batched retry` : batch.reason);
    if (singletons.length) {
      let results;
      try {
        results = await c.parallel(singletons.map(({ finding, index }) => () => {
          const attempt = `r2.${index}`;
          const singleRecord = { ...prepareVerifySlice([finding], record.baseBranch), model: record.model };
          return dispatchVerifySlice(c, inp, i, [finding], singleRecord, `${inp.nonce}.${i}.${attempt}`, `verify-slice-${i}-retry-${index}`, attempt);
        }));
      } catch (e) {
        results = singletons.map(() => ({ reason: `singleton dispatch failed (${e?.message || 'unknown'})` }));
      }
      for (const [j, { finding }] of singletons.entries()) {
        const result = results?.[j] || { reason: 'singleton retry returned no result' };
        collect(result);
        failures.set(finding.id, result.reason);
      }
    }
  }
  const groups = new Map();
  for (const f of slice) {
    if (accepted.has(f.id)) continue;
    const reason = failures.get(f.id) || first.reason;
    if (!groups.has(reason)) groups.set(reason, []);
    groups.get(reason).push(f.id);
  }
  const degraded = slice.length - accepted.size;
  return {
    eliminated: slice.filter((f) => accepted.get(f.id)?.verified === false)
      .map((f) => ({ ...f, eliminated_by: 'verify', elimination_reason: accepted.get(f.id).elimination_reason })),
    // Accepted membership includes trusted eliminations exactly once after recovery.
    verified: slice.flatMap((f) => accepted.has(f.id)
      ? joinVerifyDeltas([f], [accepted.get(f.id)], matchedProofIds) : degradedSlice([f])),
    degraded,
    localised: unresolved.length ? slice.length - unresolved.length : 0,
    retriedFindings: unresolved.length,
    proven: record.expectedInputChecksum != null && provenIds.size === slice.length,
    reason: degraded ? [...groups].map(([reason, ids]) => `${reason} [ids: ${ids.map((id) => JSON.stringify(id)).join(', ')}]`).join('; ')
      + (groups.has(first.reason) ? '' : `; first attempt: ${first.reason}`) : first.reason,
    inputFault,
  };
}

async function dispatchVerifySlice(c, inp, i, slice, record, sliceNonce, label, attempt = '') {
  let env;
  try {
    env = await callVerify(c, verifyPrompt(inp, i, sliceNonce, record.inlinePayload, attempt), {
      label,
      agentType: 'code-gauntlet:executor',
      model: record.model,
    });
  } catch (e) {
    return { ok: false, reason: `executor threw (${(e && e.message) || 'unknown'})` };
  }
  return trustSlice(env, { ...record, diffPath: (inp.verify || {}).diffPath || null,
    nonce: sliceNonce, headShaShort: inp.headShaShort, n: slice.length });
}

// Match ids exactly, as gauntlet.verify.wire does; trimming for matching would
// collide padded ids. Only the usability check trims.
function dispatchableIds(slice) {
  const seen = new Set();
  for (const f of slice) {
    const id = f && typeof f.id === 'string' ? f.id : '';
    if (!id.trim()) return { ok: false, reason: 'a dispatched finding has no usable id' };
    if (seen.has(id)) return { ok: false, reason: `duplicate finding id in the slice (${id})` };
    seen.add(id);
  }
  return { ok: true };
}

// Trust model: a stale or confused executor. Nonces are argv-visible and proofs
// travel with data, so these checks cannot authenticate a malicious executor.
function trustSlice(env, { nonce, headShaShort, n, ids, expectedProofs, baseBranch, diffPath, expectedInputChecksum, expectedInlineChecksum }) {
  if (!env || typeof env !== 'object') return { ok: false, reason: 'executor returned no envelope' };
  if (env.status !== 'ok') return { ok: false, reason: `status=${env.status == null ? 'missing' : env.status}${env.stderr ? ` (${env.stderr})` : ''}` };
  const r = env.receipt || {};
  if (r.nonce !== nonce) return { ok: false, reason: `receipt nonce mismatch (got ${r.nonce == null ? 'missing' : r.nonce}, expected ${nonce})` };
  if (r.sha !== headShaShort) return { ok: false, reason: `receipt sha mismatch (got ${r.sha == null ? 'missing' : r.sha})` };
  if (r.base_branch !== baseBranch) return { ok: false, reason: 'receipt base_branch mismatch' };
  if (r.diff_source?.path !== diffPath || r.diff_source?.read !== true) {
    return { ok: false, reason: 'receipt diff_source mismatch or unreadable' };
  }
  if (!Array.isArray(r.record_proofs)) return {
    ok: false, reason: 'receipt missing or malformed record_proofs',
    inputFault: r.record_proofs == null ? 'missing' : 'mismatch',
  };
  const proofs = new Map();
  for (const row of r.record_proofs) {
    if (!row || typeof row !== 'object' || typeof row.id !== 'string' || !row.id.trim()
      || (typeof row.checksum !== 'string' && row.checksum !== null)) {
      return { ok: false, reason: 'receipt missing or malformed record_proofs', inputFault: 'mismatch' };
    }
    if (proofs.has(row.id)) return { ok: false, reason: `record_proofs repeats a finding id (${row.id})` };
    proofs.set(row.id, row.checksum);
  }
  const result = env.result || {};
  if (!Array.isArray(result.deltas)) return { ok: false, reason: 'result missing deltas array' };

  const expected = new Set(ids);
  const byId = new Map();
  const malformedLocations = new Set();
  for (const d of result.deltas) {
    if (!d || typeof d !== 'object') return { ok: false, reason: 'delta entry is not an object' };
    const id = typeof d.id === 'string' ? d.id : '';
    if (!id.trim()) return { ok: false, reason: 'delta entry has no id' };
    if (byId.has(id)) return { ok: false, reason: `delta repeats a finding id (${id})` };
    if (typeof d.verified !== 'boolean') return { ok: false, reason: `delta ${id} has no boolean verified flag` };
    for (const k of ['origin', 'severity', 'elimination_reason']) {
      if (deltaHas(d, k) && typeof d[k] !== 'string') return { ok: false, reason: `delta ${id}: ${k} is not a string` };
    }
    // Integer confidence avoids cross-runtime float spelling differences.
    if (deltaHas(d, 'confidence') && !Number.isInteger(d.confidence)) {
      return { ok: false, reason: `delta ${id}: confidence is not an integer` };
    }
    if (!LOCATION_STATUSES.includes(d.location_status)
      || (d.location_status === 'unconfirmed'
        ? Object.hasOwn(d, 'line_start') || Object.hasOwn(d, 'line_end')
        : !canonicalRange(d))) malformedLocations.add(id);
    if (d.diff_validation !== undefined && (!d.diff_validation || typeof d.diff_validation !== 'object'
      || ![true, false, null].includes(d.diff_validation.in_diff))) malformedLocations.add(id);
    if (d.resolved_sites !== undefined && (!Array.isArray(d.resolved_sites) || d.resolved_sites.some((site) =>
      !site || typeof site.file !== 'string' || typeof site.anchor !== 'string' || !canonicalRange(site)
      || ![true, false, null].includes(site.diff_validation?.in_diff)))) malformedLocations.add(id);
    const stamp = typeof d.elimination_reason === 'string' ? d.elimination_reason.trim() : '';
    if (d.verified === false && stamp === '') {
      return { ok: false, reason: `delta ${id}: eliminated without the elimination_reason stamp (fabricated elimination — the verify script always stamps a real one)` };
    }
    if (d.verified === true && stamp !== '') {
      return { ok: false, reason: `delta ${id}: verified finding carries an elimination_reason stamp` };
    }
    byId.set(id, d);
  }
  if (proofs.size !== byId.size || [...proofs.keys()].some((id) => !byId.has(id))) {
    return { ok: false, reason: 'record_proofs ids differ from delta ids' };
  }
  if ([...proofs.keys()].filter((id) => !expected.has(id)).length > 1) {
    return { ok: false, reason: 'receipt names multiple findings this slice did not dispatch' };
  }

  const proof = typeof r.deltas_checksum === 'string' ? r.deltas_checksum.trim() : '';
  if (!proof) return { ok: false, reason: 'receipt carries no deltas_checksum (content proof missing)' };
  const recomputed = deltaContentProof([...proofs.keys()], result.deltas);
  if (proof !== recomputed) {
    return { ok: false, reason: `delta content proof mismatch (receipt ${proof}, recomputed ${recomputed}) — the echoed values are not the ones the script wrote` };
  }

  const wholeSlice = [...expectedProofs.values()].some((proof) => proof === null);
  let diagnostic = r.n_in === n ? null : {
    reason: `receipt n_in mismatch (got ${r.n_in == null ? 'missing' : r.n_in}, expected ${n})`, inputFault: 'mismatch',
  };
  for (const [field, expected, missingReason, name, noun] of [
    ['inline_checksum', expectedInlineChecksum, 'inline token proof missing from receipt', 'token', 'token'],
    ['input_checksum', expectedInputChecksum, 'input content proof missing from receipt', 'content', 'document'],
  ]) {
    // Proven decoded records do not depend on how the executor spelled the token.
    if (expected == null || (field === 'inline_checksum' && !wholeSlice)) continue;
    const got = typeof r[field] === 'string' ? r[field].trim() : '';
    if (!got) diagnostic ??= { reason: missingReason, inputFault: 'missing' };
    else if (got !== expected) diagnostic ??= { reason: `slice-input ${name} proof mismatch (receipt ${got}, dispatched ${expected}) — the ${noun} the script decoded is not the ${noun} this stage dispatched`, inputFault: 'mismatch' };
  }

  // An unsupported expected value cannot safely be localised by record proof.
  const matchesRecordProof = (id) => expectedProofs.get(id) !== null && proofs.get(id) === expectedProofs.get(id);
  if (wholeSlice && diagnostic) return { ok: false, ...diagnostic };
  const deltas = ids.filter((id) => !malformedLocations.has(id) && byId.has(id) && (wholeSlice || matchesRecordProof(id)))
    .map((id) => byId.get(id));
  if (wholeSlice && deltas.length !== ids.length) return { ok: false, reason: 'delta does not cover dispatched findings' };
  return {
    ok: true, deltas,
    retryIds: wholeSlice ? [] : ids.filter((id) => !byId.has(id) || !matchesRecordProof(id)),
    matchedProofIds: deltas.filter((d) => matchesRecordProof(d.id)).map((d) => d.id),
    proven: !diagnostic && deltas.length === ids.length,
    reason: diagnostic?.reason || `record proof missing or mismatched for ${ids.length - deltas.length} of ${n} finding(s)`,
    inputFault: deltas.length !== ids.length
      ? (ids.some((id) => !proofs.has(id)) ? 'missing' : 'mismatch') : diagnostic?.inputFault,
  };
}

// Read the deltas prefix because length-capped Reads give no truncation notice;
// the later full finding arrays are unnecessary for the join.
function verifyPrompt(inp, i, sliceNonce, inlinePayload, attempt) {
  return 'Reproduce the quoted --input-inline token character for character with no line breaks.\n'
    + verifyCommand(inp, i, sliceNonce, inlinePayload, attempt);
}
