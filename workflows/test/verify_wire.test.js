import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  joinVerifyDeltas,
  deltaContentProof,
  encodeSliceInline,
  sliceTokenChecksum,
} from '../src/verifyWire.js';
import { fnv1a32 } from '../src/wire.js';
import { loadCases } from './helpers/goldenCases.js';

// --- verify_deltas: cross-runtime equivalence claim -----------
//
// Python (gauntlet.verify.wire.build_deltas and gauntlet.jsjson.checksum_or_none,
// run by the recorder) owns the producing half; this block owns the reconstructing half (joinVerifyDeltas/
// deltaContentProof, the same functions verifyStage actually calls). One golden fixture
// sits between them, so a change to either side that breaks the join is caught here
// rather than only by the two runtimes agreeing with themselves.
for (const c of loadCases('verify_deltas')) {
  test(`verify_deltas parity: ${c.name}`, () => {
    // (1) THE join reproduces, for every field any downstream stage consumes, what
    // verify_findings.py itself left on the finding (minus its own audit trail -- the
    // recorder's project() drops exactly those three fields). This is the
    // equivalence claim itself, not a proxy for it.
    const joined = joinVerifyDeltas(c.input.dispatched, c.expected.deltas);
    assert.deepEqual(joined, c.expected.joined);

    // (2) The two runtimes compute the SAME content proof over the SAME deltas --
    // Python's gauntlet.jsjson.checksum_or_none(deltas) (over the deltas in dispatch
    // order) against JS's
    // deltaContentProof (which re-keys by id before stringifying), so an order-dependent
    // divergence between the two canonicalisations would fail here even though each
    // runtime's own deltas array already carries the checksum that produced it.
    const ids = c.input.dispatched.map((f) => f.id);
    assert.equal(deltaContentProof(ids, c.expected.deltas), c.expected.checksum);

    // (3) Deterministic `agent` values survive on the trusted path: every dispatched
    // finding that carried an `agent` still carries the SAME `agent` after the join.
    const agentById = new Map(c.input.dispatched.map((f) => [f.id, f.agent]));
    for (const f of joined) {
      if (agentById.get(f.id) !== undefined) assert.equal(f.agent, agentById.get(f.id));
    }
  });
}

// --- slice_input_proof: the persisted JSON content proof's cross-runtime agreement -
//
// The legacy writer path remains covered by this golden. The inline path below additionally
// pins the exact percent-encoded token, including its receipt proof.
for (const c of loadCases('slice_input_proof')) {
  test(`slice_input_proof parity: ${c.name}`, () => {
    assert.equal(fnv1a32(JSON.stringify(c.input.doc, null, 2)), c.expected.checksum);
  });
}

for (const c of loadCases('slice_inline')) {
  test(`slice_inline parity: ${c.name}`, () => {
    assert.equal(encodeSliceInline(c.input.doc), c.expected.encoded);
    assert.equal(fnv1a32(JSON.stringify(c.input.doc, null, 2)), c.expected.checksum);
    // The token proof, over the same bytes Python hashes in gauntlet.verify.wire.run_receipt.
    assert.equal(sliceTokenChecksum(c.expected.encoded), c.expected.token_checksum);
  });
}
