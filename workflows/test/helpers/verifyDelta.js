// verifyDelta.js — build the envelope shape the verify executor returns (issue #25 PR2).
//
// After the delta echo, an executor answer is a receipt plus a per-finding DELTA: what
// verify_findings.py DECIDED about each dispatched id, never the findings themselves.
// Every verify test needs to synthesise one, so it is built once here.
//
// The checksum is computed with the REAL exported computation (deltaContentProof), not a
// second copy of the canonicalisation: a helper that re-derived it would agree with a
// broken implementation just as happily as with a correct one. What the JS suite proves
// with this helper is the trust/join/degradation LOGIC; that the computation itself
// matches Python's is pinned separately by the golden fixture in
// tests/fixtures/parity/verify_deltas/, whose checksum verify_findings.py produced.
import { deltaContentProof, fnv1a32 } from '../../src/stages.js';
import { shellSplit } from './shellWords.js';

// The exact string run_verification() stamps on every real elimination. Tests that
// synthesise an eliminated delta must use it — trustSlice requires a non-empty stamp,
// and using the real one keeps the fixtures honest about what the script writes.
export const ELIMINATION_STAMP = 'evidence does not match file content';

// deltaFor(finding, overrides) -> the delta verify_findings.py would emit for a finding it
// verified without changing anything: the fields it re-decides, echoed from the finding
// itself, so a trusted join reproduces the dispatched finding (minus `agent`).
// `confidence` rides only when it is already an integer — the script canonicalises it to
// one, and trustSlice rejects anything else.
export function deltaFor(finding, overrides = {}) {
  const delta = { id: finding.id, verified: true };
  if (typeof finding.origin === 'string') delta.origin = finding.origin;
  if (typeof finding.severity === 'string') delta.severity = finding.severity;
  if (Number.isInteger(finding.confidence)) delta.confidence = finding.confidence;
  return { ...delta, ...overrides };
}

// deltasFor(findings, overridesById) -> one delta per finding, in dispatch order.
// overridesById maps a finding id to per-delta overrides, e.g.
//   deltasFor(slice, { F2: { verified: false, elimination_reason: ELIMINATION_STAMP } })
export function deltasFor(findings, overridesById = {}) {
  return findings.map((f) => deltaFor(f, overridesById[f.id] || {}));
}

// deltaEnvelope(findings, opts) -> a trusted VERIFY_SCHEMA envelope for `findings`.
//   sha / nonce / n_in   — receipt fields, defaulted to the happy path
//   deltas               — replaces the derived delta list wholesale (substitution tests)
//   ids                  — the id order the checksum is computed over; defaults to the
//                          dispatched findings' ids, which is what trustSlice will use
//   checksum             — overrides the computed proof (drift tests)
//   overrides            — per-id delta overrides, passed to deltasFor
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

// The slice-input content proof is computed by the workflow over the content it
// DISPATCHED, so a faithful executor mock must echo the checksum of the decoded inline
// document -- not a value the test invented. This recorder parses the command token,
// remembers each slice's content, and stamps the matching proof onto any executor
// envelope that does not already declare one. Tests that probe the proof declare
// `input_checksum` explicitly (a wrong value, or null for "the executor dropped it") and
// the stamp leaves them alone.
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

// The slice-input content proof is computed by the workflow over the content it
// dispatched. This test recorder decodes the command token so happy-path mocks echo
// the same proof without duplicating the stage planner or projection.
export function sliceInputRecorder() {
  const byPath = new Map();
  const contentFromPrompt = (prompt) => {
    const argv = shellSplit(prompt.split('\n').pop());
    const index = argv.indexOf('--input-inline');
    if (index < 0) return null;
    return decodeInlineValue(JSON.parse(argv[index + 1]));
  };
  const checksumFor = (i) => {
    for (const [p, content] of byPath) {
      if (p.endsWith('.slice' + i + '.json')) return fnv1a32(JSON.stringify(content, null, 2));
    }
    return null;
  };
  return {
    checksumFor,
    stamp(env, i, prompt) {
      const content = contentFromPrompt(prompt);
      if (content) {
        const argv = shellSplit(prompt.split('\n').pop());
        const inputPath = argv[argv.indexOf('--input') + 1];
        byPath.set(inputPath, content);
      }
      if (env && env.status === 'ok' && env.receipt && !Object.hasOwn(env.receipt, 'input_checksum')) {
        env.receipt.input_checksum = checksumFor(i);
      }
      return env;
    },
  };
}
