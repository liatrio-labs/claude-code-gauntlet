// Integer coercion policies: one acceptance table, three columns.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { inspect } from 'node:util';
import { pyIntStrict } from '../src/applyValidations.js';
import { pyIntOrNull } from '../src/filterFindings.js';
import { joinVerifyDeltas, verifyStage } from '../src/stages.js';

const validation = (value) => pyIntStrict(value);
const filter = (value) => pyIntOrNull(value);
const joinOne = (finding) => joinVerifyDeltas([{ id: 'F1', ...finding }], [{ id: 'F1', verified: true }])[0];
const verifyPin = (value) => joinOne({ confidence: value }).confidence;

// The verify pin leaves a value it cannot coerce exactly as it was.
const SAME = Symbol('unchanged');
const label = (value) => (typeof value === 'string'
  ? JSON.stringify(value).replace(/[^\x20-\x7e]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`)
  : inspect(value));

const DIGITS_400 = '9'.repeat(400);
const EMPTY_OBJECT = {};
const EMPTY_ARRAY = [];

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
  [EMPTY_OBJECT, null, null, SAME],
  [EMPTY_ARRAY, null, null, SAME],
];

for (const [input, asValidation, asFilter, asVerify] of INT_POLICY_TABLE) {
  const name = typeof input === 'string' && input.length > 100 ? `${label(input.slice(0, 4))} x${input.length}` : label(input);
  test(`integer policies: ${name}`, () => {
    assert.equal(validation(input), asValidation, 'validation');
    assert.equal(filter(input), asFilter, 'filter');
    assert.equal(verifyPin(input), asVerify === SAME ? input : asVerify, 'verify');
  });
}

for (const field of ['line_start', 'line_end', 'line', 'end_line', 'confidence']) {
  test(`verify pins ${field}`, () => {
    assert.equal(joinOne({ [field]: '153' })[field], 153);
  });
}

test('verify pins no field outside its list', () => {
  assert.equal(joinOne({ line_count: '153' }).line_count, '153');
});

test('verify pin adds no key the finding lacks', () => {
  assert.deepEqual(Object.keys(joinOne({})), ['id']);
});

test('a degraded slice carries a pinned string confidence', async () => {
  const ctx = { agent: async () => ({ status: 'failed', exitCode: 1 }) };
  const out = await verifyStage(ctx, {
    findings: [{ id: 'F1', file: 'a.js', line_start: 1, confidence: '85', origin: 'new' }],
    nonce: 'n-1',
    headShaShort: 'abc123',
    limits: { verifySliceSize: 200 },
    policy: {},
    verify: { scriptPath: '/p/verify_findings.py', inputPathBase: '/o/in', outputPathBase: '/o/out', baseBranch: 'main', diffPath: '/o/d.patch' },
  });
  assert.equal(out.verified, false);
  assert.deepEqual(out.findings.map((f) => [f.confidence, f.origin]), [[85, 'unknown']]);
});
