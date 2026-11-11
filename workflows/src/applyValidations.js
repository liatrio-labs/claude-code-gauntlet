import { canonicalRange } from './location.js';
import { deepClone } from './wire.js';
import { VERDICT_PROPERTIES } from './registry.js';
import { verdictRejections, routeVerdict } from './verdict.js';

export function applyValidations(findings, validations) {
  const byId = new Map();
  for (const finding of findings) {
    for (const field of ['sites', 'verdict', 'validator_verdict', 'verdict_route', 'refutation_check', 'reachability',
      'policy_subject', 'admission_assessments', 'admission', 'body_fix_task_eligible', 'effective_test_kind',
      'contested', 'contestation_drop', 'contestation_reason', 'validator_confidence']) delete finding[field];
    byId.set(finding.id, finding);
  }
  let adjustedCount = 0;
  const unmatchedIds = [];
  for (const validation of validations) {
    if (verdictRejections(validation).length) continue;
    const finding = byId.get(validation.id);
    if (!finding) { unmatchedIds.push(validation.id); continue; }
    const verdict = Object.fromEntries(Object.keys(VERDICT_PROPERTIES).filter((key) => validation[key] !== undefined)
      .map((key) => [key, deepClone(validation[key])]));
    finding.verdict = finding.validator_verdict = verdict;
    finding.validation_justification = verdict.justification;
    if (validation.refutation_check) finding.refutation_check = deepClone(validation.refutation_check);
    const key = (site) => JSON.stringify([site.file, site.line_start, site.line_end]);
    const confirmations = validation.site_confirmations || [];
    if (finding.resolved_sites) finding.sites = finding.resolved_sites.filter((site) => canonicalRange(site)
      && confirmations.filter((s) => key(s) === key(site)).length === 1
      && confirmations.find((s) => key(s) === key(site)).confirmed === true);
    routeVerdict(finding);
    adjustedCount += 1;
  }
  for (const finding of findings) routeVerdict(finding);
  return { adjustedCount, unmatchedIds };
}
