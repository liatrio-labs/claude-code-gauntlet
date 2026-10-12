import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runWith } from '../src/run.js';
import { makeCtx, validArgs } from './helpers/pipelineMock.js';

const UNMEASURED_GAP = 'context_unmeasured: args.contextLines was not stamped, so the shared-context read plan could not be computed '
  + '\u2014 every agent got the count-free "read until a Read returns no further content" wording instead of the exact '
  + 'Read calls covering the file. That restores the failure mode of issue #48: a Read returns part of a large file '
  + 'with no truncation notice, and an agent that stops there reviews only what it saw. Stamp contextLines '
  + '(and contextChars) from the Phase 2 step that writes the context file.';
const UNPLANNABLE_GAP = 'context_unplannable: args.contextLines was stamped (1500001) but exceeds the read-plan chunk ceiling, '
  + 'so the shared-context read plan could not be built \u2014 every agent got the count-free '
  + '"read until a Read returns no further content" wording instead of the exact Read calls covering the file. '
  + 'That restores the failure mode of issue #48 for an oversized context. Shrink the shared context '
  + '(or raise READ_PLAN_MAX_CHUNKS with a matching prompt-size budget) so a plan can be computed.';
const CONTEXT_RUN_ROWS = [
  { name: 'measured plan reaches every reader', input: { contextLines: 2028, contextChars: 94784 }, gaps: [],
    markers: ['exactly 4 Read calls', 'Read(offset=1924, limit=105)'] },
  { name: 'unmeasured has one actionable gap and fallback', input: {}, gaps: [UNMEASURED_GAP],
    markers: ['until a call returns no further content', 'Do not stop before that'], fallback: true },
  { name: 'unplannable measurement has its own gap and fallback', input: { contextLines: 1500001 },
    gaps: [UNPLANNABLE_GAP], markers: ['until a call returns no further content', 'Do not stop before that'], fallback: true },
];
for (const row of CONTEXT_RUN_ROWS) test(`context run: ${row.name}`, async () => {
  const args = validArgs(row.input);
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.notEqual(out.status, 'args-invalid');
  assert.deepEqual(out.gaps.filter((gap) => /context_unmeasured|context_unplannable/.test(gap)), row.gaps);
  const contextPath = '/repo/.code-gauntlet/code-gauntlet-context-abc1234.md';
  const naming = ctx.calls.filter((call) => call.prompt.includes(contextPath));
  assert.ok(naming.length >= 9);
  assert.deepEqual(naming.map((call) => call.label), [
    'summarize', 'code-gauntlet:bug-detector', 'code-gauntlet:security-reviewer', 'code-gauntlet:cross-file-impact',
    'code-gauntlet:test-analyzer', 'code-gauntlet:conventions-and-intent', 'code-gauntlet:type-design-analyzer',
    'code-gauntlet:code-simplifier', 'validate-batch-0',
  ]);
  for (const call of naming) {
    assert.ok(call.prompt.includes('NO truncation notice'));
    for (const marker of row.markers) assert.ok(call.prompt.includes(marker), call.label);
    if (row.fallback) assert.doesNotMatch(call.prompt, /It is \d+ lines|undefined|NaN/);
  }
  for (const call of ctx.calls.filter((call) => call.label === 'artifact-writer' || call.label.startsWith('challenge-'))) {
    assert.doesNotMatch(call.prompt, /Read the shared context/);
    assert.equal(call.prompt.includes(contextPath), false);
  }
  assert.deepEqual(ctx.violations, []);
});
