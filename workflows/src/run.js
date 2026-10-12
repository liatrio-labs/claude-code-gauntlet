// Pipeline sequencing, replay, diagnostics, and return envelopes.
// Every stage takes an injected `ctx` ({ agent, parallel }) so unit tests can drive
// it with a mock (the runtime globals do not exist under node:test). Defaults fall
// back to the runtime globals when present, so the shipped bundle needs no wiring.
//
// Failure contract (Phase 0): bare agent() THROWS on schema-retry-exhaustion (cap 5)
// and unknown agentType; parallel() converts a failed member to null. So single agent()
// calls are wrapped in try/catch. Null members are NOT handled uniformly: discover,
// validate and challenge keep the results positionally aligned and push a NAMED gap per
// null member, because a degraded member must be traceable to the exact work it dropped
// (see validateStage's header). Summarize is the exception — it .filter(Boolean)s null
// bucket members without per-bucket gaps, and emits one generic gap if no partial
// survives, the merge or single-call result is null, or any summarize dispatch throws.
// No wall-clock, no import at runtime.
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

// Runtime globals are injected by the workflow host; under node:test they are absent,
// so ctx must be supplied. defaultCtx lets the shipped pipeline call stages without wiring.
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

// resolvedPolicyEnvelope(policy) -> the envelope's resolvedPolicy field, shared by the
// success return and the all-degraded failure return so the two report the identical
// resolution and a change to either fallback costs one edit.
// 'firstParty' when the waist omitted provider — the omission and the explicit value
// resolve identically in resolvePolicy, and the envelope reports the resolution.
//
// `gateway`/`conditionalSchema`: a provider that would 400 on the conditional
// per-dimension construct must be self-diagnosing from the envelope alone, the same reason
// `provider` itself rides here — an operator staring at an all-degraded conventions-and-intent
// dispatch should not have to re-derive conditionalSchemaActive by hand. `conditionalSchema`
// is the SAME predicate the dispatch gate used (not re-derived by a parallel computation that
// could drift from it), so the envelope can never claim a resolution the dispatch disagrees
// with.
function resolvedPolicyEnvelope(policy) {
  return {
    subagentModel: policy.subagentModel || null,
    provider: policy.provider || 'firstParty',
    gateway: !!policy.gateway,
    conditionalSchema: conditionalSchemaActive(policy),
  };
}

// merge()'s per-agent channel/dedup/validation-warning granularity — the only
// place it exists — reaches a consumer on the compact return. Count fields ride as-is; free-text
// warning ARRAYS ride as lengths, so the compact-return principle (names/counts, never bulk)
// holds. Adding a methodology COUNT field is one edit in mergeFindings.js; adding a
// FREE-TEXT one is two — that entry plus one name here. Stated plainly rather than made
// clever: a heuristic that guessed at array-of-strings would silently truncate a future
// count array.
const METHODOLOGY_COUNT_ONLY_FIELDS = ['truncation_warnings', 'validation_warnings'];
export function compactMethodology(m) {
  if (!m || typeof m !== 'object' || Array.isArray(m)) return {};
  const out = {};
  for (const [k, v] of Object.entries(m)) {
    out[k] = METHODOLOGY_COUNT_ONLY_FIELDS.includes(k) && Array.isArray(v) ? v.length : v;
  }
  return out;
}

// --- Full orchestration: runWith --------------------------------------------

// runWith(ctx, rawArgs) -> compact envelope.
// The single testable orchestration seam (pipeline_entry.js's run() just builds the
// real-globals ctx and delegates here). Validates the args waist up front (reject ->
// ok:false, no dispatch), then threads summarize -> discover -> merge -> verify ->
// validate -> filter -> challenge -> report inside ONE top-level try/catch, checking
// checkpoints before each phase and persisting via writeArtifacts at the end. Every
// stage's gaps aggregate into the final envelope. writeArtifacts catches its own agent
// throws (degrading to a partial-artifacts gap), so the top-level catch is the last
// resort for unexpected throws in the deterministic glue — it returns
// { ok:false, error, phaseReached } and NEVER lets a throw escape.
// The return is compact by design: counts + artifact paths + gaps, never the raw
// findings bulk.
export async function runWith(ctx, rawArgs) {
  // Keep this seam throw-free for direct callers while returning the entry's refusal shape.
  const entry = entryArgs(rawArgs);
  if (!entry.ok) return entry.envelope;
  // Tolerated nulls and respellings can change the effective configuration, so disclose
  // each actual substitution on both exits. Use the waist entryArgs already unwrapped.
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

  // Keep runtime globals while letting callers replace individual seams.
  const c = { ...defaultCtx(), ...(ctx || {}) };
  // Coarsen once with the changed-file count and again after merge knows the finding count.
  const nChangedFiles = (A.changedFiles || []).length;
  let limits = coarsenLimits(A.limits || {}, nChangedFiles, 0, A.baseBranch);
  const policy = A.policy || {};
  const contextPath = `${A.outputDir}/code-gauntlet-context-${A.headShaShort}.md`;
  // The host measures this file. Its line and character counts form the read plan; agents
  // receive the resulting sentence, not the path or measurements.
  const { contextLine, contextSizeGap } = setupContext({ contextPath, contextLines: A.contextLines, contextChars: A.contextChars });
  // Inspect the stamped checkpoint waist before readCheckpoints can discard its shape.
  const discardGap = checkpointDiscardGap(A.checkpoints);
  const checkpoints = readCheckpoints(c, A);
  // Refuse malformed replay data before any phase can read it or dispatch work.
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

  // Reuse checkpoint outputs without dispatching, while recording each reached phase.
  // Only undefined dispatches; null means replay and is refused by the shape gate.
  const runPhase = async (name, thunk) => {
    phaseAttempting = name;
    const replayed = checkpoints[name] !== undefined;
    const out = replayed ? checkpoints[name] : await thunk();
    // Discover is the entry to every downstream finding-bearing phase.
    if (replayed || name === 'discover') normalizePhaseFindings(name, out, replayed, A);
    phaseOutputs[name] = out;
    completed.push(name);
    phaseReached = name;
    return out;
  };

  // Start independent work together, but await it in phase order and attach rejection
  // handlers immediately so errors stay attributed to the phase that failed.
  // stages_latency.test.js pins overlap, phase order, replay, and rejection attribution.
  // settle(p) -> Promise<thunk>: the thunk returns the value or re-throws the error.
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

    // Fail loud when nothing was reviewed, but preserve a replayable challenge result or
    // findings from a version-skew checkpoint. Drop a failed discovery checkpoint so a
    // retry dispatches it again; carry dimension and policy details for diagnosis.
    if (allActiveDimensionsDegraded(discoverOut.dispatched, discoverOut.degraded)
      && (discoverOut.findings || []).length === 0
      && checkpoints.challenge === undefined) {
      gaps.push(`all-degraded: every active discovery dimension degraded (${(discoverOut.degraded || []).join(', ')}) — no discovery agent completed, so nothing was reviewed; failing loud instead of returning an empty clean review (see the per-agent gaps above; a model/provider mismatch is the most likely cause — the envelope's resolvedPolicy names the resolution)`);
      const resumable = { ...phaseOutputs };
      // Drop discover only from returned checkpoints, never from phaseOutputs.
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

    // The finding count now exists — re-coarsen so verify slices, validate batches,
    // and the challenge cap keep the remaining worst-case fan-out under the guard.
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

    // Resolve both parsed and raw review configuration once for filtering and reporting.
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
      // The challenge prompt is limited to each finding's claim and location; shared context
      // would weaken that independent check.
      findings: filterOut.filtered || [], limits, policy, generatedAt: A.generatedAt,
    }));
    gaps.push(...(challengeOut.gaps || []));

    applyChallengeReplayBelt(challengeOut);
    if (challengeOut && typeof challengeOut === 'object') {
      // Derive from the carried eliminated entries so resumes disclose the same loss.
      // Keep this gap outside challengeOut because that object is persisted and re-scanned.
      const markedCount = challengeOut.eliminated.filter((f) => f.replay_belt === true).length;
      if (markedCount > 0) {
        gaps.push(`replay-filter: ${markedCount} finding(s) recorded by an earlier pipeline pass matched this run's injection filter and were removed — disclosed per-finding in the eliminated set (eliminated_by:'injection', replay_belt:true), not counted in stats.filter`);
      }

    }

    gaps.push(...hostPathTextGaps(
      challengeOut,
      summaryOut,
      [A.repoRoot, A.pluginRoot, A.outputDir],
    ));

    // Deterministic delivery selection: the challenge-survivors filtered by the user-chosen
    // delivery TIER (args.delivery.tier — 'all' by default, 'main_only' to withhold
    // suggestions), rank-ordered and capped at limits.deliveryCap (fed from
    // CODE_GAUNTLET_PR_COMMENT_CAP by the skill). Persisted so Phase 8 posts it verbatim — the
    // live agent never re-filters or re-ranks. Challenge-removed (challengeOut.eliminated) and
    // challenge-skipped (challengeOut.unverified) are already absent here, so they stay excluded.
    const deliveryTier = A.delivery && A.delivery.tier;
    // selectDelivery ranks through the renderer-owned tolerant projection, while retaining
    // the original finding objects for persistence. renderReport applies the projection again
    // to its own input, so malformed replay findings cannot reach a raw rankKey.
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
      // The report needs the actual dispatched and degraded dimensions to describe scope.
      // dispatched lists the agents discovery recorded as activated, possibly from a replayed checkpoint;
      // a scope-gated agent is absent, not present with empty results.
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
    // Render from current pipeline data so report structure and counts are deterministic.
    // The report is code-rendered; a generated fence pins its sections to report-format.md.
    let reportOut = await runPhase('report', () => ({ report: renderReport(reportInput), gaps: [] }));
    const reportGaps = reportOut.gaps || [];
    gaps.push(...reportGaps);
    // A replayed report checkpoint is legacy input, never a substitute for rendering.
    // Rebuild the persisted bytes from the current pipeline data while carrying forward
    // any gaps the checkpoint disclosed. Fresh reports are rebuilt too, keeping the
    // phaseOutputs invariant explicit at this seam.
    reportOut = { report: renderReport(reportInput), gaps: reportGaps };
    phaseOutputs.report = reportOut;

    // Persistence is a post-phase step: writeArtifacts owns its try/catch, so a
    // writer failure degrades to a partial-artifacts gap rather than the top-level catch.
    const writeOut = await writeArtifacts(c, {
      findings: challengeOut.findings,
      postReview,
      prIdentity: (A.delivery || {}).prIdentity,
      reviewBody,
      report: reportOut.report,
      // Only the resume-consumed challenge phase needs its full output persisted.
      checkpoints: slimPersistedCheckpoints(phaseOutputs, completed, phaseReached),
      outputDir: A.outputDir,
      headShaShort: A.headShaShort,
      // The run's own id, echoed in persistReturn so the materializer can find this
      // run's task output file by content when no task id is in hand.
      nonce: A.nonce,
      // When an assembler is supplied, the writer saves unique content and derives projections.
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
        // The inline proof ledger. `verified` is one
        // boolean about the whole stage; this says how many slices were PROVEN to have
        // been verified against the document this run dispatched, and how many were not.
        // `retried` counts slices trusted only on their second inline transcription
        // attempt; `oversize` counts findings degraded before dispatch.
        inputProof: verifyOut.inputProof,
        highConfidence: (challengeOut.findings || []).length,
        unverified: (challengeOut.unverified || []).length,
        degraded: discoverOut.degraded || [],
        validate: validateOut.stats,
        filter: filterOut.stats,
        // Expose provenance labels and counts without returning review document contents.
        // Review provenance stats describe this run, not a replayed filter checkpoint.
        reviewConfigSource: resolvedReview.reviewConfigSource,
        exclusionsSource: resolvedReview.exclusionsSource,
        reviewMdEntryCount: resolvedReview.reviewMdEntryCount,
        reviewMdSubtrees,
        // Echo the derived decision so a reader can verify scope without recomputing it.
        scope: {
          lightEligible: computeLightEligible(A.riskTable, A.changedLines),
          scopeAnswer: A.scopeAnswer !== undefined ? A.scopeAnswer : null,
          deep: derivedAgentFlags.deep !== false,
        },
        challenge: challengeOut.stats,
      },
      artifactPaths: writeOut.artifactPaths,
      resolvedPolicy: resolvedPolicyEnvelope(policy),
      // On persist success the resume state lives in artifactPaths.checkpoints — the
      // compact return carries only phase NAMES (never the findings bulk). If the writer
      // FAILED nothing was persisted, so carry the in-memory resume state (phases, or
      // names+truncated when it would exceed the budget) so the skill can still resume.
      checkpoints: writeOut.partial ? buildResumeCheckpoints(phaseOutputs) : { completed },
      gaps,
      // Keep bulk persistence data last so a truncated return still includes status and gaps.
      // Twin: scripts/gauntlet/awaiting.py elides persistReturn.entries[].text.
      ...(writeOut.persistReturn ? { persistReturn: writeOut.persistReturn } : {}),
    };
  } catch (e) {
    // Nothing was persisted on the throw path either — carry the in-memory resume state
    // (bounded by the char budget) in the compact return so the skill can resume the
    // failed run rather than restarting from scratch.
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
