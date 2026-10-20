import { readFileSync } from 'node:fs';
import { measurementScenario, persistedScenario } from '../helpers/measurementScenario.js';

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

if (process.argv[2] === '--bundle') {
  const result = await emitAgentPrompts(process.argv[3]);
  process.stdout.write(JSON.stringify(result));
}
