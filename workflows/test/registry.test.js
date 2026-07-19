// registry.test.js — DIMENSIONS registry + resolvePolicy (S5) unit tests.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DIMENSIONS, AGENTS, resolvePolicy } from '../src/registry.js';

test('7 unique discovery agents', () => { assert.equal(AGENTS.length, 7); });
test('conventions-and-intent covers 3 dimensions', () => {
  const dims = DIMENSIONS.filter((d) => d.agentType === 'deep-review:conventions-and-intent').map((d) => d.dimension);
  assert.deepEqual(dims.sort(), ['comment_accuracy', 'convention', 'intent']);
});
test('security-reviewer default is opus (S5 deviation)', () => {
  assert.equal(resolvePolicy('deep-review:security-reviewer', {}).model, 'opus');
});
test('discovery default is sonnet', () => {
  assert.equal(resolvePolicy('deep-review:bug-detector', {}).model, 'sonnet');
});
test('frontier upgrades challenger to the full model-id string (Fable alias unconfirmed)', () => {
  const r = resolvePolicy('deep-review:challenger', { frontier: true, frontierModelId: 'claude-fable-5' });
  assert.equal(r.model, 'claude-fable-5');
});
test('CLAUDE_CODE_SUBAGENT_MODEL overrides everything and is flagged', () => {
  const r = resolvePolicy('deep-review:bug-detector', { subagentModelEnv: 'claude-haiku-4-5' });
  assert.equal(r.model, 'claude-haiku-4-5');
  assert.match(r.note, /CLAUDE_CODE_SUBAGENT_MODEL/);
});
