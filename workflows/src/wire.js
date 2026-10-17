// Owns shared wire coercion, checksums, cloning, shell quoting, and freezing primitives.
// Keep orchestration out so consumers can import these mechanisms without a cycle.

// The sandbox has no byte encoder, so content checksums use UTF-16 code units; surrogate pairs count twice.
export function fnv1a32(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `fnv1a32:0x${h.toString(16).padStart(8, '0')}`;
}

// Read presentation changes must not alter either parsed settings or their proof.
export function normalizeReviewText(s) {
  return s.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').replace(/[ \t]+(?=\n|$)/g, '').replace(/\n+$/, '');
}

// The Write tool may add a BOM or one newline; tolerate those without hiding additional trailing newlines.
export function normalizeForChecksum(s) {
  let out = typeof s === 'string' ? s : '';
  if (out.charCodeAt(0) === 0xfeff) out = out.slice(1);
  if (out.endsWith('\r\n')) return out.slice(0, -2);
  if (out.endsWith('\n')) return out.slice(0, -1);
  return out;
}

// The sandbox has no structuredClone. JSON-safe findings permit a faithful JSON round-trip copy.
export function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

// Cross-runtime JSON number spellings can differ, so content proofs admit only safe integers.
// Reject before persistence while a fallback is available; unsafe integers may already be rounded.
export function firstUnsafeNumber(root, rootPath) {
  const stack = [[root, rootPath]];
  while (stack.length > 0) {
    const [node, where] = stack.pop();
    if (typeof node === 'number') {
      if (!Number.isSafeInteger(node)) return where;
      continue;
    }
    if (Array.isArray(node)) {
      for (let i = node.length - 1; i >= 0; i -= 1) stack.push([node[i], `${where}[${i}]`]);
      continue;
    }
    if (node && typeof node === 'object') {
      const entries = Object.entries(node);
      for (let i = entries.length - 1; i >= 0; i -= 1) {
        const [k, v] = entries[i];
        stack.push([v, `${where}.${k}`]);
      }
    }
  }
  return null;
}

// POSIX quoting keeps unsafe tokens in one shell word for command parsing.
// Embedded quotes require concatenation, which may prevent automatic approval.
const SHELL_SAFE_RE = /^[A-Za-z0-9_%+=:,.\/@-]+$/;
export function shellWord(tok) {
  if (tok == null) return '';
  const s = String(tok);
  if (s === '' || SHELL_SAFE_RE.test(s)) return s;
  return `'${s.replaceAll("'", `'\\''`)}'`;
}

// Use the union whitespace class so wire trimming accepts both runtime whitespace vocabularies.
const WIRE_TRIM_CLASS = String.raw`\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff`;
export const WS_TRIM_RE = new RegExp(`^[${WIRE_TRIM_CLASS}]+|[${WIRE_TRIM_CLASS}]+$`, 'g');

// Keep integer policies distinct: filter coercion accepts Python-style integers while verification uses numeric-string grammar.
export const INT_POLICY = Object.freeze({
  validation: Object.freeze({ trim: null, mode: 'pyInt' }),
  filter: Object.freeze({ trim: WS_TRIM_RE, mode: 'pyInt' }),
  verify: Object.freeze({ trim: null, mode: 'jsNumber' }),
});

// Long digit strings can overflow parseInt to infinity; callers must retain their boundary policy for that result.
export function coerceInt(value, policy) {
  const pyInt = policy.mode === 'pyInt';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    // Preserve integer values: rounding would erase negative zero or shift large odd integers.
    if (Number.isInteger(value)) return value;
    // Verification rounds ties toward positive infinity; the integer grammar truncates instead.
    return pyInt ? Math.trunc(value) : Math.floor(value + 0.5);
  }
  // The Python-style integer policy accepts booleans because they are integer values at that wire boundary.
  if (typeof value === 'boolean') return pyInt ? (value ? 1 : 0) : null;
  if (typeof value !== 'string') return null;
  if (pyInt) {
    const s = policy.trim ? value.replace(policy.trim, '') : value.trim();
    return /^[+-]?[0-9]+$/.test(s) ? parseInt(s, 10) : null;
  }
  if (value.trim() === '') return null;
  const n = Number(value);
  return Number.isInteger(n) ? n : null;
}

// Bound pathological return payloads so persistence can fall back to the artifact writer and a smaller resume state.
export const RETURN_CHAR_BUDGET = 1000000;

export function dottedValue(object, path) {
  let current = object;
  for (const part of path.split('.')) {
    if (!isPlainObject(current)) return undefined;
    current = current[part];
  }
  return current;
}

export const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

export function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Non-string model fields must not participate in text matching through implicit coercion.
export function asText(value) {
  return typeof value === 'string' ? value : '';
}

// Keep infinities as numeric filter inputs; the report's finite-only policy is deliberately separate.
// Non-numbers and NaN need the numeric fallback so arithmetic cannot concatenate text or propagate NaN.
export function asConfidence(value) {
  return typeof value === 'number' && !Number.isNaN(value) ? value : 0;
}

// Freeze nested row data too, since freezing a table alone still permits rule changes.
const rejectFrozenCollectionMutation = () => { throw new TypeError('Cannot modify a frozen collection'); };

export function deepFreeze(value) {
  const visited = new Set();
  function freeze(node) {
    // Stateful regexes need a writable lastIndex; table membership can still be frozen.
    if (node === null || typeof node !== 'object' || node instanceof RegExp || visited.has(node)) return node;
    visited.add(node);
    if (node instanceof Map || node instanceof Set) {
      // Object.freeze does not lock a collection's internal entries.
      if (node instanceof Map) for (const key of node.keys()) freeze(key);
      for (const child of node.values()) freeze(child);
      if (!Object.isFrozen(node)) {
        for (const method of ['delete', 'clear', node instanceof Map ? 'set' : 'add']) {
          Object.defineProperty(node, method, { value: rejectFrozenCollectionMutation });
        }
      }
    }
    for (const child of Object.values(node)) freeze(child);
    return Object.freeze(node);
  }
  return freeze(value);
}

export function longestBacktickRun(text) {
  let longest = 0;
  for (const match of text.matchAll(/`+/g)) longest = Math.max(longest, match[0].length);
  return longest;
}
