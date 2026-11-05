import { locationAllows } from './location.js';
// Validation, filtering, challenge, and delivery selection stages.
import { incompleteVetting } from './vetting.js';
import { effectiveBatchSize, effectiveChallengeCap } from './capacity.js';
import { modelFor, VALIDATION_RETURN_PROPERTIES } from './registry.js';
import { INT_POLICY, coerceInt } from './wire.js';
import { applyValidations, REACHABILITY_VALUES } from './applyValidations.js';
import { applyFilterPipeline } from './filterFindings.js';
import { applyChallenges, rankFindings } from './applyChallenges.js';
import { coerceReportFindings } from './renderReport.js';
import { deliveryGroups } from './dispose.js';

// The platform rejects array-rooted tool schemas, so validation entries need an object wrapper.
const VALIDATE_SCHEMA = {
  type: 'object',
  properties: {
    validations: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          finding_id: { type: 'string' },
          confidence: { type: 'number' },
          justification: { type: 'string' },
          reachability: { type: 'string', enum: REACHABILITY_VALUES },
          ...VALIDATION_RETURN_PROPERTIES,
        },
        required: ['finding_id', 'confidence'],
      },
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

// Parallel results retain batch positions so gaps identify the exact findings left unvalidated.
// Failed batches keep findings at their original confidence because dispatch failure supplies no evidence against them.
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

  const thunks = batches.map((batch, idx) => () => ctx.agent(validatePrompt(inp, batch), {
    label: `validate-batch-${idx}`,
    agentType: 'code-gauntlet:validator',
    model,
    schema: VALIDATE_SCHEMA,
  }));

  const results = await ctx.parallel(thunks);

  const gaps = [];
  const validations = [];
  const skippedSet = new Set();
  let completed = 0;

  results.forEach((res, idx) => {
    const batch = batches[idx];
    const list = res === null || res === undefined
      ? null
      : (Array.isArray(res) ? res : (Array.isArray(res.validations) ? res.validations : null));
    if (list === null) {
      gaps.push(`validate-batch-${idx}: validator returned null/malformed — ${batch.length} finding(s) unvalidated (validation=skipped, held back to the report)`);
      for (const f of batch) skippedSet.add(f);
      return;
    }
    completed += 1;
    const entries = list.filter((e) => e && typeof e === 'object')
      .map((e) => ({ ...e, id: e.id ?? e.finding_id }));
    validations.push(...entries);
    const scored = new Set(entries.filter((e) => coerceInt(e.confidence, INT_POLICY.validation) !== null).map((e) => e.id));
    const unscored = batch.filter((f) => !scored.has(f.id));
    if (unscored.length) {
      gaps.push(`validate-batch-${idx}: validator omitted scores — ${unscored.length} finding(s) unvalidated (validation=skipped, held back to the report)`);
      for (const f of unscored) skippedSet.add(f);
    }
  });

  const { adjustedCount } = applyValidations(findings, validations);

  let skipped = 0;
  for (const f of findings) {
    if (skippedSet.has(f)) { f.validation = 'skipped'; skipped += 1; }
    else f.validation = 'validated';
  }

  return {
    findings,
    gaps,
    stats: {
      batches_dispatched: batches.length,
      batches_completed: completed,
      validated: findings.length - skipped,
      skipped,
      adjusted: adjustedCount,
    },
  };
}

function validatePrompt(inp, batch) {
  const ctxLine = inp.contextLine || '';
  // Locations and claims let validators inspect the relevant code instead of scoring identifiers without evidence.
  const block = batch.map((f) => {
    const range = !locationAllows(f, 'sourceLink') ? 'location unconfirmed' : f.line_end != null && f.line_end !== f.line_start
      ? `${f.line_start}-${f.line_end}`
      : `${f.line_start != null ? f.line_start : '?'}`;
    const ev = f.evidence ? ` | evidence: ${f.evidence}` : '';
    const sites = (f.resolved_sites || []).filter((site) => locationAllows(site, 'sourceLink'));
    const requests = sites.length ? ` | site_requests: ${JSON.stringify(sites)}` : '';
    return `- ${f.id} [${f.dimension || '?'}/${f.severity || '?'}] ${f.file || '?'}:${range} — ${f.description || ''}${ev}${requests}`;
  }).join('\n');
  return `${ctxLine}Independently validate the supplied batch using the code-evidence and disproof contract. For each resolved site request, confirm whether it is the same defect requiring the same correction; return site_confirmations only for those requests. Findings:\n${block}\nReturn { validations: [{ ${validateReturnDescriptor()} }] } — confidence 0-100 (one entry per finding).`;
}

// The runtime has no wall clock, so generated_at must come from the supplied arguments.
export function filterStage(input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const config = inp.reviewConfig || {};
  const exclusionPatterns = inp.exclusionPatterns || [];
  return applyFilterPipeline(findings, config, exclusionPatterns, inp.generatedAt);
}

const CHALLENGE_SCHEMA = {
  type: 'object',
  properties: {
    confidence_claim_is_correct: { type: 'number' },
    justification: { type: 'string' },
  },
  required: ['confidence_claim_is_correct'],
};

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

// Conservative scores prevent unverifiable absence claims from becoming high-confidence findings.
function challengePrompt(finding, diffPath) {
  const b = blindChallengeFields(finding, diffPath);
  const range = b.location || (b.line_end !== '' && b.line_end !== b.line_start ? `${b.line_start}-${b.line_end}` : `${b.line_start}`);
  const removedContext = b.removed_test
    ? `\nRead saved diff ${b.diffPath}: deleted test ${JSON.stringify(b.removed_test.path)}, old lines ${b.removed_test.old_start}-${b.removed_test.old_end}. Check the removed input and assertion, then search surviving tests at HEAD for the same behavior. This coverage-loss claim is verifiable from the saved diff and surviving tests.` : '';
  return `Independently check the claim's central facts against raw code at ${b.file}:${range}, opening enough surrounding context with Read/Grep/LSP to prove or disprove the actual claim. Runtime failures require a current trigger; static defects require evidence of the static claim. Do not lower the score merely because upstream evidence is withheld. If the central assertion remains unverifiable from code and context, score it 25 or below (below 25 when nothing in code confirms it). Reserve scores above 25 for positively confirmed claims. A removed-test claim can be verified from its deleted location in the saved diff and surviving assertions at HEAD.${removedContext}\nClaim: ${b.title}\n${b.description}\nLocation to inspect: ${b.file}:${range}\nReturn { confidence_claim_is_correct, justification }; confidence_claim_is_correct 0-100 (higher = the claim holds).`;
}

// Parallel results retain candidate positions so failures stay attributable to individual findings.
// Only scored findings enter applyChallenges; its unchallenged pass-through would otherwise admit skipped findings into the high-confidence set.
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
        challenge_removed: 0, challenge_downgraded: 0, challenge_contested: 0,
        challenge_survived: 0, unchallenged: 0, final_count: 0,
      },
      generated_at: inp.generatedAt,
    };
  }

  const model = modelFor('code-gauntlet:challenger', policy);

  // Reserve scarce challenge capacity for findings that can still be delivered.
  const ranked = [
    ...rankFindings(findings.filter((f) => incompleteVetting(f).length === 0)),
    ...rankFindings(findings.filter((f) => incompleteVetting(f).length > 0)),
  ];
  const candidates = ranked.slice(0, cap);
  const overflow = ranked.slice(cap);

  const thunks = candidates.map((finding, idx) => () => ctx.agent(challengePrompt(finding, inp.diffPath), {
    label: `challenge-${idx}`,
    agentType: 'code-gauntlet:challenger',
    model,
    schema: CHALLENGE_SCHEMA,
  }));

  const results = thunks.length ? await ctx.parallel(thunks) : [];

  const gaps = [];
  const challenged = [];
  const challenges = [];
  const skipped = [];

  results.forEach((res, idx) => {
    const finding = candidates[idx];
    // The schema field is confidence_claim_is_correct; score is accepted as a legacy alias.
    // Nullish fallback preserves a legitimate zero score.
    const rawScore = res && typeof res === 'object' ? (res.confidence_claim_is_correct ?? res.score) : undefined;
    if (res === null || res === undefined || coerceInt(rawScore, INT_POLICY.validation) === null) {
      gaps.push(`challenge-${idx}: challenger returned null/unscored — finding ${finding.id} unchallenged (challenge=failed, pipeline-degraded)`);
      skipped.push({ ...finding, challenge: 'failed' });
      return;
    }
    // Use the known finding id because challenger output cannot be trusted to echo it.
    challenges.push({ id: finding.id, score: rawScore, justification: res.justification });
    challenged.push(finding);
  });

  for (const f of overflow) skipped.push({ ...f, challenge: 'capped' });
  if (overflow.length) {
    gaps.push(`challenge: ${overflow.length} finding(s) over challengeCap=${cap} left unchallenged (challenge=capped, pipeline-degraded)`);
  }

  const applied = applyChallenges(challenged, challenges);

  const unverified = rankFindings(skipped);

  return {
    findings: applied.findings,
    unverified,
    eliminated: applied.eliminated,
    gaps,
    stats: {
      total_input: findings.length,
      dispatched: candidates.length,
      completed: challenged.length,
      skipped: skipped.length,
      challenge_removed: applied.stats.challenge_removed,
      challenge_downgraded: applied.stats.challenge_downgraded,
      challenge_contested: applied.stats.challenge_contested,
      challenge_survived: applied.stats.challenge_survived,
      unchallenged: applied.stats.unchallenged,
      final_count: applied.stats.final_count,
    },
    generated_at: inp.generatedAt,
  };
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
