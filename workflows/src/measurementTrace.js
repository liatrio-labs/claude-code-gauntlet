import { deepClone, RETURN_CHAR_BUDGET } from './wire.js';
import { SKEPTICAL_FINDING_PROPERTIES, EXCLUSION_CATEGORIES, acceptSkepticalReturn, assessmentGaps } from './registry.js';
import { validatorAssessment } from './applyValidations.js';
import { matchedCitation } from './citeWire.js';

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
export function traceDecision(f, reason_code, reason, related = [], decision = 'eliminated', detail = null) {
  // An unlisted code would fail the bench's closed decoder and lose the whole trace; record it as unknown instead.
  const code = TRACE_REASONS.includes(reason_code) ? reason_code : 'unknown';
  const a = f.reduction_detail?.find((r) => r.reason === code) || f.reduction_provenance || {};
  const row = EXCLUSION_CATEGORIES.find((r) => r.category === (detail?.category || a.category));
  const stage = detail?.stage || a.stage || row?.stage;
  return { trace_id: f.trace_id, decision, reason_code: code, reason: reason || null, related_trace_ids: related,
    detail: { reduction: a, finding_id: typeof f.id === 'string' ? f.id : null,
      issue_id: f.delivery_group?.issue_key || f.consolidation_key || f.trace_id,
      original_producer: f.discovery_agent || f.agent || 'unknown',
      ...(stage ? { stage } : {}),
      ...(row ? { category: row.category, predicate: row.predicate } : {}),
      source: f.cite_results?.find((r) => r.request?.stage === stage)?.source || f.scope_facts || null,
      request_ids: a.request_ids || [], ...detail } };
}
export function eliminationDecisions(output) {
  const codes = { verify: 'verify_eliminated', threshold: 'threshold', exclusion: 'exclusion', injection: 'injection',
    'suppressed:intentional': 'suppression', 'suppressed:generated': 'suppression',
    refuted: 'refuted' };
  return (output.eliminated || []).filter((f) => f.trace_id)
    .map((f) => traceDecision(f, f.eliminated_by === 'out_of_scope' ? 'out_of_scope' : codes[f.eliminated_by] || 'unknown', f.elimination_reason));
}

// Use the same acceptance and proof joins as disposal; counting quotes would inflate success.
function verdictObservations(fs) {
  return fs.filter((f) => f.trace_id).flatMap((f) => ['validator', 'challenger'].flatMap((stage) => {
    if (stage === 'challenger' && ['validator_terminal', 'admission_terminal', 'scope_terminal'].includes(f.challenge)) return [];
    const es = (f.skeptical_assessments || []).filter((a) => a.stage === stage);
    const a = es.length === 1 ? es[0].raw ?? es[0] : {};
    const g = assessmentGaps(stage === 'validator' ? validatorAssessment(f, a) : acceptSkepticalReturn(a, f.id, stage));
    const verdict = ['real', 'refuted', 'needs_evidence'].includes(a.verdict) ? a.verdict : null;
    const cites = (a) => Array.isArray(a?.citations) ? a.citations : [];
    const qs = [...cites(a), ...(Array.isArray(a.scope_assessments) ? a.scope_assessments : []).flatMap(cites)];
    return [{ trace_id: f.trace_id, stage, verdict,
      unresolved: !verdict || verdict === 'needs_evidence' || g.semanticErrors.length > 0,
      proved: ['real', 'refuted'].includes(verdict) && !g.proofErrors.length && !g.semanticErrors.length
        && qs.every((q) => matchedCitation(f, q, stage)),
      unavailable: (f.cite_results || []).some((r) => r.request?.stage === stage && r.status === 'unavailable') }];
  }));
}

export function stampDiscovery(trace, finding, spec, dispatch, occurrence) {
  if (!finding || typeof finding !== 'object' || Array.isArray(finding)) return finding;
  const out = finding;
  out.trace_id = `${trace.run_nonce}:${dispatch}:${occurrence}`;
  out.discovery_agent = spec.agentType.split(':').pop();
  out.discovery_dimension = spec.dimensions.includes(out.dimension) ? out.dimension : 'unknown';
  return out;
}

function traceVersion(t, f) {
  if (!f || typeof f.trace_id !== 'string') return null;
  const v = deepClone(f);
  const key = JSON.stringify(v);
  const repeated = traceHistory.get(t).get(key);
  if (repeated) return repeated;
  const version_id = `v${t.versions.length}`;
  t.versions.push({ version_id, trace_id: v.trace_id,
    discovery_agent: v.discovery_agent || 'unknown', discovery_dimension: v.discovery_dimension || 'unknown', finding: v });
  traceHistory.get(t).set(key, version_id);
  return version_id;
}
export function recordStage(t, stage, out, d = {}) {
  if (!t) return;
  if (!TRACE_STAGES.includes(stage)) throw new Error('unknown measurement stage');
  const existing = t.snapshots.findIndex((s) => s.stage === stage);
  if (existing >= 0 && d.replayed) { t.snapshots[existing].replayed = true; return; }
  if (existing >= 0) t.snapshots = t.snapshots.slice(0, existing);
  const fs = Array.isArray(out) ? out : out.findings || out.filtered || [];
  const all = [...fs, ...(out.unverified || []), ...(out.eliminated || [])];
  const ds = [...decisionsFor(out), ...(d.decisions || [])];
  if (stage === 'dispose') for (const d of ds) if (d.detail) {
    d.detail.issue_id = fs.find((f) => f.trace_id === d.trace_id)?.delivery_group?.issue_key || d.detail.issue_id;
  }
  const vs = stage === 'cite' ? verdictObservations([...fs, ...(out.unverified || [])]) : [];
  for (const v of vs) {
    if (!v.proved) {
      const code = v.unavailable ? 'cite_unavailable' : v.unresolved ? 'held_back' : 'cite_unmatched';
      ds.push(traceDecision(all.find((f) => f.trace_id === v.trace_id), code, code, [], 'kept', { stage: v.stage }));
    }
  }
  if (stage === 'dispose') for (const f of all) {
    for (const scope of f.scope_decisions || []) if (f.trace_id && scope.reason === 'scope_contradicted') {
      ds.push(traceDecision(f, scope.reason, scope.category, [], 'kept', { category: scope.category, stage: scope.stage,
        request_ids: scope.request_ids || [] }));
    }
  }
  for (const finding of [...(d.removed || out.eliminated || []), ...(out.unverified || [])]) traceVersion(t, finding);
  t.snapshots.push({ sequence: t.snapshots.length, stage,
    finding_version_ids: fs.map((f) => traceVersion(t, f)).filter((id) => id !== null),
    decisions: deepClone(ds), executed: d.executed !== false, replayed: !!d.replayed,
    coverage: d.coverage || (d.replayed ? 'partial' : 'complete'),
    cap: d.cap ?? null, cap_hit: !!d.cap_hit,
    verdicts: stage === 'cite' ? vs : null,
    workload: stage === 'cite' ? { requests: out.cite_ledger?.entries?.length || 0,
      batches: (out.cite_ledger?.batches || []).map(({ attempts, state }) => ({ attempts, state })),
      oversize: out.stats?.cite_oversize || 0, source_capture: out.verify?.sourceCapture || null } : stage === 'verify' ? out.inputProof || null
      : ['validate', 'challenge'].includes(stage) ? out.stats || null : null });
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
