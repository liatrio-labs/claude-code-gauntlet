import { test } from 'node:test';
import assert from 'node:assert/strict';
import { writeArtifacts, parseWriterPayload } from '../src/persist.js';
import { writerPayload, persistPrimaries, persistPlan } from '../src/persistPlan.js';
import { fnv1a32 } from '../src/wire.js';
import { makeFinding } from './helpers/pipelineMock.js';
import { shellSplit } from './helpers/shellWords.js';
import { PATHS, PLAN_PATH, expectedWith, persistInput, persistCtx } from './helpers/persistFixtures.js';

const SCRIPT = '/plugin/scripts/assemble_artifacts.py';
const NULL_PATHS = { findings: null, report: null, postReview: null, checkpoints: null };
const labels = (ctx) => ctx.calls.map((call) => call.label);
const SUCCESS_GOLDEN = { artifactPaths: PATHS, gaps: [], partial: false };

const PARTIAL_GOLDEN = { artifactPaths: NULL_PATHS, partial: true };
const DERIVED_ECHO_REASON = 'writer echo did not cover all three primary artifact paths (no write proof)';
const LEGACY_ECHO_REASON = 'writer echo did not account for all four planned artifact paths (no write proof)';
const REFUSED_RECEIPTS = [
  ['failure with two errors', (r) => ({ ...r, ok: false, errors: ['first', 'second'] }),
    'assemble script reported failure (first; second)', [PATHS.findings, PATHS.report]],
  ['foreign checksum', (r) => ({ ...r, planChecksum: 'other-plan' }),
    'assemble receipt echoed plan checksum other-plan but the pipeline computed '
    + 'fnv1a32:0xda6a80c2 \u2014 the executor did not run this persist plan', []],
  ['unverified report', (r) => ({ ...r, verified: r.verified.slice(0, 1) }),
    `assemble receipt did not verify ${PATHS.report} (no content proof)`, [PATHS.findings]],
  ['unwritten checkpoint', (r) => ({ ...r, written: r.written.slice(0, 1) }),
    `assemble receipt did not write ${PATHS.checkpoints} (no write proof)`, [PATHS.findings, PATHS.report]],
  ['foreign expected chars', (r) => { r.verified[0].expected_chars = 9; return r; },
    `assemble receipt checked ${PATHS.findings} against a foreign expectation (receipt says `
    + `9 chars/fnv1a32:0xf7c0f7c7, the pipeline handed the writer 1336/fnv1a32:0xf7c0f7c7)`, [PATHS.report]],
  ['actual checksum contradicts match', (r) => { r.verified[0].checksum = 'foreign'; return r; },
    `assemble receipt is incoherent for ${PATHS.findings}: content_proof:"match" `
    + `contradicts its own chars/checksum (1336/foreign vs expected 1336/fnv1a32:0xf7c0f7c7)`, [PATHS.report]],
  ['derived foreign numbers', (r) => { r.written[0].chars = 19; r.written[0].checksum = 'foreign'; return r; },
    `derived document ${PATHS.postReview} does not match the pipeline's own derivation `
    + `(receipt reports 19 chars/foreign, the pipeline derived 1336/fnv1a32:0xf7c0f7c7) `
    + `\u2014 the delivered payload is not what this run produced`, [PATHS.findings, PATHS.report]],
  ['null receipt', () => null, 'assemble executor returned no receipt', []],
  ['mismatched primary is not salvageable', (r) => {
    r.verified[0] = { ...r.verified[0], chars: 1185, checksum: 'fnv1a32:0x0000dead', content_proof: 'mismatch' };
    r.written = r.written.filter((_, i) => i !== 1);
    return r;
  }, `assemble receipt did not write ${PATHS.checkpoints} (no write proof)`, [PATHS.report]],
];
for (const [name, patch, reason, proven] of REFUSED_RECEIPTS) {
  test(`receipt refusal: ${name}`, async () => {
    const ctx = persistCtx({ receipt: patch });
    const out = await writeArtifacts(ctx, persistInput());
    const findings = proven.includes(PATHS.findings) ? PATHS.findings : null;
    const report = proven.includes(PATHS.report) ? PATHS.report : null;
    assert.deepEqual(out, {
      artifactPaths: { findings, report, postReview: null, checkpoints: null }, partial: true,
      gaps: [`writeArtifacts: ${reason} \u2014 retried once after the first attempt failed `
      + `(${reason}) \u2014 artifacts not persisted (partial-artifacts)`
        + (proven.length ? ` \u2014 content-proven on disk and still usable: ${proven.join(', ')}` : '')],
    });
    assert.deepEqual(labels(ctx), ['artifact-writer', 'assemble-artifacts', 'artifact-writer', 'assemble-artifacts']);
    for (const call of ctx.calls.filter((call) => call.label === 'artifact-writer')) {
      assert.ok(Array.isArray(parseWriterPayload(call.prompt)));
    }
  });
}
const FINDINGS_MISMATCH_GAP = `artifact-content-proof: ${PATHS.findings} bytes on disk differ from the payload handed `
  + 'to the writer (expected 1336 chars/checksum fnv1a32:0xf7c0f7c7, got 1185/fnv1a32:0x0000dead)';
const ACCEPTED_RECEIPTS = [
  ['honest', (r) => r, []],
  ['findings content differs without retry', (r) => {
    r.verified[0] = { ...r.verified[0], chars: 1185, checksum: 'fnv1a32:0x0000dead', content_proof: 'mismatch' }; return r; }, [
    FINDINGS_MISMATCH_GAP]],
  ['diverged source explains projected delivery differences', (r) => {
    r.verified[0] = { ...r.verified[0], chars: 1185, checksum: 'fnv1a32:0x0000dead', content_proof: 'mismatch' };
    r.written[0] = { ...r.written[0], chars: 1189, checksum: 'fnv1a32:0x0000beef' };
    return r;
  }, [
    FINDINGS_MISMATCH_GAP,
    `artifact-content-proof: derived document ${PATHS.postReview} does not match the `
    + `pipeline's own derivation (receipt reports 1189 chars/fnv1a32:0x0000beef, the pipeline `
    + `derived 1336/fnv1a32:0xf7c0f7c7) \u2014 expected, since ${PATHS.findings} it was `
    + `derived from also diverged`,
  ]],
];
for (const [name, patch, gaps] of ACCEPTED_RECEIPTS) {
  test(`accepted receipt: ${name}`, async () => {
    const ctx = persistCtx({ receipt: patch });
    assert.deepEqual(await writeArtifacts(ctx, persistInput()), expectedWith(SUCCESS_GOLDEN, { gaps }));
    assert.deepEqual(labels(ctx), ['artifact-writer', 'assemble-artifacts']);
  });
}

const WRITER_FAILURE_ROWS = [
  ['derived missing plan', {}, (entries) => ({ written: entries.slice(0, 2).map((e) => e.path) }),
    DERIVED_ECHO_REASON],
  ['derived escape', {}, (entries) => ({ written: entries.map((e) => e.path).concat('/tmp/evil.json') }),
    'path-escape: writer-reported paths outside outputDir (written[3]=/tmp/evil.json)'],
  ['legacy missing echo', { persist: undefined }, () => ({}), LEGACY_ECHO_REASON],
  ['legacy null', { persist: undefined }, () => null, 'writer returned null'],
  ['derived null', {}, () => null, 'writer returned null'],
  ['legacy escaping checkpoint', { persist: undefined }, () => ({ artifactPaths: { ...PATHS, checkpoints: '/tmp/evil-checkpoints.json' } }),
    'path-escape: writer-reported paths outside outputDir (checkpoints=/tmp/evil-checkpoints.json)'],
];
for (const [name, input, writer, reason] of WRITER_FAILURE_ROWS) {
  test(`writer refusal: ${name}`, async () => {
    const ctx = persistCtx({ writer });
    const out = await writeArtifacts(ctx, persistInput(input));
    assert.deepEqual(out, expectedWith(PARTIAL_GOLDEN, {
      gaps: [`writeArtifacts: ${reason} \u2014 artifacts not persisted (partial-artifacts)`],
    }));
    assert.deepEqual(labels(ctx), ['artifact-writer']);
  });
}

for (const [name, input, options, calls] of [
    ['derived writer', {}, { writer: () => { throw new Error('disk full'); } }, ['artifact-writer']],
  ['legacy writer', { persist: undefined }, { writer: () => { throw new Error('disk full'); } }, ['artifact-writer']],
  ['executor', {}, { receipt: () => { throw new Error('disk full'); } }, ['artifact-writer', 'assemble-artifacts']],
]) {
  test('exception: ' + name, async () => {
    const ctx = persistCtx(options);
    const out = await writeArtifacts(ctx, persistInput(input));
    assert.deepEqual(out.artifactPaths, NULL_PATHS);
    assert.equal(out.partial, true);
    assert.match(out.gaps[0], /threw \(disk full\).*partial-artifacts/);
    assert.deepEqual(labels(ctx), calls);
  });
}

test('receipt fence rejects appended paths in both receipt lists', async () => {
  const ctx = persistCtx({
    receipt: (r) => ({ ...r, verified: [...r.verified, { path: '/tmp/evil-verified.json' }],
      written: [...r.written, { path: '/tmp/evil-written.json' }] })
  });
  assert.deepEqual(await writeArtifacts(ctx, persistInput()), { ...PARTIAL_GOLDEN, gaps: ['writeArtifacts: path-escape: writer-reported paths outside outputDir (verified[2]=/tmp/evil-verified.json, receipt.written[2]=/tmp/evil-written.json) \u2014 artifacts not persisted (partial-artifacts)'] });
  assert.deepEqual(labels(ctx), ['artifact-writer', 'assemble-artifacts']);
});
const REFUSED = (r) => ({ ...r, ok: false, errors: ['findings.json is not valid JSON (line 99)'] });
const RETRY_ROWS = [
  ['recovers after structural refusal', REFUSED, (r) => r, false, PATHS, [
    'artifact-persist-retry: the first derived persist attempt failed (assemble script '
    + 'reported failure (findings.json is not valid JSON (line 99))); a second '
    + 'artifact-writer dispatch succeeded and the artifacts below are from that attempt']],
  ['second failure names both attempts', REFUSED, (r) => ({ ...r, ok: false, errors: ['duplicate id F1 in source'] }), true,
    { ...PATHS, postReview: null, checkpoints: null }, [
    `writeArtifacts: assemble script reported failure (duplicate id F1 in source) \u2014 `
    + `retried once after the first attempt failed (assemble script reported failure `
    + `(findings.json is not valid JSON (line 99))) \u2014 artifacts not persisted `
    + `(partial-artifacts) \u2014 content-proven on disk and still usable: ${PATHS.findings}, ` + `${PATHS.report}`]],
];
for (const [name, first, second, partial, artifactPaths, gaps] of RETRY_ROWS) {
  test(`one retry: ${name}`, async () => {
    const ctx = persistCtx({ receipt: (r, attempt) => (attempt === 1 ? first : second)(r) });
    assert.deepEqual(await writeArtifacts(ctx, persistInput()), { artifactPaths, gaps, partial });
    assert.deepEqual(labels(ctx), ['artifact-writer', 'assemble-artifacts', 'artifact-writer', 'assemble-artifacts']);
    for (const call of ctx.calls.filter((call) => call.label === 'artifact-writer')) {
      assert.ok(Array.isArray(parseWriterPayload(call.prompt)));
    }
  });
}
const LEGACY_WRITER_LINE = 'Persist these code-gauntlet artifacts to disk exactly as given '
  + `(the workflow has no disk access). Write the payload's findings (as pretty JSON) to ${PATHS.findings}, `
  + `the payload's report (verbatim markdown) to ${PATHS.report}, the payload's postReview `
  + `(the pre-selected delivery set, as pretty JSON) to ${PATHS.postReview}, and the payload's checkpoints `
  + `(as JSON) to ${PATHS.checkpoints}. Return { artifactPaths } echoing the paths you wrote. `
  + 'The payload is the single JSON line after the marker below.';
const LEGACY_ROWS = [
  ['absent persist', { persist: undefined }, []],
  ['duplicate ids', { findings: [makeFinding('DUP'), makeFinding('DUP')] }, [
    'writeArtifacts: derived persistence unavailable (duplicate finding id DUP) \u2014 ' + 'persisted the full by-value payload instead'
    ]],
  ['unsafe number', { findings: [makeFinding('F1', { confidence: 0.9 })] }, [
    'writeArtifacts: derived persistence unavailable (findings[0].confidence is not a '
    + 'JS-safe integer \u2014 the Python assembler cannot spell it byte-identically, so the '
    + 'derived artifact would diverge) \u2014 persisted the full by-value payload instead']],
];
for (const [name, patch, gaps] of LEGACY_ROWS) {
  test(`legacy channel: ${name}`, async () => {
    const ctx = persistCtx();
    const input = persistInput(patch);
    const out = await writeArtifacts(ctx, input);
    assert.deepEqual(out, expectedWith(SUCCESS_GOLDEN, { gaps }));
    assert.deepEqual(labels(ctx), ['artifact-writer']);
    const payload = parseWriterPayload(ctx.calls[0].prompt);
    assert.equal(ctx.calls[0].prompt.split('\n')[0], LEGACY_WRITER_LINE);
    assert.ok(!Array.isArray(payload));
    assert.deepEqual(payload, writerPayload(input));
    assert.equal(out.persistReturn, undefined);
  });
}

test('fallback retains disclosure when the legacy echo is empty', async () => {
  const ctx = persistCtx({ writer: () => ({ artifactPaths: {} }) });
  assert.deepEqual(await writeArtifacts(ctx, persistInput({ findings: [makeFinding('DUP'), makeFinding('DUP')] })), { artifactPaths: NULL_PATHS, partial: true, gaps: ['writeArtifacts: derived persistence unavailable (duplicate finding id DUP) \u2014 ' + 'persisted the full by-value payload instead', 'writeArtifacts: ' + LEGACY_ECHO_REASON + ' \u2014 artifacts not persisted (partial-artifacts)'] });
  assert.deepEqual(labels(ctx), ['artifact-writer']);
});

test('relative output root is refused before dispatch', async () => {
  const ctx = persistCtx();
  await assert.rejects(writeArtifacts(ctx, persistInput({ outputDir: '.code-gauntlet' })), { name: 'Error',
    message: 'outputDir must be an absolute confined root (no .. or \\ segments); got ".code-gauntlet"',
  });
  assert.deepEqual(labels(ctx), []);
});

test('JSON input uses the same artifact paths', async () => {
  const ctx = persistCtx({ writer: () => ({ artifactPaths: PATHS }) });
  const input = JSON.stringify(persistInput({ persist: undefined }));
  assert.deepEqual(await writeArtifacts(ctx, input), { artifactPaths: PATHS, gaps: [], partial: false });
  assert.deepEqual(labels(ctx), ['artifact-writer']);
});
const DERIVED_WRITER_LINE = 'Persist these code-gauntlet artifacts to disk exactly as given '
  + '(the workflow has no disk access). For every entry in the payload, write its "text" VERBATIM to its "path" '
  + '\u2014 byte for byte, nothing before it and NOTHING AFTER THE FINAL BYTE (no trailing commentary, no tool-call markup). '
  + 'Do not reformat, re-indent, or re-serialize. Return { written } listing the paths you wrote. '
  + 'The payload is the single JSON line after the marker below.';
const ASSEMBLE_LINE = 'Run exactly this command, then return its single line of stdout JSON verbatim via the schema:';
const COMMAND_WORDS = ['python3', SCRIPT, '--plan', PLAN_PATH];
const PROMPT_ROWS = [
  ...[['python', ['python']], ['py -3', ['py', '-3']]].map(([python, words]) => [
    `drive interpreter ${python}`, { python, pluginRoot: 'D:/plug in', outputDir: 'D:/Work/out', persist: { assembleScriptPath: '/untrusted.py' } },
    [...words, 'D:/plug in/scripts/assemble_artifacts.py', '--plan', 'D:/Work/out/code-gauntlet-persist-plan-abc1234.json'],
    { findings: 'D:/Work/out/code-gauntlet-findings-abc1234.json', report: 'D:/Work/out/code-gauntlet-report-abc1234.md',
      postReview: 'D:/Work/out/code-gauntlet-post-review-abc1234.json', checkpoints: 'D:/Work/out/code-gauntlet-checkpoint-all-abc1234.json' },
  ]),
  ['space and apostrophe paths', {
    outputDir: '/My Repo/.code-gauntlet',
    pluginRoot: "/plug in/o'brien", persist: {}
  }, [
    'python3',
    "/plug in/o'brien/scripts/assemble_artifacts.py",
    '--plan',
    '/My Repo/.code-gauntlet/code-gauntlet-persist-plan-abc1234.json'
  ], {
    findings: '/My Repo/.code-gauntlet/code-gauntlet-findings-abc1234.json',
    report: '/My Repo/.code-gauntlet/code-gauntlet-report-abc1234.md',
    postReview: '/My Repo/.code-gauntlet/code-gauntlet-post-review-abc1234.json',
    checkpoints: '/My Repo/.code-gauntlet/code-gauntlet-checkpoint-all-abc1234.json',
  }],
  ['escaped checkpoint prose',
    { checkpoints: { phases: { verify: { gaps: ['verify: the executor wrote \\"receipt\\" to C:\\tmp and stopped'] } } } }, undefined,
    undefined, { escapedEntry: 2 }],
];
for (const [name, input, commandWords = COMMAND_WORDS, expectedPaths = PATHS, checks = {}] of PROMPT_ROWS) {
  test(`derived prompt: ${name}`, async () => {
    const ctx = persistCtx();
    const inp = persistInput(input);
    const out = await writeArtifacts(ctx, inp);
    assert.equal(out.partial, false);
    assert.equal(out.persistReturn, undefined);
    assert.deepEqual(out.artifactPaths, expectedPaths);
    assert.deepEqual(Object.keys(out.artifactPaths), ['findings', 'report', 'postReview', 'checkpoints']);
    assert.deepEqual(out.gaps, []);
    assert.deepEqual(labels(ctx), ['artifact-writer', 'assemble-artifacts']);
    assert.equal(ctx.calls[0].prompt.split('\n')[0], DERIVED_WRITER_LINE);
    assert.equal(ctx.calls[1].prompt.split('\n')[0], ASSEMBLE_LINE);
    assert.equal(ctx.calls[0].agentType, 'code-gauntlet:artifact-writer');
    assert.equal(ctx.calls[1].agentType, 'code-gauntlet:executor');
    const entries = parseWriterPayload(ctx.calls[0].prompt);
    assert.deepEqual(entries.map((e) => e.path), [expectedPaths.findings, expectedPaths.report, commandWords.at(-1)]);
    const primaries = persistPrimaries(inp);
    assert.equal(entries[0].text, primaries.findingsJson);
    assert.equal(entries[1].text, primaries.reportMd);
    const plan = JSON.parse(entries[2].text);
    assert.deepEqual(plan, persistPlan(inp, expectedPaths));
    const { planChecksum, ...body } = plan;
    assert.equal(fnv1a32(JSON.stringify(body, null, 2)), planChecksum);
    assert.deepEqual(shellSplit(ctx.calls[1].prompt.split('\n').at(-1)), commandWords);
    if (checks.escapedEntry !== undefined) {
      assert.ok(Math.max(...ctx.calls[0].prompt.match(/\\+/g).map((run) => run.length)) <= 3);
      const text = entries[checks.escapedEntry].text;
      assert.equal(Math.max(...text.match(/\\+/g).map((run) => run.length)), 1);
      assert.ok(text.includes('receipt'));
    }
  });
}

const EMPTY_PLAN_TEXT = JSON.stringify({
  'planVersion': 3,
  'expect': [
    { 'path': PATHS.findings, 'chars': 2, 'checksum': 'fnv1a32:0x741638a5' },
    { 'path': PATHS.report, 'chars': 0, 'checksum': 'fnv1a32:0x811c9dc5' },
  ],
  'derive': [
    { 'path': PATHS.postReview, 'chars': 2, 'checksum': 'fnv1a32:0x741638a5' },
    { 'path': PATHS.checkpoints, 'chars': 2, 'checksum': 'fnv1a32:0x5465b825' },
  ],
  'postReview': { 'path': PATHS.postReview, 'source': PATHS.findings, 'ids': [], 'wrapper': null },
  'checkpoint': { 'path': PATHS.checkpoints, 'mode': 'literal', 'skeleton': {} },
  'planChecksum': 'fnv1a32:0xb6fb0e64',
}, null, 2);
const EMPTY_RETURN_INPUT = { findings: [], postReview: [], report: '', checkpoints: {}, persist: { returnPrimaries: true } };
const RETURN_GOLDEN = {
  artifactPaths: PATHS, gaps: [], partial: false,
  persistReturn: { channel: 'return', nonce: null, planPath: PLAN_PATH, entries: [
    { path: PATHS.findings, text: '[]' }, { path: PATHS.report, text: '' }, { path: PLAN_PATH, text: EMPTY_PLAN_TEXT },
  ] },
};
const RETURN_ROWS = [
  ['empty literal bytes and absent nonce', EMPTY_RETURN_INPUT, null, { literalBytes: true }],
  ['supplied nonce', { persist: { assembleScriptPath: SCRIPT, returnPrimaries: true }, nonce: 'nonce-xyz' }, 'nonce-xyz'],
  ['exact return budget', { ...EMPTY_RETURN_INPUT, report: 'r'.repeat(998615) }, null, { exactBudget: true }],
];
for (const [name, patch, nonce, checks = {}] of RETURN_ROWS) {
  test(`return channel: ${name}`, async () => {
    const ctx = persistCtx();
    const input = persistInput(patch);
    const out = await writeArtifacts(ctx, input);
    assert.deepEqual(labels(ctx), []);
    assert.equal(out.partial, false);
    assert.deepEqual(out.gaps, []);
    assert.deepEqual(out.artifactPaths, PATHS);
    assert.deepEqual(Object.keys(out.artifactPaths), ['findings', 'report', 'postReview', 'checkpoints']);
    assert.equal(out.persistReturn.channel, 'return');
    assert.equal(out.persistReturn.nonce, nonce);
    assert.equal(out.persistReturn.planPath, PLAN_PATH);
    assert.deepEqual(out.persistReturn.entries.map((e) => e.path), [PATHS.findings, PATHS.report, PLAN_PATH]);
    if (checks.literalBytes) assert.deepEqual(out, RETURN_GOLDEN);
    else if (checks.exactBudget) {
      assert.deepEqual({
        ...out, persistReturn: {
          ...out.persistReturn, entries: out.persistReturn.entries.map(({ path, text }) => ({ path, chars: text.length })),
        },
      }, expectedWith(RETURN_GOLDEN, { 'persistReturn.entries': [
        { path: PATHS.findings, chars: 2 }, { path: PATHS.report, chars: 998615 }, { path: PLAN_PATH, chars: 1034 },
      ] }));
    } else {
      const writer = persistCtx();
      await writeArtifacts(writer, { ...input, persist: { assembleScriptPath: SCRIPT } });
      assert.deepEqual(out.persistReturn.entries, parseWriterPayload(writer.calls[0].prompt));
      const { findingsJson, reportMd } = persistPrimaries(input);
      assert.equal(out.persistReturn.entries[0].text, findingsJson);
      assert.equal(out.persistReturn.entries[1].text, reportMd);
      assert.equal(JSON.parse(out.persistReturn.entries[2].text).expect[0].checksum, fnv1a32(findingsJson));
    }
  });
}
const OVERSIZED_ROWS = [
  ['script fallback', { assembleScriptPath: SCRIPT, returnPrimaries: true }, ['artifact-writer', 'assemble-artifacts']],
  ['scriptless fallback', { returnPrimaries: true }, ['artifact-writer'], { pluginRoot: undefined }],
];
for (const [name, persist, dispatched, extra] of OVERSIZED_ROWS) {
  test(`return budget: ${name}`, async () => {
    const ctx = persistCtx();
    const out = await writeArtifacts(ctx, persistInput({ ...EMPTY_RETURN_INPUT, persist, ...extra, report: 'r'.repeat(1000000) }));
    assert.equal(out.partial, false);
    assert.equal(out.persistReturn, undefined);
    assert.deepEqual(labels(ctx), dispatched);
    assert.deepEqual(out.artifactPaths, PATHS);
    assert.deepEqual(out.gaps, [
      'writeArtifacts: the persisted primaries serialize to 1001386 chars, over the '
      + '1000000-char return budget \u2014 persisted through the artifact-writer instead, which '
      + 'transcribes the bytes'
    ]);
  });
}
