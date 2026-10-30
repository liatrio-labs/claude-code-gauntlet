import { EXCLUSION_CATEGORIES, SKEPTICAL_FINDING_PROPERTIES, matchesDispatchSchema, dimensionRow } from './registry.js';
import { fnv1a32 } from './wire.js';

const scopeRows = EXCLUSION_CATEGORIES.filter((row) => ['early', 'post_validation'].includes(row.stage));
const scopeInputFields = ['id', 'file', 'dimension', 'origin_dimension', 'security_kind', 'path_role', 'cross_file_refs',
  'blame_class', 'blame_source', 'scope_facts', 'diff_validation', 'reachability', 'validation', 'admission'];
const scopeAnd = (...values) => values.includes(false) ? false : values.every((value) => value === true) ? true : null;
const scopeOr = (...values) => values.includes(true) ? true : values.every((value) => value === false) ? false : null;
const capturedScope = (f) => f.scope_facts?.file === f.file && f.scope_facts.head && f.scope_facts.snapshot_id ? f.scope_facts : {};
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
};
const scopeExceptions = {
  cross_file_effect_created_by_pr: (f) => Array.isArray(f.cross_file_refs) ? f.cross_file_refs.length > 0 : null,
  security_dependency_vulnerability_claim: (f) => f.dimension !== 'security' ? false
    : f.security_kind === 'dependency_vulnerability' ? true : f.security_kind === 'other' ? false : null,
  documentation_or_comment_claim: (f) => dimensionRow(f.dimension)?.dimension === 'comment_accuracy',
  test_applicable_documented_convention: (f) => dimensionRow(f.dimension)?.dimension === 'convention',
  present_static_defect: (f) => f.reachability === 'current',
};

// Stamps include the row definitions so replay cannot reuse outcomes from changed policy.
export function scopeDecisions(finding) {
  const identity = JSON.stringify([scopeRows, scopeInputFields.map((key) => finding[key] ?? null)]);
  const proof = (decisions) => fnv1a32(JSON.stringify([identity, decisions.map(({ input_proof, ...row }) => row)]));
  const saved = finding.scope_decisions;
  if (matchesDispatchSchema(saved, SKEPTICAL_FINDING_PROPERTIES.scope_decisions) && saved.length === scopeRows.length
    && saved.every((row) => row.input_proof === proof(saved))) return saved;
  const decisions = scopeRows.map((row) => ({ category: row.category, stage: row.stage, outcome: row.outcome,
    applies: scopeAnd(scopePredicates[row.predicate]?.(finding),
      !finding.admission?.reasons?.some((reason) => reason.predicate === 'present_defect' && reason.status === 'held_back'),
      ...row.exceptions.map((name) => { const value = scopeExceptions[name]?.(finding); return value == null ? null : !value; })) }));
  const inputProof = proof(decisions);
  finding.scope_decisions = decisions.map((row) => ({ ...row, input_proof: inputProof }));
  return finding.scope_decisions;
}
