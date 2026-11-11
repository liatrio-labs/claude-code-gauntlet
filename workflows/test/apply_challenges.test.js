import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyChallenges } from '../src/applyChallenges.js';
import { finding } from './helpers/findings.js';

test('challenge retains caller objects and consolidation while changing typed consequence', () => {
  const original = finding({ consolidation_key: 'x:10', consolidation_primary: true });
  const before = JSON.parse(JSON.stringify(original));
  const out = applyChallenges([original], [{ id: original.id, claim: 'confirmed', consequence: 'latent',
    reachability: 'future_change_only', justification: 'Future caller required', latent_condition: 'Add a caller' }]);
  assert.deepEqual(original, before);
  assert.equal(out.findings[0].verdict_route, 'posted-latent');
  assert.equal(out.findings[0].consolidation_key, 'x:10');
  assert.equal(out.findings[0].severity, 'low');
});
