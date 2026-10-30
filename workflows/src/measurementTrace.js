import { deepClone, RETURN_CHAR_BUDGET } from './wire.js';
import { SKEPTICAL_FINDING_PROPERTIES, EXCLUSION_CATEGORIES } from './registry.js';

export const TRACE_STAGES = ['discover', 'merge', 'verify', 'validate', 'filter', 'admission', 'challenge', 'cite', 'dispose', 'holdback', 'select', 'deliver'];
export const TRACE_REASONS = ['not_found', 'duplicate_id', 'invalid_shape', 'invalid_path', 'audit_overlap', 'verify_eliminated', 'verify_unproven', 'validation_skipped', 'threshold', 'exclusion', 'injection', 'suppression', 'challenge_removed', 'challenge_downgraded_removed', 'challenge_failed', 'challenge_cap', 'delivery_cap', 'delivery_tier', 'unanchored', 'body_budget', 'post_omission', 'body_routed', 'report_only', 'held_back', 'folded', 'demoted', 'refuted', 'out_of_scope', 'scope_contradicted', 'cite_unmatched', 'cite_unavailable', 'unknown'];
const traceHistory = new WeakMap();
const stageDecisions = new WeakMap();
export const decisionsFor = (output) => stageDecisions.get(output) || [];
const traceIssueId = (finding) => finding.delivery_group?.issue_key || finding.consolidation_key || finding.trace_id;
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
  const reduction = finding.reduction_detail?.find((r) => r.reason === code && (!detail?.stage || r.stage === detail.stage)) || finding.reduction_provenance || {};
  const row = EXCLUSION_CATEGORIES.find((r) => r.category === (detail?.category || reduction.category));
  const stage = detail?.stage || reduction.stage || row?.stage;
  return { trace_id: finding.trace_id, decision, reason_code: code, reason: reason || null, related_trace_ids: related,
    detail: { reduction, finding_id: typeof finding.id === 'string' ? finding.id : null,
      issue_id: traceIssueId(finding),
      original_producer: finding.discovery_agent || finding.agent || 'unknown',
      ...(stage ? { stage } : {}),
      ...(row ? { category: row.category, predicate: row.predicate } : {}),
      source: finding.cite_results?.find((r) => r.request?.stage === stage)?.source || finding.scope_facts || null,
      request_ids: reduction.request_ids || [], ...detail } };
}
export function eliminationDecisions(output) {
  const codes = { verify: 'verify_eliminated', threshold: 'threshold', exclusion: 'exclusion', injection: 'injection',
    'suppressed:intentional': 'suppression', 'suppressed:generated': 'suppression',
    refuted: 'refuted' };
  return (output.eliminated || []).filter((f) => f.trace_id)
    .map((f) => traceDecision(f, f.eliminated_by === 'out_of_scope' ? 'out_of_scope' : codes[f.eliminated_by] || 'unknown', f.elimination_reason));
}

// Disposal owns proof acceptance; measurement consumes its per-stage stamp.
function verdictObservations(findings) {
  return findings.filter((f) => f.trace_id).flatMap((f) => (f.reduction_detail || [])
    .filter((row) => row.proof).map((row) => ({ trace_id: f.trace_id, stage: row.stage, ...row.proof })));
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
  const version = deepClone(finding);
  const fingerprint = JSON.stringify(version);
  const repeated = traceHistory.get(trace).get(fingerprint);
  if (repeated) return repeated;
  const version_id = `v${trace.versions.length}`;
  trace.versions.push({ version_id, trace_id: version.trace_id,
    issue_id: traceIssueId(version),
    discovery_agent: version.discovery_agent || 'unknown', discovery_dimension: version.discovery_dimension || 'unknown', finding: version });
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
  const all = [...findings, ...(output.unverified || []), ...(output.eliminated || [])];
  const decisions = [...decisionsFor(output), ...(details.decisions || [])];
  if (stage === 'dispose') for (const decision of decisions) if (decision.detail) {
    decision.detail.issue_id = findings.find((finding) => finding.trace_id === decision.trace_id)?.delivery_group?.issue_key || decision.detail.issue_id;
  }
  const verdicts = stage === 'cite' ? verdictObservations(details.verdictFindings || []) : [];
  for (const version of verdicts) {
    if (!version.proved) {
      const code = version.unavailable ? 'cite_unavailable' : version.unresolved ? 'held_back' : 'cite_unmatched';
      decisions.push(traceDecision(all.find((finding) => finding.trace_id === version.trace_id), code, code, [], 'kept', { stage: version.stage }));
    }
  }
  for (const finding of [...(details.removed || output.eliminated || []), ...(output.unverified || [])]) traceVersion(trace, finding);
  trace.snapshots.push({ sequence: trace.snapshots.length, stage,
    finding_version_ids: findings.map((finding) => traceVersion(trace, finding)).filter((id) => id !== null),
    decisions: deepClone(decisions), executed: details.executed !== false, replayed: !!details.replayed,
    coverage: details.coverage || (details.replayed ? 'partial' : 'complete'),
    cap: details.cap ?? null, cap_hit: !!details.cap_hit,
    verdicts: stage === 'cite' ? verdicts : null,
    workload: stage === 'cite' ? { requests: output.cite_ledger?.entries?.length || 0,
      batches: (output.cite_ledger?.batches || []).map(({ attempts, state, attempt_timestamps }) => ({ attempts, state,
        ...(attempt_timestamps ? { attempt_timestamps } : {}) })),
      oversize: output.stats?.cite_oversize || 0, source_capture: output.verify?.sourceCapture || null } : stage === 'verify' ? output.inputProof || null
      : ['validate', 'challenge'].includes(stage) ? output.stats || null : null });
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
