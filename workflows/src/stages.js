// stages.js — orchestration stage functions for the code-gauntlet v3 pipeline,
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

// Resolve the dispatch model for an agent type from the args-waist policy object —
// the single place the policy shape maps onto resolvePolicy's opts.
function modelFor(agentType, policy) {
  return resolvePolicy(agentType, { subagentModelEnv: policy.subagentModel }).model;
}

// Shared char budget for a single agent's by-value prompt payload. Above it, stages
// that carry findings by value (report generation, verify slice-input writing) segment
// into multiple dispatches to stay under the writer's context.
const SEGMENT_CHAR_BUDGET = 100000;

// Greedy pack: accumulate items into a chunk until adding the next would exceed
// `budget` serialized chars, then start a new chunk. A single oversized item still
// goes in a chunk of its own (never dropped). Shared by report segmentation and
// verify slice-input writing.
function chunkBySerializedSize(items, budget) {
  const chunks = [];
  let cur = [];
  let curSize = 0;
  for (const it of items) {
    const size = JSON.stringify(it).length;
    if (cur.length && curSize + size > budget) {
      chunks.push(cur);
      cur = [];
      curSize = 0;
    }
    cur.push(it);
    curSize += size;
  }
  if (cur.length) chunks.push(cur);
  return chunks.length ? chunks : [[]];
}

// --- Phase 1: Summarize -----------------------------------------------------

// The change-summarizer returns its prose wrapped as { summary } (StructuredOutput).
const SUMMARIZE_SCHEMA = { type: 'object', properties: { summary: { type: 'string' } }, required: ['summary'] };

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
  const model = modelFor('code-gauntlet:change-summarizer', policy);

  const bucketed = changedLines > 500 && changedFiles.length > bucketSize;
  try {
    if (bucketed) {
      const buckets = [];
      for (let i = 0; i < changedFiles.length; i += bucketSize) buckets.push(changedFiles.slice(i, i + bucketSize));
      // parallel() takes thunks; each calls agent(promptString, opts).
      const thunks = buckets.map((files, idx) => () => c.agent(summarizePrompt(inp, files), {
        label: `summarize-bucket-${idx}`,
        agentType: 'code-gauntlet:change-summarizer',
        model,
        schema: SUMMARIZE_SCHEMA,
      }));
      const partials = (await c.parallel(thunks)).filter(Boolean);
      if (partials.length === 0) return { summary: '', gaps: ['summarize failed'] };
      const mergeResult = await c.agent(summarizeMergePrompt(partials), {
        label: 'summarize-merge',
        agentType: 'code-gauntlet:change-summarizer',
        model,
        schema: SUMMARIZE_SCHEMA,
      });
      if (!mergeResult) return { summary: '', gaps: ['summarize failed'] };
      return { summary: mergeResult.summary || '', gaps: [] };
    }
    const result = await c.agent(summarizePrompt(inp, changedFiles), {
      label: 'summarize',
      agentType: 'code-gauntlet:change-summarizer',
      model,
      schema: SUMMARIZE_SCHEMA,
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
    if (!byAgent.has(d.agentType)) byAgent.set(d.agentType, { agentType: d.agentType, dimensions: [], schemaExtra: {}, conditionalFlags: [], promptExtra: null });
    const spec = byAgent.get(d.agentType);
    spec.dimensions.push(d.dimension);
    Object.assign(spec.schemaExtra, d.schemaExtra || {});
    spec.conditionalFlags.push(d.conditionalFlag);
    // promptExtra is scoped per AGENT, not per dimension — every DIMENSIONS row for a
    // multi-dimension agent is expected to carry the same value (see registry.js), so a
    // truthy value on any of an agent's rows sets it for the whole spec.
    if (d.promptExtra) spec.promptExtra = d.promptExtra;
  }
  // Preserve AGENTS order (derived from DIMENSIONS) so dispatch order is deterministic.
  return AGENTS.map((a) => byAgent.get(a));
}

// An agent is active when at least one of its dimensions is enabled: the dimension's
// conditionalFlag is null (always on) or the corresponding agentFlags entry is truthy.
function agentActive(spec, agentFlags) {
  return spec.conditionalFlags.some((flag) => flag === null || flag === undefined || agentFlags[flag]);
}

// Canonical finding property types. confidence is a NUMBER end-to-end: agents emit a
// numeric 0-100 score per their .md contracts, so declaring it `number` here makes
// StructuredOutput return the number at EVERY by-value boundary (discovery included) —
// the string form "85" the schema used to declare simply never exists, so the filter's
// consensus `+` boost can never string-concatenate ("85"+10 -> "8510"). pinNumericFields
// stays as defense-in-depth for legacy/checkpoint-resume findings that predate this pin.
const FINDING_PROP_TYPES = {
  id: 'string', file: 'string', line_start: 'number', line_end: 'number',
  title: 'string', description: 'string', severity: 'string', confidence: 'number',
  dimension: 'string', origin: 'string', evidence: 'string',
};
const FINDING_REQUIRED = ['id', 'file', 'line_start', 'title', 'description', 'severity', 'confidence', 'dimension'];

// The canonical finding ITEM schema (one array element). Declared IN FULL — every
// canonical property with a concrete type, `description` among them — everywhere an agent
// returns findings BY VALUE. An items schema of `{ type:'object', properties:{} }` is the
// trap: StructuredOutput leaves an empty-properties object UNCONSTRAINED, so a model
// transcribing findings back "verbatim via the schema" is free to drop the single largest
// field — `description` — which is exactly what the verify executor did, emptying
// descriptions for every downstream stage (validate/filter/challenge) and false-firing the
// filter's short-description injection guard on high-confidence findings. confidence is
// NUMBER everywhere now (FINDING_PROP_TYPES), so there is no per-stage confidence override.
// `schemaExtra` entries are EITHER a type-name shorthand string ({ k: 'string' } ->
// { type:'string' }) OR a full JSON-Schema fragment used verbatim (how array-valued extras
// like affected_consumers are declared); the shorthand keeps the common case terse while
// the fragment form supports arrays the platform's schema validator requires `items` on.
function findingItemSchema(schemaExtra) {
  const props = {};
  for (const [k, t] of Object.entries(FINDING_PROP_TYPES)) props[k] = { type: t };
  props.cross_file_refs = { type: 'array', items: { type: 'string' } };
  for (const [k, t] of Object.entries(schemaExtra || {})) props[k] = typeof t === 'string' ? { type: t } : t;
  return { type: 'object', properties: props, required: FINDING_REQUIRED };
}

// The union of EVERY dimension's schemaExtra. The verify slice carries findings from ALL
// agents mixed together (post-merge), so its echo item schema must declare every agent's
// per-dimension extras — not one agent's — or a field one agent emitted is dropped when the
// executor transcribes the --output file "verbatim via the schema" (the same field-dropping
// class the empty-properties trap caused for description).
function allSchemaExtras() {
  const out = {};
  for (const d of DIMENSIONS) for (const [k, t] of Object.entries(d.schemaExtra || {})) out[k] = t;
  return out;
}

// The verify echo item schema: the full canonical finding shape (numeric confidence) PLUS
// (1) `agent` — injected by merge, and detectDisagreement keys suppression / security-
// escalation / test-correctness routing on it, so it MUST survive the echo or that routing
// fires stochastically; (2) the union of all per-dimension extras; and (3) elimination_reason
// — run_verification() ALWAYS stamps it on a real elimination (verify_findings.py), and
// trustSlice's content-fidelity gate requires it on every eliminated[] entry, so it must be
// declarable or an honest elimination's stamp is dropped in transcription and the gate false-
// fires. (eliminated_by is a JS-pipeline-only field the verify script never sets — declaring
// it would only invite the executor to fabricate it, so it is intentionally NOT declared.)
function verifyItemSchema() {
  const item = findingItemSchema(allSchemaExtras());
  item.properties.agent = { type: 'string' };
  item.properties.elimination_reason = { type: 'string' };
  return item;
}

// Canonical finding schema (per-dimension schemaExtra unioned on top), wrapped in the
// per-agent result envelope { findings, complete, total_seen }. REAL JSON Schema —
// {type, properties, required, items} — because the platform validates schemas before
// dispatch and StructuredOutput enforces them (shorthand {id:'string'} is rejected).
// schemaExtra is shorthand {key: typeName} (or a full JSON-Schema fragment for arrays).
function findingSchema(spec) {
  return {
    type: 'object',
    properties: {
      findings: {
        type: 'array',
        items: findingItemSchema(spec.schemaExtra),
      },
      complete: { type: 'boolean' },
      total_seen: { type: 'number' },
    },
    required: ['findings', 'complete', 'total_seen'],
  };
}

// discover(ctx, input) -> { findings, gaps, degraded, dispatched }
// `dispatched` is the full fan-out list (every active agentType, whether it succeeded,
// failed, or returned zero findings) — mergeStage() uses it so a zero-finding agent
// stays distinguishable from one never dispatched at all (e.g. disabled via agentFlags).
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
  // Platform contract: parallel() takes an array of ZERO-ARG THUNKS, each calling
  // agent(promptString, opts). label IS the agentType (identity for gaps); the prompt
  // already names the dimensions, so no non-standard opts field is passed.
  const thunks = specs.map((spec) => {
    const model = modelFor(spec.agentType, policy);
    return () => c.agent(discoverPrompt(inp, spec), {
      label: spec.agentType,
      agentType: spec.agentType,
      model,
      schema: findingSchema(spec),
    });
  });

  const results = await c.parallel(thunks);

  const gaps = [];
  const findings = [];
  const degradedDims = [];

  // parallel() resolves a failed member to null IN PLACE (Phase 0 verified): the
  // results array is positionally aligned with `thunks`, so results[i] pairs with specs[i].
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
      // Inject the SHORT agent name (canonical schema: 'bug-detector', not the dispatch
      // agentType 'code-gauntlet:bug-detector'). filterFindings matches short names for
      // disagreement suppression / security escalation, and mergeStage regroups on this —
      // the full prefix silently broke both on the live path.
      f.agent = spec.agentType.split(':').pop();
      findings.push(f);
    }
    const nearCap = discoveryCap != null && (res.total_seen >= discoveryCap || list.length >= discoveryCap);
    if (res.complete === false || nearCap) {
      gaps.push(`${spec.agentType}: possibly incomplete (complete=${res.complete === false ? 'false' : 'true'}, total_seen=${res.total_seen}) — dimensions ${spec.dimensions.join('/')}`);
    }
  });

  // Each dimension belongs to a single agent so no overlap is possible today; the Set
  // keeps degraded deduplicated and insertion-ordered should that ever change.
  return {
    findings,
    gaps,
    degraded: [...new Set(degradedDims)],
    dispatched: specs.map((spec) => spec.agentType),
  };
}

// v2-grade elicitation frame (v3's terse one-liner cut discovery yield ~40% — see the
// skill's phase3-dispatch.md history): read context first, investigate with the agent's
// OWN methodology/tools per its .md definition (loaded as its system prompt via
// agentType), no cap/no minimum on findings, and a reminder of the canonical schema's
// single-paragraph description constraint. Kept short — StructuredOutput's `schema`
// (findingSchema) does the actual shape enforcement, this prompt only sets behavior.
//
// Hill-climb iter 5: two additions. (1) A dimension-agnostic EVIDENCE DISCIPLINE clause
// in the base prompt — every finding must cite concrete, investigated evidence, and any
// absence/"missing" claim must name the specific file or path checked (the
// unverifiable-claim source the challenger later gates on). (2) spec.promptExtra
// (registry.js) is appended verbatim when the agent carries one — the per-agent
// discovery-breadth sweeps (security: SSRF/postMessage/string-bypass; bug-detector +
// conventions-and-intent: typo/naming). Scoping lives entirely in the registry; no
// agent-name special-casing here.
function discoverPrompt(inp, spec) {
  const ctxLine = inp.contextPath
    ? `Read the shared context at ${inp.contextPath} first — it has the diff, project rules, and risk classification. `
    : '';
  const dims = spec.dimensions.join(', ');
  const base = `${ctxLine}This is a code gauntlet built for thoroughness, not speed: investigate using your own methodology and tools (LSP first, Grep fallback) as defined for your role, across the full codebase context around the diff — not just the changed lines. Your dimension(s): ${dims}. Report EVERY genuine finding for these dimension(s): there is no cap and no minimum. An empty findings list must reflect a genuine post-investigation absence of issues, never brevity or a quota. Every finding MUST cite concrete evidence: the evidence field must be non-empty and reference real lines you actually inspected (in the diff or in a file you opened) — a finding you cannot ground in inspected code is noise, do not emit it. For any absence or "missing" claim (e.g. a test-coverage negative asserting no test exists), name in evidence the specific file or path you checked; an unproven absence is not a finding. Return { findings, complete, total_seen }; each finding must match the canonical schema, with description as a single paragraph of prose, at most 500 characters — no code blocks or bullet lists; put code references in evidence and cross_file_refs instead.`;
  return spec.promptExtra ? `${base} ${spec.promptExtra}` : base;
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

  // agents drives merge()'s per-agent iteration AND methodology.agents_dispatched — and
  // merge()'s injectAgentField RE-STAMPS every finding's `.agent` to whichever string is
  // in this list. discover() now injects the SHORT agent name onto findings (FIX 1: the
  // full 'code-gauntlet:' prefix broke filterFindings' short-name matching), so this list
  // must match that short form too, or the `nd[agent]` lookup below misses for every
  // agent (silently dropping all its findings) and injectAgentField would re-inject the
  // long prefix, undoing FIX 1 downstream. discover()'s own fan-out list (`dispatched`)
  // is still the full 'code-gauntlet:<agent>' agentType (unaffected by FIX 1), so it is
  // normalized here — Object.keys(ndjsonContents) is already short (built straight from
  // findings' own .agent) and needs no normalization.
  //
  // Prefer discover()'s own fan-out list (`dispatched`) so a zero-finding agent is
  // still counted as dispatched, distinguishable from one never dispatched at all
  // (disabled via agentFlags). Older/synthetic callers that omit `dispatched` fall back
  // to the agents that actually produced findings, and finally the full roster so an
  // empty run still yields an envelope.
  const shortAgentName = (a) => (typeof a === 'string' ? a.split(':').pop() : a);
  const agents = Array.isArray(out.dispatched)
    ? out.dispatched.map(shortAgentName)
    : (Object.keys(ndjsonContents).length ? Object.keys(ndjsonContents) : AGENTS.map(shortAgentName));
  return merge(ndjsonContents, {}, { ...M, agents });
}

// --- Phase 4: Verify --------------------------------------------------------

// The discriminated-union envelope the executor returns. Both shapes coexist so an
// honest failure is schema-valid — the executor never fabricates a success under
// StructuredOutput retry pressure ({status:'failed'} is a legal answer).
const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    status: { type: 'string' }, // 'ok' | 'failed'
    receipt: {
      type: 'object',
      properties: { sha: { type: 'string' }, n_in: { type: 'number' }, nonce: { type: 'string' } },
    },
    result: {
      type: 'object',
      properties: {
        // verified/eliminated carry findings BY VALUE — verifyStage collects result.verified
        // as THE findings for every later stage — so their items must declare the FULL finding
        // shape (verifyItemSchema). An empty-properties item let the executor drop `description`
        // when echoing the --output file "verbatim", which emptied descriptions downstream and
        // false-fired the filter's injection guard. verifyItemSchema declares numeric confidence,
        // the injected `agent` field (detectDisagreement routes on it), every per-dimension extra,
        // and elimination_reason (the script's real-elimination stamp, gated by trustSlice).
        verified: { type: 'array', items: verifyItemSchema() },
        eliminated: { type: 'array', items: verifyItemSchema() },
        batches: { type: 'array', items: { type: 'object', properties: {} } },
        stats: { type: 'object', properties: {} },
      },
    },
    exitCode: { type: 'number' },
    stderr: { type: 'string' },
  },
  required: ['status'], // discriminated union: receipt/result only present on status:'ok'
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

  const model = modelFor('code-gauntlet:executor', policy);

  const slices = [];
  for (let i = 0; i < findings.length; i += sliceSize) slices.push(findings.slice(i, i + sliceSize));

  // Materialize each slice's --input JSON on disk BEFORE the executor loop. The
  // executor reads ${inputPathBase}.slice{i}.json, but the workflow script has no disk
  // access and the merged findings exist only mid-workflow (the skill CANNOT pre-write
  // them). One or more artifact-writer dispatches (segmented like the report stage when
  // the payload is large) write them by value. Any writer failure -> the WHOLE set takes
  // the UNVERIFIED path, exactly like an untrusted slice: never fabricate a verification.
  const materialized = await materializeVerifySlices(c, inp, slices, policy);
  if (!materialized.ok) return unverifiedResult(findings, materialized.reason);

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
      // agent(promptString, opts); the pinned command is embedded in the prompt
      // (verifyPrompt), which is how the executor agent receives it.
      env = await c.agent(verifyPrompt(inp, i), {
        label: `verify-slice-${i}`,
        agentType: 'code-gauntlet:executor',
        model,
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

  if (failureReason !== null) return unverifiedResult(findings, failureReason);

  return { findings: verifiedOut, verified: true, gaps: [] };
}

// The UNVERIFIED degradation: every ORIGINAL finding re-emitted with origin='unknown'
// (surfaced-classification skipped), verified=false, a loud gap. Findings are never
// dropped and success is never fabricated. Reached by an untrusted slice, an executor
// throw, OR a slice-input writer failure. Numeric-string fields are pinned here for the
// same reason they are pinned on the slice-input path: the trusted path returns the
// script's re-scored numbers, but this path re-emits discovery-shaped findings whose
// confidence is the schema's numeric STRING ("85") — leaked downstream, the filter's
// consensus `+` boost concatenates ("85" + 10 -> "8510" -> clamped to 100).
function unverifiedResult(findings, reason) {
  return {
    findings: findings.map((f) => ({ ...pinNumericFields(f), origin: 'unknown' })),
    verified: false,
    gaps: [`verify: UNVERIFIED — ${reason}; all ${findings.length} finding(s) marked origin=unknown, surfaced-classification skipped`],
  };
}

// Numeric finding fields that verify_findings.py does arithmetic on (line_start - 1,
// line comparisons). Pin them to real numbers before the slice-input JSON is written:
// a value that reaches the script as a string ("153") makes the receipt-path arithmetic
// raise `unsupported operand type(s) for -: 'str' and 'int'` and degrade the whole slice
// to UNVERIFIED (the TypeError the live smoke run hit). Coerce only clean numeric strings;
// leave everything else (null, non-numeric) untouched so the script's own guards still fire.
const VERIFY_NUMERIC_FIELDS = ['line_start', 'line_end', 'line', 'end_line', 'confidence'];
function pinNumericFields(finding) {
  const out = { ...finding };
  for (const k of VERIFY_NUMERIC_FIELDS) {
    const val = out[k];
    if (typeof val === 'string' && val.trim() !== '' && Number.isFinite(Number(val))) out[k] = Number(val);
  }
  return out;
}

// Dispatch the artifact-writer to persist each slice's --input JSON (the shape
// verify_findings.py --input reads: { findings, base_branch }). Segmented under the
// shared char budget. Returns { ok } / { ok:false, reason }; a throw OR a null result
// is a failure (the caller degrades the whole set to UNVERIFIED).
async function materializeVerifySlices(c, inp, slices, policy) {
  const v = inp.verify || {};
  const inputPathBase = v.inputPathBase || 'phase4-input';
  const model = modelFor('code-gauntlet:artifact-writer', policy);
  const entries = slices.map((slice, i) => ({
    path: `${inputPathBase}.slice${i}.json`,
    content: { findings: slice.map(pinNumericFields), base_branch: v.baseBranch },
  }));
  const groups = chunkBySerializedSize(entries, SEGMENT_CHAR_BUDGET);
  for (let g = 0; g < groups.length; g += 1) {
    let result;
    try {
      result = await c.agent(verifySliceWriterPrompt(groups[g]), {
        label: `verify-input-writer-${g}`,
        agentType: 'code-gauntlet:artifact-writer',
        model,
        schema: WRITTEN_SCHEMA,
      });
    } catch (e) {
      return { ok: false, reason: `slice-input writer threw (${(e && e.message) || 'unknown'})` };
    }
    if (!result) return { ok: false, reason: 'slice-input writer returned null' };
    // Write-proof: the echoed `written` list must cover every slice-input path this group
    // dispatched. WRITTEN_SCHEMA declares no `required`, so an empty { written: [] } is
    // schema-valid — without this a writer that persisted nothing would pass and the
    // executor would then read slice-input files that were never written. An uncovered
    // path degrades the WHOLE set to UNVERIFIED (findings kept), never a fabricated verify.
    const written = new Set(Array.isArray(result.written) ? result.written : []);
    const dispatchedPaths = groups[g].map((e) => e.path);
    if (!dispatchedPaths.every((p) => written.has(p))) {
      return { ok: false, reason: 'slice-input writer echo did not cover all dispatched slice paths (no write proof)' };
    }
  }
  return { ok: true };
}

function verifySliceWriterPrompt(entries) {
  const payload = JSON.stringify(entries);
  return `Persist each verify slice-input file to disk exactly as given (the workflow has no disk access). For every entry in the payload, write its "content" as JSON to its "path". Return { written } listing the paths you wrote. The payload is the single JSON line after the marker below.\n${WRITER_PAYLOAD_MARKER}${payload}`;
}

// A slice envelope is trusted only if it is the honest success shape AND its receipt
// echoes exactly what we dispatched: the nonce (this answer is for OUR call), the head
// sha (same tree the workflow resolved), and n_in (the executor loaded every finding we
// sent). Two guards beyond the receipt:
//   (1) COUNT — the result arrays must ACCOUNT for n_in findings (verified + eliminated
//       === n_in — an invariant run_verification always satisfies), so a receipt that
//       survives transport while its result body is truncated cannot silently drop findings.
//   (2) CONTENT FIDELITY — every eliminated[] entry must carry the elimination_reason stamp
//       that run_verification() ALWAYS writes before pushing a finding to eliminated[]
//       (verify_findings.py sets f['elimination_reason'] = 'evidence does not match file
//       content'). An executor that moves a finding verified->eliminated in its ECHO never
//       ran the script's elimination path for it, so it cannot carry the stamp — the receipt
//       and count both still pass (observed live: script disk 10v/0e, echo 7v/3e with a valid
//       receipt), but an unstamped elimination proves the echo is not the script's output.
//       (The stamp is elimination_reason, NOT eliminated_by: the verify script never sets
//       eliminated_by — that is a JS-pipeline-only field — so requiring it would reject every
//       honest elimination.) A failed fidelity check degrades the WHOLE slice to UNVERIFIED,
//       which is conservative: every original finding is KEPT (origin=unknown), so an
//       executor claiming a spurious elimination cannot use it to drop a real finding.
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
  for (const e of result.eliminated) {
    const reason = e && typeof e === 'object' ? e.elimination_reason : undefined;
    if (typeof reason !== 'string' || reason.trim() === '') {
      return { ok: false, reason: 'eliminated entry missing elimination_reason stamp (fabricated elimination — the verify script always stamps a real one)' };
    }
  }
  return { ok: true };
}

// The pinned command: a single `python3 <script> --flags...` invocation of plain word
// tokens only (CLAUDE.md AST-safe emission — no command substitution, heredocs, env
// prefix, or shell operators). Per-slice input/output paths are sha-scoped and index-
// suffixed; verifyStage materializes the slice inputs via the artifact-writer (see
// materializeVerifySlices) before dispatch, then the executor reads the slice output.
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

// Mirror challengeStage's cap semantics EXACTLY: an absent/null challengeCap means
// "challenge every finding" (the stage defaults to findings.length), while 0 is a real
// cap of zero. The guard math must never undercount the stage's actual fan-out.
const effectiveChallengeCap = (L, findings) =>
  Math.max(0, L.challengeCap != null ? L.challengeCap : findings);

// Mirror each stage's own absent/zero-size default EXACTLY (summarize: bucket 20;
// verify/validate: ONE slice/batch over all findings) so the guard arithmetic can
// never go NaN (Math.max(1, undefined) is NaN — a NaN worst case silently disables
// the coarsening loop) and never counts a different fan-out than the stage dispatches.
const effectiveBucketSize = (L) => Math.max(1, L.summarizeBucketSize || 20);
const effectiveSliceSize = (L, findings) => Math.max(1, L.verifySliceSize || findings || 1);
const effectiveBatchSize = (L, findings) => Math.max(1, L.validateBatch || findings || 1);

// worstCaseAgentCount(limits, nFiles, nFindings) -> number
// summarize buckets (+1 merge) + the 7 discovery agents + verify slices + validate
// batches + min(nFindings, challengeCap) challengers + 2 (report + writer).
export function worstCaseAgentCount(limits, nFiles, nFindings) {
  const L = limits || {};
  const files = Math.max(0, nFiles || 0);
  const findings = Math.max(0, nFindings || 0);
  const summarizeCalls = ceilDiv(files, effectiveBucketSize(L)) + 1;
  const verifyCalls = ceilDiv(findings, effectiveSliceSize(L, findings));
  const validateCalls = ceilDiv(findings, effectiveBatchSize(L, findings));
  const challengeCalls = Math.min(findings, effectiveChallengeCap(L, findings));
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
    const summarizeTerm = ceilDiv(files, effectiveBucketSize(L)) + 1;
    if (summarizeTerm > SUMMARIZE_TERM_BOUND) {
      // Double from the EFFECTIVE size (pinning a concrete value): doubling from a raw
      // absent field (|| 1 -> 2) would LOWER the effective bucket below the stage's
      // default of 20 and move the term the wrong way.
      L.summarizeBucketSize = effectiveBucketSize(L) * 2;
      continue;
    }
    const verifyTerm = ceilDiv(findings, effectiveSliceSize(L, findings));
    const validateTerm = ceilDiv(findings, effectiveBatchSize(L, findings));
    const challengeTerm = Math.min(findings, effectiveChallengeCap(L, findings));
    if (validateTerm >= verifyTerm && validateTerm >= challengeTerm) {
      L.validateBatch = effectiveBatchSize(L, findings) * 2;
    } else if (verifyTerm >= validateTerm && verifyTerm >= challengeTerm) {
      L.verifySliceSize = effectiveSliceSize(L, findings) * 2;
    } else {
      // Halve the EFFECTIVE cap (min(cap, findings)) so C strictly decreases even when
      // the nominal cap already exceeds nFindings — or is absent (= findings).
      L.challengeCap = Math.max(CHALLENGE_CAP_FLOOR, Math.floor(Math.min(effectiveChallengeCap(L, findings), findings) / 2));
    }
  }
  return L;
}

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
        },
        required: ['finding_id', 'confidence'],
      },
    },
  },
  required: ['validations'],
};

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

  const model = modelFor('code-gauntlet:validator', policy);

  const batches = [];
  for (let i = 0; i < findings.length; i += batchSize) batches.push(findings.slice(i, i + batchSize));

  const thunks = batches.map((batch, idx) => () => c.agent(validatePrompt(inp, batch), {
    label: `validate-batch-${idx}`,
    agentType: 'code-gauntlet:validator',
    model,
    schema: VALIDATE_SCHEMA,
  }));

  const results = await c.parallel(thunks);

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
  const ctxLine = inp.contextPath
    ? `Read the shared context at ${inp.contextPath} first — it has the diff, project rules, and risk classification. `
    : '';
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
  return `${ctxLine}Independently validate this batch of findings. For each, Read the code at the file and line range shown, attempt to disprove the claim, and score it. Findings:\n${block}\nReturn { validations: [{ finding_id, confidence, justification }] } — confidence 0-100 (one entry per finding you scored; omit the rest).`;
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

  const model = modelFor('code-gauntlet:challenger', policy);

  // Rank first so the cap, when it bites, challenges the HIGHEST-priority findings;
  // the lower-ranked overflow is skipped (routed to `unverified`, never dropped).
  const ranked = rankFindings(findings);
  const candidates = ranked.slice(0, cap);
  const overflow = ranked.slice(cap);

  const thunks = candidates.map((finding, idx) => () => c.agent(challengePrompt(finding), {
    label: `challenge-${idx}`,
    agentType: 'code-gauntlet:challenger',
    model,
    schema: CHALLENGE_SCHEMA,
  }));

  const results = thunks.length ? await c.parallel(thunks) : [];

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
  const ranked = rankFindings(pool);
  if (cap === undefined || cap === null) return ranked;
  return ranked.slice(0, Math.max(0, cap));
}

// --- Phase 8: Report --------------------------------------------------------

const REPORT_SCHEMA = { type: 'object', properties: { report: { type: 'string' } }, required: ['report'] };

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
// SEGMENT_CHAR_BUDGET the findings are chunked and one report-writer is
// dispatched PER chunk (sequentially, each with the same try/catch), then the
// per-chunk reports are concatenated under titled segment headings. Any single
// chunk that fails degrades to its own minimal section — the rest still render.
export async function reportStage(ctx, input) {
  const c = ctx || defaultCtx();
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const policy = inp.policy || {};
  const model = modelFor('code-gauntlet:report-writer', policy);

  const findings = inp.findings || [];
  const oversized = JSON.stringify(findings).length > SEGMENT_CHAR_BUDGET;
  if (!oversized) {
    return dispatchReportSegment(c, model, inp, findings, null);
  }

  // Segment: one dispatch per chunk, sequentially, titled sections joined.
  const chunks = chunkBySerializedSize(findings, SEGMENT_CHAR_BUDGET);
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
    const result = await c.agent(reportPrompt(segInp, seg), {
      label: seg ? `report-writer-${seg.index}` : 'report-writer',
      agentType: 'code-gauntlet:report-writer',
      model,
      schema: REPORT_SCHEMA,
    });
    if (!result || !result.report) {
      return { report: minimalReport(segInp), gaps: [`report${tag}: writer returned no report — assembled a minimal report from pipeline stats`] };
    }
    return { report: result.report, gaps: [] };
  } catch (e) {
    return { report: minimalReport(segInp), gaps: [`report${tag}: writer agent threw (${(e && e.message) || 'unknown'}) — assembled a minimal report from pipeline stats`] };
  }
}

// Deterministic fallback report (no agent, no wall-clock) built from what the
// pipeline already knows. Never throws — this is the last-resort degradation.
function minimalReport(inp) {
  const findings = inp.findings || [];
  const unverified = inp.unverified || [];
  const lines = [
    '# Code Gauntlet (minimal report)',
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
  return `${ctxLine}${segLine}Write the code-gauntlet report as markdown for these results. Put high-confidence findings in the main section and unverified/pipeline-degraded findings in a clearly-labelled secondary section. Results JSON:\n${body}\nReturn { report } where report is the full markdown document.`;
}

// --- Persistence: writeArtifacts --------------------------------------------

const WRITER_SCHEMA = {
  type: 'object',
  properties: { artifactPaths: { type: 'object', properties: {} } },
};
// The verify slice-input writer returns the list of paths it wrote.
const WRITTEN_SCHEMA = {
  type: 'object',
  properties: { written: { type: 'array', items: { type: 'string' } } },
};

// The four artifacts writeArtifacts plans (and asks the writer to echo). Exported so a
// faithful mock/recorder echoes the SAME paths the write-proof gate checks against — the
// gate rejects any echo that fails to account for all four planned paths.
export function plannedArtifactPaths(outputDir, sha) {
  return {
    findings: `${outputDir}/code-gauntlet-findings-${sha}.json`,
    report: `${outputDir}/code-gauntlet-report-${sha}.md`,
    postReview: `${outputDir}/code-gauntlet-post-review-${sha}.json`,
    checkpoints: `${outputDir}/${checkpointPath('all', sha)}`,
  };
}
const ARTIFACT_PATH_KEYS = ['findings', 'report', 'postReview', 'checkpoints'];

// Write-proof: the echoed artifactPaths must account for EVERY planned path (each key
// present and echoing the exact path we dispatched). WRITER_SCHEMA declares no `required`,
// so an empty {} echo is schema-valid — a writer under StructuredOutput retry pressure can
// return one having written nothing. Same threat model as trustSlice: a self-reported echo
// is a consistency/liveness check, not proof-of-write, but requiring the four exact paths
// stops a degenerate {} (or a partial echo) from passing as a full persist.
function writerEchoCoversPaths(echoed, paths) {
  if (!echoed || typeof echoed !== 'object') return false;
  return ARTIFACT_PATH_KEYS.every((k) => echoed[k] === paths[k]);
}

// writeArtifacts(ctx, { findings, postReview, report, checkpoints, outputDir,
// headShaShort, policy }) -> { artifactPaths, gaps, partial }
// The workflow script has NO disk access, so a writer agent persists findings.json
// + report.md + the checkpoint/progress JSON to {output_dir}; the content is carried
// BY VALUE in the dispatch prompt. Wrapped in its own try/catch (like reportStage):
// a throw OR null result degrades to a partial-artifacts gap with null paths and is
// NON-FATAL — it never bubbles to the top-level catch.
export async function writeArtifacts(ctx, input) {
  const c = ctx || defaultCtx();
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const outputDir = inp.outputDir || '.code-gauntlet';
  const sha = inp.headShaShort || 'head';
  const policy = inp.policy || {};
  const paths = plannedArtifactPaths(outputDir, sha);
  const model = modelFor('code-gauntlet:artifact-writer', policy);
  const partial = (reason) => ({
    artifactPaths: { findings: null, report: null, postReview: null, checkpoints: null },
    gaps: [`writeArtifacts: ${reason} — artifacts not persisted (partial-artifacts)`],
    partial: true,
  });
  try {
    const result = await c.agent(writeArtifactsPrompt(inp, paths), {
      label: 'artifact-writer',
      agentType: 'code-gauntlet:artifact-writer',
      model,
      schema: WRITER_SCHEMA,
    });
    if (!result) return partial('writer returned null');
    if (!writerEchoCoversPaths(result.artifactPaths, paths)) {
      return partial('writer echo did not account for all four planned artifact paths (no write proof)');
    }
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

// The by-value payload the writer agent persists. findings/postReview are aliased to the
// union schema so the persisted JSON is consumable by BOTH boundary scripts unchanged.
// `postReview` is the deterministic delivery set (selectDelivery output): every
// challenge-survivor, ranked and capped, each carrying its report_tag — persisted so Phase 8
// posts it verbatim without re-selecting. The pipeline-degraded `unverified` bucket is NOT
// carried here: it is persisted to no file (findings.json is the high-confidence set only),
// the report already renders it, and the slimmed checkpoint's challenge entry carries it for
// resume — so re-sending it in the writer prompt was dead by-value weight (each finding-scale
// piece now crosses the writer prompt exactly once). Pure + exported so tests (and the node
// recorder) can assert the persist output is REAL pipeline output, not a hand-authored fixture.
export function writerPayload(inp) {
  return {
    findings: (inp.findings || []).map(toV2Aliased),
    postReview: (inp.postReview || []).map(toV2Aliased),
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
  return `Persist these code-gauntlet artifacts to disk exactly as given (the workflow has no disk access). Write the payload's findings (as pretty JSON) to ${paths.findings}, the payload's report (verbatim markdown) to ${paths.report}, the payload's postReview (the pre-selected delivery set, as pretty JSON) to ${paths.postReview}, and the payload's checkpoints (as JSON) to ${paths.checkpoints}. Return { artifactPaths } echoing the paths you wrote. The payload is the single JSON line after the marker below.\n${WRITER_PAYLOAD_MARKER}${payload}`;
}

// --- Checkpoints ------------------------------------------------------------

// checkpointPath(phase, sha) -> bare filename for a phase's persisted checkpoint.
// The skill layer reads these on a rerun and injects the recovered outputs into
// the args waist (args.checkpoints); the pipeline has no disk access of its own.
export function checkpointPath(phase, sha) {
  return `code-gauntlet-checkpoint-${phase}-${sha}.json`;
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

// buildResumeCheckpoints(phaseOutputs) -> resume state for a FAILURE-path return.
// Carries the full per-phase outputs map ({ phases, completed }) so the skill can resume
// from the compact return when nothing was persisted — UNLESS that map would exceed the
// char budget, in which case only the completed-phase NAMES are returned with
// truncated:true (resume then falls back to re-running rather than shipping findings bulk
// through the compact return). readCheckpoints unwraps the .phases form directly.
export function buildResumeCheckpoints(phaseOutputs) {
  const completed = Object.keys(phaseOutputs);
  const withPhases = { phases: phaseOutputs, completed };
  if (JSON.stringify(withPhases).length <= SEGMENT_CHAR_BUDGET) return withPhases;
  return { completed, truncated: true };
}

// Phases whose FULL output a resume from the PERSISTED (successful-run) checkpoint actually
// consumes. Traced from runWith's post-challenge tail: `challenge` carries the delivered
// high-confidence findings/unverified that selectDelivery, the report input, and
// writeArtifacts read by value; `filter` carries the post-filter set the empty-report guard
// counts (postFilterCount). Every OTHER phase contributes only a count/stat to the final
// envelope on a resume (discovered/merged/verified/validate.stats/filter.stats), never its
// findings bulk — so those re-run on resume rather than being carried by value.
const PERSISTED_RESUME_PHASES = ['filter', 'challenge'];

// phaseFindingCount(out) -> the count summarizing one phase's output for the slim checkpoint
// (findings-bearing stages carry `findings`; the filter stage carries `filtered`).
function phaseFindingCount(out) {
  if (!out || typeof out !== 'object') return 0;
  if (Array.isArray(out.findings)) return out.findings.length;
  if (Array.isArray(out.filtered)) return out.filtered.length;
  return 0;
}

// slimPersistedCheckpoints(phaseOutputs, completed, phaseReached) -> the checkpoint artifact
// the writer persists at the end of a successful run. Only the resume-consumed phases
// (PERSISTED_RESUME_PHASES) keep their FULL output; every phase additionally records a bare
// count. This drops the by-value duplication where the OLD persisted checkpoint carried every
// phase's full findings array (discover/merge/verify/validate each ~a full findings set) inside
// the single artifact-writer prompt. readCheckpoints unwraps `.phases`, so a resume from this
// artifact skips exactly the preserved phases (reusing the delivered findings verbatim) and
// re-runs the rest. The in-memory failure-path resume (buildResumeCheckpoints) is intentionally
// NOT slimmed — a crash-recovery resume still carries every phase's full output for a fast skip.
export function slimPersistedCheckpoints(phaseOutputs, completed, phaseReached) {
  const outputs = phaseOutputs || {};
  const phases = {};
  for (const name of PERSISTED_RESUME_PHASES) {
    if (outputs[name] !== undefined) phases[name] = outputs[name];
  }
  const counts = {};
  for (const [name, out] of Object.entries(outputs)) counts[name] = phaseFindingCount(out);
  return { phases, completed, phaseReached, counts };
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
  // Agent-count guard: coarsenLimits is applied at the two points its inputs exist.
  // The changed-file count is known at entry (bounds the summarize term); the finding
  // count exists only after merge, where the verify/validate/challenge terms get
  // re-coarsened. At or below benchmark scale the worst case sits far under the guard,
  // so both calls return the limits values unchanged.
  const nChangedFiles = (A.changedFiles || []).length;
  let limits = coarsenLimits(A.limits || {}, nChangedFiles, 0);
  const policy = A.policy || {};
  const contextPath = `${A.outputDir}/code-gauntlet-context-${A.headShaShort}.md`;
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

    // The finding count now exists — re-coarsen so verify slices, validate batches,
    // and the challenge cap keep the remaining worst-case fan-out under the guard.
    limits = coarsenLimits(limits, nChangedFiles, (mergeOut.findings || []).length);

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

    // Deterministic delivery selection: the challenge-survivors filtered by the user-chosen
    // delivery TIER (args.delivery.tier — 'all' by default, 'main_only' to withhold
    // suggestions), rank-ordered and capped at limits.deliveryCap (fed from
    // CODE_GAUNTLET_PR_COMMENT_CAP by the skill). Persisted so Phase 8 posts it verbatim — the
    // live agent never re-filters or re-ranks. Challenge-removed (challengeOut.eliminated) and
    // challenge-skipped (challengeOut.unverified) are already absent here, so they stay excluded.
    const deliveryTier = A.delivery && A.delivery.tier;
    const postReview = selectDelivery(challengeOut.findings, limits.deliveryCap, deliveryTier);

    const reportInput = {
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
    };
    let reportOut = await runPhase('report', () => reportStage(c, reportInput));
    gaps.push(...(reportOut.gaps || []));

    // Empty-report guard (false-negative defense). A report that is empty/absent while
    // findings survived the filter is a false negative — most often a RESUME replaying the
    // degenerate empty-report stub a crashed run left in its checkpoint. Never ship or
    // persist it silently:
    //   1) if it came from a replayed checkpoint, re-run report from scratch (a resume must
    //      re-run report+persist, not skip past the crashed stub); and
    //   2) if it is STILL empty with findings present, keep ok:true but record an explicit
    //      'empty_report' gap and null the report artifact path — never a silent empty report.
    const postFilterCount = (filterOut.filtered || []).length;
    const reportIsEmpty = (out) => !out || typeof out.report !== 'string' || out.report.trim() === '';
    if (reportIsEmpty(reportOut) && postFilterCount > 0 && checkpoints.report !== undefined) {
      reportOut = await reportStage(c, reportInput);
      phaseOutputs.report = reportOut;
      gaps.push(...(reportOut.gaps || []));
    }
    const emptyReport = reportIsEmpty(reportOut) && postFilterCount > 0;
    if (emptyReport) {
      gaps.push(`empty_report: report stage produced no report while ${postFilterCount} finding(s) survived the filter — refusing to ship a silent empty report`);
    }

    // Persistence is a post-phase step: writeArtifacts owns its try/catch, so a
    // writer failure degrades to a partial-artifacts gap rather than the top-level catch.
    const writeOut = await writeArtifacts(c, {
      findings: challengeOut.findings,
      postReview,
      report: reportOut.report,
      // Persist a SLIM checkpoint: only the resume-consumed phases (filter, challenge) carry
      // full output; every other phase is reduced to a count, so the single artifact-writer
      // prompt no longer duplicates every phase's findings bulk by value. readCheckpoints
      // unwraps .phases, so a resume skips exactly the preserved phases and re-runs the rest.
      // The in-memory failure-path return below still carries the full phaseOutputs map.
      checkpoints: slimPersistedCheckpoints(phaseOutputs, completed, phaseReached),
      outputDir: A.outputDir,
      headShaShort: A.headShaShort,
      generatedAt: A.generatedAt,
      policy,
    });
    gaps.push(...(writeOut.gaps || []));
    // On an empty report the findings/checkpoints still persist, but the report path is
    // nulled so no consumer mistakes an empty stub for a real review (envelope contract).
    if (emptyReport && writeOut.artifactPaths) writeOut.artifactPaths = { ...writeOut.artifactPaths, report: null };

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
      },
      // On persist success the resume state lives in artifactPaths.checkpoints — the
      // compact return carries only phase NAMES (never the findings bulk). If the writer
      // FAILED nothing was persisted, so carry the in-memory resume state (phases, or
      // names+truncated when it would exceed the budget) so the skill can still resume.
      checkpoints: writeOut.partial ? buildResumeCheckpoints(phaseOutputs) : { completed },
      gaps,
    };
  } catch (e) {
    // Nothing was persisted on the throw path either — carry the in-memory resume state
    // (bounded by the char budget) in the compact return so the skill can resume the
    // failed run rather than restarting from scratch.
    return {
      ok: false,
      error: (e && e.message) || String(e),
      phaseReached,
      artifactPaths: {},
      stats: {},
      checkpoints: buildResumeCheckpoints(phaseOutputs),
      gaps,
    };
  }
}
