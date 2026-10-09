// Wire primitives shared by more than one stage: checksums, cloning, shell quoting and
// integer coercion. A leaf module, so any stage can import it without a cycle.

// The content-proof checksum, computed identically by scripts/gauntlet/jsjson.py. The
// sandbox has no TextEncoder or Buffer, so it walks UTF-16 code units (a surrogate pair
// counts twice on both sides); Math.imul is a language builtin, not a host global.
export function fnv1a32(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `fnv1a32:0x${h.toString(16).padStart(8, '0')}`;
}

// Strip a BOM and at most one trailing newline before checksumming: the Write tool may
// add either, and gauntlet.jsjson.normalize_content applies the same tolerance. Two
// trailing newlines are a real difference.
export function normalizeForChecksum(s) {
  let out = typeof s === 'string' ? s : '';
  if (out.charCodeAt(0) === 0xfeff) out = out.slice(1);
  if (out.endsWith('\r\n')) return out.slice(0, -2);
  if (out.endsWith('\n')) return out.slice(0, -1);
  return out;
}

// The sandbox has no structuredClone. Findings are JSON-safe by construction, so a JSON
// round-trip is a faithful deep copy.
export function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

// The path of the first number the Python twin could not spell identically, or null.
// repr(float) and Number#toString disagree (1e-7 against 1e-07, 90 against 90.0), and
// scripts/gauntlet/jsjson.py refuses such numbers instead of porting the spelling, so
// this applies the same rule one step earlier, where refusing still has a fallback.
// An integer outside the safe range is refused too: JS has already parsed it lossily.
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

// One shell word. Ordinary path characters stay bare, so a plain command is unchanged;
// anything else is POSIX single-quoted, which the sandbox's auto-approval parse reads as
// one raw string. The class is shlex.quote's. An embedded single quote parses as a
// concatenation, so auto-approval is not guaranteed on that edge.
const SHELL_SAFE_RE = /^[A-Za-z0-9_%+=:,.\/@-]+$/;
export function shellWord(tok) {
  // An absent optional field contributes nothing, as Array.join did.
  if (tok == null) return '';
  const s = String(tok);
  if (s === '' || SHELL_SAFE_RE.test(s)) return s;
  return `'${s.replaceAll("'", `'\\''`)}'`;
}

// Trim the union whitespace class at both ends of titles and review lines.
export const WS_TRIM_RE = /^[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+|[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+$/g;

// One integer parser for the three boundaries that read a number a model wrote. `mode`
// selects rounding, boolean handling and the string grammar; `trim` is the class stripped
// before the pyInt grammar, with null meaning String#trim.
export const INT_POLICY = Object.freeze({
  validation: Object.freeze({ trim: null, mode: 'pyInt' }),
  filter: Object.freeze({ trim: WS_TRIM_RE, mode: 'pyInt' }),
  verify: Object.freeze({ trim: null, mode: 'jsNumber' }),
});

// Returns an integer or null; pyInt can also return an infinity for an over-long digit
// string, as parseInt does.
export function coerceInt(value, policy) {
  const pyInt = policy.mode === 'pyInt';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    // Returned as is: rounding would turn -0 into 0 and move a large odd integer.
    if (Number.isInteger(value)) return value;
    // pyInt truncates as Python's int(); jsNumber rounds half up as the verify script's
    // int(math.floor(value + 0.5)), which Math.round would not match on negative ties.
    return pyInt ? Math.trunc(value) : Math.floor(value + 0.5);
  }
  // A Python bool is an int subclass.
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
