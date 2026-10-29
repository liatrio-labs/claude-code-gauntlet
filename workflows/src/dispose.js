import { deepClone } from './wire.js';
import { incompleteVetting } from './vetting.js';
import { locationAllows, deliveryAllows } from './location.js';
import { consolidateCrossAgent } from './filterFindings.js';
import { eliminationDecisions, traceDecision } from './measurementTrace.js';

export function disposeFindings(challenged) {
  const input = deepClone(challenged);
  const findings = [];
  const unverified = [];
  const eliminated = input.eliminated || [];
  const decisions = [];
  for (const finding of [...(input.findings || []), ...(input.unverified || [])]) {
    delete finding.consolidation_key;
    delete finding.consolidation_primary;
    delete finding.delivery_group;
    delete finding.site_requests;
    delete finding.resolved_sites;
    const reasons = incompleteVetting(finding).map((stage) => stage === 'verify' ? 'verify_unproven' : 'validation_skipped');
    if (['failed', 'capped'].includes(finding.challenge)) reasons.push(finding.challenge === 'capped' ? 'challenge_cap' : 'challenge_failed');
    if ((input.unverified || []).includes(finding) && !reasons.length) reasons.push('held_back');
    const disposition = reasons.length ? 'held_back'
      : (finding.diff_validation === undefined || finding.diff_validation?.in_diff === true) && locationAllows(finding, 'inline') ? 'deliver' : 'body';
    finding.disposition = disposition;
    finding.disposition_reasons = reasons.length ? reasons : disposition === 'body' ? ['body_routed'] : [];
    (disposition === 'held_back' ? unverified : findings).push(finding);
    if (finding.trace_id) for (const reason of finding.disposition_reasons) {
      decisions.push(traceDecision(finding, reason, reason, [], disposition === 'held_back' ? 'held_back' : 'body_routed'));
    }
  }
  const eliminations = eliminationDecisions({ eliminated: eliminated.map((f) => ({ ...f, trace_id: f.trace_id || 'unused' })) });
  for (const [index, finding] of eliminated.entries()) {
    delete finding.consolidation_key;
    delete finding.consolidation_primary;
    finding.disposition = 'eliminate';
    finding.disposition_reasons = [eliminations[index].reason_code];
  }
  decisions.push(...eliminations.filter((_, index) => eliminated[index].trace_id));
  const { consolidatedCount } = consolidateCrossAgent(findings, (f) => deliveryAllows(f, 'inline'));
  const groups = deliveryGroups(findings).map(({ primary, corroborators }, index) => {
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
    skipped: unverified.filter((f) => ['capped', 'failed', 'skipped'].includes(f.challenge)).length,
    held_back: unverified.filter((f) => !['capped', 'failed', 'skipped'].includes(f.challenge)).length,
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
