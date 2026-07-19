// filter_unit.test.js — pure JS-side unit tests for filterFindings.js that
// have no Python twin to record parity against (banker's-rounding trap,
// determinism invariants). Parity-backed behavior lives in parity.test.js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pyRound, applyFilterPipeline } from '../src/filterFindings.js';

test('pyRound is banker\'s rounding (half-to-even)', () => {
  assert.equal(pyRound(2.5), 2); // 25/10 -> bucket 20, NOT 30
  assert.equal(pyRound(3.5), 4);
  assert.equal(pyRound(0.5), 0);
  assert.equal(pyRound(1.5), 2);
  assert.equal(pyRound(2.4), 2);
  assert.equal(pyRound(2.6), 3);
});

test('applyFilterPipeline stamps generated_at from the injected value, never a wall clock', () => {
  const cfg = { confidence_threshold: 70, security_min_confidence: 70, severity_threshold: 'low' };
  const out = applyFilterPipeline([], cfg, [], '2026-07-18T00:00:00Z');
  assert.equal(out.generated_at, '2026-07-18T00:00:00Z');
});

// Beyond the brief's pinned determinism check: a small end-to-end smoke test
// that exercises every applyFilterPipeline stage in composition (threshold ->
// exclusions -> injection -> disagreement -> tag), since no golden-fixture
// coverage exists for the composed pipeline itself (only its sub-functions,
// each individually proven at parity). Asserts internal bookkeeping
// consistency rather than exact values, to stay robust to the sub-function
// behavior already pinned elsewhere.
test('applyFilterPipeline composes stages consistently on a small mixed batch', () => {
  const cfg = { confidence_threshold: 50, security_min_confidence: 50, severity_threshold: 'low', ignore: [] };
  const findings = [
    { id: 'P1', agent: 'bug-detector', dimension: 'bug', file: 'x.py', line_start: 10, severity: 'high', confidence: 80, title: 'real bug', description: 'a genuine null pointer dereference on the error path' },
    { id: 'P2', agent: 'security-reviewer', dimension: 'security', file: 'x.py', line_start: 11, severity: 'high', confidence: 60, title: 'possible injection', description: 'user input reaches a raw SQL query without parameterization' },
    { id: 'P3', agent: 'code-simplifier', dimension: 'convention', file: 'y.py', line_start: 50, severity: 'low', confidence: 20, title: 'style nit', description: 'prefer a list comprehension here for readability' },
  ];

  const out = applyFilterPipeline(findings, cfg, [], '2026-07-18T00:00:00Z');

  assert.equal(out.generated_at, '2026-07-18T00:00:00Z');
  assert.equal(out.stats.total, 3);
  // P3 falls below the confidence threshold (20 < 50) and is eliminated there.
  assert.equal(out.stats.passed_threshold, 2);
  // Every input finding is accounted for exactly once across filtered+eliminated.
  assert.equal(out.filtered.length + out.eliminated.length, 3);
  // P1 and P2 share a proximity bucket with different agents -> consensus boost.
  assert.equal(out.stats.consensus_boosted, 2);
  assert.equal(out.stats.tagged_main + out.stats.tagged_suggestion, out.filtered.length);
});
