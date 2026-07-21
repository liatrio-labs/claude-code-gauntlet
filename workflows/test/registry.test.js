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
test('report-writer / artifact-writer suffixes bind to STAGE_DEFAULTS (not the bare "report" key)', () => {
  // The split(':').pop() suffix is the full 'report-writer'/'artifact-writer', so the
  // tunable must be keyed by that or it silently never binds. Both resolve to sonnet.
  assert.equal(resolvePolicy('deep-review:report-writer', {}).model, 'sonnet');
  assert.equal(resolvePolicy('deep-review:artifact-writer', {}).model, 'sonnet');
});

// S7 model bump: security-reviewer's agent frontmatter says opus; assert the registry's
// modelOverride actually binds through resolvePolicy AND that security-reviewer is the ONLY
// discovery agent bumped off the sonnet default (the deviation Task 8 review confirmed).
test('S7: resolvePolicy routes security-reviewer to opus, the sole opus discovery agent', () => {
  assert.equal(DIMENSIONS.find((d) => d.dimension === 'security').modelOverride, 'opus');
  assert.equal(resolvePolicy('deep-review:security-reviewer', {}).model, 'opus');
  const opusAgents = AGENTS.filter((a) => resolvePolicy(a, {}).model === 'opus');
  assert.deepEqual(opusAgents, ['deep-review:security-reviewer']);
});

// Hill-climb iter 5 (discovery breadth): per-agent promptExtra sweeps live in the registry.
test('promptExtra: security sweep on security-reviewer, typo/naming on bug + conventions, none elsewhere', () => {
  const byDim = (dim) => DIMENSIONS.find((d) => d.dimension === dim);
  assert.match(byDim('security').promptExtra, /SSRF/);
  assert.match(byDim('bug').promptExtra, /typo and naming sweep/);
  // conventions-and-intent is multi-dimension; every one of its rows must carry the SAME
  // value (agentSpecs unions them, so a mismatch would be iteration-order-dependent).
  const convRows = DIMENSIONS.filter((d) => d.agentType === 'deep-review:conventions-and-intent');
  assert.ok(convRows.every((d) => d.promptExtra === byDim('convention').promptExtra));
  assert.match(byDim('convention').promptExtra, /typo and naming sweep/);
  // bug-detector and conventions share the one typo/naming sweep string.
  assert.equal(byDim('bug').promptExtra, byDim('intent').promptExtra);
  // Agents without a sweep carry null.
  for (const dim of ['cross_file_impact', 'test_coverage', 'type_design', 'simplification']) {
    assert.equal(byDim(dim).promptExtra, null);
  }
});
