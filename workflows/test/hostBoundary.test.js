import { test } from 'node:test';
import assert from 'node:assert/strict';
import { stageCtx } from './helpers/stageFixtures.js';
import { verifyCtx } from './helpers/verifyDelta.js';
import { makeCtx } from './helpers/pipelineMock.js';
import { persistCtx } from './helpers/persistFixtures.js';

for (const [name, ctx] of [
  ['stages', stageCtx()], ['verify', verifyCtx(() => null)],
  ['pipeline', makeCtx({})], ['persist', persistCtx()],
]) test(`${name} parallel copies each thunk result and loses collections and prototypes`, async () => {
  class Record {
    constructor() { this.ids = ['F1']; }
    includes(id) { return this.ids.includes(id); }
  }
  const result = await ctx.parallel([
    () => ({ set: new Set(['F1']), map: new Map([['F1', true]]), record: new Record() }),
  ]);
  assert.deepEqual(result, [{ set: {}, map: {}, record: { ids: ['F1'] } }]);
});
