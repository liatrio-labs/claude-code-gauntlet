import { INT_POLICY, coerceInt, fnv1a32 } from './wire.js';

// A narrower alphabet than shellWord keeps encoded strings free of shell syntax.
export const VERIFY_INLINE_SAFE = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,:/_-';
const VERIFY_INLINE_HEX = '0123456789ABCDEF';
const VERIFY_INLINE_PRINTABLE_RE = /^[\x20-\x26\x28-\x7E]*$/;

function appendInlineByte(out, byte) {
  out.push(`%${VERIFY_INLINE_HEX[(byte >> 4) & 0x0F]}${VERIFY_INLINE_HEX[byte & 0x0F]}`);
}

function appendInlineUtf8(out, codePoint) {
  if (codePoint <= 0x7F) {
    appendInlineByte(out, codePoint);
  } else if (codePoint <= 0x7FF) {
    appendInlineByte(out, 0xC0 | (codePoint >> 6));
    appendInlineByte(out, 0x80 | (codePoint & 0x3F));
  } else if (codePoint <= 0xFFFF) {
    appendInlineByte(out, 0xE0 | (codePoint >> 12));
    appendInlineByte(out, 0x80 | ((codePoint >> 6) & 0x3F));
    appendInlineByte(out, 0x80 | (codePoint & 0x3F));
  } else {
    appendInlineByte(out, 0xF0 | (codePoint >> 18));
    appendInlineByte(out, 0x80 | ((codePoint >> 12) & 0x3F));
    appendInlineByte(out, 0x80 | ((codePoint >> 6) & 0x3F));
    appendInlineByte(out, 0x80 | (codePoint & 0x3F));
  }
}

// Walk UTF-16 so Python can restore lone surrogates via %uXXXX without replacement.
export function encodeInlineString(s) {
  const text = String(s);
  const out = [];
  for (let i = 0; i < text.length; i += 1) {
    const unit = text.charCodeAt(i);
    const ch = text[i];
    if (VERIFY_INLINE_SAFE.includes(ch)) {
      out.push(ch);
      continue;
    }
    if (unit >= 0xD800 && unit <= 0xDFFF) {
      const next = text.charCodeAt(i + 1);
      if (unit <= 0xDBFF && next >= 0xDC00 && next <= 0xDFFF) {
        appendInlineUtf8(out, 0x10000 + ((unit - 0xD800) << 10) + (next - 0xDC00));
        i += 1;
        continue;
      }
      out.push(`%u${unit.toString(16).toUpperCase().padStart(4, '0')}`);
      continue;
    }
    appendInlineUtf8(out, unit);
  }
  return out.join('');
}

function encodeInlineValue(value) {
  if (typeof value === 'string') return encodeInlineString(value);
  if (Array.isArray(value)) return value.map(encodeInlineValue);
  if (value && typeof value === 'object') {
    const out = Object.create(null);
    for (const [key, child] of Object.entries(value)) {
      out[encodeInlineString(key)] = encodeInlineValue(child);
    }
    return out;
  }
  return value;
}

// Printable ASCII without apostrophes fits one POSIX single-quoted shell word.
export function encodeSliceInline(content) {
  const encoded = JSON.stringify(encodeInlineValue(content));
  if (typeof encoded !== 'string' || !VERIFY_INLINE_PRINTABLE_RE.test(encoded)) {
    throw new Error('encodeSliceInline produced a non-printable or quoted payload');
  }
  return encoded;
}

// 51,744 chars was the largest payload measured to copy exactly (2/2 sonnet executors);
// above it is unmeasured. The rest of the command adds about 300 chars.
export const VERIFY_INLINE_CHAR_BUDGET = 50000;

// Python delta generation and JS checksum/join share this order. Only script-written
// fields belong here; held findings retain merge-added fields such as agent. The generator
// projects this list into Python, but VERIFY_SCHEMA is written beside it by hand.
export const DELTA_KEYS = ['id', 'verified', 'origin', 'severity', 'confidence', 'elimination_reason'];

const DELTA_VALUE_KEYS = DELTA_KEYS.filter((k) => k !== 'id' && k !== 'verified');

export const deltaHas = (d, k) => d[k] !== undefined && d[k] !== null;

// Keep exact ids to avoid collisions between padded and unpadded findings.
function deltasById(deltas) {
  const byId = new Map();
  for (const d of Array.isArray(deltas) ? deltas : []) {
    if (d && typeof d.id === 'string') byId.set(d.id, d);
  }
  return byId;
}

// Walk held findings to preserve untouched fields, membership and order. Call only
// after trustSlice proves coverage; otherwise a missing delta would drop a finding.
export function joinVerifyDeltas(slice, deltas) {
  const byId = deltasById(deltas);
  const out = [];
  for (const f of slice) {
    const delta = byId.get(f.id);
    if (!delta || delta.verified === false) continue;
    const joined = pinNumericFields(f);
    for (const k of DELTA_VALUE_KEYS) if (deltaHas(delta, k)) joined[k] = delta[k];
    out.push(joined);
  }
  return out;
}

// Twin of gauntlet.verify.wire.coerce_numeric_fields. Pin numbers before dispatch
// and join so verification, degraded output and downstream arithmetic agree.
const VERIFY_NUMERIC_FIELDS = ['line_start', 'line_end', 'line', 'end_line', 'confidence'];
export function pinNumericFields(finding) {
  const out = { ...finding };
  for (const k of VERIFY_NUMERIC_FIELDS) {
    const n = coerceInt(out[k], INT_POLICY.verify);
    if (n !== null) out[k] = n;
  }
  return out;
}

// Fixed order makes the wire deterministic. The generator projects this list into
// gauntlet.registry; classify_blame overwrites origin before reading it.
export const VERIFY_SLICE_FIELDS = ['id', 'file', 'line_start', 'line_end', 'description', 'evidence', 'severity', 'confidence', 'cross_file_refs', 'origin', 'removed_test'];

// Absent and null differ under Python .get() defaults. The join restores fields
// omitted here from the held finding.
export function projectVerifySliceFinding(finding) {
  const projected = {};
  for (const k of VERIFY_SLICE_FIELDS) {
    if (finding && Object.hasOwn(finding, k) && finding[k] !== undefined) projected[k] = finding[k];
  }
  return pinNumericFields(projected);
}

// Twin of gauntlet.jsjson.checksum_or_none. Preserve key order to detect an executor
// regenerating the document instead of copying it.
export function sliceInputChecksum(content) {
  return fnv1a32(JSON.stringify(content, null, 2));
}

// Hash the ASCII token to catch respelled numbers and Unicode escapes without
// cross-runtime collation or number-spelling rules.
export function sliceTokenChecksum(payload) {
  return fnv1a32(String(payload));
}

// Twin of gauntlet.verify.wire.build_deltas(): dispatched id order, DELTA_KEYS
// order, absent values omitted. The join also ignores keys outside DELTA_KEYS.
function canonicalDeltas(ids, byId) {
  return ids.map((id) => {
    const src = byId.get(id) || {};
    const out = {};
    for (const k of DELTA_KEYS) if (deltaHas(src, k)) out[k] = src[k];
    return out;
  });
}

// Fixtures use this canonicalisation; Python goldens provide an independent oracle.
export function deltaContentProof(ids, deltas) {
  return fnv1a32(JSON.stringify(canonicalDeltas(ids || [], deltasById(deltas)), null, 2));
}
