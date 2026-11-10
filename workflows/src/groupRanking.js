import { asText, asConfidence } from './wire.js';
import { deliveryAllows } from './location.js';
import { SEVERITY_ORDER, findingPolicy } from './registry.js';

function groupTier(finding) {
  return (finding.report_tag ?? finding.report_destination ?? finding.admission?.tier) === 'main' ? 1 : 0;
}

function groupSeverity(finding) {
  const index = SEVERITY_ORDER.indexOf(finding.severity);
  return index < 0 ? 0 : SEVERITY_ORDER.length - index;
}

export function chooseGroupLead(members, modelLead) {
  const key = (f) => [groupTier(f), groupSeverity(f), deliveryAllows(f, 'inline') ? 1 : 0,
    f.id === modelLead ? 1 : 0, asConfidence(f.confidence), findingPolicy(f).core ? 1 : 0, asText(f.description).length];
  return [...members].sort((a, b) => {
    const ka = key(a), kb = key(b);
    for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return kb[i] - ka[i];
    return 0;
  })[0];
}

export function dominatedGroups(members, modelLead) {
  const groups = [];
  let remaining = [...members];
  while (remaining.length) {
    const lead = chooseGroupLead(remaining, modelLead);
    const dominates = (member) => groupTier(lead) >= groupTier(member)
      && groupSeverity(lead) >= groupSeverity(member)
      && ((member.dimension ?? '').toLowerCase() !== 'security' || (lead.dimension ?? '').toLowerCase() === 'security');
    groups.push({ primary: lead, members: remaining.filter(dominates) });
    remaining = remaining.filter((member) => !dominates(member));
  }
  return groups;
}
