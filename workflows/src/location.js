import { ANCHOR_LIMITS, LOCATION_RIGHTS, DISPOSITION_RIGHTS } from './registry.js';
import { coerceInt, INT_POLICY } from './wire.js';

export function lineBucket(line, proximity) {
  const n = coerceInt(line, INT_POLICY.filter);
  if (n === null) return 0;
  const value = n / proximity;
  const floor = Math.floor(value);
  return (value - floor === 0.5 ? (floor % 2 === 0 ? floor : floor + 1) : Math.round(value)) * proximity;
}

export function canonicalRange(finding) {
  if (!['confirmed', 'relocated'].includes(finding?.location_status)) return null;
  const start = finding.line_start;
  const end = finding.line_end;
  return Number.isSafeInteger(start) && start > 0 && Number.isSafeInteger(end) && end > 0
    && end >= start && end - start + 1 <= ANCHOR_LIMITS.lines ? [start, end] : null;
}

export function locationAllows(finding, right) {
  const status = canonicalRange(finding) ? finding.location_status : 'unconfirmed';
  return LOCATION_RIGHTS[status][right] === true;
}

export function deliveryAllows(finding, right) {
  let stamp = finding?.disposition;
  const reasons = finding?.disposition_reasons;
  if (typeof stamp !== 'string' || !Object.hasOwn(DISPOSITION_RIGHTS, stamp)
    || (reasons !== undefined && (!Array.isArray(reasons) || !reasons.every((reason) => typeof reason === 'string')))) {
    stamp = 'held_back';
  }
  const rights = DISPOSITION_RIGHTS[stamp];
  if (right === 'report') return rights.report === 'finding';
  if (right === 'unverified' || right === 'eliminated') return rights.report === right;
  if (finding.admission?.version !== 1 || finding.admission.status !== 'admitted' || typeof finding.admission.input_proof !== 'string') return false;
  if (stamp === 'body' && right === 'fixTask' && finding.body_fix_task_eligible !== true) return false;
  return rights[right] === true && locationAllows(finding, right);
}

// Claims survive clearing so replay and persistence cannot turn hints into rights.
export function clearLocation(finding) {
  const out = { ...finding, location_status: 'unconfirmed' };
  for (const [canonical, claimed] of [['line_start', 'claimed_line_start'], ['line_end', 'claimed_line_end']]) {
    if (!Object.hasOwn(out, claimed) && Object.hasOwn(out, canonical)) out[claimed] = out[canonical];
    delete out[canonical];
  }
  delete out.line;
  delete out.end_line;
  return out;
}

export function normalizeLocation(finding, request = false) {
  if (request || !canonicalRange(finding)) {
    const out = clearLocation(finding);
    if (request) {
      for (const key of ['line_start', 'line_end']) if (Object.hasOwn(finding, key)) out[key] = finding[key];
    }
    return out;
  }
  const out = { ...finding };
  out.line = out.line_start;
  out.end_line = out.line_end;
  return out;
}
