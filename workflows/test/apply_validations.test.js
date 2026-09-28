import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyValidations } from '../src/applyValidations.js';
import { validateStage } from '../src/stages.js';
import { finding } from './helpers/findings.js';

const VALIDATION_CASES = [
  { name: 'unchanged confidence still saves original', findings: [finding({ confidence: 80 })], validations: [{ id: 'f1', confidence: 80 }], adjusted: 1, expected: { confidence: 80, original_confidence: 80, validator_confidence: 80 } },
  { name: 'unknown reachability ignored', findings: [finding()], validations: [{ id: 'f1', confidence: 72, reachability: 'later' }], adjusted: 1, absent: 'reachability' },
  { name: 'numeric reachability ignored', findings: [finding()], validations: [{ id: 'f1', confidence: 72, reachability: 42 }], adjusted: 1, absent: 'reachability' },
  { name: 'array reachability ignored', findings: [finding()], validations: [{ id: 'f1', confidence: 72, reachability: ['current'] }], adjusted: 1, absent: 'reachability' },
  { name: 'null reachability ignored', findings: [finding()], validations: [{ id: 'f1', confidence: 72, reachability: null }], adjusted: 1, absent: 'reachability' },
  { name: 'missing validation id skipped', findings: [finding({ confidence: 80 })], validations: [{ confidence: 50 }], adjusted: 0, expected: { confidence: 80 }, absent: 'original_confidence' },
  { name: 'missing validation confidence skipped', findings: [finding({ confidence: 80 })], validations: [{ id: 'f1' }], adjusted: 0, expected: { confidence: 80 }, absent: 'original_confidence' },
  { name: 'noninteger validation confidence skipped', findings: [finding({ confidence: 80 })], validations: [{ id: 'f1', confidence: 'high' }], adjusted: 0, expected: { confidence: 80 }, absent: 'original_confidence' },
  { name: 'empty findings reports unmatched id', findings: [], validations: [{ id: 'x', confidence: 50 }], adjusted: 0, unmatched: ['x'] },
  { name: 'missing finding confidence saves zero', findings: [{ id: 'f1', file: 'src/x.py', title: 'T' }], validations: [{ id: 'f1', confidence: 65 }], adjusted: 1, expected: { original_confidence: 0, confidence: 65 } },
];
for (const c of VALIDATION_CASES) test(`validation: ${c.name}`, () => {
  const findings = JSON.parse(JSON.stringify(c.findings));
  const result = applyValidations(findings, c.validations);
  assert.equal(result.adjustedCount, c.adjusted);
  assert.deepEqual(result.unmatchedIds, c.unmatched ?? []);
  for (const [key, value] of Object.entries(c.expected ?? {})) assert.deepEqual(findings[0][key], value);
  if (c.absent) assert.equal(Object.hasOwn(findings[0], c.absent), false);
});

const VALIDATE_STAGE_CASES = [
  { name: 'validations envelope adjusts matching finding', result: { validations: [{ id: 'f1', confidence: 85 }] }, expected: 85 },
];
for (const c of VALIDATE_STAGE_CASES) test(`validation stage: ${c.name}`, async () => {
  const ctx = { agent: async () => c.result, parallel: async (thunks) => Promise.all(thunks.map((thunk) => thunk())) };
  const out = await validateStage(ctx, { findings: [finding({ confidence: 40 })] });
  assert.equal(out.findings[0].confidence, c.expected);
  assert.equal(out.stats.adjusted, 1);
});
