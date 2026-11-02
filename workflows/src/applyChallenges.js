// Ranking remains independent of skeptical verdicts and citation proof.

import { SEVERITY_ORDER } from './registry.js';

export function rankKey(finding) {
  const sev = (finding.severity ?? 'low').toLowerCase();
  let sevIdx = SEVERITY_ORDER.indexOf(sev);
  if (sevIdx < 0) sevIdx = SEVERITY_ORDER.length;
  const degraded = finding.origin === 'unknown' ? 1 : 0;
  const conf = finding.confidence ?? 0;
  let tertiary;
  const rl = finding.risk_level;
  if (rl !== undefined && rl !== null && Number.isFinite(Number(rl))) tertiary = -Number(rl);
  else tertiary = -((finding.description ?? '').length);
  return [sevIdx, degraded, -conf, tertiary];
}

export function rankFindings(findings) {
  return [...findings].sort((a, b) => {
    const ka = rankKey(a);
    const kb = rankKey(b);
    return ka[0] - kb[0] || ka[1] - kb[1] || ka[2] - kb[2] || ka[3] - kb[3];
  });
}
