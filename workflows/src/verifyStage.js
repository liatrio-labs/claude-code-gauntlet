// The verify stage: plan the slices, dispatch one executor per slice, trust or degrade each
// answer, and rebuild the verified findings by joining the script's deltas onto the
// findings this stage already holds.
import { modelFor } from './registry.js';
import { firstUnsafeNumber, shellWord } from './wire.js';
import { VERIFY_INLINE_CHAR_BUDGET, deltaContentProof, deltaHas, encodeSliceInline, joinVerifyDeltas, pinNumericFields, projectVerifySliceFinding, sliceInputChecksum, sliceTokenChecksum } from './verifyWire.js';
import { VERIFY_ATTEMPTS_PER_SLICE, effectiveSliceSize, effectiveVerifyBaseBranch, planVerifySlices, predictVerifySliceInlineLength } from './capacity.js';

// The discriminated-union envelope the executor returns. Both shapes are legal, so an
// honest failure is schema-valid and the executor never has to fabricate a success under
// StructuredOutput retry pressure. It carries deltas only: findings are never echoed, so
// no transcription can drop or mangle a finding field.
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
        // The three proofs are optional here and mandatory in trustSlice: an absent proof
        // is a legal thing for the executor to say and an untrusted thing to act on.
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

// verifySliceSize is not floored: a very small slice is the legitimate mitigation for a
// transcription-fidelity failure. Above this many slices the stage instead discloses the
// dispatch ceiling and the bound that closed the slices, so an operator sees whether
// raising verifySliceSize can reduce the fan-out.
const VERIFY_FANOUT_DISCLOSE_THRESHOLD = 5;

// Dispatches one executor per planned slice sequentially (never parallel()), so each
// envelope pairs to its slice by order. Degradation is per slice: an untrusted slice
// re-emits only its own findings with origin='unknown', so the damage tracks the fault.
// No finding is dropped and no echo can substitute one, because joinVerifyDeltas walks the
// dispatched findings. `verified` is true only when zero slices degraded.
export async function verifyStage(ctx, input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const sliceSize = effectiveSliceSize(inp.limits || {}, findings.length);

  // The ledger is still emitted, zero-filled, so a consumer never has to tell "no slices"
  // from "the field is missing".
  if (findings.length === 0) {
    return { findings: [], verified: true, gaps: [], inputProof: emptyInputProof() };
  }

  const model = modelFor('code-gauntlet:executor', inp.policy || {});
  const baseBranch = effectiveVerifyBaseBranch((inp.verify || {}).baseBranch);
  const plan = planVerifySlices(findings, sliceSize, VERIFY_INLINE_CHAR_BUDGET, baseBranch);
  const slices = plan.slices;

  const gaps = [];
  if (slices.length > VERIFY_FANOUT_DISCLOSE_THRESHOLD) {
    // Read the planner's tags: a skipped oversize finding prevents reconstructing a
    // boundary from the next slice. Every non-terminal slice names one of these bounds.
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

  // Oversize findings keep their original position among the slices. They are degraded
  // individually and consume no slice index, nonce or executor attempt.
  const units = [
    ...slices.map((slice, i) => ({ kind: 'slice', slice, index: i, position: findings.indexOf(slice[0]) })),
    ...plan.oversize.map((finding) => ({ kind: 'oversize', finding, position: findings.indexOf(finding) })),
  ].sort((a, b) => a.position - b.position);

  // Trusted and degraded findings alike leave in input order: applyChallenges' stable
  // sort breaks ties by array position, so any other order would vary delivery run to run.
  for (const unit of units) {
    if (unit.kind === 'oversize') {
      const content = { findings: [projectVerifySliceFinding(unit.finding)], base_branch: baseBranch };
      const encodedLength = encodeSliceInline(content).length;
      inputProof.oversize += 1;
      out.push(...degradedSlice([unit.finding]));
      gaps.push(verifyDegradeGap(
        `finding ${unit.finding && unit.finding.id !== undefined ? unit.finding.id : '(missing id)'} has encoded length ${encodedLength}, over VERIFY_INLINE_CHAR_BUDGET=${VERIFY_INLINE_CHAR_BUDGET}`,
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
    if (payload.length !== predictedPayloadLength || predictedPayloadLength > VERIFY_INLINE_CHAR_BUDGET) {
      throw new Error(`verify inline planner produced an oversized slice ${i} (${payload.length} > ${VERIFY_INLINE_CHAR_BUDGET})`);
    }
    const degrade = (detail) => {
      out.push(...degradedSlice(slice));
      gaps.push(verifyDegradeGap(detail, slice.length, findings.length));
      degradedSlices += 1;
    };

    // An echo cannot be joined without a usable id set, so a merge-side id regression
    // degrades this slice before an executor is spent on an answer the stage cannot use.
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

// A degraded slice re-emits its original findings: nothing dropped, nothing upgraded.
function degradedSlice(slice) {
  return slice.map((f) => ({ ...pinNumericFields(f), origin: 'unknown' }));
}

// The inline proof ledger, built at one site so no path can forget a key. `retried` counts
// slices trusted only on their second attempt; `retriedMismatch`/`retriedMissing` are its
// disjoint subsets whose first attempt had that input fault. `mismatched`/`missing` count
// slices that still failed after the retry, `oversize` findings too large to dispatch.
const emptyInputProof = () => ({
  slices: 0, proven: 0, mismatched: 0, missing: 0, unprovable: 0, oversize: 0,
  retried: 0, retriedMismatch: 0, retriedMissing: 0,
});

// `k of n` states the blast radius: a run that degrades 2 of 16 findings must not read
// like one that degrades all 16. The UNVERIFIED token is load-bearing: the bench checker's
// degrade scan keys on substrings of this string, and `detail` passes through verbatim.
function verifyDegradeGap(detail, k, n) {
  return `verify: UNVERIFIED — ${detail}; ${k} of ${n} finding(s) marked origin=unknown, surfaced-classification skipped`;
}

// One slice, dispatched at most VERIFY_ATTEMPTS_PER_SLICE times. The executor is a sampled
// agent, so a second attempt is a fresh sample and a plausible fix for a garbled echo, a
// truncated body, schema-retry exhaustion or a timeout. The retry carries a distinct
// nonce: with the slice nonce reused, a replay of the untrusted first receipt would
// satisfy the second attempt. The `.r1` suffix stays inside the args-waist nonce charset
// (args.js NONCE_RE). Each nonce is spelled once here and handed to both the command and
// the trust check, so the two cannot derive different values.
async function verifySliceWithRetry(c, inp, i, slice, record) {
  const { nonce } = inp;
  const attempt = (sliceNonce, label) =>
    dispatchVerifySlice(c, inp, i, slice, record, sliceNonce, label);

  const first = await attempt(`${nonce}.${i}`, `verify-slice-${i}`);
  if (first.ok) return { ok: true, verified: first.verified, gap: null, retried: false };

  const second = await attempt(`${nonce}.${i}.r1`, `verify-slice-${i}-retry`);
  if (second.ok) {
    // Disclosed without the UNVERIFIED token: it took two dispatches, but nothing degraded.
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
    // The first attempt's fault when the second failed some other way, or the ledger
    // would degrade the slice while attributing it to nothing.
    inputFault: second.inputFault ?? first.inputFault,
  };
}

// One executor dispatch for one slice. Never throws: a thrown agent() becomes an
// untrusted result carrying the message.
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

// The delta echo is joined by id, so every finding in a slice needs a usable string id no
// other shares. Each slice's join is independent, so duplicates across slices are fine.
// Ids match exactly here, in trustSlice and in the join; only the usability test trims,
// mirroring gauntlet.verify.wire's `id.strip()` guard. Matching on the trimmed form would
// collide two ids that differ only by padding into a degrade the script never produces.
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

// A slice envelope is trusted only if it is the success shape and its receipt echoes the
// dispatched nonce (this answer is for our call), head sha (the same tree) and n_in (every
// finding was loaded). Four guards beyond the receipt:
//   (1) coverage: the deltas name exactly the dispatched ids; sibling slices share a
//       length, so a count cannot bind an answer to its question.
//   (2) shape and stamp: typed fields, and only an eliminated delta carries the script's
//       elimination_reason. Redundant with (3), kept for the precise reason in the gap.
//   (3) content proof: the canonical rebuild checksums to what the script computed.
//   (4) input proof: what the script decoded is what this stage dispatched.
// Every failure degrades the whole slice and keeps every finding, so no guard can drop one.
//
// Threat model: a stale, drifting or confused executor, not a Byzantine one. The nonce is
// argv-visible and each checksum travels beside the data it covers, so a malicious
// executor could recompute both; an LLM transcribing a document cannot.
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
    // The script canonicalises confidence to an integer (_delta_confidence) so the two
    // runtimes never have to agree on a float's spelling; a non-integer is not the script's.
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

  // The token proof goes first as the stronger half: it moves on every change the value
  // proof catches, and it is always computable, where the value proof is null for a
  // number the two runtimes spell differently.
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

// One `python3 <script> --flags...` invocation of AST-safe tokens (no substitution, heredoc,
// env prefix or operator), each shellWord-quoted so a path with a space stays one argv
// word. The --input path is the code-written destination verify_findings.py uses. The nonce
// varies by attempt; the paths do not, so a retry overwrites the same output.
function verifyCommand(inp, i, sliceNonce, inlinePayload) {
  const v = inp.verify || {};
  const inPath = `${v.inputPathBase || 'phase4-input'}.slice${i}.json`;
  const outPath = `${v.outputPathBase || 'phase4-output'}.slice${i}.json`;
  const parts = [
    'python3', v.scriptPath || 'scripts/verify_findings.py',
    '--input', inPath,
    '--input-inline', inlinePayload,
    '--output', outPath,
    '--nonce', sliceNonce,
    '--head-sha', inp.headShaShort,
    '--base-branch', effectiveVerifyBaseBranch(v.baseBranch),
  ];
  if (v.diffPath) parts.push('--diff-file', v.diffPath);
  return parts.map(shellWord).join(' ');
}

// The executor is asked for a prefix of the output document: the script writes
// `result.deltas` first so a length-capped Read, which gives no truncation notice, still
// holds everything named here. The verified/eliminated arrays after it are for other
// consumers, and naming them as not wanted is cheaper than letting the agent decide.
function verifyPrompt(inp, i, sliceNonce, inlinePayload) {
  return `Run exactly this command, then read the --output file and return, via the schema: its "status"; its "receipt" object with every field it contains (sha, n_in, nonce, deltas_checksum, inline_checksum, and input_checksum when present — never invent an absent one) copied exactly; and every entry of its "result.deltas" array, copied exactly. The quoted --input-inline token IS the slice document and must be reproduced character for character with no line breaks. The same file also holds large "verified" and "eliminated" arrays — do NOT return those and do not summarise them. Copy character for character: the deltas carry a checksum and a single altered value costs this slice its verification.\n${verifyCommand(inp, i, sliceNonce, inlinePayload)}`;
}
