import { AGENTS, TRUSTED_SCRIPT_BASENAMES } from './registry.js';
import { LIMIT_DEFAULTS } from './registry.js';
import { VERIFY_INLINE_CHAR_BUDGET, encodeSliceInline, projectVerifySliceFinding } from './verifyWire.js';

import { pythonArgv, shellWord } from './wire.js';
import { hostPlatformForRoots, trustedScriptPath } from './paths.js';

// Shell quoting preserves each argv word; retry paths stay fixed so outputs
// overwrite the first attempt.
export function verifyCommand(inp, i, sliceNonce, inlinePayload) {
  const v = inp.verify || {};
  const inPath = `${v.inputPathBase || 'phase4-input'}.slice${i}.json`;
  const outPath = `${v.outputPathBase || 'phase4-output'}.slice${i}.json`;
  const parts = [
    ...pythonArgv(inp.python), inp.pluginRoot ? trustedScriptPath(inp.pluginRoot, TRUSTED_SCRIPT_BASENAMES.verify) : 'scripts/verify_findings.py',
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

// CreateProcess counts the entire UTF-16 command, including executable and quoting.
export function verifyInlineBudget(input = {}) {
  if (hostPlatformForRoots(input) !== 'windows') return VERIFY_INLINE_CHAR_BUDGET;
  const prepared = { ...input, verify: { ...(input.verify || {}),
    baseBranch: input.baseBranch ?? input.verify?.baseBranch, diffPath: input.diffPath ?? input.verify?.diffPath } };
  const overhead = verifyCommand(prepared, 999999999, `${input.nonce || ''}.999999999.r1`, '').length;
  return Math.max(0, 32767 - overhead - 2048);
}

export const effectiveVerifyBaseBranch = (baseBranch) => baseBranch || 'main';

// Native Windows argv quotes JSON quotes again, including the Bash -c launch envelope.
export function verifyInlineCost(token, hostPlatform) {
  return token.length + (hostPlatform === 'windows' ? (token.match(/["\\]/g) || []).length : 0);
}

const verifySliceLengthFromCosts = (envelopeLength, findingCost, findingCount) =>
  envelopeLength + findingCost + Math.max(0, findingCount - 1);
const projectedVerifyFindingInlineLength = (finding, hostPlatform) =>
  verifyInlineCost(encodeSliceInline(projectVerifySliceFinding(finding)), hostPlatform);

// Planning and dispatch must agree on wire cost.
export function predictVerifySliceInlineLength(slice, baseBranch, hostPlatform) {
  const branch = effectiveVerifyBaseBranch(baseBranch);
  const envelopeLength = verifyInlineCost(encodeSliceInline({ findings: [], base_branch: branch }), hostPlatform);
  let findingCost = 0;
  for (const finding of slice) findingCost += projectedVerifyFindingInlineLength(finding, hostPlatform);
  return verifySliceLengthFromCosts(envelopeLength, findingCost, slice.length);
}

// A skipped oversize finding hides a boundary that neighboring slices cannot reconstruct, so the planner records closing reasons.
export function planVerifySlices(findings, sliceSize, budget, baseBranch, hostPlatform) {
  const source = Array.isArray(findings) ? findings : [];
  const branch = effectiveVerifyBaseBranch(baseBranch);
  const maxFindings = Math.max(1, sliceSize || source.length || 1);
  const maxChars = Math.max(0, budget ?? VERIFY_INLINE_CHAR_BUDGET);
  const envelopeAllowance = verifyInlineCost(encodeSliceInline({ findings: [], base_branch: branch }), hostPlatform);
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
    const findingCost = projectedVerifyFindingInlineLength(finding, hostPlatform);
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

// Zero caps must remain distinct from absent caps.
export const effectiveChallengeCap = (L, findings) =>
  Math.max(0, L.challengeCap != null ? L.challengeCap : findings);

// Stage defaults must also govern guard arithmetic; undefined sizes can produce NaN and silently disable coarsening.
export const effectiveBucketSize = (L) => Math.max(1, L.summarizeBucketSize || LIMIT_DEFAULTS.summarizeBucketSize);
export const effectiveBatchSize = (L, findings) => Math.max(1, L.validateBatch || findings || 1);

function findingCount(findings) {
  return Array.isArray(findings) ? findings.length : Math.max(0, findings || 0);
}

function verifyTerm(L, findings, baseBranch = 'main', input = {}) {
  const count = findingCount(findings);
  if (Array.isArray(findings)) {
    return planVerifySlices(findings, effectiveSliceSize(L, count), verifyInlineBudget(input), baseBranch, hostPlatformForRoots(input)).slices.length
      * VERIFY_ATTEMPTS_PER_SLICE;
  }
  return ceilDiv(count, effectiveSliceSize(L, count)) * VERIFY_ATTEMPTS_PER_SLICE;
}

// Every verify slice can retry independently, so its term must include retries.
// The +2 covers up to two artifact-writer attempts; renderReport is code-only.
export function worstCaseAgentCount(limits, nFiles, nFindings, baseBranch = 'main', input = {}) {
  const L = limits || {};
  const files = Math.max(0, nFiles || 0);
  const findings = findingCount(nFindings);
  const summarizeCalls = ceilDiv(files, effectiveBucketSize(L)) + 1;
  const verifyCalls = verifyTerm(L, nFindings, baseBranch, input);
  const validateCalls = ceilDiv(findings, effectiveBatchSize(L, findings));
  const challengeCalls = Math.min(findings, effectiveChallengeCap(L, findings));
  return summarizeCalls + AGENTS.length + verifyCalls + validateCalls + challengeCalls + 2;
}

// Removing terms that cannot shrink guarantees termination when the inline budget binds.
// Summarize must also shrink because its term alone can exceed the guard.
export function coarsenLimits(limits, nFiles, nFindings, baseBranch = 'main', input = {}) {
  const L = { ...(limits || {}) };
  const files = Math.max(0, nFiles || 0);
  const findings = findingCount(nFindings);
  const candidates = new Set(['summarize', 'verify', 'validate', 'challenge']);

  while (worstCaseAgentCount(L, files, nFindings, baseBranch, input) >= AGENT_COUNT_GUARD && candidates.size > 0) {
    const summarizeTerm = ceilDiv(files, effectiveBucketSize(L)) + 1;
    if (summarizeTerm > SUMMARIZE_TERM_BOUND && candidates.has('summarize')) {
      // Doubling a raw absent size could lower the effective bucket size and increase fan-out.
      const before = summarizeTerm;
      L.summarizeBucketSize = effectiveBucketSize(L) * 2;
      if (ceilDiv(files, effectiveBucketSize(L)) + 1 >= before) candidates.delete('summarize');
      continue;
    }
    // Compare each term using its full dispatch cost so retrying verify slices receive the same weight as in the guard.
    const currentVerifyTerm = verifyTerm(L, nFindings, baseBranch, input);
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
      if (verifyTerm(L, nFindings, baseBranch, input) >= currentVerifyTerm) candidates.delete('verify');
    } else {
      // Halving the effective cap ensures progress even when the nominal cap exceeds the finding count.
      L.challengeCap = Math.max(CHALLENGE_CAP_FLOOR, Math.floor(Math.min(effectiveChallengeCap(L, findings), findings) / 2));
      if (Math.min(findings, effectiveChallengeCap(L, findings)) >= challengeTerm) candidates.delete('challenge');
    }
  }
  return L;
}
