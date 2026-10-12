import { runWith } from '../../src/run.js';
import { makeCtx, makeFinding } from './pipelineMock.js';

const ABSENT_GLOBALS = ['structuredClone', 'Buffer', 'TextEncoder', 'TextDecoder', 'setTimeout', 'queueMicrotask'];
export async function withSandboxGlobals(fn) {
  const saved = ABSENT_GLOBALS.map((name) => globalThis[name]);
  for (const name of ABSENT_GLOBALS) delete globalThis[name];
  try { return await fn(); } finally { ABSENT_GLOBALS.forEach((name, index) => { globalThis[name] = saved[index]; }); }
}

export function mixedSurvivors() {
  return [
    makeFinding('M1', { severity: 'critical', confidence: 95, report_tag: 'main', report_destination: 'main' }),
    makeFinding('S1', { severity: 'high', confidence: 90, report_tag: 'suggestion', report_destination: 'suggestion' }),
    makeFinding('M2', { severity: 'medium', confidence: 80, report_tag: 'main', report_destination: 'main' }),
  ];
}

export async function captureRun(args, { summary, pipelineVersion, ...options } = {}) {
  let payload = null;
  const ctx = makeCtx(args, { ...options, onPersist: (value) => { payload = value; } });
  if (pipelineVersion !== undefined) ctx.pipelineVersion = pipelineVersion;
  const agent = ctx.agent;
  ctx.agent = (prompt, dispatch) => summary !== undefined && dispatch.label.startsWith('summarize')
    ? Promise.resolve({ summary }) : agent(prompt, dispatch);
  const out = await runWith(ctx, args);
  return { out, ctx, payload };
}

// A sequential implementation parks on the promise gate before this drain ends.
export async function drainMicrotasks() {
  for (let index = 0; index < 100; index += 1) await Promise.resolve();
}
