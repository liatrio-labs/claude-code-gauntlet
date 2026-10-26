import { normalizeLocation } from './location.js';
import { deepClone, firstUnsafeNumber, fnv1a32, normalizeForChecksum } from './wire.js';

// Aliases let persisted findings serve consumers of both field vocabularies.
const PERSIST_ALIASES = [['line', 'line_start'], ['end_line', 'line_end'], ['body', 'description']];

function toV2Aliased(f) {
  const out = normalizeLocation(f);
  for (const [alias, canonical] of PERSIST_ALIASES) {
    if (alias === 'body') {
      if (out.body === undefined && out.description !== undefined) out.body = out.description;
    } else if (out[canonical] !== undefined) out[alias] = out[canonical];
    else delete out[alias];
  }
  return out;
}

export function postReviewWrapper(id, reviewBody) {
  return {
    owner: id.owner,
    repo: id.repo,
    pr_number: id.pr_number,
    sha: id.sha_full,
    platform: id.platform,
    web_origin: id.web_origin,
    review_body: typeof reviewBody === 'string' ? reviewBody : '',
  };
}

// Returns the by-value writer payload with aliases and an optional PR envelope.
// The caller supplies the selected postReview set so persistence preserves delivery selection.
export function writerPayload(inp) {
  const postReviewSet = (inp.postReview || []).map(toV2Aliased);
  const id = inp.prIdentity;
  return {
    findings: (inp.findings || []).map(toV2Aliased),
    // The envelope carries the reviewed commit and resolved platform without remote inference.
    postReview: id
      ? { ...postReviewWrapper(id, inp.reviewBody), findings: postReviewSet }
      : postReviewSet,
    report: inp.report || '',
    checkpoints: inp.checkpoints || {},
  };
}

// Stripping appended aliases restores canonical values and key order for checkpoint derivation.
const PERSIST_ALIAS_FIELDS = PERSIST_ALIASES.map(([alias]) => alias);

function stripPersistAliases(f) {
  const out = {};
  for (const [k, v] of Object.entries(f)) {
    if (!PERSIST_ALIAS_FIELDS.includes(k)) out[k] = v;
  }
  return out;
}

// Returns escape-hardened JSON text, passing non-strings through.
// The caller passes serializer output so replacement consumes whole escapes without changing values.
// Unicode escapes avoid adjacent backslashes that a model writer can collapse.
export function hardenEscapeRuns(json) {
  return typeof json === 'string' ? json.replace(/\\\\/g, '\\u005c') : json;
}

// Returns { findingsJson, reportMd } as the strings to persist.
// Hardening precedes checksumming so the proof describes the bytes sent to the writer.
// Markdown stays verbatim because JSON escape spelling would change its content.
export function persistPrimaries(inp) {
  return {
    findingsJson: hardenEscapeRuns(JSON.stringify((inp.findings || []).map(toV2Aliased), null, 2)),
    reportMd: inp.report || '',
  };
}

// Returns the persistence plan without mutating the caller's checkpoints.
// Explicit id lists preserve the caller's selected subsets and ordering across derivation.
export function persistPlan(inp, paths) {
  const { findingsJson, reportMd } = persistPrimaries(inp);
  const id = inp.prIdentity;
  const skeleton = deepClone(inp.checkpoints || {});
  const challenge = (skeleton.phases || {}).challenge;
  let challengeFindingIds = [];
  if (challenge && Array.isArray(challenge.findings)) {
    challengeFindingIds = challenge.findings.map((f) => f && f.id);
    // Emptying rather than deleting preserves the key position for the derived checkpoint proof.
    challenge.findings = [];
  }
  const expectOf = (path, text) => {
    const normalized = normalizeForChecksum(text);
    return { path, chars: normalized.length, checksum: fnv1a32(normalized) };
  };
  // Derived-content expectations come from the held payload so path presence cannot stand in for content.
  // They stay unhardened: the assembler writes these documents with its own serializer, not through the writer.
  const held = writerPayload(inp);
  const plan = {
    planVersion: 2,
    expect: [expectOf(paths.findings, findingsJson), expectOf(paths.report, reportMd)],
    derive: [
      expectOf(paths.postReview, JSON.stringify(held.postReview, null, 2)),
      expectOf(paths.checkpoints, JSON.stringify(held.checkpoints, null, 2)),
    ],
    postReview: {
      path: paths.postReview,
      source: paths.findings,
      ids: (inp.postReview || []).map((f) => f && f.id),
      wrapper: id ? postReviewWrapper(id, inp.reviewBody) : null,
    },
    checkpoint: {
      path: paths.checkpoints,
      source: paths.findings,
      challengeFindingIds,
      stripAliasFields: PERSIST_ALIAS_FIELDS,
      skeleton,
    },
  };
  // The plan controls selection, so self-proof must detect dropped ids as well as changed payloads.
  // Appending the checksum last lets its removal recover the exact object used for self-proof.
  plan.planChecksum = fnv1a32(JSON.stringify(plan, null, 2));
  return plan;
}

// Returns { ok: true } or { ok: false, reason } for faithful id-based derivation.
// Alias stripping must preserve values and key order; refusal lets writeArtifacts use the by-value writer.
export function persistDerivable(inp) {
  // The Python serializer accepts only safe integers for byte-identical derivation.
  for (const [label, value] of [
    ['findings', inp.findings],
    ['postReview', inp.postReview],
    ['checkpoints', inp.checkpoints],
    ['prIdentity', inp.prIdentity],
  ]) {
    if (value === undefined || value === null) continue;
    const bad = firstUnsafeNumber(value, label);
    if (bad !== null) {
      return { ok: false, reason: `${bad} is not a JS-safe integer — the Python assembler cannot spell it byte-identically, so the derived artifact would diverge` };
    }
  }
  const findings = inp.findings || [];
  const byId = new Map();
  for (let i = 0; i < findings.length; i += 1) {
    const f = findings[i];
    const fid = f && f.id;
    if (typeof fid !== 'string' || fid === '') {
      return { ok: false, reason: `finding at index ${i} has no usable string id` };
    }
    if (byId.has(fid)) return { ok: false, reason: `duplicate finding id ${fid}` };
    byId.set(fid, f);
  }
  const twinCheck = (list, label, project) => {
    for (let i = 0; i < list.length; i += 1) {
      const entry = list[i];
      const fid = entry && entry.id;
      if (typeof fid !== 'string' || !byId.has(fid)) {
        return `${label} entry at index ${i} (id ${fid === undefined ? 'missing' : fid}) is not present in the findings set`;
      }
      if (JSON.stringify(project(entry)) !== JSON.stringify(project(byId.get(fid)))) {
        return `${label} entry ${fid} differs from the findings entry with the same id`;
      }
    }
    return null;
  };
  const postReviewBad = twinCheck(inp.postReview || [], 'postReview', (f) => {
    const view = toV2Aliased(f);
    return { ...stripPersistAliases(view), ...(view.body !== undefined ? { body: view.body } : {}) };
  });
  if (postReviewBad) return { ok: false, reason: postReviewBad };
  const challenge = ((inp.checkpoints || {}).phases || {}).challenge;
  const challengeFindings = challenge && Array.isArray(challenge.findings) ? challenge.findings : [];
  const challengeBad = twinCheck(challengeFindings, 'checkpoint challenge', (f) => stripPersistAliases(toV2Aliased(f)));
  if (challengeBad) return { ok: false, reason: challengeBad };
  // The assembler strips aliases only; any other normalization needs the by-value writer.
  for (const f of challengeFindings) {
    if (JSON.stringify(stripPersistAliases(toV2Aliased(byId.get(f.id)))) !== JSON.stringify(f)) {
      return { ok: false, reason: `checkpoint challenge entry ${f.id} requires normalization before derivation` };
    }
  }
  return { ok: true };
}
