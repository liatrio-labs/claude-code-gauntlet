import { test } from 'node:test';
import assert from 'node:assert/strict';
import { joinVerifyDeltas, deltaContentProof, encodeInlineString, encodeSliceInline, recordProof, sliceInputChecksum, sliceTokenChecksum, projectVerifySliceFinding } from '../src/verifyWire.js';
import { fnv1a32, normalizeReviewText } from '../src/wire.js';
import { reviewMdSetChecksum } from '../src/args.js';
import { loadCases } from './helpers/goldenCases.js';

// Python-produced fixtures pin the join and content proof independently of the mock.
for (const c of loadCases('verify_deltas')) {
  test(`verify_deltas parity: ${c.name}`, () => {
    const before = JSON.parse(JSON.stringify(c.input.dispatched));
    const joined = joinVerifyDeltas(c.input.dispatched, c.expected.deltas);
    assert.deepEqual(joined, c.expected.joined);
    assert.deepEqual(c.input.dispatched, before);
    assert.equal(deltaContentProof(c.input.dispatched.map((f) => f.id), c.expected.deltas), c.expected.checksum);
  });
}
for (const c of loadCases('slice_input_proof')) {
  test(`slice_input_proof parity: ${c.name}`, () => {
    assert.equal(sliceInputChecksum(c.input.doc), c.expected.checksum);
  });
}
for (const c of loadCases('record_proof')) test(`record_proof parity: ${c.name}`, () => {
  assert.equal(recordProof(c.input.record), c.expected.checksum);
});
for (const c of loadCases('slice_inline')) {
  test(`slice_inline parity: ${c.name}`, () => {
    assert.equal(encodeSliceInline(c.input.doc), c.expected.encoded);
    assert.equal(sliceInputChecksum(c.input.doc), c.expected.checksum);
    assert.equal(sliceTokenChecksum(c.expected.encoded), c.expected.token_checksum);
  });
}

test('inline encoder preserves safe text and contains Unicode and shell syntax', () => {
  assert.equal(encodeInlineString("Az09 .,:/_-\u00e9\u2014\ud83d\ude00'\\\"$\n"), 'Az09 .,:/_-%C3%A9%E2%80%94%F0%9F%98%80%27%5C%22%24%0A');
});
test('inline encoder deep-walks keys and values, preserving primitives', () => {
  const encoded = encodeSliceInline({ 'cl\u00e9': { '\u{1F4A5}': "a'b\\c\n" }, list: [null, true, 7], text: '%41' });
  assert.equal(encoded, '{"cl%C3%A9":{"%F0%9F%92%A5":"a%27b%5Cc%0A"},"list":[null,true,7],"text":"%2541"}');
});

test('projection drops undeclared fields and pins numbers', () => {
  const projected = projectVerifySliceFinding({
    id: 'F1', file: 'a.js', line_start: 3.5, line_end: 3, description: '`zebra`', evidence: '`alpha` `zebra`',
    severity: 'high', confidence: 90.2, cross_file_refs: [], origin: 'new',
    agent: 'security', dimension: 'security', title: 'title', suggestion: 'fix',
  });
  const expected = {
    id: 'F1', file: 'a.js', line_start: 4, line_end: 3,
    severity: 'high', confidence: 90, symbols: 'alpha zebra',
  };
  assert.deepEqual(projected, expected);
  assert.deepEqual(Object.keys(projected), Object.keys(expected));
});

test('projection caches each finding object and preserves absent versus null scalars', () => {
  const finding = { id: 'F1', line_start: null, line_end: undefined };
  const projected = projectVerifySliceFinding(finding);
  assert.deepEqual(projected, { id: 'F1', line_start: null });
  assert.equal(projectVerifySliceFinding(finding), projected);
  assert.notEqual(projectVerifySliceFinding({ ...finding }), projected);
  assert.deepEqual(projectVerifySliceFinding(null), {});
  assert.deepEqual(projectVerifySliceFinding(Object.create({ id: 'inherited' })), {});
});

test('projection emits decision flags only for valid references and removed-test locations', () => {
  for (const refs of [[], 'x', { length: 1 }, ['x']]) {
    assert.deepEqual(projectVerifySliceFinding({ cross_file_refs: refs }), Array.isArray(refs) && refs.length
      ? { has_cross_file_refs: true } : {});
  }
  const valid = { id: 'loss', path: 't', old_start: 1, old_end: 2 };
  const rows = [
    [valid, true], [{ ...valid, extra: 'ignored' }, true],
    [{ ...valid, id: '\uFEFF', path: '\u0085\u001Ctests/old' }, true],
    [{ ...valid, old_start: 2 ** 54, old_end: 2 ** 55 }, true],
    ...[null, [], '', { ...valid, id: '' }, { ...valid, id: '\u0085\u001C' },
      { ...valid, path: '\u001C' }, { ...valid, path: 42 }, { ...valid, id: 42 },
      { ...valid, old_start: true }, { ...valid, old_end: true },
      { ...valid, old_start: '1' }, { ...valid, old_start: 1.5 },
      { ...valid, old_start: 0 }, { ...valid, old_start: 3 }, { ...valid, old_end: undefined },
    ].map((value) => [value, false]),
  ];
  for (const [removed_test, expected] of rows) {
    assert.deepEqual(projectVerifySliceFinding({ removed_test }), expected ? { has_removed_test_location: true } : {});
  }
});

test('join pins numerics when the delta omits confidence', () => {
  assert.deepEqual(joinVerifyDeltas([{ id: 'F0', line_start: '42', line_end: 9.2, confidence: 82.5 }], [{ id: 'F0', verified: true }]),
    [{ id: 'F0', claimed_line_start: 42, claimed_line_end: 9, confidence: 83, location_status: 'unconfirmed' }]);
});

for (const c of loadCases('review_text_checksum')) {
  test(`review_text_checksum parity: ${c.name}`, () => {
    const normalized = normalizeReviewText(c.input.text);
    assert.deepEqual({ normalized, checksum: fnv1a32(normalized) }, c.expected);
  });
}

test('normalizeReviewText is linear on long space and newline runs', () => {
  const text = `${' '.repeat(100_000)}x\n${'\n'.repeat(100_000)}`;
  const started = Date.now();
  assert.equal(normalizeReviewText(text), `${' '.repeat(100_000)}x`);
  assert.ok(Date.now() - started < 1000);
});

for (const c of loadCases('review_md_set_checksum')) {
  test(`review_md_set_checksum parity: ${c.name}`, () => {
    assert.deepEqual({ checksum: reviewMdSetChecksum(c.input.entries) }, c.expected);
  });
}

test('proven locations replace held coordinates and aliases', () => {
  const held = { id: 'f', line_start: 800, line_end: 804, line: 2, end_line: 7,
    description: 'The claim survives relocation.', anchor: 'perform_action(value)' };
  const [resolved] = joinVerifyDeltas([held], [{ id: 'f', verified: true, line_start: 12, line_end: 12, location_status: 'relocated' }]);
  assert.equal(resolved.description, held.description);
  assert.deepEqual([resolved.line, resolved.end_line], [12, 12]);
  assert.deepEqual([resolved.line_start, resolved.line_end, resolved.claimed_line_start, resolved.claimed_line_end], [12, 12, 800, 804]);
  const [unconfirmed] = joinVerifyDeltas([held], [{ id: 'f', verified: true, location_status: 'unconfirmed' }]);
  for (const key of ['line_start', 'line_end', 'line', 'end_line']) assert.equal(Object.hasOwn(unconfirmed, key), false);
  assert.equal(unconfirmed.claimed_line_end, 804);
  assert.equal(held.line, 2);
});
