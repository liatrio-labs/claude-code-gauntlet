import { VERDICT_ROUTES, VERDICT_PROPERTIES, VERDICT_CASES, CONSEQUENCE_ORDER } from './registry.js';
import { deepClone } from './wire.js';

export function verdictRejections(value) {
  if (!value || typeof value !== 'object' || !Object.hasOwn(VERDICT_CASES, value.claim)) return ['missing_verdict'];
  const fields = VERDICT_CASES[value.claim];
  const reasons = [];
  for (const key of ['claim', 'justification', ...fields]) {
    const schema = VERDICT_PROPERTIES[key];
    if (key === 'latent_condition' && value.consequence !== 'latent' && value[key] === undefined) continue;
    if (schema.type === 'object' && (!value[key] || typeof value[key] !== 'object' || Array.isArray(value[key]))) reasons.push(`invalid_${key}`);
    if (schema.enum ? !schema.enum.includes(value[key]) : schema.type === 'string'
      && (typeof value[key] !== 'string' || !value[key].trim())) reasons.push(`invalid_${key}`);
  }
  for (const key of Object.values(VERDICT_CASES).flat()) {
    if (!fields.includes(key) && value[key] !== undefined) reasons.push(`unexpected_${key}`);
  }
  if (value.reachability !== undefined) reasons.push('unexpected_reachability');
  if (value.site_confirmations !== undefined && (!Array.isArray(value.site_confirmations)
    || value.site_confirmations.some((site) => !site || typeof site !== 'object'
      || typeof site.file !== 'string' || !Number.isInteger(site.line_start) || !Number.isInteger(site.line_end)
      || typeof site.confirmed !== 'boolean'))) reasons.push('invalid_site_confirmations');
  return reasons;
}

export function buildVerdict(value) {
  return Object.fromEntries(['claim', 'justification', ...VERDICT_CASES[value.claim]]
    .filter((key) => value[key] !== undefined).map((key) => [key, deepClone(value[key])]));
}

// A confirming reader cannot discard the other reader's established consequence.
export function resolveVerdict(validator, challenger) {
  if (!verdictRejections(validator).length && validator.claim === 'confirmed' && challenger.claim === 'confirmed'
    && CONSEQUENCE_ORDER.indexOf(validator.consequence) < CONSEQUENCE_ORDER.indexOf(challenger.consequence)) return validator;
  return challenger;
}

export function verdictRule(finding) {
  const value = finding.verdict;
  if (verdictRejections(value).length) return VERDICT_ROUTES.not_established;
  const context = { dimension: finding.dimension?.toLowerCase(), phase: finding.challenge_verdict ? 'challenge' : 'validation', claim: value.claim,
    verified: finding.refutation_check?.verified === true,
    validatorRefuted: finding.validator_verdict?.claim === 'refuted',
    validatorVerified: finding.validator_refutation_check?.verified === true,
    anyRefutation: value.claim === 'refuted' || finding.validator_verdict?.claim === 'refuted' };
  const override = VERDICT_ROUTES.overrides
    .find((row) => Object.entries(row.when).every(([key, expected]) => context[key] === expected));
  if (override) return override;
  if (value.claim === 'refuted') return VERDICT_ROUTES.refuted[context.verified ? 'verified' : 'unverified'];
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
  delete finding.reachability;
  finding.report_destination = finding.report_tag = rule.tier;
  if (rule.severity) {
    finding.original_severity ??= finding.severity;
    finding.severity = rule.severity;
    finding.demoted_by = 'consequence';
    finding.demotion_reason = finding.verdict.latent_condition ?? finding.verdict.justification;
  }
  return finding;
}
