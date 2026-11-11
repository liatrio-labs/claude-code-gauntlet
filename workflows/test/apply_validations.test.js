import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyValidations } from '../src/applyValidations.js';
import { finding } from './helpers/findings.js';

for (const confirmed of [true, false]) test(`duplicate site confirmations cannot grant placement: ${confirmed}`, () => {
  const site = { file: 'extra', line_start: 1, line_end: 1, anchor: 'return value;', location_status: 'confirmed' };
  const findings = [finding({ resolved_sites: [site] })];
  applyValidations(findings, [{ id: 'f1', claim: 'confirmed', consequence: 'present_failure', justification: 'Caller fails', site_confirmations: [
    { ...site, confirmed: true }, { ...site, confirmed },
  ] }]);
  assert.deepEqual(findings[0].sites, []);
});
