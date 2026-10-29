import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  writerPayload, persistPlan, persistPrimaries, persistDerivable, hardenEscapeRuns,
} from '../src/persistPlan.js';
import { fnv1a32 } from '../src/wire.js';
import {
  PATHS,
  PROJECTION_A,
  PROJECTION_B,
  PROJECTION_C,
  ESCAPED_PROJECTION,
} from './helpers/persistFixtures.js';
import { deriveFromPlan } from './helpers/deriveFromPlan.js';

const FINDINGS = [PROJECTION_A, PROJECTION_B, PROJECTION_C];

const ALIASED = [
  { ...PROJECTION_A, line: 7, end_line: 9, body: "body text" },
  { ...PROJECTION_B, line: 3, end_line: 3, body: "other" },
  { ...PROJECTION_C, body: "\u65e5\u672c\u8a9e\u306e\u8aac\u660e \ud83d\ude00 with an astral \ud835\udd4f character" },
];

const CHECKPOINT = {
  phases: {
    challenge: {
      findings: FINDINGS,
      unverified: [],
      eliminated: [],
      gaps: [],
      stats: { final_count: 3 },
      generated_at: "2026-07-27T00:00:00Z"
    }
  },
  completed: ["summarize", "discover", "challenge"],
  phaseReached: "report",
  counts: { challenge: 3 }
};
const INPUT = {
  findings: FINDINGS,
  postReview: [PROJECTION_C, PROJECTION_A],
  report: "\ufeff# r\r\n",
  checkpoints: CHECKPOINT
};

test('projection preserves artifact bytes, selection and checkpoint order', () => {
  const before = JSON.stringify(INPUT);
  const text = JSON.stringify(ALIASED, null, 2);
  const primary = persistPrimaries(INPUT);
  assert.deepEqual(primary, { findingsJson: text, reportMd: "\ufeff# r\r\n" });
  assert.deepEqual(writerPayload(INPUT), { findings: ALIASED, postReview: [ALIASED[2], ALIASED[0]], report: INPUT.report, checkpoints: CHECKPOINT });
  const plan = persistPlan(INPUT, PATHS);
  assert.deepEqual(plan, {
    planVersion: 3,
    expect: [
      { path: PATHS.findings, chars: 652, checksum: "fnv1a32:0x95c36e7d" },
      { path: PATHS.report, chars: 3, checksum: "fnv1a32:0x34bd778c" }
    ],
    derive: [
      {
        path: PATHS.postReview,
        chars: 422,
        checksum: "fnv1a32:0x7b3f7426"
      },
      {
        path: PATHS.checkpoints,
        chars: 994,
        checksum: "fnv1a32:0x24099ac4"
      }
    ],
    postReview: {
      path: PATHS.postReview,
      source: PATHS.findings,
      ids: ["C", "A"],
      wrapper: null
    },
    checkpoint: {
      path: PATHS.checkpoints,
      mode: 'literal',
      skeleton: CHECKPOINT
    },
    planChecksum: plan.planChecksum
  });
  const derived = deriveFromPlan(plan, text);
  assert.equal(JSON.stringify(derived.postReview, null, 2), JSON.stringify([ALIASED[2], ALIASED[0]], null, 2));
  assert.equal(JSON.stringify(derived.checkpoints, null, 2), JSON.stringify(CHECKPOINT, null, 2));
  assert.equal(JSON.stringify(INPUT), before);
  const { planChecksum, ...body } = plan;
  assert.equal(fnv1a32(JSON.stringify(body, null, 2)), planChecksum);
});

for (const [name, reviewBody] of [['absent', undefined], ['text', 'Review body']]) {
  test('wrapped selection: identity and ' + name + ' review body', () => {
    const input = {
      ...INPUT, reviewBody,
      prIdentity: {
        owner: "o", repo: "r", pr_number: 7,
        sha_full: 'd'.repeat(40),
        platform: 'gitlab',
        title: "Must not leak",
        web_origin: 'https://gitlab.com'
      }
    };
    const wrapper = {
      owner: "o", repo: "r", pr_number: 7, sha: 'd'.repeat(40),
      platform: 'gitlab',
      web_origin: 'https://gitlab.com',
      review_body: reviewBody || ''
    };
    const payload = writerPayload(input);
    const plan = persistPlan(input, PATHS);
    assert.deepEqual(payload.postReview, { ...wrapper, findings: [ALIASED[2], ALIASED[0]] });
    assert.deepEqual(plan.postReview.wrapper, wrapper);
    assert.equal(JSON.stringify(deriveFromPlan(plan, persistPrimaries(input).findingsJson).postReview), JSON.stringify(payload.postReview));
  });
}

test('escape-hardened primary pins transmitted bytes and keeps the original prose', () => {
  const input = { findings: [ESCAPED_PROJECTION] };
  const text = persistPrimaries(input).findingsJson;
  const escaped = "gradeInputProof produces \\u005c\\\"the executor's receipt carried no input_checksum\\u005c\\\" here";
  assert.equal(text, '[\n  {\n    "id": "A",\n    "location_status": "unconfirmed",\n    "description": "' + escaped + '",\n    "body": "' + escaped + '"\n  }\n]');
  assert.equal(fnv1a32(text), 'fnv1a32:0xae22a9e7');
  assert.deepEqual(JSON.parse(text), [{ ...ESCAPED_PROJECTION, body: ESCAPED_PROJECTION.description }]);
});

for (const [name, input, expected] of [
  ['canonical selection', INPUT, { ok: true }],
  [
    'checkpoint needs location normalization', {
      findings: [{ id: 'F1', line_start: 99 }],
      checkpoints: { phases: { challenge: { findings: [{ id: 'F1', line_start: 99 }] } } },
    },
    { ok: true },
  ],
  [
    'preexisting alias', { findings: [{ id: 'F1', line: 999 }] },
    { ok: true }
  ],
  [
    'fractional number', { findings: [{ id: 'F1', confidence: 90.5 }] },
    {
      ok: false,
      reason: 'findings[0].confidence is not a JS-safe integer \u2014 the Python assembler cannot '
      + 'spell it byte-identically, so the derived artifact would diverge'
    }
  ],
]) {
  test(`derivability: ${name}`, () => {
    assert.deepEqual(persistDerivable(input), expected);
  });
}

for (const [name, input, expected] of [
  ['backslash before quote', '{"d":"\\\\\\""}', '{"d":"\\u005c\\""}'],
  ['ordinary quote escape', '{"d":"\\""}', '{"d":"\\""}'],
]) {
  test(`escape hardening: ${name}`, () => {
    assert.equal(hardenEscapeRuns(input), expected);
    assert.equal(hardenEscapeRuns(expected), expected);
    assert.deepEqual(JSON.parse(expected), JSON.parse(input));
  });
}

test('canonical location overwrites aliases in both writers', () => {
  const findings = [
    { id: 'resolved', location_status: 'relocated', line_start: 5, line_end: 6, line: 999, end_line: 1000, description: 'Claim' },
    { id: 'unresolved', location_status: 'unconfirmed', claimed_line_start: 999, line: 999, end_line: 1000, description: 'Retained' },
  ];
  const input = { findings, postReview: findings };
  const payload = writerPayload(input);
  assert.deepEqual(JSON.parse(persistPrimaries(input).findingsJson), payload.findings);
  assert.deepEqual(payload.postReview, payload.findings);
  assert.equal(payload.findings[0].line, 5);
  assert.equal(payload.findings[0].end_line, 6);
  assert.equal(Object.hasOwn(payload.findings[1], 'line'), false);
  assert.equal(Object.hasOwn(payload.findings[1], 'end_line'), false);
  assert.equal(findings[0].line, 999);
  assert.equal(persistDerivable(input).ok, true);
});

test('canonical checkpoint aliases survive derivation', () => {
  const findings = [
    { id: 'placed', location_status: 'confirmed', line_start: 7, line_end: 8, line: 7, end_line: 8 },
    { id: 'unplaced', location_status: 'unconfirmed' },
  ];
  const input = { findings, checkpoints: { phases: { challenge: { findings } } } };
  assert.deepEqual(persistDerivable(input), { ok: true });
  const plan = persistPlan(input, PATHS);
  assert.deepEqual(deriveFromPlan(plan, persistPrimaries(input).findingsJson).checkpoints, input.checkpoints);
});
