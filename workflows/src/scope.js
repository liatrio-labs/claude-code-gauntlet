import { EXCLUSION_CATEGORIES, SKEPTICAL_FINDING_PROPERTIES, SCOPE_ASSESSMENT_SCHEMA, DISPATCH_RESULTS, acceptSkepticalReturn, assessmentGaps, matchesDispatchSchema, dimensionRow, findingPolicy } from './registry.js';
import { canonicalRange } from './location.js';
import { fnv1a32 } from './wire.js';
import { citeChecksum, collectCitations, matchedCitation } from './citeWire.js';
import { lineBucket } from './filterFindings.js';
import { validatorAssessment } from './applyValidations.js';

const scopeRows = EXCLUSION_CATEGORIES.filter((row) => ['early', 'post_validation'].includes(row.stage));
const scopeInputFields = ['id', 'file', 'agent', 'dimension', 'origin_dimension', 'test_kind', 'quality_basis', 'security_kind', 'path_role', 'cross_file_refs',
  'blame_class', 'blame_source', 'scope_facts', 'diff_validation', 'reachability', 'validation', 'admission',
  'line_start', 'line_end', 'location_status', 'policy_subject', 'skeptical_assessments', 'cite_results'];
const scopeAnd = (...values) => values.includes(false) ? false : values.every((value) => value === true) ? true : null;
const scopeOr = (...values) => values.includes(true) ? true : values.every((value) => value === false) ? false : null;
const capturedScope = (f) => f.scope_facts?.file === f.file && f.scope_facts.head && f.scope_facts.snapshot_id ? f.scope_facts : {};
const exceptionRequests = (f) => collectCitations([f]).filter((r) => r.request?.role === 'exception'
  && f.skeptical_assessments.some((a) => a.stage === r.stage && (a.raw ?? a).verdict === 'real')).map((r) => r.request);
const scopePredicates = {
  blame_pre_existing: (f) => scopeAnd(capturedScope(f).file ? true : null,
    f.blame_class === 'unknown' || !f.blame_class ? null : f.blame_class === 'pre_existing',
    f.blame_source === 'git_blame', Array.isArray(f.cross_file_refs) ? f.cross_file_refs.length === 0 : null),
  off_diff: (f) => f.diff_validation?.in_diff == null ? null : f.diff_validation.in_diff === false,
  generated_or_vendored: (f) => scopeOr(...['linguist_generated', 'linguist_vendored', 'generated_marker'].map((key) => capturedScope(f)[key])),
  documentation_only_code_claim: (f) => scopeAnd(capturedScope(f).all_changed_files_documentation,
    f.path_role === 'unknown' || !f.path_role ? null : f.path_role === 'source'),
  mechanical_lockfile: (f) => scopeAnd(capturedScope(f).lockfile_basename, capturedScope(f).mechanical_change),
  future_change_only: (f) => f.validation === 'validated' ? f.reachability === 'future_change_only' : null,
  harmless_test_pattern: (f) => !f.path_role || f.path_role === 'unknown' ? null : f.path_role === 'test',
};
const scopeExceptions = {
  cross_file_effect_created_by_pr: (f) => Array.isArray(f.cross_file_refs) ? f.cross_file_refs.length > 0 : null,
  security_dependency_vulnerability_claim: (f) => f.dimension !== 'security' ? false
    : f.security_kind === 'dependency_vulnerability' ? true : f.security_kind === 'other' ? false : null,
  documentation_or_comment_claim: (f) => dimensionRow(f.dimension)?.dimension === 'comment_accuracy',
  test_applicable_documented_convention: (f) => dimensionRow(f.dimension)?.dimension === 'convention',
  present_static_defect: (f) => f.reachability === 'current',
  test_coverage_origin: (f) => findingPolicy({ ...f, policy_subject: undefined }, false).row?.dimension === 'test_coverage',
  test_applicable_convention: (f) => dimensionRow(f.dimension)?.dimension === 'convention',
  genuine_test_defect: (f) => f.policy_subject === 'test_defect',
  proven_incorrect_or_dangerous_behavior_including_security: (f) => {
    const requests = exceptionRequests(f);
    return requests.some((q) => matchedCitation(f, q, q.stage)) ? true : requests.length ? null : false;
  },
};

function skepticalScope(finding, confirmations) {
  return (finding.skeptical_assessments || []).flatMap((a) => {
    const raw = a.raw ?? a;
    const accepted = a.stage === 'validator' ? validatorAssessment(finding, raw) : acceptSkepticalReturn(raw, finding.id, a.stage);
    return (Array.isArray(raw.scope_assessments) ? raw.scope_assessments : []).flatMap((scope) => {
      const row = EXCLUSION_CATEGORIES.find((r) => r.category === scope?.category && r.consumers.includes(a.stage));
      if (!row) return [];
      const confirmation = confirmations.find((c) => matchesDispatchSchema(c,
        DISPATCH_RESULTS['code-gauntlet:conventions-and-intent'].conventions_confirmations.items)
        && confirmations.filter((other) => other?.id === c.id).length === 1
        && canonicalRange(finding) && c.file === finding.file && lineBucket(c.line_start, 10) === lineBucket(finding.line_start, 10));
      const exceptions = row.exceptions.map((name) => scopeExceptions[name]?.(finding));
      const excepted = exceptions.includes(true);
      const contradicted = ['inapplicable', 'contradicted'].includes(scope.applicability) || excepted
        || scopePredicates[row.predicate]?.(finding) === false
        || (row.predicate === 'confirmed_intent' && !confirmation);
      const citations = Array.isArray(scope.citations) ? scope.citations : [];
      const proved = !assessmentGaps(accepted).semanticErrors.length
        && !exceptions.includes(null)
        && finding.skeptical_assessments.filter((other) => other?.stage === a.stage).length === 1
        && raw.scope_assessments.filter((other) => other?.category === row.category).length === 1
        && (!scopePredicates[row.predicate] || scopePredicates[row.predicate](finding) === true)
        && matchesDispatchSchema(scope, SCOPE_ASSESSMENT_SCHEMA) && scope.finding_id === finding.id && scope.stage === a.stage
        && citations.every((q) => matchedCitation(finding, q, a.stage));
      const necessary = [...citations, ...(exceptions.includes(null) ? exceptionRequests(finding) : [])];
      const unavailable = necessary.some((q) => finding.cite_results?.some((r) =>
        citeChecksum(r.request) === citeChecksum(q) && r.status === 'unavailable'));
      return [{ category: row.category, stage: a.stage, outcome: row.outcome,
        applies: contradicted ? false : proved ? true : null,
        reason: contradicted ? 'scope_contradicted' : proved ? 'out_of_scope' : unavailable ? 'cite_unavailable' : 'cite_unmatched',
        request_ids: citations.map((q) => q?.request_id).filter((id) => typeof id === 'string'),
        ...(row.predicate === 'confirmed_intent' && confirmation ? { confirmation_id: confirmation.id } : {}) }];
    });
  });
}

// Stamps include the row definitions so replay cannot reuse outcomes from changed policy.
export function scopeDecisions(finding, confirmations = []) {
  const identity = JSON.stringify([EXCLUSION_CATEGORIES, scopeInputFields.map((key) => finding[key] ?? null), confirmations]);
  const proof = (decisions) => fnv1a32(JSON.stringify([identity, decisions.map(({ input_proof, ...row }) => row)]));
  const saved = finding.scope_decisions;
  if (matchesDispatchSchema(saved, SKEPTICAL_FINDING_PROPERTIES.scope_decisions) && saved.length >= scopeRows.length
    && saved.every((row) => row.input_proof === proof(saved))) return saved;
  const decisions = scopeRows.map((row) => ({ category: row.category, stage: row.stage, outcome: row.outcome,
    applies: scopeAnd(scopePredicates[row.predicate]?.(finding),
      !finding.admission?.reasons?.some((reason) => reason.predicate === 'present_defect' && reason.status === 'held_back'),
      ...row.exceptions.map((name) => { const value = scopeExceptions[name]?.(finding); return value == null ? null : !value; })) }));
  decisions.push(...skepticalScope(finding, confirmations));
  const inputProof = proof(decisions);
  finding.scope_decisions = decisions.map((row) => ({ ...row, input_proof: inputProof }));
  return finding.scope_decisions;
}
