// Summary and discovery stages, finding schemas, and scope flags.
import { AGENT_TYPE_TEST_ANALYZER, DIMENSIONS, DISCOVERY_REQUIREMENTS, DISCOVERY_REVIEWERS, discoveryReviewer, DISPATCH_RESULTS, modelFor, DISCOVERY_FINDING_PROP_TYPES, FINDING_REQUIRED, conditionalSchemaActive } from './registry.js';
import { computeLightEligible } from './knobs.js';
import { LIMIT_DEFAULTS } from './registry.js';
import { effectiveBucketSize } from './capacity.js';
import { deepClone, untrustedJson } from './wire.js';
import { checkRemovedTestAudit } from './removedTests.js';
import { stampDiscovery, recordStage, measurementDecisions, traceDecision } from './measurementTrace.js';
import { coverageLedger, findingInRegions, gapHunterPrompt, selectGapAssignment, boundedCoverageLedger } from './coverageLedger.js';
import { validateFindings } from './mergeFindings.js';

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

function summaryCountLine(inp) {
  return typeof inp.changedLines === 'number' && inp.changedLines > 0
    ? ` If mentioning change size, use the authoritative count: ${inp.changedLines} changed lines.`
    : '';
}

function summarizePrompt(inp, files) {
  // The prebuilt context instruction gives summarizers the same read coverage as discovery agents.
  const ctxLine = inp.contextLine || '';
  // The supplied changedLines count is authoritative; deriving it from the diff can make summary and report totals disagree.
  const countLine = summaryCountLine(inp);
  return `${ctxLine}${countLine}\n${untrustedJson({ changed_files: files }, 'changed-files')}`;
}

// The merge prompt needs the same count pin because it can otherwise reintroduce drift after bucket summaries.
function summarizeMergePrompt(inp, partials) {
  const joined = partials.map((p) => p.summary || '').filter(Boolean).join('\n---\n');
  const countLine = summaryCountLine(inp);
  return `Merge the supplied partial summaries.${countLine}\n${untrustedJson({ summaries: joined }, 'partial-summaries')}`;
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
export function agentSpecs(dims = DIMENSIONS, reviewers = dims === DIMENSIONS ? DISCOVERY_REVIEWERS : []) {
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
  const primary = order.map((a) => {
    const spec = byAgent.get(a);
    spec.requiredExtra = intersectRequiredExtra(spec.rows);
    // Sort conditional requirements so schema derivation does not depend on registry row order.
    spec.conditionalRequired = spec.rows
      .filter((r) => (r.requiredWhenDimension || []).length)
      .map((r) => ({ dimension: r.dimension, required: [...r.requiredWhenDimension].sort() }))
      .sort((a1, b1) => a1.dimension.localeCompare(b1.dimension));
    spec.admissionRequired = spec.rows.flatMap((row) => row.admission ? Object.entries(row.admission.kinds)
      .filter(([, kind]) => kind.required.length || kind.anyOf?.length)
      .map(([kind, rule]) => ({ dimension: row.dimension, field: row.admission.field, kind, ...rule })) : []);
    delete spec.rows;
    spec.dispatch = 'parallel';
    return spec;
  });
  return [...primary, ...reviewers.map((raw) => {
    const row = discoveryReviewer(raw);
    const lane = agentSpecs(dims.filter((d) => d.dimension === row.dimension), [])[0];
    if (!lane) throw new Error(`Unknown discovery lane: ${row.dimension}`);
    return { ...deepClone(lane), ...row, dimensions: [row.dimension], conditionalFlags: [row.conditionalFlag] };
  })];
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
  for (const [k, t] of Object.entries({ ...DISCOVERY_FINDING_PROP_TYPES, ...(schemaExtra || {}) })) {
    // The host may normalize schema fragments in place; copies keep registry data immutable.
    props[k] = typeof t === 'string' ? { type: t } : deepClone(t);
  }
  // Only discovery dispatch requires a quote; pipeline-authored and replayed findings can remain unconfirmed.
  props.anchor.minLength = 1;
  const required = [...FINDING_REQUIRED, 'anchor', 'evidence', 'change_origin', ...(requiredExtra || [])];
  const itemSchema = { type: 'object', properties: props, required, additionalProperties: false, allOf: DISCOVERY_REQUIREMENTS.map((rule) => ({
    if: { properties: Object.fromEntries(Object.entries(rule.when).map(([key, value]) => [key, { const: value }])), required: Object.keys(rule.when) },
    then: { required: [...rule.required] },
  })) };
  if (conditionalRequired && conditionalRequired.length) {
    // The platform accepts conditional requirements nested in finding items rather than top-level schema composition.
    // A dimension enum would reject tolerated aliases and degrade every sibling dimension in the shared dispatch.
    itemSchema.allOf.push(...conditionalRequired.map(({ dimension, required: dimRequired }) => ({
      if: { properties: { dimension: { const: dimension } }, required: ['dimension'] },
      then: { required: [...dimRequired] },
    })));
  }
  return itemSchema;
}

// The platform requires full JSON Schema and rejects shorthand schemas.
// Provider capability gates dimension rules; intake requirements always stay nested in finding items.
export function findingSchema(spec, policy) {
  const conditionalRequired = conditionalSchemaActive(policy) ? (spec.conditionalRequired || []) : [];
  const resultFields = DISPATCH_RESULTS[spec.agentType] || {};
  const item = findingItemSchema(spec.schemaExtra, spec.requiredExtra, conditionalRequired);
  if (conditionalSchemaActive(policy) && spec.admissionRequired?.length) {
    item.allOf = [...(item.allOf || []), ...spec.admissionRequired.map((rule) => ({
      if: { properties: { dimension: { const: rule.dimension }, [rule.field]: { const: rule.kind } }, required: ['dimension', rule.field] },
      then: { required: [...rule.required], ...(rule.anyOf ? { anyOf: rule.anyOf.map((field) => ({ required: [field] })) } : {}) },
    }))];
  }
  // Dispatch owns identity; multi-lane agents still choose which lane a claim belongs to.
  delete item.properties.id;
  item.required = item.required.filter((key) => key !== 'id');
  if (spec.dimensions.length === 1) {
    delete item.properties.dimension;
    item.required = item.required.filter((key) => key !== 'dimension');
    for (const rule of item.allOf || []) {
      delete rule.if.properties.dimension;
      rule.if.required = rule.if.required.filter((key) => key !== 'dimension');
      if (rule.if.required.length === 0) item.required.push(...rule.then.required);
    }
    if (item.allOf) item.allOf = item.allOf.filter((rule) => rule.if.required.length > 0);
    if (item.allOf?.length === 0) delete item.allOf;
  } else {
    item.properties.dimension.description = `Review lane: ${spec.dimensions.join(', ')}.`;
  }
  return {
    type: 'object',
    properties: {
      findings: {
        type: 'array',
        items: item,
      },
      complete: { type: 'boolean', description: 'Whether investigation is finished.' },
      total_seen: { type: 'number', description: 'Number of candidates examined, including unreported ones.' },
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

function discoveryCandidate(raw, spec, occurrence) {
  const candidate = deepClone(raw);
  if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) {
    const name = spec.agentType.split(':').pop();
    if (candidate.id === undefined) candidate.id = `${name}-${occurrence + 1}`;
    if (spec.dimensions.length === 1 && candidate.dimension === undefined) candidate.dimension = spec.dimensions[0];
    for (const key of ['removed_test', 'location_status', 'claimed_line_start', 'claimed_line_end', 'line', 'end_line', 'reference_diagnostics']) delete candidate[key];
    candidate.agent = name;
  }
  return candidate;
}

function missingDiscoveryFields(finding) {
  return DISCOVERY_REQUIREMENTS.flatMap((rule) => Object.entries(rule.when).every(([key, value]) => finding[key] === value)
    ? rule.required.filter((key) => typeof finding[key] !== 'string' || !finding[key].trim()) : []);
}

// The roster distinguishes agents returning no findings from agents never dispatched.
// A terminal failure loses every dimension owned by that agent, while incomplete output still contributes findings.
export async function discover(ctx, input) {
  const inp = typeof input === 'string' ? JSON.parse(input) : (input || {});
  const agentFlags = inp.agentFlags || {};
  const limits = inp.limits || {};
  const policy = inp.policy || {};
  const discoveryCap = limits.discoveryCap;

  const activeSpecs = agentSpecs().filter((spec) => agentActive(spec, agentFlags));
  const specs = activeSpecs.filter((spec) => spec.dispatch === 'parallel');
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

  const ledger = coverageLedger(inp.discoveryPlan, specs, results, inp.repoRoot, inp.contextLines);
  const hunters = activeSpecs.filter((spec) => spec.dispatch === 'uncovered');
  const primaryUsable = results.some((res, i) => Array.isArray(res?.findings) && (res.findings.length === 0
    || res.findings.some((raw, n) => {
      const candidate = discoveryCandidate(raw, specs[i], n);
      return validateFindings([candidate]).valid.length > 0 && missingDiscoveryFields(candidate).length === 0;
    })));
  const assignment = selectGapAssignment(inp.contextPath, inp.discoveryPlan, ledger, inp.riskTable, hunters.length > 0 && primaryUsable,
    primaryUsable ? 'scope' : 'primary_failure');
  const assigned = assignment.regions;
  ledger.omitted = assignment.omitted;
  ledger.assigned_bytes = assignment.bytes;
  ledger.omitted_fact_packets = assignment.omitted_fact_packets;
  if (assigned.length && inp.contextPath) {
    const hunterResults = await ctx.parallel(hunters.map((spec) => () => ctx.agent(
      gapHunterPrompt(inp.contextPath, inp.discoveryPlan, assigned, assignment.facts), {
        label: spec.agentType, agentType: spec.agentType, model: modelFor(spec.agentType, policy), schema: findingSchema(spec, policy),
      })));
    hunters.forEach((spec, i) => {
      specs.push(spec);
      results.push(hunterResults[i]);
      ledger.hunters.push({ agent: spec.agentType, assigned: assigned.map((r) => r.id),
        status: Array.isArray(hunterResults[i]?.findings) ? (hunterResults[i].complete === false ? 'incomplete' : 'complete') : 'failed' });
    });
  }

  const finalResults = results.map((result, i) => specs[i].dispatch === 'uncovered' && Array.isArray(result?.findings)
    ? { findings: result.findings.filter((f) => findingInRegions(f, assigned, inp.repoRoot)) } : result);
  ledger.remaining_uncovered = coverageLedger(inp.discoveryPlan, specs, finalResults, inp.repoRoot, inp.contextLines).uncovered;
  const ledgerGaps = [];
  if (inp.discoveryPlan !== undefined && ledger.status !== 'available') ledgerGaps.push('discovery coverage unavailable: invalid or unavailable region plan');
  if (assigned.length && !inp.contextPath) ledgerGaps.push('gap hunter: uncovered regions could not be dispatched without contextPath');
  const gaps = [];
  const hunterGaps = [];
  const findings = [];
  const intake = [];
  const rejected = [];
  const decisions = [];
  const trace = inp.measurementTrace;
  const degradedDims = [];

  const schemaActive = conditionalSchemaActive(policy);

  // Failed parallel members become null in place, preserving positional attribution to the dispatched specs.
  results.forEach((res, i) => {
    const spec = specs[i];
    const channelGaps = spec.dispatch === 'uncovered' ? hunterGaps : gaps;
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
      channelGaps.push(spec.dispatch === 'uncovered' ? `${spec.agentType}: assigned gap investigation failed`
        : `${spec.agentType}: agent returned null (dispatch failed) — dimensions ${spec.dimensions.join('/')} not covered${conditionalNote}`);
      if (spec.dispatch === 'parallel') degradedDims.push(...spec.dimensions);
      return;
    }
    const list = Array.isArray(res.findings) ? res.findings : null;
    if (list === null) {
      // A malformed result provides no usable coverage and must degrade like a null result.
      channelGaps.push(`${spec.agentType}: malformed result (no findings array)`);
      if (spec.dispatch === 'parallel') degradedDims.push(...spec.dimensions);
      return;
    }
    for (const [occurrence, raw] of list.entries()) {
      const candidate = discoveryCandidate(raw, spec, occurrence);
      const f = trace ? stampDiscovery(trace, candidate, spec, i, occurrence) : candidate;
      if (trace) intake.push(f);
      const checked = validateFindings([f], decisions);
      const missing = checked.valid.length ? missingDiscoveryFields(f) : [];
      if (missing.length) {
        checked.valid.length = 0;
        checked.rejected.push(f);
        checked.warnings.push(`Missing discovery field(s): ${missing.join(', ')} — finding rejected`);
        if (trace) decisions.push(traceDecision(f, 'invalid_shape', checked.warnings.at(-1)));
      }
      if (checked.rejected.length) channelGaps.push(...checked.warnings.map((warning) => `${spec.agentType}: ${warning}`));
      rejected.push(...checked.rejected);
      if (!checked.valid.length) continue;
      if (spec.dispatch === 'uncovered' && !findingInRegions(f, assigned, inp.repoRoot)) {
        rejected.push(f);
        if (trace) decisions.push(traceDecision(f, 'discover_scope', 'gap hunter finding is outside assigned regions'));
        channelGaps.push(`${spec.agentType}: finding outside assigned regions rejected`);
        continue;
      }
      findings.push(...checked.valid);
    }
    const nearCap = discoveryCap != null && (res.total_seen >= discoveryCap || list.length >= discoveryCap);
    if (res.complete === false || nearCap) {
      channelGaps.push(spec.dispatch === 'uncovered' ? `${spec.agentType}: assigned gap investigation incomplete`
        : `${spec.agentType}: possibly incomplete (complete=${res.complete === false ? 'false' : 'true'}, total_seen=${res.total_seen}) — dimensions ${spec.dimensions.join('/')}`);
    }
  });

  if (!primaryUsable) degradedDims.push(...specs.filter((s) => s.dispatch === 'parallel').flatMap((s) => s.dimensions));
  if (trace) trace.coverage_ledger = boundedCoverageLedger(ledger);
  if (trace) recordStage(trace, 'discover', intake, {
    decisions, coverage: gaps.length ? 'partial' : 'complete', cap: discoveryCap, cap_hit: gaps.some((gap) => gap.includes('possibly incomplete')) });
  gaps.push(...ledgerGaps, ...hunterGaps);

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
    ...(rejected.length ? { rejected } : {}),
    gaps,
    degraded: [...new Set(degradedDims)],
    dispatched: specs.map((spec) => spec.agentType),
    removedTestAudit,
    ...(inp.discoveryPlan ? { coverageLedger: boundedCoverageLedger(ledger) } : {}),
  }, decisions);
}

// Dispatch carries per-review inputs; the named agent owns investigation guidance.
function discoverPrompt(inp, spec) {
  const ctxLine = inp.contextLine || '';
  const base = `${ctxLine}Repository: ${inp.repoRoot || '.'}.`;
  const audit = spec.agentType === AGENT_TYPE_TEST_ANALYZER
    ? `\n${untrustedJson(inp.removedTests || { status: 'unavailable', items: [] }, 'removed-test-inventory')}` : '';
  return `${base}${spec.promptExtra ? ` ${spec.promptExtra}` : ''}${audit}`;
}
