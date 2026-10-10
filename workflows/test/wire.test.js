// Integer coercion policies: one acceptance table, three columns.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { inspect } from 'node:util';
import { INT_POLICY, WS_TRIM_RE, coerceInt, deepClone } from '../src/wire.js';
import { verifyStage } from '../src/verifyStage.js';
import { pinNumericFields } from '../src/verifyWire.js';
import { verifyCtx, verifyInput } from './helpers/verifyDelta.js';

const validation = (value) => coerceInt(value, INT_POLICY.validation);
const filter = (value) => coerceInt(value, INT_POLICY.filter);
const verifyPin = (value) => pinNumericFields({ confidence: value }).confidence;

// The verify pin leaves a value it cannot coerce exactly as it was.
const SAME = Symbol('unchanged');
const label = (value) => (typeof value === 'string'
  ? JSON.stringify(value).replace(/[^\x20-\x7e]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`)
  : inspect(value));

const DIGITS_400 = '9'.repeat(400);

// [input, validation, filter, verify]
const INT_POLICY_TABLE = [
  [true, 1, 1, SAME],
  [false, 0, 0, SAME],
  [72, 72, 72, 72],
  [-0, -0, -0, -0],
  [2 ** 52 + 1, 2 ** 52 + 1, 2 ** 52 + 1, 2 ** 52 + 1],
  [72.9, 72, 72, 73],
  [4.5, 4, 4, 5],
  [-4.5, -4, -4, -4],
  [-4.6, -4, -4, -5],
  [-0.5, -0, -0, 0],
  [NaN, null, null, SAME],
  [Infinity, null, null, SAME],
  [-Infinity, null, null, SAME],
  ['153', 153, 153, 153],
  [' 153 ', 153, 153, 153],
  ['+7', 7, 7, 7],
  ['-7', -7, -7, -7],
  ['007', 7, 7, 7],
  ['﻿42', 42, 42, 42],
  ['42\n', 42, 42, 42],
  ['12.0', null, null, 12],
  ['1e5', null, null, 100000],
  ['0x1f', null, null, 31],
  ['0b11', null, null, 3],
  ['72.9', null, null, SAME],
  ['.5', null, null, SAME],
  ['', null, null, SAME],
  ['   ', null, null, SAME],
  ['abc', null, null, SAME],
  ['1_0', null, null, SAME],
  ['１２', null, null, SAME],
  ['\u001c42', null, 42, SAME],
  ['42\u0085', null, 42, SAME],
  ['\u001f42\u001e', null, 42, SAME],
  ['​42', null, null, SAME],
  [DIGITS_400, Infinity, Infinity, SAME],
  [`-${DIGITS_400}`, -Infinity, -Infinity, SAME],
  [null, null, null, SAME],
  [undefined, null, null, SAME],
  [{}, null, null, SAME],
  [[], null, null, SAME],
];

for (const [input, asValidation, asFilter, asVerify] of INT_POLICY_TABLE) {
  const name = typeof input === 'string' && input.length > 100 ? `${label(input.slice(0, 4))} x${input.length}` : label(input);
  test(`integer policies: ${name}`, () => {
    assert.equal(validation(input), asValidation, 'validation');
    assert.equal(filter(input), asFilter, 'filter');
    assert.equal(verifyPin(input), asVerify === SAME ? input : asVerify, 'verify');
  });
}

// One per input class the verify policy refuses: the pin above shows the value kept,
// this shows the parser said null and did not hand the value back.
for (const refused of [true, NaN, '72.9', null]) {
  test(`verify policy refuses ${label(refused)}`, () => {
    assert.equal(coerceInt(refused, INT_POLICY.verify), null);
  });
}

// Line buckets: the leading whitespace the policy table lacks, and a non-ASCII digit.
const FILTER_LINE_TABLE = [
  ['\x1e12', 12], // U+001E RS
  ['\x8512', 12], // U+0085 NEL
  ['\u0661\u0662', null], // Arabic-Indic digits
];

for (const [input, expected] of FILTER_LINE_TABLE) {
  test(`filter line: ${label(input)}`, () => {
    assert.equal(filter(input), expected);
  });
}

test('a degraded slice carries a pinned string confidence', async () => {
  const ctx = verifyCtx(() => ({ status: 'failed', exitCode: 1 }));
  const out = await verifyStage(ctx, verifyInput([{ id: 'F1', file: 'a.js', line_start: 1, confidence: '85', origin: 'new' }]));
  assert.equal(out.verified, false);
  assert.deepEqual(out.findings.map((f) => [f.confidence, f.origin]), [[85, 'unknown']]);
});

test('deepClone returns a structurally-independent copy (no shared references)', () => {
  const src = { a: 1, nested: { b: [1, 2, 3] }, list: [{ x: 1 }] };
  const copy = deepClone(src);
  assert.deepEqual(copy, src);
  assert.notEqual(copy, src);
  assert.notEqual(copy.nested, src.nested);
  assert.notEqual(copy.nested.b, src.nested.b);
  assert.notEqual(copy.list[0], src.list[0]);
  copy.nested.b.push(4);
  copy.list[0].x = 99;
  assert.deepEqual(src.nested.b, [1, 2, 3], 'mutating the copy never touches the source');
  assert.equal(src.list[0].x, 1);
});

// Dedup titles trim the full review whitespace class.
const TITLE_STRIP_TABLE = [
  ['empty string stays empty', '', ''],
  ['spaces strip to empty', '   ', ''],
  ['divergent whitespace strips to empty', '\x1c\x1d\x1e\x1f\x85\ufeff', ''],
  ['plain text stays unchanged', 'alpha', 'alpha'],
  ['leading file separator strips', '\x1calpha', 'alpha'],
  ['trailing NEL strips', 'alpha\x85', 'alpha'],
  ['BOM strips at both ends', '\ufeffalpha\ufeff', 'alpha'],
  ['GS and RS strip without changing interior spaces', '\x1d alpha bravo \x1e', 'alpha bravo'],
  ['interior file separator survives', 'a\x1cb', 'a\x1cb'],
  ['interior NEL survives and trailing BOM strips', 'mixed\x85 case\ufeff', 'mixed\x85 case'],
];

for (const [name, text, expected] of TITLE_STRIP_TABLE) {
  test(`whitespace strip: ${name}`, () => {
    assert.equal(text.replace(WS_TRIM_RE, ''), expected);
  });
}
