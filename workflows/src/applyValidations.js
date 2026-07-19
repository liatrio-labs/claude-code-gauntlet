// applyValidations.js — JS twin of scripts/apply_validations.py:154-226
// (apply_validations). Merges validator confidence adjustments into a list
// of findings in place: matches by id, clamps confidence to [0, 100], sets
// original_confidence once (first validation only), and copies a truthy
// justification.

// Replicate Python int(): accepts a JS number (truncate toward zero, matching
// Python's int(float) truncation direction -- confirmed against the running
// interpreter: int(72.9) == 72, int(-0.5) == 0) or an integer-valued decimal
// string (Python strips surrounding whitespace and accepts a leading sign --
// int(" 72 ") == int("+72") == 72). REJECTS decimal strings like "72.9" and
// non-numeric strings (Python int("72.9") / int("abc") / int("") all raise
// ValueError). Returns null on reject; the caller skips the validation entry,
// mirroring Python's `except (TypeError, ValueError): ... continue`.
export function pyIntStrict(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? Math.trunc(v) : null;
  if (typeof v === 'string') {
    const s = v.trim();
    if (/^[+-]?\d+$/.test(s)) return parseInt(s, 10); // int-string only
    return null; // "72.9", "abc", "" all rejected
  }
  return null; // bool/None/object -> skip (see Task 6 self-review: Python's
  // int(True)/int(False) actually succeed as 1/0 rather than raising -- this
  // is a documented, fixture-uncovered divergence, not a Python-verified trap).
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
