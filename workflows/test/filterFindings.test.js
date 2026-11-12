import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeFieldNames } from '../src/filterFindings.js';
import { FIELD_RENAMES } from '../src/registry.js';

const NORMALIZE_ROWS = [
  {
    name: "existing description retains body",
    input: [{ id: "n2", body: "old body", description: "canonical desc" }],
    expected: { count: 0, findings: [{ id: "n2", body: "old body", description: "canonical desc" }] }
  },

  {
    name: "all three legacy fields count once",
    input: [{ id: "n7", body: "explanation", line: 99, blame_tag: "new" }],
    expected: { count: 1, findings: [{ id: "n7", description: "explanation", line_start: 99, origin: "new" }] }
  },

  {
    name: "copied renames",
    input: [{ legacy: "x" }],
    renames: { ...FIELD_RENAMES, legacy: "custom" },
    expected: { count: 1, findings: [{ custom: "x" }] }
  }
];
for (const row of NORMALIZE_ROWS) test('normalize: ' + row.name, () => {
  const input = structuredClone(row.input);
  const count = normalizeFieldNames(input, row.renames);
  assert.deepEqual({ count, findings: input }, row.expected);
});
