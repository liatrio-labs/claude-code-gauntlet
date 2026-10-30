import { ADMISSION_PREDICATES, RIGHT_ELIGIBILITY, VERIFY_FACT_PROPERTIES, VERIFY_REFERENCE_DESCRIPTORS, FINDING_PROP_TYPES, DIMENSIONS, findingPolicy } from './registry.js';
import { canonicalRange } from './location.js';
import { fnv1a32 } from './wire.js';

const resolvedReference = (ref) => canonicalRange(ref) !== null;
const hasValue = (value) => typeof value === 'string' && value.trim().length > 0;

// Stable request names bind semantic assessments to the source facts sent for this finding.
export function admissionRequests(finding) {
  const requests = {};
  if (canonicalRange(finding)) requests.primary = { file: finding.file, line_start: finding.line_start, line_end: finding.line_end };
  for (const [name, kind] of Object.entries(VERIFY_REFERENCE_DESCRIPTORS)) {
    const value = finding.resolved_references?.[name];
    const refs = kind !== 'reference' ? value : [value];
    for (const [index, ref] of (Array.isArray(refs) ? refs : []).entries()) {
      if (kind === 'search_paths' ? ref?.available === true : resolvedReference(ref)) {
        requests[kind !== 'reference' ? `${name}:${index}` : name] = ref;
      }
    }
  }
  return requests;
}

export function matchAssessments(finding, entries) {
  const requests = admissionRequests(finding);
  if (!Array.isArray(entries)) return [];
  return entries.filter((entry) => {
    const descriptor = ADMISSION_PREDICATES[entry?.predicate];
    if (!descriptor?.references || !['confirmed', 'rejected', 'unresolved'].includes(entry.status)
      || entries.filter((other) => other?.predicate === entry.predicate).length !== 1
      || !Array.isArray(entry.references) || new Set(entry.references).size !== entry.references.length
      || entry.references.some((key) => typeof key !== 'string' || !Object.hasOwn(requests, key))) return false;
    const expected = Object.keys(requests).filter((key) => descriptor.references.some((name) => key === name || key.startsWith(`${name}:`)));
    return (expected.length > 0 || entry.status === 'unresolved')
      && expected.length === entry.references.length && expected.every((key) => entry.references.includes(key));
  }).map(({ predicate, status, references }) => ({ predicate, status, references: [...references] }));
}

const ADMISSION_INPUT_FIELDS = [...new Set(['id', 'file', 'anchor', 'dimension', 'origin_dimension', 'agent', 'origin', 'validation',
  'policy_subject', 'admission_assessments', 'reachability', 'location_status', 'line_start', 'line_end', 'diff_validation',
  'reference_diagnostics', ...Object.keys(VERIFY_FACT_PROPERTIES),
  ...DIMENSIONS.flatMap((row) => row.admission ? [row.admission.field,
    ...Object.values(row.admission.kinds).flatMap((kind) => [...kind.required, ...(kind.optional || [])])] : [])])];
function admissionProof(finding, fields) {
  const verdict = { ...finding.admission };
  delete verdict.input_proof;
  return fnv1a32(JSON.stringify({ verdict, inputs: [...ADMISSION_INPUT_FIELDS, ...fields].map((key) =>
    ADMISSION_PRESENCE_FIELDS.has(key) ? hasValue(finding[key]) : finding[key] ?? null) }));
}

// Delivery rewrites host paths in prose; only its presence is a policy input.
const ADMISSION_PRESENCE_FIELDS = new Set(DIMENSIONS.flatMap((row) => row.admission
  ? Object.values(row.admission.kinds).flatMap((kind) => [...kind.required, ...(kind.optional || []), ...(kind.anyOf || [])])
    .filter((key) => { const schema = row.schemaExtra[key] || FINDING_PROP_TYPES[key]; return schema === 'string' || (schema?.type === 'string' && !schema.enum); })
  : []));

function referenceResult(finding, name) {
  if (resolvedReference(finding.resolved_references?.[name])) return null;
  const supplied = finding[name] !== undefined || finding.reference_diagnostics?.some((diagnostic) => diagnostic.field === name);
  return supplied ? 'held_back' : 'report_only';
}

const prOwnedTest = (finding) => finding.path_role === 'test' && finding.diff_validation?.in_diff === true;

export function hasAdmission(finding) {
  const stamp = finding?.admission;
  return stamp?.version === 1 && ['admitted', 'report_only', 'held_back'].includes(stamp.status)
    && Array.isArray(stamp.reasons) && Array.isArray(stamp.input_fields)
    && stamp.input_proof === admissionProof(finding, stamp.input_fields);
}

function factAssessment(finding, predicate) {
  const ref = finding.resolved_references;
  const facts = {
    pr_owned_test: () => {
      if (prOwnedTest(finding)) return null;
      return finding.diff_validation?.in_diff == null ? 'held_back' : 'report_only';
    },
    criticality: () => typeof finding.criticality === 'number' && finding.criticality >= 8 ? null : 'report_only',
    searches: () => {
      const incomplete = !Array.isArray(ref?.searched_paths) || ref.searched_paths.length === 0
        || ref.searched_paths.some((path) => path.available !== true) || !Array.isArray(ref?.candidate_tests)
        || ref.candidate_tests.some((test) => !resolvedReference(test))
        || !Array.isArray(finding.candidate_tests) || finding.candidate_tests.some((test) => !hasValue(test?.input) || !hasValue(test?.assertion))
        || finding.reference_diagnostics?.length;
      return incomplete ? 'held_back' : null;
    },
    duplicate_of: () => referenceResult(finding, 'duplicate_of'),
    precedent: () => referenceResult(finding, 'precedent'),
    precedent_not_added: () => {
      const result = referenceResult(finding, 'precedent');
      if (result) return result;
      if (ref.precedent.file_added == null) return 'held_back';
      return ref.precedent.file_added ? 'report_only' : null;
    },
    equivalent: () => hasValue(finding.suggestion) || hasValue(finding.suggested_fix_code) ? null : 'report_only',
    reachability: () => finding.reachability === 'current' ? null : 'held_back',
    location: () => canonicalRange(finding) ? null : 'held_back',
  };
  return Object.hasOwn(facts, predicate) ? facts[predicate]() : 'held_back';
}

function predicateResult(finding, predicate) {
  const descriptor = ADMISSION_PREDICATES[predicate];
  if (!descriptor) return 'held_back';
  if (descriptor.placement) return null;
  const fact = descriptor.fact ? factAssessment(finding, descriptor.fact) : null;
  if (fact) return fact;
  if (descriptor.references) {
    for (const name of descriptor.references) {
      if (name === 'primary' && !canonicalRange(finding)) return 'held_back';
      if (['production_element', 'duplicate_of', 'precedent'].includes(name)) {
        const result = referenceResult(finding, name);
        if (result) return result;
      }
    }
    const assessment = matchAssessments(finding, finding.admission_assessments).find((entry) => entry.predicate === predicate);
    if (!assessment || assessment.status === 'unresolved') return 'held_back';
    if (assessment.status === 'rejected') return 'report_only';
  }
  return null;
}

// This is the only policy evaluation. Disposal and sinks consume its persisted result.
export function evaluateAdmission(finding) {
  delete finding.admission;
  delete finding.effective_test_kind;
  const original = findingPolicy({ ...finding, policy_subject: undefined });
  const promotion = original.row?.admission?.promotion;
  const promoted = promotion && matchAssessments(finding, finding.admission_assessments)
    .some((entry) => entry.predicate === promotion.predicate && entry.status === 'confirmed');
  const terminal = !promoted && original.kind?.defaultDisposition === 'report_only';
  const policyFinding = promoted ? { ...finding, policy_subject: 'ordinary', [original.row.admission.field]: promotion.kind } : finding;
  const { row, kind, core, tier } = terminal ? original : findingPolicy(policyFinding);
  const subject = row?.policySubjects[policyFinding.policy_subject];
  const reasons = [];
  const fail = (predicate, status) => { if (status) reasons.push({ predicate, status }); };
  if (!['new', 'modified', 'surfaced', 'pre_existing'].includes(finding.origin)) fail('verification', 'held_back');
  if (finding.validation !== 'validated') fail('validation', 'held_back');
  if (!terminal && ((row?.admission && !subject) || policyFinding.policy_subject === 'unresolved')) fail('policy_subject', 'held_back');
  if (!row || (row.admission && !kind)) fail('kind', 'held_back');
  if (kind?.defaultDisposition === 'report_only') fail('value', 'report_only');
  for (const predicate of kind?.predicates || []) fail(predicate, predicateResult(finding, predicate));
  let status = 'admitted';
  if (reasons.some((reason) => reason.status === 'held_back')) status = 'held_back';
  else if (reasons.length) status = 'report_only';
  if (subject?.stamp && status === 'admitted') finding[subject.stamp] = subject.kind;
  const selectedKind = (promoted && promotion.kind) || (!terminal && subject?.kind) || (row?.admission ? finding[row.admission.field] : 'ordinary');
  if (promoted && status === 'admitted') {
    finding.origin_dimension ??= original.row.dimension;
    if (kind.dimension) finding.dimension = kind.dimension;
    if (promotion.stamp) finding[promotion.stamp] = promotion.kind;
  }
  const inputFields = kind?.anyOf || [];
  const routedTier = finding.report_tag ?? finding.report_destination;
  let effectiveTier = tier || 'main';
  if (!promoted && ['main', 'suggestion'].includes(routedTier)) effectiveTier = routedTier;
  finding.admission = { version: 1, input_fields: inputFields, status, reasons,
    kind: selectedKind || 'unknown', core, tier: effectiveTier,
    inline_eligible: !kind?.predicates.includes('test_diff_site_for_inline')
      || prOwnedTest(finding) };
  finding.admission.input_proof = admissionProof(finding, inputFields);
  finding.body_fix_task_eligible = status === 'admitted' && RIGHT_ELIGIBILITY.body.fixTask.every((predicate) => predicateResult(finding, predicate) === null);
  if (status === 'admitted') finding.report_destination = finding.report_tag = finding.admission.tier;
  return finding;
}

export function admissionContext(finding) {
  const policy = findingPolicy(finding, false);
  return { requests: admissionRequests(finding), facts: Object.fromEntries(Object.keys(VERIFY_FACT_PROPERTIES).map((key) => [key, finding[key]])),
    policy: { predicates: policy.kind?.predicates || [], promotion: policy.row?.admission?.promotion,
      policy_subjects: Object.fromEntries(Object.keys(policy.row?.policySubjects || {}).map((subject) =>
        [subject, findingPolicy({ ...finding, policy_subject: subject }, false).kind?.predicates || []])),
      body_fix_task: RIGHT_ELIGIBILITY.body.fixTask },
    claim: Object.fromEntries(DIMENSIONS.flatMap((row) => Object.keys(row.schemaExtra)).map((key) => [key, finding[key]])),
    fix: finding.suggestion || finding.suggested_fix_code };
}
