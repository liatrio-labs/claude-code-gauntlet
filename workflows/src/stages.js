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
import { DIMENSIONS, AGENTS, resolvePolicy, FINDING_PROP_TYPES, FINDING_REQUIRED } from './registry.js';
import { merge } from './mergeFindings.js';
import { applyValidations, pyIntStrict } from './applyValidations.js';
import { applyFilterPipeline } from './filterFindings.js';
import { applyChallenges, rankFindings, deepClone } from './applyChallenges.js';
import { normalizeArgsReport, nullToleranceGap, nullToleranceRejectedKeys, validateArgs, entryArgs, makeArgsRejectEnvelope, SKILL_RECOVERY_LINE } from './args.js';

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

// --- Shared context file: the deterministic read plan (issue #48) ---------------
//
// A `Read` returns only PART of a large file and says nothing about it. Measured on
// run wf_cef39739-577: all 7 discovery agents' first Read of the 95,057-byte /
// 2,028-line shared context file returned 58,145 chars ending at line 1083, with no
// truncation notice in ANY of the 7 tool results. Six agents inferred the cutoff and
// paginated on; security-reviewer did not, and reviewed roughly the first half of the
// diff while reporting as if it had seen all of it.
//
// The fix is arithmetic, not instruction. The skill stamps the file's size
// (contextLines/contextChars, measured after it writes the file); these helpers turn
// that into the EXACT list of Read calls that covers it, and the prompt enumerates
// them. The agent's job is "make these listed calls", not "notice an invisible
// truncation and drive a loop" — a judgment it demonstrably gets wrong 1 time in 7.
//
// Both bounds below are observed platform behavior, not a documented contract, so the
// planner stays well under each: the Read tool documents a 2,000-line default cap, and
// the run above puts a character cap near 58,000. A plan chunk is sized by whichever
// binds first, from the stamped chars-per-line.
const READ_PLAN_MAX_LINES = 750;
const READ_PLAN_MAX_CHARS = 30000;
// Above this many chunks, the prompt states the plan as its generating rule (first
// offset, span, final line) instead of listing every call — identical information,
// bounded prompt size. Four chunks covers the profiled file; the rule form is for
// pathological inputs (very many very short lines), never the normal path.
const READ_PLAN_MAX_ENUMERATED = 10;
// Hard ceiling on the plan's SIZE, checked before a single element is allocated. Without
// it the loop below allocates one object per chunk for whatever `lines` it is handed, and
// the waist accepts any positive safe integer: contextLines = Number.MAX_SAFE_INTEGER
// OOM-kills the node process — a V8 fatal error, not a catchable throw, so runWith's
// top-level catch never runs, no gap is recorded, and nothing is dispatched. 2000 chunks
// covers a 1.5M-line context file; past that the plan degrades to the count-free
// read-to-end wording, because a review context that large is already beyond triage.
const READ_PLAN_MAX_CHUNKS = 2000;

// contextReadPlan(lines, chars) -> [{ offset, limit }, ...] covering lines 1..lines.
// PURE arithmetic. Offsets are 1-based to match the Read tool's `cat -n` numbering
// (the profiled agents' own follow-up Reads used 1-based offsets). An unusable or
// absent line count yields [] — the caller then falls back to the count-free wording
// rather than emitting a plan built from a guess.
export function contextReadPlan(lines, chars) {
  const total = Number.isSafeInteger(lines) && lines > 0 ? lines : 0;
  if (total === 0) return [];
  // Chars is advisory: absent/unusable just means the line cap binds alone.
  const perLine = Number.isSafeInteger(chars) && chars > 0 ? chars / total : 0;
  const byChars = perLine > 0 ? Math.floor(READ_PLAN_MAX_CHARS / perLine) : READ_PLAN_MAX_LINES;
  const span = Math.max(1, Math.min(READ_PLAN_MAX_LINES, byChars));
  // Bound the ALLOCATION, not just the input: computed before the loop so an absurd size
  // degrades instead of exhausting the heap (see READ_PLAN_MAX_CHUNKS).
  if (Math.ceil(total / span) > READ_PLAN_MAX_CHUNKS) return [];
  const plan = [];
  for (let offset = 1; offset <= total; offset += span) {
    plan.push({ offset, limit: Math.min(span, total - offset + 1) });
  }
  return plan;
}

// sharedContextLine(inp) -> the leading context-read sentence, or '' when no context
// path was threaded. Called ONCE, by runWith, which threads the resulting STRING to the
// stages that need it. The stages never receive the path, so no stage is ABLE to build a
// context-read instruction of its own — the property that used to be defended by scanning
// this file's source text for hand-rolled copies. Capability removed rather than guarded:
// an adversarial review defeated the text-scan version in one edit by rewording.
export function sharedContextLine(inp) {
  const path = inp && inp.contextPath;
  if (!path) return '';
  const head = `Read the shared context at ${path} first — it has the diff, project rules, and risk classification.`;
  // A partial Read is INVISIBLE: it carries no truncation notice and looks exactly
  // like a complete file. Stated on every path, plan or no plan.
  const warn = 'A single Read of this file returns only PART of it and gives NO truncation notice, so one Read is never the whole file.';
  const prefix = `${head} ${warn}`;
  const plan = contextReadPlan(inp.contextLines, inp.contextChars);
  if (plan.length === 0) {
    // No measurement (or one too large to plan): the terminus is unknown, so end-detection
    // is unavoidable here. The STEPPING still is not — spelling out fixed-size steps keeps
    // the only judgment "did that call return anything", instead of the open-ended "have I
    // read enough yet" that issue #48 records an agent answering wrong.
    const s = READ_PLAN_MAX_LINES;
    return `${prefix} Read it in ${s}-line steps — Read(offset=1, limit=${s}), then Read(offset=${1 + s}, limit=${s}), then Read(offset=${1 + 2 * s}, limit=${s}), continuing to step by ${s} until a call returns no further content. Do not stop before that. `;
  }
  const total = inp.contextLines;
  if (plan.length === 1) {
    return `${prefix} It is ${total} lines: read it with Read(offset=1, limit=${plan[0].limit}), and if that call does not reach line ${total}, continue with offset set past the last line you received until it does. `;
  }
  const tail = `Make ALL of them — stopping early means you review only the part you saw. If any call does not land where the plan says, continue with offset set past the last line you received until you reach line ${total}.`;
  if (plan.length <= READ_PLAN_MAX_ENUMERATED) {
    const calls = plan.map((p) => `Read(offset=${p.offset}, limit=${p.limit})`).join(', ');
    return `${prefix} It is ${total} lines, which takes exactly ${plan.length} Read calls: ${calls}. ${tail} `;
  }
  const span = plan[0].limit;
  return `${prefix} It is ${total} lines, which takes exactly ${plan.length} Read calls of limit=${span}, at offsets 1, ${1 + span}, ${1 + 2 * span}, … stepping by ${span} through line ${total}. ${tail} `;
}

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
      const mergeResult = await c.agent(summarizeMergePrompt(inp, partials), {
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
  // Prebuilt by runWith (issue #48): the summarizer opens the SAME file the discovery
  // agents do, so it gets the same read plan. This stage cannot construct one — it never
  // sees the context path.
  const ctxLine = inp.contextLine || '';
  // Single-source the size number (live-run L7): triage said 1211 changed lines, the
  // report said ~1218 because the summarizer re-derived it from the diff. The waist's
  // changedLines is the one authoritative count.
  const countLine = typeof inp.changedLines === 'number' && inp.changedLines > 0
    ? ` The authoritative changed-line count is ${inp.changedLines}; cite that number verbatim if you mention change size — never re-estimate it from the diff.`
    : '';
  return `${ctxLine}Summarize the semantic intent of these changed files for downstream reviewers: ${files.join(', ')}.${countLine} Return { summary }.`;
}

// The merge call produces the FINAL summary on the bucketed path, so it needs the same
// changedLines pin as summarizePrompt — without it the merge step can re-drift the size
// number the per-bucket prompts were pinned to (Bugbot PR-20 wave 2).
function summarizeMergePrompt(inp, partials) {
  const joined = partials.map((p) => p.summary || '').filter(Boolean).join('\n---\n');
  const countLine = typeof inp.changedLines === 'number' && inp.changedLines > 0
    ? ` The authoritative changed-line count is ${inp.changedLines}; cite that number verbatim if you mention change size — never re-estimate it.`
    : '';
  return `Combine these per-bucket change summaries into one concise semantic summary.${countLine} Partials:\n${joined}\nReturn { summary }.`;
}

// --- Phase 2: Discover ------------------------------------------------------

// Group DIMENSIONS by agentType, unioning each agent's per-dimension schemaExtra into
// one finding schema. One task per unique AGENT (7) — agents covering several
// dimensions (conventions-and-intent -> convention/intent/comment_accuracy) dispatch once.
export function agentSpecs() {
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

// An agent is active when at least one of its dimensions is enabled. A dimension is
// enabled when its conditionalFlag is null/undefined (UNGATEABLE — always on, e.g. the
// core bug/security dimensions) OR its agentFlags entry is not the literal `false`.
// OPT-OUT semantics: a MISSING key counts as enabled, so absent/empty agentFlags leaves
// every dimension on — byte-identical to the pre-flag behavior where all flags were null.
// Only an explicit `false` (stamped by a light-scope run, e.g. { deep: false }) disables.
export function agentActive(spec, agentFlags) {
  const flags = agentFlags || {};
  return spec.conditionalFlags.some((flag) => flag === null || flag === undefined || flags[flag] !== false);
}

// The canonical finding ITEM schema (one array element). Declared IN FULL — every
// canonical property with a concrete type, `description` among them — everywhere an agent
// returns findings BY VALUE. An items schema of `{ type:'object', properties:{} }` is the
// trap: StructuredOutput leaves an empty-properties object UNCONSTRAINED, so a model
// transcribing findings back "verbatim via the schema" is free to drop the single largest
// field — `description` — which is exactly what the verify executor did, emptying
// descriptions for every downstream stage (validate/filter/challenge) and false-firing the
// filter's short-description injection guard on high-confidence findings.
//
// An UNDECLARED property is dropped by the same mechanism, silently and by design — which is
// why FINDING_PROP_TYPES and every dimension's `schemaExtra` live together in registry.js and
// are pinned against the agent .md output contracts by tests/test_dimensions_registry.py
// (issue #47: `suggestion`/`claude_md_rule`/`spec_text`/`criticality`/`failure_scenario` were
// instructed by the contracts, declared by nothing, and dropped at this boundary on every run).
// Entries are EITHER a type-name shorthand string ({ k: 'string' } -> { type:'string' }) OR a
// full JSON-Schema fragment used verbatim (how array-valued fields like cross_file_refs and
// affected_consumers are declared); the shorthand keeps the common case terse while the
// fragment form supports arrays the platform's schema validator requires `items` on.
// schemaExtra wins on a key collision — a dimension may narrow a canonical field, never the
// reverse — and `required` is always the flat FINDING_REQUIRED (see its note in registry.js).
function findingItemSchema(schemaExtra) {
  const props = {};
  for (const [k, t] of Object.entries({ ...FINDING_PROP_TYPES, ...(schemaExtra || {}) })) {
    props[k] = typeof t === 'string' ? { type: t } : t;
  }
  return { type: 'object', properties: props, required: FINDING_REQUIRED };
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
  const ctxLine = inp.contextLine || '';
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

// The canonical key order of one delta, and the ONLY keys that carry meaning. Both the
// dispatch schema and the checksum canonicalisation are built from this one list, so a
// field added to the delta cannot be declared in one place and forgotten in the other.
// It mirrors verify_findings.py's `_DELTA_FIELDS` plus the two structural keys; the
// script's audit comment is the authority for why the set is exactly this.
const DELTA_KEYS = ['id', 'verified', 'origin', 'severity', 'confidence', 'elimination_reason'];

// The discriminated-union envelope the executor returns. Both shapes coexist so an
// honest failure is schema-valid — the executor never fabricates a success under
// StructuredOutput retry pressure ({status:'failed'} is a legal answer).
//
// `result.deltas` REPLACED the by-value verified/eliminated finding arrays (#25 req 1).
// The workflow already holds every dispatched finding, so the only thing the executor can
// tell it is what the SCRIPT decided; re-transcribing the findings themselves bought
// nothing and cost everything — 22.95s of one profiled executor's 31.9s generation, a
// 1,990-byte schema (the file's largest) dispatched per slice, and the entire surface on
// which a live run's 10-verified/0-eliminated disk result came back as a 7/3 echo under a
// valid receipt. The item shape is deliberately flat and typed: six scalars, no nested
// objects, no per-dimension extras to union, nothing whose absence can silently empty a
// finding field downstream (the `description`-stripping class this schema used to enable).
//
// `agent` is not declarable here because no finding is echoed at all any more — the
// withholding that used to depend on leaving one property out of a finding schema is now
// structural. joinVerifyDeltas strips it from the findings it emits, and a test fails if a
// joined finding is ever filter-visibly tagged (V3.1 item 4 was reverted for exactly this:
// deterministic agent identity past verify moved mini-subset A's dedup eliminations
// 7 -> 33 and same-6 recall 20/30 -> 13/30; it re-lands only with #22's redesign).
const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    status: { type: 'string' }, // 'ok' | 'failed'
    receipt: {
      type: 'object',
      properties: {
        sha: { type: 'string' },
        n_in: { type: 'number' },
        nonce: { type: 'string' },
        // The delta echo's content proof (fnv1a32 over the script's own serialisation).
        // Optional in the SCHEMA, mandatory in trustSlice — an absent proof is a legal
        // thing for the executor to say and an untrusted thing for the workflow to act on.
        deltas_checksum: { type: 'string' },
      },
    },
    result: {
      type: 'object',
      properties: {
        deltas: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              id: { type: 'string' },
              verified: { type: 'boolean' },
              origin: { type: 'string' },
              severity: { type: 'string' },
              confidence: { type: 'number' },
              elimination_reason: { type: 'string' },
            },
            required: ['id', 'verified'],
          },
        },
      },
    },
    exitCode: { type: 'number' },
    stderr: { type: 'string' },
  },
  required: ['status'], // discriminated union: receipt/result only present on status:'ok'
};

// Dispatches per verify slice: the first executor call plus EXACTLY ONE deterministic
// re-dispatch when that call comes back untrusted (verifySliceWithRetry).
//
// It is a named constant because two independent things must agree with the retry
// verifySliceWithRetry actually performs: worstCaseAgentCount's verify term (a guard that
// undercounts the worst case is a guard that does not hold) and the tests that pin the
// dispatch count. Exported so those tests assert against THIS value rather than a
// hand-copied literal — raise it here without touching verifySliceWithRetry and the
// dispatch-count test fails instead of the guard silently over-counting.
export const VERIFY_ATTEMPTS_PER_SLICE = 2;

// verifyStage(ctx, input) -> { findings, verified: boolean, gaps }
// Slices findings into limits.verifySliceSize chunks and dispatches an `executor` agent
// per slice — one call, plus at most one retry (below) — SEQUENTIALLY (not parallel())
// so each envelope pairs to its slice by order. Each executor runs the pinned
// verify_findings.py receipt command and returns VERIFY_SCHEMA: the receipt plus a
// per-finding DELTA, never the findings themselves (#25 req 1). A slice is TRUSTED only
// when status==='ok', the receipt echoes the dispatched nonce and head sha and n_in, the
// deltas cover exactly the ids this slice dispatched, and their content proof matches
// (trustSlice). The verified findings are then rebuilt HERE, by joining the delta onto
// the findings this stage already holds by value — the executor's transcription is no
// longer in the data path at all, only in the decision path.
//
// DEGRADATION IS PER SLICE (issue #54, and issue #25 requirement 3). An untrusted slice
// — receipt mismatch, status:'failed', an agent() throw, or a slice whose --input file
// was never provably written — degrades ONLY ITS OWN findings to the UNVERIFIED shape
// (origin='unknown', surfaced-classification skipped) and the loop keeps going; slices
// that verify cleanly keep their verified output. This replaced an all-or-nothing
// `break` under which one transient hiccup on slice 0 cost EVERY slice its
// classification: measured live on 2026-07-27 (smoke-20260727-205454-f99d948), a single
// dropped nonce echo marked all 16 findings of a one-slice PR origin=unknown. The size
// of the damage now tracks the size of the fault.
//
// Findings are never dropped and success is never fabricated, now at slice granularity:
// against every failure class this stage DETECTS, every finding leaves this stage either
// as its slice's trusted verified output or as itself with origin='unknown' — never
// missing, never silently upgraded. `verified` is true only when ZERO slices degraded, so
// the one top-level boolean keeps meaning "this whole run's classification is trustworthy".
//
// ("this stage", not "trustSlice": an agent() throw is caught in dispatchVerifySlice and a
// missing slice input in materializeVerifySlices — trustSlice never sees either.)
//
// The never-drop half is now structural rather than merely intended (#25 req 2, which
// removed the qualifier that used to stand here). The findings this stage emits on the
// trusted path are the ones IT dispatched — joinVerifyDeltas walks the dispatched slice
// and enriches it — so no echo, however wrong, can substitute or delete a finding: the
// worst an untrusted echo achieves is its own slice's honest degrade. The previous design
// pushed the findings themselves through the executor, where an envelope carrying a
// same-length SIBLING slice's findings satisfied every guard and the real slice's findings
// were dropped outright while the run reported verified=true (reproduced end to end during
// the #54 review). That class is closed here, not mitigated.
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
  // the payload is large) write them by value. A writer GROUP that fails takes only the
  // slices IT carried to the UNVERIFIED path — never fabricate a verification for a
  // slice whose input is not provably on disk, and never punish the slices whose input is.
  const materialized = await materializeVerifySlices(c, inp, slices, policy);

  const out = [];
  const gaps = [];
  let degradedSlices = 0;

  // The output is assembled in SLICE-INDEX order — trusted output and degraded originals
  // alike — because downstream ranking (applyChallenges' stable sort on severity then
  // confidence) breaks ties by array position. Completion order or "trusted first" would
  // make delivery ordering vary run to run for tied findings.
  for (let i = 0; i < slices.length; i += 1) {
    const slice = slices[i];
    const degrade = (detail) => {
      out.push(...degradedSlice(slice));
      gaps.push(verifyDegradeGap(detail, slice.length, findings.length));
      degradedSlices += 1;
    };

    // No write proof for this slice's --input file: the executor would read a file that
    // may not exist, so it is never dispatched. Only this slice degrades.
    const unwritten = materialized.failed.get(i);
    if (unwritten !== undefined) {
      degrade(`slice ${i} (slice-input group ${unwritten.group}): ${unwritten.reason}`);
      continue;
    }

    // The delta echo is keyed by finding id, so a slice whose own findings have no usable
    // id set cannot be joined no matter how faithfully the executor answers. Checked
    // BEFORE dispatch: spending an executor on an answer this stage could not use is
    // strictly worse than degrading now. Degrade-and-disclose rather than reject, so a
    // merge-side id regression costs one slice its classification and says so, instead of
    // failing the run (post-merge ids are present by construction — mergeFindings drops
    // id-less findings — which is why this is a guard, not a routine path).
    const ids = dispatchableIds(slice);
    if (!ids.ok) {
      degrade(`slice ${i}: ${ids.reason} — the delta echo is keyed by id, so this slice cannot be verified`);
      continue;
    }

    const attempt = await verifySliceWithRetry(c, inp, i, slice, { model, nonce, headShaShort, ids: ids.ids });
    if (!attempt.ok) {
      degrade(`slice ${i}: ${attempt.reason}`);
      continue;
    }
    // Trusted: this slice's OWN findings, enriched by the script's delta (origin
    // new/surfaced, the surfaced severity downgrade, the factual-verification confidence
    // re-score). Everything the script did not touch — description, evidence,
    // cross_file_refs, suggestion, every per-dimension extra — is the value this stage
    // already held, so it cannot be dropped or mangled in transcription. Findings the
    // script really eliminated are absent by design (their delta says verified:false, and
    // trustSlice requires the script's elimination stamp on every one of them).
    out.push(...attempt.verified);
    if (attempt.gap) gaps.push(attempt.gap);
  }

  return { findings: out, verified: degradedSlices === 0, gaps };
}

// One slice's share of the UNVERIFIED degradation: its ORIGINAL findings re-emitted with
// origin='unknown' (surfaced-classification skipped). Nothing is dropped and nothing is
// upgraded. Numeric-string fields are pinned here for the same reason they are pinned on
// the slice-input path: the trusted path returns the script's re-scored numbers, but this
// path re-emits discovery-shaped findings whose confidence is the schema's numeric STRING
// ("85") — leaked downstream, the filter's consensus `+` boost concatenates ("85" + 10 ->
// "8510" -> clamped to 100).
function degradedSlice(slice) {
  return slice.map((f) => ({ ...pinNumericFields(f), origin: 'unknown' }));
}

// The loud gap for one degraded slice. `detail` names the slice (and, for a write
// failure, the writer group) and carries the underlying reason, so a reader can tell
// WHICH share of the run lost its classification and why; `k of n` states the blast
// radius directly, which is the whole point of per-slice degradation — a run that
// degrades 2 of 16 findings must not read the same as one that degrades all 16.
//
// The UNVERIFIED token is load-bearing: tests and the bench checker's degrade scan key
// on substrings of this string, and the underlying reason is passed through VERBATIM (a
// write-proof failure's "(no write proof)" is exactly what bench/runner/check.py's
// _DEGRADE_RE matches — paraphrasing it would silently disable that detection).
function verifyDegradeGap(detail, k, n) {
  return `verify: UNVERIFIED — ${detail}; ${k} of ${n} finding(s) marked origin=unknown, surfaced-classification skipped`;
}

// verifySliceWithRetry -> { ok:true, verified, gap } | { ok:false, reason }
// One slice, dispatched at most VERIFY_ATTEMPTS_PER_SLICE times: the first executor call
// and — only if that one came back untrusted — exactly ONE re-dispatch before degrading.
// Same shape and rationale as writeArtifactsDerived's single retry: the executor is a
// SAMPLED AGENT, not a function, so a second dispatch is a fresh sample and is a
// plausible fix for the whole failure set here (a dropped/garbled receipt echo, a
// truncated result body, a fabricated elimination, a schema-retry exhaustion or timeout
// throw). That is why this retries uniformly where the persist path classifies: there is
// no verify failure class for which "our dispatch never reached the script" is provable,
// so a blanket single retry is both simpler and never less honest than a taxonomy.
//
// The retry carries a DISTINCT nonce (`${nonce}.${i}.r1`). Re-using the slice nonce
// would move the exact confusion trustSlice defends against from space (two equal-length
// slices satisfying each other's receipts) into time (attempt 2 satisfied by a replay of
// attempt 1's receipt) — and since attempt 1 was untrusted, a fresh receipt is precisely
// the thing we are re-dispatching to obtain. The suffix stays inside the args-waist nonce
// charset (args.js NONCE_RE) and inside one AST-safe word token.
//
// The nonce is now COMPUTED ONCE and threaded into verifyPrompt/verifyCommand. It used
// to be derived independently at both sites, which silently required the two formulas to
// stay identical; an attempt-varying nonce makes that a live bug rather than a latent one.
async function verifySliceWithRetry(c, inp, i, slice, { model, nonce, headShaShort, ids }) {
  const attempt = (sliceNonce, label) =>
    dispatchVerifySlice(c, inp, i, slice, { model, headShaShort, sliceNonce, label, ids });

  const first = await attempt(`${nonce}.${i}`, `verify-slice-${i}`);
  if (first.ok) return { ok: true, verified: first.verified, gap: null };

  const second = await attempt(`${nonce}.${i}.r1`, `verify-slice-${i}-retry`);
  if (second.ok) {
    // Disclosed, not degraded: no finding lost its classification, but the run states
    // that it took two dispatches to get there (the persist path's retry discloses the
    // same way). Deliberately carries no UNVERIFIED token — nothing was degraded.
    return {
      ok: true,
      verified: second.verified,
      gap: `verify-slice-retry: slice ${i}'s first executor dispatch was untrusted (${first.reason}); a second dispatch was trusted and this slice's verified findings are from that attempt`,
    };
  }
  return { ok: false, reason: `${second.reason} — retried once after the first attempt failed (${first.reason})` };
}

// One executor dispatch for one slice. Never throws: a thrown agent() becomes an
// untrusted result carrying the message, exactly as the pre-retry loop recorded it.
async function dispatchVerifySlice(c, inp, i, slice, { model, headShaShort, sliceNonce, label, ids }) {
  let env;
  try {
    // agent(promptString, opts); the pinned command is embedded in the prompt
    // (verifyPrompt), which is how the executor agent receives it.
    env = await c.agent(verifyPrompt(inp, i, sliceNonce), {
      label,
      agentType: 'code-gauntlet:executor',
      model,
      schema: VERIFY_SCHEMA,
    });
  } catch (e) {
    return { ok: false, reason: `executor threw (${(e && e.message) || 'unknown'})` };
  }
  const trust = trustSlice(env, { nonce: sliceNonce, headShaShort, n: slice.length, ids });
  if (!trust.ok) return { ok: false, reason: trust.reason };
  return { ok: true, verified: joinVerifyDeltas(slice, env.result.deltas) };
}

// dispatchableIds(slice) -> { ok:true, ids:[...] } | { ok:false, reason }
// The delta echo is joined by id, so a slice is only dispatchable when every finding in
// it carries a usable string id and no two share one. Duplicates ACROSS slices are fine —
// each slice's join is independent — so this is deliberately a per-slice check.
//
// Ids are matched EXACTLY, everywhere: here, in trustSlice's coverage check, and in the
// join. Only the USABILITY test trims, mirroring verify_findings.py's `id.strip()` guard —
// an id that is nothing but whitespace is one the script would skip. Matching on the
// trimmed form was tried and removed: it bought no tolerance the checksum did not
// immediately take back (the proof compares the id text the script wrote, so an echo that
// added or stripped whitespace failed there instead), while making two dispatched findings
// whose ids differ ONLY by surrounding whitespace collide into a false whole-slice degrade
// the script itself would never have produced. Strict everywhere is both simpler and
// strictly less likely to lose a slice.
function dispatchableIds(slice) {
  const ids = [];
  const seen = new Set();
  for (const f of slice) {
    const id = f && typeof f.id === 'string' ? f.id : '';
    if (!id.trim()) return { ok: false, reason: 'a dispatched finding has no usable id' };
    if (seen.has(id)) return { ok: false, reason: `duplicate finding id in the slice (${id})` };
    seen.add(id);
    ids.push(id);
  }
  return { ok: true, ids };
}

// The delta keys that carry a VALUE onto a finding (DELTA_KEYS minus the two structural
// ones). Iterated in the same fixed order the canonicalisation uses.
const DELTA_VALUE_KEYS = DELTA_KEYS.filter((k) => k !== 'id' && k !== 'verified');

const deltaHas = (d, k) => d[k] !== undefined && d[k] !== null;

// joinVerifyDeltas(slice, deltas) -> the slice's verified findings, enriched.
// Exported for the dual-runtime golden fixtures: the parity case records
// verify_findings.py's own delta and its own verified findings, and asserts THIS function
// reconstructs the latter from the former — which is the whole equivalence claim #25
// requirement 1 makes ("the enriched set after the join must be equivalent to today's
// trusted-path output for every field downstream stages consume").
//
// Walks the DISPATCHED slice, never the echo: order, membership and every untouched field
// come from data this stage already holds. A finding whose delta says verified:false was
// eliminated by the script and is omitted, exactly as it was omitted from the old echo's
// verified[] array.
//
// PRECONDITION, and the one way this function could ever drop a finding: it must be called
// only with deltas that trustSlice has already accepted, which is what proves every
// dispatched id has exactly one delta carrying a boolean `verified`. A finding with NO
// delta is skipped here — there is no honest alternative, since keeping it would deliver an
// unverified finding as a verified one — so a caller that skips the coverage check would
// reintroduce the silent drop this whole change exists to close. Nothing but
// dispatchVerifySlice (immediately after its trustSlice call) and the golden-fixture test
// calls it; keep it that way.
//
// `agent` is stripped here — the one place where the withholding #25 requirement 1
// mandates is enforced. It used to happen by omission (the echo schema simply did not
// declare `agent`, so StructuredOutput dropped it... most of the time — measured surviving
// on 2 of 6 PRs). Joining onto findings this stage holds would have made it deterministic,
// which is the measured dedup recall-collapse mechanism, so the withholding had to become
// explicit. It re-lands only with the cross-dimension consolidation redesign (#22).
export function joinVerifyDeltas(slice, deltas) {
  const byId = new Map();
  for (const d of Array.isArray(deltas) ? deltas : []) {
    if (d && typeof d.id === 'string') byId.set(d.id, d);
  }
  const out = [];
  for (const f of slice) {
    const delta = byId.get(f.id);
    if (!delta || delta.verified === false) continue;
    // pinNumericFields for the same reason the degraded path applies it: the script
    // coerces numeric strings at its --input boundary, so the trusted output has always
    // carried real numbers, and a leaked "85" makes the filter's consensus boost
    // string-concatenate ("85" + 10 -> "8510").
    const joined = pinNumericFields(f);
    delete joined.agent;
    for (const k of DELTA_VALUE_KEYS) if (deltaHas(delta, k)) joined[k] = delta[k];
    out.push(joined);
  }
  return out;
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
// shared char budget. Returns { failed } — a Map from SLICE INDEX to
// { group, reason } for every slice whose input file is not provably on disk. An
// empty map means every slice's input was written and proven.
//
// The per-GROUP fan-out goes through parallel() (issue #38, S2) — the groups are
// independent writes to distinct paths — and each thunk owns its try/catch, so a thrown
// member can never escape and parallel() never has to null it (the message survives into
// the reason string). NOTE this is the group loop only; verifyStage's per-SLICE executor
// loop stays sequential on purpose (each envelope pairs to its slice by order).
//
// A failed group takes down only ITS OWN slices (issue #54). This used to return the
// FIRST failure in group-index order and degrade the entire run, which threw away both
// the groups that wrote successfully and every other group's failure reason. Failures are
// still surfaced in a deterministic order — verifyStage walks slices by index and groups
// are contiguous in that index, so gap order is fixed by construction, not by which
// parallel member happened to settle first.
async function materializeVerifySlices(c, inp, slices, policy) {
  const v = inp.verify || {};
  const inputPathBase = v.inputPathBase || 'phase4-input';
  const model = modelFor('code-gauntlet:artifact-writer', policy);
  const entries = slices.map((slice, i) => ({
    path: `${inputPathBase}.slice${i}.json`,
    content: { findings: slice.map(pinNumericFields), base_branch: v.baseBranch },
  }));
  // path -> slice index. Keyed on the PATH (unique per slice by construction) rather than
  // on a group's position, so attributing a group's failure to its slices cannot be
  // silently invalidated by a future change to how entries are chunked.
  const sliceOfPath = new Map(entries.map((e, i) => [e.path, i]));
  const groups = chunkBySerializedSize(entries, SEGMENT_CHAR_BUDGET);
  const thunks = groups.map((group, g) => async () => {
    let result;
    try {
      result = await c.agent(verifySliceWriterPrompt(group), {
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
    // path degrades THIS GROUP's slices to UNVERIFIED (findings kept), never a fabricated
    // verify. The literal "(no write proof)" is the bench checker's degrade sentinel
    // (bench/runner/check.py _DEGRADE_RE) — do not paraphrase it.
    const written = new Set(Array.isArray(result.written) ? result.written : []);
    const dispatchedPaths = group.map((e) => e.path);
    if (!dispatchedPaths.every((p) => written.has(p))) {
      return { ok: false, reason: 'slice-input writer echo did not cover all dispatched slice paths (no write proof)' };
    }
    return { ok: true };
  });

  const results = await c.parallel(thunks);
  const failed = new Map();
  for (let g = 0; g < groups.length; g += 1) {
    const r = results[g];
    // A null member is unreachable while the thunks above swallow their own throws; it is
    // still handled so a platform-side null can never be mistaken for a successful write.
    let reason = null;
    if (!r) reason = `slice-input writer group ${g} produced no result`;
    else if (r.ok !== true) reason = r.reason;
    if (reason === null) continue;
    for (const e of groups[g]) {
      const idx = sliceOfPath.get(e.path);
      if (idx !== undefined) failed.set(idx, { group: g, reason });
    }
  }
  return { failed };
}

function verifySliceWriterPrompt(entries) {
  const payload = JSON.stringify(entries);
  return `Persist each verify slice-input file to disk exactly as given (the workflow has no disk access). For every entry in the payload, write its "content" as JSON to its "path". Return { written } listing the paths you wrote. The payload is the single JSON line after the marker below.\n${WRITER_PAYLOAD_MARKER}${payload}`;
}

// canonicalDeltas(ids, deltas) -> the deltas in a form both runtimes spell identically.
// Rebuilt from the DISPATCHED id order, one object per id, keys in DELTA_KEYS order,
// absent values omitted — so the echo's own array order, key order, and any field it
// invented cannot move the checksum. Only the VALUES the script decided can.
// verify_findings.py's build_deltas() emits exactly this shape in exactly this order.
//
// The rebuild therefore also makes the proof BLIND to any key outside DELTA_KEYS. That is
// deliberate and not a hole: an undeclared key does not survive StructuredOutput, and
// joinVerifyDeltas copies only DELTA_VALUE_KEYS, so a key the proof ignores is a key
// nothing reads. Covering it would buy no protection and would cost the order- and
// noise-tolerance that keeps a harmless echo quirk from degrading a slice.
function canonicalDeltas(ids, byId) {
  return ids.map((id) => {
    const src = byId.get(id) || {};
    const out = {};
    for (const k of DELTA_KEYS) if (deltaHas(src, k)) out[k] = src[k];
    return out;
  });
}

// deltaContentProof(ids, deltas) -> "fnv1a32:0x........"
// The workflow half of the delta echo's content proof. Exported because the tests must
// build valid envelopes with the REAL computation rather than a second copy of it — a
// helper that re-derives the canonicalisation would happily agree with a broken one. The
// cross-runtime half is pinned separately, by the golden fixture whose checksum
// verify_findings.py itself produced.
export function deltaContentProof(ids, deltas) {
  const byId = new Map();
  for (const d of Array.isArray(deltas) ? deltas : []) {
    if (d && typeof d.id === 'string') byId.set(d.id.trim(), d);
  }
  return fnv1a32(JSON.stringify(canonicalDeltas(ids || [], byId), null, 2));
}

// A slice envelope is trusted only if it is the honest success shape AND its receipt
// echoes exactly what we dispatched: the nonce (this answer is for OUR call), the head
// sha (same tree the workflow resolved), and n_in (the executor loaded every finding we
// sent). Three guards beyond the receipt, in increasing strength:
//
//   (1) DELTA-ID COVERAGE (#25 req 2) — the echoed deltas must name EXACTLY the ids this
//       slice dispatched: no missing id, no duplicate, no stranger. This replaced a count
//       guard (verified.length + eliminated.length === n_in) that could only ever catch
//       transport truncation: verifySliceSize is a constant, so most slices in a run share
//       a length, and an envelope carrying a SIBLING slice's findings summed identically.
//       That was not a misclassification — the real slice's findings were dropped outright
//       while the run reported verified=true (reproduced end to end during the #54 review).
//       An id set is the cheapest thing that binds an answer to its question.
//   (2) SHAPE AND STAMP — `verified` must be a boolean; the value fields must be the types
//       the script emits; a verified:false delta must carry the elimination_reason stamp
//       run_verification() ALWAYS writes before eliminating ('evidence does not match file
//       content'), and a verified:true delta must NOT carry one. The stamp check is
//       preserved from the by-value design, where it was the only defence against an
//       executor moving a finding verified->eliminated in its echo (observed live: script
//       disk 10v/0e, echo 7v/3e under a valid receipt). It is now redundant with (3) and
//       kept anyway, because a precise reason in the gap is worth more than a checksum
//       mismatch when a human reads the run.
//   (3) CONTENT PROOF — fnv1a32 over the canonical rebuild must equal the checksum the
//       SCRIPT computed over its own deltas. This is what closes coherent drift: an
//       executor that flips one origin, shifts one confidence, or invents a plausible
//       elimination satisfies (1) and (2) and fails here. It is the same checksum pair the
//       persist path uses (assemble_artifacts.py <-> fnv1a32/JSON.stringify here), pinned
//       across runtimes by tests/test_assemble_artifacts.py.
//
// Every failure degrades the WHOLE slice to UNVERIFIED, which is conservative in the
// direction that matters: every dispatched finding is KEPT (origin=unknown, disclosed in a
// gap), so no guard here can be used to drop a real finding.
//
// Threat model: this defends against a STALE, DRIFTING or CONFUSED executor (an old/wrong
// result, a fabricated success, another slice's answer, a garbled transcription) — NOT a
// Byzantine one. The nonce is argv-visible and the checksum travels in the same envelope
// as the data it covers, so a malicious executor could recompute both; an LLM transcribing
// a document cannot, which is exactly the failure class this boundary keeps hitting.
function trustSlice(env, { nonce, headShaShort, n, ids }) {
  if (!env || typeof env !== 'object') return { ok: false, reason: 'executor returned no envelope' };
  if (env.status !== 'ok') return { ok: false, reason: `status=${env.status == null ? 'missing' : env.status}${env.stderr ? ` (${env.stderr})` : ''}` };
  const r = env.receipt || {};
  if (r.nonce !== nonce) return { ok: false, reason: `receipt nonce mismatch (got ${r.nonce == null ? 'missing' : r.nonce}, expected ${nonce})` };
  if (r.sha !== headShaShort) return { ok: false, reason: `receipt sha mismatch (got ${r.sha == null ? 'missing' : r.sha})` };
  if (r.n_in !== n) return { ok: false, reason: `receipt n_in mismatch (got ${r.n_in == null ? 'missing' : r.n_in}, expected ${n})` };
  const result = env.result || {};
  if (!Array.isArray(result.deltas)) return { ok: false, reason: 'result missing deltas array' };

  const expected = new Set(ids || []);
  const byId = new Map();
  for (const d of result.deltas) {
    if (!d || typeof d !== 'object') return { ok: false, reason: 'delta entry is not an object' };
    const id = typeof d.id === 'string' ? d.id : '';
    if (!id.trim()) return { ok: false, reason: 'delta entry has no id' };
    if (!expected.has(id)) return { ok: false, reason: `delta names a finding this slice did not dispatch (${id})` };
    if (byId.has(id)) return { ok: false, reason: `delta repeats a finding id (${id})` };
    if (typeof d.verified !== 'boolean') return { ok: false, reason: `delta ${id} has no boolean verified flag` };
    for (const k of ['origin', 'severity', 'elimination_reason']) {
      if (deltaHas(d, k) && typeof d[k] !== 'string') return { ok: false, reason: `delta ${id}: ${k} is not a string` };
    }
    // The script canonicalises confidence to an integer precisely so this side never has
    // to agree with Python on how a float is spelled (_delta_confidence). A non-integer
    // here therefore did not come from the script.
    if (deltaHas(d, 'confidence') && !Number.isInteger(d.confidence)) {
      return { ok: false, reason: `delta ${id}: confidence is not an integer` };
    }
    const stamp = typeof d.elimination_reason === 'string' ? d.elimination_reason.trim() : '';
    if (d.verified === false && stamp === '') {
      return { ok: false, reason: `delta ${id}: eliminated without the elimination_reason stamp (fabricated elimination — the verify script always stamps a real one)` };
    }
    if (d.verified === true && stamp !== '') {
      return { ok: false, reason: `delta ${id}: verified finding carries an elimination_reason stamp` };
    }
    byId.set(id, d);
  }
  const missing = (ids || []).filter((id) => !byId.has(id));
  if (missing.length) {
    return { ok: false, reason: `delta does not cover ${missing.length} of ${(ids || []).length} dispatched finding(s) (first: ${missing[0]})` };
  }

  const proof = typeof r.deltas_checksum === 'string' ? r.deltas_checksum.trim() : '';
  if (!proof) return { ok: false, reason: 'receipt carries no deltas_checksum (content proof missing)' };
  const recomputed = deltaContentProof(ids || [], result.deltas);
  if (proof !== recomputed) {
    return { ok: false, reason: `delta content proof mismatch (receipt ${proof}, recomputed ${recomputed}) — the echoed values are not the ones the script wrote` };
  }
  return { ok: true };
}

// The pinned command: a single `python3 <script> --flags...` invocation of plain word
// tokens only (CLAUDE.md AST-safe emission — no command substitution, heredocs, env
// prefix, or shell operators). Per-slice input/output paths are sha-scoped and index-
// suffixed; verifyStage materializes the slice inputs via the artifact-writer (see
// materializeVerifySlices) before dispatch, then the executor reads the slice output.
//
// `sliceNonce` is THREADED IN, never re-derived: it is the same value verifySliceWithRetry
// hands trustSlice as the expected receipt nonce, so argv and the trust check cannot
// disagree. It varies by ATTEMPT as well as by slice (the retry's `.r1` suffix), which is
// exactly what a second, independently-derived formula here would get wrong. The paths do
// NOT vary by attempt: a retry re-runs the same script over the same slice input and
// overwrites the same output.
function verifyCommand(inp, i, sliceNonce) {
  const v = inp.verify || {};
  const inPath = `${v.inputPathBase || 'phase4-input'}.slice${i}.json`;
  const outPath = `${v.outputPathBase || 'phase4-output'}.slice${i}.json`;
  const parts = [
    'python3', v.scriptPath || 'scripts/verify_findings.py',
    '--input', inPath,
    '--output', outPath,
    '--nonce', sliceNonce,
    '--head-sha', inp.headShaShort,
    '--base-branch', v.baseBranch || 'main',
  ];
  if (v.diffPath) parts.push('--diff-file', v.diffPath);
  return parts.join(' ');
}

// What the executor is asked for is now a PREFIX of the output document, not the whole of
// it: the script writes `result.deltas` as the first key precisely so a length-capped Read
// (which returns no truncation notice — CLAUDE.md) still contains everything this prompt
// names. The large verified/eliminated arrays that follow are for bench and v2 consumers;
// naming them here as explicitly-not-wanted is cheaper than letting the agent decide.
function verifyPrompt(inp, i, sliceNonce) {
  return `Run exactly this command, then read the --output file and return, via the schema: its "status"; its "receipt" object with all four fields (sha, n_in, nonce, deltas_checksum) copied exactly; and every entry of its "result.deltas" array, copied exactly. The same file also holds large "verified" and "eliminated" arrays — do NOT return those and do not summarise them. Copy character for character: the deltas carry a checksum and a single altered value costs this slice its verification.\n${verifyCommand(inp, i, sliceNonce)}`;
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
//
// The verify term counts VERIFY_ATTEMPTS_PER_SLICE dispatches per slice, because every
// slice can independently take its one deterministic retry (verifySliceWithRetry) and a
// worst case that assumes the happy path is not a bound. The remaining terms are still
// NOMINAL: the report/writer pair can segment, and writeArtifactsDerived has its own
// single retry, neither of which is counted here. That looseness is covered by the
// headroom between AGENT_COUNT_GUARD and the platform ceiling; the verify term is the one
// that scales with finding count, which is why it is the one made exact.
export function worstCaseAgentCount(limits, nFiles, nFindings) {
  const L = limits || {};
  const files = Math.max(0, nFiles || 0);
  const findings = Math.max(0, nFindings || 0);
  const summarizeCalls = ceilDiv(files, effectiveBucketSize(L)) + 1;
  const verifyCalls = ceilDiv(findings, effectiveSliceSize(L, findings)) * VERIFY_ATTEMPTS_PER_SLICE;
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
    // Same VERIFY_ATTEMPTS_PER_SLICE scaling worstCaseAgentCount applies, so the "reduce
    // whichever term is largest" choice is made against each term's real contribution to
    // the count it is trying to pull down. Doubling verifySliceSize still strictly halves
    // this term, so the loop's termination argument is unchanged.
    const verifyTerm = ceilDiv(findings, effectiveSliceSize(L, findings)) * VERIFY_ATTEMPTS_PER_SLICE;
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
// dispatched PER chunk (through parallel(), each with the same try/catch), then
// the per-chunk reports are concatenated under titled segment headings. Any single
// chunk that fails degrades to its own minimal section — the rest still render.
// parallel() preserves INPUT order, so the `## Report segment i of n` concatenation
// and the gap ordering are byte-identical to the old sequential loop regardless of
// which segments answer first (issue #38, S2).
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

  // Segment: one dispatch per chunk through parallel(), titled sections joined IN INDEX
  // ORDER. dispatchReportSegment already owns its try/catch and never throws, so no member
  // can be nulled by parallel(); the null branch below is defense-in-depth only.
  const chunks = chunkBySerializedSize(findings, SEGMENT_CHAR_BUDGET);
  const thunks = chunks.map((chunk, i) => () => dispatchReportSegment(c, model, inp, chunk, { index: i, total: chunks.length }));
  const results = await c.parallel(thunks);
  const parts = [];
  const gaps = [];
  for (let i = 0; i < chunks.length; i += 1) {
    const out = results[i] || {
      report: minimalReport({ ...inp, findings: chunks[i] }),
      gaps: [`report segment ${i}: dispatch produced no result — assembled a minimal report from pipeline stats`],
    };
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
    const report = unwrapWrappedReport(result && result.report);
    if (!report) {
      return { report: minimalReport(segInp), gaps: [`report${tag}: writer returned no report — assembled a minimal report from pipeline stats`] };
    }
    return { report, gaps: [] };
  } catch (e) {
    return { report: minimalReport(segInp), gaps: [`report${tag}: writer agent threw (${(e && e.message) || 'unknown'}) — assembled a minimal report from pipeline stats`] };
  }
}

// The report-writer intermittently returns its markdown ALREADY WRAPPED as a JSON
// document: the string in its `report` field is literally `{"report": "# Code Gauntlet
// Report..."}` instead of the markdown. The artifact-writer then persists that wrapper
// verbatim — correctly, its contract is to write the text exactly as given — so
// report.md holds JSON where every non-Phase-8 consumer expects markdown. Measured
// across dated runs since 2026-07-22: roughly 15 of 25, i.e. long-standing and FLAKY,
// not a regression of any one change.
//
// Fixed here, at the point the string is FIRST received, so the single-dispatch and the
// segmented paths (both go through dispatchReportSegment) are covered by one rule and
// the persisted artifact — not just the delivered one — is markdown.
//
// Phase 8 ALSO unwraps this shape at delivery time (references/phase8-delivery.md).
// That is deliberate belt-and-braces, not a dead duplicate: this fix repairs the
// PERSISTED artifact, Phase 8's repairs a report that reached it by any other route
// (a resumed run, a hand-edited file, an older artifact). Neither makes the other
// unnecessary; removing this one silently restores the corrupt-on-disk behaviour.
//
// Conservative by construction — a legitimate markdown report may well open with `{`.
// Unwrapping requires ALL of: a successful JSON.parse, a plain object (not an array,
// not a bare JSON string), a STRING `report` member, and no other meaningful content.
// Anything else is returned byte-for-byte untouched. One level only, never a loop.
function unwrapWrappedReport(s) {
  if (typeof s !== 'string' || s === '') return s;
  const trimmed = s.trim();
  if (trimmed.charAt(0) !== '{') return s; // cheap reject before any parse attempt
  let parsed;
  try {
    parsed = JSON.parse(trimmed);
  } catch (e) {
    return s; // not JSON at all — ordinary markdown that happens to start with a brace
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return s;
  if (typeof parsed.report !== 'string') return s;
  // "Only meaningful content": tolerate a null/empty sibling key (an agent echoing an
  // empty envelope field), but never discard real data by unwrapping past it.
  for (const [k, v] of Object.entries(parsed)) {
    if (k === 'report') continue;
    if (v !== null && v !== undefined && v !== '') return s;
  }
  return parsed.report;
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

// The report-writer is deliberately NOT given the shared context path (issue #38, R1):
// its contract renders entirely from the by-value { summary, findings, unverified, stats }
// below, and references/report-format.md sources its code snippet from finding.evidence —
// also carried by value. In the profiled run the writer spent 5.7 s Reading the 95 KB
// context file, got a TRUNCATED read back, and then said it had enough context without it.
// Dropping the read removes that latency and the truncated-read exposure for this agent.
function reportPrompt(inp, seg) {
  const segLine = seg ? `This is report segment ${seg.index + 1} of ${seg.total}; render only the findings in this segment. ` : '';
  const body = JSON.stringify({
    summary: inp.summary || '',
    findings: inp.findings || [],
    unverified: (!seg || seg.index === 0) ? (inp.unverified || []) : [], // render the unverified bucket once, in segment 0
    stats: inp.stats || {},
  });
  return `${segLine}Write the code-gauntlet report as markdown for these results. Put high-confidence findings in the main section and unverified/pipeline-degraded findings in a clearly-labelled secondary section. Results JSON:\n${body}\nReturn { report } where report is the full markdown document.`;
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
// headShaShort, policy, persist }) -> { artifactPaths, gaps, partial }
// The workflow script has NO disk access, so agents persist findings.json + report.md
// + the post-review delivery set + the checkpoint/progress JSON to {output_dir}.
//
// TWO persistence paths, one PUBLIC contract (same return shape, the same four
// artifactPaths keys, the same partial-artifacts degradation — Phase 8 is untouched):
//
//   DERIVED (issue #38, D3; taken when args.persist.assembleScriptPath is present).
//     Measured on a real run: of the 88,389 B the writer emitted, the post-review
//     findings array was canonically byte-identical to findings.json, the checkpoint's
//     phases.challenge.findings was the alias-stripped twin of the same array, and the
//     genuine residual was 383 B. So the writer now persists ONLY the unique content
//     (findings.json, report.md, the persist plan) and the executor runs the pinned
//     scripts/assemble_artifacts.py to DERIVE the two projections from what actually
//     landed on disk, returning a content-proof receipt.
//
//   LEGACY (no persist waist, or the id-integrity guard refused). One artifact-writer
//     dispatch carrying all four artifacts by value. Unchanged.
//
// Wrapped in its own try/catch (like reportStage): a throw OR null result degrades to
// a partial-artifacts gap with null paths and is NON-FATAL — it never bubbles to the
// top-level catch. The try covers the WHOLE body, not just the dispatches: the derived
// path computes the plan and the primaries (JSON.stringify over caller-supplied objects,
// a deep clone of the checkpoint) BEFORE any agent is called, and a throw there is exactly
// as non-fatal as a writer failure. Keeping those computations outside the guard is the
// regression this shape exists to prevent — SKILL.md's Error Recovery promises the caller
// that writer failure never ends the run.
export async function writeArtifacts(ctx, input) {
  const partial = (reason, extraGaps) => ({
    artifactPaths: { findings: null, report: null, postReview: null, checkpoints: null },
    gaps: (extraGaps || []).concat([`writeArtifacts: ${reason} — artifacts not persisted (partial-artifacts)`]),
    partial: true,
  });

  try {
    const c = ctx || defaultCtx();
    const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
    const outputDir = inp.outputDir || '.code-gauntlet';
    const sha = inp.headShaShort || 'head';
    const policy = inp.policy || {};
    const paths = plannedArtifactPaths(outputDir, sha);

    const assembleScriptPath = (inp.persist || {}).assembleScriptPath;
    if (typeof assembleScriptPath === 'string' && assembleScriptPath !== '') {
      // The guard that keeps the derived path safe under pathological input. Every
      // projection is by finding id, so a missing/duplicate id — or a delivery/challenge
      // entry that is not a byte-identical twin of its findings.json row — makes the
      // derivation unfaithful. Rather than degrade the run (null paths, no artifacts) we
      // fall back to the legacy full by-value writer and name the reason in a gap.
      const derivable = persistDerivable(inp);
      if (derivable.ok) {
        return await writeArtifactsDerived(c, inp, paths, outputDir, sha, policy, partial, assembleScriptPath);
      }
      return await writeArtifactsLegacy(c, inp, paths, policy, partial, [
        `writeArtifacts: derived persistence unavailable (${derivable.reason}) — persisted the full by-value payload instead`,
      ]);
    }
    // No persist waist: the documented clean degradation for older callers (bench
    // included). Legacy path, no gap — nothing was lost, only latency.
    return await writeArtifactsLegacy(c, inp, paths, policy, partial, []);
  } catch (e) {
    return partial(`persistence threw before any artifact was written (${(e && e.message) || 'unknown'})`, []);
  }
}

// The legacy full by-value persist: ONE artifact-writer dispatch carrying all four
// artifacts. `extraGaps` rides along on both the success and the degradation return so
// an id-integrity fallback is always visible in the envelope.
async function writeArtifactsLegacy(c, inp, paths, policy, partial, extraGaps) {
  const model = modelFor('code-gauntlet:artifact-writer', policy);
  try {
    const result = await c.agent(writeArtifactsPrompt(inp, paths), {
      label: 'artifact-writer',
      agentType: 'code-gauntlet:artifact-writer',
      model,
      schema: WRITER_SCHEMA,
    });
    if (!result) return partial('writer returned null', extraGaps);
    if (!writerEchoCoversPaths(result.artifactPaths, paths)) {
      return partial('writer echo did not account for all four planned artifact paths (no write proof)', extraGaps);
    }
    return { artifactPaths: paths, gaps: extraGaps, partial: false };
  } catch (e) {
    return partial(`writer agent threw (${(e && e.message) || 'unknown'})`, extraGaps);
  }
}

// The derived persist (issue #38, D3.2): write the three primaries, then derive the two
// projections on disk. Failure at ANY step takes the same partial-artifacts degradation
// as the legacy path — a content-proof MISMATCH is the one exception (see below).
//
// ONE deterministic retry on a STRUCTURAL assemble failure (live smoke
// smoke-20260727-205454-f99d948). On discourse-graphite#6 the writer transcribed
// findings.json with `\"` over-escaped to `\\"`, producing unparseable JSON at line 99;
// assemble_artifacts.py correctly refused to derive anything and the run lost ALL its
// artifacts. The writer is a sampled agent, not a deterministic function — the other two
// runs on the same commit produced parseable JSON — so a second dispatch is a genuinely
// fresh sample rather than a repeat of the same computation. Hence: retry the whole
// derived persist (writer + assembler) exactly once, then degrade.
//
// The boundaries are load-bearing:
//   * ONLY when the assemble script REFUSED (trustAssembleReceipt structural failure:
//     unparseable JSON, missing/duplicate id, bad plan checksum, a derived-document
//     mismatch). A tolerated PRIMARY content-proof mismatch never reaches here — it is a
//     successful persist with a disclosed divergence, and re-rolling it would trade a
//     known-divergent artifact for an unknown one.
//   * A writer throw / null / failed write-proof degrades immediately, as before: the
//     dispatch itself did not complete, so there is nothing the assembler could refuse.
//   * EXACTLY once. `attemptDerivedPersist` is called at most twice from here and calls
//     nothing recursively — there is no loop to unbound.
//   * NEVER fall back to the legacy by-value writer on this path. This was considered and
//     REJECTED: the legacy writer carries NO content proof, so falling back to it converts
//     a visible failure into a silent one. The same smoke run measured the by-value writer
//     diverging from its payload on 3 of 3 runs (16 chars, 8 chars, and the invalid JSON
//     above) — silent corruption is the normal case there, not the exception. Do not
//     "helpfully" add the fallback.
//   * BOTH attempts are disclosed, whichever way the retry lands, so a degraded (or
//     narrowly-rescued) run stays honest about what happened.
async function writeArtifactsDerived(c, inp, paths, outputDir, sha, policy, partial, assembleScriptPath) {
  const planPath = persistPlanPath(outputDir, sha);
  const { findingsJson, reportMd } = persistPrimaries(inp);
  // Kept as an OBJECT as well as a string: trustAssembleReceipt grades the receipt
  // against the expectations the pipeline itself computed, never against the ones the
  // receipt echoes back at us.
  const plan = persistPlan(inp, paths);
  const planJson = JSON.stringify(plan, null, 2);
  const entries = [
    { path: paths.findings, text: findingsJson },
    { path: paths.report, text: reportMd },
    { path: planPath, text: planJson },
  ];
  const attempt = () => attemptDerivedPersist(c, entries, planPath, plan, paths, policy, assembleScriptPath);

  const first = await attempt();
  if (first.ok) return { artifactPaths: paths, gaps: first.gaps, partial: false };
  if (!first.retryable) return partial(first.reason, []);

  const second = await attempt();
  if (second.ok) {
    return {
      artifactPaths: paths,
      gaps: [`artifact-persist-retry: the first derived persist attempt failed (${first.reason}); a second artifact-writer dispatch succeeded and the artifacts below are from that attempt`]
        .concat(second.gaps),
      partial: false,
    };
  }
  return partial(`${second.reason} — retried once after the first attempt failed (${first.reason})`, []);
}

// One derived-persist attempt: dispatch the writer for the three primaries, prove they
// landed, run the assembler, grade the receipt. Returns
//   { ok: true, gaps }                       — persisted (gaps may disclose a tolerated mismatch)
//   { ok: false, retryable, reason }         — failed; `retryable` iff the assemble script
//                                              structurally refused (see the caller).
// Never throws: every dispatch keeps its own try/catch, exactly as before.
async function attemptDerivedPersist(c, entries, planPath, plan, paths, policy, assembleScriptPath) {
  const fail = (reason, retryable) => ({ ok: false, retryable: !!retryable, reason });

  let writerOut;
  try {
    writerOut = await c.agent(finalArtifactsWriterPrompt(entries), {
      label: 'artifact-writer',
      agentType: 'code-gauntlet:artifact-writer',
      model: modelFor('code-gauntlet:artifact-writer', policy),
      schema: WRITTEN_SCHEMA,
    });
  } catch (e) {
    return fail(`writer agent threw (${(e && e.message) || 'unknown'})`);
  }
  if (!writerOut) return fail('writer returned null');
  // Write-proof, same threat model as materializeVerifySlices: WRITTEN_SCHEMA declares
  // no `required`, so an empty { written: [] } is schema-valid and a writer under
  // StructuredOutput retry pressure can return one having written nothing. Without this
  // the assembler would then read primaries that never landed.
  const written = new Set(Array.isArray(writerOut.written) ? writerOut.written : []);
  if (!entries.every((e) => written.has(e.path))) {
    return fail('writer echo did not cover all three primary artifact paths (no write proof)');
  }

  let receipt;
  try {
    receipt = await c.agent(assemblePrompt(assembleScriptPath, planPath), {
      label: 'assemble-artifacts',
      agentType: 'code-gauntlet:executor',
      model: modelFor('code-gauntlet:executor', policy),
      schema: ASSEMBLE_RECEIPT_SCHEMA,
    });
  } catch (e) {
    return fail(`assemble executor threw (${(e && e.message) || 'unknown'})`);
  }
  const trust = trustAssembleReceipt(receipt, paths, plan);
  // The one retryable class: the primaries reached disk (write-proof passed) and the
  // pinned script graded them and refused. A fresh writer sample can fix exactly this.
  if (!trust.ok) return fail(trust.reason, true);
  return { ok: true, gaps: trust.gaps };
}

// The assemble receipt gate. A STRUCTURAL failure (no receipt, ok:false, a path the
// script never verified/wrote) is untrustworthy persistence -> degrade. A content-proof
// MISMATCH is deliberately NOT a failure: the script derived from what is actually on
// disk and the artifacts are self-consistent with it, so the findings are still
// delivered and the divergence is raised as a LOUD gap instead of costing the run its
// artifacts (never-fabricate cuts both ways — inventing a new way to lose a run is
// exactly as wrong as inventing a success).
//
// A receipt must NEVER be allowed to grade itself (issue #38 L1-2). The receipt is a
// self-report relayed by an executor agent, so every claim it makes about what it was
// CHECKING AGAINST is compared here to the value the pipeline computed independently:
//
//   * receipt.planChecksum must equal the plan's own planChecksum. The script recomputes
//     it from the plan it actually read and refuses to run on a mismatch, so a receipt
//     echoing a different value did not execute THIS plan.
//   * each verified entry's expected_chars/expected_checksum must equal the plan's
//     expect[] entry for that path. Otherwise a wholly self-consistent fabricated
//     receipt ("expected 10, got 10, match") passes.
//   * content_proof must agree with the numbers the receipt itself reports, since the
//     script derives it as exactly `chars === expected_chars && checksum ===
//     expected_checksum`. An incoherent receipt is a broken relay, not a proof.
//   * each DERIVED document's reported chars/checksum must equal the plan's `derive[]` entry
//     for that path — the pipeline's own serialization of the document it holds. Path
//     presence proved only that something was written there.
function trustAssembleReceipt(receipt, paths, plan) {
  if (!receipt || typeof receipt !== 'object') return { ok: false, reason: 'assemble executor returned no receipt' };
  if (receipt.ok !== true) {
    const errors = Array.isArray(receipt.errors) ? receipt.errors.join('; ') : '';
    return { ok: false, reason: `assemble script reported failure (${errors || 'no reason given'})` };
  }
  if (receipt.planChecksum !== plan.planChecksum) {
    return {
      ok: false,
      reason: `assemble receipt echoed plan checksum ${receipt.planChecksum === undefined ? 'none' : receipt.planChecksum} but the pipeline computed ${plan.planChecksum} — the executor did not run this persist plan`,
    };
  }
  const verified = Array.isArray(receipt.verified) ? receipt.verified : [];
  const written = Array.isArray(receipt.written) ? receipt.written : [];
  const verifiedByPath = new Map(verified.map((e) => [(e && e.path), e]));
  const expectByPath = new Map((plan.expect || []).map((e) => [e.path, e]));
  const writtenPaths = new Set(written.map((e) => (e && e.path)));
  for (const p of [paths.findings, paths.report]) {
    const got = verifiedByPath.get(p);
    if (!got) return { ok: false, reason: `assemble receipt did not verify ${p} (no content proof)` };
    const want = expectByPath.get(p);
    if (!want || got.expected_chars !== want.chars || got.expected_checksum !== want.checksum) {
      return {
        ok: false,
        reason: `assemble receipt checked ${p} against a foreign expectation (receipt says ${got.expected_chars} chars/${got.expected_checksum}, the pipeline handed the writer ${want ? want.chars : 'none'}/${want ? want.checksum : 'none'})`,
      };
    }
    const same = got.chars === want.chars && got.checksum === want.checksum;
    if ((got.content_proof === 'match') !== same) {
      return {
        ok: false,
        reason: `assemble receipt is incoherent for ${p}: content_proof:"${got.content_proof}" contradicts its own chars/checksum (${got.chars}/${got.checksum} vs expected ${want.chars}/${want.checksum})`,
      };
    }
  }
  // The DERIVED documents (the delivered payload). Path presence alone proved nothing about
  // their content, so each is checked against the plan's own `derive` expectation — the
  // chars/checksum the pipeline computed for the document it holds in memory, compared to
  // what the script reports for the bytes it actually wrote.
  //
  // Threat model, stated plainly for whoever reads this next: the plan is on disk, so a
  // BYZANTINE executor can read these numbers and echo them back. This is NOT authentication.
  // It catches a stale/confused/hallucinating executor and a real Python-vs-JS serializer
  // divergence — the same bound trustSlice documents for the verify receipt.
  //
  // Unlike a primary mismatch, this is STRUCTURAL: a primary can still be derived from
  // on-disk truth, but a derived document that disagrees with the pipeline has no other
  // copy to fall back to — the derivation itself is what went wrong.
  //
  // ONE EXCEPTION, and it is not a loophole. Both derived documents are projections of
  // findings.json ALONE, so when findings.json's OWN content proof came back `mismatch` the
  // derived documents are EXPECTED to differ: the script faithfully projected the divergent
  // bytes that are actually on disk. Failing there would convert the deliberately non-fatal
  // primary mismatch into a lost run — inventing a new way to lose a run, which the
  // never-fabricate contract rules out in the same breath as inventing a success. The
  // difference is still reported, as a gap, right beside the primary mismatch that caused it.
  const writtenByPath = new Map(written.map((e) => [(e && e.path), e]));
  const deriveByPath = new Map((plan.derive || []).map((e) => [e.path, e]));
  const findingsProof = verifiedByPath.get(paths.findings);
  const sourceDiverged = !!(findingsProof && findingsProof.content_proof === 'mismatch');
  const derivedGaps = [];
  for (const p of [paths.postReview, paths.checkpoints]) {
    if (!writtenPaths.has(p)) return { ok: false, reason: `assemble receipt did not write ${p} (no write proof)` };
    const want = deriveByPath.get(p);
    if (!want) return { ok: false, reason: `persist plan carries no derived-content expectation for ${p} (no content proof)` };
    const got = writtenByPath.get(p);
    if (got.chars === want.chars && got.checksum === want.checksum) continue;
    const detail = `derived document ${p} does not match the pipeline's own derivation (receipt reports ${got.chars == null ? 'no' : got.chars} chars/${got.checksum == null ? 'no checksum' : got.checksum}, the pipeline derived ${want.chars}/${want.checksum})`;
    if (sourceDiverged) {
      derivedGaps.push(`artifact-content-proof: ${detail} — expected, since ${paths.findings} it was derived from also diverged`);
      continue;
    }
    return { ok: false, reason: `${detail} — the delivered payload is not what this run produced` };
  }
  // Primary mismatches first, then any derived difference they explain — the primary gap
  // ordering is unchanged for every run that has no derived difference.
  const gaps = [];
  for (const e of verified) {
    if (e && e.content_proof === 'mismatch') {
      gaps.push(`artifact-content-proof: ${e.path} bytes on disk differ from the payload handed to the writer (expected ${e.expected_chars} chars/checksum ${e.expected_checksum}, got ${e.chars}/${e.checksum})`);
    }
  }
  gaps.push(...derivedGaps);
  return { ok: true, gaps };
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
  const postReviewSet = (inp.postReview || []).map(toV2Aliased);
  const id = inp.prIdentity;
  return {
    findings: (inp.findings || []).map(toV2Aliased),
    // With a PR identity (delivery.prIdentity, live-run L3) the persisted post-review
    // artifact IS the post_review.py input wrapper — Phase 8 posts it without hand-
    // assembling { owner, repo, pr_number, ... } around a bare array (the wrap was
    // documented but got reverse-engineered anyway, ~8 turns in the PR-310 run).
    // review_body is intentionally '' — Phase 8 composes the summary narrative and may
    // fill it before posting; post_review.py treats '' as a valid empty summary. sha is
    // provenance (post_review.py resolves its own HEAD); platform stays absent so
    // post_review.py auto-detects. The findings SET is byte-identical either way —
    // the wrapper only changes the envelope, never the scored content (D16).
    postReview: id
      ? { owner: id.owner, repo: id.repo, pr_number: id.pr_number, sha: id.sha_full, review_body: '', findings: postReviewSet }
      : postReviewSet,
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

// --- Derived persistence (issue #38, D3) -------------------------------------

// The v2 aliases toV2Aliased ADDS at the persist boundary. The checkpoint's
// challenge findings are the alias-stripped twin of findings.json, so removing
// exactly these keys restores the canonical shape (and, because the aliases are
// appended LAST, the key order too).
const PERSIST_ALIAS_FIELDS = ['line', 'end_line', 'body'];

function stripPersistAliases(f) {
  const out = {};
  for (const [k, v] of Object.entries(f)) {
    if (!PERSIST_ALIAS_FIELDS.includes(k)) out[k] = v;
  }
  return out;
}

// fnv1a32(s) -> "fnv1a32:0x........" — the content-proof checksum.
//
// It must be computable IDENTICALLY here and in Python. The workflow sandbox has no
// TextEncoder and no Buffer, so the only byte source available is String#charCodeAt —
// i.e. UTF-16 code units. scripts/assemble_artifacts.py reproduces this exactly by
// unpacking the string's utf-16-le encoding, including surrogate pairs (an emoji
// contributes TWO units on both sides). Math.imul is a language builtin, NOT a host
// global, so it is available in the sandbox.
export function fnv1a32(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `fnv1a32:0x${h.toString(16).padStart(8, '0')}`;
}

// Strip a UTF-8 BOM and AT MOST ONE trailing newline before checksumming. The Write
// tool may normalise a trailing newline or prepend a BOM, and a false content-proof
// degrade must not cost a run its artifacts. Applied on BOTH sides (here and in
// assemble_artifacts.py) so the tolerance is symmetric; two trailing newlines is a
// REAL difference and still reports as a mismatch.
export function normalizeForChecksum(s) {
  let out = typeof s === 'string' ? s : '';
  if (out.charCodeAt(0) === 0xfeff) out = out.slice(1);
  if (out.endsWith('\r\n')) return out.slice(0, -2);
  if (out.endsWith('\n')) return out.slice(0, -1);
  return out;
}

// The persist plan's own path. Deliberately matches the Phase 2 stale-file truncation
// glob `code-gauntlet-*-<sha>.*`, so no skill change is needed to clean it up. It is
// NOT an artifactPaths key — the public contract stays at exactly four.
export function persistPlanPath(outputDir, sha) {
  return `${outputDir}/code-gauntlet-persist-plan-${sha}.json`;
}

// The two PRIMARY strings — the only genuinely unique content the writer persists.
// Pure and exported so a test can assert the writer prompt carries them verbatim.
export function persistPrimaries(inp) {
  return {
    findingsJson: JSON.stringify(((inp || {}).findings || []).map(toV2Aliased), null, 2),
    reportMd: (inp || {}).report || '',
  };
}

// persistPlan(inp, paths) -> the plan scripts/assemble_artifacts.py consumes.
// PURE (deep-clones the checkpoint before emptying the challenge findings), and
// exported so the projection rules are directly unit-testable — the in-run
// byte-identity test applies them to the primaries and asserts the result equals
// writerPayload(inp).postReview / .checkpoints.
//
// The skeleton is derived FROM slimPersistedCheckpoints' output by EMPTYING (not
// deleting) phases.challenge.findings, so there is exactly one source of truth for the
// checkpoint shape and the key keeps its position — the derived document is
// key-order-identical to the one the pipeline holds in memory.
//
// THE PLAN'S SELF-PROOF (`planChecksum`, issue #38 L1-2). The plan is transcribed to
// disk by the artifact-writer agent exactly like findings.json and report.md — but
// unlike them it is not data to be checked, it is the INSTRUCTION SET. `postReview.ids`
// is the sole authority for which finding ids reach the delivered post-review artifact,
// so a writer that elides two entries from that list produces a silently smaller
// delivered set with a self-consistent ok:true receipt and no gap. The two primaries had
// expect[].chars/checksum to prove them; the plan had nothing. So it now carries a
// checksum of itself, computed over the plan MINUS that field:
//
//   planChecksum = fnv1a32(JSON.stringify(planWithoutPlanChecksum, null, 2))
//
// Unambiguous in both runtimes because (a) the field is appended LAST, so deleting it
// on the Python side restores this exact object (both runtimes preserve insertion order
// and neither reorders on re-serialization), and (b) the serializer is the same pretty
// printer that produces the derived artifacts — which makes the plan checksum a canary
// for serializer divergence too: a Python/JS spelling difference over the plan's content
// fails the proof before a divergent artifact is written.
//
// THE DERIVED DOCUMENTS' CONTENT PROOF (`derive`, issue #38 F1-persist-1/F4-4). The two
// primaries are proven by `expect[].chars/checksum`; the two DERIVED documents —
// post-review.json and checkpoint-all.json, i.e. the payload that actually reaches the user
// — were gated on PATH PRESENCE only ("the script says it wrote something there"). So the
// plan also carries a { path, chars, checksum } expectation for each derived document,
// computed here from writerPayload(inp).postReview / .checkpoints through the SAME
// normalizeForChecksum + fnv1a32 the primaries use. It costs nothing at dispatch (~40 bytes
// each; the documents themselves are still never sent) and it makes the byte-identity claim
// PROVABLE for the delivered payload rather than merely argued.
//
// WHAT THIS IS NOT: authentication. The plan is on disk and the executor can read it, so a
// BYZANTINE executor could echo back any value the plan names — exactly the threat model
// trustSlice already documents for the verify receipt. This is a consistency/liveness check
// against a STALE, CONFUSED or HALLUCINATING executor (a receipt from another run, a
// half-finished derivation, an invented success), and against a real serializer divergence
// between the two runtimes. Do not build a security argument on top of it.
//
// WHY A DERIVED MISMATCH IS FATAL WHERE A PRIMARY MISMATCH IS NOT: a primary mismatch still
// leaves on-disk truth to derive from, so the run keeps its artifacts and raises a loud gap.
// A derived mismatch means the DERIVATION disagreed with the pipeline — there is no other
// copy of that document to fall back to — so trustAssembleReceipt treats it as structural.
//
// WHY THE ID LISTS STAY (the weak point is transcription, not encoding). No wire format
// removes the failure mode: the plan must cross an LLM agent's transcription to reach
// disk (the sandbox has no disk access at all), and no encoding stops an agent from
// writing fewer bytes than it was handed. The alternatives are strictly worse — deriving
// the delivery set on the Python side means reimplementing selectDelivery's ranking and
// cap in a second language, i.e. an order-sensitive stage whose divergence would silently
// change the delivered findings; and "all of findings.json, in order" is simply false,
// delivery is a ranked capped subset. So the list stays explicit and gains a proof.
export function persistPlan(inp, paths) {
  const { findingsJson, reportMd } = persistPrimaries(inp);
  const id = inp.prIdentity;
  const skeleton = deepClone(inp.checkpoints || {});
  const challenge = (skeleton.phases || {}).challenge;
  const challengeFindingIds = challenge && Array.isArray(challenge.findings)
    ? challenge.findings.map((f) => f && f.id)
    : [];
  if (challenge && Array.isArray(challenge.findings)) challenge.findings = [];
  const expectOf = (path, text) => {
    const normalized = normalizeForChecksum(text);
    return { path, chars: normalized.length, checksum: fnv1a32(normalized) };
  };
  // The derived documents, as the pipeline itself would serialize them — the same pretty
  // printer assemble_artifacts.py reproduces (js_stringify_pretty) and the same source
  // (writerPayload) the legacy by-value path persists. Only their chars/checksum travel.
  const held = writerPayload(inp || {});
  const plan = {
    planVersion: 2,
    expect: [expectOf(paths.findings, findingsJson), expectOf(paths.report, reportMd)],
    derive: [
      expectOf(paths.postReview, JSON.stringify(held.postReview, null, 2)),
      expectOf(paths.checkpoints, JSON.stringify(held.checkpoints, null, 2)),
    ],
    postReview: {
      path: paths.postReview,
      source: paths.findings,
      ids: (inp.postReview || []).map((f) => f && f.id),
      // Same envelope decision writerPayload makes (live-run L3, D16): with a PR
      // identity the artifact IS the post_review.py input wrapper; without one it is a
      // bare array. Key order is the wire contract — findings are appended last.
      wrapper: id
        ? { owner: id.owner, repo: id.repo, pr_number: id.pr_number, sha: id.sha_full, review_body: '' }
        : null,
    },
    checkpoint: {
      path: paths.checkpoints,
      source: paths.findings,
      challengeFindingIds,
      stripAliasFields: PERSIST_ALIAS_FIELDS,
      skeleton,
    },
  };
  // Appended LAST and computed over the object WITHOUT it — see the construction note
  // above. assemble_artifacts.py deletes exactly this key and recomputes.
  plan.planChecksum = fnv1a32(JSON.stringify(plan, null, 2));
  return plan;
}

// firstUnsafeNumber(root, rootPath) -> the path of the first number the Python twin
// could not spell identically, or null.
//
// JS numbers are doubles and Number#toString has its own spelling rules; Python's
// repr(float) does not share them (1e-7 vs 1e-07, 0.000001 vs 1e-06, 90 vs 90.0, 0 vs
// -0.0, null vs NaN). scripts/assemble_artifacts.py deliberately does NOT reimplement
// Number#toString — a port whose own bugs would be invisible is worse than a
// precondition — so it refuses any number it cannot round-trip and this guard applies
// the SAME rule one step earlier, where refusing is free: the run falls back to the
// legacy by-value writer instead of writing a divergent artifact or losing artifacts.
// Every number the pipeline actually produces is a count, a line number, or a
// confidence, so the precondition never binds in practice. Integers outside JS's safe
// range are rejected too: JS would already have parsed them lossily.
const JS_MAX_SAFE_INTEGER = 9007199254740991;

function firstUnsafeNumber(root, rootPath) {
  const stack = [[root, rootPath]];
  while (stack.length > 0) {
    const [node, where] = stack.pop();
    if (typeof node === 'number') {
      if (!Number.isInteger(node) || node > JS_MAX_SAFE_INTEGER || node < -JS_MAX_SAFE_INTEGER) return where;
      continue;
    }
    if (Array.isArray(node)) {
      for (let i = node.length - 1; i >= 0; i -= 1) stack.push([node[i], `${where}[${i}]`]);
      continue;
    }
    if (node && typeof node === 'object') {
      const entries = Object.entries(node);
      for (let i = entries.length - 1; i >= 0; i -= 1) {
        const [k, v] = entries[i];
        stack.push([v, `${where}.${k}`]);
      }
    }
  }
  return null;
}

// persistDerivable(inp) -> { ok } | { ok:false, reason }
// The guard on the derived path. Every projection is BY FINDING ID out of
// findings.json, so the derivation is only faithful when:
//   0. every number in the persisted content is a JS-safe integer (see
//      firstUnsafeNumber — otherwise the Python serializer spells it differently);
//   1. every finding carries a unique, non-empty string id;
//   2. every postReview / challenge entry has a twin in the findings set; and
//   3. that twin is byte-identical to it under the projection rules — including the
//      alias round trip, because toV2Aliased only ADDS an alias when absent, so a
//      finding that ALREADY carries `line`/`end_line`/`body` would silently lose it to
//      the checkpoint's alias strip.
// A refusal is NOT a degradation: writeArtifacts falls back to the legacy full
// by-value writer and records the reason as a gap. Pure and exported for direct tests.
export function persistDerivable(inp) {
  // Everything the derived documents are built from: findings.json's content, the
  // checkpoint skeleton, and the post-review envelope's pr_number.
  for (const [label, value] of [
    ['findings', (inp || {}).findings],
    ['postReview', (inp || {}).postReview],
    ['checkpoints', (inp || {}).checkpoints],
    ['prIdentity', (inp || {}).prIdentity],
  ]) {
    if (value === undefined || value === null) continue;
    const bad = firstUnsafeNumber(value, label);
    if (bad !== null) {
      return { ok: false, reason: `${bad} is not a JS-safe integer — the Python assembler cannot spell it byte-identically, so the derived artifact would diverge` };
    }
  }
  const findings = (inp || {}).findings || [];
  const byId = new Map();
  for (let i = 0; i < findings.length; i += 1) {
    const f = findings[i];
    const fid = f && f.id;
    if (typeof fid !== 'string' || fid === '') {
      return { ok: false, reason: `finding at index ${i} has no usable string id` };
    }
    if (byId.has(fid)) return { ok: false, reason: `duplicate finding id ${fid}` };
    byId.set(fid, f);
    if (JSON.stringify(stripPersistAliases(toV2Aliased(f))) !== JSON.stringify(f)) {
      return { ok: false, reason: `finding ${fid} already carries a v2 alias field (${PERSIST_ALIAS_FIELDS.join('/')}) — the checkpoint alias strip is not reversible` };
    }
  }
  const twinCheck = (list, label, project) => {
    for (let i = 0; i < list.length; i += 1) {
      const entry = list[i];
      const fid = entry && entry.id;
      if (typeof fid !== 'string' || !byId.has(fid)) {
        return `${label} entry at index ${i} (id ${fid === undefined ? 'missing' : fid}) is not present in the findings set`;
      }
      if (JSON.stringify(project(entry)) !== JSON.stringify(project(byId.get(fid)))) {
        return `${label} entry ${fid} differs from the findings entry with the same id`;
      }
    }
    return null;
  };
  const postReviewBad = twinCheck(inp.postReview || [], 'postReview', toV2Aliased);
  if (postReviewBad) return { ok: false, reason: postReviewBad };
  const challenge = ((inp.checkpoints || {}).phases || {}).challenge;
  const challengeFindings = challenge && Array.isArray(challenge.findings) ? challenge.findings : [];
  const challengeBad = twinCheck(challengeFindings, 'checkpoint challenge', (f) => f);
  if (challengeBad) return { ok: false, reason: challengeBad };
  return { ok: true };
}

// The DERIVED writer payload: `[{ path, text }]` — each `text` is written VERBATIM
// (the verify slice-input shape uses `content` and is written as JSON). Sending the
// exact string removes every reformatting degree of freedom, which is what makes the
// content-proof checksum meaningful.
function finalArtifactsWriterPrompt(entries) {
  const payload = JSON.stringify(entries);
  return `Persist these code-gauntlet artifacts to disk exactly as given (the workflow has no disk access). For every entry in the payload, write its "text" VERBATIM to its "path" — byte for byte, nothing before it and NOTHING AFTER THE FINAL BYTE (no trailing commentary, no tool-call markup). Do not reformat, re-indent, or re-serialize. Return { written } listing the paths you wrote. The payload is the single JSON line after the marker below.\n${WRITER_PAYLOAD_MARKER}${payload}`;
}

// The assemble receipt shape (scripts/assemble_artifacts.py's single stdout line).
const ASSEMBLE_RECEIPT_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    planVersion: { type: 'number' },
    // The plan checksum the script RECOMPUTED from the plan it read; trustAssembleReceipt
    // compares it against the pipeline's own so the receipt cannot grade itself.
    planChecksum: { type: 'string' },
    verified: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          path: { type: 'string' },
          chars: { type: 'number' },
          expected_chars: { type: 'number' },
          checksum: { type: 'string' },
          expected_checksum: { type: 'string' },
          content_proof: { type: 'string' },
        },
      },
    },
    written: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          path: { type: 'string' },
          chars: { type: 'number' },
          checksum: { type: 'string' },
        },
      },
    },
    errors: { type: 'array', items: { type: 'string' } },
  },
};

// The pinned command: a single `python3 <script> --plan <plan>` invocation of plain
// word tokens only (CLAUDE.md AST-safe emission — no command substitution, heredocs,
// env prefix, or shell operators), exactly like verifyCommand.
function assemblePrompt(scriptPath, planPath) {
  return `Run exactly this command, then return its single line of stdout JSON verbatim via the schema:\npython3 ${scriptPath} --plan ${planPath}`;
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
// consumes. Exactly ONE qualifies: `challenge` carries the delivered high-confidence findings
// and the `unverified` bucket that selectDelivery, the report input, and writeArtifacts read
// BY VALUE — replaying it is what makes the delivered set on a resume verbatim-identical.
//
// `filter` is deliberately NOT persisted (issue #38, P1). Its only resume consumers are
// `postFilterCount` (the empty-report guard) and `filterOut.stats` (report input + envelope),
// and on a resume from the persisted checkpoint `summarize`/`discover`/`verify`/`validate`
// re-run anyway (they were never persisted) — so `filter`, a PURE agent-free JS function
// (filterStage -> applyFilterPipeline), simply re-runs too at ZERO dispatch cost and both
// consumers are computed from the freshly re-derived set. Persisting it bought nothing and
// cost 35% of the checkpoint artifact's bytes in the profiled run.
//
// CONSEQUENCE FOR THE EMPTY-REPORT GUARD (issue #38, L2-1/L5-3): because filter re-runs
// while challenge is REPLAYED, on a resume `postFilterCount` describes a freshly
// rediscovered set, NOT the set being delivered. A resume that rediscovers nothing has
// postFilterCount 0 while the replayed challenge still carries real findings — so the guard
// in runWith keys on the UNION of postFilterCount and the delivered challenge count. Do not
// narrow it back to postFilterCount alone. Every OTHER phase
// contributes only a count/stat to the final envelope on a resume
// (discovered/merged/verified/validate.stats/filter.stats), never its findings bulk.
const PERSISTED_RESUME_PHASES = ['challenge'];

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
  // Two seams into args handling, one message (issue #27). pipeline_entry.js's
  // parseEntryArgs is the live naked-call path and THROWS on a refusal — a throw is the
  // only signal the platform renders as a failure (a returned ok:false reports as
  // <status>completed</status>, identical to success; see args.js's parseEntryArgs
  // comment). This arm exists because runWith's OWN contract is throw-free (this doc
  // comment already promised it "NEVER lets a throw escape") and empirically was not:
  // normalizeArgsReport's JSON.parse below used to sit outside any try/catch, so
  // `runWith(undefined, 'PR 310')` escaped as an uncaught native SyntaxError. So a refusal
  // here RETURNS the same entryArgs(rawArgs) refusal instead of throwing — same
  // refusalFrom wording as the entry, wrapped in makeArgsRejectEnvelope, so the wording
  // cannot drift between the two signals (pinned by a test). This arm is defensive, not
  // the primary guard: in production the entry throws first, so a live naked call never
  // reaches here. It is still worth fixing — runWith is exported, directly unit-tested,
  // and documented as throw-free.
  const entry = entryArgs(rawArgs);
  if (!entry.ok) return entry.envelope;
  // Normalization is TOLERANT of a stamped null for the narrow NULLABLE_TOP_LEVEL allowlist
  // (issue #38 A1 — a rejected dispatch cost a 21.3s round trip). Tolerance without
  // disclosure would be a silent config substitution, though: a mis-stamped
  // `reviewConfig: null` reviews on the Filter stage's built-in 55/70 instead of the
  // operator's REVIEW.md thresholds, changing the DELIVERED set. So every drop that actually
  // tolerated something becomes an operator-actionable gap here — the one place that owns the
  // returned envelope — and it rides on BOTH exits below (the reject envelope and the success
  // envelope). Drops validateArgs would have ACCEPTED anyway (`checkpoints`, which has no
  // shape check at all) are filtered out by nullToleranceRejectedKeys: nothing was tolerated,
  // so claiming a degradation would be gap-channel noise on a previously valid, silent run.
  // Normalize from entry.waist, not rawArgs: entryArgs has already unwrapped every JSON
  // layer, and normalizeArgsReport peels exactly one — re-normalizing the raw value would
  // hand validateArgs a string for any waist encoded more than once.
  const { args: A, dropped: droppedNulls } = normalizeArgsReport(entry.waist);
  const nullArgGaps = nullToleranceRejectedKeys(A, droppedNulls).map(nullToleranceGap);
  const check = validateArgs(A);
  if (!check.ok) {
    // The field list says WHAT is wrong; SKILL_RECOVERY_LINE says where the fields come
    // from. A naked caller that hand-built an object reads only this string (the platform
    // reports the run as completed either way), so it has to carry both. Shape comes from
    // makeArgsRejectEnvelope — same factory entryArgs uses for its refusal arm.
    return makeArgsRejectEnvelope(
      `invalid args: ${check.errors.join('; ')}. ${SKILL_RECOVERY_LINE}`,
      [...nullArgGaps, ...check.errors],
    );
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
  // The context file's own size, measured by the skill right after it writes the file
  // (the workflow has no disk and cannot measure it). Feeds contextReadPlan, which turns
  // it into the exact Read calls the prompt enumerates — issue #48. Both are OPTIONAL
  // because Phase 2 is model-executed and can skip the stamp; hard-failing there would
  // trade a partial read for a dead review. Absent (or unplannable) measurement falls
  // back to the count-free read-to-end wording — disclosed via a gap below, never silent.
  // Built ONCE, here, and threaded to the stages as a plain string. The stages are given
  // no path and no size, so none of them CAN name the shared context file without the read
  // plan — the invariant is structural, not asserted over this file's source text.
  const contextPlan = contextReadPlan(A.contextLines, A.contextChars);
  const contextLine = sharedContextLine({
    contextPath, contextLines: A.contextLines, contextChars: A.contextChars,
  });
  // DEGRADED IS NOT SILENT. Falling back to the count-free wording is legal (hard-failing
  // would trade a partial read for a dead run — a worse deal), but it drops the pipeline
  // back to the agent's own judgment about when it has read enough, which is exactly the
  // judgment that failed in #48. Two producers of that fallback must both disclose:
  //   1. contextLines absent — Phase 2 skipped the stamp (live; model-executed).
  //   2. contextLines stamped but contextReadPlan returns [] — the size clears the waist
  //      ceiling (5M lines) yet exceeds READ_PLAN_MAX_CHUNKS (~1.5M at the line cap). Checking
  //      only `=== undefined` left that second path silent: same fallback wording, zero gaps.
  // Same contract args.js states for a tolerated null: what was lost, and what to do about it.
  const contextSizeGap = A.contextLines === undefined
    ? ['context_unmeasured: args.contextLines was not stamped, so the shared-context read plan could not be computed — '
      + 'every agent got the count-free "read until a Read returns no further content" wording instead of the exact '
      + 'Read calls covering the file. That restores the failure mode of issue #48: a Read returns part of a large file '
      + 'with no truncation notice, and an agent that stops there reviews only what it saw. Stamp contextLines '
      + '(and contextChars) from the Phase 2 step that writes the context file.']
    : (contextPlan.length === 0
      ? ['context_unplannable: args.contextLines was stamped (' + A.contextLines + ') but exceeds the read-plan chunk '
        + 'ceiling, so the shared-context read plan could not be built — every agent got the count-free '
        + '"read until a Read returns no further content" wording instead of the exact Read calls covering the file. '
        + 'That restores the failure mode of issue #48 for an oversized context. Shrink the shared context '
        + '(or raise READ_PLAN_MAX_CHUNKS with a matching prompt-size budget) so a plan can be computed.']
      : []);
  const checkpoints = readCheckpoints(c, A);

  const gaps = [...nullArgGaps, ...contextSizeGap];
  const completed = [];
  const phaseOutputs = {}; // per-phase output map — persisted as the checkpoint artifact
  let phaseReached = 'start';
  // The phase currently being ATTEMPTED — distinct from phaseReached (last COMPLETED).
  // On a throw, phaseReached names the phase BEFORE the one that blew up; narrating the
  // crash from it misattributes the failure (live run: a Filter throw reported as
  // "failed during Validate"). The catch envelope carries both.
  let phaseAttempting = null;

  // Resume: a phase whose checkpoint is present reuses that output instead of
  // dispatching. Either way the phase counts as reached, and its output is recorded
  // into phaseOutputs so the persisted checkpoint artifact is a producible resume map.
  const runPhase = async (name, thunk) => {
    phaseAttempting = name;
    const out = checkpoints[name] !== undefined ? checkpoints[name] : await thunk();
    phaseOutputs[name] = out;
    completed.push(name);
    phaseReached = name;
    return out;
  };

  // Summarize and Discover have NO data dependency: summarize's output is first read at
  // reportInput, and discover's input is built only from A.agentFlags / limits / policy /
  // contextPath — `limits` being coarsenLimits(A.limits, nChangedFiles, 0), computed above,
  // before either. So both are STARTED here and awaited in order below. Four properties are
  // load-bearing and each is pinned by a test in stages_latency.test.js:
  //   1. Checkpoint semantics: a phase whose checkpoint is present must NOT dispatch, so the
  //      promise is only created when checkpoints[name] === undefined (null = replay it).
  //   2. Record ORDER: summarize is still awaited (and so recorded into phaseOutputs /
  //      completed / counts) BEFORE discover — both are consumer-visible in the artifact.
  //   3. No unhandled rejection: `settle` attaches its handlers the instant the promise is
  //      created, so a discover rejection arriving while summarize is still being awaited is
  //      captured, never floating. It is re-thrown at the point the phase is awaited.
  //   4. Error attribution: because the re-throw happens inside runPhase's thunk, a discover
  //      failure is still attributed to failingPhase 'discover', never 'summarize'.
  // settle(p) -> Promise<thunk>: the thunk returns the value or re-throws the error.
  const settle = (p) => p.then((value) => () => value, (error) => () => { throw error; });
  const replay = (name) => checkpoints[name] !== undefined;

  try {
    const summarizeSettled = replay('summarize') ? null : settle(summarize(c, {
      changedFiles: A.changedFiles || [], changedLines: A.changedLines || 0, limits, policy, contextLine,
    }));
    const discoverSettled = replay('discover') ? null : settle(discover(c, {
      agentFlags: A.agentFlags || {}, limits, policy, contextLine,
    }));

    const summaryOut = await runPhase('summarize', async () => (await summarizeSettled)());
    gaps.push(...(summaryOut.gaps || []));

    const discoverOut = await runPhase('discover', async () => (await discoverSettled)());
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
      findings: verifyOut.findings || [], limits, policy, contextLine,
    }));
    gaps.push(...(validateOut.gaps || []));

    const filterOut = await runPhase('filter', () => filterStage({
      findings: validateOut.findings || [], reviewConfig: A.reviewConfig || {},
      exclusionPatterns: A.exclusionPatterns || [], generatedAt: A.generatedAt,
    }));

    const challengeOut = await runPhase('challenge', () => challengeStage(c, {
      // No context line: challengeStage never read one, and challengePrompt takes only the
      // finding — the challenger is structurally blind (it gets title/description/location
      // and opens the code itself). A dead contextPath was threaded here until issue #48;
      // passing context to a stage that must not use it invites a future edit to "use the
      // context we already have" and quietly break the blindness the round exists for.
      findings: filterOut.filtered || [], limits, policy, generatedAt: A.generatedAt,
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
      // No contextPath (issue #38, R1): the report-writer renders from the by-value
      // { summary, findings, unverified, stats } above and never needs the shared context
      // file. Every OTHER stage still receives contextPath — this is scoped to the writer.
      policy, generatedAt: A.generatedAt,
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
    //
    // "findings present" is the UNION of THREE counts, not postFilterCount alone (issue #38,
    // L2-1/L5-3/F2-1). Since `filter` was dropped from the persisted checkpoint (P1), a resume
    // RE-RUNS summarize/discover/verify/validate/filter while REPLAYING challenge — so
    // postFilterCount is a freshly recomputed number about a set the run is not delivering,
    // while the delivered set comes from the replayed challenge output. A resume that
    // rediscovers nothing has postFilterCount 0, and the guard would go blind to a real
    // delivered set behind an empty report. deliveredCount (challengeOut.findings — exactly
    // what selectDelivery and the writer consume) closes that hole, and unverifiedCount
    // (challengeOut.unverified — the challenge-skipped / cap-overflow bucket the report is
    // CONTRACTUALLY required to render in its secondary section) closes the remaining one: a
    // replayed challenge can route every finding to that bucket, leaving both other counts 0
    // while the report still has real content to lose.
    // The union is a strict SUPERSET of the old condition, so it can never fire less often
    // than before: on a FRESH run challenge only ever removes findings, so postFilterCount >=
    // deliveredCount and the union reduces to postFilterCount > 0 with the message unchanged.
    const postFilterCount = (filterOut.filtered || []).length;
    const deliveredCount = (challengeOut.findings || []).length;
    const unverifiedCount = (challengeOut.unverified || []).length;
    const findingsAtRisk = postFilterCount > 0 || deliveredCount > 0 || unverifiedCount > 0;
    const reportIsEmpty = (out) => !out || typeof out.report !== 'string' || out.report.trim() === '';
    if (reportIsEmpty(reportOut) && findingsAtRisk && checkpoints.report !== undefined) {
      reportOut = await reportStage(c, reportInput);
      phaseOutputs.report = reportOut;
      gaps.push(...(reportOut.gaps || []));
    }
    const emptyReport = reportIsEmpty(reportOut) && findingsAtRisk;
    if (emptyReport) {
      // Word the gap from whichever count is actually non-zero: the fresh-run wording is
      // byte-identical to before, and the resume case names BOTH replayed buckets (delivered
      // and unverified) rather than reporting a misleading "0 survived the filter" — the
      // operator needs to know which one is at risk, since either can be the sole reason the
      // guard fired.
      gaps.push(postFilterCount > 0
        ? `empty_report: report stage produced no report while ${postFilterCount} finding(s) survived the filter — refusing to ship a silent empty report`
        : `empty_report: report stage produced no report while ${deliveredCount} finding(s) replayed from the resumed challenge checkpoint would be delivered and ${unverifiedCount} would be reported as unverified/pipeline-degraded — refusing to ship a silent empty report`);
    }

    // Persistence is a post-phase step: writeArtifacts owns its try/catch, so a
    // writer failure degrades to a partial-artifacts gap rather than the top-level catch.
    const writeOut = await writeArtifacts(c, {
      findings: challengeOut.findings,
      postReview,
      prIdentity: (A.delivery || {}).prIdentity, // L3: writer emits the post_review-ready wrapper when present
      report: reportOut.report,
      // Persist a SLIM checkpoint: only the resume-consumed phase (challenge) carries full
      // output; every other phase is reduced to a count, so the single artifact-writer
      // prompt no longer duplicates every phase's findings bulk by value. readCheckpoints
      // unwraps .phases, so a resume skips exactly the preserved phase and re-runs the rest.
      // The in-memory failure-path return below still carries the full phaseOutputs map.
      checkpoints: slimPersistedCheckpoints(phaseOutputs, completed, phaseReached),
      outputDir: A.outputDir,
      headShaShort: A.headShaShort,
      generatedAt: A.generatedAt,
      // Optional (issue #38, D3.4): with an assembleScriptPath the writer persists only
      // the unique content and the executor derives the two projections on disk. Absent
      // (bench, older callers) -> the legacy full by-value path, no gap.
      persist: A.persist,
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
      failingPhase: phaseAttempting,
      artifactPaths: {},
      stats: {},
      checkpoints: buildResumeCheckpoints(phaseOutputs),
      gaps,
    };
  }
}
