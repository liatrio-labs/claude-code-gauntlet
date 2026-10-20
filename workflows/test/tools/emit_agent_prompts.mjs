import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { measurementScenario, persistedScenario } from '../helpers/measurementScenario.js';
import { runWith } from '../../src/run.js';
import { agentSpecs } from '../../src/discover.js';

export async function emitAgentPrompts(bundlePath, mode = 'legacy', overrides = {}, options = {}) {
  const body = readFileSync(bundlePath, 'utf8').replace(/^export const meta.*$/m, '');
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const execute = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', body);
  const { args, ctx, state } = measurementScenario(mode, overrides, options);
  const out = await execute(args, ctx.agent, ctx.parallel, undefined);
  if (!out.ok) throw new Error(out.error);
  const calls = ctx.calls.filter((c) => !['artifact-writer', 'assemble-artifacts'].includes(c.label));
  return { prompts: calls.map((c) => c.prompt), schemas: calls.map((c) => JSON.stringify(c.schema)),
    labels: calls.map((c) => c.label), payload: persistedScenario(out, state), out, calls: ctx.calls };
}

// Capture the public caller so concatenated prompt fragments and persistence commands are included.
export async function emitLeakPrompts() {
  const surfaces = [];
  const variants = [
    ['legacy', 'legacy', {}, {}],
    ['derived', 'derived', {}, {}],
    ['bucketed', 'return', { changedLines: 900, changedFiles: Array.from({ length: 24 }, (_, i) => `sample${i}.js`) }, {}],
    ['fallback', 'return', {}, {}],
  ];
  for (const [scenario, mode, overrides, options] of variants) {
    const { args, ctx } = measurementScenario(mode, overrides, options);
    if (scenario === 'fallback') {
      const capture = ctx.agent;
      ctx.agent = async (prompt, dispatch) => {
        const result = await capture(prompt, dispatch);
        // A fractional metadata value exercises the serializer's legacy fallback.
        if (dispatch.agentType === 'code-gauntlet:bug-detector') result.findings[0].sampleRatio = 0.5;
        return result;
      };
    }
    const out = await runWith(ctx, args);
    if (!out.ok) throw new Error(`synthetic prompt capture failed: ${scenario}`);
    const labels = new Set(ctx.calls.map((call) => call.label));
    for (const spec of agentSpecs()) {
      if (!labels.has(spec.agentType)) throw new Error('incomplete discovery prompt coverage');
    }
    const counts = new Map();
    for (const call of ctx.calls) {
      const producer = call.label.startsWith('summarize') || call.label.startsWith('code-gauntlet:') ? 'discover'
        : call.label.startsWith('verify-') ? 'verifyStage'
          : call.label.startsWith('validate-') || call.label.startsWith('challenge-') ? 'reviewStages' : 'persist';
      const count = counts.get(call.label) || 0;
      counts.set(call.label, count + 1);
      surfaces.push({ surface_id: `generated/${scenario}/${call.label}/${count}`,
        producer_path: `workflows/src/${producer}.js`, scenario, text: call.prompt });
    }
  }
  return surfaces;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href && process.argv[2] === '--bundle') {
  const result = await emitAgentPrompts(process.argv[3]);
  process.stdout.write(JSON.stringify(result));
} else if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.stdout.write(JSON.stringify(await emitLeakPrompts()));
}
