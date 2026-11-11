import { VERDICT_ENTRY_SCHEMA } from './registry.js';
import { rankFindings } from './groupRanking.js';
import { deepClone } from './wire.js';
import { evaluateAdmission } from './admission.js';
import { verdictRejections, routeVerdict, buildVerdict, resolveVerdict } from './verdict.js';

export function applyChallenges(findings, challenges) {
  const byId = new Map(challenges.filter((entry) => verdictRejections(entry, VERDICT_ENTRY_SCHEMA).length === 0).map((entry) => [entry.id, entry]));
  const active = [], eliminated = [];
  const stats = { total_input: findings.length, challenge_removed: 0, challenge_survived: 0, unchallenged: 0 };
  for (const original of findings) {
    const entry = byId.get(original.id);
    if (!entry) { active.push(original); stats.unchallenged += 1; continue; }
    const finding = deepClone(original);
    const { refutation_check } = entry;
    const verdict = buildVerdict(entry);
    finding.challenge_verdict = verdict;
    const validator = finding.validator_verdict ?? finding.verdict;
    finding.verdict = resolveVerdict(validator, verdict, finding.validator_refutation_check, refutation_check);
    delete finding.challenge_refutation_check;
    if (refutation_check) finding.challenge_refutation_check = deepClone(refutation_check);
    const effectiveCheck = finding.verdict === verdict ? refutation_check : finding.validator_refutation_check;
    delete finding.refutation_check;
    if (effectiveCheck) finding.refutation_check = deepClone(effectiveCheck);
    finding.challenge_justification = verdict.justification;
    routeVerdict(finding);
    finding.challenge = finding.verdict_route === 'not-established' ? 'failed' : 'survived';
    if (finding.verdict_route === 'eliminated') {
      eliminated.push({ ...finding, eliminated_by: 'verified_refutation', elimination_reason: finding.verdict.justification });
      stats.challenge_removed += 1;
    } else {
      active.push(evaluateAdmission(finding));
      if (finding.verdict_route !== 'not-established') stats.challenge_survived += 1;
    }
  }
  return { findings: rankFindings(active), eliminated, stats: { ...stats, final_count: active.length } };
}
