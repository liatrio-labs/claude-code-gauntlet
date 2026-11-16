import { test } from 'node:test';
import assert from 'node:assert/strict';
import { renderPublicSummary, renderReport } from '../src/renderReport.js';
import { finding } from './helpers/reportFixtures.js';

test('delivery issue identity overrides conflicting legacy keys in the PR index', () => {
  const stamp = { issue_key: 'group:A', lead_id: 'A', finding_ids: ['A', 'B'] };
  const a = finding('A', { delivery_group: stamp, consolidation_key: 'left' });
  const b = finding('B', { delivery_group: stamp, consolidation_key: 'right' });
  const input = { findings: [a, b], delivered: [b], deliveryCap: 1 };
  const body = renderPublicSummary(input).body;
  assert.match(body, /^Found 1 issue in this change: 1 high\./);
  assert.equal(body.split('\n').filter((line) => line.startsWith('- ')).length, 1);
  assert.match(body, /finding B/);
  assert.doesNotMatch(body, /not listed here/);
  const report = renderReport(input);
  assert.match(report, /finding A/);
  assert.match(report, /finding B/);
});

test('body issues are counted once and indexed only in the local report', () => {
  const body = finding('BODY', { disposition: 'body' });
  const inline = finding('INLINE');
  const held = finding('HELD', { disposition: 'held_back' });
  const input = { findings: [held, body, inline], delivered: [body, inline] };
  const summary = renderPublicSummary(input).body;
  assert.match(summary, /^Found 2 issues in this change: 2 high\./);
  assert.match(summary, /finding INLINE/);
  assert.doesNotMatch(summary, /finding BODY|finding HELD|not listed here|admission|pipeline/);
  const report = renderReport(input);
  const index = report.split('## Summary\n\n')[1].split('\n\n## Change Context')[0];
  assert.match(index, /finding BODY/);
  assert.match(index, /finding INLINE/);
});
