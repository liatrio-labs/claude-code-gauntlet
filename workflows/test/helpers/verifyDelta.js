// Honest executor envelopes echo the dispatched token; fault rows override proofs explicitly.
import { deltaContentProof } from '../../src/verifyWire.js';
import { fnv1a32 } from '../../src/wire.js';
import { shellSplit } from './shellWords.js';
import { assertPrompt, assertValidSchema } from './pipelineMock.js';

export const ELIMINATION_STAMP = 'evidence does not match file content';

// Confidence rides only when already an integer, matching the script output.
export function deltaFor(finding, overrides = {}) {
  const delta = { id: finding.id, verified: true };
  if (typeof finding.origin === 'string') delta.origin = finding.origin;
  if (typeof finding.severity === 'string') delta.severity = finding.severity;
  if (Number.isInteger(finding.confidence)) delta.confidence = finding.confidence;
  return { ...delta, ...overrides };
}

export function deltasFor(findings, overridesById = {}) {
  return findings.map((f) => deltaFor(f, overridesById[f.id] || {}));
}

export function deltaEnvelope(findings, opts = {}) {
  const deltas = opts.deltas || deltasFor(findings, opts.overrides || {});
  const ids = opts.ids || findings.map((f) => f.id);
  return {
    status: 'ok',
    receipt: {
      sha: opts.sha === undefined ? 'abc123' : opts.sha,
      nonce: opts.nonce === undefined ? 'n-1' : opts.nonce,
      n_in: opts.n_in === undefined ? findings.length : opts.n_in,
      deltas_checksum: opts.checksum === undefined ? deltaContentProof(ids, deltas) : opts.checksum,
    },
    result: { deltas },
  };
}

function decodeInlineString(value) {
  let out = '';
  let i = 0;
  while (i < value.length) {
    if (value[i] !== '%') {
      out += value[i];
      i += 1;
      continue;
    }
    if (value[i + 1] === 'u') {
      out += String.fromCharCode(Number.parseInt(value.slice(i + 2, i + 6), 16));
      i += 6;
      continue;
    }
    let bytes = '';
    while (i < value.length && value[i] === '%' && value[i + 1] !== 'u') {
      bytes += value.slice(i, i + 3);
      i += 3;
    }
    out += decodeURIComponent(bytes);
  }
  return out;
}

function decodeInlineValue(value) {
  if (typeof value === 'string') return decodeInlineString(value);
  if (Array.isArray(value)) return value.map(decodeInlineValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [
      decodeInlineString(k),
      decodeInlineValue(v),
    ]));
  }
  return value;
}

// Derive honest mock proofs from the actual command, leaving explicit faults untouched.
export function sliceInputRecorder() {
  return {
    stamp(env, _index, prompt) {
      const argv = shellSplit(prompt.split('\n').pop());
      const index = argv.indexOf('--input-inline');
      if (index < 0) return env;
      const token = argv[index + 1];
      if (env && env.status === 'ok' && env.receipt) {
        if (!Object.hasOwn(env.receipt, 'input_checksum')) {
          env.receipt.input_checksum = fnv1a32(JSON.stringify(decodeInlineValue(JSON.parse(token)), null, 2));
        }
        if (!Object.hasOwn(env.receipt, 'inline_checksum')) env.receipt.inline_checksum = fnv1a32(token);
      }
      return env;
    },
  };
}

export function verifyCtx(executorImpl) {
  const calls = [];
  const rec = sliceInputRecorder();
  let inParallel = 0;
  return {
    calls,
    agent: async (prompt, opts = {}) => {
      assertPrompt(prompt);
      assertValidSchema(opts.schema);
      const call = { prompt, ...opts };
      calls.push(call);
      const match = /^verify-slice-(\d+)(-retry)?$/.exec(opts.label || '');
      if (!match) throw new Error('verifyStage must dispatch only slice executors');
      if (inParallel > 0) throw new Error('verifyStage must not use parallel()');
      const index = Number(match[1]);
      const attempt = match[2] ? 2 : 1;
      return rec.stamp(await executorImpl(index, attempt, call), index, prompt);
    },
    parallel: async (thunks) => {
      inParallel += 1;
      try {
        return await Promise.all(thunks.map(async (thunk) => {
          try { return await thunk(); } catch { return null; }
        }));
      } finally { inParallel -= 1; }
    },
  };
}

export function verifyInput(findings, over = {}) {
  return {
    findings, nonce: 'n-1', headShaShort: 'abc123',
    limits: { verifySliceSize: 200 }, policy: {},
    verify: {
      scriptPath: '/plugin/scripts/verify_findings.py',
      inputPathBase: '/out/phase4-input-abc123',
      outputPathBase: '/out/phase4-output-abc123',
      baseBranch: 'main', diffPath: '/out/code-gauntlet-diff-abc123.patch',
    },
    ...over,
  };
}
