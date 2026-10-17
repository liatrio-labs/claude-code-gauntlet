import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DIMENSIONS } from '../src/registry.js';
import { WEB_ORIGIN_RE, resolvePolicy, modelFor } from '../src/registry.js';

const POLICY_ROWS = [
  { name: 'mapped first-party dimension', agent: 'code-gauntlet:bug-detector',
    input: { provider: 'firstParty', mappedAliases: ['sonnet'] }, expected: { model: 'sonnet' } },
  { name: 'unmapped first-party dimension', agent: 'code-gauntlet:security-reviewer',
    input: { provider: 'firstParty', mappedAliases: ['sonnet'] }, expected: { model: 'claude-opus-5-5' } },
  { name: 'mapped default provider stage', agent: 'code-gauntlet:executor',
    input: { mappedAliases: ['sonnet'] }, expected: { model: 'sonnet' } },
  { name: 'mapped bare override', agent: 'code-gauntlet:security-reviewer',
    input: { subagentModelEnv: 'sonnet', mappedAliases: ['sonnet'] }, expected: { model: 'sonnet' } },
  { name: 'mapped explicit deployment override', agent: 'code-gauntlet:security-reviewer',
    input: { subagentModelEnv: 'deployment-id', mappedAliases: ['sonnet'] }, expected: { model: 'deployment-id' } },
  { name: 'third-party alias override', agent: 'code-gauntlet:bug-detector',
    input: { provider: 'bedrock', subagentModelEnv: 'sonnet' }, expected: { model: 'sonnet' } },
  { name: 'vertex stage default', agent: 'code-gauntlet:executor', input: { provider: 'vertex' },
    expected: { model: 'sonnet' } },
  { name: 'copied dimension', agent: 'code-gauntlet:bug-detector', input: {},
    table: DIMENSIONS.map((row) => ({ ...row, modelOverride: row.dimension === 'bug' ? 'haiku' : row.modelOverride })),
    expected: { model: 'claude-haiku-4-5-20251001' } },
];
for (const { name, agent, input, table, expected } of POLICY_ROWS) {
  test(`policy: ${name}`, () => {
    assert.deepEqual(resolvePolicy(agent, input, table), expected);
    assert.equal(modelFor(agent, { ...input, subagentModel: input?.subagentModelEnv }, table), expected.model);
  });
}

const PATTERN_ROWS = [
  { name: 'origin port bound', pattern: WEB_ORIGIN_RE, input: 'https://git.example:65535', expected: true },
  { name: 'origin port overflow', pattern: WEB_ORIGIN_RE, input: 'https://git.example:65536', expected: false },
];
for (const { name, pattern, input, expected } of PATTERN_ROWS) {
  test(`pattern: ${name}`, () => assert.equal(pattern.test(input), expected));
}
