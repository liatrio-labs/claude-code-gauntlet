import { deepClone } from './wire.js';
import { incompleteVetting } from './vetting.js';
import { locationAllows } from './location.js';
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
  const { consolidatedCount } = consolidateCrossAgent(findings);
  for (const finding of findings.filter((f) => f.consolidation_key && !f.consolidation_primary && f.trace_id)) {
    const related = findings.filter((f) => f.consolidation_key === finding.consolidation_key && f !== finding && f.trace_id).map((f) => f.trace_id);
    if (related.length) decisions.push(traceDecision(finding, 'folded', 'cross-agent consolidation', related, 'coalesced'));
  }
  return { findings, unverified, eliminated, decisions, stats: { ...input.stats,
    skipped: unverified.filter((f) => ['capped', 'failed', 'skipped'].includes(f.challenge)).length,
    held_back: unverified.filter((f) => !['capped', 'failed', 'skipped'].includes(f.challenge)).length,
    cross_agent_consolidated: consolidatedCount, final_count: findings.length } };
}
