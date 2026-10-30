import { deepClone } from './wire.js';
import { incompleteVetting, UNVERIFIED_REASONS } from './vetting.js';
import { locationAllows, deliveryAllows } from './location.js';
import { consolidateCrossAgent } from './filterFindings.js';
import { eliminationDecisions, traceDecision, admissionDetail } from './measurementTrace.js';
import { hasAdmission } from './admission.js';
import { assessmentGaps } from './registry.js';
import { citeResult } from './citeWire.js';
import { skepticalProof } from './skepticalProof.js';
import { scopeDecisions } from './scope.js';

function skepticalOutcome(finding) {
  const reasons = [];
  const proofs = [];
  let proven = null;
  for (const stage of ['validator', 'challenger']) {
    if (stage === 'challenger' && ['validator_terminal', 'admission_terminal', 'scope_terminal'].includes(finding.challenge)) continue;
    const entries = (finding.skeptical_assessments || []).filter((a) => a.stage === stage);
    const operational_gap = stage === 'validator' ? finding.validation === 'skipped' : ['failed', 'capped', 'skipped'].includes(finding.challenge);
    const proof = { stage, verdict: null, proved: false, unresolved: !operational_gap, unavailable: false,
      operational_gap, non_dispatch: stage === 'validator' ? operational_gap : ['capped', 'skipped'].includes(finding.challenge) };
    proofs.push(proof);
    if (entries.length !== 1) {
      reasons.push({ reason: 'held_back', stage, missing_field: 'verdict' });
      continue;
    }
    const raw = entries[0].raw ?? entries[0];
    const { assessment, requests, proved } = skepticalProof(finding, raw, stage);
    const { semanticErrors } = assessmentGaps(assessment);
    Object.assign(proof, { verdict: ['real', 'refuted', 'needs_evidence'].includes(raw.verdict) ? raw.verdict : null,
      proved, unresolved: !operational_gap && (!['real', 'refuted'].includes(raw.verdict) || semanticErrors.length > 0),
      unavailable: (finding.cite_results || []).some((r) => r.request?.stage === stage && r.status === 'unavailable') });
    if (raw.verdict === 'needs_evidence') reasons.push({ reason: 'held_back', stage, missing_evidence: raw.missing_evidence });
    if (['real', 'refuted'].includes(raw.verdict) && !proved) {
      const unavailable = requests.some((q) => citeResult(finding, q)?.status === 'unavailable');
      reasons.push({ reason: unavailable ? 'cite_unavailable' : 'cite_unmatched', stage, verdict: raw.verdict });
    }
    if (raw.verdict === 'refuted' && proved) proven = { reason: 'refuted', stage,
      request_ids: requests.map((q) => q.request_id) };
    // A malformed real proof is diagnostic; missing semantic assessments still close delivery.
    if (semanticErrors.length) reasons.push({ reason: 'held_back', stage, missing_fields: semanticErrors });
  }
  return { proven, reasons, proofs, held: reasons.some((r) => r.reason === 'held_back' || r.verdict === 'refuted') };
}

function dispositionDecision(finding, reason) {
  if (reason === 'scope_contradicted') return 'kept';
  if (finding.disposition === 'eliminate') return 'eliminated';
  if (['held_back', 'report_only'].includes(finding.disposition)) return finding.disposition;
  return 'body_routed';
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
    const scopeRows = scopeDecisions(finding, input.conventions_confirmations || []);
    const scope = scopeRows.filter((row) => row.applies === true);
    const skeptical = skepticalOutcome(finding);
    finding.reduction_detail = skeptical.reasons;
    finding.reduction_proofs = skeptical.proofs;
    for (const row of scopeRows.filter((r) => r.reason && r.applies !== true)) {
      skeptical.reasons.push({ reason: row.reason, category: row.category, stage: row.stage });
      if (row.applies === null) skeptical.held = true;
    }
    if (skeptical.proven) {
      finding.eliminated_by = 'refuted';
      finding.elimination_reason = `${skeptical.proven.stage} refutation with matched proof`;
      finding.reduction_provenance = { finding_id: finding.id, disposition: 'eliminate', ...skeptical.proven };
      eliminated.push(finding);
      continue;
    }
    const terminal = scope.filter((row) => !row.request_ids || (!skeptical.held
      && ['survived', 'validator_terminal'].includes(finding.challenge)
      && !scopeRows.some((other) => other.category === row.category && other.stage !== row.stage && other.applies !== true)));
    const terminalScope = terminal.find((row) => row.outcome === 'eliminate') || terminal.find((row) => row.outcome === 'report_only');
    if (scope.some((row) => row.request_ids && !terminal.includes(row))) {
      skeptical.held = true;
      skeptical.reasons.push({ reason: 'held_back', missing_field: 'agreed_scope' });
    }
    if (terminalScope) {
      finding.disposition = terminalScope.outcome;
      finding.disposition_reasons = [terminalScope.outcome === 'eliminate' ? 'out_of_scope' : 'report_only'];
      finding.reduction_provenance = { finding_id: finding.id, disposition: terminalScope.outcome,
        reason: finding.disposition_reasons[0], category: terminalScope.category,
        ...(terminalScope.request_ids ? { stage: terminalScope.stage, request_ids: terminalScope.request_ids } : {}) };
      if (terminalScope.outcome === 'eliminate') {
        finding.eliminated_by = 'out_of_scope';
        finding.elimination_reason = terminalScope.category;
        eliminated.push(finding);
      } else {
        findings.push(finding);
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
    finding.disposition_reasons = [...new Set([...reasons, ...(admitted ? finding.admission.reasons.map((reason) => reason.status) : [])])];
    (disposition === 'held_back' ? unverified : findings).push(finding);
  }
  const eliminations = eliminationDecisions({ eliminated: eliminated.map((f) => ({ ...f, trace_id: f.trace_id || 'unused' })) });
  for (const [index, finding] of eliminated.entries()) {
    delete finding.consolidation_key;
    delete finding.consolidation_primary;
    delete finding.delivery_group;
    finding.disposition = 'eliminate';
    finding.disposition_reasons = [eliminations[index].reason_code];
  }
  for (const finding of [...findings, ...unverified, ...eliminated].filter((f) => f.trace_id)) {
    const rows = [...(finding.reduction_detail || [])];
    rows.push(...finding.disposition_reasons.filter((reason) => !rows.some((row) => row.reason === reason)).map((reason) => ({ reason })));
    for (const row of rows) decisions.push(traceDecision(finding, row.reason, row.reason, [],
      dispositionDecision(finding, row.reason),
      { ...(finding.disposition !== 'eliminate' ? admissionDetail(finding, { predicates: finding.admission?.reasons || [], surface: finding.disposition }) : {}),
        ...(row.stage ? { stage: row.stage } : {}), ...(row.category ? { category: row.category } : {}),
        ...(row.stage ? { reduction: row } : {}) }));
  }
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
