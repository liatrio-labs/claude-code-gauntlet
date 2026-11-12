import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateStage } from '../src/reviewStages.js';
import { runAndCapture, replayProducedCheckpoint } from './helpers/persistFixtures.js';
import { validArgs } from './helpers/argsFixtures.js';
import { stageCtx } from './helpers/stageFixtures.js';
import { makeFinding } from './helpers/pipelineMock.js';

for (const [dimension, fields] of [
  ['test_coverage', { test_kind: 'additional_case' }], ['test_coverage', { test_kind: 'critical_gap' }],
]) test(`typed truth preserves deterministic kind admission: ${dimension}/${JSON.stringify(fields)}`, async () => {
  const ctx = stageCtx({ reply: { validations: [{ finding_id: 'f', claim: 'confirmed', consequence: 'present_failure',
    justification: 'Current caller fails' }] } });
  const validated = await validateStage(ctx, { findings: [makeFinding('f', { dimension, ...fields })] });
  const result = await runAndCapture(validArgs({ checkpoints: { validate: validated } }));
  assert.equal(result.payload.postReview.length, 0);
  assert.equal(result.payload.findings[0].admission.status, fields.test_kind === 'critical_gap' ? 'held_back' : 'report_only');
  assert.equal(ctx.calls.length, 1);
  const replay = await replayProducedCheckpoint(validArgs(), result.payload.checkpoints);
  assert.deepEqual(replay.payload.findings, result.payload.findings);
});
