import { AGENT_SECURITY_REVIEWER, SCOPE_ROWS, SCOPE_FACT_KEYS, SCOPE_ASSESSMENT_SCHEMA, DISPATCH_RESULTS, CITE_VERIFIER_VERSION, VALIDATION_RETURN_PROPERTIES, matchesDispatchSchema, dimensionRow, findingPolicy } from './registry.js';
import { canonicalRange, lineBucket } from './location.js';
import { fnv1a32 } from './wire.js';
import { citeResult, collectCitations, matchedCitation } from './citeWire.js';
import { skepticalProof } from './skepticalProof.js';

const scopeRows = SCOPE_ROWS.filter((row) => row.proof === 'verified_fact');
const scopeInputFields = ['id', 'file', 'agent', 'dimension', 'origin_dimension', 'test_kind', 'quality_basis', 'path_role',
  'blame_class', 'blame_source', 'scope_facts', 'diff_validation', 'reachability', 'validation', 'admission',
  'line_start', 'line_end', 'location_status', 'policy_subject', 'skeptical_assessments', 'cite_results'];
const scopeAnd = (...values) => {
  if (values.includes(false)) return false;
  return values.every((value) => value === true) ? true : null;
};
const scopeOr = (...values) => {
  if (values.includes(true)) return true;
  return values.every((value) => value === false) ? false : null;
};
const exceptionRequests = (f) => collectCitations([f]).filter((r) => r.request?.role === 'exception'
  && f.skeptical_assessments.some((a) => a.stage === r.stage && a.raw?.verdict === 'real')).map((r) => r.request);
const capturedScope = (f) => f.scope_facts?.file === f.file && f.scope_facts.snapshot_id
  ? Object.fromEntries(SCOPE_FACT_KEYS.map((key) => [key, f.scope_facts[key]])) : {};
const offDiff = (f) => f.diff_validation?.in_diff == null ? null : f.diff_validation.in_diff === false;
const scopePredicates = {
  blame_pre_existing: (f) => scopeAnd(Object.keys(capturedScope(f)).length > 0,
    f.blame_class === 'unknown' || !f.blame_class ? null : f.blame_class === 'pre_existing',
    f.blame_source === 'git_blame', offDiff(f)),
  off_diff: offDiff,
  generated_or_vendored: (f) => { const facts = capturedScope(f); return scopeOr(facts.linguist_generated, facts.linguist_vendored,
    facts.linguist_generated === false ? false : facts.generated_marker); },
  documentation_only_code_claim: (f) => { const facts = capturedScope(f); return scopeAnd(facts.all_changed_files_documentation,
    facts.linguist_documentation == null ? null : facts.linguist_documentation === false,
    f.path_role === 'unknown' || !f.path_role ? null : f.path_role === 'source'); },
  lockfile: (f) => capturedScope(f).lockfile_basename,
  future_change_only: (f) => f.validation === 'validated' ? f.reachability === 'future_change_only' : null,
  harmless_test_pattern: (f) => !f.path_role || f.path_role === 'unknown' ? null : f.path_role === 'test',
};
const scopeExceptions = {
  // The producing agent counts too: a security reviewer's free-text dimension may not say "security".
  security_dimension: (f) => f.dimension === 'security' || f.agent === AGENT_SECURITY_REVIEWER,
  documentation_or_comment_claim: (f) => dimensionRow(f.dimension)?.dimension === 'comment_accuracy',
  present_static_defect: (f) => f.reachability === 'current' ? true : f.reachability === 'future_change_only' ? false : null,
  test_coverage_origin: (f) => findingPolicy({ ...f, policy_subject: undefined }, false).row?.dimension === 'test_coverage',
  test_applicable_convention: (f) => dimensionRow(f.dimension)?.dimension === 'convention',
  genuine_test_defect: (f) => !VALIDATION_RETURN_PROPERTIES.policy_subject.enum.includes(f.policy_subject) || f.policy_subject === 'unresolved' ? null : f.policy_subject === 'test_defect',
  proven_incorrect_or_dangerous_behavior_including_security: (f) => {
    const requests = exceptionRequests(f);
    return requests.some((q) => matchedCitation(f, q, q.stage)) ? true : requests.length ? null : false;
  },
};

export function outsideAddedLines(finding, request) {
  const result = citeResult(finding, request);
  if (!matchedCitation(finding, request, request.stage) || result?.source?.verifier_version !== CITE_VERIFIER_VERSION
    || !Array.isArray(result?.added_lines) || !result.added_lines.every((line) => Number.isSafeInteger(line) && line > 0)) return null;
  return !result.added_lines.some((line) => line >= request.line_start && line <= request.line_end);
}

function skepticalScope(finding, confirmations) {
  return (finding.skeptical_assessments || []).flatMap((a) => {
    const raw = a.raw ?? {};
    const proof = skepticalProof(finding, raw, a.stage);
    return (Array.isArray(raw.scope_assessments) ? raw.scope_assessments : []).flatMap((scope) => {
      const row = SCOPE_ROWS.find((r) => r.category === scope?.category && r.consumers.includes(a.stage));
      if (!row) return [];
      const citations = Array.isArray(scope.citations) ? scope.citations : [];
      const confirmed = confirmations.filter((c) => matchesDispatchSchema(c,
        DISPATCH_RESULTS['code-gauntlet:conventions-and-intent'].conventions_confirmations.items)
        && confirmations.filter((other) => other?.id === c.id).length === 1
        && canonicalRange(finding) && c.file === finding.file && lineBucket(c.line_start, 10) === lineBucket(finding.line_start, 10))
        .map((c) => ({ confirmation: c, proof: scopeOr(...citations
        .filter((q) => q.file === c.file && q.side === 'head' && q.line_start <= c.line_start && q.line_end >= c.line_start)
        .map((q) => outsideAddedLines(finding, q))) }));
      const confirmation = (confirmed.find((c) => c.proof === true) || confirmed[0])?.confirmation;
      const exceptions = row.exceptions.map((name) => scopeExceptions[name]?.(finding));
      const intent = row.predicate !== 'confirmed_intent' ? true : scopeAnd(
        ...citations.filter((q) => q.role === 'rule').map((q) => q.side === 'old' ? true : outsideAddedLines(finding, q)),
        scopeOr(...confirmed.map((c) => c.proof)));
      const contradicted = ['inapplicable', 'contradicted'].includes(scope.applicability) || exceptions.includes(true)
        || scopePredicates[row.predicate]?.(finding) === false || intent === false;
      const proved = proof.proved && intent === true
        && !exceptions.includes(null)
        && finding.skeptical_assessments.filter((other) => other?.stage === a.stage).length === 1
        && raw.scope_assessments.filter((other) => other?.category === row.category).length === 1
        && (!scopePredicates[row.predicate] || scopePredicates[row.predicate](finding) === true)
        && matchesDispatchSchema(scope, SCOPE_ASSESSMENT_SCHEMA) && scope.finding_id === finding.id && scope.stage === a.stage
        && citations.every((q) => matchedCitation(finding, q, a.stage));
      const necessary = [...citations, ...(exceptions.includes(null) ? exceptionRequests(finding) : [])];
      const unavailable = necessary.some((q) => citeResult(finding, q)?.status === 'unavailable');
      return [{ category: row.category, stage: a.stage, outcome: row.outcome,
        applies: contradicted ? false : proved ? true : null,
        reason: contradicted ? 'scope_contradicted' : proved ? 'out_of_scope' : unavailable ? 'cite_unavailable'
          : intent !== true ? 'held_back' : 'cite_unmatched',
        request_ids: citations.map((q) => q?.request_id).filter((id) => typeof id === 'string'),
        ...(row.predicate === 'confirmed_intent' && confirmation ? { confirmation_id: confirmation.id } : {}) }];
    });
  });
}

// One audit stamp binds the recomputed decisions to their policy and inputs.
export function scopeDecisions(finding, confirmations = []) {
  const identity = JSON.stringify([SCOPE_ROWS, scopeInputFields.map((key) => finding[key] ?? null), confirmations]);
  const decisions = scopeRows.map((row) => ({ category: row.category, stage: row.stage, outcome: row.outcome,
    applies: scopeAnd(scopePredicates[row.predicate]?.(finding),
      !finding.admission?.reasons?.some((reason) => reason.predicate === 'present_defect' && reason.status === 'held_back'),
      ...row.exceptions.map((name) => { const value = scopeExceptions[name]?.(finding); return value == null ? null : !value; })) }));
  decisions.push(...skepticalScope(finding, confirmations));
  finding.scope_input_proof = fnv1a32(JSON.stringify([identity, decisions]));
  finding.scope_decisions = decisions;
  return finding.scope_decisions;
}
