import { deepClone, RETURN_CHAR_BUDGET } from './wire.js';
import { SKEPTICAL_FINDING_PROPERTIES } from './registry.js';

export const TRACE_STAGES = ['discover', 'merge', 'verify', 'validate', 'filter', 'admission', 'challenge', 'cite', 'dispose', 'holdback', 'select', 'deliver'];
export const TRACE_REASONS = ['not_found', 'duplicate_id', 'invalid_shape', 'invalid_path', 'audit_overlap', 'verify_eliminated', 'verify_unproven', 'validation_skipped', 'threshold', 'exclusion', 'injection', 'suppression', 'challenge_removed', 'challenge_downgraded_removed', 'challenge_failed', 'challenge_cap', 'delivery_cap', 'delivery_tier', 'unanchored', 'body_budget', 'post_omission', 'body_routed', 'report_only', 'held_back', 'folded', 'demoted', 'refuted', 'out_of_scope', 'scope_contradicted', 'cite_unmatched', 'cite_unavailable', 'unknown'];
const traceHistory = new WeakMap();
const stageDecisions = new WeakMap();
export const decisionsFor = (output) => stageDecisions.get(output) || [];
export function createMeasurementTrace(nonce) {
  const trace = { schema_version: 1, run_nonce: nonce,
    versions: [], snapshots: [], complete: false };
  traceHistory.set(trace, new Map());
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
    refuted: 'refuted', out_of_scope: 'out_of_scope' };
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
  const copy = deepClone(finding);
  const fingerprint = JSON.stringify(copy);
  const repeated = traceHistory.get(trace).get(fingerprint);
  if (repeated) return repeated;
  const version_id = `v${trace.versions.length}`;
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
  return appendBudgetedMeasurement(envelope,
    { status: 'available', text: JSON.stringify(trace) },
    { status: 'unavailable', reason: 'return_budget' });
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
  // Skeptical records retain the producer's exact audit input across replay.
  for (const [key, child] of Object.entries(value)) if (!Object.hasOwn(SKEPTICAL_FINDING_PROPERTIES, key)) stripTraceFields(child);
}
