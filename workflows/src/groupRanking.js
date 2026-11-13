import { asText, asConfidence } from './wire.js';
import { deliveryAllows } from './location.js';
import { SEVERITY_ORDER, findingPolicy } from './registry.js';

function groupTier(finding) {
  return (finding.report_tag ?? finding.report_destination ?? finding.admission?.tier ?? 'main') === 'main' ? 1 : 0;
}

function groupSeverityRank(finding) {
  const index = SEVERITY_ORDER.indexOf(asText(finding.severity).trim().toLowerCase());
  return index < 0 ? 0 : SEVERITY_ORDER.length - index;
}

export function chooseGroupLead(members) {
  const key = (f) => [groupSeverityRank(f), (f.dimension ?? '').toLowerCase() === 'security' ? 1 : 0,
    groupTier(f), deliveryAllows(f, 'inline') ? 1 : 0, asConfidence(f.confidence),
    findingPolicy(f).core ? 1 : 0, asText(f.description).length];
  return [...members].sort((a, b) => {
    const ka = key(a), kb = key(b);
    for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return kb[i] - ka[i];
    return 0;
  })[0];
}


export function groupDelivery(primary, corroborators = []) {
  const members = [primary, ...corroborators];
  const severity = SEVERITY_ORDER[SEVERITY_ORDER.length - Math.max(...members.map(groupSeverityRank))] ?? 'low';
  const tier = members.some((f) => groupTier(f)) ? 'main' : 'suggestion';
  return { ...primary, severity, report_tag: tier, report_destination: tier };
}
