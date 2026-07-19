// applyValidations.js — JS twin of scripts/apply_validations.py:154-226
// (apply_validations). Merges validator confidence adjustments into a list
// of findings in place: matches by id, clamps confidence to [0, 100], sets
// original_confidence once (first validation only), and copies a truthy
// justification.

// Replicate Python int(): accepts a JS number (truncate toward zero, matching
// Python's int(float) truncation direction -- confirmed against the running
// interpreter: int(72.9) == 72, int(-0.5) == 0), an integer-valued decimal
// string (Python strips surrounding whitespace and accepts a leading sign --
// int(" 72 ") == int("+72") == 72), or a JS boolean (Python's bool is an int
// subclass, so int(True) == 1 and int(False) == 0 -- verified against the
// running interpreter; apply_validations.py:196-204 calls int() with no type
// gate, so a validator emitting `"confidence": true` is applied as 1 in
// Python and must be too here). REJECTS decimal strings like "72.9" and
// non-numeric strings (Python int("72.9") / int("abc") / int("") all raise
// ValueError). Returns null on reject; the caller skips the validation entry,
// mirroring Python's `except (TypeError, ValueError): ... continue`.
export function pyIntStrict(v) {
  if (typeof v === 'boolean') return v ? 1 : 0;
  if (typeof v === 'number') return Number.isFinite(v) ? Math.trunc(v) : null;
  if (typeof v === 'string') {
    const s = v.trim();
    if (/^[+-]?\d+$/.test(s)) return parseInt(s, 10); // int-string only
    return null; // "72.9", "abc", "" all rejected
  }
  return null; // None/object -> skip (int(None) raises TypeError in Python;
  // a plain object has no int() coercion path either).
}

export function applyValidations(findings, validations) {
  // Build id -> finding index for O(n) lookup (matches Python's finding_by_id).
  const findingById = new Map();
  for (const finding of findings) {
    const fid = 'id' in finding ? finding.id : undefined;
    if (fid !== null && fid !== undefined) findingById.set(fid, finding);
  }

  let adjustedCount = 0;
  const unmatchedIds = [];

  for (const validation of validations) {
    const vid = 'id' in validation ? validation.id : undefined;
    if (vid === null || vid === undefined) continue; // missing id -- skipped (warning)

    const rawConf = 'confidence' in validation ? validation.confidence : undefined;
    if (rawConf === null || rawConf === undefined) continue; // missing confidence -- skipped (warning)

    const parsed = pyIntStrict(rawConf);
    if (parsed === null) continue; // non-integer confidence -- skipped (warning)

    const newConf = Math.max(0, Math.min(100, parsed));

    const finding = findingById.get(vid);
    if (finding === undefined) {
      unmatchedIds.push(vid);
      continue;
    }

    // Save original_confidence before updating (only on first validation).
    if (!('original_confidence' in finding)) {
      finding.original_confidence = 'confidence' in finding ? finding.confidence : 0;
    }
    finding.validator_confidence = newConf;
    finding.confidence = newConf;

    const justification = 'justification' in validation ? validation.justification : undefined;
    if (justification) finding.validation_justification = justification;

    adjustedCount += 1;
  }

  return { adjustedCount, unmatchedIds };
}
