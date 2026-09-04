// emit_task_output.mjs — run the WIRED pipeline (runWith) on the RETURN persist channel
// and write a task-output file shaped exactly as the harness writes one: the Workflow
// tool's `{ summary, agentCount, logs, result, workflowProgress, ... }` envelope,
// pretty-printed, with the compact return nested at `result`.
//
// Consumed by tests/test_materialize_artifacts.py so the materializer is proven against
// REAL pipeline output rather than a hand-authored payload: if a stage stops emitting a
// field, or writeArtifacts stops carrying the primaries home, the Python side sees it.
//
// usage: node emit_task_output.mjs <out.output> <outputDir> [nonce]
import { writeFileSync } from 'node:fs';
import { runWith } from '../../src/stages.js';
import { validArgs, makeCtx, makeFinding } from '../helpers/pipelineMock.js';

const [outPath, outputDir, nonce] = process.argv.slice(2);
if (!outPath || !outputDir) {
  console.error('usage: node emit_task_output.mjs <out.output> <outputDir> [nonce]');
  process.exit(2);
}

// Content chosen to be exactly what a model transcriber loses: a literal backslash ahead
// of a quote (the run of three that collapsed 18 times out of 18 on the run that lost
// every artifact), astral-plane characters, embedded newlines, and prose long enough that
// summarizing it would be invisible to a schema check.
const NASTY = 'the executor wrote \\"receipt\\" to C:\\tmp\\out — 😀 𝕏 — '
  + 'and then went on at length: '.repeat(40);

const args = validArgs({
  outputDir,
  nonce: nonce || 'nonce-return-channel',
  persist: { assembleScriptPath: '/plugin/scripts/assemble_artifacts.py', returnPrimaries: true },
});
const ctx = makeCtx(args, {
  reportText: `# Code Gauntlet\n\n${NASTY}\n\n- one\n- two\n`,
  findings: [
    makeFinding('F1', { description: NASTY, evidence: 'line 1\nline 2\t"quoted"' }),
    makeFinding('F2', { description: `${NASTY} second`, code: 'const re = /\\d+\\\\/g;' }),
  ],
});
const result = await runWith(ctx, args);

if (!result.ok || !result.persistReturn) {
  console.error(`pipeline did not return its primaries (ok=${result.ok})`);
  process.exit(1);
}

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
