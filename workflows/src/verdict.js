import { VERDICT_ROUTES, VERDICT_SCHEMA } from './registry.js';

export function verdictRejections(value) {
  if (!value || typeof value !== 'object') return ['missing_verdict'];
  const reasons = [];
  for (const [key, schema] of Object.entries(VERDICT_SCHEMA.properties)) {
    if (VERDICT_SCHEMA.required.includes(key) || value[key] !== undefined) {
      if (schema.enum ? !schema.enum.includes(value[key]) : schema.type === 'string'
        && (typeof value[key] !== 'string' || !value[key].trim())) reasons.push(`invalid_${key}`);
    }
  }
  if (value.claim === 'not_established' && (typeof value.missing_fact !== 'string' || !value.missing_fact.trim())) reasons.push('missing_fact');
  if (value.consequence === 'latent' && (typeof value.latent_condition !== 'string' || !value.latent_condition.trim())) reasons.push('latent_condition');
  // Missing citations cannot refute, but are retained for the next independent check.
  return reasons;
}

export function verdictRule(finding) {
  const value = finding.verdict;
  if (verdictRejections(value).length) return VERDICT_ROUTES.not_established;
  if (value.claim === 'refuted') return VERDICT_ROUTES.refuted[finding.refutation_check?.verified === true ? 'verified' : 'unverified'];
  return value.claim === 'confirmed' ? VERDICT_ROUTES.confirmed[value.consequence] : VERDICT_ROUTES.not_established;
}

export function routeVerdict(finding) {
  const rule = verdictRule(finding);
  if (['reachability', 'consequence'].includes(finding.demoted_by) && !rule.severity) {
    if (finding.original_severity) finding.severity = finding.original_severity;
    delete finding.demoted_by;
    delete finding.demotion_reason;
  }
  delete finding.route_reason;
  finding.verdict_route = rule.route;
  finding.reachability = finding.verdict?.reachability ?? 'uncertain';
  finding.report_destination = finding.report_tag = rule.tier;
  if (rule.severity) {
    finding.original_severity ??= finding.severity;
    finding.severity = rule.severity;
    finding.demoted_by = rule.demotion ?? 'reachability';
    finding.demotion_reason = finding.verdict.latent_condition ?? finding.verdict.justification;
  }
  return finding;
}
