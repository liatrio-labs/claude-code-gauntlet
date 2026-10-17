import { incompleteVetting, UNVERIFIED_REASONS } from './vetting.js';
import { consolidateCrossAgent } from './filterFindings.js';

// Recompute from membership so fresh outputs and repeated checkpoint replay agree.
export function holdBackIncomplete(challengeOut) {
  const clean = [];
  const unverified = challengeOut.unverified || [];
  for (const finding of challengeOut.findings || []) {
    if (incompleteVetting(finding).length) unverified.push(finding);
    else clean.push(finding);
  }
  for (const finding of unverified) {
    delete finding.consolidation_key;
    delete finding.consolidation_primary;
  }
  const { findings, consolidatedCount } = consolidateCrossAgent(clean);
  challengeOut.findings = findings;
  challengeOut.unverified = unverified;
  challengeOut.stats = {
    ...challengeOut.stats,
    skipped: unverified.filter((f) => Object.hasOwn(UNVERIFIED_REASONS.challenge, f.challenge)).length,
    held_back: unverified.filter((f) => !Object.hasOwn(UNVERIFIED_REASONS.challenge, f.challenge) && incompleteVetting(f).length).length,
    cross_agent_consolidated: consolidatedCount,
    final_count: findings.length,
  };
  return challengeOut;
}
