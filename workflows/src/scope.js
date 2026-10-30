import { EXCLUSION_CATEGORIES, SCOPE_FACT_KEYS, dimensionRow } from './registry.js';
import { fnv1a32 } from './wire.js';

const scopeRows = EXCLUSION_CATEGORIES.filter((row) => ['early', 'post_validation'].includes(row.stage));
const scopeInputFields = ['id', 'file', 'dimension', 'origin_dimension', 'path_role',
  'blame_class', 'blame_source', 'scope_facts', 'diff_validation', 'reachability', 'validation', 'admission'];
const scopeAnd = (...values) => values.includes(false) ? false : values.every((value) => value === true) ? true : null;
const scopeOr = (...values) => values.includes(true) ? true : values.every((value) => value === false) ? false : null;
const capturedScope = (f) => f.scope_facts?.file === f.file && f.scope_facts.snapshot_id
  ? Object.fromEntries(SCOPE_FACT_KEYS.map((key) => [key, f.scope_facts[key]])) : {};
const scopePredicates = {
  blame_pre_existing: (f) => scopeAnd(f.scope_facts?.file === f.file && !!f.scope_facts.snapshot_id,
    f.blame_class === 'unknown' || !f.blame_class ? null : f.blame_class === 'pre_existing',
    f.blame_source === 'git_blame', f.diff_validation?.in_diff == null ? null : f.diff_validation.in_diff === false),
  off_diff: (f) => f.diff_validation?.in_diff == null ? null : f.diff_validation.in_diff === false,
  generated_or_vendored: (f) => { const facts = capturedScope(f); return scopeOr(facts.linguist_generated, facts.linguist_vendored,
    facts.linguist_generated === false ? false : facts.generated_marker); },
  documentation_only_code_claim: (f) => scopeAnd(capturedScope(f).all_changed_files_documentation,
    capturedScope(f).linguist_documentation == null ? null : capturedScope(f).linguist_documentation === false,
    f.path_role === 'unknown' || !f.path_role ? null : f.path_role === 'source'),
  lockfile: (f) => capturedScope(f).lockfile_basename,
  future_change_only: (f) => f.validation === 'validated' ? f.reachability === 'future_change_only' : null,
};
const scopeExceptions = {
  security_dimension: (f) => f.dimension === 'security',
  documentation_or_comment_claim: (f) => dimensionRow(f.dimension)?.dimension === 'comment_accuracy',
  test_applicable_documented_convention: (f) => dimensionRow(f.dimension)?.dimension === 'convention',
  present_static_defect: (f) => f.reachability === 'current',
};

// One audit stamp binds the recomputed decisions to their policy and inputs.
export function scopeDecisions(finding) {
  const identity = JSON.stringify([scopeRows, scopeInputFields.map((key) => finding[key] ?? null)]);
  const decisions = scopeRows.map((row) => ({ category: row.category, stage: row.stage, outcome: row.outcome,
    applies: scopeAnd(scopePredicates[row.predicate]?.(finding),
      !finding.admission?.reasons?.some((reason) => reason.predicate === 'present_defect' && reason.status === 'held_back'),
      ...row.exceptions.map((name) => { const value = scopeExceptions[name]?.(finding); return value == null ? null : !value; })) }));
  finding.scope_input_proof = fnv1a32(JSON.stringify([identity, decisions]));
  finding.scope_decisions = decisions;
  return finding.scope_decisions;
}
