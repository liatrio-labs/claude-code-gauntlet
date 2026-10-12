// Summary and discovery stages, finding schemas, and scope flags.
import { DIMENSIONS, modelFor, FINDING_PROP_TYPES, FINDING_REQUIRED, conditionalSchemaActive } from './registry.js';
import { computeLightEligible, LIMIT_DEFAULTS } from './args.js';
import { effectiveBucketSize } from './capacity.js';

// Runtime globals are injected by the workflow host; under node:test they are absent,
// so ctx must be supplied. defaultCtx lets the shipped pipeline call stages without wiring.
export function defaultCtx() {
  return {
    agent: typeof agent === 'function' ? agent : undefined,
    parallel: typeof parallel === 'function' ? parallel : undefined,
    pipeline: typeof pipeline === 'function' ? pipeline : undefined,
    pipelineVersion: null,
  };
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
  // Prebuilt by runWith: the summarizer opens the SAME file the discovery
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
// number the per-bucket prompts were pinned to.
function summarizeMergePrompt(inp, partials) {
  const joined = partials.map((p) => p.summary || '').filter(Boolean).join('\n---\n');
  const countLine = typeof inp.changedLines === 'number' && inp.changedLines > 0
    ? ` The authoritative changed-line count is ${inp.changedLines}; cite that number verbatim if you mention change size — never re-estimate it.`
    : '';
  return `Combine these per-bucket change summaries into one concise semantic summary.${countLine} Partials:\n${joined}\nReturn { summary }.`;
}

// --- Phase 2: Discover ------------------------------------------------------

// intersectRequiredExtra(rows) -> string[]. The safe algebra for a multi-dimension
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
    // conditionalRequired: one { dimension, required } entry per row that
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

// deriveAgentFlags(riskTable, changedLines, scopeAnswer) -> { deep: false } | {}
// The pipeline's OWN, tested derivation of the scope-gating map agentActive
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

// allActiveDimensionsDegraded(dispatched, degraded) -> boolean.
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
// pass-through tolerated at random (one undeclared field survived 0/8, then
// 8/8, then 5/5 across three PRs of a single smoke — permitted, never a contract). Closing the
// item schema is safe because the declaration is complete and STAYS complete: every field an
// agent contract instructs is one entry in FINDING_PROP_TYPES or a dimension's `schemaExtra` in
// registry.js, and tests/test_registry_contracts.py fails the build when the declared set
// drifts from what the .md contracts instruct (declared − instructed == {origin}), so the
// closed schema cannot reject a field a contract asks for (`suggestion`/
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
// (agentSpecs' intersection) — empty for bug-detector, conventions-and-intent and
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
// `conditionalRequired` is the ONLY caller-gated argument here: findingSchema
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
// `policy` gates the conditional-required construct: spec.conditionalRequired is
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
// see allActiveDimensionsDegraded and its guard in runWith.
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
      // A dispatch failure on a spec that carried the conditional allOf/if/then
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

  // Bound an implausible discovery-agent line_end at intake, before anything
  // reaches mergeStage. A 2026-08-17 live run had an agent emit line_start=68,
  // line_end=940 (an ~872-line span crossing hunks) — post_review.py's delivery-side fix
  // now falls back to a single-line comment rather than 422ing the whole
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
      // A finding that never emitted line_end has no stated
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
      // suggested_fix_code states its replacement range as line_start..line_end.
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

// The discovery-intake line-span bound (see its call site in discover()).
// Same accessor pattern as its siblings above — read the ONE LIMIT_DEFAULTS constant
// rather than restating the literal here.
const effectiveMaxLineSpan = (L) => Math.max(1, L.maxLineSpan || LIMIT_DEFAULTS.maxLineSpan);
