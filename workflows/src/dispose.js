import { deepClone } from './wire.js';
import { incompleteVetting, UNVERIFIED_REASONS } from './vetting.js';
import { locationAllows, deliveryAllows } from './location.js';
import { consolidateCrossAgent } from './filterFindings.js';
import { eliminationDecisions, traceDecision, admissionDetail } from './measurementTrace.js';
import { hasAdmission } from './admission.js';
import { chooseGroupLead, groupDelivery } from './groupRanking.js';

const DELIVERABLE_CHALLENGES = ['survived'];

export function findingDisposition(finding, heldBack = false) {
  const reasons = incompleteVetting(finding).map((stage) => stage === 'verify' ? 'verify_unproven' : 'validation_skipped');
  if (Object.hasOwn(UNVERIFIED_REASONS.challenge, finding.challenge) && finding.challenge !== 'skipped') {
    reasons.push(finding.challenge === 'capped' ? 'challenge_cap' : 'challenge_failed');
  }
  if (heldBack && !reasons.length) reasons.push('held_back');
  let disposition = 'body';
  const admitted = hasAdmission(finding);
  if (reasons.length) disposition = 'held_back';
  else if (!admitted || finding.admission.status !== 'admitted') {
    disposition = admitted ? finding.admission.status : 'held_back';
    reasons.push(disposition);
  } else if (!DELIVERABLE_CHALLENGES.includes(finding.challenge)) {
    disposition = 'held_back';
    reasons.push('challenge_failed');
  } else if (finding.admission.inline_eligible
    && finding.diff_validation?.in_diff === true && locationAllows(finding, 'inline')) {
    disposition = 'deliver';
  } else reasons.push('body_routed');
  return { disposition, reasons: [...new Set([...reasons, ...(admitted ? finding.admission.reasons.map((reason) => reason.status) : [])])] };
}

export function isDeliveryCandidate(finding) {
  return ['deliver', 'body'].includes(findingDisposition(finding).disposition);
}

export function disposeFindings(challenged) {
  const input = deepClone(challenged);
  const findings = [];
  const unverified = [];
  const eliminated = input.eliminated || [];
  const decisions = [];
  for (const finding of [...(input.findings || []), ...(input.unverified || [])]) {
    delete finding.site_requests;
    delete finding.resolved_sites;
    delete finding.consolidation_key;
    delete finding.consolidation_primary;
    delete finding.delivery_group;
    const { disposition, reasons } = findingDisposition(finding, (input.unverified || []).includes(finding));
    finding.disposition = disposition;
    finding.disposition_reasons = reasons;
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
    finding.disposition = 'eliminate';
    finding.disposition_reasons = [eliminations[index].reason_code];
  }
  decisions.push(...eliminations.filter((_, index) => eliminated[index].trace_id));
  const actionable = findings.filter((f) => deliveryAllows(f, 'body'));
  const groupingNeeded = (input.grouping_ids || actionable).length >= 2;
  const accepted = groupingNeeded ? acceptGroupingReply(input.grouping_reply, input.grouping_ids || [
    ...(input.findings || []), ...(input.unverified || []), ...(input.eliminated || []),
  ].map((f) => f.id), actionable) : { error: null, groups: [] };
  const grouping = !groupingNeeded ? 'not_needed' : accepted.error ? 'positional' : 'model';
  const gaps = accepted.error ? [`grouping: ${input.grouping_error || accepted.error}; same-lines grouping used`] : [];
  if (accepted.error) consolidateCrossAgent(actionable);
  const candidates = accepted.error
    ? deliveryGroups(actionable).filter((g) => g.corroborators.length).map(({ primary, corroborators }) => ({
      members: [primary, ...corroborators], consolidation_key: primary.consolidation_key,
    })) : accepted.groups;
  for (const group of candidates) {
    const primary = chooseGroupLead(group.members);
    for (const member of group.members) {
      member.consolidation_key = group.consolidation_key ?? primary.id;
      member.consolidation_primary = member === primary;
    }
  }
  const delivery = deliveryGroups(actionable);
  const placementTradeoffs = delivery.filter(({ primary, corroborators }) => !deliveryAllows(primary, 'inline')
    && corroborators.some((f) => deliveryAllows(f, 'inline'))).length;
  const groupedMembers = delivery.filter((g) => g.corroborators.length).reduce((n, g) => n + 1 + g.corroborators.length, 0);
  const groups = delivery.map(({ primary, corroborators }, index) => {
    const members = [primary, ...corroborators];
    const ids = members.map((f) => f.id);
    const issueKey = primary.consolidation_key ? `group:${grouping === 'model' ? primary.id : primary.consolidation_key}` : `issue:${primary.id || index}`;
    const stamp = { issue_key: issueKey, lead_id: primary.id, finding_ids: ids };
    const delivered = groupDelivery(primary, corroborators);
    if (ids.every((id) => typeof id === 'string' && id)) for (const member of members) member.delivery_group = stamp;
    return { ...stamp, sites: members.flatMap((f) => f.sites || []),
      severity: delivered.severity, tier: delivered.report_tag,
      disposition: primary.disposition, same_challenge_verdict: new Set(members.map((f) => f.verdict_route ?? f.challenge)).size === 1 };
  });
  for (const finding of findings.filter((f) => f.consolidation_key && !f.consolidation_primary && f.trace_id)) {
    const related = findings.filter((f) => f.consolidation_key === finding.consolidation_key && f !== finding && f.trace_id).map((f) => f.trace_id);
    if (related.length) decisions.push(traceDecision(finding, 'folded', 'cross-agent consolidation', related, 'coalesced'));
  }
  return { findings, unverified, eliminated, groups, decisions, gaps,
    ...Object.fromEntries(['grouping_reply', 'grouping_ids', 'grouping_error'].filter((key) => Object.hasOwn(input, key)).map((key) => [key, input[key]])), stats: { ...input.stats,
    skipped: unverified.filter((f) => Object.hasOwn(UNVERIFIED_REASONS.challenge, f.challenge)).length,
    held_back: unverified.length,
    grouping, grouped_members: groupedMembers, grouping_placement_tradeoffs: placementTradeoffs,
    grouping_verdicts: groups.filter((g) => g.finding_ids.length > 1).map((g) => ({ issue_key: g.issue_key, same_challenge_verdict: g.same_challenge_verdict })),
    cross_agent_consolidated: groupedMembers, final_count: findings.length } };
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

// Validate the complete reply before filtering members removed by replay safeguards.
export function acceptGroupingReply(reply, ids, findings) {
  const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
  const reject = (error) => ({ error, groups: [] });
  if (!object(reply)) return reject('missing or non-object reply');
  if (!Array.isArray(reply.groups)) return reject('groups is not an array');
  if (Object.keys(reply).some((key) => key !== 'groups')) return reject('unexpected reply field');
  const known = new Set(ids);
  const seen = new Set();
  for (const group of reply.groups) {
    if (!object(group) || !Array.isArray(group.finding_ids)) return reject('invalid group');
    for (const id of group.finding_ids) {
      if (!known.has(id)) return reject('unknown id');
      if (seen.has(id)) return reject('overlapping id');
      seen.add(id);
    }
  }
  const byId = new Map(findings.map((f) => [f.id, f]));
  return { error: null, groups: reply.groups.map((group) => ({
    members: group.finding_ids.map((id) => byId.get(id)).filter(Boolean),
  })).filter((group) => group.members.length >= 2) };
}
