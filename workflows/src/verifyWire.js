// The verify wire: the inline slice encoding, the slice projection, the delta join and the
// checksums that prove each side of a dispatch. Holds no dispatch code, so the planner and
// the stage can both import it.
import { INT_POLICY, coerceInt, fnv1a32 } from './wire.js';

// The verify boundary carries the slice as one percent-encoded, shell-inert token. The
// alphabet is intentionally narrower than shellWord's safe class: JSON punctuation remains
// in the outer document, but no encoded string can contain a shell operator, quote, or
// escape-bearing character.
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

// encodeInlineString(s) -> a JSON-string-safe value with no JSON or shell escapes.
// UTF-16 is walked deliberately: a well-formed surrogate pair becomes its four UTF-8
// bytes, while a lone surrogate is represented as %uXXXX so Python can restore the exact
// code unit rather than replacing it. A safe code unit passes through unchanged.
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

// encodeSliceInline(content) -> compact JSON whose only non-JSON punctuation is from
// printable ASCII excluding apostrophe. This post-condition is the shell safety belt:
// shellWord can therefore wrap the complete document in one POSIX single-quoted token.
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

// The canonical key order of one delta, and the ONLY keys that carry meaning. The checksum
// canonicalisation and the join derive from this list; the dispatch schema is written
// beside it by hand. The generator projects the Python delta value fields from this list.
// Audit against gauntlet.verify.decide's assignments: origin is set by
// classify_blame and validate_diff_lines; severity by their downgrade;
// confidence by verify_factual; elimination_reason by run_verification.
// No deletion path mutates a finding. These are the downstream-visible writes.
// blame_metadata, factual_verification, and diff_validation remain only in the
// on-disk audit trail; the finding schema does not declare them. The merge-added
// `agent` is not a delta key: the script never writes it, and the join keeps it from the dispatched copy.
export const DELTA_KEYS = ['id', 'verified', 'origin', 'severity', 'confidence', 'elimination_reason'];

// The delta keys that carry a VALUE onto a finding (DELTA_KEYS minus the two structural
// ones). Iterated in the same fixed order the canonicalisation uses.
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

// Walks the DISPATCHED slice, never the echo: order, membership and every untouched field
// (the merge-added `agent` among them) come from data this stage already holds. A finding
// whose delta says verified:false was eliminated by the script and is omitted.
//
// Call only after trustSlice accepts the deltas: it proves every dispatched id has exactly
// one boolean `verified`. Skipping that coverage check would silently drop findings with
// no delta; retaining them here would falsely deliver unverified findings as verified.
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

// Numeric finding fields that verify_findings.py does arithmetic on (line_start - 1, line
// comparisons), pinned to real numbers before inline encoding, on the join and on the
// degraded path, mirroring gauntlet.verify.wire.coerce_numeric_fields. A quoted "153"
// would make receipt-path arithmetic raise and degrade the whole slice to UNVERIFIED, and
// a leaked "85" makes the filter's consensus boost concatenate ("85" + 10 -> "8510"). A
// fractional number is rounded half-up because line fields are not in the delta: the join
// would otherwise keep the fractional value while verification ran against the rounded
// one. A delta that carries a script-decided confidence overwrites the rounded one
// afterward. Whatever the verify policy refuses is left alone so the script's own guards
// still fire.
const VERIFY_NUMERIC_FIELDS = ['line_start', 'line_end', 'line', 'end_line', 'confidence'];
export function pinNumericFields(finding) {
  const out = { ...finding };
  for (const k of VERIFY_NUMERIC_FIELDS) {
    const n = coerceInt(out[k], INT_POLICY.verify);
    if (n !== null) out[k] = n;
  }
  return out;
}

// The inline slice projection: the fields verify_findings.py consults on a
// dispatched slice, walked in this fixed order so the serialized key order is
// deterministic — NOT the order the script reads them in (classify_blame's reads come
// first there and don't match this order). The generator projects this list into
// gauntlet.registry; the verifier read-site test checks it against Python source.
// `origin` is tolerated forward-compat even though classify_blame overwrites it
// before every read site.
export const VERIFY_SLICE_FIELDS = ['id', 'file', 'line_start', 'line_end', 'description', 'evidence', 'severity', 'confidence', 'cross_file_refs', 'origin'];

// projectVerifySliceFinding(finding) -> a finding narrowed to VERIFY_SLICE_FIELDS, in that
// key order, with absent fields left absent (never written as null — an omitted key and an
// explicit null are different signals to the script's own `.get()` defaults). The delta
// echo (joinVerifyDeltas) rebuilds every verified finding from the workflow's OWN in-memory
// copy, never from this projection, so a field dropped here loses nothing downstream. The
// numeric pin is the one applied to the full finding, so a slice's on-disk numbers and its
// in-memory numbers never diverge.
export function projectVerifySliceFinding(finding) {
  const projected = {};
  for (const k of VERIFY_SLICE_FIELDS) {
    if (finding && Object.hasOwn(finding, k) && finding[k] !== undefined) projected[k] = finding[k];
  }
  return pinNumericFields(projected);
}

// sliceInputChecksum(content) -> the VALUE proof over the dispatched slice document.
// The ONE site that spells this canonical form; gauntlet.jsjson.checksum_or_none is
// its Python twin. Deliberately unsorted: the document is built in VERIFY_SLICE_FIELDS
// order by projectVerifySliceFinding, the script reads it back in that order, and a
// document that comes back in a different shape is a regenerated token rather than a
// copied one. Sorting would forgive exactly that regeneration while leaving the proof's
// array-order, extra-field and number-spelling sensitivities untouched.
export function sliceInputChecksum(content) {
  return fnv1a32(JSON.stringify(content, null, 2));
}

// sliceTokenChecksum(payload) -> the BYTE proof over the inline token itself.
// It covers the exact characters the executor was asked to reproduce, so it catches every
// alteration the value proof can miss — key transposition, a findings-array or
// cross_file_refs permutation, an invented field, a re-spelled number, and an astral
// character rewritten as an escaped surrogate pair. The decoder rejects that spelling by
// name; independently constructed documents containing an astral character or its
// surrogate pair still serialize identically for the value proof. The token is printable
// ASCII by construction (encodeSliceInline's postcondition), so this proof needs no
// collation, escaping or number-spelling contract in either runtime.
export function sliceTokenChecksum(payload) {
  return fnv1a32(String(payload));
}

// The deltas in a form both runtimes spell identically.
// Rebuilt from the DISPATCHED id order, one object per id, keys in DELTA_KEYS order,
// absent values omitted — so the echo's own array order, key order, and any field it
// invented cannot move the checksum. Only the VALUES the script decided can.
// gauntlet.verify.wire.build_deltas() emits exactly this shape in exactly this order.
//
// The rebuild therefore also makes the proof BLIND to any key outside DELTA_KEYS. That is
// deliberate and not a hole: joinVerifyDeltas copies only DELTA_VALUE_KEYS, so a key the
// proof ignores is a key nothing reads — and VERIFY_SCHEMA is deliberately OPEN, so an
// undeclared key genuinely can arrive. Covering it would buy no protection and would cost
// the order- and noise-tolerance that keeps a harmless echo quirk from degrading a slice.
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
