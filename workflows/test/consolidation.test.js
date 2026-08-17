// consolidation.test.js — issue #22 D1/D3: consolidateCrossAgent (never drops), and the
// origin-aware extensions to detectDisagreement's consensus grouping and rankFindings'
// ranking. Parity-backed golden fixtures live under tests/fixtures/parity/; this file
// covers the JS-only regression pins and the mixed-origin behavior called out in #73's
// evidence block, which has no Python-recorded fixture of its own (the same #73 A/B/C
// finding shapes are asserted directly here, mirroring the brief's TDD list verbatim).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { consolidateCrossAgent, detectDisagreement } from '../src/filterFindings.js';
import { applyChallenges, rankFindings } from '../src/applyChallenges.js';

function f(over) {
  return { id: 'x', file: 'a.py', line_start: 10, agent: 'bug-detector', dimension: 'bug', severity: 'high', confidence: 70, title: 't', description: 'd', ...over };
}

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

test('findings without a truthy id pass through unstamped', () => {
  const noId = f({ id: undefined, file: 'a.py', line_start: 10, agent: 'bug-detector' });
  const withId = f({ id: 'has-id', file: 'a.py', line_start: 11, agent: 'test-analyzer' });
  const { consolidatedCount } = consolidateCrossAgent([noId, withId]);
  assert.equal('consolidation_key' in noId, false);
  assert.equal(withId.consolidation_key, 'a.py:10');
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
