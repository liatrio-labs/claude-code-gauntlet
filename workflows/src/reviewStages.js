import { locationAllows } from './location.js';
// Validation, filtering, challenge, and delivery selection stages.
import { incompleteVetting } from './vetting.js';
import { effectiveBatchSize, effectiveChallengeCap, effectiveValidationRetrySize } from './capacity.js';
import { modelFor, ADMISSION_PREDICATES, EXCLUSION_CATEGORIES, SKEPTICAL_CONTRACTS, ANCHOR_LIMITS, acceptSkepticalReturn, assessmentGaps } from './registry.js';
import { applyValidations, validationRejections, validatorAssessment } from './applyValidations.js';
import { applyFilterPipeline } from './filterFindings.js';
import { rankFindings } from './applyChallenges.js';
import { coerceReportFindings } from './renderReport.js';
import { deliveryGroups } from './dispose.js';
import { admissionContext, hasAdmission } from './admission.js';
import { scopeDecisions } from './scope.js';

// The platform rejects array-rooted tool schemas, so validation entries need an object wrapper.
const VALIDATE_ITEM = SKEPTICAL_CONTRACTS.validator.dispatchSchema;

const VALIDATE_RETURN = Object.entries(VALIDATE_ITEM.properties)
  .map(([name, { enum: values }]) => name + (values ? ` (${values.join(' | ')})` : '')).join(', ');

function admissionOnlyRetry(batch, accepted) {
  return batch.every((f) => accepted.has(f.id)
    && validationRejections(f, accepted.get(f.id)).every((reason) => reason.startsWith('missing_assessment:')));
}

function validateSchema(batch, accepted) {
  const onlyAdmission = admissionOnlyRetry(batch, accepted);
  const fields = onlyAdmission ? ['finding_id', 'admission_assessments'] : Object.keys(VALIDATE_ITEM.properties);
  let required = VALIDATE_ITEM.required;
  if (onlyAdmission) required = fields;
  else if (batch.some((f) => accepted.has(f.id))) required = ['finding_id'];
  const properties = Object.fromEntries(fields.map((key) => [key, VALIDATE_ITEM.properties[key]]));
  properties.finding_id = { type: 'string', enum: batch.map((f) => f.id) };
  return { type: 'object', additionalProperties: false, required: ['validations'],
    properties: { validations: { type: 'array', items: { ...VALIDATE_ITEM, required, properties } } } };
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
  const batchSize = effectiveBatchSize(limits, findings.length);

  if (findings.length === 0) {
    return { findings: [], gaps: [], stats: { batches_dispatched: 0, batches_completed: 0, validated: 0, skipped: 0, incomplete: 0, adjusted: 0 } };
  }

  const batches = [];
  for (let i = 0; i < findings.length; i += batchSize) batches.push(findings.slice(i, i + batchSize));

  const gaps = [];
  const accepted = new Map();
  const assessments = new Map();
  const audit = { batches: [], omitted_batches: 0 };
  const terminalRejections = new Map();
  let completed = 0;
  let dispatched = batches.length;
  const dispatch = (batch, label) => () => ctx.agent(validatePrompt(inp, batch, accepted), {
    label, agentType: 'code-gauntlet:validator', model: modelFor('code-gauntlet:validator', inp.policy || {}), schema: validateSchema(batch, accepted),
  });
  const consume = (res, batch, label) => {
    const list = Array.isArray(res) ? res : Array.isArray(res?.validations) ? res.validations : null;
    // A usable list completes the dispatch; per-finding omissions stay local and are disclosed.
    if (list) completed += 1;
    const entries = (list || []).filter((e) => e && typeof e === 'object').map((e) => ({ ...e, id: e.id ?? e.finding_id }));
    const rejections = [];
    for (const f of batch) {
      const matches = entries.filter((entry) => entry.id === f.id);
      if (!matches.length) rejections.push({ id: String(f.id).slice(0, 128), reasons: [list ? 'missing_entry' : 'malformed_return'] });
      for (const entry of matches) {
        const prior = accepted.get(f.id);
        let merged = entry;
        if (prior && label.startsWith('validate-retry-')) {
          const missing = validationRejections(f, prior, false);
          const field = 'admission_assessments';
          const updates = (Array.isArray(entry[field]) ? entry[field] : [])
            .filter((a) => missing.includes(`missing_assessment:${a?.predicate}`));
          merged = { ...prior, [field]: [...(Array.isArray(prior[field]) ? prior[field] : [])
            .filter((a) => !updates.some((u) => u?.predicate === a?.predicate)), ...updates] };
          if (assessmentGaps(validatorAssessment(f, prior)).semanticErrors.length)
            for (const key of ['stage', 'verdict', 'citations', 'missing_evidence', 'scope_assessments'])
              if (Object.hasOwn(entry, key)) merged[key] = entry[key];
        }
        assessments.set(f.id, merged);
        const reasons = validationRejections(f, merged);
        if (reasons.length) rejections.push({ id: String(f.id).slice(0, 128), reasons });
        // Missing admission evidence still warrants a retry, but does not invalidate a score.
        if (!reasons.some((reason) => ['invalid_confidence', 'invalid_policy_subject', 'missing_entry'].includes(reason))) accepted.set(f.id, merged);
      }
    }
    validatorAudit(audit, label, res, rejections);
    for (const rejection of rejections) terminalRejections.set(rejection.id, rejection.reasons);
    return list;
  };
  const results = await ctx.parallel(batches.map((batch, idx) => dispatch(batch, `validate-batch-${idx}`)));
  const mainLists = batches.map((batch, idx) => consume(results[idx], batch, `validate-batch-${idx}`));
  const pending = findings.filter((f) => validationRejections(f, accepted.get(f.id)).length);
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
    const unscored = batch.filter((f) => !accepted.has(f.id));
    if (unscored.length) {
      const reasons = [...new Set(unscored.flatMap((f) => terminalRejections.get(String(f.id).slice(0, 128)) || []))]
        .filter((reason) => !reason.startsWith('missing_assessment:'));
      let problem = mainLists[idx] ? 'omitted scores' : 'returned null/malformed';
      if (reasons.some((reason) => !['missing_entry', 'malformed_return'].includes(reason))) {
        problem = `returned invalid/incomplete entries (${reasons.join(', ')})`;
      }
      gaps.push(`validate-batch-${idx}: validator ${problem} — ${unscored.length} finding(s) unvalidated (validation=skipped, held back to the report)`);
    }
  });

  const { adjustedCount } = applyValidations(findings, [...accepted.values()]);
  let skipped = 0;
  let incomplete = 0;
  for (const f of findings) {
    const score = accepted.get(f.id);
    f.validation = score ? 'validated' : 'skipped';
    if (score) incomplete += Number(validationRejections(f, score).length > 0);
    else {
      skipped += 1;
      if (assessments.has(f.id)) f.skeptical_assessments = [validatorAssessment(f, assessments.get(f.id))];
    }
  }

  return { findings, gaps, audit, stats: { batches_dispatched: dispatched, batches_completed: completed,
    validated: findings.length - skipped, skipped, incomplete, adjusted: adjustedCount } };
}

function validatePrompt(inp, batch, accepted) {
  const ctxLine = inp.contextLine || '';
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
    return `- ${f.id} [${f.dimension || '?'}/${f.severity || '?'}] ${f.file || '?'}:${range} — ${f.description || ''}${ev}${requests} | admission_context: ${JSON.stringify(admissionContext(f, accepted.get(f.id)))}`;
  }).join('\n');
  const admission = `Assess confirmed/rejected/unresolved; references name inspected requests. Admission predicates: ${JSON.stringify(ADMISSION_PREDICATES)}`;
  if (admissionOnlyRetry(batch, accepted)) return `${ctxLine}Supply only the missing admission assessments in admission_context. ${admission}\nFindings:\n${block}\nReturn { validations: [{ finding_id, admission_assessments }] } (one entry per finding).`;
  return `${ctxLine}Independently validate using the code-evidence and disproof contract. First judge the correctness, confidence, justification and reachability from code; missing admission evidence or posting value must not lower confidence or change reachability. Then classify policy_subject independently of producer/location. Assess admission_context's subject predicates or promotion.confirmed when confirmed; resolution/confidence cannot confirm. Assess body_fix_task separately. Confirm requested sites: same defect requiring the same correction. Preserve accepted judgments. ${admission}\n${VERDICT_GUIDANCE}\nScope rows:\n${scope}\nTarget identity: ${JSON.stringify(inp.sourceCapture || {})}\nFindings:\n${block}\nReturn { validations: [{ ${VALIDATE_RETURN} }] } — confidence 0-100 (one entry per finding).`;
}

// The runtime has no wall clock, so generated_at must come from the supplied arguments.
export function filterStage(input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const config = inp.reviewConfig || {};
  const exclusionPatterns = inp.exclusionPatterns || [];
  return applyFilterPipeline(findings, config, exclusionPatterns, inp.generatedAt);
}

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

const VERDICT_GUIDANCE = `Use real, refuted or needs_evidence. Real/refuted: snapshot-bound exact claim citations (max ${ANCHOR_LIMITS.lines} lines/${ANCHOR_LIMITS.chars} chars). needs_evidence names missing evidence; no self-resolution. scope_assessments: empty or cite rule/applicability; defeated assertions: inapplicable/contradicted. Cite proven incorrect/dangerous behavior as exception on real.`;
function challengePrompt(finding, diffPath, sourceCapture) {
  const b = blindChallengeFields(finding, diffPath);
  const removed = b.removed_test ? `Read saved diff ${diffPath}: deleted test ${JSON.stringify(b.removed_test.path)}, old lines ${b.removed_test.old_start}-${b.removed_test.old_end}. Check the deleted assertion and surviving tests.` : '';
  return `Independently check the claim's central facts against raw code. Prove a current trigger for runtime failures, the static claim for static defects. ${removed}\nTarget identity: ${JSON.stringify(sourceCapture || {})}\nFinding identity: ${finding.id}\nClaim/location: ${JSON.stringify(b)}\n${VERDICT_GUIDANCE}`;
}

export async function challengeStage(ctx, input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const cap = effectiveChallengeCap(inp.limits || {}, findings.length);
  const validatorTerminal = (f) => f.skeptical_assessments?.some((a) => a.stage === 'validator'
    && ['refuted', 'needs_evidence'].includes(a.raw?.verdict));
  const scopeTerminal = (f) => scopeDecisions(f).some((row) => row.applies === true && ['eliminate', 'report_only'].includes(row.outcome));
  const actionable = (f) => !scopeTerminal(f) && !validatorTerminal(f) && hasAdmission(f)
    && f.admission.status === 'admitted' && incompleteVetting(f).length === 0;
  const terminal = findings.filter((f) => !actionable(f)).map((f) => {
    if (scopeTerminal(f)) return { ...f, challenge: 'scope_terminal' };
    return { ...f, challenge: validatorTerminal(f) ? 'validator_terminal' : 'admission_terminal' };
  });
  const ranked = rankFindings(findings.filter(actionable));
  const candidates = ranked.slice(0, cap);
  const overflow = ranked.slice(cap);
  const results = candidates.length ? await ctx.parallel(candidates.map((finding, idx) => () => ctx.agent(
    challengePrompt(finding, inp.diffPath, inp.sourceCapture), {
      label: `challenge-${idx}`, agentType: 'code-gauntlet:challenger',
      model: modelFor('code-gauntlet:challenger', inp.policy || {}), schema: SKEPTICAL_CONTRACTS.challenger.dispatchSchema,
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
