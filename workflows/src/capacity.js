import { AGENTS, TRUSTED_SCRIPT_BASENAMES, STAGE_COSTS, LIMIT_DEFAULTS } from './registry.js';
import { VERIFY_INLINE_CHAR_BUDGET, encodeSliceInline, projectVerifySliceFinding } from './verifyWire.js';

import { pythonArgv, shellWord } from './wire.js';
import { hostPlatformForRoots, trustedScriptPath } from './paths.js';
import { collectCitations, planCiteBatches } from './citeWire.js';

// Shell quoting preserves each argv word; retry paths stay fixed so outputs
// overwrite the first attempt.
export function verifyCommand(inp, i, sliceNonce, inlinePayload) {
  const v = inp.verify || {};
  const stem = inp.citeMode ? 'cite' : 'slice';
  const inPath = `${v.inputPathBase || 'phase4-input'}.${stem}${i}.json`;
  const outPath = `${v.outputPathBase || 'phase4-output'}.${stem}${i}.json`;
  const parts = [
    ...pythonArgv(inp.python), inp.pluginRoot ? trustedScriptPath(inp.pluginRoot, TRUSTED_SCRIPT_BASENAMES.verify) : 'scripts/verify_findings.py',
    '--input', inPath,
    '--input-inline', inlinePayload,
    '--output', outPath,
    '--nonce', sliceNonce,
    '--head-sha', inp.headShaShort,
    '--base-branch', effectiveVerifyBaseBranch(v.baseBranch),
  ];
  if (inp.citeMode) parts.push('--mode', 'cite', '--cite-batch', i, '--cite-attempt', inp.citeAttempt ?? STAGE_COSTS.cite.attempts);
  if (v.diffPath) parts.push('--diff-file', v.diffPath);
  if (v.sourceCapture) parts.push('--source-capture', JSON.stringify(v.sourceCapture), '--capture-output-dir', inp.outputDir);
  for (const [key, flag] of [['contextMap', '--context-map'], ['contextFile', '--context-file'], ['targetKind', '--target-kind']]) {
    if (v[key]) parts.push(flag, v[key]);
  }
  return parts.map(shellWord).join(' ');
}

// CreateProcess counts the entire UTF-16 command, including executable and quoting.
export function verifyInlineBudget(input = {}) {
  if (hostPlatformForRoots(input) !== 'windows') return VERIFY_INLINE_CHAR_BUDGET;
  const prepared = { ...input, verify: { ...(input.verify || {}),
    baseBranch: input.baseBranch ?? input.verify?.baseBranch, diffPath: input.diffPath ?? input.verify?.diffPath } };
  const index = input.citeMode ? 'ffffffff' : 999999999;
  const nonce = input.citeMode ? `${input.nonce || ''}.cite.${index}.${STAGE_COSTS.cite.attempts}` : `${input.nonce || ''}.${index}.r1`;
  const overhead = verifyCommand(prepared, index, nonce, '').length;
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
export const VERIFY_ATTEMPTS_PER_SLICE = STAGE_COSTS.verify.attempts;

// Stay strictly under the platform's per-run agent fan-out ceiling; the gap is headroom.
const AGENT_COUNT_GUARD = 900;
// Widen the summarize bucket once its term alone exceeds this.
const SUMMARIZE_TERM_BOUND = 300;
const CHALLENGE_CAP_FLOOR = 5;

const ceilDiv = (n, d) => Math.ceil(Math.max(0, n) / Math.max(1, d));

// Zero caps must remain distinct from absent caps.
export const effectiveChallengeCap = (L, findings) =>
  Math.max(0, L.challengeCap != null ? L.challengeCap : findings);

// Stage defaults must also govern guard arithmetic; undefined sizes can produce NaN and silently disable coarsening.
export const effectiveBucketSize = (L) => Math.max(1, L.summarizeBucketSize || LIMIT_DEFAULTS.summarizeBucketSize);
export const effectiveBatchSize = (L, findings) => Math.max(1, L.validateBatch || findings || 1);
export const effectiveValidationRetrySize = (L, findings) => Math.max(1, Math.ceil(effectiveBatchSize(L, findings) / STAGE_COSTS.validate.retryBatchDivisor));
const validationCost = (L, findings) => ceilDiv(findings, effectiveBatchSize(L, findings)) + ceilDiv(findings, effectiveValidationRetrySize(L, findings));

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

function citeCost(findings, input, baseBranch) {
  const records = Array.isArray(findings) ? collectCitations(findings) : [];
  return planCiteBatches(records, verifyInlineBudget({ ...input, citeMode: true }),
    baseBranch, hostPlatformForRoots(input)).batches.length * STAGE_COSTS.cite.attempts;
}

export function worstCaseAgentCount(limits, nFiles, nFindings, baseBranch = 'main', input = {}, includeCite = true) {
  const L = limits || {};
  const files = Math.max(0, nFiles || 0);
  const findings = findingCount(nFindings);
  const summarizeCalls = ceilDiv(files, effectiveBucketSize(L)) + 1;
  const verifyCalls = verifyTerm(L, nFindings, baseBranch, input);
  const validateCalls = validationCost(L, findings);
  const challengeCalls = Math.min(findings, effectiveChallengeCap(L, findings));
  const citeCalls = includeCite ? citeCost(nFindings, input, baseBranch) : 0;
  return summarizeCalls + AGENTS.length + verifyCalls + validateCalls + challengeCalls + citeCalls + STAGE_COSTS.writer.attempts;
}

// Removing terms that cannot shrink guarantees termination when the inline budget binds.
// Summarize must also shrink because its term alone can exceed the guard.
export function coarsenLimits(limits, nFiles, nFindings, baseBranch = 'main', input = {}) {
  const L = { ...(limits || {}) };
  const files = Math.max(0, nFiles || 0);
  const findings = findingCount(nFindings);
  const candidates = new Set(['summarize', 'verify', 'validate', 'challenge']);

  while (worstCaseAgentCount(L, files, nFindings, baseBranch, input, false) >= AGENT_COUNT_GUARD && candidates.size > 0) {
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
    const challengeCount = Math.min(findings, effectiveChallengeCap(L, findings));
    const validateTerm = validationCost(L, findings);
    const challengeTerm = challengeCount;
    const choices = [
      ['validate', validateTerm],
      ['verify', currentVerifyTerm],
      ['challenge', challengeTerm],
    ].filter(([name, cost]) => candidates.has(name) && cost > 0);
    if (choices.length === 0) break;
    choices.sort((a, b) => b[1] - a[1]);
    const [largest] = choices;
    if (largest[0] === 'validate') {
      L.validateBatch = effectiveBatchSize(L, findings) * 2;
      if (validationCost(L, findings) >= validateTerm) candidates.delete('validate');
    } else if (largest[0] === 'verify') {
      L.verifySliceSize = effectiveSliceSize(L, findings) * 2;
      if (verifyTerm(L, nFindings, baseBranch, input) >= currentVerifyTerm) candidates.delete('verify');
    } else {
      // Halving the effective cap ensures progress even when the nominal cap exceeds the finding count.
      L.challengeCap = Math.min(challengeCount, Math.max(CHALLENGE_CAP_FLOOR, Math.floor(challengeCount / 2)));
      const next = Math.min(findings, effectiveChallengeCap(L, findings));
      if (next >= challengeTerm) candidates.delete('challenge');
    }
  }
  return L;
}
