import { locationAllows } from './location.js';
// Validation, filtering, challenge, and delivery selection stages.
import { incompleteVetting } from './vetting.js';
import { effectiveBatchSize, effectiveChallengeCap, effectiveValidationRetrySize } from './capacity.js';
import { modelFor, ADMISSION_PREDICATES, DIMENSIONS, EXCLUSION_CATEGORIES, SKEPTICAL_CONTRACTS, ANCHOR_LIMITS, acceptSkepticalReturn, assessmentGaps } from './registry.js';
import { applyValidations, validationRejections, validatorAssessment } from './applyValidations.js';
import { applyFilterPipeline } from './filterFindings.js';
import { rankFindings } from './applyChallenges.js';
import { coerceReportFindings } from './renderReport.js';
import { deliveryGroups } from './dispose.js';
import { admissionContext, hasAdmission } from './admission.js';
import { scopeDecisions } from './scope.js';

// The platform rejects array-rooted tool schemas, so validation entries need an object wrapper.
const VALIDATE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    validations: {
      type: 'array',
      items: SKEPTICAL_CONTRACTS.validator.dispatchSchema,
    },
  },
  required: ['validations'],
};

function validateReturnDescriptor() {
  return Object.entries(VALIDATE_SCHEMA.properties.validations.items.properties)
    .map(([name, schema]) => {
      const values = Array.isArray(schema.enum) ? ` (${schema.enum.join(' | ')})` : '';
      return `${name}${values}`;
    })
    .join(', ');
}

function validateSchema(batch) {
  const base = VALIDATE_SCHEMA.properties.validations.items;
  return { ...VALIDATE_SCHEMA, properties: { validations: { type: 'array', items: { ...base,
    properties: { ...base.properties, finding_id: { type: 'string', enum: batch.map((f) => f.id) } },
  } } } };
}

// Keep exact small returns and explicit truncation receipts without consuming the checkpoint budget.
function validatorAudit(audit, label, result, rejections) {
  const raw = JSON.stringify(result ?? null);
  const record = { label, raw: raw.slice(0, 8192), raw_characters: raw.length, truncated: raw.length > 8192,
    rejections: rejections.slice(0, 100), omitted_rejections: Math.max(0, rejections.length - 100) };
  if (audit.batches.length < 64 && JSON.stringify(audit).length + JSON.stringify(record).length < 64000) audit.batches.push(record);
  else audit.omitted_batches += 1;
}

// Parallel results retain batch positions so gaps identify the exact findings left unvalidated.
// Failed batches keep findings at their original confidence because dispatch failure supplies no evidence against them.
export async function validateStage(ctx, input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const limits = inp.limits || {};
  const policy = inp.policy || {};
  const batchSize = effectiveBatchSize(limits, findings.length);

  if (findings.length === 0) {
    return { findings: [], gaps: [], stats: { batches_dispatched: 0, batches_completed: 0, validated: 0, skipped: 0, incomplete: 0, adjusted: 0 } };
  }

  const model = modelFor('code-gauntlet:validator', policy);

  const batches = [];
  for (let i = 0; i < findings.length; i += batchSize) batches.push(findings.slice(i, i + batchSize));

  const gaps = [];
  const accepted = new Map();
  const assessments = new Map();
  const audit = { batches: [], omitted_batches: 0 };
  const terminalRejections = new Map();
  let completed = 0;
  let dispatched = 0;
  const dispatch = (batch, label) => () => ctx.agent(validatePrompt(inp, batch), {
    label, agentType: 'code-gauntlet:validator', model, schema: validateSchema(batch),
  });
  const consume = (res, batch, label) => {
    const list = res === null || res === undefined
      ? null
      : (Array.isArray(res) ? res : (Array.isArray(res.validations) ? res.validations : null));
    // A usable list completes the dispatch; per-finding omissions stay local and are disclosed.
    if (list) completed += 1;
    const entries = (list || []).filter((e) => e && typeof e === 'object').map((e) => ({ ...e, id: e.id ?? e.finding_id }));
    const rejections = [];
    for (const finding of batch) {
      const matches = entries.filter((entry) => entry.id === finding.id);
      if (!matches.length) rejections.push({ id: String(finding.id).slice(0, 128), reasons: [list ? 'missing_entry' : 'malformed_return'] });
      for (const entry of matches) {
        assessments.set(finding.id, entry);
        const reasons = validationRejections(finding, entry);
        if (reasons.length) rejections.push({ id: String(finding.id).slice(0, 128), reasons });
        // Missing admission evidence still warrants a retry, but does not invalidate a score.
        if (!validationRejections(finding, entry, false).some((reason) => ['invalid_confidence', 'invalid_policy_subject', 'missing_entry'].includes(reason))) accepted.set(finding.id, entry);
      }
    }
    validatorAudit(audit, label, res, rejections);
    for (const rejection of rejections) terminalRejections.set(rejection.id, rejection.reasons);
    return list;
  };
  const results = await ctx.parallel(batches.map((batch, idx) => dispatch(batch, `validate-batch-${idx}`)));
  dispatched += batches.length;
  const mainLists = batches.map((batch, idx) => consume(results[idx], batch, `validate-batch-${idx}`));
  const pending = findings.filter((finding) => validationRejections(finding, accepted.get(finding.id)).length);
  if (pending.length) {
    // One follow-up round only; small batches isolate incomplete records from successful siblings.
    const retries = [];
    const retrySize = effectiveValidationRetrySize(limits, findings.length);
    for (let i = 0; i < pending.length; i += retrySize) retries.push(pending.slice(i, i + retrySize));
    const retried = await ctx.parallel(retries.map((batch, idx) => dispatch(batch, `validate-retry-${idx}`)));
    dispatched += retries.length;
    retries.forEach((batch, idx) => consume(retried[idx], batch, `validate-retry-${idx}`));
  }
  batches.forEach((batch, idx) => {
    const unscored = batch.filter((finding) => !accepted.has(finding.id));
    if (unscored.length) {
      const reasons = [...new Set(unscored.flatMap((finding) => terminalRejections.get(String(finding.id).slice(0, 128)) || []))]
        .filter((reason) => !reason.startsWith('missing_assessment:'));
      let problem = mainLists[idx] ? 'omitted scores' : 'returned null/malformed';
      if (reasons.some((reason) => !['missing_entry', 'malformed_return'].includes(reason))) {
        problem = `returned invalid/incomplete entries (${reasons.join(', ')})`;
      }
      gaps.push(`validate-batch-${idx}: validator ${problem} — ${unscored.length} finding(s) unvalidated (validation=skipped, held back to the report)`);
    }
  });

  const { adjustedCount } = applyValidations(findings, [...accepted.values()]);
  for (const finding of findings) if (!accepted.has(finding.id) && assessments.has(finding.id)) {
    finding.skeptical_assessments = [validatorAssessment(finding, assessments.get(finding.id))];
  }

  let skipped = 0;
  let incomplete = 0;
  for (const f of findings) {
    if (!accepted.has(f.id)) { f.validation = 'skipped'; skipped += 1; }
    else f.validation = 'validated';
    if (accepted.has(f.id) && validationRejections(f, accepted.get(f.id)).length) incomplete += 1;
  }

  return {
    findings,
    gaps,
    audit,
    stats: {
      batches_dispatched: dispatched,
      batches_completed: completed,
      validated: findings.length - skipped,
      skipped,
      incomplete,
      adjusted: adjustedCount,
    },
  };
}

function validatePrompt(inp, batch) {
  const ctxLine = inp.contextLine || '';
  const promotionRows = DIMENSIONS.filter((row) => row.admission?.promotion);
  const promotionHeader = [...new Set(promotionRows.map((row) => row.admission.promotion.predicate))].map((predicate) =>
    `For ${promotionRows.filter((row) => row.admission.promotion.predicate === predicate).map((row) => row.dimension).join(', ')} findings also assess ${predicate}.`).join(' ');
  const scope = EXCLUSION_CATEGORIES.filter((row) => row.consumers.includes('validator'))
    .map((row) => `- ${row.category}: ${row.applicability} Exceptions: ${row.exceptions.join(', ') || 'none'}. Proof: ${row.proof}; outcome: ${row.outcome}. ${row.reference}`).join('\n');
  // Locations and claims let validators inspect the relevant code instead of scoring identifiers without evidence.
  const block = batch.map((f) => {
    const range = !locationAllows(f, 'sourceLink') ? 'location unconfirmed' : f.line_end != null && f.line_end !== f.line_start
      ? `${f.line_start}-${f.line_end}`
      : `${f.line_start != null ? f.line_start : '?'}`;
    const ev = f.evidence ? ` | evidence: ${f.evidence}` : '';
    const sites = (f.resolved_sites || []).filter((site) => locationAllows(site, 'sourceLink'));
    const requests = sites.length ? ` | site_requests: ${JSON.stringify(sites)}` : '';
    return `- ${f.id} [${f.dimension || '?'}/${f.severity || '?'}] ${f.file || '?'}:${range} — ${f.description || ''}${ev}${requests} | admission_context: ${JSON.stringify(admissionContext(f))}`;
  }).join('\n');
  return `${ctxLine}Independently validate the supplied batch using the code-evidence and disproof contract. Classify policy_subject as ordinary, test_defect, coverage or unresolved, regardless of producer. A test location alone does not reclassify a grounded convention claim. Assess applicable admission predicates independently with confirmed, rejected or unresolved and the exact request names inspected in references. Resolution and confidence are not semantic confirmation. Assess current_defect separately for local task eligibility. For each resolved site request, confirm whether it is the same defect requiring the same correction; return site_confirmations only for those requests. Admission predicates: ${JSON.stringify(ADMISSION_PREDICATES)}\n${promotionHeader}\n${VERDICT_GUIDANCE}\nScope rows:\n${scope}\nTarget identity: ${JSON.stringify(inp.sourceCapture || {})}\nFindings:\n${block}\nReturn { validations: [{ ${validateReturnDescriptor()} }] } — confidence 0-100 (one entry per finding).`;
}

// The runtime has no wall clock, so generated_at must come from the supplied arguments.
export function filterStage(input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const config = inp.reviewConfig || {};
  const exclusionPatterns = inp.exclusionPatterns || [];
  return applyFilterPipeline(findings, config, exclusionPatterns, inp.generatedAt);
}

const CHALLENGE_SCHEMA = SKEPTICAL_CONTRACTS.challenger.dispatchSchema;

// An explicit allowlist excludes confirming rationale even when new finding fields are added.
// The location lets the challenger investigate the raw code independently.
export function blindChallengeFields(finding, diffPath) {
  return {
    title: finding.title || '',
    description: finding.description || '',
    file: finding.file || '',
    ...(locationAllows(finding, 'sourceLink') ? { line_start: finding.line_start, line_end: finding.line_end } : { location: 'location unconfirmed' }),
    ...(finding.removed_test ? {
      removed_test: {
        path: finding.removed_test.path,
        old_start: finding.removed_test.old_start,
        old_end: finding.removed_test.old_end,
      },
      diffPath,
    } : {}),
  };
}

const VERDICT_GUIDANCE = `Use real, refuted or needs_evidence with scope_assessments (possibly empty). Real and refuted require claim citations against the supplied snapshot identity, with file, side, line_start, line_end and exact quote (at most ${ANCHOR_LIMITS.lines} lines and ${ANCHOR_LIMITS.chars} characters). Needs_evidence must name the missing evidence and cannot resolve itself. For each scope assertion cite both rule and applicability with those roles; use inapplicable or contradicted when exceptions or code defeat it. Cite proven incorrect or dangerous behavior with the exception role on a real verdict. Return finding_id and stage for every assessment.`;
function challengePrompt(finding, diffPath, sourceCapture) {
  const b = blindChallengeFields(finding, diffPath);
  const removed = b.removed_test ? `Read saved diff ${diffPath}: deleted test ${JSON.stringify(b.removed_test.path)}, old lines ${b.removed_test.old_start}-${b.removed_test.old_end}. Check the deleted assertion and surviving tests.` : '';
  return `Independently check the claim's central facts against raw code. Runtime failures require a current trigger; static defects require evidence of the static claim. ${removed}\nTarget identity: ${JSON.stringify(sourceCapture || {})}\nFinding identity: ${finding.id}\nClaim/location: ${JSON.stringify(b)}\n${VERDICT_GUIDANCE}`;
}

export async function challengeStage(ctx, input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const cap = effectiveChallengeCap(inp.limits || {}, findings.length);
  const validatorTerminal = (f) => f.skeptical_assessments?.some((a) => a.stage === 'validator'
    && ['refuted', 'needs_evidence'].includes((a.raw ?? a).verdict));
  const scopeTerminal = (f) => scopeDecisions(f).some((row) => row.applies === true && ['eliminate', 'report_only'].includes(row.outcome));
  const actionable = (f) => !scopeTerminal(f) && !validatorTerminal(f) && hasAdmission(f)
    && f.admission.status === 'admitted' && incompleteVetting(f).length === 0;
  const terminal = findings.filter((f) => !actionable(f)).map((f) => ({ ...f,
    challenge: scopeTerminal(f) ? 'scope_terminal' : validatorTerminal(f) ? 'validator_terminal' : 'admission_terminal' }));
  const ranked = rankFindings(findings.filter(actionable));
  const candidates = ranked.slice(0, cap);
  const overflow = ranked.slice(cap);
  const results = candidates.length ? await ctx.parallel(candidates.map((finding, idx) => () => ctx.agent(
    challengePrompt(finding, inp.diffPath, inp.sourceCapture), {
      label: `challenge-${idx}`, agentType: 'code-gauntlet:challenger',
      model: modelFor('code-gauntlet:challenger', inp.policy || {}), schema: CHALLENGE_SCHEMA,
    }))) : [];
  const gaps = [];
  const present = [];
  const skipped = overflow.map((f) => ({ ...f, challenge: 'capped' }));
  let completed = 0;
  let incomplete = 0;
  candidates.forEach((finding, idx) => {
    const res = results[idx];
    if (res === null || res === undefined) {
      gaps.push(`challenge-${idx}: challenger returned null — finding ${finding.id} unchallenged (challenge=failed, pipeline-degraded)`);
      skipped.push({ ...finding, challenge: 'failed' });
      return;
    }
    completed += 1;
    // The dispatch position owns the finding; an echoed identity cannot redirect an assessment.
    const assessment = acceptSkepticalReturn({ ...res, finding_id: finding.id }, finding.id, 'challenger');
    const semanticGap = assessmentGaps(assessment).semanticErrors.length > 0;
    const out = { ...finding, skeptical_assessments: [
      ...(finding.skeptical_assessments || []).filter((a) => a.stage !== 'challenger'), assessment],
      challenge: semanticGap ? 'incomplete' : 'survived' };
    if (semanticGap) { incomplete += 1; skipped.push(out); }
    else present.push(out);
  });
  if (overflow.length) gaps.push(`challenge: ${overflow.length} finding(s) over challengeCap=${cap} left unchallenged (challenge=capped, pipeline-degraded)`);
  return { findings: rankFindings([...present, ...terminal]), unverified: rankFindings(skipped), eliminated: [], gaps,
    stats: { total_input: findings.length, dispatched: candidates.length, completed, incomplete,
      skipped: skipped.length, capped: overflow.length, final_count: present.length + terminal.length }, generated_at: inp.generatedAt };
}

// Delivery defaults to all survivors to avoid silently narrowing scope.
// Persisting the selected set lets delivery consume the policy decision without independently filtering or ranking it.
export function selectDelivery(survivors, cap, tier) {
  const members = tier === 'main_only'
    ? (survivors || []).filter((f) => (f.report_tag ?? f.report_destination) === 'main')
    : (survivors || []);
  const pool = deliveryGroups(members);
  // Rank a tolerant projection to handle malformed replay fields, then preserve the original entries for persistence identity.
  const rankable = pool.map(({ primary, corroborators }) => rankFindings(coerceReportFindings([primary, ...corroborators]))[0]);
  const originalByRankable = new Map(rankable.map((finding, index) => [finding, pool[index]]));
  let ranked = rankFindings(rankable).map((finding) => originalByRankable.get(finding));
  if (cap !== undefined && cap !== null) ranked = ranked.slice(0, Math.max(0, cap));
  return ranked.flatMap(({ primary, corroborators }) => [primary, ...corroborators]);
}
