// consolidation.test.js — issue #22 D1/D3: consolidateCrossAgent (never drops), and the
// origin-aware extensions to detectDisagreement's consensus grouping and rankFindings'
// ranking. Parity-backed golden fixtures live under tests/fixtures/parity/; this file
// covers the JS-only regression pins and the mixed-origin behavior called out in #73's
// evidence block, which has no Python-recorded fixture of its own (the same #73 A/B/C
// finding shapes are asserted directly here, mirroring the brief's TDD list verbatim).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { consolidateCrossAgent, detectDisagreement, groupByProximity, tagFindings, applyFilterPipeline } from '../src/filterFindings.js';
import { applyChallenges, rankFindings } from '../src/applyChallenges.js';

function f(over) {
  return { id: 'x', file: 'a.py', line_start: 10, agent: 'bug-detector', dimension: 'bug', severity: 'high', confidence: 70, title: 't', description: 'd', ...over };
}

const PROXIMITY_CASES = [
  { name: 'same file distant lines separate', rows: [f({ id: 'a', line_start: 10 }), f({ id: 'b', line_start: 100 })], groups: 2 },
  { name: 'different files separate', rows: [f({ id: 'a', file: 'a.py' }), f({ id: 'b', file: 'b.py' })], groups: 2 },
  { name: 'empty input has no groups', rows: [], groups: 0 },
  { name: 'lines 12 and 13 straddle a bucket', rows: [f({ id: 'a', line_start: 12 }), f({ id: 'b', line_start: 13 })], groups: 2 },
];
for (const c of PROXIMITY_CASES) test(`proximity: ${c.name}`, () => {
  assert.equal(groupByProximity(c.rows).size, c.groups);
});

const CONSOLIDATION_CASES = [
  { name: 'different files do not consolidate', rows: [f({ id: 'f1', file: 'a.py' }), f({ id: 'f2', file: 'b.py', agent: 'test-analyzer' })], count: 0, primary: [] },
  { name: 'distant lines do not consolidate', rows: [f({ id: 'f1', line_start: 10 }), f({ id: 'f2', line_start: 100, agent: 'test-analyzer' })], count: 0, primary: [] },
  { name: 'null confidence loses to sixty', rows: [f({ id: 'f1', line_start: 20, dimension: 'test_coverage', agent: 'test-analyzer', confidence: null }), f({ id: 'f2', line_start: 21, dimension: 'simplification', agent: 'code-simplifier', confidence: 60 })], count: 2, primary: ['f2'] },
  { name: 'three agents retain all and core confidence wins', rows: [f({ id: 'sec-1', line_start: 20, dimension: 'security', agent: 'security-reviewer', confidence: 75 }), f({ id: 'bug-1', line_start: 21, confidence: 80 }), f({ id: 'test-1', line_start: 22, dimension: 'test_coverage', agent: 'test-analyzer', confidence: 95 })], count: 3, primary: ['bug-1'] },
  { name: 'intent core beats higher confidence noncore', rows: [f({ id: 'conv-1', line_start: 20, dimension: 'intent', agent: 'conventions-and-intent', confidence: 75 }), f({ id: 'test-1', line_start: 21, dimension: 'test_coverage', agent: 'test-analyzer', confidence: 90 })], count: 2, primary: ['conv-1'] },
  { name: 'different report routes still consolidate', rows: [f({ id: 'bug-2', file: 'AssertEvents.java', line_start: 483, report_destination: 'main', confidence: 95 }), f({ id: 'conv-2', file: 'AssertEvents.java', line_start: 483, dimension: 'comment_accuracy', agent: 'conventions-and-intent', report_destination: 'suggestion', confidence: 97 })], count: 2, primary: ['bug-2'] },
  { name: 'empty input has zero consolidated', rows: [], count: 0, primary: [] },
];
for (const c of CONSOLIDATION_CASES) test(`consolidate: ${c.name}`, () => {
  const out = consolidateCrossAgent(c.rows.map((row) => ({ ...row })));
  assert.equal(out.findings.length, c.rows.length);
  assert.equal(out.consolidatedCount, c.count);
  assert.deepEqual(out.findings.filter((row) => row.consolidation_primary).map((row) => row.id), c.primary);
  assert.equal(out.findings.filter((row) => Object.hasOwn(row, 'consolidation_key')).length, c.count);
});

const CONSOLIDATION_STATS_CASES = [
  { name: 'tagging returns consolidated count', run: (rows) => tagFindings(rows), count: (out) => out.consolidatedCount },
  { name: 'filter pipeline reports consolidated count', run: (rows) => applyFilterPipeline(rows, { confidence_threshold: 70 }, [], '2026-01-01T00:00:00Z'), count: (out) => out.stats.cross_agent_consolidated },
];
for (const c of CONSOLIDATION_STATS_CASES) test(`consolidation stats: ${c.name}`, () => {
  const rows = [f({ id: 'bug-1', description: 'A real wrong result in this function.' }), f({ id: 'test-1', line_start: 12, agent: 'test-analyzer', dimension: 'test_coverage', confidence: 80, description: 'Missing a test for a specific edge case.' })];
  const out = c.run(rows);
  assert.equal(c.count(out), 2);
  const active = out.tagged ?? out.filtered;
  assert.deepEqual(active.map((row) => row.id).sort(), ['bug-1', 'test-1']);
  assert.equal(active.some((row) => row.eliminated_by === 'dedup:cross-agent'), false);
});

// --- #73 req 4: mixed-origin array (A degraded, B/C verified) ---------------

test('#73: a degraded finding gets no consensus boost from a verified neighbor', () => {
  const a = f({ id: 'A', origin: 'unknown', confidence: 75, agent: 'bug-detector' });
  const b = f({ id: 'B', origin: 'verified', confidence: 80, agent: 'security-reviewer' });
  const { active } = detectDisagreement([a, b]);
  const byId = Object.fromEntries(active.map((x) => [x.id, x]));
  // A and B are co-located (same file+bucket) but different `degraded` groups
  // -> each is a singleton within its own group, not a 2-member consensus group.
  assert.equal(byId.A.consensus_count, 1);
  assert.equal(byId.B.consensus_count, 1);
  assert.deepEqual(byId.A.corroborated_by, []);
  assert.deepEqual(byId.B.corroborated_by, []);
});

test('#73: order is B > C > A after ranking (severity high all; B verified 80, C verified 74, A degraded 75)', () => {
  const a = f({ id: 'A', origin: 'unknown', confidence: 75, severity: 'high' });
  const b = f({ id: 'B', origin: 'verified', confidence: 80, severity: 'high' });
  const c = f({ id: 'C', origin: 'verified', confidence: 74, severity: 'high' });
  const ranked = rankFindings([a, b, c]);
  assert.deepEqual(ranked.map((x) => x.id), ['B', 'C', 'A']);
});

// --- #73 req 2: uniform-origin regression pins (captured before the change) -

test('#73 req 2: an all-verified run is unaffected by the degraded grouping key extension', () => {
  const findings = [
    f({ id: 'V1', origin: 'verified', file: 'a.py', line_start: 10, agent: 'bug-detector', confidence: 70 }),
    f({ id: 'V2', origin: 'verified', file: 'a.py', line_start: 11, agent: 'security-reviewer', confidence: 60 }),
  ];
  const { active, boostedCount } = detectDisagreement(findings);
  const byId = Object.fromEntries(active.map((x) => [x.id, x]));
  assert.equal(boostedCount, 2);
  assert.equal(byId.V1.consensus_count, 2);
  assert.equal(byId.V2.consensus_count, 2);
  assert.equal(byId.V1.confidence, 80); // 70 + CONSENSUS_BOOST(10)
  assert.equal(byId.V2.confidence, 70); // 60 + CONSENSUS_BOOST(10)
});

test('#73 req 2: an all-degraded run is unaffected by the degraded grouping key extension', () => {
  const findings = [
    f({ id: 'D1', origin: 'unknown', file: 'a.py', line_start: 10, agent: 'bug-detector', confidence: 70 }),
    f({ id: 'D2', origin: 'unknown', file: 'a.py', line_start: 11, agent: 'security-reviewer', confidence: 60 }),
  ];
  const { active, boostedCount } = detectDisagreement(findings);
  const byId = Object.fromEntries(active.map((x) => [x.id, x]));
  assert.equal(boostedCount, 2);
  assert.equal(byId.D1.consensus_count, 2);
  assert.equal(byId.D2.consensus_count, 2);
  assert.equal(byId.D1.confidence, 80);
  assert.equal(byId.D2.confidence, 70);
});

test('#73 req 2 rank: uniform origin is unaffected by the degraded rank component', () => {
  const findings = [
    f({ id: 'H1', origin: 'verified', severity: 'high', confidence: 60 }),
    f({ id: 'H2', origin: 'verified', severity: 'high', confidence: 90 }),
    f({ id: 'M1', origin: 'verified', severity: 'medium', confidence: 99 }),
  ];
  assert.deepEqual(rankFindings(findings).map((x) => x.id), ['H2', 'H1', 'M1']);
});

// --- D1: consolidateCrossAgent never drops -----------------------------------

test('cross-agent 5-line group: nothing eliminated; shared key; exactly one primary', () => {
  const bug = f({ id: 'bug-1', file: 'a.py', line_start: 10, agent: 'bug-detector', dimension: 'bug', confidence: 80 });
  const test1 = f({ id: 'test-1', file: 'a.py', line_start: 12, agent: 'test-analyzer', dimension: 'test_coverage', confidence: 95 });
  const { findings, consolidatedCount } = consolidateCrossAgent([bug, test1]);
  assert.equal(findings.length, 2); // nothing dropped
  assert.equal(consolidatedCount, 2);
  assert.equal(bug.consolidation_key, test1.consolidation_key);
  assert.equal(bug.consolidation_primary, true); // core dim wins
  assert.equal(test1.consolidation_primary, false);
});

test('same-agent group gets no stamps at all', () => {
  const f1 = f({ id: 'f1', file: 'a.py', line_start: 10, agent: 'bug-detector' });
  const f2 = f({ id: 'f2', file: 'a.py', line_start: 11, agent: 'bug-detector' });
  const { consolidatedCount } = consolidateCrossAgent([f1, f2]);
  assert.equal(consolidatedCount, 0);
  assert.equal('consolidation_key' in f1, false);
  assert.equal('consolidation_key' in f2, false);
});

test('singleton gets no stamps', () => {
  const only = f({ id: 'only-1' });
  const { consolidatedCount } = consolidateCrossAgent([only]);
  assert.equal(consolidatedCount, 0);
  assert.equal('consolidation_key' in only, false);
});

// The id-less member outranks the id-carrying one (core dimension AND higher
// confidence), so the primary walk has to step PAST it -- an id-less finding
// cannot carry the stamp.
test('findings without a truthy id pass through unstamped', () => {
  const noId = f({ id: undefined, file: 'a.py', line_start: 10, agent: 'bug-detector', dimension: 'bug', confidence: 95 });
  const withId = f({ id: 'has-id', file: 'a.py', line_start: 11, agent: 'test-analyzer', dimension: 'test_coverage', confidence: 50 });
  const { consolidatedCount } = consolidateCrossAgent([noId, withId]);
  assert.equal('consolidation_key' in noId, false);
  assert.equal('consolidation_primary' in noId, false);
  assert.equal(withId.consolidation_key, 'a.py:10');
  assert.equal(withId.consolidation_primary, true);
  assert.equal(consolidatedCount, 1);
});

// --- post-challenge path: zero dedup:cross-agent eliminations ---------------

test('applyChallenges: zero eliminated_by dedup:cross-agent; consolidation stamped on survivors', () => {
  const bug = f({ id: 'bug-1', file: 'a.py', line_start: 10, agent: 'bug-detector', dimension: 'bug', confidence: 80, severity: 'high' });
  const test1 = f({ id: 'test-1', file: 'a.py', line_start: 12, agent: 'test-analyzer', dimension: 'test_coverage', confidence: 95, severity: 'high' });
  const { findings, eliminated, stats } = applyChallenges([bug, test1], []);
  assert.equal(eliminated.some((e) => e.eliminated_by === 'dedup:cross-agent'), false);
  assert.equal(findings.length, 2);
  const byId = Object.fromEntries(findings.map((x) => [x.id, x]));
  assert.equal(byId['bug-1'].consolidation_key, byId['test-1'].consolidation_key);
  assert.equal(stats.cross_agent_consolidated, 2);
});

// --- stale stamp clearing: a re-run must not leave orphaned stamps ----------

test('a group that no longer qualifies after its primary is eliminated loses its stamps on survivors', () => {
  const bug = f({ id: 'bug-1', file: 'a.py', line_start: 10, agent: 'bug-detector', dimension: 'bug', confidence: 95, severity: 'high' });
  const test1 = f({ id: 'test-1', file: 'a.py', line_start: 12, agent: 'test-analyzer', dimension: 'test_coverage', confidence: 60, severity: 'high' });
  // Simulate the filter stage's earlier stamping pass.
  consolidateCrossAgent([bug, test1]);
  assert.equal(bug.consolidation_primary, true);
  assert.equal(test1.consolidation_key, bug.consolidation_key);

  // Challenge eliminates the primary (bug-1); test-1 survives alone -> the
  // group no longer has 2+ distinct agents, so the re-run must clear test-1's
  // stale stamps rather than leave it pointing at a vanished primary.
  const challenges = [{ id: 'bug-1', score: 10 }, { id: 'test-1', score: 90 }];
  const { findings } = applyChallenges([bug, test1], challenges);
  assert.equal(findings.length, 1);
  const survivor = findings[0];
  assert.equal(survivor.id, 'test-1');
  assert.equal('consolidation_key' in survivor, false, 'stale stamp must be cleared');
  assert.equal('consolidation_primary' in survivor, false, 'stale stamp must be cleared');
});
