import { test } from 'node:test';
import assert from 'node:assert/strict';
import { selectDelivery } from '../src/reviewStages.js';
import { finding } from './helpers/findings.js';
const MIXED = [
  finding({ id: 'SUGG_LOW', severity: 'low', report_tag: 'suggestion', report_destination: 'suggestion' }),
  finding({ id: 'MAIN_MED', severity: 'medium', report_tag: 'main', report_destination: 'main' }),
  finding({ id: 'SUGG_HIGH', severity: 'high', report_tag: 'suggestion', report_destination: 'suggestion' }),
  finding({ id: 'MAIN_CRIT', severity: 'critical', report_tag: 'main', report_destination: 'main' }),
];
const DELIVERY_GOLDEN = ['MAIN_CRIT', 'SUGG_HIGH', 'MAIN_MED', 'SUGG_LOW'];
const DELIVERY_ROWS = [
  { name: 'critical corroborator ranks its group above a medium lead', cap: 1, input: [
    finding({ id: 'OTHER', severity: 'medium', confidence: 99 }),
    finding({ id: 'LEAD', severity: 'medium', confidence: 90, consolidation_key: 'issue', consolidation_primary: true }),
    finding({ id: 'CRITICAL', severity: 'critical', confidence: 80, consolidation_key: 'issue' }),
  ], expected: ['LEAD', 'CRITICAL'] },
  { name: 'main only keeps every member of a main group', tier: 'main_only', input: [
    finding({ id: 'SUGG_LEAD', report_tag: 'suggestion', consolidation_key: 'one', consolidation_primary: true }),
    finding({ id: 'MAIN_CHILD', report_tag: 'main', consolidation_key: 'one' }),
    finding({ id: 'MAIN_LEAD', report_tag: 'main', consolidation_key: 'two', consolidation_primary: true }),
    finding({ id: 'SUGG_CHILD', report_tag: 'suggestion', consolidation_key: 'two' }),
    finding({ id: 'SUGG_ONLY', report_tag: 'suggestion', consolidation_key: 'three' }),
  ], expected: ['MAIN_LEAD', 'SUGG_CHILD', 'SUGG_LEAD', 'MAIN_CHILD'] },
  { name: 'body groups compete with inline groups under the cap', cap: 1, input: [
    finding({ id: 'INLINE', disposition: 'deliver', severity: 'low' }),
    finding({ id: 'BODY', disposition: 'body', severity: 'critical' }),
  ], expected: ['BODY'] },
  { name: 'zero cap includes body groups', cap: 0, input: [finding({ id: 'BODY', disposition: 'body' })], expected: [] },
  { name: 'persisted membership overrides legacy grouping', cap: 1, input: [
    finding({ id: 'CHILD', confidence: 99, consolidation_key: 'stale-child', consolidation_primary: true,
      delivery_group: { issue_key: 'current', lead_id: 'LEAD', finding_ids: ['LEAD', 'CHILD'] } }),
    finding({ id: 'LEAD', confidence: 95, consolidation_key: 'stale-lead', consolidation_primary: false,
      delivery_group: { issue_key: 'current', lead_id: 'LEAD', finding_ids: ['LEAD', 'CHILD'] } }),
    finding({ id: 'OTHER', confidence: 90 }),
  ], expected: ['LEAD', 'CHILD'] },
  { name: 'absent cap and tier' }, { name: 'null cap and tier', cap: null, tier: null },
  { name: 'nonbinding cap', cap: 25 },
  { name: 'negative cap', cap: -1, expected: [] },
  { name: 'zero cap', cap: 0, expected: [] }, { name: 'binding cap includes suggestion', cap: 2, expected: ['MAIN_CRIT', 'SUGG_HIGH'] },
  { name: 'main only', tier: 'main_only', expected: ['MAIN_CRIT', 'MAIN_MED'] },
  { name: 'main only cap filters before slicing', tier: 'main_only', cap: 2, expected: ['MAIN_CRIT', 'MAIN_MED'] },
  { name: 'main only cap', tier: 'main_only', cap: 1, expected: ['MAIN_CRIT'] },
  { name: 'destination fallback', tier: 'main_only', input: [
    finding({ id: 'D_MAIN', report_destination: 'main' }), finding({ id: 'D_SUGG', report_destination: 'suggestion' }),
  ], expected: ['D_MAIN'] },
  { name: 'severity and stable location ranking', input: [
    finding({ id: 'LOW', severity: 'low', confidence: 99 }), finding({ id: 'CRIT', severity: 'critical', confidence: 60 }),
    finding({ id: 'HIGH_A', confidence: 70 }), finding({ id: 'HIGH_B', confidence: 90 }),
  ], expected: ['CRIT', 'HIGH_A', 'HIGH_B', 'LOW'] },
  { name: 'off-enum winner retains original', cap: 1, input: [
    finding({ id: 'OFF_ENUM', severity: 'bogus', confidence: 95 }), finding({ id: 'LOW', severity: 'low', confidence: 80 }),
  ], expected: ['LOW'], severity: 'low' },
  { name: 'empty pool', input: [], cap: 5, expected: [] }, { name: 'absent pool', absent: true, cap: 5, expected: [] },
];
for (const row of DELIVERY_ROWS) test(`delivery selection: ${row.name}`, () => {
  const input = row.absent ? undefined : structuredClone(row.input ?? MIXED);
  const before = JSON.stringify(input);
  const out = selectDelivery(input, row.cap, row.tier);
  assert.deepEqual(out.map((f) => f.id), row.expected ?? DELIVERY_GOLDEN);
  assert.equal(JSON.stringify(input), before);
  for (const f of out) assert.strictEqual(f, input.find((original) => original.id === f.id));
  if (row.severity) assert.equal(out[0].severity, row.severity);
  if (out.some((f) => f.id === 'SUGG_HIGH')) assert.equal(out.find((f) => f.id === 'SUGG_HIGH').report_tag, 'suggestion');
});
