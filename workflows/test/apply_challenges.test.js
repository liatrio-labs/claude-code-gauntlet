import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyChallenges, rankFindings } from '../src/applyChallenges.js';
import { finding, consolidationFinding } from './helpers/findings.js';

test('legacy score aliases have no authority over security or ordinary findings', () => {
  const nativeClone = globalThis.structuredClone;
  try {
    delete globalThis.structuredClone;
  for (const dimension of ['security', 'bug']) for (const origin of ['new','surfaced']) for (const severity of ['critical','low','info'])
    for (const key of ['score', 'confidence_claim_is_correct']) for (const score of [0,24,25,40,49,50,60,74,75,90,'invalid']) {
    const input = [finding({ dimension, origin, severity })];
    const before = JSON.parse(JSON.stringify(input));
    let out;
    assert.doesNotThrow(()=>{out=applyChallenges(input,[{id:'f1',[key]:score}]);});
    assert.equal(out.findings.length,1);
    assert.equal(out.findings[0].severity,severity);
    assert.equal(out.findings[0].report_destination,input[0].report_destination);
    assert.equal(out.findings[0].confidence, 90);
    for (const field of ['challenge_score', 'challenge_contested', 'severity_downgraded', 'original_severity']) {
      assert.equal(Object.hasOwn(out.findings[0], field), false);
    }
    assert.deepEqual(out.eliminated, []);
    assert.deepEqual(input, before);
  }
  } finally { globalThis.structuredClone = nativeClone; }
});

const RANK_CASES = [
  { name: 'severity order', rows: [['low', 'low', 90], ['critical', 'critical', 70], ['high', 'high', 80], ['medium', 'medium', 85]],
    expected: ['critical', 'high', 'medium', 'low'] },
  { name: 'longer description breaks a tie', rows: [['short', 'high', 80, 'Short.'], ['long', 'high', 80,
    'A much longer description with more detail.']], expected: ['long', 'short'] },
  { name: 'risk level beats description length', rows: [['low-risk-long', 'high', 80, 'A very long description indeed for this finding.',
    1], ['high-risk-short', 'high', 80, 'Short.', 9]], expected: ['high-risk-short', 'low-risk-long'] },
];
for (const c of RANK_CASES) test(`rank: ${c.name}`, () => {
  const rows = c.rows.map(([id, severity, confidence, description, risk_level]) => finding({ id, severity, confidence,
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
