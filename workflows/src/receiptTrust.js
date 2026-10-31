export const VERIFY_RECEIPT_FIELDS = {
  sha: { type: 'string' }, n_in: { type: 'number' }, nonce: { type: 'string' },
  deltas_checksum: { type: 'string' }, input_checksum: { type: 'string' }, inline_checksum: { type: 'string' },
  base_branch: { type: 'string' },
  diff_source: { type: 'object', properties: { path: { type: ['string', 'null'] }, read: { type: 'boolean' } }, required: ['path', 'read'] },
  record_proofs: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, checksum: { type: ['string', 'null'] } }, required: ['id', 'checksum'] } },
};

// These checks pair both executor modes with the command that produced their data.
export function receiptIdentityError(env, { nonce, headShaShort, baseBranch, diffPath }) {
  if (!env || typeof env !== 'object') return 'executor returned no envelope';
  if (env.status !== 'ok') return `status=${env.status == null ? 'missing' : env.status}${env.stderr ? ` (${env.stderr})` : ''}`;
  const r = env.receipt || {};
  if (r.nonce !== nonce) return `receipt nonce mismatch (got ${r.nonce == null ? 'missing' : r.nonce}, expected ${nonce})`;
  if (r.sha !== headShaShort) return `receipt sha mismatch (got ${r.sha == null ? 'missing' : r.sha})`;
  if (r.base_branch !== baseBranch) return 'receipt base_branch mismatch';
  if (r.diff_source?.path !== diffPath || r.diff_source?.read !== true) return 'receipt diff_source mismatch or unreadable';
  return null;
}

export function receiptInputFault(r, n, expectedInlineChecksum, expectedInputChecksum) {
  let diagnostic = r.n_in === n ? null : {
    reason: `receipt n_in mismatch (got ${r.n_in == null ? 'missing' : r.n_in}, expected ${n})`, inputFault: 'mismatch',
  };
  for (const [field, expected, missingReason, name, noun] of [
    ['inline_checksum', expectedInlineChecksum, 'inline token proof missing from receipt', 'token', 'token'],
    ['input_checksum', expectedInputChecksum, 'input content proof missing from receipt', 'content', 'document'],
  ]) {
    if (expected == null) continue;
    const got = typeof r[field] === 'string' ? r[field].trim() : '';
    if (!got) diagnostic ??= { reason: missingReason, inputFault: 'missing' };
    else if (got !== expected) diagnostic ??= { reason: `slice-input ${name} proof mismatch (receipt ${got}, dispatched ${expected}) — the ${noun} the script decoded is not the ${noun} this stage dispatched`, inputFault: 'mismatch' };
  }
  return diagnostic;
}
