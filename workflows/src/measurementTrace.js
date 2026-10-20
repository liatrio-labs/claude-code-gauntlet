import { deepClone, RETURN_CHAR_BUDGET } from './wire.js';
import { resolvedDispatchModels } from './registry.js';

export const MEASUREMENT_TRACE_VERSION = '1';
const traceState = new WeakMap();
const traceHistory = new WeakMap();
const replaySnapshots = new WeakMap();
const stageDecisions = new WeakMap();
const stageCapacity = new WeakMap();
const thresholdEvidence = new WeakMap();

export function createMeasurementTrace(nonce, previous = null) {
  const trace = { schema_version: 1, trace_version: MEASUREMENT_TRACE_VERSION, run_nonce: nonce,
    versions: [], snapshots: [], resolved_models: {}, complete: false };
  const states = new Map();
  const history = new Map();
  if (previous?.schema_version === 1 && previous.trace_version === MEASUREMENT_TRACE_VERSION && previous.run_nonce === nonce) {
    const held = deepClone(previous);
    const byVersion = new Map();
    for (const version of held.versions) {
      const finding = version.finding || { ...byVersion.get(version.base_version_id), ...version.set_fields };
      for (const key of version.unset_fields) delete finding[key];
      byVersion.set(version.version_id, finding);
      states.set(version.trace_id, { version_id: version.version_id, finding });
      history.set(JSON.stringify(finding), version.version_id);
    }
    trace.versions = held.versions;
    replaySnapshots.set(trace, held.snapshots);
    trace.resolved_models = held.resolved_models;
  }
  traceState.set(trace, states);
  traceHistory.set(trace, history);
  return trace;
}

export function measurementDecisions(output, decisions) {
  stageDecisions.set(output, deepClone(decisions));
  return output;
}

export function measurementThreshold(finding, field, value) {
  thresholdEvidence.set(finding, { field, value });
  return finding;
}

export function measurementCapacity(output, cap, cap_hit) {
  stageCapacity.set(output, { cap, cap_hit });
  return output;
}

export function traceDecision(finding, reason_code, reason, related = [], decision = 'eliminated') {
  return { trace_id: finding.trace_id, decision, reason_code, reason: reason || null, related_trace_ids: related };
}

export function eliminationDecisions(output) {
  const codes = { threshold: 'threshold', exclusion: 'exclusion', injection: 'injection',
    'suppressed:intentional': 'suppression', 'suppressed:generated': 'suppression',
    'challenge:removed': 'challenge_removed', 'challenge:downgraded': 'challenge_downgraded_removed' };
  return (output.eliminated || []).filter((f) => f.trace_id)
    .map((f) => traceDecision(f, codes[f.eliminated_by] || 'unknown', f.elimination_reason));
}

export function stampDiscovery(trace, finding, spec, dispatch, occurrence) {
  const out = finding && typeof finding === 'object' && !Array.isArray(finding) ? finding : { invalid_value: finding };
  delete out.trace_id;
  delete out.discovery_agent;
  delete out.discovery_dimension;
  out.trace_id = `${trace.run_nonce}:${dispatch}:${occurrence}`;
  out.discovery_agent = spec.agentType.split(':').pop();
  out.discovery_dimension = spec.dimensions.includes(out.dimension) ? out.dimension : 'unknown';
  return out;
}

function traceVersion(trace, finding) {
  if (!finding || typeof finding.trace_id !== 'string') return null;
  const states = traceState.get(trace);
  const previous = states.get(finding.trace_id);
  const copy = deepClone(finding);
  if (thresholdEvidence.has(finding)) copy.measurement_threshold = deepClone(thresholdEvidence.get(finding));
  const fingerprint = JSON.stringify(copy);
  const repeated = traceHistory.get(trace).get(fingerprint);
  if (repeated) { states.set(copy.trace_id, { version_id: repeated, finding: copy }); return repeated; }
  if (previous && JSON.stringify(previous.finding) === JSON.stringify(copy)) return previous.version_id;
  const version_id = `v${trace.versions.length}`;
  const set_fields = {};
  const unset_fields = [];
  if (previous) {
    for (const [key, value] of Object.entries(copy)) {
      if (JSON.stringify(value) !== JSON.stringify(previous.finding[key])) set_fields[key] = value;
    }
    for (const key of Object.keys(previous.finding)) if (!Object.hasOwn(copy, key)) unset_fields.push(key);
  }
  trace.versions.push({ version_id, trace_id: copy.trace_id, model_finding_id: typeof copy.id === 'string' ? copy.id : null,
    discovery_agent: copy.discovery_agent || 'unknown', discovery_dimension: copy.discovery_dimension || 'unknown',
    finding: previous ? null : copy, base_version_id: previous?.version_id || null, set_fields, unset_fields });
  states.set(copy.trace_id, { version_id, finding: copy });
  traceHistory.get(trace).set(fingerprint, version_id);
  return version_id;
}

export function recordStage(trace, stage, output, details = {}) {
  details = { ...(stageCapacity.get(output) || {}), ...details };
  const prior = replaySnapshots.get(trace)?.find((s) => s.stage === stage);
  if (details.replayed && prior) {
    trace.snapshots.push({ ...deepClone(prior), sequence: trace.snapshots.length, replayed: true });
    return;
  }
  const existing = trace.snapshots.findIndex((s) => s.stage === stage);
  if (existing >= 0 && details.replayed) { trace.snapshots[existing].replayed = true; return; }
  if (existing >= 0) trace.snapshots = trace.snapshots.slice(0, existing);
  const findings = Array.isArray(output) ? output : output.findings || output.filtered || [];
  const decisions = [...(stageDecisions.get(output) || []), ...(details.decisions || [])];
  // Removed states remain addressable without being counted as surviving claims.
  for (const finding of details.removed || output.eliminated || []) traceVersion(trace, finding);
  trace.snapshots.push({ sequence: trace.snapshots.length, stage,
    finding_version_ids: findings.map((f) => traceVersion(trace, f)).filter((id) => id !== null),
    decisions: deepClone(decisions), executed: details.executed !== false, replayed: !!details.replayed,
    coverage: details.coverage || (details.replayed ? 'partial' : 'complete'),
    dispatched_dimensions: deepClone(details.dispatched_dimensions || []), degraded_dimensions: deepClone(details.degraded_dimensions || []),
    cap: details.cap ?? null, cap_hit: !!details.cap_hit });
}

export function finishMeasurementTrace(trace, selection) {
  recordStage(trace, 'deliver', selection);
  trace.complete = true;
  return deepClone(trace);
}

export function traceModels(trace, policy) {
  trace.resolved_models = resolvedDispatchModels(policy);
}

// JSON is escaped to ASCII so byte hashes need no host text encoder.
export function measurementText(trace) {
  return JSON.stringify(trace).replace(/[\u007f-\uffff]/g, (ch) => `\\u${ch.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

const SHA256_K = [
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
];
const rotateHash = (x, n) => (x >>> n) | (x << (32 - n));
export function measurementHash(text) {
  const bytes = [...text].map((ch) => ch.charCodeAt(0));
  if (bytes.some((byte) => byte > 127)) throw new Error('measurement hash requires ASCII serialization');
  const bits = bytes.length * 8;
  bytes.push(128);
  while (bytes.length % 64 !== 56) bytes.push(0);
  for (let i = 7; i >= 0; i -= 1) bytes.push(Math.floor(bits / 2 ** (i * 8)) & 255);
  const hash = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
  for (let offset = 0; offset < bytes.length; offset += 64) {
    const words = [];
    for (let i = 0; i < 16; i += 1) words.push((bytes[offset+i*4]<<24)|(bytes[offset+i*4+1]<<16)|(bytes[offset+i*4+2]<<8)|bytes[offset+i*4+3]);
    for (let i = 16; i < 64; i += 1) {
      const x = words[i-15], y = words[i-2];
      words[i] = (words[i-16] + (rotateHash(x,7)^rotateHash(x,18)^(x>>>3)) + words[i-7] + (rotateHash(y,17)^rotateHash(y,19)^(y>>>10))) | 0;
    }
    let [a,b,c,d,e,f,g,h] = hash;
    for (let i = 0; i < 64; i += 1) {
      const t1 = (h + (rotateHash(e,6)^rotateHash(e,11)^rotateHash(e,25)) + ((e&f)^(~e&g)) + SHA256_K[i] + words[i]) | 0;
      const t2 = ((rotateHash(a,2)^rotateHash(a,13)^rotateHash(a,22)) + ((a&b)^(a&c)^(b&c))) | 0;
      [h,g,f,e,d,c,b,a] = [g,f,e,(d+t1)|0,c,b,a,(t1+t2)|0];
    }
    [a,b,c,d,e,f,g,h].forEach((value, i) => { hash[i] = (hash[i] + value) | 0; });
  }
  return hash.map((value) => (value >>> 0).toString(16).padStart(8,'0')).join('');
}

export function appendMeasurement(envelope, trace, args) {
  const text = measurementText(trace);
  const entry = { path: `${args.outputDir}/code-gauntlet-measurement-${args.headShaShort}.json`, text, sha256: measurementHash(text) };
  const { persistReturn: existingChannel, ...primary } = envelope;
  const channel = existingChannel || { channel: 'return', nonce: args.nonce || null, planPath: null, entries: [] };
  const candidate = { ...primary, measurement: { status: 'available', path: entry.path, sha256: entry.sha256 },
    persistReturn: { ...channel, entries: [...channel.entries, entry] } };
  if (JSON.stringify(candidate).length > RETURN_CHAR_BUDGET) {
    return { ...envelope, measurement: { status: 'unavailable', reason: 'return_budget' } };
  }
  return candidate;
}
