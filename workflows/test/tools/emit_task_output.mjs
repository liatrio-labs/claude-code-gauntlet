// emit_task_output.mjs — run the WIRED pipeline (runWith) on the RETURN persist channel
// and write a task-output file shaped exactly as the harness writes one: the Workflow
// tool's `{ summary, agentCount, logs, result, workflowProgress, ... }` envelope,
// pretty-printed, with the compact return nested at `result`.
//
// Consumed by tests/test_materialize.py so the materializer is proven against
// REAL pipeline output rather than a hand-authored payload: if a stage stops emitting a
// field, or writeArtifacts stops carrying the primaries home, the Python side sees it.
//
import { writeFileSync } from 'node:fs';
import { runWith } from '../../src/run.js';
import { validArgs } from '../helpers/argsFixtures.js';
import { makeCtx, makeFinding } from '../helpers/pipelineMock.js';
import { hardenEscapeRuns } from '../../src/persistPlan.js';
import { decodeMeasurement } from '../helpers/decodeMeasurement.js';
import { RETURN_CHAR_BUDGET } from '../../src/wire.js';
import assert from 'node:assert/strict';

const [outPath, outputDir, nonce, size, fractional] = process.argv.slice(2);
if (!outPath || !outputDir) {
  console.error('usage: node emit_task_output.mjs <out.output> <outputDir> [nonce] [size] [fractional]');
  process.exit(2);
}

// Quotes, backslashes, astral characters and long prose require byte preservation:
// schema validation alone cannot detect transcription loss or summarization.
const NASTY = 'the executor wrote \\"receipt\\" to C:\\tmp\\out — 😀 𝕏 — '
  + 'and then went on at length: '.repeat(40);

const count = Number(size || 2);
const large = count > 2;
const prose = (label, length, index) => (`${label} ${index}: ${NASTY}`).repeat(Math.ceil(length / NASTY.length)).slice(0, length);
const findings = large ? Array.from({ length: count }, (_, index) => {
  const validator = { claim: 'confirmed', consequence: 'present_failure', justification: prose('validation', 365, index),
    comment: { title: `Finding ${index}`, body: prose('reader explanation', 550, index) } };
  const challenger = { ...validator, justification: prose('challenge', 960, index) };
  return makeFinding(`F${index}`, { description: prose('description', 500, index), evidence: prose('evidence', 350, index),
    suggestion: prose('suggestion', 350, index),
    validator_verdict: validator, challenge_verdict: challenger, verdict: challenger,
    validation_justification: validator.justification, challenge_justification: challenger.justification,
    ...(fractional ? { criticality: 0.75 } : {}) });
}) : [
  makeFinding('F1', { description: NASTY, evidence: 'line 1\nline 2\t"quoted"' }),
  makeFinding('F2', { description: `${NASTY} second`, code: 'const re = /\\d+\\\\/g;' }),
];
const args = validArgs({
  outputDir,
  nonce: nonce || 'nonce-return-channel',
  persist: { assembleScriptPath: '/plugin/scripts/assemble_artifacts.py', returnPrimaries: true },
  ...(large ? { measurementTrace: true, checkpoints: { challenge: {
    findings, unverified: [], eliminated: [], gaps: [], stats: {},
  } } } : {}),
});
const ctx = makeCtx(args, {
  reportText: `# Code Gauntlet\n\n${NASTY}\n\n- one\n- two\n`,
  findings: large ? undefined : findings,
});
const oracleArgs = JSON.parse(JSON.stringify(args));
delete oracleArgs.persist;
delete oracleArgs.measurementTrace;
const result = await runWith(ctx, args);

if (!result.ok || !result.persistReturn) {
  console.error(`pipeline did not return its primaries (ok=${result.ok})`);
  process.exit(1);
}

assert.equal(ctx.calls.some(({ label }) => /artifact-writer|assemble-artifacts/.test(label)), false);
if (large) {
  assert.equal(JSON.parse(result.persistReturn.entries[0].text).length, count);
  assert.ok(JSON.stringify(result).length > RETURN_CHAR_BUDGET);
  assert.equal(result.measurement.status, 'available');
  assert.ok(JSON.stringify(result.measurement).length <= RETURN_CHAR_BUDGET);
  assert.deepEqual(decodeMeasurement(result.measurement).dropped_stages, []);
  assert.deepEqual(result.measurement.replay, { status: 'unavailable', reason: 'return_budget' });
  assert.ok(result.gaps.some((gap) => /delivery replay not persisted.*return budget/.test(gap)));
  if (!fractional) assert.ok(result.persistReturn.entries[2].text.length < count * 1000,
    'the persist plan must reference finding fields rather than repeat their prose');
}

// A separate by-value run provides the bytes before any return-plan projection.
// Neither the materializer nor the new projection implementation supplies this oracle.
let held;
const oracle = await runWith(makeCtx(oracleArgs, { findings: large ? undefined : findings,
  onPersist: (payload) => { held = payload; } }), oracleArgs);
assert.equal(oracle.ok, true, oracle.error);
writeFileSync(`${outPath}.expected.json`, JSON.stringify([
  hardenEscapeRuns(JSON.stringify(held.findings, null, 2)), held.report,
  JSON.stringify(held.postReview, null, 2), JSON.stringify(held.checkpoints, null, 2),
]));

// The envelope the Workflow tool writes, pretty-printed as it writes it. The nesting is
// load-bearing for the test: await_workflow.py's terminal detection has to find the
// compact return at `result` and refuse the sibling receipts around it.
writeFileSync(outPath, JSON.stringify({
  summary: 'code-gauntlet pipeline run',
  agentCount: ctx.calls.length,
  logs: [],
  result,
  workflowProgress: [
    { label: 'artifact-writer', ok: true, phaseReached: 'never dispatched on this channel' },
  ],
  totalTokens: 0,
  totalToolCalls: 0,
}, null, 2));
