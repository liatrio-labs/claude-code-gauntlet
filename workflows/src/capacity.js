// Verify capacity: how many findings fit one inline slice, and how many dispatches a slice
// can cost. The stage and the agent-count guard both read it, so the two cannot disagree.
import { VERIFY_INLINE_CHAR_BUDGET, encodeSliceInline, projectVerifySliceFinding } from './verifyWire.js';

// planVerifySlices(findings, sliceSize, budget, baseBranch) -> { slices, oversize, closeReasons }.
// A projected finding's cost is its encoded object alone. A slice's exact cost is the
// encoded empty envelope plus those object costs plus one comma for each additional
// finding. The greedy planner keeps both the finding-count and inline-character bounds
// in one place, and planner consumers and the dispatch assertion share this accounting.
// closeReasons is aligned with slices: a terminal slice has no following boundary and is
// tagged null; every other closed slice names the bound that closed it.
export const effectiveVerifyBaseBranch = (baseBranch) => baseBranch || 'main';
const verifySliceLengthFromCosts = (envelopeLength, findingCost, findingCount) =>
  envelopeLength + findingCost + Math.max(0, findingCount - 1);
const projectedVerifyFindingInlineLength = (finding) =>
  encodeSliceInline(projectVerifySliceFinding(finding)).length;

// This is the planner's predicted length, exposed for the exact-accounting test and
// kept on the same cost primitive as the planner's greedy admission check.
export function predictVerifySliceInlineLength(slice, baseBranch = 'main') {
  const branch = effectiveVerifyBaseBranch(baseBranch);
  const envelopeLength = encodeSliceInline({ findings: [], base_branch: branch }).length;
  let findingCost = 0;
  for (const finding of slice) findingCost += projectedVerifyFindingInlineLength(finding);
  return verifySliceLengthFromCosts(envelopeLength, findingCost, slice.length);
}

export function planVerifySlices(findings, sliceSize, budget, baseBranch = 'main') {
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

// Dispatches per verify slice: the first executor call plus exactly one fresh re-dispatch
// when that call comes back untrusted. Named because the retry, the agent-count guard's
// verify term and the dispatch-count tests must agree on it; the tests assert against this
// value, so raising it without changing the retry fails a test.
export const VERIFY_ATTEMPTS_PER_SLICE = 2;
