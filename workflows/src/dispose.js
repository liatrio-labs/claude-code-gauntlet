import { deepClone } from './wire.js';
import { incompleteVetting, UNVERIFIED_REASONS } from './vetting.js';
import { locationAllows, deliveryAllows } from './location.js';
import { consolidateCrossAgent } from './filterFindings.js';
import { eliminationDecisions, traceDecision, admissionDetail } from './measurementTrace.js';
import { hasAdmission } from './admission.js';
import { acceptSkepticalReturn, assessmentGaps, CITATION_REQUEST_SCHEMA, matchesDispatchSchema } from './registry.js';
import { citeChecksum } from './citeWire.js';
import { validatorAssessment } from './applyValidations.js';
import { scopeDecisions } from './scope.js';

function skepticalOutcome(finding) {
  const reasons = [];
  let proven = null;
  for (const stage of ['validator', 'challenger']) {
    const entries = (finding.skeptical_assessments || []).filter((a) => a.stage === stage);
    if (entries.length !== 1) {
      if (!(stage === 'challenger' && (finding.challenge === 'validator_terminal'
        || (finding.challenge === 'admission_terminal' && hasAdmission(finding) && finding.admission.status === 'report_only')))) {
        reasons.push({ reason: 'held_back', stage, missing_field: 'verdict' });
      }
      continue;
    }
    const raw = entries[0].raw ?? entries[0];
    const assessment = stage === 'validator' ? validatorAssessment(finding, raw) : acceptSkepticalReturn(raw, finding.id, stage);
    if (raw.verdict === 'needs_evidence') reasons.push({ reason: 'held_back', stage, missing_evidence: raw.missing_evidence });
    const requests = Array.isArray(raw.citations) ? raw.citations : [];
    const proof = requests.length > 0 && requests.some((q) => q.role === 'claim') && requests.every((q) => {
      const rows = (finding.cite_results || []).filter((r) => citeChecksum(r.request) === citeChecksum(q));
      return matchesDispatchSchema(q, CITATION_REQUEST_SCHEMA) && q.finding_id === finding.id && q.stage === stage
        && rows.length === 1 && rows[0].status === 'matched' && rows[0].source?.snapshot_id === q.snapshot_id;
    });
    if (['real', 'refuted'].includes(raw.verdict) && !proof) {
      const unavailable = requests.some((q) => finding.cite_results?.some((r) =>
        citeChecksum(r.request) === citeChecksum(q) && r.status === 'unavailable'));
      reasons.push({ reason: unavailable ? 'cite_unavailable' : 'cite_unmatched', stage, verdict: raw.verdict });
    }
    if (raw.verdict === 'refuted' && proof && assessment.status === 'complete') proven = { reason: 'refuted', stage,
      request_ids: requests.map((q) => q.request_id) };
    // A malformed real proof is diagnostic; missing semantic assessments still close delivery.
    const { semanticErrors } = assessmentGaps(assessment);
    if (semanticErrors.length) reasons.push({ reason: 'held_back', stage, missing_fields: semanticErrors });
  }
  return { proven, reasons, held: reasons.some((r) => r.reason === 'held_back' || r.verdict === 'refuted') };
}

export function disposeFindings(challenged) {
  const input = deepClone(challenged);
  const findings = [];
  const unverified = [];
  const eliminated = [];
  const legacy = (input.eliminated || []).filter((f) => String(f.eliminated_by).startsWith('challenge:'));
  eliminated.push(...(input.eliminated || []).filter((f) => !legacy.includes(f)));
  const decisions = [];
  for (const finding of [...(input.findings || []), ...(input.unverified || []), ...legacy]) {
    delete finding.consolidation_key;
    delete finding.consolidation_primary;
    delete finding.delivery_group;
    const scope = scopeDecisions(finding).filter((row) => row.applies === true);
    const skeptical = skepticalOutcome(finding);
    if (skeptical.proven) {
      finding.eliminated_by = 'refuted';
      finding.elimination_reason = `${skeptical.proven.stage} refutation with matched proof`;
      finding.reduction_provenance = { finding_id: finding.id, disposition: 'eliminate', ...skeptical.proven };
      eliminated.push(finding);
      continue;
    }
    const terminalScope = scope.find((row) => row.outcome === 'eliminate') || scope.find((row) => row.outcome === 'report_only');
    if (terminalScope) {
      finding.disposition = terminalScope.outcome;
      finding.disposition_reasons = [terminalScope.outcome === 'eliminate' ? 'out_of_scope' : 'report_only'];
      finding.reduction_provenance = { finding_id: finding.id, disposition: terminalScope.outcome,
        reason: finding.disposition_reasons[0], category: terminalScope.category };
      if (terminalScope.outcome === 'eliminate') {
        finding.eliminated_by = 'out_of_scope';
        finding.elimination_reason = terminalScope.category;
        eliminated.push(finding);
      } else {
        findings.push(finding);
        if (finding.trace_id) decisions.push(traceDecision(finding, 'report_only', terminalScope.category, [], 'report_only'));
      }
      continue;
    }
    const reasons = incompleteVetting(finding).map((stage) => stage === 'verify' ? 'verify_unproven' : 'validation_skipped');
    if (Object.hasOwn(UNVERIFIED_REASONS.challenge, finding.challenge) && finding.challenge !== 'skipped') {
      reasons.push(finding.challenge === 'capped' ? 'challenge_cap' : finding.challenge === 'incomplete' ? 'held_back' : 'challenge_failed');
    }
    let disposition = 'body';
    const admitted = hasAdmission(finding);
    const valueTerminal = admitted && finding.admission.status === 'report_only' && finding.challenge === 'admission_terminal';
    reasons.push(...skeptical.reasons.map((r) => r.reason));
    if ((input.unverified || []).includes(finding) && !reasons.length) reasons.push('held_back');
    const hold = skeptical.held;
    if (hold || incompleteVetting(finding).length || (!valueTerminal && (input.unverified || []).includes(finding))) disposition = 'held_back';
    else if (!admitted || finding.admission.status !== 'admitted') {
      disposition = admitted ? finding.admission.status : 'held_back';
      reasons.push(disposition);
    } else if (finding.challenge !== 'survived') {
      disposition = 'held_back';
      if (!Object.hasOwn(UNVERIFIED_REASONS.challenge, finding.challenge)) reasons.push('challenge_failed');
    } else if (!scope.some((row) => row.outcome === 'body') && finding.admission.inline_eligible
      && finding.diff_validation?.in_diff === true && locationAllows(finding, 'inline')) {
      disposition = 'deliver';
    } else reasons.push('body_routed');
    finding.disposition = disposition;
    finding.reduction_detail = skeptical.reasons;
    finding.disposition_reasons = [...new Set([...reasons, ...(admitted ? finding.admission.reasons.map((reason) => reason.status) : [])])];
    (disposition === 'held_back' ? unverified : findings).push(finding);
    if (finding.trace_id) for (const reason of finding.disposition_reasons) {
      decisions.push(traceDecision(finding, reason, reason, [], ['held_back', 'report_only'].includes(disposition) ? disposition : 'body_routed',
        admissionDetail(finding, { predicates: finding.admission?.reasons || [], surface: disposition })));
    }
  }
  const eliminations = eliminationDecisions({ eliminated: eliminated.map((f) => ({ ...f, trace_id: f.trace_id || 'unused' })) });
  for (const [index, finding] of eliminated.entries()) {
    delete finding.consolidation_key;
    delete finding.consolidation_primary;
    delete finding.delivery_group;
    finding.disposition = 'eliminate';
    finding.disposition_reasons = [eliminations[index].reason_code];
  }
  decisions.push(...eliminations.filter((_, index) => eliminated[index].trace_id));
  const actionable = findings.filter((f) => deliveryAllows(f, 'body'));
  const { consolidatedCount } = consolidateCrossAgent(actionable, (f) => deliveryAllows(f, 'inline'));
  const groups = deliveryGroups(actionable).map(({ primary, corroborators }, index) => {
    const members = [primary, ...corroborators];
    const ids = members.map((f) => f.id);
    const issueKey = primary.consolidation_key ? `group:${primary.consolidation_key}` : `issue:${primary.id || index}`;
    const stamp = { issue_key: issueKey, lead_id: primary.id, finding_ids: ids };
    if (ids.every((id) => typeof id === 'string' && id)) for (const member of members) member.delivery_group = stamp;
    return { ...stamp, sites: members.flatMap((f) => f.sites || []),
      tier: members.some((f) => (f.report_tag ?? f.report_destination) === 'main') ? 'main' : 'suggestion',
      disposition: primary.disposition };
  });
  for (const finding of findings.filter((f) => f.consolidation_key && !f.consolidation_primary && f.trace_id)) {
    const related = findings.filter((f) => f.consolidation_key === finding.consolidation_key && f !== finding && f.trace_id).map((f) => f.trace_id);
    if (related.length) decisions.push(traceDecision(finding, 'folded', 'cross-agent consolidation', related, 'coalesced'));
  }
  return { findings, unverified, eliminated, groups, decisions, stats: { ...input.stats,
    skipped: unverified.filter((f) => Object.hasOwn(UNVERIFIED_REASONS.challenge, f.challenge)).length,
    held_back: unverified.length,
    cross_agent_consolidated: consolidatedCount, final_count: findings.length } };
}

// A persisted group is authoritative; legacy stamps only serve older artifacts.
export function deliveryGroups(findings) {
  const groups = [];
  const byKey = new Map();
  for (const finding of findings || []) {
    const key = finding.delivery_group?.issue_key || finding.consolidation_key;
    if (!key) { groups.push([finding]); continue; }
    if (!byKey.has(key)) { byKey.set(key, []); groups.push(byKey.get(key)); }
    byKey.get(key).push(finding);
  }
  return groups.map((members) => {
    const stamp = members[0].delivery_group;
    const ordered = stamp ? [...members].sort((a, b) => stamp.finding_ids.indexOf(a.id) - stamp.finding_ids.indexOf(b.id)) : members;
    const primary = ordered.find((f) => stamp ? f.id === stamp.lead_id : f.consolidation_primary) || ordered[0];
    return { primary, corroborators: ordered.filter((f) => f !== primary) };
  });
}
