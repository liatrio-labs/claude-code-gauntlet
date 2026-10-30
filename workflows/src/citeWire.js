import { CITE_VERIFIER_VERSION, CITATION_REQUEST_PROPERTIES, CITATION_REQUEST_SCHEMA, EXCLUSION_CATEGORIES, matchesDispatchSchema } from './registry.js';
import { encodeSliceInline } from './verifyWire.js';
import { fnv1a32 } from './wire.js';

export const citeChecksum = (value) => fnv1a32(JSON.stringify(value, null, 2) ?? 'undefined');
export const citeSource = (capture = {}) => ({ snapshot_id: capture.manifest_sha256 || '',
  target_kind: capture.target_kind || '', head: capture.head || '', base: capture.base || '',
  diff_sha256: capture.diff_sha256 || '', verifier_version: CITE_VERIFIER_VERSION });
export const citeRecordKey = (record, source) => JSON.stringify({ record, source });
export function reusableCiteResult(entry, record, source) {
  const result = entry?.result;
  return result && (['matched', 'unmatched', 'rejected'].includes(result.status)
    || (result.status === 'unavailable' && ['oversize', 'request_cap'].includes(result.detail)))
    && typeof result.detail === 'string'
    && citeChecksum(result.request) === citeChecksum(record?.request)
    && citeChecksum(result.source) === citeChecksum(source)
    && (result.status !== 'matched' || (matchesDispatchSchema(record?.request, CITATION_REQUEST_SCHEMA)
      && record.request.snapshot_id === source.snapshot_id));
}
export const CITE_ROLES_PER_STAGE = CITATION_REQUEST_PROPERTIES.role.enum.length
  * (1 + EXCLUSION_CATEGORIES.filter((row) => row.stage === 'skeptical').length);

// Ownership comes from the enclosing assessment, never from an untrusted request.
export function collectCitations(findings) {
  return findings.flatMap((f) => {
    const checks = [];
    const add = (entry, stage) => {
      for (const q of Array.isArray(entry?.citations) ? entry.citations : []) {
        const record = { finding_id: f.id, stage, request: q };
        checks.push({ id: citeChecksum(record), ...record });
      }
    };
    for (const assessment of Array.isArray(f.skeptical_assessments) ? f.skeptical_assessments : []) {
      const entry = assessment?.raw ?? assessment;
      const stage = assessment?.stage;
      add(entry, stage);
      for (const scope of Array.isArray(entry?.scope_assessments) ? entry.scope_assessments : []) add(scope, stage);
    }
    for (const scope of Array.isArray(f.scope_decisions) ? f.scope_decisions : []) add(scope, scope?.stage);
    return checks;
  });
}

export const citeDocument = (records, baseBranch) => ({ requests: records, base_branch: baseBranch });

// Count and byte limits constrain the same encoded document sent to the executor.
export function planCiteBatches(records, budget, baseBranch, platform) {
  const batches = [];
  const oversize = [];
  const cost = (rows) => {
    const token = encodeSliceInline(citeDocument(rows, baseBranch));
    return token.length + (platform === 'windows' ? (token.match(/["\\]/g) || []).length : 0);
  };
  let batch = [];
  for (const record of records) {
    if (cost([record]) > budget) { oversize.push(record); continue; }
    if (batch.length && cost([...batch, record]) > budget) {
      batches.push(batch); batch = [];
    }
    batch.push(record);
  }
  if (batch.length) batches.push(batch);
  return { batches, oversize };
}
