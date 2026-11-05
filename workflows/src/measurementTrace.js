import { deepClone, RETURN_CHAR_BUDGET } from './wire.js';

export const TRACE_STAGES = ['discover', 'merge', 'verify', 'validate', 'filter', 'admission', 'challenge', 'cite', 'dispose', 'holdback', 'select', 'deliver'];
// Bench discovery rendering, delivery provenance/replay, admission comparisons and description evaluation
// determine the kept fields. The generated Python registry shares this list; bench tests guard reader coverage.
export const TRACE_FINDING_FIELDS = ['trace_id', 'discovery_agent', 'discovery_dimension', 'id', 'file',
  'line_start', 'line_end', 'line', 'end_line', 'claimed_line_start', 'claimed_line_end', 'anchor', 'location_status',
  'title', 'description', 'body', 'evidence', 'severity', 'suggestion', 'suggested_fix_code', 'claude_md_rule', 'spec_text',
  'rule_source', 'agent', 'dimension', 'confidence', 'sites', 'admission', 'disposition', 'disposition_reasons',
  'consolidation_key', 'consolidation_primary', 'delivery_group', 'origin_dimension', 'policy_subject', 'path_role'];
export const TRACE_REASONS = ['not_found', 'duplicate_id', 'invalid_shape', 'invalid_path', 'audit_overlap', 'verify_eliminated', 'verify_unproven', 'validation_skipped', 'threshold', 'exclusion', 'injection', 'suppression', 'challenge_removed', 'challenge_downgraded_removed', 'challenge_failed', 'challenge_cap', 'delivery_cap', 'delivery_tier', 'unanchored', 'body_budget', 'post_omission', 'body_routed', 'report_only', 'held_back', 'folded', 'demoted', 'refuted', 'out_of_scope', 'scope_contradicted', 'cite_unmatched', 'cite_unavailable', 'unknown'];
const traceHistory = new WeakMap();
const stageDecisions = new WeakMap();
export const decisionsFor = (output) => stageDecisions.get(output) || [];
function traceFinding(finding) {
  return deepClone(Object.fromEntries(TRACE_FINDING_FIELDS.filter((key) => Object.hasOwn(finding, key)).map((key) => [key, finding[key]])));
}
export function createMeasurementTrace(nonce, previous = null) {
  const trace = { schema_version: 2, run_nonce: nonce,
    versions: previous ? previous.versions.map((v) => ({ ...deepClone(v), finding: traceFinding(v.finding) })) : [],
    snapshots: [], complete: false, dropped_stages: [] };
  // Historical snapshots reference their original IDs, even when their full findings now share a projection.
  traceHistory.set(trace, new Map(trace.versions.map((v) => [JSON.stringify(v.finding), v.version_id])));
  return trace;
}
export function measurementDecisions(output, decisions) {
  if (decisions.length) stageDecisions.set(output, deepClone(decisions));
  return output;
}
export function admissionDetail(finding, extra = {}) {
  return { kind: finding.admission?.kind || 'unknown', policy_subject: finding.policy_subject || 'unresolved',
    original_producer: finding.agent, ...extra };
}
export function traceDecision(finding, reason_code, reason, related = [], decision = 'eliminated', detail = null) {
  // An unlisted code would fail the bench's closed decoder and lose the whole trace; record it as unknown instead.
  const code = TRACE_REASONS.includes(reason_code) ? reason_code : 'unknown';
  return { trace_id: finding.trace_id, decision, reason_code: code, reason: reason || null, related_trace_ids: related, detail };
}
export function eliminationDecisions(output) {
  const codes = { verify: 'verify_eliminated', threshold: 'threshold', exclusion: 'exclusion', injection: 'injection',
    'suppressed:intentional': 'suppression', 'suppressed:generated': 'suppression',
    'challenge:removed': 'challenge_removed', 'challenge:downgraded': 'challenge_downgraded_removed' };
  return (output.eliminated || []).filter((f) => f.trace_id)
    .map((f) => traceDecision(f, codes[f.eliminated_by] || 'unknown', f.elimination_reason));
}
export function stampDiscovery(trace, finding, spec, dispatch, occurrence) {
  if (!finding || typeof finding !== 'object' || Array.isArray(finding)) return finding;
  const out = finding;
  out.trace_id = `${trace.run_nonce}:${dispatch}:${occurrence}`;
  out.discovery_agent = spec.agentType.split(':').pop();
  out.discovery_dimension = spec.dimensions.includes(out.dimension) ? out.dimension : 'unknown';
  return out;
}

function traceVersion(trace, finding) {
  if (!finding || typeof finding.trace_id !== 'string') return null;
  const copy = traceFinding(finding);
  const fingerprint = JSON.stringify(copy);
  const repeated = traceHistory.get(trace).get(fingerprint);
  if (repeated) return repeated;
  let index = trace.versions.length;
  while (trace.versions.some((v) => v.version_id === `v${index}`)) index++;
  const version_id = `v${index}`;
  trace.versions.push({ version_id, trace_id: copy.trace_id,
    discovery_agent: copy.discovery_agent || 'unknown', discovery_dimension: copy.discovery_dimension || 'unknown', finding: copy });
  traceHistory.get(trace).set(fingerprint, version_id);
  return version_id;
}
export function recordStage(trace, stage, output, details = {}) {
  if (!trace) return;
  if (!TRACE_STAGES.includes(stage)) throw new Error('unknown measurement stage');
  const existing = trace.snapshots.findIndex((s) => s.stage === stage);
  if (existing >= 0 && details.replayed) { trace.snapshots[existing].replayed = true; return; }
  if (existing >= 0) trace.snapshots = trace.snapshots.slice(0, existing);
  const findings = Array.isArray(output) ? output : output.findings || output.filtered || [];
  const decisions = [...decisionsFor(output), ...(details.decisions || [])];
  for (const finding of [...(details.removed || output.eliminated || []), ...(output.unverified || [])]) traceVersion(trace, finding);
  trace.snapshots.push({ sequence: trace.snapshots.length, stage,
    finding_version_ids: findings.map((f) => traceVersion(trace, f)).filter((id) => id !== null),
    decisions: deepClone(decisions), executed: details.executed !== false, replayed: !!details.replayed,
    coverage: details.coverage || (details.replayed ? 'partial' : 'complete'),
    cap: details.cap ?? null, cap_hit: !!details.cap_hit });
}
export function appendMeasurement(envelope, trace) {
  if (!trace) return envelope;
  const candidate = deepClone(trace);
  const available = () => ({ ...envelope, measurement: { status: 'available', text: JSON.stringify(candidate) } });
  let result = available();
  // Keep the discovery prefix longest, and never turn a cut stage into an observed empty set.
  for (const snapshot of [...candidate.snapshots].reverse()) {
    if (JSON.stringify(result).length <= RETURN_CHAR_BUDGET) return result;
    if (candidate.dropped_stages.includes(snapshot.stage)) continue;
    snapshot.finding_version_ids = [];
    snapshot.decisions = [];
    snapshot.coverage = 'unavailable';
    candidate.dropped_stages.unshift(snapshot.stage);
    const referenced = new Set(candidate.snapshots.flatMap((s) => s.finding_version_ids));
    const identities = new Set(candidate.versions.filter((v) => referenced.has(v.version_id)).map((v) => v.trace_id));
    const needed = new Set(candidate.snapshots.flatMap((s) => s.decisions.flatMap((d) => [d.trace_id, ...d.related_trace_ids])));
    candidate.versions = candidate.versions.filter((v) => {
      if (referenced.has(v.version_id)) return true;
      if (!needed.has(v.trace_id) || identities.has(v.trace_id)) return false;
      identities.add(v.trace_id);
      return true;
    });
    result = available();
  }
  return appendBudgetedMeasurement(envelope, result.measurement, { status: 'unavailable', reason: 'return_budget' });
}
export function appendBudgetedMeasurement(envelope, available, unavailable) {
  const candidate = { ...envelope, measurement: available };
  if (JSON.stringify(candidate).length <= RETURN_CHAR_BUDGET) return candidate;
  const fallback = { ...envelope, measurement: unavailable };
  return JSON.stringify(fallback).length <= RETURN_CHAR_BUDGET ? fallback : envelope;
}

// Persistence agents must see exactly the same bytes as ordinary reviews.
export function stripTraceFields(value) {
  if (!value || typeof value !== 'object') return;
  for (const key of ['trace_id', 'discovery_agent', 'discovery_dimension']) delete value[key];
  for (const child of Object.values(value)) stripTraceFields(child);
}
