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
import { parseWriterPayload, plannedArtifactPaths } from '../../src/stages.js';
import { AGENTS } from '../../src/registry.js';
import { deltaEnvelope, sliceInputRecorder } from './verifyDelta.js';

const DISCOVERY_AGENT_TYPES = new Set(AGENTS);

// --- Platform-contract assertions (shared by every mock) --------------------

// agent()'s first arg must be a real prompt STRING (the live smoke's object-dispatch
// delivered the literal "[object Object]").
export function assertPrompt(prompt) {
  if (typeof prompt !== 'string') {
    throw new Error(`platform contract: agent() prompt must be a STRING (got ${typeof prompt}: ${JSON.stringify(prompt)})`);
  }
}

// A single allOf entry of the conditional-required construct (issue #218): { if, then }, NOT
// a full JSON Schema — `if`/`then` here carry only `properties`/`required`, no `type` (the
// measured-accepted spelling stages.js emits). Local validity coverage only: this pins the
// SHAPE (if.properties.dimension.const, if.required, then.required), not that it is
// semantically the right dimension/field pair — that is finding_schema.test.js's job.
function assertValidConditional(clause, path) {
  if (!clause || typeof clause !== 'object') throw new Error(`schema ${path}: allOf entry must be an object`);
  if (!clause.if || typeof clause.if !== 'object') throw new Error(`schema ${path}: allOf entry must declare "if"`);
  if (!clause.then || typeof clause.then !== 'object') throw new Error(`schema ${path}: allOf entry must declare "then"`);
  if (!clause.if.properties || typeof clause.if.properties !== 'object') throw new Error(`schema ${path}.if: must declare "properties"`);
  if (!clause.if.properties.dimension || typeof clause.if.properties.dimension.const !== 'string') {
    throw new Error(`schema ${path}.if.properties.dimension: must declare a string "const"`);
  }
  if (!Array.isArray(clause.if.required)) throw new Error(`schema ${path}.if: must declare "required" as an array`);
  if (!Array.isArray(clause.then.required)) throw new Error(`schema ${path}.then: must declare "required" as an array`);
}

// opts.schema must be REAL JSON Schema: a "type", object schemas carry "properties",
// array schemas carry "items" — recursively (catches nested shorthand like {id:'string'}).
export function assertValidSchema(schema, path = '$') {
  if (!schema || typeof schema !== 'object') throw new Error(`schema ${path}: must be a JSON Schema object (got ${typeof schema})`);
  if (typeof schema.type !== 'string') throw new Error(`schema ${path}: must declare a string "type" (shorthand like {id:'string'} is invalid)`);
  if (schema.type === 'object') {
    if (!schema.properties || typeof schema.properties !== 'object') throw new Error(`schema ${path}: object must declare "properties"`);
    for (const [k, v] of Object.entries(schema.properties)) assertValidSchema(v, `${path}.${k}`);
    if (schema.allOf !== undefined) {
      if (!Array.isArray(schema.allOf)) throw new Error(`schema ${path}: allOf must be an array`);
      schema.allOf.forEach((clause, i) => assertValidConditional(clause, `${path}.allOf[${i}]`));
    }
  } else if (schema.type === 'array') {
    if (!schema.items) throw new Error(`schema ${path}: array must declare "items"`);
    assertValidSchema(schema.items, `${path}[]`);
  }
}

// Every top-level dispatch schema must be OBJECT-rooted: the Messages API rejects an
// array-rooted tool input_schema with `tools.N.custom.input_schema.type: Input should
// be 'object'` (the 400 the live smoke run hit on the array-rooted VALIDATE_SCHEMA).
// Nested schemas may still be arrays — this constrains only the dispatch ROOT.
export function assertObjectRootSchema(schema) {
  if (!schema || schema.type !== 'object') {
    throw new Error(`dispatch schema must be object-rooted (API contract: root type must be 'object', got ${schema && schema.type})`);
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
// riskTable is DERIVED from `over.changedFiles` (each entry 'medium') when the caller
// overrides changedFiles but not riskTable — riskTable's path set must equal changedFiles'
// exactly, so a fixture whose default riskTable never tracked an overridden changedFiles
// would fail validateArgs's path-set guard on every such call site. 'medium', not 'low': the
// default fixture must NOT be light-eligible (computeLightEligible requires every entry
// 'low'), so tests that don't care about scope get a coherent full-scope waist without also
// having to stamp scopeAnswer.
export function validArgs(over = {}) {
  const changedFiles = over.changedFiles || ['a.js'];
  const base = {
    argsVersion: 1,
    mode: 'headless',
    repoRoot: '/repo',
    outputDir: '/repo/.code-gauntlet',
    headShaShort: 'abc1234',
    nonce: 'nonce-xyz',
    generatedAt: '2026-07-18T00:00:00Z',
    diffPath: '/repo/.code-gauntlet/diff.patch',
    changedFilesPath: '/repo/.code-gauntlet/changed.txt',
    changedFiles,
    changedLines: 1,
    riskTable: changedFiles.map((path) => ({ path, risk: 'medium' })),
    policy: {},
    limits: { validateBatch: 25, verifySliceSize: 100, challengeCap: 40, summarizeBucketSize: 20, deliveryCap: 25 },
    configEcho: {
      model_tier: { value: 'optimized', source: 'default' },
      delivery: { value: 'markdown', source: 'default' },
      post_mode: { value: 'dry-run', source: 'default' },
      pr_comment_cap: { value: '25', source: 'default' },
      delivery_tier: { value: 'all', source: 'default' },
      draft_policy: { value: 'review', source: 'default' },
      reviewed_policy: { value: 'full', source: 'default' },
      pr_not_found_policy: { value: 'error', source: 'default' },
      trivial_scope: { value: 'full', source: 'default' },
    },
    pluginRoot: '/plugin',
    reviewScope: { requested: 'full', kind: 'full', since: null, commits: null, detector: null },
  };
  const args = { ...base, ...over, limits: { ...base.limits, ...(over.limits || {}) } };
  if (!Object.hasOwn(over, 'configEcho')) {
    const cap = args.limits.deliveryCap;
    args.configEcho = {
      ...base.configEcho,
      pr_comment_cap: { value: cap == null ? 'null' : String(cap), source: 'default' },
      delivery_tier: { value: args.delivery && args.delivery.tier ? args.delivery.tier : 'all', source: 'default' },
      trivial_scope: { value: args.scopeAnswer || 'full', source: 'default' },
    };
  }
  return args;
}

// --- Mock ctx ---------------------------------------------------------------

// makeCtx(args, opts) — platform-contract mock. Every agent() dispatch asserts the
// contract (recording any breach in ctx.violations so the sweep test surfaces even a
// null-isolated one). Records each dispatch's opts on `calls`. Options:
//   - agentThrowLabel: agent() throws when opts.label === this (writer tests)
//   - parallelThrows: parallel() itself throws (simulates a platform/glue failure —
//     the ONLY realistic way a throw reaches runWith's top-level catch, since member
//     failures null-isolate and every single-dispatch stage catches its own throw)
//   - onPersist(payload): called with the parsed writer payload at the artifact-writer
//     dispatch (lets a test/recorder capture the REAL persisted findings/checkpoints)
//   - findings: replaces the default makeFindings() set that bug-detector discovers. Under
//     the delta echo (issue #25 PR2) the verify-slice executor never receives or returns the
//     finding itself — only a per-id DECISION — so a specific finding shape (e.g. a long
//     description) survives verify because trustSlice/joinVerifyDeltas re-attach the delta to
//     the DISPATCHED slice finding, not because anything here echoes it back. This option
//     still lets a test drive that shape end-to-end and assert it survives to persist.
//   - verifySliceFailIndex: when set to a slice index N, both `verify-slice-N` and
//     `verify-slice-N-retry` return an untrusted envelope so that slice degrades after its
//     one retry; other slices still echo a trusted per-slice delta envelope. Multi-slice
//     tests must keep limits.verifySliceSize under the agent-count coarsening guard (same
//     constraint the mock uses when reconstructing slices from seed findings + args.limits).
//   - nullAgentLabels: a LIST of agent labels whose dispatch throws, so parallel()'s
//     null-isolation (Phase 0) resolves that member to null in place — siblings dispatch
//     normally. Discovery labels ARE the agentType (e.g. 'code-gauntlet:bug-detector'), so
//     this is how a test drives "every/some discovery agent failed" end-to-end through
//     runWith, mirroring stages_discover.test.js's local fakeCtx({nulls}) (issue #178).
//     Generic by label, not scoped to discovery — any dispatch labeled here nulls out.
export function makeCtx(args, opts = {}) {
  const calls = [];
  const violations = [];
  const A = args;
  const rec = sliceInputRecorder();
  const seedFindings = () => (opts.findings ? opts.findings.map((f) => ({ ...f })) : makeFindings());

  // Mirror verifyStage's chunking so a multi-slice mock can echo the right findings and
  // per-slice nonce. Uses args.limits.verifySliceSize — callers that need >1 slice must
  // choose a size that coarsenLimits will not widen before verify runs.
  const sliceForIndex = (i) => {
    const all = seedFindings();
    const size = Math.max(1, (A.limits && A.limits.verifySliceSize) || all.length || 1);
    return all.slice(i * size, i * size + size);
  };

  const agent = async (prompt, dispatch = {}) => {
    try {
      assertPrompt(prompt);
      assertValidSchema(dispatch.schema);
      assertObjectRootSchema(dispatch.schema);
    } catch (e) {
      violations.push(`${dispatch.label || '?'}: ${e.message}`);
      throw e;
    }
    const label = dispatch.label || '';
    calls.push({ prompt, ...dispatch });
    if (opts.agentThrowLabel && label === opts.agentThrowLabel) throw new Error(`injected agent throw on ${label}`);
    if (opts.nullAgentLabels && opts.nullAgentLabels.includes(label)) throw new Error(`injected null-agent failure on ${label}`);

    if (label === 'summarize' || label === 'summarize-merge' || label.startsWith('summarize-bucket-')) {
      return { summary: 'the PR changes X' };
    }
    if (label.startsWith('verify-slice-')) {
      // Per-slice DELTA receipt (issue #25 PR2): label carries the index (and optional
      // -retry); nonce is `${nonce}.${i}` on attempt 1 and `${nonce}.${i}.r1` on the retry.
      // Build the envelope over only THIS slice's findings so n_in and the id-coverage
      // check in trustSlice match when verifySliceSize < nFindings.
      const m = /^verify-slice-(\d+)(-retry)?$/.exec(label);
      const sliceIndex = m ? Number(m[1]) : 0;
      const isRetry = Boolean(m && m[2]);
      if (opts.verifySliceFailIndex === sliceIndex) {
        return { status: 'failed', exitCode: 1, stderr: `injected verify failure on slice ${sliceIndex}` };
      }
      const slice = sliceForIndex(sliceIndex);
      const sliceNonce = isRetry ? `${A.nonce}.${sliceIndex}.r1` : `${A.nonce}.${sliceIndex}`;
      // deltaEnvelope builds a TRUSTED delta echo for this slice — one {id, verified, ...}
      // decision per dispatched finding, never the finding itself (the executor can no
      // longer supply findings at all under the delta echo). The old by-value mock stamped
      // origin:'new' onto every echoed finding so the pipeline's happy-path tests could tell
      // "verified" apart from "unknown"/degraded origin; the override here reproduces that
      // same signal on every delta in the slice.
      const originOverrides = Object.fromEntries(slice.map((f) => [f.id, { origin: 'new' }]));
      return rec.stamp(deltaEnvelope(slice, {
        sha: A.headShaShort,
        nonce: sliceNonce,
        n_in: slice.length,
        overrides: originOverrides,
      }), sliceIndex, prompt);
    }
    if (label.startsWith('validate-batch-')) return { validations: [] }; // object-rooted { validations: [...] }
    if (label.startsWith('challenge-')) return { confidence_claim_is_correct: 80, justification: 'claim holds' };
    if (label === 'artifact-writer') {
      if (opts.onPersist) opts.onPersist(parseWriterPayload(prompt));
      // Faithful writer: echo the exact planned paths so writeArtifacts' write-proof gate
      // (echo must account for all four planned paths) passes. plannedArtifactPaths is the
      // single source of truth shared by writeArtifacts and this mock.
      return { artifactPaths: plannedArtifactPaths(A.outputDir, A.headShaShort) };
    }
    // Discovery: label IS the agentType. Only bug-detector yields findings.
    if (DISCOVERY_AGENT_TYPES.has(dispatch.agentType)) {
      const disc = seedFindings();
      return dispatch.agentType === 'code-gauntlet:bug-detector'
        ? { findings: disc, complete: true, total_seen: disc.length }
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
