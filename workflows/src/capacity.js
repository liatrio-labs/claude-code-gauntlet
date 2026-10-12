// Verify capacity: how many findings fit one inline slice, and how many dispatches a slice
// can cost. The stage and the agent-count guard both read it, so the two cannot disagree.
import { AGENTS } from './registry.js';
import { LIMIT_DEFAULTS } from './args.js';
import { VERIFY_INLINE_CHAR_BUDGET, encodeSliceInline, projectVerifySliceFinding } from './verifyWire.js';

export const effectiveVerifyBaseBranch = (baseBranch) => baseBranch || 'main';

// A slice's exact cost is the encoded empty envelope, plus each projected finding's
// encoded object, plus one comma per additional finding.
const verifySliceLengthFromCosts = (envelopeLength, findingCost, findingCount) =>
  envelopeLength + findingCost + Math.max(0, findingCount - 1);
const projectedVerifyFindingInlineLength = (finding) =>
  encodeSliceInline(projectVerifySliceFinding(finding)).length;

// The planner's predicted length, on the same cost primitive as its admission check. The
// stage asserts each dispatched payload against it.
export function predictVerifySliceInlineLength(slice, baseBranch) {
  const branch = effectiveVerifyBaseBranch(baseBranch);
  const envelopeLength = encodeSliceInline({ findings: [], base_branch: branch }).length;
  let findingCost = 0;
  for (const finding of slice) findingCost += projectedVerifyFindingInlineLength(finding);
  return verifySliceLengthFromCosts(envelopeLength, findingCost, slice.length);
}

// Greedy, under both the finding-count and the inline-character bound. closeReasons names
// what closed each slice (null for the final flush), because a skipped oversize finding
// hides a boundary that neighbouring slices cannot reconstruct.
export function planVerifySlices(findings, sliceSize, budget, baseBranch) {
  const source = Array.isArray(findings) ? findings : [];
  const branch = effectiveVerifyBaseBranch(baseBranch);
  const maxFindings = Math.max(1, sliceSize || source.length || 1);
  const maxChars = Math.max(1, budget || VERIFY_INLINE_CHAR_BUDGET);
  const envelopeAllowance = encodeSliceInline({ findings: [], base_branch: branch }).length;
  const slices = [];
  const oversize = [];
  const closeReasons = [];
  let current = [];
  let currentFindingCost = 0;
  const flush = (reason = null) => {
    if (current.length > 0) {
      slices.push(current);
      closeReasons.push(reason);
    }
    current = [];
    currentFindingCost = 0;
  };

  for (const finding of source) {
    const findingCost = projectedVerifyFindingInlineLength(finding);
    const singleCost = verifySliceLengthFromCosts(envelopeAllowance, findingCost, 1);
    if (singleCost > maxChars) {
      flush('oversize');
      oversize.push(finding);
      continue;
    }
    const candidateCost = verifySliceLengthFromCosts(
      envelopeAllowance,
      currentFindingCost + findingCost,
      current.length + 1,
    );
    if (current.length > 0 && (current.length >= maxFindings || candidateCost > maxChars)) {
      flush(current.length >= maxFindings ? 'count' : 'budget');
    }
    current.push(finding);
    currentFindingCost += findingCost;
  }
  flush();
  return { slices, oversize, closeReasons };
}

// An absent or zero verifySliceSize means one slice over all findings, the stage's own
// default. Never NaN: a NaN worst case would silently disable the coarsening loop.
export const effectiveSliceSize = (L, findings) => Math.max(1, L.verifySliceSize || findings || 1);

// Dispatches per verify slice: the first executor call plus one re-dispatch when it comes
// back untrusted. Named because the retry and the agent-count guard's verify term must
// agree on it.
export const VERIFY_ATTEMPTS_PER_SLICE = 2;

// --- Agent-count coarsening -------------------------------------------------

const AGENT_COUNT_GUARD = 900;   // stay strictly under the platform fan-out ceiling
const SUMMARIZE_TERM_BOUND = 300; // widen the summarize bucket once its term alone exceeds this
const CHALLENGE_CAP_FLOOR = 5;    // never challenge fewer than this many findings

const ceilDiv = (n, d) => Math.ceil(Math.max(0, n) / Math.max(1, d));

// The SINGLE accessor path for a stage's effective limit — summarize,
// verifyStage, validateStage, and challengeStage all call these directly rather than
// re-deriving their own fallback, so worstCaseAgentCount/coarsenLimits can never count a
// different fan-out than the stage actually dispatches. normalizeArgs (args.js
// LIMIT_DEFAULTS) fills summarizeBucketSize/validateBatch/verifySliceSize/challengeCap on
// every real waist before a stage ever sees `limits`, so the `|| <literal>` fallbacks below
// are now the DEFENSIVE case (a caller that built `limits` by hand, a test, or a bare
// `coarsenLimits({}, …)` call) rather than the common one. Keep them anyway: a literal
// default here still has to agree with LIMIT_DEFAULTS, but that agreement is not load-
// bearing for a normalized waist.
//
// Mirror challengeStage's cap semantics EXACTLY: an absent/null challengeCap means
// "challenge every finding" (the stage defaults to findings.length), while 0 is a real
// cap of zero. The guard math must never undercount the stage's actual fan-out.
export const effectiveChallengeCap = (L, findings) =>
  Math.max(0, L.challengeCap != null ? L.challengeCap : findings);

// Mirror each stage's own absent/zero-size default EXACTLY (summarize: bucket
// LIMIT_DEFAULTS.summarizeBucketSize; verify/validate: ONE slice/batch over all findings) so the
// guard arithmetic can never go NaN (Math.max(1, undefined) is NaN — a NaN worst case silently
// disables the coarsening loop) and never counts a different fan-out than the stage dispatches.
// The four LIMIT_DEFAULTS numbers live in exactly one place (workflows/src/args.js); these
// helpers read it rather than restating a literal.
export const effectiveBucketSize = (L) => Math.max(1, L.summarizeBucketSize || LIMIT_DEFAULTS.summarizeBucketSize);
export const effectiveBatchSize = (L, findings) => Math.max(1, L.validateBatch || findings || 1);

function findingCount(findings) {
  return Array.isArray(findings) ? findings.length : Math.max(0, findings || 0);
}

function verifyTerm(L, findings, baseBranch = 'main') {
  const count = findingCount(findings);
  if (Array.isArray(findings)) {
    return planVerifySlices(findings, effectiveSliceSize(L, count), VERIFY_INLINE_CHAR_BUDGET, baseBranch).slices.length
      * VERIFY_ATTEMPTS_PER_SLICE;
  }
  return ceilDiv(count, effectiveSliceSize(L, count)) * VERIFY_ATTEMPTS_PER_SLICE;
}

// worstCaseAgentCount(limits, nFiles, findings, baseBranch) -> number
// summarize buckets (+1 merge) + the 7 discovery agents + verify slices + validate
// batches + min(nFindings, challengeCap) challengers + 2 (report + writer).
//
// The verify term counts VERIFY_ATTEMPTS_PER_SLICE dispatches per slice, because every
// slice can independently take its one retry (verifySliceWithRetry) and a
// worst case that assumes the happy path is not a bound. The remaining terms are still
// NOMINAL: the report/writer pair can segment, and writeArtifactsDerived has its own
// single retry, neither of which is counted here. That looseness is covered by the
// headroom between AGENT_COUNT_GUARD and the platform ceiling; the verify term is the one
// that scales with finding count, which is why it is the one made exact.
export function worstCaseAgentCount(limits, nFiles, nFindings, baseBranch = 'main') {
  const L = limits || {};
  const files = Math.max(0, nFiles || 0);
  const findings = findingCount(nFindings);
  const summarizeCalls = ceilDiv(files, effectiveBucketSize(L)) + 1;
  const verifyCalls = verifyTerm(L, nFindings, baseBranch);
  const validateCalls = ceilDiv(findings, effectiveBatchSize(L, findings));
  const challengeCalls = Math.min(findings, effectiveChallengeCap(L, findings));
  return summarizeCalls + AGENTS.length + verifyCalls + validateCalls + challengeCalls + 2;
}

// coarsenLimits(limits, nFiles, nFindings, baseBranch) -> limits
// Iteratively pulls the worst-case count below the guard. A candidate that no longer
// reduces its term is removed, which is required once the inline budget makes the
// verify fan-out size-bound.
//   - When the summarize term alone exceeds SUMMARIZE_TERM_BOUND, widen the bucket
//     (doubling). Without this, a pathological changed-file count (~>17k) keeps the
//     summarize term above the guard and no validate/challenge coarsening can converge.
//   - Otherwise reduce whichever of {verify, validate, challenge} is currently largest:
//     RAISE verifySliceSize / validateBatch (fewer batches, since ceil(n/x) shrinks) or
//     LOWER challengeCap (the challenge term is min(n, cap), so a SMALLER cap lowers the
//     count — raising it is the inversion trap).
// summarizeBucketSize / validateBatch / verifySliceSize rise monotonically while
// challengeCap falls to CHALLENGE_CAP_FLOOR, so the chosen term is always reducible
// whenever the count is still >= guard, guaranteeing termination.
export function coarsenLimits(limits, nFiles, nFindings, baseBranch = 'main') {
  const L = { ...(limits || {}) };
  const files = Math.max(0, nFiles || 0);
  const findings = findingCount(nFindings);
  const candidates = new Set(['summarize', 'verify', 'validate', 'challenge']);

  while (worstCaseAgentCount(L, files, nFindings, baseBranch) >= AGENT_COUNT_GUARD && candidates.size > 0) {
    const summarizeTerm = ceilDiv(files, effectiveBucketSize(L)) + 1;
    if (summarizeTerm > SUMMARIZE_TERM_BOUND && candidates.has('summarize')) {
      // Double from the EFFECTIVE size (pinning a concrete value): doubling from a raw
      // absent field (|| 1 -> 2) would LOWER the effective bucket below the stage's
      // default of 20 and move the term the wrong way.
      const before = summarizeTerm;
      L.summarizeBucketSize = effectiveBucketSize(L) * 2;
      if (ceilDiv(files, effectiveBucketSize(L)) + 1 >= before) candidates.delete('summarize');
      continue;
    }
    // Same VERIFY_ATTEMPTS_PER_SLICE scaling worstCaseAgentCount applies, so the "reduce
    // whichever term is largest" choice is made against each term's real contribution to
    // the count it is trying to pull down. With an array of actual findings, doubling
    // verifySliceSize can stop reducing the term once the inline budget binds; the update
    // below removes that candidate when its measured term does not change.
    const currentVerifyTerm = verifyTerm(L, nFindings, baseBranch);
    const validateTerm = ceilDiv(findings, effectiveBatchSize(L, findings));
    const challengeTerm = Math.min(findings, effectiveChallengeCap(L, findings));
    const choices = [
      ['validate', validateTerm],
      ['verify', currentVerifyTerm],
      ['challenge', challengeTerm],
    ].filter(([name]) => candidates.has(name));
    if (choices.length === 0) break;
    choices.sort((a, b) => b[1] - a[1]);
    const [largest] = choices;
    if (largest[0] === 'validate') {
      L.validateBatch = effectiveBatchSize(L, findings) * 2;
      if (ceilDiv(findings, effectiveBatchSize(L, findings)) >= validateTerm) candidates.delete('validate');
    } else if (largest[0] === 'verify') {
      L.verifySliceSize = effectiveSliceSize(L, findings) * 2;
      if (verifyTerm(L, nFindings, baseBranch) >= currentVerifyTerm) candidates.delete('verify');
    } else {
      // Halve the EFFECTIVE cap (min(cap, findings)) so C strictly decreases even when
      // the nominal cap already exceeds nFindings — or is absent (= findings).
      L.challengeCap = Math.max(CHALLENGE_CAP_FLOOR, Math.floor(Math.min(effectiveChallengeCap(L, findings), findings) / 2));
      if (Math.min(findings, effectiveChallengeCap(L, findings)) >= challengeTerm) candidates.delete('challenge');
    }
  }
  return L;
}
