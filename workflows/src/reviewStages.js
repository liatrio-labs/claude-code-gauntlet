import { defineAgentCall } from './agentCalls.js';
import { locationAllows } from './location.js';
// Validation, filtering, challenge, and delivery selection stages.
import { effectiveBatchSize, effectiveChallengeCap, effectiveValidationRetrySize } from './capacity.js';
import { modelFor, VERDICT_SCHEMA, VALIDATION_VERDICT_SCHEMA } from './registry.js';
import { checkRefutations } from './refutations.js';
import { verdictRejections } from './verdict.js';
import { applyValidations } from './applyValidations.js';
import { applyFilterPipeline } from './filterFindings.js';
import { applyChallenges } from './applyChallenges.js';
import { coerceReportFindings } from './renderReport.js';
import { groupDelivery, rankFindings } from './groupRanking.js';
import { deliveryGroups } from './dispose.js';
import { challengeEligible } from './admission.js';

// Both independent readers answer the same closed questions.
const VALIDATE_SCHEMA = { type: 'object', properties: { validations: { type: 'array',
  items: VALIDATION_VERDICT_SCHEMA,
} }, required: ['validations'], additionalProperties: false };

const callValidator = defineAgentCall('validate', () => VALIDATE_SCHEMA);

// Keep exact small returns and explicit truncation receipts without consuming the checkpoint budget.
function validatorAudit(audit, label, result, rejections) {
  const raw = JSON.stringify(result ?? null);
  const record = { label, raw: raw.slice(0, 8192), raw_characters: raw.length, truncated: raw.length > 8192,
    rejections: rejections.slice(0, 100), omitted_rejections: Math.max(0, rejections.length - 100) };
  if (audit.batches.length < 64 && JSON.stringify(audit).length + JSON.stringify(record).length < 64000) audit.batches.push(record);
  else audit.omitted_batches += 1;
}

// Parallel results retain batch positions so gaps identify the exact findings left unvalidated.
// Missing answers go to the independent challenger because a failed dispatch supplies no counter-evidence.
export async function validateStage(ctx, input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const limits = inp.limits || {};
  const policy = inp.policy || {};
  const batchSize = effectiveBatchSize(limits, findings.length);

  if (findings.length === 0) {
    return { findings: [], gaps: [], stats: { batches_dispatched: 0, batches_completed: 0, validated: 0, skipped: 0, adjusted: 0 } };
  }

  const model = modelFor('code-gauntlet:validator', policy);

  const batches = [];
  for (let i = 0; i < findings.length; i += batchSize) batches.push(findings.slice(i, i + batchSize));

  const gaps = [];
  const accepted = new Map();
  const audit = { batches: [], omitted_batches: 0 };
  const terminalRejections = new Map();
  let completed = 0;
  let dispatched = 0;
  const dispatch = (batch, label) => () => callValidator(ctx, validatePrompt(inp, batch), {
    label, agentType: 'code-gauntlet:validator', model,
  });
  const consume = (res, batch, label) => {
    const list = res === null || res === undefined
      ? null
      : (Array.isArray(res) ? res : (Array.isArray(res.validations) ? res.validations : null));
    if (list) completed += 1;
    const entries = (list || []).filter((e) => e && typeof e === 'object').map((e) => ({ raw: e, id: e.finding_id }));
    const rejections = [];
    for (const finding of batch) {
      const matches = entries.filter((entry) => entry.id === finding.id);
      if (!matches.length) rejections.push({ id: String(finding.id).slice(0, 128), reasons: [list ? 'missing_entry' : 'malformed_return'] });
      for (const entry of matches) {
        const reasons = matches.length === 1 ? verdictRejections(entry.raw, VALIDATION_VERDICT_SCHEMA) : ['duplicate_entry'];
        if (reasons.length) rejections.push({ id: String(finding.id).slice(0, 128), reasons });
        else accepted.set(finding.id, { ...entry.raw, id: entry.id });
      }
    }
    validatorAudit(audit, label, res, rejections);
    for (const rejection of rejections) terminalRejections.set(rejection.id, rejection.reasons);
    return list;
  };
  const results = await ctx.parallel(batches.map((batch, idx) => dispatch(batch, `validate-batch-${idx}`)));
  dispatched += batches.length;
  const mainLists = batches.map((batch, idx) => consume(results[idx], batch, `validate-batch-${idx}`));
  const pending = findings.filter((finding) => !accepted.has(finding.id));
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
    const unanswered = batch.filter((finding) => !accepted.has(finding.id));
    if (unanswered.length) {
      const reasons = [...new Set(unanswered.flatMap((finding) => terminalRejections.get(String(finding.id).slice(0, 128)) || []))];
      let problem = mainLists[idx] ? 'omitted verdicts' : 'returned null/malformed';
      if (reasons.some((reason) => !['missing_entry', 'malformed_return'].includes(reason))) {
        problem = `returned invalid/incomplete entries (${reasons.join(', ')})`;
      }
      gaps.push(`validate-batch-${idx}: validator ${problem} — ${unanswered.length} finding(s) unvalidated (validation=skipped, sent to blind challenge)`);
    }
  });

  gaps.push(...await checkRefutations(ctx, inp, [...accepted.values()], 'validate-refutations'));
  const { adjustedCount } = applyValidations(findings, [...accepted.values()]);

  let skipped = 0;
  for (const f of findings) {
    if (!accepted.has(f.id)) { f.validation = 'skipped'; skipped += 1; }
    else f.validation = 'validated';
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
      adjusted: adjustedCount,
    },
  };
}

function validatePrompt(inp, batch) {
  const block = batch.map((f) => `- ${f.id} [${f.dimension || '?'}/${f.severity || '?'}] ${f.file || '?'}:${f.line_start ?? '?'}-${f.line_end ?? f.line_start ?? '?'} | location_status=${f.location_status ?? 'unconfirmed'} — ${f.description || ''}${f.evidence ? ` | evidence: ${f.evidence}` : ''}${f.resolved_sites?.length ? ` | site_requests: ${JSON.stringify(f.resolved_sites)}` : ''}`).join('\n');
  return `${inp.contextLine || ''}Return factual claim and consequence separately using the schema. Confirm additional sites only for the same correction.\nFindings:\n${block}`;
}

// The runtime has no wall clock, so generated_at must come from the supplied arguments.
export function filterStage(input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  return applyFilterPipeline(findings, inp.generatedAt);
}

const CHALLENGE_SCHEMA = { type: 'object', properties: { verdict: VERDICT_SCHEMA }, required: ['verdict'], additionalProperties: false };
const callChallenger = defineAgentCall('challenge', () => CHALLENGE_SCHEMA);

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

// Deleted locations need the saved diff because they cannot be inspected at HEAD.
function challengePrompt(finding, diffPath) {
  const b = blindChallengeFields(finding, diffPath);
  const range = b.location || (b.line_end !== '' && b.line_end !== b.line_start ? `${b.line_start}-${b.line_end}` : `${b.line_start}`);
  const removedContext = b.removed_test
    ? `\nRead saved diff ${b.diffPath}: deleted test ${JSON.stringify(b.removed_test.path)}, old lines ${b.removed_test.old_start}-${b.removed_test.old_end}. Compare its input and assertion with surviving tests at HEAD for the same behavior.` : '';
  return `${removedContext}\nClaim: ${b.title}\n${b.description}\nLocation to inspect: ${b.file}:${range}`;
}

// Parallel results retain candidate positions so failures stay attributable to individual findings.
// Only complete typed answers can grant delivery eligibility.
export async function challengeStage(ctx, input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const limits = inp.limits || {};
  const policy = inp.policy || {};
  const cap = effectiveChallengeCap(limits, findings.length);

  if (findings.length === 0) {
    return {
      findings: [], unverified: [], eliminated: [], gaps: [],
      stats: {
        total_input: 0, dispatched: 0, completed: 0, skipped: 0,
        challenge_removed: 0,
        challenge_survived: 0, unchallenged: 0, final_count: 0,
      },
      generated_at: inp.generatedAt,
    };
  }

  const model = modelFor('code-gauntlet:challenger', policy);

  const terminal = findings.filter((f) => !challengeEligible(f)).map((f) => ({ ...f, challenge: 'admission_terminal' }));
  const ranked = rankFindings(findings.filter(challengeEligible));
  const candidates = ranked.slice(0, cap);
  const overflow = ranked.slice(cap);

  const thunks = candidates.map((finding, idx) => () => callChallenger(ctx, challengePrompt(finding, inp.diffPath), {
    label: `challenge-${idx}`,
    agentType: 'code-gauntlet:challenger',
    model,
  }));

  const results = thunks.length ? await ctx.parallel(thunks) : [];

  const gaps = [];
  const challenged = [];
  const challenges = [];
  const skipped = [];

  results.forEach((result, idx) => {
    const res = result?.verdict;
    const finding = candidates[idx];
    if (verdictRejections(res).length) {
      gaps.push(`challenge-${idx}: challenger returned no typed verdict — finding ${finding.id} unchallenged`);
      skipped.push({ ...finding, challenge: 'failed' });
      return;
    }
    challenges.push({ ...res, id: finding.id });
    challenged.push({ ...finding, challenge: 'survived' });
  });

  for (const f of overflow) skipped.push({ ...f, challenge: 'capped' });
  if (overflow.length) {
    gaps.push(`challenge: ${overflow.length} finding(s) over challengeCap=${cap} left unchallenged (challenge=capped, pipeline-degraded)`);
  }

  gaps.push(...await checkRefutations(ctx, inp, challenges, 'challenge-refutations'));
  const applied = applyChallenges(challenged, challenges);
  const unresolved = applied.findings.filter((f) => f.verdict_route === 'not-established');
  skipped.push(...unresolved);
  applied.findings = applied.findings.filter((f) => f.verdict_route !== 'not-established');

  const unverified = rankFindings(skipped);

  return {
    findings: [...applied.findings, ...terminal],
    unverified,
    eliminated: applied.eliminated,
    gaps,
    stats: {
      total_input: findings.length,
      ...(terminal.length ? { admission_terminal: terminal.length } : {}),
      dispatched: candidates.length,
      completed: challenged.length,
      skipped: skipped.length,
      challenge_removed: applied.stats.challenge_removed,
      challenge_survived: applied.stats.challenge_survived,
      unchallenged: applied.stats.unchallenged,
      final_count: applied.findings.length + terminal.length,
    },
    generated_at: inp.generatedAt,
  };
}

// Delivery defaults to all survivors to avoid silently narrowing scope.
// Persisting the selected set lets delivery consume the policy decision without independently filtering or ranking it.
export function selectDelivery(survivors, cap, tier) {
  const pool = deliveryGroups(survivors || []).filter(({ primary, corroborators }) =>
    tier !== 'main_only' || groupDelivery(primary, corroborators).report_tag === 'main');
  const rankable = pool.map(({ primary, corroborators }) => groupDelivery(
    coerceReportFindings([primary])[0], coerceReportFindings(corroborators)));
  const originals = new Map(rankable.map((finding, index) => [finding, pool[index]]));
  let ranked = rankFindings(rankable).map((finding) => originals.get(finding));
  if (cap !== undefined && cap !== null) ranked = ranked.slice(0, Math.max(0, cap));
  return ranked.flatMap(({ primary, corroborators }) => [primary, ...corroborators]);
}

export function deliveryOmissions(survivors, selected, tier) {
  const posted = new Set(selected);
  return deliveryGroups(survivors).filter(({ primary }) => !posted.has(primary)).map(({ primary, corroborators }) => ({
    findings: [primary, ...corroborators],
    reason: tier === 'main_only' && groupDelivery(primary, corroborators).report_tag !== 'main' ? 'delivery_tier' : 'delivery_cap',
  }));
}
