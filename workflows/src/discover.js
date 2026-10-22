// Summary and discovery stages, finding schemas, and scope flags.
import { AGENT_TYPE_TEST_ANALYZER, DIMENSIONS, DISPATCH_RESULTS, modelFor, FINDING_PROP_TYPES, FINDING_REQUIRED, conditionalSchemaActive } from './registry.js';
import { computeLightEligible } from './knobs.js';
import { LIMIT_DEFAULTS } from './registry.js';
import { effectiveBucketSize } from './capacity.js';
import { deepClone } from './wire.js';
import { checkRemovedTestAudit } from './removedTests.js';
import { repoRelativeFindingPath } from './paths.js';
import { stampDiscovery, recordStage, traceDecision, measurementDecisions } from './measurementTrace.js';

// StructuredOutput requires an object wrapper around summary prose.
const SUMMARIZE_SCHEMA = { type: 'object', properties: { summary: { type: 'string' } }, required: ['summary'] };

export async function summarize(ctx, input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const changedFiles = inp.changedFiles || [];
  const changedLines = inp.changedLines || 0;
  const limits = inp.limits || {};
  const policy = inp.policy || {};
  const bucketSize = effectiveBucketSize(limits);
  const model = modelFor('code-gauntlet:change-summarizer', policy);

  const bucketed = changedLines > 500 && changedFiles.length > bucketSize;
  // Single agent calls throw on schema retry exhaustion, so summary failures need a local catch to preserve downstream review work.
  try {
    if (bucketed) {
      const buckets = [];
      for (let i = 0; i < changedFiles.length; i += bucketSize) buckets.push(changedFiles.slice(i, i + bucketSize));
      const thunks = buckets.map((files, idx) => () => ctx.agent(summarizePrompt(inp, files), {
        label: `summarize-bucket-${idx}`,
        agentType: 'code-gauntlet:change-summarizer',
        model,
        schema: SUMMARIZE_SCHEMA,
      }));
      const partials = (await ctx.parallel(thunks)).filter(Boolean);
      if (partials.length === 0) return { summary: '', gaps: ['summarize failed'] };
      const mergeResult = await ctx.agent(summarizeMergePrompt(inp, partials), {
        label: 'summarize-merge',
        agentType: 'code-gauntlet:change-summarizer',
        model,
        schema: SUMMARIZE_SCHEMA,
      });
      if (!mergeResult) return { summary: '', gaps: ['summarize failed'] };
      return { summary: mergeResult.summary || '', gaps: [] };
    }
    const result = await ctx.agent(summarizePrompt(inp, changedFiles), {
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

function summaryCountLine(inp, suffix) {
  return typeof inp.changedLines === 'number' && inp.changedLines > 0
    ? ` The authoritative changed-line count is ${inp.changedLines}; cite that number verbatim if you mention change size — never re-estimate it${suffix}`
    : '';
}

function summarizePrompt(inp, files) {
  // The prebuilt context instruction gives summarizers the same read coverage as discovery agents.
  const ctxLine = inp.contextLine || '';
  // The supplied changedLines count is authoritative; deriving it from the diff can make summary and report totals disagree.
  const countLine = summaryCountLine(inp, ' from the diff.');
  return `${ctxLine}Summarize the semantic intent of these changed files for report change context: ${files.join(', ')}.${countLine} Return { summary }.`;
}

// The merge prompt needs the same count pin because it can otherwise reintroduce drift after bucket summaries.
function summarizeMergePrompt(inp, partials) {
  const joined = partials.map((p) => p.summary || '').filter(Boolean).join('\n---\n');
  const countLine = summaryCountLine(inp, '.');
  return `Combine these per-bucket change summaries into one concise semantic summary.${countLine} Partials:\n${joined}\nReturn { summary }.`;
}

// Only fields required by every sibling dimension can safely constrain a shared dispatch.
// The intersection remains safe if registry parity checks are bypassed.
export function intersectRequiredExtra(rows) {
  if (!rows || rows.length === 0) return [];
  let acc = new Set(rows[0].requiredExtra || []);
  for (const row of rows.slice(1)) {
    const next = new Set(row.requiredExtra || []);
    acc = new Set([...acc].filter((f) => next.has(f)));
  }
  return [...acc].sort();
}

// Derive agent order from the supplied dimensions so alternate registries cannot produce missing specs.
export function agentSpecs(dims = DIMENSIONS) {
  const byAgent = new Map();
  for (const d of dims) {
    if (!byAgent.has(d.agentType)) byAgent.set(d.agentType, { agentType: d.agentType, dimensions: [], schemaExtra: {}, conditionalFlags: [], promptExtra: null, rows: [] });
    const spec = byAgent.get(d.agentType);
    spec.dimensions.push(d.dimension);
    Object.assign(spec.schemaExtra, d.schemaExtra || {});
    spec.conditionalFlags.push(d.conditionalFlag);
    spec.rows.push(d);
    // Prompt extras apply to the whole agent dispatch, so sibling dimensions must agree on them.
    if (d.promptExtra) spec.promptExtra = d.promptExtra;
  }
  const order = [...new Set(dims.map((d) => d.agentType))];
  return order.map((a) => {
    const spec = byAgent.get(a);
    spec.requiredExtra = intersectRequiredExtra(spec.rows);
    // Sort conditional requirements so schema derivation does not depend on registry row order.
    spec.conditionalRequired = spec.rows
      .filter((r) => (r.requiredWhenDimension || []).length)
      .map((r) => ({ dimension: r.dimension, required: [...r.requiredWhenDimension].sort() }))
      .sort((a1, b1) => a1.dimension.localeCompare(b1.dimension));
    delete spec.rows;
    return spec;
  });
}

// Missing flags enable dimensions so incomplete flag maps cannot silently narrow review scope.
export function agentActive(spec, agentFlags) {
  const flags = agentFlags || {};
  return spec.conditionalFlags.some((flag) => flag === null || flag === undefined || flags[flag] !== false);
}

// Sharing computeLightEligible with validation prevents dispatch scope from disagreeing with the validated inputs.
export function deriveAgentFlags(riskTable, changedLines, scopeAnswer) {
  const lightEligible = computeLightEligible(riskTable, changedLines);
  return (lightEligible && scopeAnswer === 'light') ? { deep: false } : {};
}

// Degradation is relative to dispatched dimensions so light reviews also fail when all active coverage is lost.
// Unresolvable replay scope fails closed when degradation is reported; the caller preserves checkpoints carrying findings.
export function allActiveDimensionsDegraded(dispatched, degraded) {
  const dispatchedSet = new Set(dispatched || []);
  const degradedSet = new Set(degraded || []);
  const activeDims = agentSpecs()
    .filter((spec) => dispatchedSet.has(spec.agentType))
    .flatMap((spec) => spec.dimensions);
  if (activeDims.length === 0) return degradedSet.size > 0;
  return activeDims.every((d) => degradedSet.has(d));
}

// Empty property schemas permit lost finding fields; complete declarations keep StructuredOutput constrained.
// Undeclared properties must fail dispatch rather than survive unpredictably.
function findingItemSchema(schemaExtra, requiredExtra, conditionalRequired) {
  const props = {};
  // Dimension extras may narrow canonical fields, so they take precedence.
  for (const [k, t] of Object.entries({ ...FINDING_PROP_TYPES, ...(schemaExtra || {}) })) {
    // The host may normalize schema fragments in place; copies keep registry data immutable.
    props[k] = typeof t === 'string' ? { type: t } : deepClone(t);
  }
  // One missing required field rejects the whole dispatch, so only unconditional fields belong in this shared list.
  const required = [...FINDING_REQUIRED, ...(requiredExtra || [])];
  const itemSchema = { type: 'object', properties: props, required, additionalProperties: false };
  if (conditionalRequired && conditionalRequired.length) {
    // The platform accepts conditional requirements nested in finding items rather than top-level schema composition.
    // A dimension enum would reject tolerated aliases and degrade every sibling dimension in the shared dispatch.
    itemSchema.allOf = conditionalRequired.map(({ dimension, required: dimRequired }) => ({
      if: { properties: { dimension: { const: dimension } }, required: ['dimension'] },
      then: { required: [...dimRequired] },
    }));
  }
  return itemSchema;
}

// The platform requires full JSON Schema and rejects shorthand schemas.
// Conditional requirements depend on provider capability; unsupported providers retain contract enforcement through agent instructions.
export function findingSchema(spec, policy) {
  const conditionalRequired = conditionalSchemaActive(policy) ? (spec.conditionalRequired || []) : [];
  const resultFields = DISPATCH_RESULTS[spec.agentType] || {};
  return {
    type: 'object',
    properties: {
      findings: {
        type: 'array',
        items: findingItemSchema(spec.schemaExtra, spec.requiredExtra, conditionalRequired),
      },
      complete: { type: 'boolean' },
      total_seen: { type: 'number' },
      ...deepClone(resultFields),
    },
    required: ['findings', 'complete', 'total_seen', ...Object.keys(resultFields)],
  };
}

const effectiveMaxLineSpan = (L) => Math.max(1, L.maxLineSpan || LIMIT_DEFAULTS.maxLineSpan);

function dropSuggestedFixCode(f) {
  if (!('suggested_fix_code' in f)) return false;
  delete f.suggested_fix_code;
  return true;
}

// The roster distinguishes agents returning no findings from agents never dispatched.
// A terminal failure loses every dimension owned by that agent, while incomplete output still contributes findings.
export async function discover(ctx, input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const agentFlags = inp.agentFlags || {};
  const limits = inp.limits || {};
  const policy = inp.policy || {};
  const discoveryCap = limits.discoveryCap;

  const specs = agentSpecs().filter((spec) => agentActive(spec, agentFlags));
  const inventory = inp.removedTests || { status: 'unavailable', items: [], omitted: 0, reason: 'inventory not supplied' };
  const auditDispatched = specs.some((spec) => spec.agentType === AGENT_TYPE_TEST_ANALYZER);
  let removedTestAudit = inventory.status === 'unavailable'
    ? { status: 'unavailable', reason: inventory.reason }
    : !auditDispatched && inventory.items.length > 0
      ? { status: 'skipped_light_scope' }
      : auditDispatched ? { status: 'gap', reason: 'audit dispatch did not complete' }
        : inventory.omitted > 0 ? { status: 'overflow', omitted: inventory.omitted } : { status: 'ran-clean' };
  // The platform accepts zero-argument thunks calling agent with a prompt string and options.
  // Labels preserve agent identity for gap attribution.
  const thunks = specs.map((spec) => {
    const model = modelFor(spec.agentType, policy);
    return () => ctx.agent(discoverPrompt(inp, spec), {
      label: spec.agentType,
      agentType: spec.agentType,
      model,
      schema: findingSchema(spec, policy),
    });
  });

  const results = await ctx.parallel(thunks);

  const gaps = [];
  const findings = [];
  const intake = [];
  const decisions = [];
  const trace = inp.measurementTrace;
  const degradedDims = [];
  let droppedAuditOverlaps = 0;

  const schemaActive = conditionalSchemaActive(policy);

  // Failed parallel members become null in place, preserving positional attribution to the dispatched specs.
  results.forEach((res, i) => {
    const spec = specs[i];
    let auditLosses = [];
    if (spec.agentType === AGENT_TYPE_TEST_ANALYZER && inventory.status === 'available' && res) {
      const checked = checkRemovedTestAudit(inventory, res.removed_test_audit, inp.repoRoot);
      removedTestAudit = checked.audit;
      auditLosses = checked.findings;
      if (trace) auditLosses = auditLosses.map((f, n) => stampDiscovery(trace, f, spec, i, `synthetic-${n}`));
      if (trace) intake.push(...auditLosses);
      findings.push(...auditLosses);
    }
    if (res === null || res === undefined) {
      // Conditional schema failures need distinct diagnostics so operators can identify the construct that may have exhausted retries.
      const conditionalNote = schemaActive && (spec.conditionalRequired || []).length
        ? ' (dispatch carried the conditional per-dimension schema)'
        : '';
      gaps.push(`${spec.agentType}: agent returned null (dispatch failed) — dimensions ${spec.dimensions.join('/')} not covered${conditionalNote}`);
      degradedDims.push(...spec.dimensions);
      return;
    }
    const list = Array.isArray(res.findings) ? res.findings : null;
    if (list === null) {
      // A malformed result provides no usable coverage and must degrade like a null result.
      gaps.push(`${spec.agentType}: malformed result (no findings array)`);
      degradedDims.push(...spec.dimensions);
      return;
    }
    for (const [occurrence, raw] of list.entries()) {
      const f = trace ? stampDiscovery(trace, raw, spec, i, occurrence) : raw;
      if (trace) intake.push(f);
      // The audit already represents coverage lost at this anchor within this dispatch.
      const normalized = repoRelativeFindingPath(inp.repoRoot, f.file);
      // Same start line only: a wider range at that anchor may describe a different untested behaviour.
      if (auditLosses.some((loss) => normalized.file === loss.file && f.line_start === loss.line_start)) {
        droppedAuditOverlaps += 1;
        if (trace) decisions.push(traceDecision(f, 'audit_overlap', 'removed-test audit represents this anchor'));
        continue;
      }
      delete f.removed_test;
      // Filtering and merge grouping require canonical short agent names, so dispatch prefixes must be stripped.
      f.agent = spec.agentType.split(':').pop();
      findings.push(f);
    }
    const nearCap = discoveryCap != null && (res.total_seen >= discoveryCap || list.length >= discoveryCap);
    if (res.complete === false || nearCap) {
      gaps.push(`${spec.agentType}: possibly incomplete (complete=${res.complete === false ? 'false' : 'true'}, total_seen=${res.total_seen}) — dimensions ${spec.dimensions.join('/')}`);
    }
  });

  if (trace) recordStage(trace, 'discover', intake, {
    coverage: gaps.length ? 'partial' : 'complete', cap: discoveryCap, cap_hit: gaps.some((gap) => gap.includes('possibly incomplete')) });

  if (droppedAuditOverlaps > 0) removedTestAudit.duplicates_dropped = droppedAuditOverlaps;

  if (inventory.omitted > 0) removedTestAudit.omitted = inventory.omitted;
  if (removedTestAudit.status !== 'ran-clean') {
    degradedDims.push('test_coverage');
    gaps.push(`removed-test audit: ${removedTestAudit.status}${removedTestAudit.reason ? ` (${removedTestAudit.reason})` : ''}${removedTestAudit.omitted ? ` (${removedTestAudit.omitted} omitted)` : ''}`);
  }

  // Dropping an implausible end line preserves the valid anchor without fabricating a replacement range.
  // The configurable span limit permits legitimate large findings.
  const maxLineSpan = effectiveMaxLineSpan(limits);
  let droppedLineSpans = 0;
  let droppedNoLineEnd = 0;
  let droppedFixForImplausibleSpan = 0;
  for (const f of findings) {
    const lineEnd = f.line_end;
    if (lineEnd === undefined || lineEnd === null) {
      // Without an end line, suggested code has no stated replacement range and cannot be delivered safely.
      if (dropSuggestedFixCode(f)) droppedNoLineEnd += 1;
      continue;
    }
    const span = lineEnd - f.line_start;
    if (!Number.isInteger(lineEnd) || span < 0 || span > maxLineSpan) {
      delete f.line_end;
      // Removing the end line invalidates the suggested patch range, so the patch must also be removed.
      // Disclose this loss just as for a missing range.
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

  return measurementDecisions({
    findings,
    gaps,
    degraded: [...new Set(degradedDims)],
    dispatched: specs.map((spec) => spec.agentType),
    removedTestAudit,
  }, decisions);
}

// Concrete evidence requirements make absence claims auditable; registry-scoped prompt extras preserve each agent's investigation breadth.
function discoverPrompt(inp, spec) {
  const ctxLine = inp.contextLine || '';
  const dims = spec.dimensions.join(', ');
  const base = `${ctxLine}Investigate using your own methodology and tools (LSP first, Grep fallback) as defined for your role, across the full codebase context around the diff — not just the changed lines. Your dimension(s): ${dims}. Report every genuine finding for these dimension(s): there is no cap and no minimum. An empty findings list must reflect a genuine post-investigation absence of issues, never brevity or a quota. Every finding must cite concrete evidence: the evidence field must be non-empty and reference real lines you actually inspected (in the diff or in a file you opened) — a finding you cannot ground in inspected code is noise, do not emit it. For any absence or "missing" claim (e.g. a test-coverage negative asserting no test exists), name in evidence the specific file or path you checked; an unproven absence is not a finding. Return { findings, complete, total_seen }; each finding must match the canonical schema, with description as a single paragraph of prose, at most 500 characters — no code blocks or bullet lists; put code references in evidence and cross_file_refs instead.`;
  const resultFields = DISPATCH_RESULTS[spec.agentType] || {};
  const resultContract = Object.keys(resultFields).length
    ? ` Also return the required dispatch-result fields: ${Object.keys(resultFields).join(', ')}.` : '';
  const audit = spec.agentType === AGENT_TYPE_TEST_ANALYZER
    ? ` Audit every removed test item below from the saved diff. Return exactly one removed_test_audit row per id (or [] when unavailable/empty). Loss locations and prose describe surviving behavior; keep deleted paths only in the item reference. Inventory: ${JSON.stringify(inp.removedTests || { status: 'unavailable', items: [] })}` : '';
  return `${base}${spec.promptExtra ? ` ${spec.promptExtra}` : ''}${resultContract}${audit}`;
}
