// pipelineMock.js — shared mock ctx + fixtures for driving runWith end-to-end,
// aligned EXACTLY to the platform dispatch contract so a mock can never again mask
// an object-vs-prompt dispatch bug (the class the Task 15 live smoke caught). The
// Python-invoked node recorder (tests/tools) reuses this so the boundary-parity test's
// input is REAL pipeline output.
//
// Platform contract (Phase 0-verified + Workflow docs):
//   agent(prompt, opts)   — prompt is a STRING; opts = { label, agentType, model, schema }
//   parallel(thunks)      — thunks is an array of ZERO-ARG FUNCTIONS each calling agent(...);
//                           a thrown member resolves to null (siblings unaffected).
import { parseWriterPayload } from '../../src/stages.js';
import { AGENTS } from '../../src/registry.js';

const DISCOVERY_AGENT_TYPES = new Set(AGENTS);

// --- Platform-contract assertions (shared by every mock) --------------------

// agent()'s first arg must be a real prompt STRING (the live smoke's object-dispatch
// delivered the literal "[object Object]").
export function assertPrompt(prompt) {
  if (typeof prompt !== 'string') {
    throw new Error(`platform contract: agent() prompt must be a STRING (got ${typeof prompt}: ${JSON.stringify(prompt)})`);
  }
}

// opts.schema must be REAL JSON Schema: a "type", object schemas carry "properties",
// array schemas carry "items" — recursively (catches nested shorthand like {id:'string'}).
export function assertValidSchema(schema, path = '$') {
  if (!schema || typeof schema !== 'object') throw new Error(`schema ${path}: must be a JSON Schema object (got ${typeof schema})`);
  if (typeof schema.type !== 'string') throw new Error(`schema ${path}: must declare a string "type" (shorthand like {id:'string'} is invalid)`);
  if (schema.type === 'object') {
    if (!schema.properties || typeof schema.properties !== 'object') throw new Error(`schema ${path}: object must declare "properties"`);
    for (const [k, v] of Object.entries(schema.properties)) assertValidSchema(v, `${path}.${k}`);
  } else if (schema.type === 'array') {
    if (!schema.items) throw new Error(`schema ${path}: array must declare "items"`);
    assertValidSchema(schema.items, `${path}[]`);
  }
}

// --- Fixtures ---------------------------------------------------------------

// A canonical discovery finding carrying every REQUIRED_FIELD (merge validates against
// these) plus the fields downstream filter/challenge read. Fresh per call so stage
// mutation never leaks across dispatches or tests.
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

// --- Mock ctx ---------------------------------------------------------------

// makeCtx(args, opts) — platform-contract mock. Every agent() dispatch asserts the
// contract (recording any breach in ctx.violations so the sweep test surfaces even a
// null-isolated one). Records each dispatch's opts on `calls`. Options:
//   - agentThrowLabel: agent() throws when opts.label === this (report/writer tests)
//   - parallelThrows: parallel() itself throws (simulates a platform/glue failure —
//     the ONLY realistic way a throw reaches runWith's top-level catch, since member
//     failures null-isolate and every single-dispatch stage catches its own throw)
//   - onPersist(payload): called with the parsed writer payload at the artifact-writer
//     dispatch (lets a test/recorder capture the REAL persisted findings/checkpoints)
export function makeCtx(args, opts = {}) {
  const calls = [];
  const violations = [];
  const A = args;

  const agent = async (prompt, dispatch = {}) => {
    try {
      assertPrompt(prompt);
      assertValidSchema(dispatch.schema);
    } catch (e) {
      violations.push(`${dispatch.label || '?'}: ${e.message}`);
      throw e;
    }
    const label = dispatch.label || '';
    calls.push({ prompt, ...dispatch });
    if (opts.agentThrowLabel && label === opts.agentThrowLabel) throw new Error(`injected agent throw on ${label}`);

    if (label === 'summarize' || label === 'summarize-merge' || label.startsWith('summarize-bucket-')) {
      return { summary: 'the PR changes X' };
    }
    if (label.startsWith('verify-input-writer')) return { written: [] };
    if (label.startsWith('verify-slice-')) {
      // A receipt the verify stage will TRUST: same head sha, per-slice nonce `${nonce}.0`
      // (one slice, verifySliceSize > nFindings), n_in === slice length, arrays accounting.
      const verified = makeFindings().map((f) => ({ ...f, origin: 'new' }));
      return {
        status: 'ok',
        receipt: { sha: A.headShaShort, n_in: verified.length, nonce: `${A.nonce}.0` },
        result: { verified, eliminated: [], batches: [], stats: {} },
      };
    }
    if (label.startsWith('validate-batch-')) return []; // validator returns an array
    if (label.startsWith('challenge-')) return { confidence_claim_is_correct: 80, justification: 'claim holds' };
    if (label === 'report-writer' || label.startsWith('report-writer-')) return { report: `# Deep Review\n\nrendered ${label}` };
    if (label === 'artifact-writer') {
      if (opts.onPersist) opts.onPersist(parseWriterPayload(prompt));
      return { artifactPaths: {} };
    }
    // Discovery: label IS the agentType. Only bug-detector yields findings.
    if (DISCOVERY_AGENT_TYPES.has(dispatch.agentType)) {
      return dispatch.agentType === 'deep-review:bug-detector'
        ? { findings: makeFindings(), complete: true, total_seen: 2 }
        : { findings: [], complete: true, total_seen: 0 };
    }
    return null;
  };

  const parallel = async (thunks) => {
    if (opts.parallelThrows) throw new Error('simulated platform failure: parallel() unavailable');
    if (!Array.isArray(thunks)) { violations.push('parallel() was not given an array'); throw new Error('parallel() takes an array of thunks'); }
    return Promise.all(thunks.map(async (thunk) => {
      if (typeof thunk !== 'function') { violations.push('parallel() member is not a zero-arg function'); return null; }
      try { return await thunk(); } catch { return null; } // null-isolate a failed member (Phase 0)
    }));
  };

  return { calls, violations, agent, parallel };
}
