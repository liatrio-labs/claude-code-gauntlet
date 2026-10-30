import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rankFindings } from '../src/applyChallenges.js';
import { finding, consolidationFinding } from './helpers/findings.js';

const RANK_CASES = [
  { name: 'in-diff outranks off-diff and unknown at equal severity', rows: [['off', 'high', 100, 'off', 9, false],
    ['unknown', 'high', 100, 'unknown', 1], ['in', 'high', 60, 'in', 1, true]], expected: ['in', 'off', 'unknown'] },
  { name: 'severity order', rows: [['low', 'low', 90], ['critical', 'critical', 70], ['high', 'high', 80], ['medium', 'medium', 85]],
    expected: ['critical', 'high', 'medium', 'low'] },
  { name: 'longer description breaks a tie', rows: [['short', 'high', 80, 'Short.'], ['long', 'high', 80,
    'A much longer description with more detail.']], expected: ['long', 'short'] },
  { name: 'risk level beats description length', rows: [['low-risk-long', 'high', 80, 'A very long description indeed for this finding.',
    1], ['high-risk-short', 'high', 80, 'Short.', 9]], expected: ['high-risk-short', 'low-risk-long'] },
];
for (const c of RANK_CASES) test(`rank: ${c.name}`, () => {
  const rows = c.rows.map(([id, severity, confidence, description, risk_level, in_diff]) => finding({ id, severity, confidence,
    diff_validation: { in_diff },
    ...(description === undefined ? {} : { description }), ...(risk_level === undefined ? {} : { risk_level }) }));
  assert.deepEqual(rankFindings(rows).map((f) => f.id), c.expected);
});


const ORIGIN_RANK_ROWS = [
  {
    name: 'verified findings outrank degraded confidence',
    input: [{ id: 'A', origin: 'unknown', confidence: 75, severity: 'high' },
      { id: 'B', origin: 'verified', confidence: 80, severity: 'high' },
      { id: 'C', origin: 'verified', confidence: 74, severity: 'high' }], expected: ['B', 'C', 'A'],
  },
];
for (const row of ORIGIN_RANK_ROWS) test(`rank origin: ${row.name}`, () => {
  assert.deepEqual(rankFindings(row.input.map(consolidationFinding)).map((item) => item.id), row.expected);
});
