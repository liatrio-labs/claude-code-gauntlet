// Apply validator confidence and reachability updates to findings.

import { INT_POLICY, coerceInt } from './wire.js';
import { canonicalRange } from './location.js';

export const REACHABILITY_VALUES = ['current', 'future_change_only', 'uncertain'];

export function applyValidations(findings, validations) {
  const findingById = new Map();
  for (const finding of findings) {
    if (finding.sites || finding.resolved_sites) finding.sites = [];
    const fid = 'id' in finding ? finding.id : undefined;
    if (fid !== null && fid !== undefined) findingById.set(fid, finding);
  }

  let adjustedCount = 0;
  const unmatchedIds = [];

  for (const validation of validations) {
    const vid = 'id' in validation ? validation.id : undefined;
    if (vid === null || vid === undefined) continue; // missing id -- skipped (warning)

    const rawConf = 'confidence' in validation ? validation.confidence : undefined;
    if (rawConf === null || rawConf === undefined) continue; // missing confidence -- skipped (warning)

    const parsed = coerceInt(rawConf, INT_POLICY.validation);
    if (parsed === null) continue; // non-integer confidence -- skipped (warning)

    const newConf = Math.max(0, Math.min(100, parsed));

    const finding = findingById.get(vid);
    if (finding === undefined) {
      unmatchedIds.push(vid);
      continue;
    }

    if (!('original_confidence' in finding)) {
      finding.original_confidence = 'confidence' in finding ? finding.confidence : 0;
    }
    finding.validator_confidence = newConf;
    finding.confidence = newConf;

    const justification = 'justification' in validation ? validation.justification : undefined;
    if (justification) finding.validation_justification = justification;
    const reachability = 'reachability' in validation ? validation.reachability : undefined;
    if (REACHABILITY_VALUES.includes(reachability)) finding.reachability = reachability;
    const key = (site) => JSON.stringify([site.file, site.line_start, site.line_end]);
    const confirmations = new Map((Array.isArray(validation.site_confirmations) ? validation.site_confirmations : [])
      .filter((site) => site && typeof site === 'object').map((site) => [key(site), site.confirmed === true]));
    if (finding.resolved_sites) finding.sites = finding.resolved_sites.filter((site) => canonicalRange(site) && confirmations.get(key(site)) === true);

    adjustedCount += 1;
  }

  return { adjustedCount, unmatchedIds };
}
