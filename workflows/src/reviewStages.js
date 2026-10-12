// Validation, filtering, challenge, and delivery selection stages.
import { effectiveBatchSize, effectiveChallengeCap } from './capacity.js';
import { modelFor } from './registry.js';
import { INT_POLICY, coerceInt } from './wire.js';
import { applyValidations, REACHABILITY_VALUES } from './applyValidations.js';
import { applyFilterPipeline } from './filterFindings.js';
import { applyChallenges, rankFindings } from './applyChallenges.js';
import { coerceReportFindings } from './renderReport.js';

// --- Phase 5: Validate ------------------------------------------------------

// The validator independently re-scores a batch of findings, one entry per finding it
// chose to score (it may omit some, which then keep their original confidence). The
// entries are an array, but the DISPATCH schema must be OBJECT-rooted: the Messages API
// rejects an array-rooted tool input_schema with `tools.N.custom.input_schema.type:
// Input should be 'object'` (the 400 the live smoke run hit). So the array is wrapped in
// a { validations: [...] } object; validateStage unwraps `.validations` at the consumer.
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

// validateStage(ctx, input) -> { findings, gaps, stats }
// Batches findings into limits.validateBatch chunks and dispatches ONE validator per
// batch through parallel(). applyValidations merges the returned confidence
// adjustments into the findings IN PLACE (id match, [0,100] clamp, original_confidence
// captured once). parallel() nulls a failed member in place, so results are positionally
// aligned with `batches` — attribution is by INDEX (like discover), not .filter(Boolean),
// because a degraded batch must be traced back to the exact findings it left unvalidated.
// A null/malformed batch means its findings went UNVALIDATED: they are kept at face value
// (conservative — never dropped, confidence never touched) and marked validation='skipped'
// with a loud gap. Every surviving finding ends marked validation='validated' or 'skipped'.
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
      gaps.push(`validate-batch-${idx}: validator returned null/malformed — ${batch.length} finding(s) unvalidated (validation=skipped, kept conservatively)`);
      for (const f of batch) skippedSet.add(f);
      return;
    }
    completed += 1;
    // Field-name normalization (v2 SKILL parity): the validator agent's shipped
    // output format emits `finding_id` (agents/validator.md), but applyValidations
    // matches on `id`. Accept BOTH the .md-shaped (`finding_id`) and schema-shaped
    // (`id`) entries so a real validator dispatch actually merges — without this the
    // adjustments silently never match and every finding keeps its raw confidence.
    for (const e of list) {
      validations.push(e && typeof e === 'object' ? { ...e, id: e.id ?? e.finding_id } : e);
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
  // Each finding carries its location + claim so the validator can open the right code
  // (validator.md step 1: "Read the code at the file and line range specified"). Passing
  // only ids left validators unable to locate anything — they scored blind.
  const block = batch.map((f) => {
    const range = f.line_end != null && f.line_end !== f.line_start
      ? `${f.line_start}-${f.line_end}`
      : `${f.line_start != null ? f.line_start : '?'}`;
    const ev = f.evidence ? ` | evidence: ${f.evidence}` : '';
    return `- ${f.id} [${f.dimension || '?'}/${f.severity || '?'}] ${f.file || '?'}:${range} — ${f.description || ''}${ev}`;
  }).join('\n');
  return `${ctxLine}Independently validate this batch of findings. For each, Read the code at the file and line range shown, attempt to disprove the claim, and score it. Findings:\n${block}\nReturn { validations: [{ ${validateReturnDescriptor()} }] } — confidence 0-100 (one entry per finding you scored; omit the rest).`;
}

// --- Phase 6: Filter --------------------------------------------------------

// filterStage(input) -> applyFilterPipeline envelope. PURE and deterministic: no ctx,
// no agents (that is the whole point of the JS twin). `reviewConfig` contains the root
// REVIEW.md layer and optional subtree layers; threshold and ignore selection uses each
// finding.file. `exclusionPatterns` is the global parsed exclusions list. generatedAt is
// threaded from the args waist into the envelope's generated_at — never new Date().
export function filterStage(input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const config = inp.reviewConfig || {};
  const exclusionPatterns = inp.exclusionPatterns || [];
  return applyFilterPipeline(findings, config, exclusionPatterns, inp.generatedAt);
}

// --- Phase 7: Challenge -----------------------------------------------------

// The challenger agent (agents/challenger.md) emits
// { confidence_claim_is_correct, justification }; the stage reads that field (?? score)
// and injects the KNOWN finding id by index (never trusts the challenger to echo it).
const CHALLENGE_SCHEMA = {
  type: 'object',
  properties: {
    confidence_claim_is_correct: { type: 'number' },
    justification: { type: 'string' },
  },
  required: ['confidence_claim_is_correct'],
};

// blindChallengeFields(finding) -> { title, description, file, line_start, line_end }
// STRUCTURAL blindness guarantee: the blind challenger sees ONLY these keys — the claim
// (title/description) plus the LOCATION so it can open the raw code itself (challenger.md
// has Read/Grep/LSP and is told to read the code at the location). Selecting them
// explicitly (an allowlist, not a delete-list) means no confirming context — evidence,
// origin, cross_file_refs, corroborated_by, the original agent's reasoning — can ever
// reach the challenger, and stays impossible even if new reasoning-bearing fields are
// added to findings later. The prior `code` field was never populated anywhere in the
// pipeline, so the challenger always received an empty code block; location + the agent's
// own tools replaces that dead field. Unit-tested both ways: the returned object has
// exactly these keys and the built prompt leaks none of the rest.
export function blindChallengeFields(finding) {
  return {
    title: finding.title || '',
    description: finding.description || '',
    file: finding.file || '',
    line_start: finding.line_start != null ? finding.line_start : '',
    line_end: finding.line_end != null ? finding.line_end : '',
  };
}

// Hill-climb iter 5: teeth + unverifiable-claim gate. The challenger must VERIFY the
// claim's central factual assertion against the raw code, and score any claim it cannot
// confirm from the code+context at or below 25 (below 25 removes non-security findings
// downstream; see applyChallenges thresholds). This targets the two noise clusters the
// subset diagnosis surfaced: test-coverage "no test exists" negatives and
// cross_file_impact claims that cite no in-diff evidence. Still fully blind — only
// {title, description, file, line_start, line_end} reach the challenger.
function challengePrompt(finding) {
  const b = blindChallengeFields(finding);
  const range = b.line_end !== '' && b.line_end !== b.line_start ? `${b.line_start}-${b.line_end}` : `${b.line_start}`;
  return `You are a blind challenger. You have NOT seen the original reviewer's rationale — assess this claim on its own merits and try to disprove it. First VERIFY the claim's central factual assertion against the raw code: the claim concerns ${b.file}:${range} — open that location and enough surrounding context yourself (Read/Grep/LSP), find the specific lines the claim rests on, and confirm they actually say what the claim needs them to say. If that central assertion cannot be verified from the code and context — for example a test-coverage "no test exists" or missing-coverage claim you cannot confirm, or a cross-file-impact claim that cites no in-diff evidence — treat the claim as UNVERIFIABLE and score it 25 or below (below 25 when nothing in the code confirms it, so it does not survive). Reserve scores above 25 for claims whose central assertion you positively confirmed in the code.\nClaim: ${b.title}\n${b.description}\nLocation to inspect: ${b.file}:${range}\nReturn { confidence_claim_is_correct, justification }; confidence_claim_is_correct 0-100 (higher = the claim holds).`;
}

// challengeStage(ctx, input) -> { findings, unverified, eliminated, gaps, stats, generated_at }
// Ranks the incoming findings and blind-challenges the top min(n, limits.challengeCap)
// through parallel() — one challenger per finding, each fed ONLY blindChallengeFields.
// parallel() nulls a failed member in place, so results are positionally aligned with the
// candidate list (attribution by INDEX — a degraded member must be traced to its exact
// finding). A finding counts as CHALLENGED only when its member returned an int-coercible
// score; applyChallenges then applies the blind-score thresholds (remove / downgrade /
// contest / survive), re-runs cross-agent dedup, and ranks — that ranked set is the
// high-confidence bucket. Every UNCHALLENGED finding — cap overflow OR a null/unscored
// member — is marked challenge='skipped' and routed to `unverified` (the pipeline-degraded
// section); it NEVER enters the high-confidence bucket. Only genuinely-challenged findings
// flow into applyChallenges, so its `unchallenged` pass-through (which would land a finding
// in the high-confidence set) can never fire here. Records dispatched-vs-completed counts.
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
        challenge_survived: 0, unchallenged: 0, cross_agent_consolidated: 0, final_count: 0,
      },
      generated_at: inp.generatedAt,
    };
  }

  const model = modelFor('code-gauntlet:challenger', policy);

  // Rank first so the cap, when it bites, challenges the HIGHEST-priority findings;
  // the lower-ranked overflow is skipped (routed to `unverified`, never dropped).
  const ranked = rankFindings(findings);
  const candidates = ranked.slice(0, cap);
  const overflow = ranked.slice(cap);

  const thunks = candidates.map((finding, idx) => () => ctx.agent(challengePrompt(finding), {
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
    // Field-name normalization (v2 SKILL parity): the challenger agent's shipped
    // output format emits `confidence_claim_is_correct` (agents/challenger.md), not
    // `score`. Accept BOTH so a real challenger dispatch is scored — without this
    // every result reads unscored, every finding is skipped, and the high-confidence
    // bucket is ALWAYS empty. `??` (not `||`) so a legitimate 0 score is honoured.
    const rawScore = res && typeof res === 'object' ? (res.confidence_claim_is_correct ?? res.score) : undefined;
    if (res === null || res === undefined || coerceInt(rawScore, INT_POLICY.validation) === null) {
      gaps.push(`challenge-${idx}: challenger returned null/unscored — finding ${finding.id} unchallenged (challenge=skipped, pipeline-degraded)`);
      skipped.push(finding);
      return;
    }
    // Pair the score with the KNOWN finding id (never trust the challenger to echo it).
    challenges.push({ id: finding.id, score: rawScore, justification: res.justification });
    challenged.push(finding);
  });

  for (const f of overflow) skipped.push(f);
  if (overflow.length) {
    gaps.push(`challenge: ${overflow.length} finding(s) over challengeCap=${cap} left unchallenged (challenge=skipped, pipeline-degraded)`);
  }

  const applied = applyChallenges(challenged, challenges);

  // Mark + rank the degraded section. Shallow-clone so the caller's findings stay
  // untouched (applyChallenges likewise clones the survivors it mutates).
  const unverified = rankFindings(skipped.map((f) => ({ ...f, challenge: 'skipped' })));

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
      cross_agent_consolidated: applied.stats.cross_agent_consolidated,
      final_count: applied.stats.final_count,
    },
    generated_at: inp.generatedAt,
  };
}

// --- Phase 8: Delivery selection --------------------------------------------

// selectDelivery(survivors, cap, tier) -> rank-ordered top-cap delivery set.
// The deterministic Phase 8 delivery policy: the pipeline — not the live agent — decides
// what gets posted, honoring the user-chosen delivery TIER (resolved at Phase 1, threaded
// through args.delivery.tier):
//   - 'all' (the default — interactive Recommended, headless CODE_GAUNTLET_DELIVERY_TIER
//     default): every challenge-survivor is a delivery candidate regardless of its
//     report_tag (main AND suggestion alike);
//   - 'main_only': keep only main-tagged survivors (suggestions stay in the report but are
//     not posted as PR comments).
// Any tier value other than 'main_only' (including undefined/null) resolves to 'all', so the
// no-silent-narrowing default holds. The report_tag is set by tagFindings/applyChallenges
// (report_destination is the older alias); tagFindings itself is unchanged — the tag stays
// meaningful metadata that this selection reads, never mutates. rankFindings then orders the
// pool (severity, confidence, risk/description) and `cap` truncates: a null/undefined cap
// means "no cap", a numeric cap keeps the top-cap floored at 0 (mirrors challengeStage's
// Math.max(0, ...) idiom so a 0/negative cap yields an empty set rather than throwing). PURE
// — never mutates its input (rankFindings copies) — and exported so the live agent consumes
// the result verbatim and never re-filters or re-ranks. Challenge-removed / challenge-skipped
// findings are already absent from `survivors`, so they stay excluded exactly as before.
export function selectDelivery(survivors, cap, tier) {
  const pool = tier === 'main_only'
    ? (survivors || []).filter((f) => (f.report_tag ?? f.report_destination) === 'main')
    : (survivors || []);
  // Rank the renderer-owned tolerant projection, then map back to the original entries so
  // persistence keeps its byte/identity relationship with the challenge output.
  const rankable = coerceReportFindings(pool);
  const originalByRankable = new Map(rankable.map((finding, index) => [finding, pool[index]]));
  const ranked = rankFindings(rankable).map((finding) => originalByRankable.get(finding));
  if (cap === undefined || cap === null) return ranked;
  return ranked.slice(0, Math.max(0, cap));
}
