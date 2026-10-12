import { parseWriterPayload } from '../../src/persist.js';
import { runWith } from '../../src/stages.js';
import { makeFinding, makeCtx } from './pipelineMock.js';

export const PATHS = {
  findings: '/repo/.code-gauntlet/code-gauntlet-findings-abc1234.json',
  report: '/repo/.code-gauntlet/code-gauntlet-report-abc1234.md',
  postReview: '/repo/.code-gauntlet/code-gauntlet-post-review-abc1234.json',
  checkpoints: '/repo/.code-gauntlet/code-gauntlet-checkpoint-all-abc1234.json',
};
export const PLAN_PATH = '/repo/.code-gauntlet/code-gauntlet-persist-plan-abc1234.json';

export function challengeOutput(over = {}) {
  return {
    findings: [], unverified: [], eliminated: [], gaps: [], stats: {},
    generated_at: '2026-07-18T00:00:00Z', ...over,
  };
}
export const PROJECTION_A = { id: 'A', line_start: 7, line_end: 9, description: 'body text', report_tag: 'suggestion' };
export const PROJECTION_B = { id: 'B', line_start: 3, description: 'other', confidence: 71, report_tag: 'main' };
export const PROJECTION_C = {
  id: 'C', title: '\u4e2d\u6587\u6807\u9898 \ud83c\udf89',
  description: '\u65e5\u672c\u8a9e\u306e\u8aac\u660e \ud83d\ude00 with an astral \ud835\udd4f character',
};
export const ESCAPED_PROJECTION = {
  id: 'A', description: "gradeInputProof produces \\\"the executor's receipt carried no input_checksum\\\" here",
};

export function persistCheckpoints(findings) {
  return {
    phases: { challenge: challengeOutput({
      findings, stats: { final_count: findings.length }, generated_at: '2026-07-27T00:00:00Z',
    }) },
    completed: ['summarize', 'discover', 'challenge'], phaseReached: 'report', counts: { challenge: findings.length },
  };
}

export function replaySurvivor() {
  return makeFinding('M1', { severity: 'critical', confidence: 95 });
}

export function persistInput(over = {}) {
  const findings = over.findings || [makeFinding('F1'), makeFinding('F2')];
  return {
    findings, postReview: findings, report: '# Code Gauntlet\n\nfindings rendered here',
    checkpoints: persistCheckpoints(findings),
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
          : { artifactPaths: PATHS };
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

export function wellFormedCheckpoints() {
  return {
    summarize: { summary: 'x', gaps: [] },
    discover: { findings: [makeFinding('D1')], gaps: [], degraded: [], dispatched: ['code-gauntlet:bug-detector'] },
    merge: { findings: [makeFinding('M1')], gaps: [] },
    verify: { findings: [makeFinding('V1')], gaps: [] },
    validate: { findings: [makeFinding('VA1')], gaps: [] },
    filter: { filtered: [makeFinding('F1')], gaps: [] },
    challenge: challengeOutput({ findings: [makeFinding('C1')] }),
    report: { report: '# r', gaps: [] },
  };
}

// Copy every ancestor so overrides cannot mutate the golden or another row's oracle.
export function expectedWith(golden, overrides = {}) {
  const copy = (value) => Array.isArray(value) ? value.map(copy)
    : value && typeof value === 'object'
      ? Object.fromEntries(Object.entries(value).map(([key, child]) => [key, copy(child)])) : value;
  const expected = copy(golden);
  for (const [path, value] of Object.entries(overrides)) {
    const keys = path.split('.');
    const parent = keys.slice(0, -1).reduce((object, key) => object[key], expected);
    parent[keys.at(-1)] = copy(value);
  }
  return expected;
}

// Original line offsets let a row describe only changed bytes, including indentation and commas.
export function textWithLines(golden, edits = []) {
  const lines = golden.split('\n');
  for (const [start, count, ...replacement] of [...edits].reverse()) lines.splice(start, count, ...replacement);
  return lines.join('\n');
}
