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
        // The delta echo's content proof (fnv1a32 over the script's own serialisation).
        // Optional in the SCHEMA, mandatory in trustSlice — an absent proof is a legal
        // thing for the executor to say and an untrusted thing for the workflow to act on.
        deltas_checksum: { type: 'string' },
        // The inline content proof: fnv1a32 over the document the script decoded,
        // compared in trustSlice against the checksum this stage computed over the
        // content it dispatched. Optional in the SCHEMA, mandatory in trustSlice.
        input_checksum: { type: 'string' },
        // The token proof: fnv1a32 over the --input-inline token the script received,
        // before it decoded anything. Optional in the SCHEMA, mandatory in trustSlice.
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

// verifySliceSize is deliberately NOT floored. A very small slice is the legitimate
// mitigation for a transcription-fidelity failure (less content per executor round trip,
// less to mismatch), so clamping it upward would remove the knob an operator reaches for.
// Instead: degrade-and-disclose. When the effective verifySliceSize produces MORE than
// VERIFY_FANOUT_DISCLOSE_THRESHOLD slices, verifyStage pushes a gap naming the effective
// size, the slice count, the dispatch ceiling (slices * VERIFY_ATTEMPTS_PER_SLICE) and the
// bound that closed the slices, so an operator sees whether raising verifySliceSize can
// reduce the fan-out.
const VERIFY_FANOUT_DISCLOSE_THRESHOLD = 5;

// verifyStage(ctx, input) -> { findings, verified: boolean, gaps, inputProof }
// Dispatches one `executor` per planned slice — one call, plus at most one retry —
// SEQUENTIALLY (not parallel()), so each envelope pairs to its slice by order.
//
// DEGRADATION IS PER SLICE. An untrusted slice (receipt mismatch, status:'failed', an
// agent() throw, or a transcription that changes the inline document) degrades ONLY ITS
// OWN findings to the UNVERIFIED shape (origin='unknown', surfaced-classification skipped)
// and the loop keeps going, so the size of the damage tracks the size of the fault.
//
// Findings are never dropped and success is never fabricated: every finding leaves this
// stage either as its slice's trusted verified output or as itself with origin='unknown'.
// `verified` is true only when ZERO slices degraded, so the one top-level boolean keeps
// meaning "this whole run's classification is trustworthy".
//
// The never-drop half is structural: joinVerifyDeltas walks the DISPATCHED findings and
// enriches them, so no echo, however wrong, can substitute or delete a finding. The worst
// an untrusted echo achieves is its own slice's honest degrade.
export async function verifyStage(ctx, input) {
  const c = ctx;
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const limits = inp.limits || {};
  const policy = inp.policy || {};
  const nonce = inp.nonce;
  const headShaShort = inp.headShaShort;
  const sliceSize = effectiveSliceSize(limits, findings.length);
  const verify = inp.verify || {};

  // Empty set: nothing to verify, trivially trusted (no executor dispatched). The
  // counters are still emitted, zero-populated: a consumer that has to distinguish
  // "no slices" from "the field is missing" is a consumer that will get it wrong.
  if (findings.length === 0) {
    return { findings: [], verified: true, gaps: [], inputProof: emptyInputProof() };
  }

  const model = modelFor('code-gauntlet:executor', policy);

  const baseBranch = effectiveVerifyBaseBranch(verify.baseBranch);
  const plan = planVerifySlices(findings, sliceSize, VERIFY_INLINE_CHAR_BUDGET, baseBranch);
  const slices = plan.slices;

  // The planner records the actual bound that closed each slice. Read those tags directly:
  // the next planned slice may begin after an oversize finding that was skipped, so its first
  // finding cannot reliably reconstruct the boundary cause.
  const fanoutBounds = { countBound: false, budgetBound: false, oversizeBound: false };
  for (let i = 0; i + 1 < slices.length; i += 1) {
    if (plan.closeReasons[i] === 'count') fanoutBounds.countBound = true;
    else if (plan.closeReasons[i] === 'budget') fanoutBounds.budgetBound = true;
    else if (plan.closeReasons[i] === 'oversize') fanoutBounds.oversizeBound = true;
  }

  // Degrade-and-disclose, not abort: a small verifySliceSize is a legitimate
  // transcription-fidelity mitigation, not a mistake to reject, but its dispatch cost is
  // real and otherwise invisible until the run runs long or worstCaseAgentCount rejects it
  // outright. Collected here rather than pushed straight into `gaps` so it lands ahead of
  // any per-slice degrade gap, in the order this stage discovers information.
  let fanoutAdvice;
  if (fanoutBounds.budgetBound) {
    fanoutAdvice = 'The inline character budget bound this split; raising verifySliceSize will not reduce this fan-out.';
  } else if (fanoutBounds.oversizeBound) {
    fanoutAdvice = 'An oversize finding forced this split; raising verifySliceSize will not reduce this fan-out.';
  } else if (fanoutBounds.countBound) {
    fanoutAdvice = 'Raise verifySliceSize to reduce fan-out.';
  } else {
    fanoutAdvice = 'The split bound could not be classified; inspect the effective slice size and inline budget.';
  }
  const fanoutGaps = slices.length > VERIFY_FANOUT_DISCLOSE_THRESHOLD
    ? [`verify_fanout: effective verifySliceSize=${sliceSize} splits ${findings.length} finding(s) into ${slices.length} slices `
      + `(above the ${VERIFY_FANOUT_DISCLOSE_THRESHOLD}-slice disclosure threshold) — up to ${slices.length * VERIFY_ATTEMPTS_PER_SLICE} `
      + `executor dispatches at ${VERIFY_ATTEMPTS_PER_SLICE} attempts per slice. ${fanoutAdvice}`]
    : [];

  const out = [];
  const gaps = [...fanoutGaps];
  let degradedSlices = 0;
  const inputProof = { ...emptyInputProof(), slices: slices.length, oversize: 0 };

  // Keep oversize findings in original order with planned slices. They are degraded
  // individually, never dispatched, and do not consume a slice nonce or executor
  // attempt. The planner returns them beside the slices and their closeReasons.
  const units = [
    ...slices.map((slice, i) => ({ kind: 'slice', slice, index: i, position: findings.indexOf(slice[0]) })),
    ...plan.oversize.map((finding) => ({ kind: 'oversize', finding, position: findings.indexOf(finding) })),
  ].sort((a, b) => a.position - b.position);

  // The output is assembled in SLICE-INDEX order — trusted output and degraded originals
  // alike — because downstream ranking (applyChallenges' stable sort on severity then
  // confidence) breaks ties by array position. Completion order or "trusted first" would
  // make delivery ordering vary run to run for tied findings.
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
    const i = unit.index;
    const slice = unit.slice;
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

    // The delta echo is keyed by finding id, so a slice whose own findings have no usable
    // id set cannot be joined no matter how faithfully the executor answers. Checked
    // BEFORE dispatch: spending an executor on an answer this stage could not use is
    // strictly worse than degrading now. Degrade-and-disclose rather than reject, so a
    // merge-side id regression costs one slice its classification and says so, instead of
    // failing the run (post-merge ids are present by construction — mergeFindings drops
    // id-less findings — which is why this is a guard, not a routine path).
    const ids = dispatchableIds(slice);
    if (!ids.ok) {
      degrade(`slice ${i}: ${ids.reason} — the delta echo is keyed by id, so this slice cannot be verified`);
      continue;
    }

    const attempt = await verifySliceWithRetry(c, inp, i, slice, { model, nonce, headShaShort, ids: ids.ids, expectedInputChecksum, expectedInlineChecksum, inlinePayload: payload });
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
    // Trusted: this slice's OWN findings, enriched by the script's delta (origin
    // new/surfaced, the surfaced severity downgrade, the factual-verification confidence
    // re-score). Everything the script did not touch — description, evidence,
    // cross_file_refs, suggestion, every per-dimension extra — is the value this stage
    // already held, so it cannot be dropped or mangled in transcription. Findings the
    // script really eliminated are absent by design (their delta says verified:false, and
    // trustSlice requires the script's elimination stamp on every one of them).
    out.push(...attempt.verified);
    if (attempt.gap) gaps.push(attempt.gap);
  }

  return { findings: out, verified: degradedSlices === 0, gaps, inputProof };
}

// One slice's share of the UNVERIFIED degradation: its ORIGINAL findings re-emitted with
// origin='unknown' (surfaced-classification skipped). Nothing is dropped and nothing is
// upgraded; numerics are pinned because this path re-emits discovery-shaped findings.
function degradedSlice(slice) {
  return slice.map((f) => ({ ...pinNumericFields(f), origin: 'unknown' }));
}

// The inline proof ledger. One object, one construction site, so a new path
// cannot forget a key. `retried` counts slices trusted only
// on their second inline transcription attempt; `retriedMismatch`/`retriedMissing` are
// disjoint subsets whose FIRST attempt had that input fault and whose second attempt was
// trusted. `mismatched`/`missing` count only slices that still failed after the retry.
// `oversize` counts findings that could not be dispatched within the measured command budget.
const emptyInputProof = () => ({
  slices: 0, proven: 0, mismatched: 0, missing: 0, unprovable: 0, oversize: 0,
  retried: 0, retriedMismatch: 0, retriedMissing: 0,
});

// The loud gap for one degraded slice. `detail` names the slice and carries the
// underlying reason, so a reader can tell
// WHICH share of the run lost its classification and why; `k of n` states the blast
// radius directly, which is the whole point of per-slice degradation — a run that
// degrades 2 of 16 findings must not read the same as one that degrades all 16.
//
// The UNVERIFIED token is load-bearing: tests and the bench checker's degrade scan key
// on substrings of this string, and the underlying reason is passed through VERBATIM.
// Verify failures carry the inline-channel reason directly; they have no registered
// sentinel of the kind the persistence functions keep.
function verifyDegradeGap(detail, k, n) {
  return `verify: UNVERIFIED — ${detail}; ${k} of ${n} finding(s) marked origin=unknown, surfaced-classification skipped`;
}

// verifySliceWithRetry -> { ok:true, verified, gap, retried } | { ok:false, reason }
// One slice, dispatched at most VERIFY_ATTEMPTS_PER_SLICE times: the first executor call
// and — only if that one came back untrusted — exactly ONE re-dispatch before degrading.
// The executor is a sampled agent, not a function, so a second inline transcription is
// a fresh sample and a plausible fix for a dropped/garbled receipt echo, a truncated
// result body, a fabricated elimination, schema-retry exhaustion, or timeout throw.
//
// The retry carries a DISTINCT nonce (`${nonce}.${i}.r1`). Re-using the slice nonce
// would move the exact confusion trustSlice defends against from space (two equal-length
// slices satisfying each other's receipts) into time (attempt 2 satisfied by a replay of
// attempt 1's receipt) — and since attempt 1 was untrusted, a fresh receipt is precisely
// the thing we are re-dispatching to obtain. The suffix stays inside the args-waist nonce
// charset (args.js NONCE_RE) and inside one AST-safe word token.
//
// The nonce is computed once here and threaded into verifyPrompt/verifyCommand, so the
// command and the trust check cannot derive different values.
async function verifySliceWithRetry(c, inp, i, slice, { model, nonce, headShaShort, ids, expectedInputChecksum, expectedInlineChecksum, inlinePayload }) {
  const attempt = (sliceNonce, label) =>
    dispatchVerifySlice(c, inp, i, slice, { model, headShaShort, sliceNonce, label, ids, expectedInputChecksum, expectedInlineChecksum, inlinePayload });

  const first = await attempt(`${nonce}.${i}`, `verify-slice-${i}`);
  if (first.ok) return { ok: true, verified: first.verified, gap: null, retried: false };

  const second = await attempt(`${nonce}.${i}.r1`, `verify-slice-${i}-retry`);
  if (second.ok) {
    // Disclosed, not degraded: no finding lost its classification, but the run states
    // that it took two dispatches to get there (the persist path's retry discloses the
    // same way). Deliberately carries no UNVERIFIED token — nothing was degraded.
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
    // The FIRST attempt's fault when the second failed some other way. Without the
    // fallback a slice whose first attempt failed its input proof and whose second died
    // of a dropped receipt, a bad nonce or a throw incremented neither `mismatched` nor
    // `retriedMismatch` — the ledger degraded the slice while attributing it to nothing.
    inputFault: second.inputFault ?? first.inputFault,
  };
}

// One executor dispatch for one slice. Never throws: a thrown agent() becomes an
// untrusted result carrying the message.
async function dispatchVerifySlice(c, inp, i, slice, { model, headShaShort, sliceNonce, label, ids, expectedInputChecksum, expectedInlineChecksum, inlinePayload }) {
  let env;
  try {
    // agent(promptString, opts); the pinned command is embedded in the prompt
    // (verifyPrompt), which is how the executor agent receives it.
    env = await c.agent(verifyPrompt(inp, i, sliceNonce, inlinePayload), {
      label,
      agentType: 'code-gauntlet:executor',
      model,
      schema: VERIFY_SCHEMA,
    });
  } catch (e) {
    return { ok: false, reason: `executor threw (${(e && e.message) || 'unknown'})` };
  }
  const trust = trustSlice(env, { nonce: sliceNonce, headShaShort, n: slice.length, ids, expectedInputChecksum, expectedInlineChecksum });
  if (!trust.ok) {
    return { ok: false, reason: trust.reason, inputFault: trust.inputFault };
  }
  return { ok: true, verified: joinVerifyDeltas(slice, env.result.deltas) };
}

// dispatchableIds(slice) -> { ok:true, ids:[...] } | { ok:false, reason }
// The delta echo is joined by id, so a slice is only dispatchable when every finding in
// it carries a usable string id and no two share one. Duplicates ACROSS slices are fine —
// each slice's join is independent — so this is deliberately a per-slice check.
//
// Ids are matched EXACTLY, everywhere: here, in trustSlice's coverage check, and in the
// join. Only the USABILITY test trims, mirroring gauntlet.verify.wire's `id.strip()` guard —
// an id that is nothing but whitespace is one the script would skip. Matching on the
// trimmed form would make two dispatched findings whose ids differ only by surrounding
// whitespace collide into a whole-slice degrade the script itself would never produce.
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

// A slice envelope is trusted only if it is the honest success shape AND its receipt
// echoes exactly what we dispatched: the nonce (this answer is for OUR call), the head
// sha (same tree the workflow resolved), and n_in (the executor loaded every finding we
// sent). Four guards beyond the receipt:
//
//   (1) DELTA-ID COVERAGE — the deltas name exactly the ids this slice dispatched: no
//       missing id, no duplicate, no stranger. A count cannot bind an answer to its
//       question, because sibling slices share a length.
//   (2) SHAPE AND STAMP — typed fields; a verified:false delta carries the script's
//       elimination_reason stamp and a verified:true delta does not. Redundant with (3),
//       kept for the precise reason it puts in the gap.
//   (3) CONTENT PROOF — fnv1a32 over the canonical rebuild equals the checksum the script
//       computed over its own deltas, which catches a coherent drift (1) and (2) pass.
//   (4) INPUT PROOF — the token proof, then the value proof: what the script decoded is
//       what this stage dispatched. (1)-(3) grade the answer; this grades the question.
//
// Every failure degrades the WHOLE slice to UNVERIFIED, which is conservative in the
// direction that matters: every dispatched finding is KEPT (origin=unknown, disclosed in a
// gap), so no guard here can be used to drop a real finding.
//
// Threat model: this defends against a STALE, DRIFTING or CONFUSED executor (an old/wrong
// result, a fabricated success, another slice's answer, a garbled transcription) — NOT a
// Byzantine one. The nonce is argv-visible and the checksum travels in the same envelope
// as the data it covers, so a malicious executor could recompute both; an LLM transcribing
// a document cannot, which is exactly the failure class this boundary keeps hitting.
function trustSlice(env, { nonce, headShaShort, n, ids, expectedInputChecksum, expectedInlineChecksum }) {
  if (!env || typeof env !== 'object') return { ok: false, reason: 'executor returned no envelope' };
  if (env.status !== 'ok') return { ok: false, reason: `status=${env.status == null ? 'missing' : env.status}${env.stderr ? ` (${env.stderr})` : ''}` };
  const r = env.receipt || {};
  if (r.nonce !== nonce) return { ok: false, reason: `receipt nonce mismatch (got ${r.nonce == null ? 'missing' : r.nonce}, expected ${nonce})` };
  if (r.sha !== headShaShort) return { ok: false, reason: `receipt sha mismatch (got ${r.sha == null ? 'missing' : r.sha})` };
  if (r.n_in !== n) return { ok: false, reason: `receipt n_in mismatch (got ${r.n_in == null ? 'missing' : r.n_in}, expected ${n})` };
  const result = env.result || {};
  if (!Array.isArray(result.deltas)) return { ok: false, reason: 'result missing deltas array' };

  const expected = new Set(ids || []);
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
    // The script canonicalises confidence to an integer precisely so this side never has
    // to agree with Python on how a float is spelled (_delta_confidence). A non-integer
    // here therefore did not come from the script.
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
  const missing = (ids || []).filter((id) => !byId.has(id));
  if (missing.length) {
    return { ok: false, reason: `delta does not cover ${missing.length} of ${(ids || []).length} dispatched finding(s) (first: ${missing[0]})` };
  }

  const proof = typeof r.deltas_checksum === 'string' ? r.deltas_checksum.trim() : '';
  if (!proof) return { ok: false, reason: 'receipt carries no deltas_checksum (content proof missing)' };
  const recomputed = deltaContentProof(ids || [], result.deltas);
  if (proof !== recomputed) {
    return { ok: false, reason: `delta content proof mismatch (receipt ${proof}, recomputed ${recomputed}) — the echoed values are not the ones the script wrote` };
  }

  // TOKEN PROOF — over the exact characters the executor was handed. Checked FIRST
  // because it is the strictly stronger half: every mutation the value proof catches
  // moves the token too, and the token also covers what the value proof cannot see
  // (a re-spelled number, or an escaped surrogate pair whose decoded JS value would be
  // unchanged). It is also always computable, where the value proof goes null on a
  // number the two runtimes spell differently.
  if (expectedInlineChecksum != null) {
    const tokenProof = typeof r.inline_checksum === 'string' ? r.inline_checksum.trim() : '';
    if (!tokenProof) {
      return { ok: false, reason: 'inline token proof missing from receipt', inputFault: 'missing' };
    }
    if (tokenProof !== expectedInlineChecksum) {
      return {
        ok: false,
        reason: `slice-input token proof mismatch (receipt ${tokenProof}, dispatched ${expectedInlineChecksum}) — the token the script decoded is not the token this stage dispatched`,
        inputFault: 'mismatch',
      };
    }
  }

  if (expectedInputChecksum != null) {
    const inputProof = typeof r.input_checksum === 'string' ? r.input_checksum.trim() : '';
    if (!inputProof) {
      // Retryable: a dropped field is a sampled executor's transcription failure, and a
      // fresh sample is a plausible fix.
      return { ok: false, reason: 'input content proof missing from receipt', inputFault: 'missing' };
    }
    if (inputProof !== expectedInputChecksum) {
      return {
        ok: false,
        reason: `slice-input content proof mismatch (receipt ${inputProof}, dispatched ${expectedInputChecksum}) — the document the script decoded is not the document this stage dispatched`,
        inputFault: 'mismatch',
      };
    }
  }

  return { ok: true };
}

// The pinned command: a single `python3 <script> --flags...` invocation whose tokens are
// AST-safe (no command substitution, heredocs, env prefix,
// or shell operators), each shellWord-quoted so a path bearing a space stays ONE argv word.
// Per-slice input/output paths are sha-scoped and index-suffixed; verifyStage
// supplies the slice document inline before dispatch, then the executor reads the slice
// output. The input path is the code-written destination used by verify_findings.py.
//
// `sliceNonce` is THREADED IN, never re-derived: it is the same value verifySliceWithRetry
// hands trustSlice as the expected receipt nonce, so argv and the trust check cannot
// disagree. It varies by ATTEMPT as well as by slice (the retry's `.r1` suffix), which is
// exactly what a second, independently-derived formula here would get wrong. The paths do
// NOT vary by attempt: a retry re-runs the same script over the same slice input and
// overwrites the same output.
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
    '--base-branch', v.baseBranch || 'main',
  ];
  if (v.diffPath) parts.push('--diff-file', v.diffPath);
  return parts.map(shellWord).join(' ');
}

// The executor is asked for a PREFIX of the output document, not the whole of
// it: the script writes `result.deltas` as the first key precisely so a length-capped Read
// (which returns no truncation notice) still contains everything this prompt
// names. The large verified/eliminated arrays that follow are for bench and v2 consumers;
// naming them here as explicitly-not-wanted is cheaper than letting the agent decide.
function verifyPrompt(inp, i, sliceNonce, inlinePayload) {
  return `Run exactly this command, then read the --output file and return, via the schema: its "status"; its "receipt" object with every field it contains (sha, n_in, nonce, deltas_checksum, inline_checksum, and input_checksum when present — never invent an absent one) copied exactly; and every entry of its "result.deltas" array, copied exactly. The quoted --input-inline token IS the slice document and must be reproduced character for character with no line breaks. The same file also holds large "verified" and "eliminated" arrays — do NOT return those and do not summarise them. Copy character for character: the deltas carry a checksum and a single altered value costs this slice its verification.\n${verifyCommand(inp, i, sliceNonce, inlinePayload)}`;
}
