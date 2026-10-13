export function finding(overrides = {}) {
  const value = {
    id: 'f1', file: 'src/x.py', line_start: 10, severity: 'high',
    confidence: 90, title: 'Real bug',
    description: 'The function returns the wrong result for a real input.',
    agent: 'bug-detector', dimension: 'bug', ...overrides,
  };
  for (const key of Object.keys(value)) if (value[key] === undefined) delete value[key];
  return value;
}

export function cleanFinding(overrides = {}) {
  return {
    id: 'test-1', file: 'src/foo.py', line_start: 42, line_end: 45,
    severity: 'high', confidence: 90, title: 'Valid Bug',
    description: 'The function process_data does not validate input types, which could lead to a runtime error ' +
      'when processing a malformed response from the external API service.',
    ...overrides,
  };
}

export function consolidationFinding(overrides = {}) {
  return {
    id: 'x', file: 'a.py', line_start: 10, agent: 'bug-detector', dimension: 'bug',
    severity: 'high', confidence: 70, title: 't', description: 'd', ...overrides,
  };
}
