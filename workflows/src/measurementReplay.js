import { deepClone } from './wire.js';
import { stripTraceFields, appendBudgetedMeasurement } from './measurementTrace.js';

// The workflow sandbox has neither crypto nor a byte encoder; prompts are hashed as UTF-8.
function promptSha256(text) {
  const bytes = (encodeURIComponent(text.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]|[\uD800-\uDFFF]/g,
    (c) => c.length === 1 ? '\ufffd' : c)).match(/%[0-9A-F]{2}|[^%]/g) || [])
    .map((c) => c[0] === '%' ? parseInt(c.slice(1), 16) : c.charCodeAt(0));
  const bits = bytes.length * 8;
  bytes.push(0x80);
  while (bytes.length % 64 !== 56) bytes.push(0);
  for (let shift = 7; shift >= 0; shift--) bytes.push(Math.floor(bits / 2 ** (shift * 8)) & 255);
  const k = '428a2f9871374491b5c0fbcfe9b5dba53956c25b59f111f1923f82a4ab1c5ed5d807aa9812835b01243185be550c7dc372be5d7480deb1fe9bdc06a7c19bf174e49b69c1efbe47860fc19dc6240ca1cc2de92c6f4a7484aa5cb0a9dc76f988da983e5152a831c66db00327c8bf597fc7c6e00bf3d5a7914706ca63511429296727b70a852e1b21384d2c6dfc53380d13650a7354766a0abb81c2c92e92722c85a2bfe8a1a81a664bc24b8b70c76c51a3d192e819d6990624f40e3585106aa07019a4c1161e376c082748774c34b0bcb5391c0cb34ed8aa4a5b9cca4f682e6ff3748f82ee78a5636f84c878148cc7020890befffaa4506cebbef9a3f7c67178f2'.match(/.{8}/g).map((v) => parseInt(v, 16));
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
  const record = { replay_version: 2, args: deepClone(state.replayArgs || args), pipelineVersion: c.pipelineVersion, state: saved, persistence: [] };
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
  return appendBudgetedMeasurement(envelope,
    { ...envelope.measurement, replay: { status: 'available', text: JSON.stringify(record) } },
    { ...envelope.measurement, replay: { status: 'unavailable', reason: 'return_budget' } });
}
