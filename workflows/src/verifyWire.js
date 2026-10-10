// The verify wire: the inline slice encoding, the slice projection, the delta join and the
// checksums that prove each side of a dispatch. Holds no dispatch code, so the planner and
// the stage can both import it.
import { INT_POLICY, coerceInt, fnv1a32 } from './wire.js';

// The slice travels as one percent-encoded, shell-inert token. The alphabet is narrower
// than shellWord's safe class: JSON punctuation remains in the outer document, but no
// encoded string can contain a shell operator, quote or escape-bearing character.
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

// UTF-16 is walked deliberately: a well-formed surrogate pair becomes its four UTF-8
// bytes, while a lone surrogate becomes %uXXXX so Python can restore the exact code unit
// instead of replacing it.
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

// Compact JSON of printable ASCII with no apostrophe. The postcondition is the shell
// safety belt: shellWord can wrap the whole document in one POSIX single-quoted token.
export function encodeSliceInline(content) {
  const encoded = JSON.stringify(encodeInlineValue(content));
  if (typeof encoded !== 'string' || !VERIFY_INLINE_PRINTABLE_RE.test(encoded)) {
    throw new Error('encodeSliceInline produced a non-printable or quoted payload');
  }
  return encoded;
}

// The largest payloads measured to copy exactly were 43,636 and 51,744 chars (2/2 sonnet
// executors at each size); anything above 51,744 is unmeasured, so the budget is pinned
// just under that measurement (the rest of the command adds ~300 chars). Linux
// MAX_ARG_STRLEN is 131,072, far above this.
export const VERIFY_INLINE_CHAR_BUDGET = 50000;

// The canonical key order of one delta, and the only keys that carry meaning. The checksum
// canonicalisation and the join derive from this list, the generator projects the Python
// delta value fields from it, and the dispatch schema is written beside it by hand. These
// are the script's downstream-visible writes (gauntlet.verify.decide): origin from
// classify_blame and validate_diff_lines, severity from their downgrade, confidence from
// verify_factual, elimination_reason from run_verification. The merge-added `agent` is not
// one: the script never writes it, and the join keeps it from the dispatched copy.
export const DELTA_KEYS = ['id', 'verified', 'origin', 'severity', 'confidence', 'elimination_reason'];

// The delta keys that carry a value onto a finding.
const DELTA_VALUE_KEYS = DELTA_KEYS.filter((k) => k !== 'id' && k !== 'verified');

export const deltaHas = (d, k) => d[k] !== undefined && d[k] !== null;

// Keep exact id text: trimming would miss padded ids and collide distinct findings.
function deltasById(deltas) {
  const byId = new Map();
  for (const d of Array.isArray(deltas) ? deltas : []) {
    if (d && typeof d.id === 'string') byId.set(d.id, d);
  }
  return byId;
}

// Walks the dispatched slice, never the echo: order, membership and every untouched field
// come from data this stage already holds. A finding whose delta says verified:false was
// eliminated by the script and is omitted.
//
// Call only after trustSlice accepts the deltas: it proves every dispatched id has exactly
// one boolean `verified`. Without that, a finding with no delta is silently dropped here.
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

// The fields verify_findings.py does arithmetic on, pinned to real numbers before inline
// encoding, on the join and on the degraded path, mirroring
// gauntlet.verify.wire.coerce_numeric_fields. A quoted "153" would make the script's line
// arithmetic raise and degrade the slice, and a leaked "85" makes the filter's consensus
// boost concatenate ("85" + 10 -> "8510"). A fraction is rounded because line fields are
// not in the delta: the join would keep the fraction while verification ran against the
// rounded value. Whatever the verify policy refuses is left for the script's own guards.
const VERIFY_NUMERIC_FIELDS = ['line_start', 'line_end', 'line', 'end_line', 'confidence'];
export function pinNumericFields(finding) {
  const out = { ...finding };
  for (const k of VERIFY_NUMERIC_FIELDS) {
    const n = coerceInt(out[k], INT_POLICY.verify);
    if (n !== null) out[k] = n;
  }
  return out;
}

// The fields verify_findings.py consults on a dispatched slice, in the fixed order that
// makes the serialized key order deterministic (not the order the script reads them in).
// The generator projects this list into gauntlet.registry, and the verifier read-site test
// checks it against the Python source. `origin` is tolerated for forward compatibility:
// classify_blame overwrites it before every read site.
export const VERIFY_SLICE_FIELDS = ['id', 'file', 'line_start', 'line_end', 'description', 'evidence', 'severity', 'confidence', 'cross_file_refs', 'origin'];

// Absent fields stay absent, never null: an omitted key and an explicit null are different
// signals to the script's `.get()` defaults. A field dropped here loses nothing, because
// the join rebuilds each finding from the in-memory copy. The numeric pin is the one the
// join applies, so a slice's dispatched numbers and its in-memory numbers never diverge.
export function projectVerifySliceFinding(finding) {
  const projected = {};
  for (const k of VERIFY_SLICE_FIELDS) {
    if (finding && Object.hasOwn(finding, k) && finding[k] !== undefined) projected[k] = finding[k];
  }
  return pinNumericFields(projected);
}

// The value proof over the dispatched slice document, and the one site that spells this
// canonical form; gauntlet.jsjson.checksum_or_none is its Python twin. Unsorted on
// purpose: the document is built and read back in VERIFY_SLICE_FIELDS order, so one that
// returns in another key order was regenerated, not copied, and sorting would forgive that.
export function sliceInputChecksum(content) {
  return fnv1a32(JSON.stringify(content, null, 2));
}

// The byte proof over the inline token: the exact characters the executor was asked to
// reproduce, so it catches what the value proof cannot see, such as a re-spelled number
// or an astral character rewritten as an escaped surrogate pair. The token is printable
// ASCII (encodeSliceInline's postcondition), so this proof needs no collation, escaping
// or number-spelling contract in either runtime.
export function sliceTokenChecksum(payload) {
  return fnv1a32(String(payload));
}

// The deltas in a form both runtimes spell identically: rebuilt in dispatched id order,
// keys in DELTA_KEYS order, absent values omitted, as gauntlet.verify.wire.build_deltas()
// emits them. The echo's array order, key order and invented fields cannot move the
// checksum; only the values the script decided can. The proof is therefore blind to a key
// outside DELTA_KEYS, which is not a hole: the join copies only DELTA_VALUE_KEYS, so a key
// the proof ignores is a key nothing reads.
function canonicalDeltas(ids, byId) {
  return ids.map((id) => {
    const src = byId.get(id) || {};
    const out = {};
    for (const k of DELTA_KEYS) if (deltaHas(src, k)) out[k] = src[k];
    return out;
  });
}

// Exported so envelope fixtures use the real canonicalisation instead of a duplicate
// that could agree with the same bug. Python-produced goldens pin the other runtime.
export function deltaContentProof(ids, deltas) {
  return fnv1a32(JSON.stringify(canonicalDeltas(ids || [], deltasById(deltas)), null, 2));
}
