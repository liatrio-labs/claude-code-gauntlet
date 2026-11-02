import { acceptSkepticalReturn } from './registry.js';
import { validatorAssessment } from './applyValidations.js';
import { matchedCitation } from './citeWire.js';

export function skepticalProof(finding, raw, stage) {
  const assessment = stage === 'validator' ? validatorAssessment(finding, raw) : acceptSkepticalReturn(raw, finding.id, stage);
  const requests = Array.isArray(raw.citations) ? raw.citations : [];
  const proved = assessment.status === 'complete' && ['real', 'refuted'].includes(raw.verdict)
    && requests.some((q) => q.role === 'claim') && requests.every((q) => matchedCitation(finding, q, stage));
  return { assessment, requests, proved };
}
