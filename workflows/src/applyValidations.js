// Apply validator confidence and reachability updates to findings.

import { INT_POLICY, coerceInt } from './wire.js';
import { canonicalRange } from './location.js';
import { matchAssessments } from './admission.js';
import { VALIDATION_RETURN_PROPERTIES, requiredAssessments, REACHABILITY_VALUES, acceptSkepticalReturn } from './registry.js';

export { REACHABILITY_VALUES };

export function validationRejections(finding, validation, semantic = true) {
  if (!validation) return ['missing_entry'];
  const reasons = [];
  if (coerceInt(validation.confidence, INT_POLICY.validation) === null) reasons.push('invalid_confidence');
  if (!VALIDATION_RETURN_PROPERTIES.policy_subject.enum.includes(validation.policy_subject)) reasons.push('invalid_policy_subject');
  const assessments = Array.isArray(validation.admission_assessments) ? validation.admission_assessments : [];
  for (const predicate of requiredAssessments(finding, validation.policy_subject)) {
    const entries = assessments.filter((entry) => entry?.predicate === predicate);
    if (entries.length !== 1
      || !VALIDATION_RETURN_PROPERTIES.admission_assessments.items.properties.status.enum.includes(entries[0].status)
      || !Array.isArray(entries[0].references) || entries[0].references.some((key) => typeof key !== 'string')) {
      reasons.push(`missing_assessment:${predicate}`);
    }
  }
  if (semantic) reasons.push(...validatorAssessment(finding, validation).errors
    .filter((error) => !['$.confidence', '$.policy_subject'].includes(error)
      && !(error.startsWith('$.admission_assessments') && reasons.some((reason) => reason.startsWith('missing_assessment:'))))
    .map((error) => `skeptical:${error}`));
  return reasons;
}

// Scores use the same coercion as application; verdict acceptance never discards usable validation fields.
export function validatorAssessment(finding, validation) {
  const { id: _id, ...raw } = validation || {};
  const normalized = { ...raw, confidence: coerceInt(raw.confidence, INT_POLICY.validation) };
  const assessment = acceptSkepticalReturn(normalized, finding.id, 'validator');
  const independent = Object.keys(VALIDATION_RETURN_PROPERTIES).concat(['confidence', 'reachability', 'justification']);
  const errors = assessment.errors.filter((error) => !independent.some((field) => error === `$.${field}` || error.startsWith(`$.${field}[`) || error.startsWith(`$.${field}.`)));
  return { ...assessment, status: errors.length ? 'incomplete' : 'complete', errors, raw };
}

export function applyValidations(findings, validations) {
  const findingById = new Map();
  for (const finding of findings) {
    delete finding.sites;
    delete finding.reachability;
    for (const field of ['policy_subject', 'admission_assessments', 'admission', 'body_fix_task_eligible', 'effective_test_kind', 'skeptical_assessments', 'cite_results']) delete finding[field];
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

    finding.skeptical_assessments = [validatorAssessment(finding, validation)];

    if (!('original_confidence' in finding)) {
      finding.original_confidence = 'confidence' in finding ? finding.confidence : 0;
    }
    finding.validator_confidence = newConf;
    finding.confidence = newConf;

    const justification = 'justification' in validation ? validation.justification : undefined;
    if (justification) finding.validation_justification = justification;
    const reachability = 'reachability' in validation ? validation.reachability : undefined;
    if (REACHABILITY_VALUES.includes(reachability)) finding.reachability = reachability;
    if (VALIDATION_RETURN_PROPERTIES.policy_subject.enum.includes(validation.policy_subject)) finding.policy_subject = validation.policy_subject;
    if (Array.isArray(validation.admission_assessments)) finding.admission_assessments = matchAssessments(finding, validation.admission_assessments);
    const key = (site) => JSON.stringify([site.file, site.line_start, site.line_end]);
    const confirmations = new Map((Array.isArray(validation.site_confirmations) ? validation.site_confirmations : [])
      .filter((site) => site && typeof site === 'object').map((site, _, sites) => [key(site),
        site.confirmed === true && sites.filter((other) => key(other) === key(site)).length === 1]));
    if (finding.resolved_sites) finding.sites = finding.resolved_sites.filter((site) => canonicalRange(site) && confirmations.get(key(site)) === true);

    adjustedCount += 1;
  }

  return { adjustedCount, unmatchedIds };
}
