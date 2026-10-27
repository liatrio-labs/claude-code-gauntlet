import { ANCHOR_LIMITS, LOCATION_RIGHTS } from './registry.js';

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
