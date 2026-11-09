import { AGENTS, TRUSTED_SCRIPT_BASENAMES, STAGE_COSTS, LIMIT_DEFAULTS } from './registry.js';
import { VERIFY_INLINE_CHAR_BUDGET, encodeSliceInline, projectVerifySliceFinding } from './verifyWire.js';

import { pythonArgv, shellWord } from './wire.js';
import { hostPlatformForRoots, trustedScriptPath } from './paths.js';

// Attempt suffixes keep parallel retries from overwriting evidence or each other.
export function verifyCommand(inp, i, sliceNonce, inlinePayload, attempt = '') {
  const v = inp.verify || {};
  const suffix = attempt ? `.${attempt}` : '';
  const inPath = `${v.inputPathBase || 'phase4-input'}.slice${i}${suffix}.json`;
  const outPath = `${v.outputPathBase || 'phase4-output'}.slice${i}${suffix}.json`;
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
  for (const [key, flag] of [['contextMap', '--context-map'], ['contextFile', '--context-file'], ['targetKind', '--target-kind']]) {
    if (v[key]) parts.push(flag, v[key]);
  }
  return parts.map(shellWord).join(' ');
}

// Keep a finite ceiling on every host; CreateProcess also needs room for executable and quoting.
export function verifyInlineBudgets(input = {}) {
  let hard = 50000;
  if (hostPlatformForRoots(input) === 'windows') {
    const prepared = { ...input, verify: { ...(input.verify || {}),
      baseBranch: input.baseBranch ?? input.verify?.baseBranch, diffPath: input.diffPath ?? input.verify?.diffPath } };
    const overhead = verifyCommand(prepared, 999999999, `${input.nonce || ''}.999999999.r2.999999999`, '', 'r2.999999999').length;
    hard = Math.min(hard, Math.max(0, 32767 - overhead - 2048));
  }
  return Object.freeze({ copy: Math.min(VERIFY_INLINE_CHAR_BUDGET, hard), hard });
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

// Isolate long copies so neighbouring findings can still use short tokens.
export function planVerifySlices(findings, sliceSize, { copy, hard }, baseBranch, hostPlatform) {
  if (!Number.isFinite(copy) || !Number.isFinite(hard) || copy < 0 || copy > hard) {
    throw new Error('verify inline budgets must satisfy 0 <= copy <= hard with finite limits');
  }
  const source = Array.isArray(findings) ? findings : [];
  const branch = effectiveVerifyBaseBranch(baseBranch);
  const maxFindings = Math.max(1, sliceSize || source.length || 1);
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
    if (singleCost > hard) {
      flush('oversize');
      oversize.push(finding);
      continue;
    }
    if (singleCost > copy) {
      flush('budget');
      current.push(finding);
      flush('budget');
      continue;
    }
    const candidateCost = verifySliceLengthFromCosts(
      envelopeAllowance,
      currentFindingCost + findingCost,
      current.length + 1,
    );
    if (current.length > 0 && (current.length >= maxFindings || candidateCost > copy)) {
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

// The stage and guard share the same finite recovery budget.
export const VERIFY_ATTEMPTS_PER_SLICE = STAGE_COSTS.verify.attempts;
export const VERIFY_SINGLETON_RETRY_CAP = STAGE_COSTS.verify.singletonRetries;
export const verifyDispatchCount = (slices) => slices * (VERIFY_ATTEMPTS_PER_SLICE + VERIFY_SINGLETON_RETRY_CAP);

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
    const { slices } = planVerifySlices(findings, effectiveSliceSize(L, count), verifyInlineBudgets(input), baseBranch, hostPlatformForRoots(input));
    return verifyDispatchCount(slices.length);
  }
  return verifyDispatchCount(ceilDiv(count, effectiveSliceSize(L, count)));
}

export function worstCaseAgentCount(limits, nFiles, nFindings, baseBranch = 'main', input = {}) {
  const L = limits || {};
  const files = Math.max(0, nFiles || 0);
  const findings = findingCount(nFindings);
  const summarizeCalls = ceilDiv(files, effectiveBucketSize(L)) + 1;
  const verifyCalls = verifyTerm(L, nFindings, baseBranch, input);
  const validateCalls = validationCost(L, findings);
  const challengeCalls = Math.min(findings, effectiveChallengeCap(L, findings));
  return summarizeCalls + AGENTS.length + verifyCalls + validateCalls + challengeCalls + STAGE_COSTS.writer.attempts;
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
