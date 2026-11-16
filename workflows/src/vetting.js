import { verdictState } from './verdict.js';
// Shared check identifiers keep holdback and report reasons aligned.
export const UNVERIFIED_REASONS = {
  validation: 'neither the validator nor the challenger established this finding',
  verify: 'the verify slice could not be proven against the dispatched document',
  challenge: {
    capped: 'the challenge cap was reached, so this finding was not challenge-verified',
    failed: 'the challenger failed or returned no typed verdict, so this finding was not challenge-verified',
    skipped: 'this finding was not challenge-verified',
  },
};

export function incompleteVetting(finding) {
  const missing = [];
  const independent = finding.challenge === 'survived' && ['confirmed', 'refuted_verified']
    .includes(verdictState(finding.challenge_verdict, finding.challenge_refutation_check));
  if (finding.validation === 'skipped' && !independent) missing.push('validation');
  if (finding.origin === 'unknown') missing.push('verify');
  return missing;
}
