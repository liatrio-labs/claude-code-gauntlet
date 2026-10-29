import { deepClone, RETURN_CHAR_BUDGET } from './wire.js';
import { stripTraceFields } from './measurementTrace.js';

// The workflow sandbox has neither crypto nor a byte encoder; prompts are hashed as UTF-8.
function promptSha256(text) {
  const bytes = [];
  for (const char of text) {
    let code = char.codePointAt(0);
    if (code >= 0xd800 && code <= 0xdfff) code = 0xfffd;
    if (code < 0x80) bytes.push(code);
    else if (code < 0x800) bytes.push(0xc0 | (code >> 6), 0x80 | (code & 63));
    else if (code < 0x10000) bytes.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 63), 0x80 | (code & 63));
    else bytes.push(0xf0 | (code >> 18), 0x80 | ((code >> 12) & 63), 0x80 | ((code >> 6) & 63), 0x80 | (code & 63));
  }
  const bits = bytes.length * 8;
  bytes.push(0x80);
  while (bytes.length % 64 !== 56) bytes.push(0);
  for (let shift = 7; shift >= 0; shift--) bytes.push(Math.floor(bits / 2 ** (shift * 8)) & 255);
  const k = [0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2];
  const hash = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
  const rotate = (word, n) => (word >>> n) | (word << (32 - n));
  for (let offset = 0; offset < bytes.length; offset += 64) {
    const w = [];
    for (let i = 0; i < 16; i++) w[i] = (bytes[offset + 4 * i] << 24) | (bytes[offset + 4 * i + 1] << 16) | (bytes[offset + 4 * i + 2] << 8) | bytes[offset + 4 * i + 3];
    for (let i = 16; i < 64; i++) {
      const x = w[i - 15], y = w[i - 2];
      w[i] = (w[i - 16] + (rotate(x, 7) ^ rotate(x, 18) ^ (x >>> 3)) + w[i - 7] + (rotate(y, 17) ^ rotate(y, 19) ^ (y >>> 10))) | 0;
    }
    let [a, b, c, d, e, f, g, h] = hash;
    for (let i = 0; i < 64; i++) {
      const t1 = (h + (rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)) + ((e & f) ^ (~e & g)) + k[i] + w[i]) | 0;
      const t2 = ((rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
      [h, g, f, e, d, c, b, a] = [g, f, e, (d + t1) | 0, c, b, a, (t1 + t2) | 0];
    }
    [a, b, c, d, e, f, g, h].forEach((word, i) => { hash[i] = (hash[i] + word) | 0; });
  }
  return hash.map((word) => (word >>> 0).toString(16).padStart(8, '0')).join('');
}

export function captureDeliveryReplay(c, args, state) {
  const { limits, gaps, completed, phaseOutputs, resolvedReview, reviewMdSubtrees, derivedAgentFlags, phaseReached } = state;
  const saved = deepClone({ limits, gaps, completed, phaseOutputs, resolvedReview, reviewMdSubtrees, derivedAgentFlags, phaseReached });
  stripTraceFields(saved);
  const record = { args: deepClone(state.replayArgs || args), pipelineVersion: c.pipelineVersion, state: saved, persistence: [] };
  const dispatch = c.agent;
  return { record, ctx: { ...c, agent: async (prompt, options) => {
    const row = { label: options.label, agent_type: options.agentType, prompt_sha256: promptSha256(prompt), result: null, error: null };
    record.persistence.push(row);
    try {
      const result = await dispatch(prompt, options);
      row.result = result === undefined ? null : deepClone(result);
      return result;
    } catch (error) { row.error = error?.message || String(error); throw error; }
  } } };
}

export function appendDeliveryReplay(envelope, record) {
  if (!record) return envelope;
  const candidate = { ...envelope, measurement: { ...envelope.measurement, replay: { status: 'available', text: JSON.stringify(record) } } };
  if (JSON.stringify(candidate).length <= RETURN_CHAR_BUDGET) return candidate;
  const fallback = { ...envelope, measurement: { ...envelope.measurement, replay: { status: 'unavailable', reason: 'return_budget' } } };
  return JSON.stringify(fallback).length <= RETURN_CHAR_BUDGET ? fallback : envelope;
}
