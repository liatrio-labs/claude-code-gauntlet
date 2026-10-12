// stages.js — orchestration stage functions for the code-gauntlet v3 pipeline: the stages
// (Summarize -> Discover -> Merge -> Verify -> Validate -> Filter -> Challenge -> Report;
// Verify lives in verifyStage.js over verifyWire.js and capacity.js), persistence,
// checkpoints and the runWith orchestrator, plus the agent-count coarsening formula that
// keeps the whole run's worst-case fan-out under the platform guard.
//
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
import { DIMENSIONS, AGENTS, modelFor, FINDING_PROP_TYPES, FINDING_REQUIRED, FINDING_TEXT_FIELD_NAMES, conditionalSchemaActive } from './registry.js';
import { merge, normalizeFindingPaths } from './mergeFindings.js';
import { mentionsPreparedHostRoot, prepareHostRootPatterns, safeFindingLabel } from './paths.js';
import { INT_POLICY, coerceInt } from './wire.js';
import { VERIFY_INLINE_CHAR_BUDGET } from './verifyWire.js';
import { VERIFY_ATTEMPTS_PER_SLICE, effectiveSliceSize, planVerifySlices } from './capacity.js';
import { verifyStage } from './verifyStage.js';
import { applyValidations, REACHABILITY_VALUES } from './applyValidations.js';
import { applyFilterPipeline, scopeMatchesFile } from './filterFindings.js';
import { applyChallenges, rankFindings } from './applyChallenges.js';
import { normalizeArgsReport, nullToleranceGap, nullRespellGap, nullToleranceRejectedKeys, validateArgs, entryArgs, makeArgsRejectEnvelope, SKILL_RECOVERY_LINE, LIMIT_DEFAULTS, resolveReviewConfig, computeLightEligible, configEchoValue } from './args.js';
import { renderReport, renderSummaryBody, coerceReportFindings } from './renderReport.js';
import { writeArtifacts } from './persist.js';
import { beltPartitionList, buildResumeCheckpoints, checkpointDiscardGap, checkpointFindingListsForPhase, checkpointShapeErrors, isPlainCheckpointObject, makeCheckpointShapeRejectEnvelope, readCheckpoints, slimPersistedCheckpoints, stripEliminatedList } from './checkpoints.js';

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
  const bucketSize = effectiveBucketSize(limits);
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
  } catch {
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

// intersectRequiredExtra(rows) -> string[] (issue #66). The safe algebra for a multi-dimension
// agent's effective requiredExtra: a field is dispatch-required only if EVERY row (dimension)
// sharing that agent's one dispatch requires it. Union semantics would be unsafe — promoting a
// single-dimension's required field (e.g. a hypothetical convention-only requirement) onto the
// whole conventions-and-intent dispatch would reject every intent/comment_accuracy finding that
// correctly omits it. Order-independent (sorted) so the result never depends on DIMENSIONS row
// iteration order. Exported standalone so the intersection can be exercised with synthetic rows
// without needing to mutate the module-level registry.
//
// registry.test.js's sibling-parity test ("rows sharing an agentType declare identical
// requiredExtra sets") is the actual AUTHORITY that keeps this safe on the real registry: it
// fails the build the moment a multi-dimension agent's rows disagree, so on a healthy tree this
// intersection is an IDENTITY (every sibling already agrees, so nothing is ever actually
// dropped). This function is not what makes divergence safe — that guard is. It is the
// fail-safe ALGEBRA underneath: if a divergence somehow reached runtime anyway (a bypassed
// guard, a registry built by different code some day), the intersection still degrades safely
// — dropping the disputed field from `required` — instead of the union's failure mode, which
// would force every compliant OMIT-ing finding on a sibling dimension to fabricate a value it
// was correctly told to omit.
export function intersectRequiredExtra(rows) {
  if (!rows || rows.length === 0) return [];
  let acc = new Set(rows[0].requiredExtra || []);
  for (const row of rows.slice(1)) {
    const next = new Set(row.requiredExtra || []);
    acc = new Set([...acc].filter((f) => next.has(f)));
  }
  return [...acc].sort();
}

// Group DIMENSIONS by agentType, unioning each agent's per-dimension schemaExtra into
// one finding schema. One task per unique AGENT (7) — agents covering several
// dimensions (conventions-and-intent -> convention/intent/comment_accuracy) dispatch once.
// `requiredExtra` on the returned spec is the INTERSECTION of that agent's rows' requiredExtra
// (intersectRequiredExtra above), not a union like schemaExtra — see the registry.js note on
// why a per-dimension required field cannot safely promote onto sibling dimensions.
//
// Takes `dims` (defaults to the module-level DIMENSIONS) so the intersection-through-grouping
// seam can be exercised with synthetic rows in tests, independent of the real registry. Agent
// order is derived from `dims` itself (`[...new Set(dims.map(d => d.agentType))]`), NOT from
// the module-level AGENTS constant — mapping over AGENTS here would look up entries by an
// agentType a synthetic `dims` never populated and return undefined specs. For the default
// call this produces the byte-identical order AGENTS already pins (see registry.test.js).
export function agentSpecs(dims = DIMENSIONS) {
  const byAgent = new Map();
  for (const d of dims) {
    if (!byAgent.has(d.agentType)) byAgent.set(d.agentType, { agentType: d.agentType, dimensions: [], schemaExtra: {}, conditionalFlags: [], promptExtra: null, rows: [] });
    const spec = byAgent.get(d.agentType);
    spec.dimensions.push(d.dimension);
    Object.assign(spec.schemaExtra, d.schemaExtra || {});
    spec.conditionalFlags.push(d.conditionalFlag);
    spec.rows.push(d);
    // promptExtra is scoped per AGENT, not per dimension — every DIMENSIONS row for a
    // multi-dimension agent is expected to carry the same value (see registry.js), so a
    // truthy value on any of an agent's rows sets it for the whole spec.
    if (d.promptExtra) spec.promptExtra = d.promptExtra;
  }
  const order = [...new Set(dims.map((d) => d.agentType))];
  return order.map((a) => {
    const spec = byAgent.get(a);
    spec.requiredExtra = intersectRequiredExtra(spec.rows);
    // conditionalRequired (issue #218): one { dimension, required } entry per row that
    // declares a non-empty requiredWhenDimension, sorted by dimension so the derivation
    // never depends on DIMENSIONS row order. Defensive `|| []` on the read — synthetic rows
    // built for tests (registry.test.js) omit the key entirely, and this must not throw.
    // Empty on every single-dimension spec (and on the one of conventions-and-intent's three
    // rows — comment_accuracy — that carries none) by construction, so findingItemSchema below
    // emits nothing extra for them regardless of policy.
    spec.conditionalRequired = spec.rows
      .filter((r) => (r.requiredWhenDimension || []).length)
      .map((r) => ({ dimension: r.dimension, required: [...r.requiredWhenDimension].sort() }))
      .sort((a1, b1) => a1.dimension.localeCompare(b1.dimension));
    delete spec.rows;
    return spec;
  });
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

// deriveAgentFlags(riskTable, changedLines, scopeAnswer) -> { deep: false } | {} (issue #24
// req 1/4/5, PR3). The pipeline's OWN, tested derivation of the scope-gating map agentActive
// consumes — replaces the orchestrator's SKILL.md-prose "stamp agentFlags at assembly time"
// rule with deterministic JS. Behavior-preserving: the eligibility test is byte-identical to
// the rule phase2-triage.md 2e documented (computeLightEligible, args.js — the ONE place it
// lives, shared with validateArgs's pre-dispatch coherence guard so the two can never drift).
//
// validateArgs already refused an incoherent waist before runWith ever calls this (scopeAnswer
// 'light' while not eligible, or eligible with no scopeAnswer) — see its "Fail-loud coherence"
// block. So by the time this runs, only two coherent cases can produce `{ deep: false }`:
// lightEligible with scopeAnswer 'light'. Every other coherent combination (not eligible,
// eligible-but-full, not-eligible-with-no-answer) is full scope.
export function deriveAgentFlags(riskTable, changedLines, scopeAnswer) {
  const lightEligible = computeLightEligible(riskTable, changedLines);
  return (lightEligible && scopeAnswer === 'light') ? { deep: false } : {};
}

// allActiveDimensionsDegraded(dispatched, degraded) -> boolean (issue #178).
// True when every dimension owned by a dispatched agent is in the degraded list.
// Dimension-level and scope-relative on purpose: a light-scope run whose two active
// agents both fail must fail loud too, and the comparison stays correct if degradation
// ever becomes finer than per-agent.
//
// FAIL-CLOSED arm: if `dispatched` resolves to zero active dimensions — agentSpecs()
// no longer recognizes an agentType in it (a registry rename between the run that wrote
// a replayed checkpoint and this one) or `dispatched` is absent entirely — this is
// UNRESOLVABLE scope, not "nothing was dispatched, so nothing degraded." Reading it as
// "not all degraded" would let a replay with unresolvable scope and something already in
// `degraded` sail through as an ok:true empty clean review: the exact silent success this
// guard exists to stop. So an empty activeDims returns whether anything was reported
// degraded at all, not false. The call site's findings-empty conjunct is what keeps this
// arm from ever aborting a replay that actually carries findings (see runWith).
export function allActiveDimensionsDegraded(dispatched, degraded) {
  const dispatchedSet = new Set(dispatched || []);
  const degradedSet = new Set(degraded || []);
  const activeDims = agentSpecs()
    .filter((spec) => dispatchedSet.has(spec.agentType))
    .flatMap((spec) => spec.dimensions);
  if (activeDims.length === 0) return degradedSet.size > 0;
  return activeDims.every((d) => degradedSet.has(d));
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
// An UNDECLARED property is REJECTED at dispatch, not silently dropped: `additionalProperties:
// false` makes a field no one declared a schema violation the platform retries, rather than
// pass-through tolerated at random (issue #53 watched one undeclared field survive 0/8, then
// 8/8, then 5/5 across three PRs of a single smoke — permitted, never a contract). Closing the
// item schema is safe because the declaration is complete and STAYS complete: every field an
// agent contract instructs is one entry in FINDING_PROP_TYPES or a dimension's `schemaExtra` in
// registry.js, and tests/test_registry_contracts.py fails the build when the declared set
// drifts from what the .md contracts instruct (declared − instructed == {origin}), so the
// closed schema cannot reject a field a contract asks for (issue #47: `suggestion`/
// `claude_md_rule`/`spec_text`/`criticality`/`failure_scenario` were instructed by the
// contracts, declared by nothing, and dropped at this boundary on every run). The blast radius
// is ONE boundary — this item schema reaches only the per-agent discovery dispatch
// (findingSchema below); verify echoes per-id deltas, validate per-id confidence adjustments,
// challenge a score, none of them finding items. `agent`, the one field whose undeclared
// survival ever mattered (the filter's cross-agent dedup keyed on it), is not emitted by
// discovery at all: discover() stamps it in code after StructuredOutput returns and
// joinVerifyDeltas deletes it — both deterministic.
// Entries are EITHER a type-name shorthand string ({ k: 'string' } -> { type:'string' }) OR a
// full JSON-Schema fragment used verbatim (how array-valued fields like cross_file_refs and
// affected_consumers are declared); the shorthand keeps the common case terse while the
// fragment form supports arrays the platform's schema validator requires `items` on.
// schemaExtra wins on a key collision — a dimension may narrow a canonical field, never the
// reverse. `required` is FINDING_REQUIRED plus the dispatch's effective `requiredExtra`
// (issue #66, agentSpecs' intersection) — empty for bug-detector, conventions-and-intent and
// type-design-analyzer, so those three dispatch the byte-identical flat list they always did.
//
// The promotion's blast radius (why registry.js's requiredExtra comment demands
// contract-UNCONDITIONAL fields only): a `required` array is a hard StructuredOutput
// constraint on every element of `findings`, so ONE finding omitting a required field makes
// the whole `{ findings, complete, total_seen }` envelope a schema violation, not just that
// one finding — the platform retries the WHOLE agent dispatch (cap 5) with the validation
// error fed back. If retries exhaust, `parallel()` resolves that member to null and discover()
// treats it as a terminal agent failure: EVERY dimension that agent owns degrades (see
// discover's null-member handling below), not just the dimension whose field was missing.
// A conditionally-omitted field (claude_md_rule, spec_text, hidden_errors,
// invalid_state_example) promoted here would make that omission — which the contract itself
// tells the model to do — a self-inflicted retry storm on every dimension the agent covers.
// `conditionalRequired` (issue #218) is the ONLY caller-gated argument here: findingSchema
// below passes spec.conditionalRequired only when conditionalSchemaActive(policy) is true, so
// this function itself stays a pure function of its inputs — every other agent's spec has an
// empty conditionalRequired regardless of policy, so this construct never appears there at
// all. When non-empty, this appends EXACTLY the measured-accepted spelling — a per-dimension
// `allOf`/`if`/`then` nested inside the item, never a top-level oneOf/allOf/anyOf (API 400,
// measured 2026-08-18). NO enum on `dimension` [delta round]: an enum was considered to close
// the case-sensitive `const` match's case-variant escape, but filterFindings.js's own
// normalization sets (`comment-accuracy`, `documentation`, `doc-accuracy`) are evidence agents
// emit variant dimension spellings the pipeline already tolerates elsewhere — pinning an enum
// here would turn one of those observed, tolerated spellings into a whole-dispatch schema
// violation (all three conventions-and-intent dimensions degrade) to close a narrower escape.
// Accepted as a fail-open risk (contract enforcement only) rather than traded for that.
function findingItemSchema(schemaExtra, requiredExtra, conditionalRequired) {
  const props = {};
  for (const [k, t] of Object.entries({ ...FINDING_PROP_TYPES, ...(schemaExtra || {}) })) {
    props[k] = typeof t === 'string' ? { type: t } : t;
  }
  const required = requiredExtra && requiredExtra.length ? [...FINDING_REQUIRED, ...requiredExtra] : FINDING_REQUIRED;
  const itemSchema = { type: 'object', properties: props, required, additionalProperties: false };
  if (conditionalRequired && conditionalRequired.length) {
    itemSchema.allOf = conditionalRequired.map(({ dimension, required: dimRequired }) => ({
      if: { properties: { dimension: { const: dimension } }, required: ['dimension'] },
      then: { required: dimRequired },
    }));
  }
  return itemSchema;
}

// Canonical finding schema (per-dimension schemaExtra unioned on top), wrapped in the
// per-agent result envelope { findings, complete, total_seen }. REAL JSON Schema —
// {type, properties, required, items} — because the platform validates schemas before
// dispatch and StructuredOutput enforces them (shorthand {id:'string'} is rejected).
// schemaExtra is shorthand {key: typeName} (or a full JSON-Schema fragment for arrays).
//
// `policy` gates the conditional-required construct (issue #218): spec.conditionalRequired is
// threaded through only when conditionalSchemaActive(policy) is true (first-party-direct, no
// gateway). Every other run — third-party providers, gateway sessions, or a spec that simply
// has no conditional rows — dispatches the byte-identical flat schema it always did; contract
// prose (agents/conventions-and-intent.md) stays the enforcement floor everywhere.
function findingSchema(spec, policy) {
  const conditionalRequired = conditionalSchemaActive(policy) ? (spec.conditionalRequired || []) : [];
  return {
    type: 'object',
    properties: {
      findings: {
        type: 'array',
        items: findingItemSchema(spec.schemaExtra, spec.requiredExtra, conditionalRequired),
      },
      complete: { type: 'boolean' },
      total_seen: { type: 'number' },
    },
    required: ['findings', 'complete', 'total_seen'],
  };
}

// Shared by the two discover-side drop branches below: a dropped suggested_fix_code is
// always the same operation (delete-and-report-whether-it-was-there), only the reason for
// dropping it differs between call sites. The report renderer owns its separate exclusion
// list through reportExtraFields().
function dropSuggestedFixCode(f) {
  if (!('suggested_fix_code' in f)) return false;
  delete f.suggested_fix_code;
  return true;
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
// collected, dimension not degraded). runWith fails loud (ok:false) instead of returning
// this output's empty findings as a clean review when EVERY active dimension degrades —
// see allActiveDimensionsDegraded and its guard in runWith (issue #178).
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
      schema: findingSchema(spec, policy),
    });
  });

  const results = await c.parallel(thunks);

  const gaps = [];
  const findings = [];
  const degradedDims = [];

  // conditionalSchemaActive is policy-only (not per-spec), so compute it once outside the
  // loop — whether a GIVEN spec actually carried the construct still depends on that spec's
  // own conditionalRequired (empty for every agent but conventions-and-intent).
  const schemaActive = conditionalSchemaActive(policy);

  // parallel() resolves a failed member to null IN PLACE (Phase 0 verified): the
  // results array is positionally aligned with `thunks`, so results[i] pairs with specs[i].
  results.forEach((res, i) => {
    const spec = specs[i];
    if (res === null || res === undefined) {
      // issue #218: a dispatch failure on a spec that carried the conditional allOf/if/then
      // construct is worth distinguishing in the gap text — the construct is the one thing
      // about this dispatch's schema that differs from every other run, so it is the first
      // thing an operator diagnosing a schema-retry-exhaustion failure should rule in or out.
      const conditionalNote = schemaActive && (spec.conditionalRequired || []).length
        ? ' (dispatch carried the conditional per-dimension schema)'
        : '';
      gaps.push(`${spec.agentType}: agent returned null (dispatch failed) — dimensions ${spec.dimensions.join('/')} not covered${conditionalNote}`);
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

  // Issue #204: bound an implausible discovery-agent line_end at intake, before anything
  // reaches mergeStage. A 2026-08-17 live run had an agent emit line_start=68,
  // line_end=940 (an ~872-line span crossing hunks) — post_review.py's delivery-side fix
  // (PR #200) now falls back to a single-line comment rather than 422ing the whole
  // review, but the emission-side bound belongs HERE too: a span this implausible is
  // agent noise, not a real multi-line finding, and letting it through just makes every
  // downstream stage (and the delivery fallback) carry dead weight. DROP `line_end`
  // rather than clamp it — a clamped value is a fabricated line number nobody reported;
  // the finding survives, anchored at its (still-valid) line_start, same as a finding
  // that never had an end line. maxLineSpan is a caller-tunable limit (LIMIT_DEFAULTS),
  // not a hardcoded literal, so a codebase with legitimately large multi-line findings
  // can raise it.
  const maxLineSpan = effectiveMaxLineSpan(limits);
  let droppedLineSpans = 0;
  let droppedNoLineEnd = 0;
  let droppedFixForImplausibleSpan = 0;
  for (const f of findings) {
    const lineEnd = f.line_end;
    if (lineEnd === undefined || lineEnd === null) {
      // Round-1 #63 fix (F4): a finding that never emitted line_end has no stated
      // replacement range for suggested_fix_code to apply against — six stages later
      // that's a guaranteed missing_end_line downgrade at delivery, so the field is
      // dead payload from here on. Drop it now rather than carry it through every
      // downstream stage; the finding itself survives, anchored at line_start, same
      // as before.
      if (dropSuggestedFixCode(f)) droppedNoLineEnd += 1;
      continue;
    }
    const span = lineEnd - f.line_start;
    if (!Number.isInteger(lineEnd) || span < 0 || span > maxLineSpan) {
      delete f.line_end;
      // Issue #63: suggested_fix_code states its replacement range as line_start..line_end.
      // Dropping line_end just destroyed that stated range, so a fence built from it would
      // mis-apply at whatever single-line anchor line_start now resolves to — delete it in
      // the same breath rather than let a now-unanchored patch survive to delivery. Count it
      // too (only when it was actually present) — the sibling missing-line_end branch counts
      // its drop and says so in its own gap; a silent discard here left the same class of
      // loss unreported and unmeasured.
      if (dropSuggestedFixCode(f)) droppedFixForImplausibleSpan += 1;
      droppedLineSpans += 1;
    }
  }
  if (droppedLineSpans > 0) {
    const fixClause = droppedFixForImplausibleSpan > 0
      ? ` — ${droppedFixForImplausibleSpan} of those also carried suggested_fix_code: patch field(s) dropped along with the range`
      : '';
    gaps.push(`discover: ${droppedLineSpans} finding(s) had an implausible line_end (non-integer, before line_start, or spanning more than maxLineSpan=${maxLineSpan} lines) — line_end dropped, finding kept anchored at line_start${fixClause}`);
  }
  if (droppedNoLineEnd > 0) {
    gaps.push(`discover: ${droppedNoLineEnd} finding(s) emitted suggested_fix_code without line_end — field dropped (a patch must state its range)`);
  }

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

  // Merge uses the short agent names stamped on findings, while dispatched carries the
  // full agent types. Keep the roster so zero-finding agents remain visible in methodology.
  const shortAgentName = (a) => (typeof a === 'string' ? a.split(':').pop() : a);
  const agents = Array.isArray(out.dispatched)
    ? out.dispatched.map(shortAgentName)
    : (Object.keys(ndjsonContents).length ? Object.keys(ndjsonContents) : AGENTS.map(shortAgentName));
  return merge(ndjsonContents, {}, { ...M, agents });
}

// --- Agent-count coarsening -------------------------------------------------

const AGENT_COUNT_GUARD = 900;   // stay strictly under the platform fan-out ceiling
const SUMMARIZE_TERM_BOUND = 300; // widen the summarize bucket once its term alone exceeds this
const CHALLENGE_CAP_FLOOR = 5;    // never challenge fewer than this many findings

const ceilDiv = (n, d) => Math.ceil(Math.max(0, n) / Math.max(1, d));

// Issue #24 req 7 (C): the SINGLE accessor path for a stage's effective limit — summarize,
// verifyStage, validateStage, and challengeStage all call these directly rather than
// re-deriving their own fallback, so worstCaseAgentCount/coarsenLimits can never count a
// different fan-out than the stage actually dispatches. normalizeArgs (args.js
// LIMIT_DEFAULTS) fills summarizeBucketSize/validateBatch/verifySliceSize/challengeCap on
// every real waist before a stage ever sees `limits`, so the `|| <literal>` fallbacks below
// are now the DEFENSIVE case (a caller that built `limits` by hand, a test, or a bare
// `coarsenLimits({}, …)` call) rather than the common one. Keep them anyway: a literal
// default here still has to agree with LIMIT_DEFAULTS, but that agreement is not load-
// bearing for a normalized waist.
//
// Mirror challengeStage's cap semantics EXACTLY: an absent/null challengeCap means
// "challenge every finding" (the stage defaults to findings.length), while 0 is a real
// cap of zero. The guard math must never undercount the stage's actual fan-out.
const effectiveChallengeCap = (L, findings) =>
  Math.max(0, L.challengeCap != null ? L.challengeCap : findings);

// Mirror each stage's own absent/zero-size default EXACTLY (summarize: bucket
// LIMIT_DEFAULTS.summarizeBucketSize; verify/validate: ONE slice/batch over all findings) so the
// guard arithmetic can never go NaN (Math.max(1, undefined) is NaN — a NaN worst case silently
// disables the coarsening loop) and never counts a different fan-out than the stage dispatches.
// The four LIMIT_DEFAULTS numbers live in exactly one place (workflows/src/args.js); these
// helpers read it rather than restating a literal.
const effectiveBucketSize = (L) => Math.max(1, L.summarizeBucketSize || LIMIT_DEFAULTS.summarizeBucketSize);
const effectiveBatchSize = (L, findings) => Math.max(1, L.validateBatch || findings || 1);

function findingCount(findings) {
  return Array.isArray(findings) ? findings.length : Math.max(0, findings || 0);
}

function verifyTerm(L, findings, baseBranch = 'main') {
  const count = findingCount(findings);
  if (Array.isArray(findings)) {
    return planVerifySlices(findings, effectiveSliceSize(L, count), VERIFY_INLINE_CHAR_BUDGET, baseBranch).slices.length
      * VERIFY_ATTEMPTS_PER_SLICE;
  }
  return ceilDiv(count, effectiveSliceSize(L, count)) * VERIFY_ATTEMPTS_PER_SLICE;
}

// Issue #204: the discovery-intake line-span bound (see its call site in discover()).
// Same accessor pattern as its siblings above — read the ONE LIMIT_DEFAULTS constant
// rather than restating the literal here.
const effectiveMaxLineSpan = (L) => Math.max(1, L.maxLineSpan || LIMIT_DEFAULTS.maxLineSpan);

// worstCaseAgentCount(limits, nFiles, findings, baseBranch) -> number
// summarize buckets (+1 merge) + the 7 discovery agents + verify slices + validate
// batches + min(nFindings, challengeCap) challengers + 2 (report + writer).
//
// The verify term counts VERIFY_ATTEMPTS_PER_SLICE dispatches per slice, because every
// slice can independently take its one retry (verifySliceWithRetry) and a
// worst case that assumes the happy path is not a bound. The remaining terms are still
// NOMINAL: the report/writer pair can segment, and writeArtifactsDerived has its own
// single retry, neither of which is counted here. That looseness is covered by the
// headroom between AGENT_COUNT_GUARD and the platform ceiling; the verify term is the one
// that scales with finding count, which is why it is the one made exact.
export function worstCaseAgentCount(limits, nFiles, nFindings, baseBranch = 'main') {
  const L = limits || {};
  const files = Math.max(0, nFiles || 0);
  const findings = findingCount(nFindings);
  const summarizeCalls = ceilDiv(files, effectiveBucketSize(L)) + 1;
  const verifyCalls = verifyTerm(L, nFindings, baseBranch);
  const validateCalls = ceilDiv(findings, effectiveBatchSize(L, findings));
  const challengeCalls = Math.min(findings, effectiveChallengeCap(L, findings));
  return summarizeCalls + AGENTS.length + verifyCalls + validateCalls + challengeCalls + 2;
}

// coarsenLimits(limits, nFiles, nFindings, baseBranch) -> limits
// Iteratively pulls the worst-case count below the guard. A candidate that no longer
// reduces its term is removed, which is required once the inline budget makes the
// verify fan-out size-bound.
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
export function coarsenLimits(limits, nFiles, nFindings, baseBranch = 'main') {
  const L = { ...(limits || {}) };
  const files = Math.max(0, nFiles || 0);
  const findings = findingCount(nFindings);
  const candidates = new Set(['summarize', 'verify', 'validate', 'challenge']);

  while (worstCaseAgentCount(L, files, nFindings, baseBranch) >= AGENT_COUNT_GUARD && candidates.size > 0) {
    const summarizeTerm = ceilDiv(files, effectiveBucketSize(L)) + 1;
    if (summarizeTerm > SUMMARIZE_TERM_BOUND && candidates.has('summarize')) {
      // Double from the EFFECTIVE size (pinning a concrete value): doubling from a raw
      // absent field (|| 1 -> 2) would LOWER the effective bucket below the stage's
      // default of 20 and move the term the wrong way.
      const before = summarizeTerm;
      L.summarizeBucketSize = effectiveBucketSize(L) * 2;
      if (ceilDiv(files, effectiveBucketSize(L)) + 1 >= before) candidates.delete('summarize');
      continue;
    }
    // Same VERIFY_ATTEMPTS_PER_SLICE scaling worstCaseAgentCount applies, so the "reduce
    // whichever term is largest" choice is made against each term's real contribution to
    // the count it is trying to pull down. With an array of actual findings, doubling
    // verifySliceSize can stop reducing the term once the inline budget binds; the update
    // below removes that candidate when its measured term does not change.
    const currentVerifyTerm = verifyTerm(L, nFindings, baseBranch);
    const validateTerm = ceilDiv(findings, effectiveBatchSize(L, findings));
    const challengeTerm = Math.min(findings, effectiveChallengeCap(L, findings));
    const choices = [
      ['validate', validateTerm],
      ['verify', currentVerifyTerm],
      ['challenge', challengeTerm],
    ].filter(([name]) => candidates.has(name));
    if (choices.length === 0) break;
    choices.sort((a, b) => b[1] - a[1]);
    const [largest] = choices;
    if (largest[0] === 'validate') {
      L.validateBatch = effectiveBatchSize(L, findings) * 2;
      if (ceilDiv(findings, effectiveBatchSize(L, findings)) >= validateTerm) candidates.delete('validate');
    } else if (largest[0] === 'verify') {
      L.verifySliceSize = effectiveSliceSize(L, findings) * 2;
      if (verifyTerm(L, nFindings, baseBranch) >= currentVerifyTerm) candidates.delete('verify');
    } else {
      // Halve the EFFECTIVE cap (min(cap, findings)) so C strictly decreases even when
      // the nominal cap already exceeds nFindings — or is absent (= findings).
      L.challengeCap = Math.max(CHALLENGE_CAP_FLOOR, Math.floor(Math.min(effectiveChallengeCap(L, findings), findings) / 2));
      if (Math.min(findings, effectiveChallengeCap(L, findings)) >= challengeTerm) candidates.delete('challenge');
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
  const c = ctx || defaultCtx();
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
  const c = ctx || defaultCtx();
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

// resolvedPolicyEnvelope(policy) -> the envelope's resolvedPolicy field, shared by the
// success return and the all-degraded failure return so the two report the identical
// resolution and a change to either fallback costs one edit.
// 'firstParty' when the waist omitted provider — the omission and the explicit value
// resolve identically in resolvePolicy, and the envelope reports the resolution.
//
// `gateway`/`conditionalSchema` (issue #218): a provider that would 400 on the conditional
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

// #181: merge()'s per-agent channel/dedup/validation-warning granularity — the only
// place it exists — reaches a consumer on the compact return. Count fields ride as-is; free-text
// warning ARRAYS ride as lengths, so the compact-return principle (names/counts, never bulk)
// holds. Adding a methodology COUNT field is one edit in mergeFindings.js; adding a
// FREE-TEXT one is two — that entry plus one name here. Stated plainly rather than made
// clever: a heuristic that guessed at array-of-strings would silently truncate a future
// count array.
export const METHODOLOGY_COUNT_ONLY_FIELDS = ['truncation_warnings', 'validation_warnings'];
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
  const contextPlan = contextReadPlan(A.contextLines, A.contextChars);
  const contextLine = sharedContextLine({
    contextPath, contextLines: A.contextLines, contextChars: A.contextChars,
  });
  // Missing and unplannable counts use the same count-free fallback, and both need disclosure.
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

  // Replayed findings need the same path validation as fresh output.
  const normalizePhaseFindings = (name, out, replayed) => {
    if (!out || typeof out !== 'object') return;
    const warnings = [];
    let rejected = 0;
    let pathRewrites = 0;
    for (const [, field] of checkpointFindingListsForPhase(name)) {
      if (!Array.isArray(out[field])) continue;
      const normalized = normalizeFindingPaths(out[field], A.repoRoot);
      rejected += out[field].length - normalized.valid.length;
      pathRewrites += normalized.path_rewrites;
      out[field] = normalized.valid;
      warnings.push(...normalized.warnings);
    }
    if (pathRewrites || (replayed && rejected)) {
      const stats = isPlainCheckpointObject(out.stats) ? out.stats : {};
      out.stats = { ...stats,
        ...(pathRewrites ? { path_rewrites: (stats.path_rewrites || 0) + pathRewrites } : {}),
        ...(replayed && rejected ? { replay_path_rejected: rejected } : {}),
      };
    }
    if (name === 'merge') {
      const methodology = isPlainCheckpointObject(out.methodology) ? out.methodology : {};
      out.methodology = methodology;
      const channels = methodology.findings_per_channel;
      const surviving = out.findings.length;
      // Attribution is approximate because replayed findings carry no channel.
      const ndjsonCount = Number.isFinite(channels?.ndjson) ? channels.ndjson : 0;
      const textFallbackCount = Number.isFinite(channels?.text_fallback) ? channels.text_fallback : 0;
      const ndjson = Math.min(Math.max(0, ndjsonCount), surviving);
      const textFallback = Math.min(Math.max(0, textFallbackCount), surviving - ndjson);
      if (channels && typeof channels === 'object') {
        out.methodology = { ...methodology, findings_per_channel: {
          ...channels, ndjson, text_fallback: textFallback,
        } };
      }
      if (warnings.length) {
        out.methodology = { ...out.methodology, validation_warnings: [
          ...(Array.isArray(methodology.validation_warnings) ? methodology.validation_warnings : []),
          ...warnings,
        ] };
      }
    }
    if (!warnings.length) return;
    out.gaps = [...(Array.isArray(out.gaps) ? out.gaps : []), ...warnings];
  };

  // Reuse checkpoint outputs without dispatching, while recording each reached phase.
  // Only undefined dispatches; null means replay and is refused by the shape gate.
  const runPhase = async (name, thunk) => {
    phaseAttempting = name;
    const replayed = checkpoints[name] !== undefined;
    const out = replayed ? checkpoints[name] : await thunk();
    // Discover is the entry to every downstream finding-bearing phase.
    if (replayed || name === 'discover') normalizePhaseFindings(name, out, replayed);
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
      delete resumable.discover; // a degraded-to-nothing discover output must never replay on resume — a retry re-dispatches discovery under (possibly corrected) policy
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

    // No finding reader may sit between runPhase('challenge') and this belt.
    // Replay skips the fresh filter stage, so re-scan challenge findings on both paths.
    // Exclude the confidence heuristic because later confidence adjustments can change its result.
    // Scan each list in order so the dedup heuristic sees the same sequence on replay.
    // Strip eliminated entries after combining them so no eliminated-only payload survives.
    // Replace the challenge arrays in place so reporting, delivery, and persistence share them.
    // Keep this guard for malformed fresh stage output; replay checkpoints are shape-gated.
    if (challengeOut && typeof challengeOut === 'object') {
      const findingsResult = beltPartitionList(challengeOut.findings);
      const unverifiedResult = beltPartitionList(challengeOut.unverified);
      challengeOut.findings = findingsResult.list;
      challengeOut.unverified = unverifiedResult.list;
      const newlyEliminated = [...findingsResult.eliminated, ...unverifiedResult.eliminated];

      const carriedEliminated = Array.isArray(challengeOut.eliminated) ? challengeOut.eliminated : [];
      challengeOut.eliminated = stripEliminatedList([...carriedEliminated, ...newlyEliminated]);

      // Counts that mirror rewritten arrays come from their actual post-scan lengths.
      // challenge_survived/contested/downgraded/unchallenged are pre-belt and need not sum to final_count.
      // replay_belt_eliminated is this call's total, including zero.
      if (challengeOut.stats && typeof challengeOut.stats === 'object') {
        const k1 = findingsResult.eliminated.length;
        const k2 = unverifiedResult.eliminated.length;
        if (typeof challengeOut.stats.final_count === 'number' && Array.isArray(challengeOut.findings)) {
          challengeOut.stats.final_count = challengeOut.findings.length;
        }
        if (typeof challengeOut.stats.skipped === 'number' && Array.isArray(challengeOut.unverified)) {
          challengeOut.stats.skipped = challengeOut.unverified.length;
        }
        challengeOut.stats.replay_belt_eliminated = k1 + k2;
      }

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
      prIdentity: (A.delivery || {}).prIdentity, // L3: writer emits the post_review-ready wrapper when present
      reviewBody,
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
