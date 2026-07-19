// stages.js — orchestration stage functions for the deep-review v3 pipeline,
// phases 1-3 (Summarize -> Discover -> Merge) plus the agent-count coarsening
// formula that keeps the whole run's worst-case fan-out under the platform guard.
//
// Every stage takes an injected `ctx` ({ agent, parallel }) so unit tests can drive
// it with a mock (the runtime globals do not exist under node:test). Defaults fall
// back to the runtime globals when present, so the shipped bundle needs no wiring.
//
// Failure contract (Phase 0): bare agent() THROWS on schema-retry-exhaustion (cap 5)
// and unknown agentType; parallel() converts a failed member to null. So: single
// agent() calls are wrapped in try/catch; parallel() results are always .filter(Boolean)ed
// and a null member is recorded as a gap. No wall-clock, no import at runtime.
import { DIMENSIONS, AGENTS, resolvePolicy } from './registry.js';
import { merge } from './mergeFindings.js';
import { applyValidations, pyIntStrict } from './applyValidations.js';
import { applyFilterPipeline } from './filterFindings.js';
import { applyChallenges, rankFindings } from './applyChallenges.js';
import { normalizeArgs, validateArgs } from './args.js';

// Runtime globals are injected by the workflow host; under node:test they are absent,
// so ctx must be supplied. defaultCtx lets the shipped pipeline call stages without wiring.
function defaultCtx() {
  return {
    agent: typeof agent === 'function' ? agent : undefined,
    parallel: typeof parallel === 'function' ? parallel : undefined,
    pipeline: typeof pipeline === 'function' ? pipeline : undefined,
  };
}

// --- Phase 1: Summarize -----------------------------------------------------

// summarize(ctx, input) -> { summary, gaps }
// Small PRs: one change-summarizer agent() call. Large PRs (>500 changed lines that
// also span more files than one bucket): fan out per-file buckets of
// limits.summarizeBucketSize through parallel(), then a single merge agent() call to
// stitch the partials. Any throw / total null-out degrades to { summary:'', gaps:[...] }.
export async function summarize(ctx, input) {
  const c = ctx || defaultCtx();
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const changedFiles = inp.changedFiles || [];
  const changedLines = inp.changedLines || 0;
  const limits = inp.limits || {};
  const policy = inp.policy || {};
  const bucketSize = Math.max(1, limits.summarizeBucketSize || 20);
  const model = resolvePolicy('deep-review:change-summarizer', {
    frontier: policy.frontier,
    frontierModelId: policy.frontierModelId,
    subagentModelEnv: policy.subagentModel,
  }).model;

  const bucketed = changedLines > 500 && changedFiles.length > bucketSize;
  try {
    if (bucketed) {
      const buckets = [];
      for (let i = 0; i < changedFiles.length; i += bucketSize) buckets.push(changedFiles.slice(i, i + bucketSize));
      const tasks = buckets.map((files, idx) => ({
        label: `summarize-bucket-${idx}`,
        agentType: 'deep-review:change-summarizer',
        model,
        contextPath: inp.contextPath,
        prompt: summarizePrompt(inp, files),
      }));
      const partials = (await c.parallel(tasks)).filter(Boolean);
      if (partials.length === 0) return { summary: '', gaps: ['summarize failed'] };
      const mergeResult = await c.agent({
        label: 'summarize-merge',
        agentType: 'deep-review:change-summarizer',
        model,
        contextPath: inp.contextPath,
        prompt: summarizeMergePrompt(partials),
      });
      if (!mergeResult) return { summary: '', gaps: ['summarize failed'] };
      return { summary: mergeResult.summary || '', gaps: [] };
    }
    const result = await c.agent({
      label: 'summarize',
      agentType: 'deep-review:change-summarizer',
      model,
      contextPath: inp.contextPath,
      prompt: summarizePrompt(inp, changedFiles),
    });
    if (!result) return { summary: '', gaps: ['summarize failed'] };
    return { summary: result.summary || '', gaps: [] };
  } catch (e) {
    return { summary: '', gaps: ['summarize failed'] };
  }
}

function summarizePrompt(inp, files) {
  const ctxLine = inp.contextPath ? `Read the shared context at ${inp.contextPath}. ` : '';
  return `${ctxLine}Summarize the semantic intent of these changed files for downstream reviewers: ${files.join(', ')}. Return { summary }.`;
}

function summarizeMergePrompt(partials) {
  const joined = partials.map((p) => p.summary || '').filter(Boolean).join('\n---\n');
  return `Combine these per-bucket change summaries into one concise semantic summary. Partials:\n${joined}\nReturn { summary }.`;
}

// --- Phase 2: Discover ------------------------------------------------------

// Group DIMENSIONS by agentType, unioning each agent's per-dimension schemaExtra into
// one finding schema. One task per unique AGENT (7) — agents covering several
// dimensions (conventions-and-intent -> convention/intent/comment_accuracy) dispatch once.
function agentSpecs() {
  const byAgent = new Map();
  for (const d of DIMENSIONS) {
    if (!byAgent.has(d.agentType)) byAgent.set(d.agentType, { agentType: d.agentType, dimensions: [], schemaExtra: {}, conditionalFlags: [] });
    const spec = byAgent.get(d.agentType);
    spec.dimensions.push(d.dimension);
    Object.assign(spec.schemaExtra, d.schemaExtra || {});
    spec.conditionalFlags.push(d.conditionalFlag);
  }
  // Preserve AGENTS order (derived from DIMENSIONS) so dispatch order is deterministic.
  return AGENTS.map((a) => byAgent.get(a));
}

// An agent is active when at least one of its dimensions is enabled: the dimension's
// conditionalFlag is null (always on) or the corresponding agentFlags entry is truthy.
function agentActive(spec, agentFlags) {
  return spec.conditionalFlags.some((flag) => flag === null || flag === undefined || agentFlags[flag]);
}

// Canonical finding schema (per-dimension schemaExtra unioned on top), wrapped in the
// per-agent result envelope { findings, complete, total_seen }.
function findingSchema(spec) {
  return {
    type: 'object',
    findings: {
      id: 'string', file: 'string', line_start: 'number', line_end: 'number',
      title: 'string', description: 'string', severity: 'string', confidence: 'string',
      dimension: 'string', origin: 'string', evidence: 'string', cross_file_refs: 'array',
      ...spec.schemaExtra,
    },
    complete: 'boolean',
    total_seen: 'number',
  };
}

// discover(ctx, input) -> { findings, gaps, degraded }
// One parallel() call fanning out to every active AGENT. A null member -> gap AND
// every dimension that agent covers is marked degraded: a null means the agent
// terminally failed after the platform's schema retries (cap 5), so those dimensions
// are entirely uncovered — the failure IS the degradation. (Each dimension maps to
// exactly one agent, so a per-dimension failure COUNTER could never cross a >1
// threshold within a single dispatch; degradation is therefore recorded on the first
// failure, not counted toward a limit.) A malformed result (no findings array) is
// treated the same. A non-null result reporting complete=false or total_seen at/over
// an optional discoveryCap -> "possibly incomplete" gap (soft: its findings are still
// collected, dimension not degraded).
export async function discover(ctx, input) {
  const c = ctx || defaultCtx();
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const agentFlags = inp.agentFlags || {};
  const limits = inp.limits || {};
  const policy = inp.policy || {};
  const discoveryCap = limits.discoveryCap; // optional per-agent finding ceiling

  const specs = agentSpecs().filter((spec) => agentActive(spec, agentFlags));
  const tasks = specs.map((spec) => {
    const model = resolvePolicy(spec.agentType, {
      frontier: policy.frontier,
      frontierModelId: policy.frontierModelId,
      subagentModelEnv: policy.subagentModel,
    }).model;
    return {
      label: spec.agentType, // label IS the agentType (identity for parallel + gaps)
      agentType: spec.agentType,
      model,
      dimensions: spec.dimensions,
      schema: findingSchema(spec),
      contextPath: inp.contextPath,
      prompt: discoverPrompt(inp, spec),
    };
  });

  const results = await c.parallel(tasks);

  const gaps = [];
  const findings = [];
  const degradedDims = [];

  // parallel() resolves a failed member to null IN PLACE (Phase 0 verified): the
  // results array is positionally aligned with `tasks`, so results[i] pairs with specs[i].
  results.forEach((res, i) => {
    const spec = specs[i];
    if (res === null || res === undefined) {
      gaps.push(`${spec.agentType}: agent returned null (dispatch failed) — dimensions ${spec.dimensions.join('/')} not covered`);
      degradedDims.push(...spec.dimensions); // terminal agent failure -> its dimensions degraded
      return;
    }
    const list = Array.isArray(res.findings) ? res.findings : null;
    if (list === null) {
      // Malformed result (no findings array) — no usable coverage, so degrade like a null.
      gaps.push(`${spec.agentType}: malformed result (no findings array)`);
      degradedDims.push(...spec.dimensions);
      return;
    }
    for (const f of list) {
      f.agent = spec.agentType; // orchestrator-injected; mergeStage regroups on this
      findings.push(f);
    }
    const nearCap = discoveryCap != null && (res.total_seen >= discoveryCap || list.length >= discoveryCap);
    if (res.complete === false || nearCap) {
      gaps.push(`${spec.agentType}: possibly incomplete (complete=${res.complete === false ? 'false' : 'true'}, total_seen=${res.total_seen}) — dimensions ${spec.dimensions.join('/')}`);
    }
  });

  // Each dimension belongs to a single agent so no overlap is possible today; the Set
  // keeps degraded deduplicated and insertion-ordered should that ever change.
  return { findings, gaps, degraded: [...new Set(degradedDims)] };
}

function discoverPrompt(inp, spec) {
  const ctxLine = inp.contextPath ? `Read the shared context at ${inp.contextPath}. ` : '';
  return `${ctxLine}Review the changes for the following dimension(s): ${spec.dimensions.join(', ')}. Return { findings, complete, total_seen } where each finding matches the canonical schema.`;
}

// --- Phase 3: Merge ---------------------------------------------------------

// mergeStage(discoverOut, meta) -> envelope
// Consumes merge() from mergeFindings.js as-is. Since merge() ingests raw per-agent
// NDJSON strings, regroup the discovered findings by their injected `agent` field and
// re-serialize each group to one JSON object per line. No text-fallback channel exists
// in v3 (parallel() returns structured findings), so textContents is empty.
export function mergeStage(discoverOut, meta) {
  const out = discoverOut || { findings: [] };
  const M = typeof meta === 'string' ? JSON.parse(meta) : (meta || {});
  const findings = out.findings || [];

  const byAgent = {};
  for (const f of findings) {
    const a = f.agent || 'unknown';
    (byAgent[a] = byAgent[a] || []).push(f);
  }

  const ndjsonContents = {};
  for (const [a, group] of Object.entries(byAgent)) {
    ndjsonContents[a] = group.map((f) => JSON.stringify(f)).join('\n');
  }

  // agents drives merge()'s per-agent iteration; use the agents that actually produced
  // findings, falling back to the full roster so an empty run still yields an envelope.
  const agents = Object.keys(ndjsonContents).length ? Object.keys(ndjsonContents) : AGENTS.slice();
  return merge(ndjsonContents, {}, { ...M, agents });
}

// --- Phase 4: Verify --------------------------------------------------------

// The discriminated-union envelope the executor returns. Both shapes coexist so an
// honest failure is schema-valid — the executor never fabricates a success under
// StructuredOutput retry pressure ({status:'failed'} is a legal answer).
const VERIFY_SCHEMA = {
  type: 'object',
  status: 'string', // 'ok' | 'failed'
  receipt: { sha: 'string', n_in: 'number', nonce: 'string' },
  result: { verified: 'array', eliminated: 'array', batches: 'array', stats: 'object' },
  exitCode: 'number',
  stderr: 'string',
};

// verifyStage(ctx, input) -> { findings, verified: boolean, gaps }
// Slices findings into limits.verifySliceSize chunks and dispatches ONE `executor`
// agent per slice, SEQUENTIALLY (not parallel()) so each envelope pairs to its slice by
// order. Each executor runs the pinned verify_findings.py receipt command and returns
// VERIFY_SCHEMA. A slice is TRUSTED only when status==='ok' AND the receipt echoes the
// dispatched nonce, head sha, and slice length (n_in — the truncation guard: proof the
// script saw every finding we sent). ANY untrusted slice — receipt mismatch,
// status:'failed', or an agent() throw — degrades the WHOLE set to the UNVERIFIED path:
// every ORIGINAL finding re-emitted with origin='unknown' (surfaced-classification
// skipped), a loud gap, verified=false. Findings are never dropped, success never faked.
export async function verifyStage(ctx, input) {
  const c = ctx || defaultCtx();
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const limits = inp.limits || {};
  const policy = inp.policy || {};
  const nonce = inp.nonce;
  const headShaShort = inp.headShaShort;
  const sliceSize = Math.max(1, limits.verifySliceSize || findings.length || 1);

  // Empty set: nothing to verify, trivially trusted (no executor dispatched).
  if (findings.length === 0) return { findings: [], verified: true, gaps: [] };

  const model = resolvePolicy('deep-review:executor', {
    frontier: policy.frontier,
    frontierModelId: policy.frontierModelId,
    subagentModelEnv: policy.subagentModel,
  }).model;

  const slices = [];
  for (let i = 0; i < findings.length; i += sliceSize) slices.push(findings.slice(i, i + sliceSize));

  const verifiedOut = [];
  let failureReason = null;

  for (let i = 0; i < slices.length && failureReason === null; i += 1) {
    const slice = slices[i];
    // Per-slice nonce: derive `${nonce}.${i}` so two equal-length slices can never
    // satisfy each other's receipt (a slice-confusion / replay defense on top of the
    // base nonce). The base nonce is charset-validated at the args waist (args.js).
    const sliceNonce = `${nonce}.${i}`;
    let env;
    try {
      env = await c.agent({
        label: `verify-slice-${i}`,
        agentType: 'deep-review:executor',
        model,
        command: verifyCommand(inp, i),
        prompt: verifyPrompt(inp, i),
        schema: VERIFY_SCHEMA,
      });
    } catch (e) {
      failureReason = `executor threw on slice ${i} (${(e && e.message) || 'unknown'})`;
      break;
    }
    const trust = trustSlice(env, { nonce: sliceNonce, headShaShort, n: slice.length });
    if (!trust.ok) {
      failureReason = `slice ${i}: ${trust.reason}`;
      break;
    }
    // Trusted: collect the enriched verified findings (origin new/surfaced set by Python;
    // cross_file_refs preserved verbatim for downstream surfaced-classification).
    const verified = env.result && Array.isArray(env.result.verified) ? env.result.verified : [];
    verifiedOut.push(...verified);
  }

  if (failureReason !== null) {
    const unknown = findings.map((f) => ({ ...f, origin: 'unknown' }));
    return {
      findings: unknown,
      verified: false,
      gaps: [`verify: UNVERIFIED — ${failureReason}; all ${findings.length} finding(s) marked origin=unknown, surfaced-classification skipped`],
    };
  }

  return { findings: verifiedOut, verified: true, gaps: [] };
}

// A slice envelope is trusted only if it is the honest success shape AND its receipt
// echoes exactly what we dispatched: the nonce (this answer is for OUR call), the head
// sha (same tree the workflow resolved), and n_in (the executor loaded every finding we
// sent). One more guard beyond the receipt: the result arrays must actually ACCOUNT for
// n_in findings (verified + eliminated === n_in — an invariant run_verification always
// satisfies), so a receipt that survives transport while its result body is truncated
// cannot silently drop findings.
//
// Threat model: this defends against a STALE, HALLUCINATING, or CONFUSED executor
// (an old/wrong result, a fabricated success, or another slice's answer) — NOT a
// Byzantine one. The nonce is argv-visible by construction, so a malicious executor
// could always echo it back; this is a consistency/liveness check, not authentication.
function trustSlice(env, { nonce, headShaShort, n }) {
  if (!env || typeof env !== 'object') return { ok: false, reason: 'executor returned no envelope' };
  if (env.status !== 'ok') return { ok: false, reason: `status=${env.status == null ? 'missing' : env.status}${env.stderr ? ` (${env.stderr})` : ''}` };
  const r = env.receipt || {};
  if (r.nonce !== nonce) return { ok: false, reason: `receipt nonce mismatch (got ${r.nonce == null ? 'missing' : r.nonce}, expected ${nonce})` };
  if (r.sha !== headShaShort) return { ok: false, reason: `receipt sha mismatch (got ${r.sha == null ? 'missing' : r.sha})` };
  if (r.n_in !== n) return { ok: false, reason: `receipt n_in mismatch (got ${r.n_in == null ? 'missing' : r.n_in}, expected ${n})` };
  const result = env.result || {};
  if (!Array.isArray(result.verified) || !Array.isArray(result.eliminated)) {
    return { ok: false, reason: 'result missing verified/eliminated arrays' };
  }
  const accounted = result.verified.length + result.eliminated.length;
  if (accounted !== r.n_in) {
    return { ok: false, reason: `result incomplete: verified+eliminated=${accounted} != n_in=${r.n_in} (transport truncation)` };
  }
  return { ok: true };
}

// The pinned command: a single `python3 <script> --flags...` invocation of plain word
// tokens only (CLAUDE.md AST-safe emission — no command substitution, heredocs, env
// prefix, or shell operators). Per-slice input/output paths are sha-scoped and index-
// suffixed; the skill layer writes the slice inputs, the executor reads the slice output.
function verifyCommand(inp, i) {
  const v = inp.verify || {};
  const inPath = `${v.inputPathBase || 'phase4-input'}.slice${i}.json`;
  const outPath = `${v.outputPathBase || 'phase4-output'}.slice${i}.json`;
  const parts = [
    'python3', v.scriptPath || 'scripts/verify_findings.py',
    '--input', inPath,
    '--output', outPath,
    '--nonce', `${inp.nonce}.${i}`, // per-slice derived nonce (matches trustSlice)
    '--head-sha', inp.headShaShort,
    '--base-branch', v.baseBranch || 'main',
  ];
  if (v.diffPath) parts.push('--diff-file', v.diffPath);
  return parts.join(' ');
}

function verifyPrompt(inp, i) {
  return `Run exactly this command, then read the --output file and return its contents verbatim via the schema:\n${verifyCommand(inp, i)}`;
}

// --- Agent-count coarsening -------------------------------------------------

const AGENT_COUNT_GUARD = 900;   // stay strictly under the platform fan-out ceiling
const SUMMARIZE_TERM_BOUND = 300; // widen the summarize bucket once its term alone exceeds this
const CHALLENGE_CAP_FLOOR = 5;    // never challenge fewer than this many findings

const ceilDiv = (n, d) => Math.ceil(Math.max(0, n) / Math.max(1, d));

// worstCaseAgentCount(limits, nFiles, nFindings) -> number
// summarize buckets (+1 merge) + the 7 discovery agents + verify slices + validate
// batches + min(nFindings, challengeCap) challengers + 2 (report + writer).
export function worstCaseAgentCount(limits, nFiles, nFindings) {
  const L = limits || {};
  const files = Math.max(0, nFiles || 0);
  const findings = Math.max(0, nFindings || 0);
  const summarizeCalls = ceilDiv(files, L.summarizeBucketSize) + 1;
  const verifyCalls = ceilDiv(findings, L.verifySliceSize);
  const validateCalls = ceilDiv(findings, L.validateBatch);
  const challengeCalls = Math.min(findings, Math.max(0, L.challengeCap || 0));
  return summarizeCalls + AGENTS.length + verifyCalls + validateCalls + challengeCalls + 2;
}

// coarsenLimits(limits, nFiles, nFindings) -> limits
// Iteratively pulls the worst-case count below the guard. Each iteration strictly
// decreases the count:
//   - When the summarize term alone exceeds SUMMARIZE_TERM_BOUND, widen the bucket
//     (doubling). Without this, a pathological changed-file count (~>17k) keeps the
//     summarize term above the guard and no validate/challenge coarsening can converge.
//   - Otherwise reduce whichever of {verify, validate, challenge} is currently largest:
//     RAISE verifySliceSize / validateBatch (fewer batches, since ceil(n/x) shrinks) or
//     LOWER challengeCap (the challenge term is min(n, cap), so a SMALLER cap lowers the
//     count — raising it is the inversion trap).
// summarizeBucketSize / validateBatch / verifySliceSize rise monotonically while
// challengeCap falls to CHALLENGE_CAP_FLOOR, so the chosen term is always reducible
// whenever the count is still >= guard, guaranteeing termination.
export function coarsenLimits(limits, nFiles, nFindings) {
  const L = { ...(limits || {}) };
  const files = Math.max(0, nFiles || 0);
  const findings = Math.max(0, nFindings || 0);

  while (worstCaseAgentCount(L, files, findings) >= AGENT_COUNT_GUARD) {
    const summarizeTerm = ceilDiv(files, L.summarizeBucketSize) + 1;
    if (summarizeTerm > SUMMARIZE_TERM_BOUND) {
      L.summarizeBucketSize = Math.max(1, L.summarizeBucketSize || 1) * 2;
      continue;
    }
    const verifyTerm = ceilDiv(findings, L.verifySliceSize);
    const validateTerm = ceilDiv(findings, L.validateBatch);
    const challengeTerm = Math.min(findings, Math.max(0, L.challengeCap || 0));
    if (validateTerm >= verifyTerm && validateTerm >= challengeTerm) {
      L.validateBatch = Math.max(1, L.validateBatch || 1) * 2;
    } else if (verifyTerm >= validateTerm && verifyTerm >= challengeTerm) {
      L.verifySliceSize = Math.max(1, L.verifySliceSize || 1) * 2;
    } else {
      // Halve the EFFECTIVE cap (min(cap, findings)) so C strictly decreases even when
      // the nominal cap already exceeds nFindings.
      L.challengeCap = Math.max(CHALLENGE_CAP_FLOOR, Math.floor(Math.min(L.challengeCap || 0, findings) / 2));
    }
  }
  return L;
}

// --- Phase 5: Validate ------------------------------------------------------

// The validator independently re-scores a batch of findings; the documented schema
// is a bare array [{id, confidence, justification}] (one entry per finding it chose
// to score — it may omit some, which then keep their original confidence).
const VALIDATE_SCHEMA = { type: 'array', items: { id: 'string', confidence: 'number', justification: 'string' } };

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
  const c = ctx || defaultCtx();
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const limits = inp.limits || {};
  const policy = inp.policy || {};
  const batchSize = Math.max(1, limits.validateBatch || findings.length || 1);

  if (findings.length === 0) {
    return { findings: [], gaps: [], stats: { batches_dispatched: 0, batches_completed: 0, validated: 0, skipped: 0, adjusted: 0 } };
  }

  const model = resolvePolicy('deep-review:validator', {
    frontier: policy.frontier,
    frontierModelId: policy.frontierModelId,
    subagentModelEnv: policy.subagentModel,
  }).model;

  const batches = [];
  for (let i = 0; i < findings.length; i += batchSize) batches.push(findings.slice(i, i + batchSize));

  const tasks = batches.map((batch, idx) => ({
    label: `validate-batch-${idx}`,
    agentType: 'deep-review:validator',
    model,
    contextPath: inp.contextPath,
    schema: VALIDATE_SCHEMA,
    prompt: validatePrompt(inp, batch),
  }));

  const results = await c.parallel(tasks);

  const gaps = [];
  const validations = [];
  const skippedSet = new Set(); // finding REFERENCES (immune to a missing/duplicate id)
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
  const ctxLine = inp.contextPath ? `Read the shared context at ${inp.contextPath}. ` : '';
  const ids = batch.map((f) => f.id).join(', ');
  return `${ctxLine}Independently validate this batch of findings (ids: ${ids}). Attempt to disprove each and return the array [{ id, confidence, justification }] — confidence 0-100.`;
}

// --- Phase 6: Filter --------------------------------------------------------

// filterStage(input) -> applyFilterPipeline envelope. PURE and deterministic: no ctx,
// no agents (that is the whole point of the JS twin). `reviewConfig` is the parsed
// REVIEW.md object (thresholds + ignore list) and `exclusionPatterns` the parsed
// exclusions list, both prepared upstream (parseReviewMd / loadExclusions). generatedAt
// is threaded from the args waist into the envelope's generated_at — never new Date().
export function filterStage(input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const config = inp.reviewConfig || {};
  const exclusionPatterns = inp.exclusionPatterns || [];
  return applyFilterPipeline(findings, config, exclusionPatterns, inp.generatedAt);
}

// --- Phase 7: Challenge -----------------------------------------------------

const CHALLENGE_SCHEMA = { type: 'object', id: 'string', score: 'number', justification: 'string' };

// blindChallengeFields(finding) -> { title, description, code }
// STRUCTURAL blindness guarantee: the blind challenger sees ONLY these three keys.
// Selecting them explicitly (an allowlist, not a delete-list) means no confirming
// context — evidence, origin, cross_file_refs, corroborated_by, the original agent's
// reasoning — can ever reach the challenger, and stays impossible even if new
// reasoning-bearing fields are added to findings later. Unit-tested both ways: the
// returned object has exactly these keys and the built prompt leaks none of the rest.
export function blindChallengeFields(finding) {
  return {
    title: finding.title || '',
    description: finding.description || '',
    code: finding.code || '',
  };
}

function challengePrompt(finding) {
  const b = blindChallengeFields(finding);
  return `You are a blind challenger. You have NOT seen the original reviewer's rationale — assess this claim on its own merits and try to disprove it.\nClaim: ${b.title}\n${b.description}\nRaw code under review:\n${b.code}\nReturn { id, score, justification }; score 0-100 (higher = the claim holds).`;
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
  const c = ctx || defaultCtx();
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const findings = inp.findings || [];
  const limits = inp.limits || {};
  const policy = inp.policy || {};
  const cap = Math.max(0, limits.challengeCap != null ? limits.challengeCap : findings.length);

  if (findings.length === 0) {
    return {
      findings: [], unverified: [], eliminated: [], gaps: [],
      stats: {
        total_input: 0, dispatched: 0, completed: 0, skipped: 0,
        challenge_removed: 0, challenge_downgraded: 0, challenge_contested: 0,
        challenge_survived: 0, unchallenged: 0, dedup_dropped: 0, final_count: 0,
      },
      generated_at: inp.generatedAt,
    };
  }

  const model = resolvePolicy('deep-review:challenger', {
    frontier: policy.frontier,
    frontierModelId: policy.frontierModelId,
    subagentModelEnv: policy.subagentModel,
  }).model;

  // Rank first so the cap, when it bites, challenges the HIGHEST-priority findings;
  // the lower-ranked overflow is skipped (routed to `unverified`, never dropped).
  const ranked = rankFindings(findings);
  const candidates = ranked.slice(0, cap);
  const overflow = ranked.slice(cap);

  const tasks = candidates.map((finding, idx) => ({
    label: `challenge-${idx}`,
    agentType: 'deep-review:challenger',
    model,
    contextPath: inp.contextPath,
    schema: CHALLENGE_SCHEMA,
    prompt: challengePrompt(finding),
  }));

  const results = tasks.length ? await c.parallel(tasks) : [];

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
    if (res === null || res === undefined || pyIntStrict(rawScore) === null) {
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
      dedup_dropped: applied.stats.dedup_dropped,
      final_count: applied.stats.final_count,
    },
    generated_at: inp.generatedAt,
  };
}

// --- Phase 8: Report --------------------------------------------------------

const REPORT_SCHEMA = { type: 'object', report: 'string' };

// Char budget for the findings payload embedded in one report-writer prompt.
// Above this, reportStage segments findings into chunks and dispatches one
// report-writer per chunk (the report generation would otherwise blow the
// writer's context). 100k chars ~= a comfortably-sized single dispatch.
const REPORT_SEGMENT_CHAR_BUDGET = 100000;

// reportStage(ctx, input) -> { report, gaps }
// Dispatches the report-writer agent to render the review markdown from the
// high-confidence + unverified buckets (carried BY VALUE in the prompt — the
// workflow script has no disk). Each agent() call is wrapped in try/catch: a bare
// agent() THROWS on schema-retry-exhaustion / unknown agentType (Phase 0), so the
// catch is what makes the "minimal report" degradation reachable — a bare
// `null -> minimal` check could never fire because the throw would escape first.
// On throw OR a null/empty result, a deterministic minimal report is assembled
// from the pipeline stats and a gap is recorded; report failure is NON-FATAL.
//
// Segmentation: when the serialized findings payload exceeds
// REPORT_SEGMENT_CHAR_BUDGET the findings are chunked and one report-writer is
// dispatched PER chunk (sequentially, each with the same try/catch), then the
// per-chunk reports are concatenated under titled segment headings. Any single
// chunk that fails degrades to its own minimal section — the rest still render.
export async function reportStage(ctx, input) {
  const c = ctx || defaultCtx();
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const policy = inp.policy || {};
  const model = resolvePolicy('deep-review:report-writer', {
    frontier: policy.frontier,
    frontierModelId: policy.frontierModelId,
    subagentModelEnv: policy.subagentModel,
  }).model;

  const findings = inp.findings || [];
  const oversized = JSON.stringify(findings).length > REPORT_SEGMENT_CHAR_BUDGET;
  if (!oversized) {
    return dispatchReportSegment(c, model, inp, findings, null);
  }

  // Segment: one dispatch per chunk, sequentially, titled sections joined.
  const chunks = chunkFindingsBySize(findings, REPORT_SEGMENT_CHAR_BUDGET);
  const parts = [];
  const gaps = [];
  for (let i = 0; i < chunks.length; i += 1) {
    const seg = { index: i, total: chunks.length };
    const out = await dispatchReportSegment(c, model, inp, chunks[i], seg);
    parts.push(`## Report segment ${i + 1} of ${chunks.length}\n\n${out.report}`);
    gaps.push(...out.gaps);
  }
  return { report: parts.join('\n\n'), gaps };
}

// One report-writer dispatch over `findings` (a whole set or one segment). Owns
// the try/catch + minimal-section fallback. `seg` (or null) labels the dispatch
// and tags the gap so a segmented failure is traceable to its chunk.
async function dispatchReportSegment(c, model, inp, findings, seg) {
  const tag = seg ? ` segment ${seg.index}` : '';
  const segInp = { ...inp, findings };
  try {
    const result = await c.agent({
      label: seg ? `report-writer-${seg.index}` : 'report-writer',
      agentType: 'deep-review:report-writer',
      model,
      contextPath: inp.contextPath,
      schema: REPORT_SCHEMA,
      prompt: reportPrompt(segInp, seg),
    });
    if (!result || !result.report) {
      return { report: minimalReport(segInp), gaps: [`report${tag}: writer returned no report — assembled a minimal report from pipeline stats`] };
    }
    return { report: result.report, gaps: [] };
  } catch (e) {
    return { report: minimalReport(segInp), gaps: [`report${tag}: writer agent threw (${(e && e.message) || 'unknown'}) — assembled a minimal report from pipeline stats`] };
  }
}

// Greedy pack: accumulate findings into a chunk until adding the next would exceed
// `budget` serialized chars, then start a new chunk. A single oversized finding
// still goes in a chunk of its own (never dropped).
function chunkFindingsBySize(findings, budget) {
  const chunks = [];
  let cur = [];
  let curSize = 0;
  for (const f of findings) {
    const size = JSON.stringify(f).length;
    if (cur.length && curSize + size > budget) {
      chunks.push(cur);
      cur = [];
      curSize = 0;
    }
    cur.push(f);
    curSize += size;
  }
  if (cur.length) chunks.push(cur);
  return chunks.length ? chunks : [[]];
}

// Deterministic fallback report (no agent, no wall-clock) built from what the
// pipeline already knows. Never throws — this is the last-resort degradation.
function minimalReport(inp) {
  const findings = inp.findings || [];
  const unverified = inp.unverified || [];
  const lines = [
    '# Deep Review (minimal report)',
    '',
    'The report-writer agent was unavailable; this fallback was assembled deterministically from pipeline results.',
    '',
    `- High-confidence findings: ${findings.length}`,
    `- Unverified / pipeline-degraded findings: ${unverified.length}`,
  ];
  if (inp.summary) lines.push('', '## Change summary', '', String(inp.summary));
  if (findings.length) {
    lines.push('', '## Findings');
    for (const f of findings) {
      lines.push(`- [${(f.severity || 'unknown').toUpperCase()}] ${f.title || f.id || 'finding'} (${f.file || '?'}:${f.line_start != null ? f.line_start : '?'})`);
    }
  }
  return lines.join('\n');
}

function reportPrompt(inp, seg) {
  const ctxLine = inp.contextPath ? `Read the shared context at ${inp.contextPath}. ` : '';
  const segLine = seg ? `This is report segment ${seg.index + 1} of ${seg.total}; render only the findings in this segment. ` : '';
  const body = JSON.stringify({
    summary: inp.summary || '',
    findings: inp.findings || [],
    unverified: (!seg || seg.index === 0) ? (inp.unverified || []) : [], // render the unverified bucket once, in segment 0
    stats: inp.stats || {},
  });
  return `${ctxLine}${segLine}Write the deep-review report as markdown for these results. Put high-confidence findings in the main section and unverified/pipeline-degraded findings in a clearly-labelled secondary section. Results JSON:\n${body}\nReturn { report } where report is the full markdown document.`;
}

// --- Persistence: writeArtifacts --------------------------------------------

const WRITER_SCHEMA = { type: 'object', artifactPaths: 'object' };

// writeArtifacts(ctx, { findings, unverified, report, checkpoints, outputDir,
// headShaShort, policy }) -> { artifactPaths, gaps, partial }
// The workflow script has NO disk access, so a writer agent persists findings.json
// + report.md + the checkpoint/progress JSON to {output_dir}; the content is carried
// BY VALUE in the dispatch prompt. Wrapped in its own try/catch (like reportStage):
// a throw OR null result degrades to a partial-artifacts gap with null paths and is
// NON-FATAL — it never bubbles to the top-level catch.
export async function writeArtifacts(ctx, input) {
  const c = ctx || defaultCtx();
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const outputDir = inp.outputDir || '.deep-review';
  const sha = inp.headShaShort || 'head';
  const policy = inp.policy || {};
  const paths = {
    findings: `${outputDir}/deep-review-findings-${sha}.json`,
    report: `${outputDir}/deep-review-report-${sha}.md`,
    checkpoints: `${outputDir}/${checkpointPath('all', sha)}`,
  };
  const model = resolvePolicy('deep-review:artifact-writer', {
    frontier: policy.frontier,
    frontierModelId: policy.frontierModelId,
    subagentModelEnv: policy.subagentModel,
  }).model;
  const partial = (reason) => ({
    artifactPaths: { findings: null, report: null, checkpoints: null },
    gaps: [`writeArtifacts: ${reason} — artifacts not persisted (partial-artifacts)`],
    partial: true,
  });
  try {
    const result = await c.agent({
      label: 'artifact-writer',
      agentType: 'deep-review:artifact-writer',
      model,
      schema: WRITER_SCHEMA,
      prompt: writeArtifactsPrompt(inp, paths),
    });
    if (!result) return partial('writer returned null');
    return { artifactPaths: paths, gaps: [], partial: false };
  } catch (e) {
    return partial(`writer agent threw (${(e && e.message) || 'unknown'})`);
  }
}

// The persisted findings must satisfy BOTH downstream boundaries: verify_findings.py
// reads canonical names (file/line_start/line_end/description...) and the retained
// post_review.py INDEXES the v2 names f["file"]/f["line"] and reads body/end_line.
// So at the persist boundary each finding carries the v2 aliases ALONGSIDE the
// canonical fields (a union schema): line<-line_start, end_line<-line_end,
// body<-description. Existing v2 keys are never overwritten.
function toV2Aliased(f) {
  const out = { ...f };
  if (out.line === undefined && out.line_start !== undefined) out.line = out.line_start;
  if (out.end_line === undefined && out.line_end !== undefined) out.end_line = out.line_end;
  if (out.body === undefined && out.description !== undefined) out.body = out.description;
  return out;
}

// The by-value payload the writer agent persists. Findings/unverified are aliased
// to the union schema so the persisted findings.json is consumable by BOTH boundary
// scripts unchanged. Pure + exported so tests (and the node recorder) can assert the
// persist output is REAL pipeline output, not a hand-authored fixture.
export function writerPayload(inp) {
  return {
    findings: (inp.findings || []).map(toV2Aliased),
    unverified: (inp.unverified || []).map(toV2Aliased),
    report: inp.report || '',
    checkpoints: inp.checkpoints || {},
  };
}

// Wire format for the writer's by-value payload: the payload is a single JSON line
// at the END of the prompt, prefixed by this marker. The artifact-writer agent (and
// parseWriterPayload) split on the marker to recover the exact object to persist.
const WRITER_PAYLOAD_MARKER = 'PAYLOAD_JSON:';

// parseWriterPayload(prompt) -> the payload object the writer was asked to persist.
// Documents/round-trips the WRITER_PAYLOAD_MARKER wire format (JSON.stringify emits a
// single physical line, so everything after the last marker is the JSON object).
export function parseWriterPayload(prompt) {
  const idx = (prompt || '').lastIndexOf(WRITER_PAYLOAD_MARKER);
  if (idx === -1) return null;
  return JSON.parse(prompt.slice(idx + WRITER_PAYLOAD_MARKER.length));
}

function writeArtifactsPrompt(inp, paths) {
  const payload = JSON.stringify(writerPayload(inp));
  return `Persist these deep-review artifacts to disk exactly as given (the workflow has no disk access). Write the payload's findings (as pretty JSON) to ${paths.findings}, the payload's report (verbatim markdown) to ${paths.report}, and the payload's checkpoints (as JSON) to ${paths.checkpoints}. Return { artifactPaths } echoing the paths you wrote. The payload is the single JSON line after the marker below.\n${WRITER_PAYLOAD_MARKER}${payload}`;
}

// --- Checkpoints ------------------------------------------------------------

// checkpointPath(phase, sha) -> bare filename for a phase's persisted checkpoint.
// The skill layer reads these on a rerun and injects the recovered outputs into
// the args waist (args.checkpoints); the pipeline has no disk access of its own.
export function checkpointPath(phase, sha) {
  return `deep-review-checkpoint-${phase}-${sha}.json`;
}

// readCheckpoints(ctx, args) -> phase-keyed resume map ({ phase: priorOutput }).
// Injected through the args waist on a rerun (a phase whose output is present is
// skipped, not re-dispatched). The platform's own resume machinery caches
// agent-level work; this is the coarser phase-level skip. Accepts EITHER a bare
// { phase: output } map OR the persisted checkpoint artifact's { phases, completed,
// phaseReached } wrapper (unwrapping .phases) so the artifact the pipeline itself
// writes can be fed straight back. Falls back to a ctx-borne map (test seam), then {}.
export function readCheckpoints(ctx, args) {
  const unwrap = (cp) => {
    if (!cp || typeof cp !== 'object') return null;
    return (cp.phases && typeof cp.phases === 'object') ? cp.phases : cp;
  };
  const A = args || {};
  return unwrap(A.checkpoints) || (ctx && unwrap(ctx.checkpoints)) || {};
}

// --- Full orchestration: runWith --------------------------------------------

// runWith(ctx, rawArgs) -> compact envelope.
// The single testable orchestration seam (pipeline_entry.js's run() just builds the
// real-globals ctx and delegates here). Validates the args waist up front (reject ->
// ok:false, no dispatch), then threads summarize -> discover -> merge -> verify ->
// validate -> filter -> challenge -> report inside ONE top-level try/catch, checking
// checkpoints before each phase and persisting via writeArtifacts at the end. Every
// stage's gaps aggregate into the final envelope. reportStage / writeArtifacts each
// catch their OWN agent throws (degrading to a minimal report / partial-artifacts gap),
// so the top-level catch is the last resort for unexpected throws in the deterministic
// glue — it returns { ok:false, error, phaseReached } and NEVER lets a throw escape.
// The return is compact by design: counts + artifact paths + gaps, never the raw
// findings bulk.
export async function runWith(ctx, rawArgs) {
  const A = normalizeArgs(rawArgs);
  const check = validateArgs(A);
  if (!check.ok) {
    return {
      ok: false,
      error: `invalid args: ${check.errors.join('; ')}`,
      phaseReached: 'args',
      artifactPaths: {},
      stats: {},
      gaps: check.errors,
    };
  }

  const c = ctx || defaultCtx();
  const limits = A.limits || {};
  const policy = A.policy || {};
  const contextPath = `${A.outputDir}/deep-review-context-${A.headShaShort}.md`;
  const checkpoints = readCheckpoints(c, A);

  const gaps = [];
  const completed = [];
  const phaseOutputs = {}; // per-phase output map — persisted as the checkpoint artifact
  let phaseReached = 'start';

  // Resume: a phase whose checkpoint is present reuses that output instead of
  // dispatching. Either way the phase counts as reached, and its output is recorded
  // into phaseOutputs so the persisted checkpoint artifact is a producible resume map.
  const runPhase = async (name, thunk) => {
    const out = checkpoints[name] !== undefined ? checkpoints[name] : await thunk();
    phaseOutputs[name] = out;
    completed.push(name);
    phaseReached = name;
    return out;
  };

  try {
    const summaryOut = await runPhase('summarize', () => summarize(c, {
      changedFiles: A.changedFiles || [], changedLines: A.changedLines || 0, limits, policy, contextPath,
    }));
    gaps.push(...(summaryOut.gaps || []));

    const discoverOut = await runPhase('discover', () => discover(c, {
      agentFlags: A.agentFlags || {}, limits, policy, contextPath,
    }));
    gaps.push(...(discoverOut.gaps || []));

    const mergeOut = await runPhase('merge', () => mergeStage(discoverOut, {
      base_branch: A.baseBranch, head_sha: A.headShaShort,
    }));

    const verifyOut = await runPhase('verify', () => verifyStage(c, {
      findings: mergeOut.findings || [], limits, policy, nonce: A.nonce, headShaShort: A.headShaShort,
      verify: { ...(A.verify || {}), baseBranch: A.baseBranch, diffPath: A.diffPath },
    }));
    gaps.push(...(verifyOut.gaps || []));

    const validateOut = await runPhase('validate', () => validateStage(c, {
      findings: verifyOut.findings || [], limits, policy, contextPath,
    }));
    gaps.push(...(validateOut.gaps || []));

    const filterOut = await runPhase('filter', () => filterStage({
      findings: validateOut.findings || [], reviewConfig: A.reviewConfig || {},
      exclusionPatterns: A.exclusionPatterns || [], generatedAt: A.generatedAt,
    }));

    const challengeOut = await runPhase('challenge', () => challengeStage(c, {
      findings: filterOut.filtered || [], limits, policy, contextPath, generatedAt: A.generatedAt,
    }));
    gaps.push(...(challengeOut.gaps || []));

    const reportOut = await runPhase('report', () => reportStage(c, {
      summary: summaryOut.summary,
      findings: challengeOut.findings,
      unverified: challengeOut.unverified,
      stats: {
        discovered: (discoverOut.findings || []).length,
        validate: validateOut.stats,
        filter: filterOut.stats,
        challenge: challengeOut.stats,
      },
      policy, contextPath, generatedAt: A.generatedAt,
    }));
    gaps.push(...(reportOut.gaps || []));

    // Persistence is a post-phase step: writeArtifacts owns its try/catch, so a
    // writer failure degrades to a partial-artifacts gap rather than the top-level catch.
    const writeOut = await writeArtifacts(c, {
      findings: challengeOut.findings,
      unverified: challengeOut.unverified,
      report: reportOut.report,
      // Persist the actual per-phase outputs map (the producible resume state) plus
      // the aggregate progress; readCheckpoints unwraps .phases when it is fed back.
      checkpoints: { phases: phaseOutputs, completed, phaseReached },
      outputDir: A.outputDir,
      headShaShort: A.headShaShort,
      generatedAt: A.generatedAt,
      policy,
    });
    gaps.push(...(writeOut.gaps || []));

    return {
      ok: true,
      phaseReached,
      stats: {
        discovered: (discoverOut.findings || []).length,
        merged: (mergeOut.findings || []).length,
        verified: verifyOut.verified,
        highConfidence: (challengeOut.findings || []).length,
        unverified: (challengeOut.unverified || []).length,
        degraded: discoverOut.degraded || [],
        validate: validateOut.stats,
        filter: filterOut.stats,
        challenge: challengeOut.stats,
      },
      artifactPaths: writeOut.artifactPaths,
      resolvedPolicy: {
        subagentModel: policy.subagentModel || null,
        frontier: !!policy.frontier,
        frontierModelId: policy.frontierModelId || null,
      },
      checkpoints: completed,
      gaps,
    };
  } catch (e) {
    return {
      ok: false,
      error: (e && e.message) || String(e),
      phaseReached,
      artifactPaths: {},
      stats: {},
      gaps,
    };
  }
}
