// Pipeline sequencing, replay, diagnostics, and return envelopes.
import { setupContext } from './contextPlan.js';
import { coarsenLimits } from './capacity.js';
import { summarize, deriveAgentFlags, discover, allActiveDimensionsDegraded } from './discover.js';
import { validateStage, filterStage, challengeStage, selectDelivery } from './reviewStages.js';
import { mergeStage } from './mergeFindings.js';
import { verifyStage } from './verifyStage.js';
import { FINDING_TEXT_FIELD_NAMES, conditionalSchemaActive } from './registry.js';
import { mentionsPreparedHostRoot, prepareHostRootPatterns, safeFindingLabel } from './paths.js';
import { normalizeArgsReport, nullToleranceGap, nullRespellGap, nullToleranceRejectedKeys, validateArgs, entryArgs, makeArgsRejectEnvelope, SKILL_RECOVERY_LINE, resolveReviewConfig, computeLightEligible, configEchoValue } from './args.js';
import { scopeMatchesFile } from './filterFindings.js';
import { renderReport, renderSummaryBody } from './renderReport.js';
import { writeArtifacts } from './persist.js';
import { applyChallengeReplayBelt, normalizePhaseFindings, buildResumeCheckpoints, checkpointDiscardGap, checkpointShapeErrors, makeCheckpointShapeRejectEnvelope, readCheckpoints, slimPersistedCheckpoints } from './checkpoints.js';

// The host injects runtime globals, while tests supply context explicitly.
function defaultCtx() {
  return {
    agent: typeof agent === 'function' ? agent : undefined,
    parallel: typeof parallel === 'function' ? parallel : undefined,
    pipeline: typeof pipeline === 'function' ? pipeline : undefined,
    pipelineVersion: null,
  };
}

function hostPathTextFields(value, patterns) {
  const fields = new Set();
  const visit = (value, field) => {
    if (typeof value === 'string') {
      if (mentionsPreparedHostRoot(value, patterns)) fields.add(field ?? 'other');
      return;
    }
    if (value === null || typeof value !== 'object') return;
    if (Array.isArray(value)) {
      for (const entry of value) visit(entry, field);
      return;
    }
    for (const [key, entry] of Object.entries(value)) {
      const name = field ?? (FINDING_TEXT_FIELD_NAMES.has(key) ? key : 'other');
      visit(key, name);
      visit(entry, name);
    }
  };
  visit(value, null);
  return [...fields].sort();
}

function hostPathTextGaps(challengeOut, summaryOut, roots) {
  const gaps = [];
  const patterns = prepareHostRootPatterns(roots);
  for (const bucket of ['findings', 'unverified', 'eliminated']) {
    const findings = Array.isArray(challengeOut?.[bucket]) ? challengeOut[bucket] : [];
    findings.forEach((finding, index) => {
      const fields = hostPathTextFields(finding, patterns);
      if (fields.length === 0) return;
      const fallback = `#${index + 1} of ${bucket}`;
      const label = safeFindingLabel(finding && finding.id, fallback);
      gaps.push(`host-path-text: finding ${label} mentions a host path in ${fields.join(', ')} - text left unchanged`);
    });
  }
  const summaryText = Object.fromEntries(Object.entries(summaryOut).filter(([key]) => key !== 'gaps'));
  if (hostPathTextFields(summaryText, patterns).length > 0) {
    gaps.push('host-path-text: change summary mentions a host path - text left unchanged');
  }
  return gaps;
}

// The envelope must report the same provider and schema resolution used at dispatch so failures are diagnosable without recomputing policy.
function resolvedPolicyEnvelope(policy) {
  return {
    subagentModel: policy.subagentModel || null,
    provider: policy.provider || 'firstParty',
    gateway: !!policy.gateway,
    conditionalSchema: conditionalSchemaActive(policy),
  };
}

// Explicit free-text fields keep the return compact without accidentally truncating future count arrays.
const METHODOLOGY_COUNT_ONLY_FIELDS = ['truncation_warnings', 'validation_warnings'];
export function compactMethodology(m) {
  if (!m || typeof m !== 'object' || Array.isArray(m)) return {};
  const out = {};
  for (const [k, v] of Object.entries(m)) {
    out[k] = METHODOLOGY_COUNT_ONLY_FIELDS.includes(k) && Array.isArray(v) ? v.length : v;
  }
  return out;
}

// This seam NEVER lets a throw escape so direct callers can recover completed work from its diagnostic envelope.
export async function runWith(ctx, rawArgs) {
  // Keep this seam throw-free for direct callers while returning the entry's refusal shape.
  const entry = entryArgs(rawArgs);
  if (!entry.ok) return entry.envelope;
  // Null substitutions and respellings can change configuration, so both exits disclose them.
  // Use the already-unwrapped arguments to avoid changing decode depth.
  const {
    args: A,
    dropped: droppedNulls,
    respelled: respelledNulls,
    derivedPaths,
  } = normalizeArgsReport(entry.waist);
  const nullArgGaps = [
    ...nullToleranceRejectedKeys(A, droppedNulls, derivedPaths).map(nullToleranceGap),
    ...respelledNulls.map(nullRespellGap),
  ];
  const check = validateArgs(A);
  if (!check.ok) {
    // Direct callers need both the invalid fields and the recovery location in this envelope.
    return makeArgsRejectEnvelope(
      `invalid args: ${check.errors.join('; ')}. ${SKILL_RECOVERY_LINE}`,
      [...nullArgGaps, ...check.errors],
    );
  }

  const c = { ...defaultCtx(), ...(ctx || {}) };
  // Before merge, only the changed-file count is known; later coarsening must also account for findings.
  const nChangedFiles = (A.changedFiles || []).length;
  let limits = coarsenLimits(A.limits || {}, nChangedFiles, 0, A.baseBranch);
  const policy = A.policy || {};
  const contextPath = `${A.outputDir}/code-gauntlet-context-${A.headShaShort}.md`;
  const { contextLine, contextSizeGap } = setupContext({ contextPath, contextLines: A.contextLines, contextChars: A.contextChars });
  // Inspect the stamped checkpoints before readCheckpoints can discard malformed input.
  const discardGap = checkpointDiscardGap(A.checkpoints);
  const checkpoints = readCheckpoints(c, A);
  // Reject malformed replay data before any phase reads it or dispatches work.
  const checkpointShapeViolations = checkpointShapeErrors(checkpoints);
  if (checkpointShapeViolations.length) {
    return makeCheckpointShapeRejectEnvelope(checkpointShapeViolations, nullArgGaps, contextSizeGap, discardGap);
  }

  const gaps = [...nullArgGaps, ...contextSizeGap, ...discardGap];
  const completed = [];
  const phaseOutputs = {};
  let phaseReached = 'start';
  // Track the attempted phase separately so a failure names the phase that threw.
  let phaseAttempting = null;

  // Only undefined means dispatch; null is replay input and must be rejected by the shape gate.
  const runPhase = async (name, thunk) => {
    phaseAttempting = name;
    const replayed = checkpoints[name] !== undefined;
    const out = replayed ? checkpoints[name] : await thunk();
    // Discovery intake covers fresh findings before downstream readers; replayed outputs need the same normalization at every phase.
    if (replayed || name === 'discover') normalizePhaseFindings(name, out, replayed, A);
    phaseOutputs[name] = out;
    completed.push(name);
    phaseReached = name;
    return out;
  };

  // Attach rejection handlers immediately, then await in phase order so overlapping work keeps failures attributed to the correct phase.
  const settle = (p) => p.then((value) => () => value, (error) => () => { throw error; });
  const replay = (name) => checkpoints[name] !== undefined;

  try {
    const summarizeSettled = replay('summarize') ? null : settle(summarize(c, {
      changedFiles: A.changedFiles || [], changedLines: A.changedLines || 0, limits, policy, contextLine,
    }));
    // Derive scope flags from validated inputs so callers cannot stamp a different decision.
    const derivedAgentFlags = deriveAgentFlags(A.riskTable, A.changedLines, A.scopeAnswer);
    const discoverSettled = replay('discover') ? null : settle(discover(c, {
      agentFlags: derivedAgentFlags, limits, policy, contextLine,
    }));

    const summaryOut = await runPhase('summarize', async () => (await summarizeSettled)());
    gaps.push(...(summaryOut.gaps || []));

    const discoverOut = await runPhase('discover', async () => (await discoverSettled)());
    gaps.push(...(discoverOut.gaps || []));

    // Total coverage loss must fail, but checkpoints containing findings must remain deliverable.
    // Discard failed discovery from returned checkpoints so retries dispatch it again.
    if (allActiveDimensionsDegraded(discoverOut.dispatched, discoverOut.degraded)
      && (discoverOut.findings || []).length === 0
      && checkpoints.challenge === undefined) {
      gaps.push(`all-degraded: every active discovery dimension degraded (${(discoverOut.degraded || []).join(', ')}) — no discovery agent completed, so nothing was reviewed; failing loud instead of returning an empty clean review (see the per-agent gaps above; a model/provider mismatch is the most likely cause — the envelope's resolvedPolicy names the resolution)`);
      const resumable = { ...phaseOutputs };
      // Preserve phaseOutputs for diagnostics while removing failed discovery from the resumable return.
      delete resumable.discover;
      return {
        ok: false,
        error: 'all-degraded: every active discovery dimension degraded — no review was performed',
        phaseReached,
        failingPhase: 'discover',
        artifactPaths: {},
        stats: { discovered: 0, degraded: discoverOut.degraded || [] },
        resolvedPolicy: resolvedPolicyEnvelope(policy),
        checkpoints: buildResumeCheckpoints(resumable),
        gaps,
      };
    }

    const mergeOut = await runPhase('merge', () => mergeStage(discoverOut, {
      base_branch: A.baseBranch, head_sha: A.headShaShort,
    }));
    gaps.push(...(mergeOut.gaps || []));

    limits = coarsenLimits(limits, nChangedFiles, mergeOut.findings || [], A.baseBranch);

    const verifyOut = await runPhase('verify', () => verifyStage(c, {
      findings: mergeOut.findings || [], limits, policy, nonce: A.nonce, headShaShort: A.headShaShort,
      verify: { ...(A.verify || {}), baseBranch: A.baseBranch, diffPath: A.diffPath },
    }));
    gaps.push(...(verifyOut.gaps || []));

    const validateOut = await runPhase('validate', () => validateStage(c, {
      findings: verifyOut.findings || [], limits, policy, contextLine,
    }));
    gaps.push(...(validateOut.gaps || []));

    // Resolve configuration once so filtering and reporting use the same layers.
    const resolvedReview = resolveReviewConfig(A);
    const reviewMdSubtrees = (Array.isArray(resolvedReview.reviewConfig.scopes)
      ? resolvedReview.reviewConfig.scopes
      : []).map((scope) => ({
        dir: scope.dir,
        matched: (validateOut.findings || []).filter((finding) =>
          scopeMatchesFile(scope, finding.file)).length,
      }));
    const filterOut = await runPhase('filter', () => filterStage({
      findings: validateOut.findings || [], reviewConfig: resolvedReview.reviewConfig,
      exclusionPatterns: resolvedReview.exclusionPatterns, generatedAt: A.generatedAt,
    }));
    gaps.push(...(filterOut.gaps || []));

    const challengeOut = await runPhase('challenge', () => challengeStage(c, {
      // Shared context would weaken the challenger's independent check, so its prompt contains only the claim and location.
      findings: filterOut.filtered || [], limits, policy, generatedAt: A.generatedAt,
    }));
    gaps.push(...(challengeOut.gaps || []));

    // Derive the loss disclosure from carried eliminations so resumes report the same loss.
    // Keep it outside the persisted challenge output to avoid duplicating gaps on replay.
    const markedCount = applyChallengeReplayBelt(challengeOut);
    if (markedCount > 0) {
      gaps.push(`replay-filter: ${markedCount} finding(s) recorded by an earlier pipeline pass matched this run's injection filter and were removed — disclosed per-finding in the eliminated set (eliminated_by:'injection', replay_belt:true), not counted in stats.filter`);
    }

    gaps.push(...hostPathTextGaps(
      challengeOut,
      summaryOut,
      [A.repoRoot, A.pluginRoot, A.outputDir],
    ));

    const deliveryTier = A.delivery && A.delivery.tier;
    const postReview = selectDelivery(challengeOut.findings, limits.deliveryCap, deliveryTier);
    const headlessCommentsEnabled = (configEchoValue(A, 'delivery') || '').split(',').includes('pr_comments');
    const includeDelivered = Boolean(A.delivery && A.delivery.prIdentity)
      && (A.mode !== 'headless'
        || headlessCommentsEnabled);

    const pathNormalization = {};
    for (const [phase, output] of Object.entries(phaseOutputs)) {
      const counts = {};
      for (const field of ['path_rewrites', 'replay_path_rejected']) {
        if (output.stats?.[field] > 0) counts[field] = output.stats[field];
      }
      if (Object.keys(counts).length) pathNormalization[phase] = counts;
    }
    const reportInput = {
      summary: summaryOut.summary,
      ...(includeDelivered ? { delivered: postReview } : {}),
      findings: challengeOut.findings,
      unverified: challengeOut.unverified,
      stats: {
        pathNormalization,
        discovered: (discoverOut.findings || []).length,
        validate: validateOut.stats,
        filter: filterOut.stats,
        challenge: challengeOut.stats,
        merge: compactMethodology(mergeOut.methodology),
      },
      // The report needs recorded dispatch scope because a gated agent differs from an agent returning no findings.
      dimensions: { dispatched: discoverOut.dispatched || [], degraded: discoverOut.degraded || [] },
      headShaShort: A.headShaShort,
      generatedAt: A.generatedAt,
      prIdentity: (A.delivery || {}).prIdentity || null,
      mode: A.mode,
      configEcho: A.configEcho,
      pluginRoot: A.pluginRoot,
      pipelineVersion: c.pipelineVersion,
      reviewScope: A.reviewScope,
      policy,
      deliveryTier: deliveryTier ?? 'all',
      deliveryCap: limits.deliveryCap ?? null,
      gapCount: gaps.length,
    };
    const reviewBody = renderSummaryBody(reportInput);
    // Render from pipeline data so report structure and counts remain deterministic.
    let reportOut = await runPhase('report', () => ({ report: renderReport(reportInput), gaps: [] }));
    const reportGaps = reportOut.gaps || [];
    gaps.push(...reportGaps);
    // Report checkpoints cannot substitute for rendering from current data.
    // Rebuild persisted report bytes while preserving disclosed gaps.
    reportOut = { report: renderReport(reportInput), gaps: reportGaps };
    phaseOutputs.report = reportOut;

    // Writer failures must degrade to artifact gaps because persistence failure does not invalidate completed review work.
    const writeOut = await writeArtifacts(c, {
      findings: challengeOut.findings,
      postReview,
      prIdentity: (A.delivery || {}).prIdentity,
      reviewBody,
      report: reportOut.report,
      // Only challenge needs full persisted output because it preserves the delivered findings for resume.
      checkpoints: slimPersistedCheckpoints(phaseOutputs, completed, phaseReached),
      outputDir: A.outputDir,
      headShaShort: A.headShaShort,
      // Echo the run id so the materializer can locate task output by content without a task id.
      nonce: A.nonce,
      persist: A.persist,
      policy,
    });
    gaps.push(...(writeOut.gaps || []));

    return {
      ok: true,
      phaseReached,
      stats: {
        pathNormalization,
        discovered: (discoverOut.findings || []).length,
        merged: (mergeOut.findings || []).length,
        merge: compactMethodology(mergeOut.methodology),
        verified: verifyOut.verified,
        // Per-slice proof counts expose partial verification trust that a single stage boolean cannot express.
        inputProof: verifyOut.inputProof,
        highConfidence: (challengeOut.findings || []).length,
        unverified: (challengeOut.unverified || []).length,
        degraded: discoverOut.degraded || [],
        validate: validateOut.stats,
        filter: filterOut.stats,
        // Provenance needs labels and counts without returning document contents.
        // Report this run's provenance because replayed filter statistics can describe different inputs.
        reviewConfigSource: resolvedReview.reviewConfigSource,
        exclusionsSource: resolvedReview.exclusionsSource,
        reviewMdEntryCount: resolvedReview.reviewMdEntryCount,
        reviewMdSubtrees,
        // Echo the derived decision so readers can verify scope without recomputing it.
        scope: {
          lightEligible: computeLightEligible(A.riskTable, A.changedLines),
          scopeAnswer: A.scopeAnswer !== undefined ? A.scopeAnswer : null,
          deep: derivedAgentFlags.deep !== false,
        },
        challenge: challengeOut.stats,
      },
      artifactPaths: writeOut.artifactPaths,
      resolvedPolicy: resolvedPolicyEnvelope(policy),
      // Successful persistence allows a compact return; failed persistence requires bounded in-memory checkpoints so the skill can still resume.
      checkpoints: writeOut.partial ? buildResumeCheckpoints(phaseOutputs) : { completed },
      gaps,
      // Keep bulk persistence data last so a truncated return still includes status and gaps.
      // scripts/gauntlet/awaiting.py elides persistReturn.entries from the terminal response; materialization reads the original task file.
      ...(writeOut.persistReturn ? { persistReturn: writeOut.persistReturn } : {}),
    };
  } catch (e) {
    // The throw path persists nothing, so bounded in-memory checkpoints are needed to resume completed work.
    return {
      ok: false,
      error: (e && e.message) || String(e),
      phaseReached,
      failingPhase: phaseAttempting,
      artifactPaths: {},
      stats: {},
      checkpoints: buildResumeCheckpoints(phaseOutputs),
      gaps,
    };
  }
}
