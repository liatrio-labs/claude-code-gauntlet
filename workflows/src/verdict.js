import { VERDICT_ROUTES, VERDICT_CASES, CONSEQUENCE_ORDER, VERDICT_SCHEMA, VERDICT_RESOLUTIONS, CONFIRMED_COMMENT_SCHEMA } from './registry.js';
import { schemaErrors } from './schema.js';
import { deepClone } from './wire.js';

export function verdictRejections(value, schema = VERDICT_SCHEMA) {
  return schemaErrors(value, schema);
}

export function confirmedComment(finding) {
  const verdict = finding?.verdict;
  return verdict?.claim === 'confirmed' && !schemaErrors(verdict.comment, CONFIRMED_COMMENT_SCHEMA).length ? verdict.comment : null;
}

export function buildVerdict(value) {
  return Object.fromEntries(['claim', 'justification', ...VERDICT_CASES[value.claim]]
    .filter((key) => value[key] !== undefined && (key !== 'comment' || confirmedComment({ verdict: value }))).map((key) => [key, deepClone(value[key])]));
}

export function verdictState(verdict, check) {
  if (verdictRejections(verdict).length) return 'not_established';
  return verdict.claim === 'refuted' ? `refuted_${check?.verified === true ? 'verified' : 'unverified'}` : verdict.claim;
}

export function resolveVerdict(validator, challenger, validatorCheck, challengerCheck) {
  const choice = VERDICT_RESOLUTIONS[verdictState(validator, validatorCheck)][verdictState(challenger, challengerCheck)];
  if (choice === 'more_serious') return CONSEQUENCE_ORDER.indexOf(validator.consequence) < CONSEQUENCE_ORDER.indexOf(challenger.consequence) ? validator : challenger;
  return choice === 'validator' ? validator : challenger;
}

export function verdictRule(finding) {
  const value = finding.verdict;
  if (verdictRejections(value).length) return VERDICT_ROUTES.not_established;
  const context = { dimension: finding.dimension?.toLowerCase(), phase: finding.challenge_verdict ? 'challenge' : 'validation', claim: value.claim,
    verified: finding.refutation_check?.verified === true,
    validatorRefuted: finding.validator_verdict?.claim === 'refuted',
    validatorVerified: finding.validator_refutation_check?.verified === true,
    challengerRefuted: finding.challenge_verdict?.claim === 'refuted', challengerVerified: finding.challenge_refutation_check?.verified === true,
    anyVerifiedRefutation: (finding.challenge_verdict?.claim === 'refuted' && finding.challenge_refutation_check?.verified === true)
      || (finding.validator_verdict?.claim === 'refuted' && finding.validator_refutation_check?.verified === true) };
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
