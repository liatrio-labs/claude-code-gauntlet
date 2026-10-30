// Shared host-contract assertions keep tests aligned with actual dispatch shapes.
import { parseWriterPayload } from '../../src/persist.js';
import { plannedArtifactPaths } from '../../src/outputPaths.js';
import { AGENTS } from '../../src/registry.js';
import { deltaEnvelope, sliceInputRecorder, dispatchedFindings } from './verifyDelta.js';

const DISCOVERY_AGENT_TYPES = new Set(AGENTS);

// --- Platform-contract assertions (shared by every mock) --------------------

// Real prompt strings keep the mock from accepting object-valued dispatches.
export function assertPrompt(prompt) {
  if (typeof prompt !== 'string') {
    throw new Error(`platform contract: agent() prompt must be a STRING (got ${typeof prompt}: ${JSON.stringify(prompt)})`);
  }
}

// Each conditional-required allOf entry has { if, then }, rather than
// a full JSON Schema — `if`/`then` here carry only `properties`/`required`, no `type` (the
// measured-accepted spelling discover.js emits). Local validity coverage only: this pins the
// SHAPE (if.properties.dimension.const, if.required, then.required), not that it is
// semantically the right dimension/field pair — that is discover.test.js's job.
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
  if (typeof schema.type !== 'string' && !(Array.isArray(schema.type) && schema.type.every((type) => typeof type === 'string'))) {
    throw new Error(`schema ${path}: must declare a string "type" (shorthand like {id:'string'} is invalid)`);
  }
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
// be 'object'`, so the mock enforces the same root shape.
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
    location_status: 'confirmed',
    diff_validation: { in_diff: true, reason: 'fixture diff membership' },
    anchor: 'return process(value);',
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
//     the delta echo the verify-slice executor never receives or returns the
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
//     runWith, mirroring discover.test.js's label-keyed failure rows.
//     Generic by label, not scoped to discovery — any dispatch labeled here nulls out.
export function makeCtx(args, opts = {}) {
  const calls = [];
  const violations = [];
  const A = args;
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
      const m = /^verify-slice-(\d+)(-retry)?$/.exec(label);
      const sliceIndex = m ? Number(m[1]) : 0;
      const isRetry = Boolean(m && m[2]);
      if (opts.verifySliceFailIndex === sliceIndex) {
        return { status: 'failed', exitCode: 1, stderr: `injected verify failure on slice ${sliceIndex}` };
      }
      const slice = dispatchedFindings(prompt) ?? sliceForIndex(sliceIndex);
      const sliceNonce = isRetry ? `${A.nonce}.${sliceIndex}.r1` : `${A.nonce}.${sliceIndex}`;
      // A deterministic classification keeps routing tests independent of git blame.
      const originOverrides = Object.fromEntries(slice.map((f) => [f.id, { origin: 'new', diff_validation: { in_diff: true } }]));
      return sliceInputRecorder(deltaEnvelope(slice, {
        sha: A.headShaShort,
        nonce: sliceNonce,
        n_in: slice.length,
        overrides: originOverrides,
      }), prompt);
    }
    if (label.startsWith('validate-batch-')) {
      const ids = [...prompt.matchAll(/^- (\S+) \[/gm)].map((m) => m[1]);
      return { validations: ids.map((finding_id) => ({ finding_id, confidence: seedFindings().find((f) => f.id === finding_id)?.confidence ?? 90 })) };
    }
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
        : { findings: [], complete: true, total_seen: 0,
          ...(dispatch.agentType === 'code-gauntlet:test-analyzer' ? { removed_test_audit: [] } : {}) };
    }
    return null;
  };

  const parallel = async (thunks) => {
    if (opts.parallelThrows) throw new Error('simulated platform failure: parallel() unavailable');
    if (!Array.isArray(thunks)) {
      violations.push('parallel() was not given an array');
      throw new Error('parallel() takes an array of thunks');
    }
    return Promise.all(thunks.map(async (thunk) => {
      if (typeof thunk !== 'function') { violations.push('parallel() member is not a zero-arg function'); return null; }
      try { return await thunk(); } catch { return null; } // null-isolate a failed member (Phase 0)
    }));
  };

  return { calls, violations, agent, parallel };
}
