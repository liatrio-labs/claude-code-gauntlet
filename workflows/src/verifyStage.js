import { modelFor } from './registry.js';
import { firstUnsafeNumber } from './wire.js';
import { deltaContentProof, deltaHas, encodeSliceInline, joinVerifyDeltas, pinNumericFields, projectVerifySliceFinding, sliceInputChecksum, sliceTokenChecksum } from './verifyWire.js';
import { VERIFY_ATTEMPTS_PER_SLICE, verifyInlineBudget, verifyInlineCost, verifyCommand, effectiveSliceSize, effectiveVerifyBaseBranch, planVerifySlices, predictVerifySliceInlineLength } from './capacity.js';

// Allow honest failures in the schema to avoid fabricated successes under retry
// pressure. Deltas avoid retranscribing finding fields.
const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    status: { type: 'string' }, // 'ok' | 'failed'
    receipt: {
      type: 'object',
      properties: {
        sha: { type: 'string' },
        n_in: { type: 'number' },
        nonce: { type: 'string' },
        // Missing proofs are legal to report but unsafe to trust.
        deltas_checksum: { type: 'string' },
        input_checksum: { type: 'string' },
        inline_checksum: { type: 'string' },
      },
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
            },
            required: ['id', 'verified'],
          },
        },
      },
    },
    exitCode: { type: 'number' },
    stderr: { type: 'string' },
  },
  required: ['status'], // discriminated union: receipt/result only present on status:'ok'
};

// Small slices can repair transcription failures; disclose fan-out instead of
// flooring the operator's chosen size.
const VERIFY_FANOUT_DISCLOSE_THRESHOLD = 5;

// Slices dispatch one at a time (never parallel()) so each envelope pairs to its slice by
// order. Join against held findings so an untrusted echo cannot replace their content.
export async function verifyStage(ctx, input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const sliceSize = effectiveSliceSize(inp.limits || {}, findings.length);

  // A zero-filled ledger spares consumers telling "no slices" from a missing field.
  if (findings.length === 0) {
    return { findings: [], verified: true, gaps: [], inputProof: emptyInputProof() };
  }

  const model = modelFor('code-gauntlet:executor', inp.policy || {});
  const baseBranch = effectiveVerifyBaseBranch((inp.verify || {}).baseBranch);
  const inlineBudget = verifyInlineBudget(inp);
  const plan = planVerifySlices(findings, sliceSize, inlineBudget, baseBranch, inp.hostPlatform);
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
    gaps.push(`verify_fanout: effective verifySliceSize=${sliceSize} splits ${findings.length} finding(s) into ${slices.length} slices `
      + `(above the ${VERIFY_FANOUT_DISCLOSE_THRESHOLD}-slice disclosure threshold) — up to ${slices.length * VERIFY_ATTEMPTS_PER_SLICE} `
      + `executor dispatches at ${VERIFY_ATTEMPTS_PER_SLICE} attempts per slice. ${fanoutAdvice}`);
  }

  const out = [];
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
      const encodedLength = verifyInlineCost(encodeSliceInline(content), inp.hostPlatform);
      const lengthKind = inp.hostPlatform === 'windows' ? 'Windows argv length' : 'encoded length';
      inputProof.oversize += 1;
      out.push(...degradedSlice([unit.finding]));
      gaps.push(verifyDegradeGap(
        `finding ${unit.finding && unit.finding.id !== undefined ? unit.finding.id : '(missing id)'} has ${lengthKind} ${encodedLength}, over VERIFY_INLINE_CHAR_BUDGET=${inlineBudget}`,
        1,
        findings.length,
      ));
      degradedSlices += 1;
      continue;
    }
    const { index: i, slice } = unit;
    const content = { findings: slice.map(projectVerifySliceFinding), base_branch: baseBranch };
    const expectedInputChecksum = firstUnsafeNumber(content, `slice${i}`) === null
      ? sliceInputChecksum(content)
      : null;
    const payload = encodeSliceInline(content);
    const expectedInlineChecksum = sliceTokenChecksum(payload);
    const predictedPayloadLength = predictVerifySliceInlineLength(slice, baseBranch);
    const commandCost = predictVerifySliceInlineLength(slice, baseBranch, inp.hostPlatform);
    if (payload.length !== predictedPayloadLength || commandCost > inlineBudget) {
      throw new Error(`verify inline planner produced an oversized slice ${i} (${commandCost} > ${inlineBudget})`);
    }
    const degrade = (detail) => {
      out.push(...degradedSlice(slice));
      gaps.push(verifyDegradeGap(detail, slice.length, findings.length));
      degradedSlices += 1;
    };

    // Unusable ids cannot support a delta join, so avoid spending an executor call.
    const ids = dispatchableIds(slice);
    if (!ids.ok) {
      degrade(`slice ${i}: ${ids.reason} — the delta echo is keyed by id, so this slice cannot be verified`);
      continue;
    }

    const attempt = await verifySliceWithRetry(ctx, inp, i, slice, { model, ids: ids.ids, expectedInputChecksum, expectedInlineChecksum, inlinePayload: payload });
    if (!attempt.ok) {
      if (attempt.inputFault === 'mismatch') inputProof.mismatched += 1;
      else if (attempt.inputFault === 'missing') inputProof.missing += 1;
      degrade(`slice ${i}: ${attempt.reason}`);
      continue;
    }
    if (expectedInputChecksum == null) inputProof.unprovable += 1;
    else inputProof.proven += 1;
    if (attempt.retried) {
      inputProof.retried += 1;
      if (attempt.inputFault === 'mismatch') inputProof.retriedMismatch += 1;
      else if (attempt.inputFault === 'missing') inputProof.retriedMissing += 1;
    }
    out.push(...attempt.verified);
    if (attempt.gap) gaps.push(attempt.gap);
  }

  return { findings: out, verified: degradedSlices === 0, gaps, inputProof };
}

function degradedSlice(slice) {
  return slice.map((f) => ({ ...pinNumericFields(f), origin: 'unknown' }));
}

const emptyInputProof = () => ({
  slices: 0, proven: 0, mismatched: 0, missing: 0, unprovable: 0, oversize: 0,
  retried: 0, retriedMismatch: 0, retriedMissing: 0,
});

// Name the affected count so partial verification failures remain attributable.
function verifyDegradeGap(detail, k, n) {
  return `verify: UNVERIFIED — ${detail}; ${k} of ${n} finding(s) marked origin=unknown, surfaced-classification skipped, held back to the report`;
}

// A fresh sample can repair transcription. A distinct retry nonce prevents replay
// of the first receipt from satisfying the retry; .r1 fits args.js NONCE_RE.
async function verifySliceWithRetry(c, inp, i, slice, record) {
  const { nonce } = inp;
  const attempt = (sliceNonce, label) =>
    dispatchVerifySlice(c, inp, i, slice, record, sliceNonce, label);

  const first = await attempt(`${nonce}.${i}`, `verify-slice-${i}`);
  if (first.ok) return { ok: true, verified: first.verified, gap: null, retried: false };

  const second = await attempt(`${nonce}.${i}.r1`, `verify-slice-${i}-retry`);
  if (second.ok) {
    // A trusted retry costs another dispatch without degrading findings.
    return {
      ok: true,
      verified: second.verified,
      gap: `verify-slice-retry: slice ${i}'s first executor dispatch was untrusted (${first.reason}); a second dispatch was trusted and this slice's verified findings are from that attempt`,
      retried: true,
      inputFault: first.inputFault,
    };
  }
  return {
    ok: false,
    reason: `${second.reason} — retried once after the first attempt failed (${first.reason})`,
    // Retain the first input fault if the retry fails for a different reason.
    inputFault: second.inputFault ?? first.inputFault,
  };
}

async function dispatchVerifySlice(c, inp, i, slice, { model, ids, expectedInputChecksum, expectedInlineChecksum, inlinePayload }, sliceNonce, label) {
  let env;
  try {
    env = await c.agent(verifyPrompt(inp, i, sliceNonce, inlinePayload), {
      label,
      agentType: 'code-gauntlet:executor',
      model,
      schema: VERIFY_SCHEMA,
    });
  } catch (e) {
    return { ok: false, reason: `executor threw (${(e && e.message) || 'unknown'})` };
  }
  const trust = trustSlice(env, { nonce: sliceNonce, headShaShort: inp.headShaShort, n: slice.length, ids, expectedInputChecksum, expectedInlineChecksum });
  if (!trust.ok) return trust;
  return { ok: true, verified: joinVerifyDeltas(slice, env.result.deltas) };
}

// Match ids exactly, as gauntlet.verify.wire does; trimming for matching would
// collide padded ids. Only the usability check trims.
function dispatchableIds(slice) {
  const ids = [];
  const seen = new Set();
  for (const f of slice) {
    const id = f && typeof f.id === 'string' ? f.id : '';
    if (!id.trim()) return { ok: false, reason: 'a dispatched finding has no usable id' };
    if (seen.has(id)) return { ok: false, reason: `duplicate finding id in the slice (${id})` };
    seen.add(id);
    ids.push(id);
  }
  return { ok: true, ids };
}

// Coverage binds sibling slices even when their counts match. Shape checks give
// precise diagnostics before checksums reject changed input or output.
// Trust model: a stale or confused executor. Nonces are argv-visible and proofs
// travel with data, so these checks cannot authenticate a malicious executor.
function trustSlice(env, { nonce, headShaShort, n, ids, expectedInputChecksum, expectedInlineChecksum }) {
  if (!env || typeof env !== 'object') return { ok: false, reason: 'executor returned no envelope' };
  if (env.status !== 'ok') return { ok: false, reason: `status=${env.status == null ? 'missing' : env.status}${env.stderr ? ` (${env.stderr})` : ''}` };
  const r = env.receipt || {};
  if (r.nonce !== nonce) return { ok: false, reason: `receipt nonce mismatch (got ${r.nonce == null ? 'missing' : r.nonce}, expected ${nonce})` };
  if (r.sha !== headShaShort) return { ok: false, reason: `receipt sha mismatch (got ${r.sha == null ? 'missing' : r.sha})` };
  if (r.n_in !== n) return { ok: false, reason: `receipt n_in mismatch (got ${r.n_in == null ? 'missing' : r.n_in}, expected ${n})` };
  const result = env.result || {};
  if (!Array.isArray(result.deltas)) return { ok: false, reason: 'result missing deltas array' };

  const expected = new Set(ids);
  const byId = new Map();
  for (const d of result.deltas) {
    if (!d || typeof d !== 'object') return { ok: false, reason: 'delta entry is not an object' };
    const id = typeof d.id === 'string' ? d.id : '';
    if (!id.trim()) return { ok: false, reason: 'delta entry has no id' };
    if (!expected.has(id)) return { ok: false, reason: `delta names a finding this slice did not dispatch (${id})` };
    if (byId.has(id)) return { ok: false, reason: `delta repeats a finding id (${id})` };
    if (typeof d.verified !== 'boolean') return { ok: false, reason: `delta ${id} has no boolean verified flag` };
    for (const k of ['origin', 'severity', 'elimination_reason']) {
      if (deltaHas(d, k) && typeof d[k] !== 'string') return { ok: false, reason: `delta ${id}: ${k} is not a string` };
    }
    // Integer confidence avoids cross-runtime float spelling differences.
    if (deltaHas(d, 'confidence') && !Number.isInteger(d.confidence)) {
      return { ok: false, reason: `delta ${id}: confidence is not an integer` };
    }
    const stamp = typeof d.elimination_reason === 'string' ? d.elimination_reason.trim() : '';
    if (d.verified === false && stamp === '') {
      return { ok: false, reason: `delta ${id}: eliminated without the elimination_reason stamp (fabricated elimination — the verify script always stamps a real one)` };
    }
    if (d.verified === true && stamp !== '') {
      return { ok: false, reason: `delta ${id}: verified finding carries an elimination_reason stamp` };
    }
    byId.set(id, d);
  }
  const missing = ids.filter((id) => !byId.has(id));
  if (missing.length) {
    return { ok: false, reason: `delta does not cover ${missing.length} of ${ids.length} dispatched finding(s) (first: ${missing[0]})` };
  }

  const proof = typeof r.deltas_checksum === 'string' ? r.deltas_checksum.trim() : '';
  if (!proof) return { ok: false, reason: 'receipt carries no deltas_checksum (content proof missing)' };
  const recomputed = deltaContentProof(ids, result.deltas);
  if (proof !== recomputed) {
    return { ok: false, reason: `delta content proof mismatch (receipt ${proof}, recomputed ${recomputed}) — the echoed values are not the ones the script wrote` };
  }

  // Token proof precedes value proof because it also covers unprovable numbers.
  for (const [field, expected, missingReason, name, noun] of [
    ['inline_checksum', expectedInlineChecksum, 'inline token proof missing from receipt', 'token', 'token'],
    ['input_checksum', expectedInputChecksum, 'input content proof missing from receipt', 'content', 'document'],
  ]) {
    if (expected == null) continue;
    const got = typeof r[field] === 'string' ? r[field].trim() : '';
    if (!got) return { ok: false, reason: missingReason, inputFault: 'missing' };
    if (got !== expected) return { ok: false, reason: `slice-input ${name} proof mismatch (receipt ${got}, dispatched ${expected}) — the ${noun} the script decoded is not the ${noun} this stage dispatched`, inputFault: 'mismatch' };
  }

  return { ok: true };
}

// Shell quoting preserves each argv word; retry paths stay fixed so outputs
// overwrite the first attempt.
// Read the deltas prefix because length-capped Reads give no truncation notice;
// the later full finding arrays are unnecessary for the join.
function verifyPrompt(inp, i, sliceNonce, inlinePayload) {
  return `Run exactly this command, then read the --output file and return, via the schema: its "status"; its "receipt" object with every field it contains (sha, n_in, nonce, deltas_checksum, inline_checksum, and input_checksum when present — never invent an absent one) copied exactly; and every entry of its "result.deltas" array, copied exactly. The quoted --input-inline token IS the slice document and must be reproduced character for character with no line breaks. The same file also holds large "verified" and "eliminated" arrays — do NOT return those and do not summarise them. Copy character for character: the deltas carry a checksum and a single altered value costs this slice its verification.\n${verifyCommand(inp, i, sliceNonce, inlinePayload)}`;
}
