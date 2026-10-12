import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  writerPayload, postReviewWrapper, persistPlan, persistPrimaries, persistDerivable, hardenEscapeRuns,
} from '../src/persistPlan.js';
import { fnv1a32, normalizeForChecksum } from '../src/wire.js';
import { makeFinding } from './helpers/pipelineMock.js';
import {
  PATHS,
  expectedWith,
  textWithLines,
  persistInput,
  PROJECTION_A,
  PROJECTION_B,
  PROJECTION_C,
  ESCAPED_PROJECTION,
} from './helpers/persistFixtures.js';
import { deriveFromPlan } from './helpers/deriveFromPlan.js';

const ALIASED_A = { ...PROJECTION_A, line: 7, end_line: 9, body: "body text" };
const ALIASED_B = { ...PROJECTION_B, line: 3, body: "other" };
const ALIASED_C = { ...PROJECTION_C, body: "\u65e5\u672c\u8a9e\u306e\u8aac\u660e \ud83d\ude00 with an astral \ud835\udd4f character" };
const ALIASED_ESCAPED = { ...ESCAPED_PROJECTION,
  body: "gradeInputProof produces \\\"the executor's receipt carried no input_checksum\\\" here" };

const CHECKPOINT_GOLDEN = {
  phases: {
    challenge: {
      findings: [PROJECTION_A, PROJECTION_B, PROJECTION_C],
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
const PROJECTION_INPUT = {
  findings: [PROJECTION_A, PROJECTION_B, PROJECTION_C],
  postReview: [PROJECTION_C, PROJECTION_A],
  report: "\ufeff# r\r\n",
  checkpoints: CHECKPOINT_GOLDEN
};
const FINDINGS_TEXT = [
  "[",
  "  {",
  "    \"id\": \"A\",",
  "    \"line_start\": 7,",
  "    \"line_end\": 9,",
  "    \"description\": \"body text\",",
  "    \"report_tag\": \"suggestion\",",
  "    \"line\": 7,",
  "    \"end_line\": 9,",
  "    \"body\": \"body text\"",
  "  },",
  "  {",
  "    \"id\": \"B\",",
  "    \"line_start\": 3,",
  "    \"description\": \"other\",",
  "    \"confidence\": 71,",
  "    \"report_tag\": \"main\",",
  "    \"line\": 3,",
  "    \"body\": \"other\"",
  "  },",
  "  {",
  "    \"id\": \"C\",",
  "    \"title\": \"\u4e2d\u6587\u6807\u9898 \ud83c\udf89\",",
  "    \"description\": \"\u65e5\u672c\u8a9e\u306e\u8aac\u660e \ud83d\ude00 with an astral \ud835\udd4f character\",",
  "    \"body\": \"\u65e5\u672c\u8a9e\u306e\u8aac\u660e \ud83d\ude00 with an astral \ud835\udd4f character\"",
  "  }",
  "]"
].join("\n");
const PROJECTION_GOLDEN = {
  primaries: { findingsJson: FINDINGS_TEXT, reportMd: "\ufeff# r\r\n" },
  payload: {
    findings: [ALIASED_A, ALIASED_B, ALIASED_C],
    postReview: [ALIASED_C, ALIASED_A],
    report: "\ufeff# r\r\n",
    checkpoints: CHECKPOINT_GOLDEN
  },
  plan: {
    planVersion: 2,
    expect: [
      { path: PATHS.findings, chars: 504, checksum: "fnv1a32:0x9c07ec11" },
      { path: PATHS.report, chars: 3, checksum: "fnv1a32:0x34bd778c" }
    ],
    derive: [
      {
        path: PATHS.postReview,
        chars: 348,
        checksum: "fnv1a32:0xcab9512b"
      },
      {
        path: PATHS.checkpoints,
        chars: 841,
        checksum: "fnv1a32:0x40a5d14d"
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
      source: PATHS.findings,
      challengeFindingIds: ["A", "B", "C"],
      stripAliasFields: ["line", "end_line", "body"],
      skeleton: expectedWith(CHECKPOINT_GOLDEN, { 'phases.challenge.findings': [] })
    },
    planChecksum: "fnv1a32:0xc7c7f363"
  }
};
const PROJECTION_ROWS = [
  { name: "bare ordered subset", input: {  }, expected: {  } },
  {
    name: "wrapped selection",
    input: {
      findings: [PROJECTION_A, PROJECTION_B],
      "postReview.0": PROJECTION_B,
      report: "# r",
      "checkpoints.phases.challenge.findings": [PROJECTION_A, PROJECTION_B],
      "checkpoints.phases.challenge.stats.final_count": 2,
      "checkpoints.counts.challenge": 2,
      prIdentity: {
        owner: "o",
        repo: "r",
        pr_number: 7,
        sha_full: "dddddddddddddddddddddddddddddddddddddddd",
        platform: "github",
        web_origin: "https://github.com",
        title: "Must not leak"
      }
    },
    expected: {
      "primaries.reportMd": "# r",
      "payload.findings": [ALIASED_A, ALIASED_B],
      "payload.postReview": {
        owner: "o",
        repo: "r",
        pr_number: 7,
        sha: "dddddddddddddddddddddddddddddddddddddddd",
        platform: "github",
        review_body: "",
        findings: [ALIASED_B, ALIASED_A]
      },
      "payload.report": "# r",
      "payload.checkpoints.phases.challenge.findings": [PROJECTION_A, PROJECTION_B],
      "payload.checkpoints.phases.challenge.stats.final_count": 2,
      "payload.checkpoints.counts.challenge": 2,
      "plan.expect.0.chars": 344,
      "plan.expect.0.checksum": "fnv1a32:0x53b51777",
      "plan.derive.0.chars": 549,
      "plan.derive.0.checksum": "fnv1a32:0x3a72bf77",
      "plan.derive.1.chars": 704,
      "plan.derive.1.checksum": "fnv1a32:0x50383852",
      "plan.postReview.ids.0": "B",
      "plan.postReview.wrapper": {
        owner: "o",
        repo: "r",
        pr_number: 7,
        sha: "dddddddddddddddddddddddddddddddddddddddd",
        platform: "github",
        review_body: ""
      },
      "plan.checkpoint.challengeFindingIds": ["A", "B"],
      "plan.checkpoint.skeleton.phases.challenge.stats.final_count": 2,
      "plan.checkpoint.skeleton.counts.challenge": 2,
      "plan.planChecksum": "fnv1a32:0x9d1f2262"
    },
    lines: [[19, 6]]
  },
  {
    name: "capped non-ASCII",
    input: { postReview: [PROJECTION_B], report: "# r" },
    expected: {
      "primaries.reportMd": "# r",
      "payload.postReview": [ALIASED_B],
      "payload.report": "# r",
      "plan.derive.0.chars": 158,
      "plan.derive.0.checksum": "fnv1a32:0x306a19c1",
      "plan.postReview.ids": ["B"],
      "plan.planChecksum": "fnv1a32:0x30dd27ab"
    }
  },
  {
    name: "escape-hardened primary",
    input: {
      findings: [ESCAPED_PROJECTION],
      postReview: [ESCAPED_PROJECTION],
      report: "# r",
      "checkpoints.phases.challenge.findings": [ESCAPED_PROJECTION],
      "checkpoints.phases.challenge.stats.final_count": 1,
      "checkpoints.counts.challenge": 1
    },
    expected: {
      "primaries.reportMd": "# r",
      "payload.findings": [ALIASED_ESCAPED],
      "payload.postReview": [ALIASED_ESCAPED],
      "payload.report": "# r",
      "payload.checkpoints.phases.challenge.findings": [ESCAPED_PROJECTION],
      "payload.checkpoints.phases.challenge.stats.final_count": 1,
      "payload.checkpoints.counts.challenge": 1,
      "plan.expect.0.chars": 252,
      "plan.expect.0.checksum": "fnv1a32:0xb3209e15",
      "plan.derive.0.chars": 236,
      "plan.derive.0.checksum": "fnv1a32:0x582a8e0d",
      "plan.derive.1.chars": 529,
      "plan.derive.1.checksum": "fnv1a32:0xcbd18a5d",
      "plan.postReview.ids": ["A"],
      "plan.checkpoint.challengeFindingIds": ["A"],
      "plan.checkpoint.skeleton.phases.challenge.stats.final_count": 1,
      "plan.checkpoint.skeleton.counts.challenge": 1,
      "plan.planChecksum": "fnv1a32:0xbab1178d"
    },
    lines: [
      [
        3,
        22,
        "    \"description\": \"gradeInputProof produces \\u005c\\\"the executor's receipt carried no input_checksum\\u005c\\\" here\",",
        "    \"body\": \"gradeInputProof produces \\u005c\\\"the executor's receipt carried no input_checksum\\u005c\\\" here\""
      ]
    ]
  },
];
for (const row of PROJECTION_ROWS) {
  test(`projection: ${row.name}`, () => {
    const input = expectedWith(PROJECTION_INPUT, row.input);
    const expected = expectedWith(PROJECTION_GOLDEN, row.expected);
    expected.primaries.findingsJson = textWithLines(FINDINGS_TEXT, row.lines);
    const before = JSON.stringify(input.checkpoints);
    const plan = persistPlan(input, PATHS);
    const primaries = persistPrimaries(input);
    const held = writerPayload(input);
    assert.deepEqual(primaries, expected.primaries);
    assert.deepEqual(held, expected.payload);
    assert.deepEqual(plan, expected.plan);
    assert.equal(JSON.stringify(input.checkpoints), before);
    assert.deepEqual(
      Object.keys(plan.checkpoint.skeleton.phases.challenge),
      ['findings', 'unverified', 'eliminated', 'gaps', 'stats', 'generated_at']
    );
    assert.equal(Object.keys(plan).at(-1), 'planChecksum');
    const { planChecksum, ...planBody } = plan;
    assert.equal(fnv1a32(JSON.stringify(planBody, null, 2)), planChecksum);
    assert.match(planChecksum, /^fnv1a32:0x[0-9a-f]{8}$/);
    const derived = deriveFromPlan(plan, primaries.findingsJson);
    for (const [index, value] of [derived.postReview, derived.checkpoints].entries()) {
      const text = JSON.stringify(value, null, 2);
      assert.equal(normalizeForChecksum(text).length, expected.plan.derive[index].chars);
      assert.equal(fnv1a32(normalizeForChecksum(text)), expected.plan.derive[index].checksum);
    }
    assert.equal(JSON.stringify(derived.postReview, null, 2), JSON.stringify(expected.payload.postReview, null, 2));
    assert.equal(JSON.stringify(derived.checkpoints, null, 2), JSON.stringify(expected.payload.checkpoints, null, 2));
    if (expected.plan.postReview.wrapper) {
      assert.deepEqual(Object.keys(plan.postReview.wrapper), ['owner', 'repo', 'pr_number', 'sha', 'platform', 'review_body']);
      assert.deepEqual(Object.keys(held.postReview), ['owner', 'repo', 'pr_number', 'sha', 'platform', 'review_body', 'findings']);
      assert.equal(JSON.stringify(held.postReview.findings), JSON.stringify(writerPayload({ ...input, prIdentity: null }).postReview));
    }
  });
}
for (const [name, ids, challengeIds] of [
  ['delivery elision', ['A', 'B'], ['A', 'B', 'C']],
  ['delivery reordering', ['C', 'B', 'A'], ['A', 'B', 'C']],
  ['checkpoint elision', ['A', 'B', 'C'], ['A']],
]) {
  test(`selection self-proof: ${name}`, () => {
    const findings = [makeFinding('A'), makeFinding('B'), makeFinding('C')];
    const plan = persistPlan(persistInput({ findings }), PATHS);
    const { planChecksum, ...body } = plan;
    body.postReview.ids = ids;
    body.checkpoint.challengeFindingIds = challengeIds;
    assert.notEqual(fnv1a32(JSON.stringify(body, null, 2)), planChecksum);
  });
}
const PAYLOAD_GOLDEN = { findings: [], postReview: [], report: '', checkpoints: {} };
const ALIASED_INPUT = { line_start: 1, line_end: 2, description: 'canonical', line: 8, end_line: 9, body: 'alias' };
const PAYLOAD_ROWS = [
  ['absent lists', {}, {}],
  ['existing aliases preserved', { findings: [ALIASED_INPUT] }, { findings: [ALIASED_INPUT] }],
];
for (const [name, input, overrides] of PAYLOAD_ROWS) {
  test(`payload: ${name}`, () => assert.deepEqual(writerPayload(input), expectedWith(PAYLOAD_GOLDEN, overrides)));
}
for (const [input, expected] of [['body', 'body'], [null, ''], [42, '']]) {
  test(`wrapper body: ${JSON.stringify(input)}`, () => {
    const out = postReviewWrapper(
      { owner: 'o', repo: 'r', pr_number: 7, sha_full: '0123456789abcdef0123456789abcdef01234567', platform: 'gitlab' },
      input
    );
    assert.deepEqual(
      out,
      {
        owner: 'o',
        repo: 'r',
        pr_number: 7,
        sha: '0123456789abcdef0123456789abcdef01234567',
        platform: 'gitlab',
        review_body: expected
      }
    );
    assert.deepEqual(Object.keys(out), ['owner', 'repo', 'pr_number', 'sha', 'platform', 'review_body']);
  });
}
const GUARD_ROWS = [
  ['valid input', {}, { ok: true }],
  ['integer endpoints', { findings: [{ id: 'F1', line_start: 0, line_end: 9007199254740991, confidence: 90 }] }, { ok: true }],
  ['missing id', { findings: [{ id: 'F1' }, {}] }, { ok: false, reason: 'finding at index 1 has no usable string id' }],
  ['duplicate ids', { findings: [{ id: 'F1' }, { id: 'F1' }] }, { ok: false, reason: 'duplicate finding id F1' }],
  ['unknown delivery id', { findings: [{ id: 'F1' }], postReview: [{ id: 'GHOST' }] },
    { ok: false, reason: 'postReview entry at index 0 (id GHOST) is not present in the findings set' }],
  ['delivery twin differs', { findings: [{ id: 'F1' }], postReview: [{ id: 'F1', severity: 'low' }] },
    { ok: false, reason: 'postReview entry F1 differs from the findings entry with the same id' }],
  ['unknown checkpoint id', { findings: [{ id: 'F1' }], checkpoints: { phases: { challenge: { findings: [{ id: 'PHANTOM' }] } } } },
    { ok: false, reason: 'checkpoint challenge entry at index 0 (id PHANTOM) is not present in the findings set' }],
  ['checkpoint twin differs',
    { findings: [{ id: 'F1' }], checkpoints: { phases: { challenge: { findings: [{ id: 'F1', severity: 'low' }] } } } },
    { ok: false, reason: 'checkpoint challenge entry F1 differs from the findings entry with the same id' }],
  ['existing alias', { findings: [{ id: 'F1', line: 999 }] }, { ok: false,
    reason: 'finding F1 already carries a v2 alias field (line/end_line/body) \u2014 the checkpoint ' + 'alias strip is not reversible'
    }],
];
for (const [name, input, expected] of GUARD_ROWS) {
  test(`derivability: ${name}`, () => {
    const findings = input.findings || [];
    assert.deepEqual(persistDerivable({ findings, postReview: findings, checkpoints: {}, ...input }), expected);
  });
}
for (const value of [1e-7, 0.000001, 90.5, NaN, Infinity, -Infinity, 9007199254740992]) {
  test(`unsafe confidence: ${value}`, () => {
    const findings = [{ id: 'F1', confidence: value }];
    assert.deepEqual(
      persistDerivable({ findings }),
      {
        ok: false,
        reason: 'findings[0].confidence is not a JS-safe integer \u2014 the Python assembler cannot '
        + 'spell it byte-identically, so the derived artifact would diverge'
      }
    );
  });
}
for (const [name, input, expectedPath] of [
  ['checkpoint stats', { checkpoints: { phases: { challenge: { stats: { rate: 0.5 } } } } }, 'checkpoints.phases.challenge.stats.rate'],
  ['identity', { prIdentity: { pr_number: 7.5 } }, 'prIdentity.pr_number'],
  [
    'first unsafe sibling',
    { checkpoints: { phases: { challenge: { stats: { rate: 0.5, count: 90.5 } } } } },
    'checkpoints.phases.challenge.stats.rate'
  ],
]) {
  test(
    `unsafe number scan: ${name}`,
    () => assert.deepEqual(
      persistDerivable(input),
      {
        ok: false,
        reason: `${expectedPath} is not a JS-safe integer \u2014 the Python assembler cannot spell it `
        + `byte-identically, so the derived artifact would diverge`
      }
    )
  );
}
const HARDEN_ROWS = [
  ['backslash before quote', '{"d":"\\\\\\""}', '{"d":"\\u005c\\""}'],
  ['one backslash', '{"d":"\\\\"}', '{"d":"\\u005c"}'],
  ['two backslashes', '{"d":"\\\\\\\\"}', '{"d":"\\u005c\\u005c"}'],
  ['ordinary quote escape', '{"d":"\\""}', '{"d":"\\""}'],
  ['non-string', null, null],
];
for (const [name, input, expected] of HARDEN_ROWS) {
  test(`escape hardening: ${name}`, () => {
    assert.equal(hardenEscapeRuns(input), expected);
    assert.equal(hardenEscapeRuns(expected), expected);
    if (typeof input === 'string') assert.deepEqual(JSON.parse(expected), JSON.parse(input));
  });
}
for (const count of [1, 2, 3, 8, 40]) {
  test(`escape corpus: ${count} literal backslashes`, () => {
    const value = '\\'.repeat(count);
    const out = hardenEscapeRuns(JSON.stringify({ d: value }, null, 2));
    assert.equal(Math.max(...out.match(/\\+/g).map((run) => run.length)), 1);
    assert.deepEqual(JSON.parse(out), { d: value });
  });
}
