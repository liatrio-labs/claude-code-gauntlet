// registry.test.js — DIMENSIONS registry + resolvePolicy (S5) unit tests.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DIMENSIONS, AGENTS, resolvePolicy } from '../src/registry.js';

test('7 unique discovery agents', () => { assert.equal(AGENTS.length, 7); });
test('conventions-and-intent covers 3 dimensions', () => {
  const dims = DIMENSIONS.filter((d) => d.agentType === 'code-gauntlet:conventions-and-intent').map((d) => d.dimension);
  assert.deepEqual(dims.sort(), ['comment_accuracy', 'convention', 'intent']);
});
test('security-reviewer default is opus (S5 deviation)', () => {
  assert.equal(resolvePolicy('code-gauntlet:security-reviewer', {}).model, 'claude-opus-4-8');
});
test('discovery default is sonnet', () => {
  assert.equal(resolvePolicy('code-gauntlet:bug-detector', {}).model, 'claude-sonnet-5');
});
test('challenger resolves sonnet — the single benchmarked policy; no mode flag changes it (fable mode is roadmap #17 V3.2)', () => {
  assert.equal(resolvePolicy('code-gauntlet:challenger', {}).model, 'claude-sonnet-5');
  // A stray legacy frontier flag in opts must be ignored, not resurrect an upgrade path.
  assert.equal(resolvePolicy('code-gauntlet:challenger', { frontier: true, frontierModelId: 'claude-fable-5' }).model, 'claude-sonnet-5');
});
test('CLAUDE_CODE_SUBAGENT_MODEL overrides everything and is flagged', () => {
  const r = resolvePolicy('code-gauntlet:bug-detector', { subagentModelEnv: 'claude-haiku-4-5' });
  assert.equal(r.model, 'claude-haiku-4-5');
  assert.match(r.note, /CLAUDE_CODE_SUBAGENT_MODEL/);
});
test('report-writer / artifact-writer suffixes bind to STAGE_DEFAULTS (not the bare "report" key)', () => {
  // The split(':').pop() suffix is the full 'report-writer'/'artifact-writer', so the
  // tunable must be keyed by that or it silently never binds. Both resolve to sonnet.
  assert.equal(resolvePolicy('code-gauntlet:report-writer', {}).model, 'claude-sonnet-5');
  assert.equal(resolvePolicy('code-gauntlet:artifact-writer', {}).model, 'claude-sonnet-5');
});

// V3.1 orchestrator-model waist: resolvePolicy pins explicit FULL model IDs so no agent
// pin can cascade the orchestrator session's model variant (measured on bench: a child
// session pinned to 'sonnet[1m]' cascaded the [1m] variant into every agent whose policy
// said the bare alias 'sonnet' — zero plain-sonnet rows in the per-model usage table).
test('resolvePolicy pins full model IDs — no bare aliases can cascade a session variant', () => {
  assert.equal(resolvePolicy('code-gauntlet:bug-detector').model, 'claude-sonnet-5');
  assert.equal(resolvePolicy('code-gauntlet:security-reviewer').model, 'claude-opus-4-8');
  assert.equal(resolvePolicy('code-gauntlet:executor').model, 'claude-sonnet-5');
});
test('subagentModelEnv override maps through the same full-ID pin', () => {
  // A bare alias in CLAUDE_CODE_SUBAGENT_MODEL now pins the plain full ID instead of
  // inheriting the session variant (intended behavior change, documented in headless-mode);
  // an explicit full/dated ID passes through untouched.
  assert.equal(resolvePolicy('code-gauntlet:bug-detector', { subagentModelEnv: 'sonnet' }).model, 'claude-sonnet-5');
  assert.equal(resolvePolicy('code-gauntlet:bug-detector', { subagentModelEnv: 'claude-haiku-4-5-20251001' }).model, 'claude-haiku-4-5-20251001');
});

// S7 model bump: security-reviewer's agent frontmatter says opus; assert the registry's
// modelOverride actually binds through resolvePolicy AND that security-reviewer is the ONLY
// discovery agent bumped off the sonnet default (the deviation Task 8 review confirmed).
test('S7: resolvePolicy routes security-reviewer to opus, the sole opus discovery agent', () => {
  assert.equal(DIMENSIONS.find((d) => d.dimension === 'security').modelOverride, 'opus');
  assert.equal(resolvePolicy('code-gauntlet:security-reviewer', {}).model, 'claude-opus-4-8');
  const opusAgents = AGENTS.filter((a) => resolvePolicy(a, {}).model === 'claude-opus-4-8');
  assert.deepEqual(opusAgents, ['code-gauntlet:security-reviewer']);
});

// Hill-climb iter 5 (discovery breadth): per-agent promptExtra sweeps live in the registry.
test('promptExtra: security sweep on security-reviewer, typo/naming on bug + conventions, none elsewhere', () => {
  const byDim = (dim) => DIMENSIONS.find((d) => d.dimension === dim);
  assert.match(byDim('security').promptExtra, /SSRF/);
  assert.match(byDim('bug').promptExtra, /typo and naming sweep/);
  // conventions-and-intent is multi-dimension; every one of its rows must carry the SAME
  // value (agentSpecs unions them, so a mismatch would be iteration-order-dependent).
  const convRows = DIMENSIONS.filter((d) => d.agentType === 'code-gauntlet:conventions-and-intent');
  assert.ok(convRows.every((d) => d.promptExtra === byDim('convention').promptExtra));
  assert.match(byDim('convention').promptExtra, /typo and naming sweep/);
  // bug-detector and conventions share the one typo/naming sweep string.
  assert.equal(byDim('bug').promptExtra, byDim('intent').promptExtra);
  // Agents without a sweep carry null.
  for (const dim of ['cross_file_impact', 'test_coverage', 'type_design', 'simplification']) {
    assert.equal(byDim(dim).promptExtra, null);
  }
});
