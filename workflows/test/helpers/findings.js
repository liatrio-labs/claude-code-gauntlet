import { evaluateAdmission } from '../../src/admission.js';
import { skepticalAssessment } from './verdicts.js';

export function finding(overrides = {}) {
  const dimensionFields = {
    security: { attack_vector: 'Untrusted input' }, cross_file_impact: { affected_consumers: [] },
    test_coverage: { test_kind: 'defective_test', criticality: 9, failure_scenario: 'Broken assertion' },
    convention: { claude_md_rule: 'Use the repository rule' }, intent: { spec_text: 'Required behavior' },
    type_design: { quality_basis: 'current_correctness' }, simplification: { quality_basis: 'current_correctness' },
  };
  const value = {
    origin: 'new', validation: 'validated', policy_subject: 'ordinary', path_role: 'source', diff_validation: { in_diff: true },
    location_status: 'confirmed', anchor: 'return process(value);', line_end: overrides.line_start ?? 10,
    id: 'f1', file: 'src/x.py', line_start: 10, severity: 'high',
    confidence: 90, title: 'Real bug',
    description: 'The function returns the wrong result for a real input.',
    agent: 'bug-detector', dimension: 'bug', ...dimensionFields[overrides.dimension], ...overrides,
  };
  value.skeptical_assessments ??= [skepticalAssessment(value.id), skepticalAssessment(value.id, 'challenger')];
  for (const key of Object.keys(value)) if (value[key] === undefined) delete value[key];
  evaluateAdmission(value);
  for (const key of ['report_tag', 'report_destination']) if (Object.hasOwn(overrides, key)) value[key] = overrides[key];
  if (Object.hasOwn(overrides, 'report_destination') && !Object.hasOwn(overrides, 'report_tag')) delete value.report_tag;
  return value;
}

export function cleanFinding(overrides = {}) {
  return {
    location_status: 'confirmed', anchor: 'return process(value);',
    id: 'test-1', file: 'src/foo.py', line_start: 42, line_end: overrides.line_start ?? 45,
    severity: 'high', confidence: 90, title: 'Valid Bug',
    description: 'The function process_data does not validate input types, which could lead to a runtime error ' +
      'when processing a malformed response from the external API service.',
    ...overrides,
  };
}

export function consolidationFinding(overrides = {}) {
  return {
    location_status: 'confirmed', anchor: 'return process(value);', line_end: overrides.line_start ?? 10,
    id: 'x', file: 'a.py', line_start: 10, agent: 'bug-detector', dimension: 'bug',
    severity: 'high', confidence: 70, title: 't', description: 'd', ...overrides,
  };
}
