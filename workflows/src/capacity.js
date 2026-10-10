// Verify capacity: how many findings fit one inline slice, and how many dispatches a slice
// can cost. The stage and the agent-count guard both read it, so the two cannot disagree.
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
