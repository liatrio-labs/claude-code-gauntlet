import { parseWriterPayload } from '../../src/persist.js';
import { plannedArtifactPaths } from '../../src/outputPaths.js';
import { runWith } from '../../src/stages.js';
import { makeFinding, makeCtx } from './pipelineMock.js';

export function challengeOutput(over = {}) {
  return {
    findings: [], unverified: [], eliminated: [], gaps: [], stats: {},
    generated_at: '2026-07-18T00:00:00Z', ...over,
  };
}

export function persistInput(over = {}) {
  const findings = over.findings || [makeFinding('F1'), makeFinding('F2')];
  return {
    findings, postReview: findings, report: '# Code Gauntlet\n\nfindings rendered here',
    checkpoints: {
      phases: { challenge: challengeOutput({ findings, stats: { final_count: findings.length }, generated_at: '2026-07-27T00:00:00Z' }) },
      completed: ['summarize', 'discover', 'challenge'], phaseReached: 'report', counts: { challenge: findings.length },
    },
    outputDir: '/repo/.code-gauntlet', headShaShort: 'abc1234', policy: {},
    persist: { assembleScriptPath: '/plugin/scripts/assemble_artifacts.py' }, ...over,
  };
}

// Plumbing receipts echo the dispatched plan; independent byte oracles live in persistPlan.test.js.
export function receiptFor(plan, over = {}) {
  return {
    ok: true, planVersion: 2, planChecksum: plan.planChecksum,
    verified: (plan.expect || []).map((e) => ({
      path: e.path, chars: e.chars, expected_chars: e.chars,
      checksum: e.checksum, expected_checksum: e.checksum, content_proof: 'match',
    })),
    written: (plan.derive || []).map((e) => ({ path: e.path, chars: e.chars, checksum: e.checksum })),
    errors: [], ...over,
  };
}

export function persistCtx({ writer, receipt } = {}) {
  const calls = [];
  let plan;
  let attempt = 0;
  return {
    calls,
    agent: async (prompt, dispatch = {}) => {
      calls.push({ prompt, ...dispatch });
      if (dispatch.label === 'artifact-writer') {
        attempt += 1;
        const payload = parseWriterPayload(prompt);
        if (Array.isArray(payload)) plan = JSON.parse(payload.at(-1).text);
        if (writer) return writer(payload, attempt);
        return Array.isArray(payload) ? { written: payload.map((e) => e.path) }
          : { artifactPaths: plannedArtifactPaths('/repo/.code-gauntlet', 'abc1234') };
      }
      if (dispatch.label === 'assemble-artifacts') return receipt ? receipt(receiptFor(plan), attempt) : receiptFor(plan);
      return null;
    },
    parallel: async (thunks) => Promise.all(thunks.map((thunk) => thunk())),
  };
}

export async function runAndCapture(args, overrides = {}) {
  let payload = null;
  const ctx = makeCtx(args, { ...overrides, onPersist: (value) => { payload = value; } });
  const out = await runWith(ctx, args);
  return { out, ctx, payload };
}
