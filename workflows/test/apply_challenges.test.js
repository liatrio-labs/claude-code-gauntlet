// Challenge decisions never mutate caller findings, including in the workflow sandbox.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyChallenges, downgradeSeverity, rankFindings } from '../src/applyChallenges.js';
import { finding } from './helpers/findings.js';
import { challengeStage } from '../src/stages.js';

const DOWNGRADE_CASES = [
  { name: 'critical becomes high', input: 'critical', expected: 'high' },
  { name: 'medium becomes low', input: 'medium', expected: 'low' },
  { name: 'unknown severity has no lower tier', input: 'unknown', expected: null },
  { name: 'severity is case insensitive', input: 'HIGH', expected: 'medium' },
];
for (const c of DOWNGRADE_CASES) test(`downgrade: ${c.name}`, () => {
  assert.equal(downgradeSeverity(c.input), c.expected);
});

const CHALLENGE_CASES = [
  { name: 'score zero downgrades critical security finding', f: { severity: 'critical', dimension: 'security' }, challenge: { id: 'f1', score: 0 }, active: ['f1'], severity: 'high', destination: 'suggestion', stat: 'challenge_downgraded' },
  { name: 'score 40 downgrades and routes to suggestion', f: { severity: 'critical' }, challenge: { id: 'f1', score: 40 }, active: ['f1'], severity: 'high', destination: 'suggestion', stat: 'challenge_downgraded', original: 'critical' },
  { name: 'surfaced score 30 routes to suggestion', f: { origin: 'surfaced', report_destination: 'main' }, challenge: { id: 'f1', score: 30 }, active: ['f1'], severity: 'medium', destination: 'suggestion', stat: 'challenge_downgraded' },
  { name: 'surfaced score 80 retains main route', f: { origin: 'surfaced', report_destination: 'main' }, challenge: { id: 'f1', score: 80 }, active: ['f1'], severity: 'high', destination: 'main', stat: 'challenge_survived' },
  { name: 'challenge without id is skipped', f: {}, challenge: { score: 80 }, active: ['f1'], severity: 'high', stat: 'unchallenged' },
  { name: 'noninteger score is skipped', f: {}, challenge: { id: 'f1', score: 'high' }, active: ['f1'], severity: 'high', stat: 'unchallenged' },
];
for (const c of CHALLENGE_CASES) test(`challenge: ${c.name}`, () => {
  const out = applyChallenges([finding(c.f)], [c.challenge]);
  assert.deepEqual(out.findings.map((f) => f.id), c.active);
  assert.equal(out.findings[0].severity, c.severity);
  if (c.destination) assert.equal(out.findings[0].report_destination, c.destination);
  if (c.original) assert.equal(out.findings[0].original_severity, c.original);
  assert.equal(out.stats[c.stat], 1);
  assert.equal(out.stats.final_count, 1);
});

test('challenge: mixed removed, downgraded, contested, survived and unchallenged scores', () => {
  const findings = ['f1', 'f2', 'f3', 'f4', 'f5'].map((id) => finding({ id, severity: id === 'f1' ? 'critical' : 'high' }));
  const scores = [10, 35, 60, 90];
  const out = applyChallenges(findings, scores.map((score, i) => ({ id: `f${i + 1}`, score })));
  assert.deepEqual(out.findings.map((f) => f.id), ['f3', 'f4', 'f5', 'f2']);
  assert.deepEqual(out.eliminated.map((f) => f.id), ['f1']);
  assert.deepEqual(out.stats, { total_input: 5, challenge_removed: 1, challenge_downgraded: 1, challenge_contested: 1, challenge_survived: 1, unchallenged: 1, cross_agent_consolidated: 0, final_count: 4 });
});

const RANK_CASES = [
  { name: 'severity order', rows: [['low', 'low', 90], ['critical', 'critical', 70], ['high', 'high', 80], ['medium', 'medium', 85]], expected: ['critical', 'high', 'medium', 'low'] },
  { name: 'longer description breaks a tie', rows: [['short', 'high', 80, 'Short.'], ['long', 'high', 80, 'A much longer description with more detail.']], expected: ['long', 'short'] },
  { name: 'higher risk level breaks a tie', rows: [['low-risk', 'high', 80, 'Same', 2], ['high-risk', 'high', 80, 'Same', 8]], expected: ['high-risk', 'low-risk'] },
  { name: 'risk level beats description length', rows: [['low-risk-long', 'high', 80, 'A very long description indeed for this finding.', 1], ['high-risk-short', 'high', 80, 'Short.', 9]], expected: ['high-risk-short', 'low-risk-long'] },
];
for (const c of RANK_CASES) test(`rank: ${c.name}`, () => {
  const rows = c.rows.map(([id, severity, confidence, description, risk_level]) => finding({ id, severity, confidence, ...(description === undefined ? {} : { description }), ...(risk_level === undefined ? {} : { risk_level }) }));
  assert.deepEqual(rankFindings(rows).map((f) => f.id), c.expected);
});

test('challenge stage: stage returns challenge envelope and timestamp', async () => {
  const ctx = { agent: async () => ({ score: 80 }), parallel: async (thunks) => Promise.all(thunks.map((thunk) => thunk())) };
  const out = await challengeStage(ctx, { findings: [finding()], generatedAt: 't' });
  assert.deepEqual(Object.keys(out).sort(), ['eliminated', 'findings', 'gaps', 'generated_at', 'stats', 'unverified']);
  assert.equal(out.findings[0].id, 'f1');
  assert.equal(out.stats.challenge_survived, 1);
  assert.equal(out.generated_at, 't');
});

test('applyChallenges never mutates the caller input, even with structuredClone absent', () => {
  const saved = globalThis.structuredClone;
  try {
    delete globalThis.structuredClone; // the sandbox has no structuredClone
    const findings = [{ id: 'F1', severity: 'high', dimension: 'bug', confidence: 90, description: 'x' }];
    const before = JSON.parse(JSON.stringify(findings));
    // score 80 -> survive; the survivor is deep-cloned before challenge_score is stamped.
    const out = applyChallenges(findings, [{ id: 'F1', score: 80 }]);
    assert.deepEqual(findings, before, 'input findings unchanged (deep-cloned before mutation)');
    assert.equal(out.findings[0].challenge_score, 80, 'the returned (cloned) finding carries the score');
    assert.ok(!('challenge_score' in findings[0]), 'the caller object never gained the score');
  } finally {
    globalThis.structuredClone = saved;
  }
});
