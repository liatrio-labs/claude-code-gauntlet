// Ranking remains independent of skeptical verdicts and citation proof.

import { SEVERITY_ORDER } from './registry.js';
import { deepClone } from './wire.js';

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

// Numeric legacy returns carry no removal or delivery authority.
export function applyChallenges(findings, challenges) {
  const byId = new Map(challenges.map((entry) => [entry.id, entry]));
  const out = deepClone(findings);
  for (const finding of out) {
    const raw = byId.get(finding.id);
    if (raw?.assessment) finding.skeptical_assessments = [
      ...(finding.skeptical_assessments || []).filter((a) => a.stage !== 'challenger'), raw.assessment];
    else if (raw) finding.challenge_audit = raw;
  }
  return { findings: rankFindings(out), eliminated: [], stats: { total_input: out.length, final_count: out.length } };
}
