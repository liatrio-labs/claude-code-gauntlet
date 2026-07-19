// pipelineMock.js — shared mock ctx + fixtures for driving runWith end-to-end.
// Single source of truth so the JS contract tests and the Python-invoked node
// recorder (tests/tools) exercise the SAME wired pipeline; the boundary-parity
// test's input is then REAL pipeline persist output, not a hand-authored fixture.
import { parseWriterPayload } from '../../src/stages.js';

// A canonical discovery finding carrying every REQUIRED_FIELD (merge validates
// against these) plus the fields downstream filter/challenge read. Built fresh per
// call so stage mutation (confidence boosts, origin, tags) never leaks across
// dispatches or tests.
export function makeFinding(id, over = {}) {
  return {
    id,
    file: `${id}.js`,
    line_start: 10,
    line_end: 10,
    title: `finding ${id}`,
    description: `a genuine correctness problem in ${id} that is described in enough words to clear the injection and threshold filters`,
    severity: 'high',
    confidence: 90,
    dimension: 'bug',
    origin: 'new',
    evidence: '',
    cross_file_refs: [],
    code: `const ${id} = broken();`,
    ...over,
  };
}

export function makeFindings() {
  return [makeFinding('F1'), makeFinding('F2')];
}

// A fully valid args waist (every REQUIRED field from args.js). `over` patches it.
export function validArgs(over = {}) {
  return {
    argsVersion: 1,
    mode: 'headless',
    repoRoot: '/repo',
    outputDir: '.deep-review',
    headShaShort: 'abc1234',
    nonce: 'nonce-xyz',
    generatedAt: '2026-07-18T00:00:00Z',
    diffPath: '/repo/.deep-review/diff.patch',
    changedFilesPath: '/repo/.deep-review/changed.txt',
    agentFlags: {},
    policy: {},
    limits: { validateBatch: 10, verifySliceSize: 100, challengeCap: 40, summarizeBucketSize: 20 },
    ...over,
  };
}

// Mock ctx. Records every dispatched task on `calls`. Each agent()/parallel() member
// returns a coherent per-stage envelope so the happy path threads all the way to
// report + persist. Options:
//   - agentThrowLabel: agent() throws when task.label === this (report/writer tests)
//   - throwOnDiscover: parallel() throws when handed discovery tasks (top-level catch test)
//   - onPersist(payload): called with the parsed writer payload at the artifact-writer
//     dispatch (lets a test/recorder capture the REAL persisted findings/checkpoints)
export function makeCtx(args, opts = {}) {
  const calls = [];
  const A = args;

  const agent = async (task) => {
    calls.push(task);
    const label = task.label || '';
    if (opts.agentThrowLabel && label === opts.agentThrowLabel) {
      throw new Error(`injected agent throw on ${label}`);
    }
    if (label === 'summarize' || label === 'summarize-merge') return { summary: 'the PR changes X' };
    // verifyStage materializes slice inputs via the artifact-writer before dispatching
    // executors; the writer succeeds by default so verify proceeds to the executor loop.
    if (label.startsWith('verify-input-writer')) return { written: true };
    if (label.startsWith('verify-slice-')) {
      // Echo a receipt the verify stage will TRUST: same head sha, the per-slice
      // derived nonce `${nonce}.0` (one slice because verifySliceSize > nFindings),
      // and n_in === the slice length. verified+eliminated must account for n_in.
      const verified = makeFindings().map((f) => ({ ...f, origin: 'new' }));
      return {
        status: 'ok',
        receipt: { sha: A.headShaShort, n_in: verified.length, nonce: `${A.nonce}.0` },
        result: { verified, eliminated: [], batches: [], stats: {} },
      };
    }
    if (label === 'report-writer' || label.startsWith('report-writer-')) return { report: `# Deep Review\n\nrendered ${label}` };
    if (label === 'artifact-writer') {
      if (opts.onPersist) opts.onPersist(parseWriterPayload(task.prompt));
      return { artifactPaths: {} };
    }
    return null;
  };

  const parallel = async (tasks) => {
    const isDiscover = tasks.some((t) => Array.isArray(t.dimensions));
    if (isDiscover && opts.throwOnDiscover) throw new Error('injected parallel throw in discover');
    return Promise.all(tasks.map(async (t) => {
      calls.push(t);
      const label = t.label || '';
      if (label.startsWith('summarize-bucket-')) return { summary: 'partial' };
      if (label.startsWith('validate-batch-')) return []; // no confidence adjustments
      if (label.startsWith('challenge-')) return { score: 80, justification: 'claim holds' };
      // discovery task: label IS the agentType. Only bug-detector yields findings.
      if (Array.isArray(t.dimensions)) {
        if (t.agentType === 'deep-review:bug-detector') return { findings: makeFindings(), complete: true, total_seen: 2 };
        return { findings: [], complete: true, total_seen: 0 };
      }
      return null;
    }));
  };

  return { calls, agent, parallel };
}
