// Shared check identifiers keep holdback and report reasons aligned.
export const UNVERIFIED_REASONS = {
  validation: 'the validator did not score this finding, so it was not skeptically validated',
  verify: 'the verify slice could not be proven against the dispatched document',
  challenge: {
    capped: 'the challenge cap was reached, so this finding was not challenge-verified',
    failed: 'the challenger failed or returned no score, so this finding was not challenge-verified',
    skipped: 'this finding was not challenge-verified',
  },
};

export function incompleteVetting(finding) {
  const missing = [];
  if (finding.validation === 'skipped') missing.push('validation');
  if (finding.origin === 'unknown') missing.push('verify');
  return missing;
}
