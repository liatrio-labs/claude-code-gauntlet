// Verify slice capacity and dispatch limits shared by stages and the agent-count guard.
import { AGENTS } from './registry.js';
import { LIMIT_DEFAULTS } from './args.js';
import { VERIFY_INLINE_CHAR_BUDGET, encodeSliceInline, projectVerifySliceFinding } from './verifyWire.js';

export const effectiveVerifyBaseBranch = (baseBranch) => baseBranch || 'main';

// Exact wire cost includes the empty envelope and commas between findings.
const verifySliceLengthFromCosts = (envelopeLength, findingCost, findingCount) =>
  envelopeLength + findingCost + Math.max(0, findingCount - 1);
const projectedVerifyFindingInlineLength = (finding) =>
  encodeSliceInline(projectVerifySliceFinding(finding)).length;

// Prediction and admission share a cost primitive so dispatch assertions cannot disagree with planning.
export function predictVerifySliceInlineLength(slice, baseBranch) {
  const branch = effectiveVerifyBaseBranch(baseBranch);
  const envelopeLength = encodeSliceInline({ findings: [], base_branch: branch }).length;
  let findingCost = 0;
  for (const finding of slice) findingCost += projectedVerifyFindingInlineLength(finding);
  return verifySliceLengthFromCosts(envelopeLength, findingCost, slice.length);
}

// A skipped oversize finding hides a boundary that neighboring slices cannot reconstruct, so the planner records closing reasons.
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

// Matching the stage default avoids undercounting fan-out; NaN would silently disable coarsening.
export const effectiveSliceSize = (L, findings) => Math.max(1, L.verifySliceSize || findings || 1);

// The retry and the agent-count guard must agree on dispatches per slice.
export const VERIFY_ATTEMPTS_PER_SLICE = 2;

// Stay strictly under the platform's per-run agent fan-out ceiling; the gap is headroom.
const AGENT_COUNT_GUARD = 900;
// Widen the summarize bucket once its term alone exceeds this.
const SUMMARIZE_TERM_BOUND = 300;
// Never challenge fewer than this many findings.
const CHALLENGE_CAP_FLOOR = 5;

const ceilDiv = (n, d) => Math.ceil(Math.max(0, n) / Math.max(1, d));

// Shared accessors prevent the guard from undercounting stage fan-out, including callers that bypass argument normalization.
// An absent challenge cap permits all findings, while zero must remain a real cap.
export const effectiveChallengeCap = (L, findings) =>
  Math.max(0, L.challengeCap != null ? L.challengeCap : findings);

// Stage defaults must also govern guard arithmetic; undefined sizes can produce NaN and silently disable coarsening.
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

// Every verify slice can retry independently, so its term must include retries.
// The +2 covers up to two artifact-writer attempts; renderReport is code-only.
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

// Removing terms that cannot shrink guarantees termination when the inline budget binds.
// Summarize must also shrink because its term alone can exceed the guard.
export function coarsenLimits(limits, nFiles, nFindings, baseBranch = 'main') {
  const L = { ...(limits || {}) };
  const files = Math.max(0, nFiles || 0);
  const findings = findingCount(nFindings);
  const candidates = new Set(['summarize', 'verify', 'validate', 'challenge']);

  while (worstCaseAgentCount(L, files, nFindings, baseBranch) >= AGENT_COUNT_GUARD && candidates.size > 0) {
    const summarizeTerm = ceilDiv(files, effectiveBucketSize(L)) + 1;
    if (summarizeTerm > SUMMARIZE_TERM_BOUND && candidates.has('summarize')) {
      // Doubling a raw absent size could lower the effective bucket size and increase fan-out.
      const before = summarizeTerm;
      L.summarizeBucketSize = effectiveBucketSize(L) * 2;
      if (ceilDiv(files, effectiveBucketSize(L)) + 1 >= before) candidates.delete('summarize');
      continue;
    }
    // Compare each term using its full dispatch cost so retrying verify slices receive the same weight as in the guard.
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
      // Halving the effective cap ensures progress even when the nominal cap exceeds the finding count.
      L.challengeCap = Math.max(CHALLENGE_CAP_FLOOR, Math.floor(Math.min(effectiveChallengeCap(L, findings), findings) / 2));
      if (Math.min(findings, effectiveChallengeCap(L, findings)) >= challengeTerm) candidates.delete('challenge');
    }
  }
  return L;
}
