import { assertPrompt, assertValidSchema, assertObjectRootSchema } from './pipelineMock.js';

// Keep failures positional, as the platform does, so attribution cannot be hidden by the fixture.
export function stageCtx({ reply = { findings: [], complete: true, total_seen: 0, removed_test_audit: [] }, replies = {}, nulls = [] } = {}) {
  const calls = [];
  const violations = [];
  return {
    calls, violations,
    agent: async (prompt, opts = {}) => {
      try {
        assertPrompt(prompt);
        assertValidSchema(opts.schema);
        assertObjectRootSchema(opts.schema);
      } catch (error) {
        violations.push(error.message);
        throw error;
      }
      calls.push({ prompt, ...opts });
      if (nulls.includes(opts.label)) throw new Error(`injected failure for ${opts.label}`);
      const value = Object.hasOwn(replies, opts.label) ? replies[opts.label] : reply;
      return typeof value === 'function' ? value(prompt, opts) : value;
    },
    parallel: async (thunks) => Promise.all(thunks.map(async (thunk) => {
      if (typeof thunk !== 'function') throw new Error('parallel() members must be zero-arg functions');
      try { return await thunk(); } catch { return null; }
    })),
  };
}

const RANDOM_TEXT_CHARS = ['a', 'B', ' ', '0', '/', "'", '\\', '%', '\n', '\u2014', '\u{1F600}'];

export function randomFindings(seed, count) {
  let state = seed >>> 0;
  const next = (limit) => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state % limit;
  };
  const text = (minimum, maximum) => {
    const length = minimum + next(maximum - minimum + 1);
    return Array.from({ length }, () => RANDOM_TEXT_CHARS[next(RANDOM_TEXT_CHARS.length)]).join('');
  };
  return Array.from({ length: count }, (_, i) => ({
    id: `F${i}-${text(1, 8)}`,
    file: `${text(2, 10)}.js`,
    line_start: 1 + next(500),
    line_end: 1 + next(500),
    description: text(20, 100),
    evidence: text(5, 50),
    severity: ['low', 'medium', 'high'][next(3)],
    confidence: next(101),
    cross_file_refs: Array.from({ length: next(3) }, () => `${text(2, 8)}:${1 + next(90)}`),
    origin: ['new', 'modified', 'unknown'][next(3)],
  }));
}
