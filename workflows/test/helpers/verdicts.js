import { citeChecksum, citeSource } from '../../src/citeWire.js';
import { fnv1a32 } from '../../src/wire.js';

export const sourceCapture = { version: 1, nonce: 'run', head: 'a'.repeat(40), base: 'b'.repeat(40),
  diff_sha256: 'c'.repeat(64), manifest_sha256: 'd'.repeat(64), target_kind: 'branch',
  copied_files: 0, copied_bytes: 0, failure_count: 0, failure_sample: [] };

export function skepticalReturn(id, stage = 'validator', extra = {}) {
  return { finding_id: id, stage, verdict: 'real', scope_assessments: [],
    ...(stage === 'validator' ? { confidence: 90, policy_subject: 'ordinary' } : {}),
    citations: [{ finding_id: id, stage, request_id: 'claim', role: 'claim',
      snapshot_id: sourceCapture.manifest_sha256, file: `${id}.js`, side: 'head',
      line_start: 10, line_end: 10, quote: 'return process(value);' }], ...extra };
}

export function citationEnvelope(prompt, args, status = () => 'matched') {
  const token = prompt.match(/--input-inline '([^']*)'/)[1];
  const decode = (v) => typeof v === 'string' ? decodeURIComponent(v) : Array.isArray(v) ? v.map(decode)
    : v && typeof v === 'object' ? Object.fromEntries(Object.entries(v).map(([k, value]) => [decodeURIComponent(k), decode(value)])) : v;
  const document = decode(JSON.parse(token));
  const rows = document.requests.map((r) => ({ id: r.id, request: r.request, status: status(r),
    detail: 'explicit_source_range', source: citeSource(args.verify?.sourceCapture) }));
  return { status: 'ok', receipt: { nonce: prompt.match(/--nonce (\S+)/)[1], sha: args.headShaShort,
    base_branch: args.baseBranch || 'main', diff_source: { path: args.diffPath || args.verify?.diffPath || null, read: true },
    batch_id: prompt.match(/--cite-batch (\S+)/)[1], attempt: Number(prompt.match(/--cite-attempt (\d+)/)[1]),
    n_in: document.requests.length, inline_checksum: fnv1a32(token), input_checksum: citeChecksum(document),
    deltas_checksum: citeChecksum(rows), record_proofs: document.requests.map((r) => ({ id: r.id, checksum: citeChecksum(r) })) },
  result: { citations: rows } };
}
