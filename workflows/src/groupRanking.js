import { asText } from './wire.js';
import { deliveryAllows } from './location.js';
import { SEVERITY_ORDER } from './registry.js';

function groupTier(finding) {
  return (finding.report_tag ?? finding.report_destination ?? finding.admission?.tier ?? 'main') === 'main' ? 1 : 0;
}

function groupSeverityRank(finding) {
  const index = SEVERITY_ORDER.indexOf(asText(finding.severity).trim().toLowerCase());
  return index < 0 ? 0 : SEVERITY_ORDER.length - index;
}

export function rankKey(finding) {
  return [-groupSeverityRank(finding), (finding.dimension ?? '').toLowerCase() === 'security' ? 0 : 1,
    finding.origin === 'unknown' ? 1 : 0, -groupTier(finding), deliveryAllows(finding, 'inline') ? 0 : 1,
    asText(finding.file), Number(finding.line_start) || 0, asText(finding.id)];
}

export function compareFindings(a, b) {
  const ka = rankKey(a), kb = rankKey(b);
  for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return ka[i] < kb[i] ? -1 : 1;
  return 0;
}

export function rankFindings(findings) { return [...findings].sort(compareFindings); }

export function chooseGroupLead(members) { return rankFindings(members)[0]; }


export function groupDelivery(primary, corroborators = []) {
  const members = [primary, ...corroborators];
  const severity = SEVERITY_ORDER[SEVERITY_ORDER.length - Math.max(...members.map(groupSeverityRank))] ?? 'low';
  const tier = members.some((f) => groupTier(f)) ? 'main' : 'suggestion';
  return { ...primary, severity, report_tag: tier, report_destination: tier };
}
