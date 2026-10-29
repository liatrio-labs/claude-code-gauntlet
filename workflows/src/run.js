import { suppressAuditOverlaps } from './removedTests.js';
// Pipeline sequencing, replay, diagnostics, and return envelopes.
import { setupContext } from './contextPlan.js';
import { coarsenLimits } from './capacity.js';
import { summarize, deriveAgentFlags, discover, allActiveDimensionsDegraded } from './discover.js';
import { validateStage, filterStage, challengeStage, selectDelivery } from './reviewStages.js';
import { mergeStage } from './mergeFindings.js';
import { verifyStage } from './verifyStage.js';
import { FINDING_TEXT_FIELD_NAMES, FINDING_REWRITE_EXEMPT_FIELDS, conditionalSchemaActive } from './registry.js';
import { mentionsPreparedHostRoot, prepareHostRootPatterns, rewritePreparedRepoRoot, safeFindingLabel } from './paths.js';
import { normalizeArgsReport, nullToleranceGap, nullRespellGap, nullToleranceRejectedKeys, validateArgs } from './args.js';
import { resolveReviewConfig, resolveRemovedTestsArgs } from './args.js';
import { entryArgs, makeArgsRejectEnvelope, SKILL_RECOVERY_LINE } from './entry.js';
import { computeLightEligible, configEchoValue } from './knobs.js';
import { scopeMatchesFile } from './reviewConfig.js';
import { renderReport, renderSummaryBody } from './renderReport.js';
import { writeArtifacts } from './persist.js';
import { applyChallengeReplayBelt, normalizePhaseFindings, buildResumeCheckpoints, checkpointDiscardGap } from './checkpoints.js';
import { checkpointChecksumErrors, checkpointShapeErrors, makeCheckpointShapeRejectEnvelope, readCheckpoints, slimPersistedCheckpoints } from './checkpoints.js';
import { disposeFindings } from './dispose.js';
import { deliveryAllows } from './location.js';
import { appendMeasurement, createMeasurementTrace, recordStage, traceDecision, eliminationDecisions, stripTraceFields, decisionsFor } from './measurementTrace.js';
import { captureDeliveryReplay, appendDeliveryReplay } from './measurementReplay.js';
import { deepClone } from './wire.js';

// The host injects runtime globals, while tests supply context explicitly.
function defaultCtx() {
  return {
    agent: typeof agent === 'function' ? agent : undefined,
    parallel: typeof parallel === 'function' ? parallel : undefined,
    pipeline: typeof pipeline === 'function' ? pipeline : undefined,
    pipelineVersion: null,
  };
}

function walkHostPathText(value, transform, field = null, exempt = false) {
  if (typeof value === 'string') return transform(value, field ?? 'other', exempt);
  if (value === null || typeof value !== 'object') return value;
  for (const [key, entry] of Object.entries(value)) {
    const name = Array.isArray(value) ? field : field ?? (FINDING_TEXT_FIELD_NAMES.includes(key) ? key : 'other');
    if (!Array.isArray(value)) transform(key, name, true);
    const rewritten = walkHostPathText(entry, transform, name, exempt || FINDING_REWRITE_EXEMPT_FIELDS.includes(key));
    if (rewritten !== entry) value[key] = rewritten;
  }
  return value;
}

function hostPathTextFields(value, patterns) {
  const fields = new Set();
  walkHostPathText(value, (text, field) => {
    if (mentionsPreparedHostRoot(text, patterns)) fields.add(field);
    return text;
  });
  return [...fields].sort();
}

function rewriteHostPathText(challengeOut, summaryOut, repoRoot) {
  const { roots } = prepareHostRootPatterns([repoRoot]);
  const rewrite = (text, field, exempt) => exempt ? text : rewritePreparedRepoRoot(text, roots);
  for (const bucket of ['findings', 'unverified', 'eliminated']) {
    if (Array.isArray(challengeOut[bucket])) walkHostPathText(challengeOut[bucket], rewrite);
  }
  for (const key of Object.keys(summaryOut)) {
    if (key !== 'gaps') summaryOut[key] = walkHostPathText(summaryOut[key], rewrite);
  }
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
    mappedAliases: policy.mappedAliases || [],
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

export async function deliverFromCheckpoint(c, A, state) {
  const policy = A.policy || {};
  const { limits, gaps, completed, phaseOutputs, measurementTrace, resolvedReview,
    reviewMdSubtrees, derivedAgentFlags, runPhase, measured } = state;
  const replay = measurementTrace ? captureDeliveryReplay(c, A, state) : null;
  if (replay) c = replay.ctx;
  const { summarize: summaryOut, discover: discoverOut, merge: mergeOut,
    verify: verifyOut, validate: validateOut, filter: filterOut } = phaseOutputs;
  const summaryCopy = deepClone(summaryOut);
  const rawChallenge = phaseOutputs.challenge;
  rewriteHostPathText(rawChallenge, summaryCopy, A.repoRoot);
  const challengeCopy = deepClone(rawChallenge);
  gaps.push(...(rawChallenge.gaps || []));

  // Keep replay disclosures outside checkpoints so resumes cannot duplicate them.
  const markedCount = applyChallengeReplayBelt(challengeCopy);
  const challengeOut = disposeFindings(challengeCopy);
  if (measurementTrace) recordStage(measurementTrace, 'dispose', challengeOut, { decisions: challengeOut.decisions });
  if (markedCount > 0) {
    gaps.push(`replay-filter: ${markedCount} finding(s) recorded by an earlier pipeline pass matched this run's injection filter and were removed — disclosed per-finding in the eliminated set (eliminated_by:'injection', replay_belt:true), not counted in stats.filter`);
  }

  gaps.push(...hostPathTextGaps(
    challengeOut,
    summaryCopy,
    [A.repoRoot, A.pluginRoot, A.outputDir],
  ));

  const deliveryTier = A.delivery && A.delivery.tier;
  const eligible = challengeOut.findings.filter((f) => deliveryAllows(f, 'body'));
  const selected = selectDelivery(eligible, limits.deliveryCap, deliveryTier);
  const postReview = [...selected.filter((f) => deliveryAllows(f, 'inline')),
    ...selected.filter((f) => f.disposition === 'body')];
  if (measurementTrace) recordStage(measurementTrace, 'select', postReview, { cap: limits.deliveryCap,
    cap_hit: selectDelivery(eligible, null, deliveryTier).length > postReview.length,
    decisions: challengeOut.findings.filter((f) => !postReview.includes(f) && f.trace_id).map((f) => traceDecision(f,
      deliveryTier === 'main_only' && (f.report_tag ?? f.report_destination) !== 'main' ? 'delivery_tier' : 'delivery_cap',
      'delivery selection', [], 'not_selected')),
  });
  if (measurementTrace) measurementTrace.complete = true;
  stripTraceFields(phaseOutputs);
  stripTraceFields(challengeOut);
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
    summary: summaryCopy.summary,
    ...(includeDelivered ? { delivered: postReview } : {}),
    findings: challengeOut.findings,
    unverified: challengeOut.unverified,
    eliminated: challengeOut.eliminated,
    stats: {
      pathNormalization,
      discovered: (discoverOut.findings || []).length,
      validate: validateOut.stats,
      filter: filterOut.stats,
      challenge: challengeOut.stats,
      merge: compactMethodology(mergeOut.methodology),
    },
    // The report needs recorded dispatch scope because a gated agent differs from an agent returning no findings.
    dimensions: { dispatched: discoverOut.dispatched || [], degraded: discoverOut.degraded || [], removedTestAudit: discoverOut.removedTestAudit || { status: 'unavailable' } },
    headShaShort: A.headShaShort,
    generatedAt: A.generatedAt,
    prIdentity: (A.delivery || {}).prIdentity || null,
    mode: A.mode,
    configEcho: A.configEcho,
    pluginRoot: A.pluginRoot,
    pipelineVersion: c.pipelineVersion,
    reviewScope: A.reviewScope,
    hasReviewMd: Array.isArray(A.reviewMd) ? A.reviewMd.length > 0 : A.reviewConfigPath != null,
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
    findings: [...challengeOut.findings, ...challengeOut.unverified, ...challengeOut.eliminated],
    postReview,
    prIdentity: (A.delivery || {}).prIdentity,
    reviewBody,
    report: reportOut.report,
    checkpoints: slimPersistedCheckpoints(phaseOutputs, completed, state.phaseReached),
    outputDir: A.outputDir,
    headShaShort: A.headShaShort,
    // Echo the run id so the materializer can locate task output by content without a task id.
    nonce: A.nonce,
    persist: A.persist, python: A.python, pluginRoot: A.pluginRoot,
    policy,
  });
  gaps.push(...(writeOut.gaps || []));

  return appendDeliveryReplay(measured({
    ok: true,
    phaseReached: state.phaseReached,
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
    checkpoints: writeOut.partial ? buildResumeCheckpoints(phaseOutputs) : { completed },
    gaps,
    // Keep bulk persistence data last so a truncated return still includes status and gaps.
    // scripts/gauntlet/awaiting.py elides persistReturn.entries from the terminal response; materialization reads the original task file.
    ...(writeOut.persistReturn ? { persistReturn: writeOut.persistReturn } : {}),
  }), replay?.record);
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
    ...nullToleranceRejectedKeys(A, droppedNulls, derivedPaths).map((key) => nullToleranceGap(key)),
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
  let limits = coarsenLimits(A.limits || {}, nChangedFiles, 0, A.baseBranch, A);
  const policy = A.policy || {};
  const contextPath = `${A.outputDir}/code-gauntlet-context-${A.headShaShort}.md`;
  const { contextLine, contextSizeGap } = setupContext({ contextPath, contextLines: A.contextLines, contextChars: A.contextChars });
  // Inspect the stamped checkpoints before readCheckpoints can discard malformed input.
  const discardGap = checkpointDiscardGap(A.checkpoints);
  const checkpoints = readCheckpoints(A);
  const checksumViolations = checkpointChecksumErrors(A.checkpoints);
  if (checksumViolations.length) {
    return makeCheckpointShapeRejectEnvelope(checksumViolations, nullArgGaps, contextSizeGap, discardGap);
  }
  // Reject malformed replay data before any phase reads it or dispatches work.
  const checkpointShapeViolations = checkpointShapeErrors(checkpoints);
  if (checkpointShapeViolations.length) {
    return makeCheckpointShapeRejectEnvelope(checkpointShapeViolations, nullArgGaps, contextSizeGap, discardGap);
  }

  const gaps = [...nullArgGaps, ...contextSizeGap, ...discardGap];
  const completed = [];
  const phaseOutputs = {};
  const measurementTrace = A.measurementTrace === true ? createMeasurementTrace(A.nonce) : null;
  const replayArgs = measurementTrace ? deepClone(A) : null;
  let discoveryDecisions = [];
  const measured = (envelope) => appendMeasurement(envelope, measurementTrace);
  let phaseReached = 'start';
  // Track the attempted phase separately so a failure names the phase that threw.
  let phaseAttempting = null;

  // Only undefined means dispatch; null is replay input and must be rejected by the shape gate.
  const runPhase = async (name, thunk) => {
    phaseAttempting = name;
    const replayed = checkpoints[name] !== undefined;
    const out = replayed ? checkpoints[name] : await thunk();
    // Discovery intake covers fresh findings before downstream readers; replayed outputs need the same normalization at every phase.
    const rejected = [];
    if (replayed || name === 'discover') normalizePhaseFindings(name, out, replayed, A, rejected);
    let verifyRequests;
    if (name === 'verify') {
      verifyRequests = out.findings || [];
      const discovery = phaseOutputs.discover;
      const audit = suppressAuditOverlaps(verifyRequests, discovery?.removedTestAudit);
      out.findings = audit.findings;
      if (discovery) discovery.removedTestAudit = audit.audit;
      rejected.push(...verifyRequests.filter((f) => !audit.findings.includes(f) && f.trace_id)
        .map((f) => traceDecision(f, 'audit_overlap', 'removed-test audit represents this anchor')));
    }
    if (name === 'discover') discoveryDecisions = [...decisionsFor(out), ...rejected];
    if (measurementTrace && ['merge', 'verify', 'validate', 'filter', 'challenge'].includes(name)) {
      recordStage(measurementTrace, name, out, { replayed,
        ...(!replayed && ((name === 'verify' && out.verified === false) || (name === 'validate' && out.stats?.skipped > 0) || (name === 'challenge' && out.stats?.skipped > 0)) ? { coverage: 'partial' } : {}),
        ...(name === 'merge' ? { decisions: [...discoveryDecisions, ...rejected] } : rejected.length ? { decisions: rejected } : {}),
        ...(name === 'filter' || name === 'challenge' ? { decisions: [
          ...rejected, ...eliminationDecisions(out),
          ...(name === 'challenge' ? (out.unverified || []).filter((f) => f.trace_id).map((f) => traceDecision(f, f.challenge === 'capped' ? 'challenge_cap' : 'challenge_failed', f.challenge, [], 'held_back')) : []),
        ], cap_hit: (out.unverified || []).some((f) => f.challenge === 'capped') } : {}),
      });
    } else if (name === 'discover' && replayed) recordStage(measurementTrace, name, out, { replayed });
    if (name === 'verify') {
      phaseOutputs[name] = { ...out, findings: verifyRequests };
    } else phaseOutputs[name] = out;
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
      measurementTrace, agentFlags: derivedAgentFlags, limits, policy, contextLine, repoRoot: A.repoRoot, removedTests: resolveRemovedTestsArgs(A),
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
      stripTraceFields(phaseOutputs);
      const resumable = { ...phaseOutputs };
      // Preserve phaseOutputs for diagnostics while removing failed discovery from the resumable return.
      delete resumable.discover;
      return measured({
        ok: false,
        error: 'all-degraded: every active discovery dimension degraded — no review was performed',
        phaseReached,
        failingPhase: 'discover',
        artifactPaths: {},
        stats: { discovered: 0, degraded: discoverOut.degraded || [] },
        resolvedPolicy: resolvedPolicyEnvelope(policy),
        checkpoints: buildResumeCheckpoints(resumable),
        gaps,
      });
    }

    const mergeOut = await runPhase('merge', () => mergeStage(discoverOut, {
      base_branch: A.baseBranch, head_sha: A.headShaShort,
    }));
    gaps.push(...(mergeOut.gaps || []));

    limits = coarsenLimits(limits, nChangedFiles, mergeOut.findings || [], A.baseBranch, A);

    const verifyOut = await runPhase('verify', () => verifyStage(c, {
      findings: mergeOut.findings || [], measurementTrace, limits, policy, nonce: A.nonce, headShaShort: A.headShaShort,
      python: A.python, repoRoot: A.repoRoot, outputDir: A.outputDir, pluginRoot: A.pluginRoot,
      verify: { ...(A.verify || {}), baseBranch: A.baseBranch, diffPath: A.diffPath,
        contextMap: `${A.outputDir}/code-gauntlet-context-map-${A.headShaShort}.json`,
        contextFile: `${A.outputDir}/code-gauntlet-context-${A.headShaShort}.md` },
    }));
    gaps.push(...(verifyOut.gaps || []));

    const validateOut = await runPhase('validate', () => validateStage(c, {
      findings: verifyOut.findings || [], limits, policy, contextLine,
    }));
    gaps.push(...(validateOut.gaps || []));

    const resolvedReview = resolveReviewConfig(A);
    // Keep input disclosures outside phase outputs so checkpoint replay cannot duplicate them.
    gaps.push(...resolvedReview.gaps);
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

    await runPhase('challenge', async () => {
      const out = await challengeStage(c, {
        // Shared context would weaken the challenger's independent check, so its prompt contains only the claim and location.
        findings: filterOut.filtered || [], limits, policy, generatedAt: A.generatedAt, diffPath: A.diffPath,
      });
      out.eliminated = [...(verifyOut.eliminated || []), ...(filterOut.eliminated || []), ...(out.eliminated || [])];
      return out;
    });
    return await deliverFromCheckpoint(c, A, {
      limits, gaps, completed, phaseOutputs, measurementTrace, resolvedReview,
      reviewMdSubtrees, derivedAgentFlags, runPhase, measured, replayArgs,
      get phaseReached() { return phaseReached; },
    });
  } catch (e) {
    stripTraceFields(phaseOutputs);
    return measured({
      ok: false,
      error: (e && e.message) || String(e),
      phaseReached,
      failingPhase: phaseAttempting,
      artifactPaths: {},
      stats: {},
      checkpoints: buildResumeCheckpoints(phaseOutputs),
      gaps,
    });
  }
}
