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
