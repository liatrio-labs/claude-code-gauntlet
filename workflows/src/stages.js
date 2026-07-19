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

// Runtime globals are injected by the workflow host; under node:test they are absent,
// so ctx must be supplied. defaultCtx lets the shipped pipeline call stages without wiring.
function defaultCtx() {
  return {
    agent: typeof agent === 'function' ? agent : undefined,
    parallel: typeof parallel === 'function' ? parallel : undefined,
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
    const trust = trustSlice(env, { nonce, headShaShort, n: slice.length });
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
// sent — a short n_in means a truncated/partial run we must not trust).
function trustSlice(env, { nonce, headShaShort, n }) {
  if (!env || typeof env !== 'object') return { ok: false, reason: 'executor returned no envelope' };
  if (env.status !== 'ok') return { ok: false, reason: `status=${env.status == null ? 'missing' : env.status}${env.stderr ? ` (${env.stderr})` : ''}` };
  const r = env.receipt || {};
  if (r.nonce !== nonce) return { ok: false, reason: `receipt nonce mismatch (got ${r.nonce == null ? 'missing' : r.nonce})` };
  if (r.sha !== headShaShort) return { ok: false, reason: `receipt sha mismatch (got ${r.sha == null ? 'missing' : r.sha})` };
  if (r.n_in !== n) return { ok: false, reason: `receipt n_in mismatch (got ${r.n_in == null ? 'missing' : r.n_in}, expected ${n})` };
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
    '--nonce', inp.nonce,
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
