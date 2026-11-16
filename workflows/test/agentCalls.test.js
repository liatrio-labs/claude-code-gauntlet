import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { AGENT_CALLS, toolSchemaErrors, requestFailureGaps } from '../src/agentCalls.js';
import { runWith } from '../src/run.js';
import { validArgs } from './helpers/argsFixtures.js';
import { makeCtx, makeFinding } from './helpers/pipelineMock.js';

const sourceRoot = new URL('../src/', import.meta.url);
const modules = readdirSync(sourceRoot).filter((name) => name.endsWith('.js'));
for (const name of modules.filter((name) => name !== 'pipeline_entry.js')) await import(new URL(name, sourceRoot));

test('every agent dispatch uses a registered, API-compatible object root', () => {
  // A direct dispatch would escape the inventory and must fail the same audit.
  for (const name of modules.filter((name) => name !== 'agentCalls.js')) {
    assert.doesNotMatch(readFileSync(new URL(name, sourceRoot), 'utf8'), /\b(?:agent\s*\(|\w+\.agent\s*\()/, name);
  }
  assert.ok(AGENT_CALLS.length);
  for (const call of AGENT_CALLS) for (const inputs of call.variants()) {
    assert.deepEqual(toolSchemaErrors(call.schemaFor(...inputs)), [], call.name);
  }
});

for (const variation of ['same request error', 'different request errors', 'ordinary failures', 'one successful call']) {
  test(`stage gaps and trace distinguish ${variation}`, async () => {
    const error = 'API Error: 400 tools.1.custom.input_schema: input_schema does not support oneOf, allOf, or anyOf at the top level';
    const args = validArgs({ measurementTrace: true, limits: { challengeCap: 200, deliveryCap: 200 } });
    let payload;
    const findings = Array.from({ length: 109 }, (_, i) => makeFinding(`BUG_${String(i).padStart(3, '0')}`));
    const ctx = makeCtx(args, { findings, onPersist: (value) => { payload = value; } });
    const original = ctx.agent;
    let calls = 0;
    ctx.agent = async (prompt, options) => {
      if (options.agentType === 'code-gauntlet:challenger') {
        calls++;
        if (variation === 'one successful call' && calls === 1) return original(prompt, options);
        throw new Error(variation === 'ordinary failures' ? 'Temporary connection failure'
          : variation === 'different request errors' ? `${error} (request ${calls})` : error);
      }
      return original(prompt, options);
    };
    const out = await runWith(ctx, args);
    assert.equal(out.ok, true, JSON.stringify(out.gaps));
    assert.equal(calls, 109);
    const snapshot = JSON.parse(out.measurement.text).snapshots.find((row) => row.stage === 'challenge');
    const stageGaps = out.gaps.filter((gap) => gap.startsWith('challenge'));
    if (variation === 'same request error') {
      const gap = `challenge: all 109 agent requests failed with the same request error: ${error}`;
      assert.equal(stageGaps.length, 1);
      assert.equal(stageGaps.at(-1), gap);
      assert.deepEqual(snapshot.gaps, stageGaps);
      assert.equal(snapshot.coverage, 'partial');
      assert.ok(payload.report.includes(error), payload.report.slice(-2000));
      assert.equal(payload.findings.filter((f) => f.challenge === 'failed').length, 109);
    } else {
      assert.equal(stageGaps.some((gap) => gap.includes('same request error')), false);
      assert.equal(stageGaps.length, variation === 'one successful call' ? 108 : 109);
    }
  });
}

test('aggregate request failures retain unrelated stage gaps', () => {
  assert.deepEqual(requestFailureGaps(['challenge-0: no answer', 'challenge-1: no answer', 'code-gauntlet:validator: no answer', 'independent: missing context'],
    [{ stage: 'challenge', label: 'challenge-0' }, { stage: 'challenge', label: 'challenge-1' }, { stage: 'challenge', label: 'code-gauntlet:validator' }], 'challenge', 'challenge: request error'),
  ['independent: missing context', 'challenge: request error']);
});
