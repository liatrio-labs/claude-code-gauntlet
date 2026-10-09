import { test } from 'node:test';
import assert from 'node:assert/strict';
import { joinVerifyDeltas, deltaContentProof, encodeInlineString, encodeSliceInline, sliceInputChecksum, sliceTokenChecksum, pinNumericFields, projectVerifySliceFinding, VERIFY_INLINE_SAFE, VERIFY_SLICE_FIELDS } from '../src/verifyWire.js';
import { FINDING_PROP_TYPES } from '../src/registry.js';
import { loadCases } from './helpers/goldenCases.js';

// Python-produced fixtures pin the join and content proof independently of the mock.
for (const c of loadCases('verify_deltas')) {
  test(`verify_deltas parity: ${c.name}`, () => {
    const before = JSON.parse(JSON.stringify(c.input.dispatched));
    const joined = joinVerifyDeltas(c.input.dispatched, c.expected.deltas);
    assert.deepEqual(joined, c.expected.joined);
    assert.deepEqual(c.input.dispatched, before);
    assert.equal(deltaContentProof(c.input.dispatched.map((f) => f.id), c.expected.deltas), c.expected.checksum);
    const agentById = new Map(c.input.dispatched.map((f) => [f.id, f.agent]));
    for (const f of joined) {
      if (agentById.get(f.id) !== undefined) assert.equal(f.agent, agentById.get(f.id));
    }
  });
}
for (const c of loadCases('slice_input_proof')) {
  test(`slice_input_proof parity: ${c.name}`, () => {
    assert.equal(sliceInputChecksum(c.input.doc), c.expected.checksum);
  });
}
for (const c of loadCases('slice_inline')) {
  test(`slice_inline parity: ${c.name}`, () => {
    assert.equal(encodeSliceInline(c.input.doc), c.expected.encoded);
    assert.equal(sliceInputChecksum(c.input.doc), c.expected.checksum);
    assert.equal(sliceTokenChecksum(c.expected.encoded), c.expected.token_checksum);
  });
}

// The encoder also needs independent values: its inline fixtures are self-recorded.
const ENCODER_ROWS = [
  ['safe alphabet', 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,:/_-', 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,:/_-'],
  ['empty string', '', ''],
  ['quotes, escapes and controls', "'\\\"`%\n\r\t\0", '%27%5C%22%60%25%0A%0D%09%00'],
  ['two-byte UTF-8', '\u00e9', '%C3%A9'],
  ['three-byte UTF-8', '\u2014', '%E2%80%94'],
  ['four-byte UTF-8', '\u{1F600}', '%F0%9F%98%80'],
  ['lone high surrogate', '\uD800', '%uD800'],
  ['lone low surrogate', '\uDC00', '%uDC00'],
  ['low then low', '\uDC00\uDC01', '%uDC00%uDC01'],
  ['high then non-surrogate', '\uD800a', '%uD800a'],
  ['high then high', '\uD800\uD800', '%uD800%uD800'],
  ['shell metacharacters', '$`;()&|<>#*?!~=+', '%24%60%3B%28%29%26%7C%3C%3E%23%2A%3F%21%7E%3D%2B'],
];
for (const [name, input, expected] of ENCODER_ROWS) {
  test(`inline encoder: ${name}`, () => {
    assert.equal(encodeInlineString(input), expected);
    if (name === 'safe alphabet') assert.equal(VERIFY_INLINE_SAFE, expected);
  });
}
test('inline encoder deep-walks keys and values, preserving primitives', () => {
  const encoded = encodeSliceInline({ 'cl\u00e9': { '\u{1F4A5}': "a'b\\c\n" }, list: [null, true, 7], text: '%41' });
  assert.equal(encoded, '{"cl%C3%A9":{"%F0%9F%92%A5":"a%27b%5Cc%0A"},"list":[null,true,7],"text":"%2541"}');
});
test('inline encoder rejects a custom toJSON apostrophe', () => {
  assert.throws(() => encodeSliceInline({ value: { toJSON: () => "'" } }), {
    name: 'Error', message: 'encodeSliceInline produced a non-printable or quoted payload',
  });
});
const PROJECTION_ROWS = [
  { name: 'drops undeclared fields and pins numbers', input: {
    id: 'F1', file: 'a.js', line_start: 3.5, line_end: 3, description: 'd', evidence: 'e',
    severity: 'high', confidence: 90.2, cross_file_refs: [], origin: 'new',
    agent: 'security', dimension: 'security', title: 'title', suggestion: 'fix',
  }, expected: {
    id: 'F1', file: 'a.js', line_start: 4, line_end: 3, description: 'd', evidence: 'e',
    severity: 'high', confidence: 90, cross_file_refs: [], origin: 'new',
  } },
  { name: 'omits absent and undefined fields', input: { id: 'F1', file: 'a.js', line_start: 1, origin: 'new', evidence: undefined },
    expected: { id: 'F1', file: 'a.js', line_start: 1, origin: 'new' } },
];
for (const row of PROJECTION_ROWS) {
  test(`projection ${row.name}`, () => {
    const projected = projectVerifySliceFinding(row.input);
    assert.deepEqual(projected, row.expected);
    assert.deepEqual(Object.keys(projected), Object.keys(row.expected));
  });
}
test('VERIFY_SLICE_FIELDS stays inside the closed finding schema', () => {
  assert.deepEqual(VERIFY_SLICE_FIELDS, ['id', 'file', 'line_start', 'line_end', 'description', 'evidence', 'severity', 'confidence', 'cross_file_refs', 'origin']);
  for (const field of VERIFY_SLICE_FIELDS) assert.ok(Object.hasOwn(FINDING_PROP_TYPES, field));
});

for (const field of ['line_start', 'line_end', 'line', 'end_line', 'confidence']) {
  test(`verify pins ${field}`, () => {
    assert.equal(pinNumericFields({ [field]: '153' })[field], 153);
  });
}

test('verify pins no field outside its list', () => {
  assert.equal(pinNumericFields({ line_count: '153' }).line_count, '153');
});

test('the join adds no key the finding lacks', () => {
  assert.deepEqual(Object.keys(joinVerifyDeltas([{ id: 'F1' }], [{ id: 'F1', verified: true }])[0]), ['id']);
});

const JOIN_PIN_ROWS = [
  ['numeric strings', { id: 'F0', line_start: '42', line_end: '44', confidence: '85' }, { id: 'F0', line_start: 42, line_end: 44, confidence: 85 }],
  ['fractional numerics', { id: 'F1', line_start: 4.6, line_end: 9.2, confidence: 82.5 }, { id: 'F1', line_start: 5, line_end: 9, confidence: 83 }],
];
for (const [name, finding, expected] of JOIN_PIN_ROWS) {
  test(`join pins ${name} when the delta omits confidence`, () => {
    assert.deepEqual(joinVerifyDeltas([finding], [{ id: finding.id, verified: true }]), [expected]);
  });
}
