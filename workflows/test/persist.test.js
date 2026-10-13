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
const WRITER_FAILURE_GOLDEN = {
  artifactPaths: NULL_PATHS, gaps: ['writeArtifacts: writer returned null \u2014 artifacts not persisted (partial-artifacts)'],
  partial: true,
};
const DERIVED_ECHO_REASON = 'writer echo did not cover all three primary artifact paths (no write proof)';
const LEGACY_ECHO_REASON = 'writer echo did not account for all four planned artifact paths (no write proof)';
const REFUSED_RECEIPTS = [
  ['null receipt', () => null, 'assemble executor returned no receipt', []],
  ['primitive receipt', () => true, 'assemble executor returned no receipt', []],
  ['failure without errors', (r) => ({ ...r, ok: false, errors: null }), 'assemble script reported failure (no reason given)',
    [PATHS.findings, PATHS.report]],
  ['failure with two errors', (r) => ({ ...r, ok: false, errors: ['first', 'second'] }),
    'assemble script reported failure (first; second)', [PATHS.findings, PATHS.report]],
  ['missing checksum', (r) => { delete r.planChecksum; return r; },
    'assemble receipt echoed plan checksum none but the pipeline computed '
    + 'fnv1a32:0xc4e79613 \u2014 the executor did not run this persist plan', []],
  ['foreign checksum', (r) => ({ ...r, planChecksum: 'other-plan' }),
    'assemble receipt echoed plan checksum other-plan but the pipeline computed '
    + 'fnv1a32:0xc4e79613 \u2014 the executor did not run this persist plan', []],
  ['non-array verified', (r) => ({ ...r, verified: {} }), `assemble receipt did not verify ${PATHS.findings} (no content proof)`, []],
  ['unverified report', (r) => ({ ...r, verified: r.verified.slice(0, 1) }),
    `assemble receipt did not verify ${PATHS.report} (no content proof)`, [PATHS.findings]],
  ['non-array written', (r) => ({ ...r, written: {} }), `assemble receipt did not write ${PATHS.postReview} (no write proof)`,
    [PATHS.findings, PATHS.report]],
  ['unwritten checkpoint', (r) => ({ ...r, written: r.written.slice(0, 1) }),
    `assemble receipt did not write ${PATHS.checkpoints} (no write proof)`, [PATHS.findings, PATHS.report]],
  ['foreign expected chars', (r) => { r.verified[0].expected_chars = 9; return r; },
    `assemble receipt checked ${PATHS.findings} against a foreign expectation (receipt says `
    + `9 chars/fnv1a32:0x231bd64b, the pipeline handed the writer 1184/fnv1a32:0x231bd64b)`, [PATHS.report]],
  ['foreign expected checksum', (r) => { r.verified[0].expected_checksum = 'foreign'; return r; },
    `assemble receipt checked ${PATHS.findings} against a foreign expectation (receipt says `
    + `1184 chars/foreign, the pipeline handed the writer 1184/fnv1a32:0x231bd64b)`, [PATHS.report]],
  ['actual checksum contradicts match', (r) => { r.verified[0].checksum = 'foreign'; return r; },
    `assemble receipt is incoherent for ${PATHS.findings}: content_proof:"match" `
    + `contradicts its own chars/checksum (1184/foreign vs expected 1184/fnv1a32:0x231bd64b)`, [PATHS.report]],
  ['non-numeric actual chars', (r) => { r.verified[0].chars = 'ten'; return r; },
    `assemble receipt is incoherent for ${PATHS.findings}: content_proof:"match" `
    + `contradicts its own chars/checksum (ten/fnv1a32:0x231bd64b vs expected ` + `1184/fnv1a32:0x231bd64b)`, [PATHS.report]],
  ['non-string actual checksum', (r) => { r.verified[0].checksum = 17; return r; },
    `assemble receipt is incoherent for ${PATHS.findings}: content_proof:"match" `
    + `contradicts its own chars/checksum (1184/17 vs expected 1184/fnv1a32:0x231bd64b)`, [PATHS.report]],
  ['unknown proof with matching numbers', (r) => { r.verified[0].content_proof = 'pending'; return r; },
    `assemble receipt is incoherent for ${PATHS.findings}: content_proof:"pending" `
    + `contradicts its own chars/checksum (1184/fnv1a32:0x231bd64b vs expected ` + `1184/fnv1a32:0x231bd64b)`, [PATHS.report]],
  ['duplicate verified last unknown wins', (r) => { r.verified.push({ ...r.verified[0], content_proof: 'pending' }); return r; },
    `assemble receipt is incoherent for ${PATHS.findings}: content_proof:"pending" `
    + `contradicts its own chars/checksum (1184/fnv1a32:0x231bd64b vs expected ` + `1184/fnv1a32:0x231bd64b)`, [PATHS.report]],
  ['derived missing numbers', (r) => { r.written[0] = { path: PATHS.postReview }; return r; },
    `derived document ${PATHS.postReview} does not match the pipeline's own derivation `
    + `(receipt reports no chars/no checksum, the pipeline derived 1184/fnv1a32:0x231bd64b) `
    + `\u2014 the delivered payload is not what this run produced`, [PATHS.findings, PATHS.report]],
  ['derived foreign numbers', (r) => { r.written[0].chars = 19; r.written[0].checksum = 'foreign'; return r; },
    `derived document ${PATHS.postReview} does not match the pipeline's own derivation `
    + `(receipt reports 19 chars/foreign, the pipeline derived 1184/fnv1a32:0x231bd64b) `
    + `\u2014 the delivered payload is not what this run produced`, [PATHS.findings, PATHS.report]],
  ['actual chars contradict match', (r) => { r.verified[0].chars = 1185; return r; },
    `assemble receipt is incoherent for ${PATHS.findings}: content_proof:"match" `
    + `contradicts its own chars/checksum (1185/fnv1a32:0x231bd64b vs expected ` + `1184/fnv1a32:0x231bd64b)`, [PATHS.report]],
  ['mismatch contradicts matching numbers', (r) => { r.verified[0].content_proof = 'mismatch'; return r; },
    `assemble receipt is incoherent for ${PATHS.findings}: content_proof:"mismatch" `
    + `contradicts its own chars/checksum (1184/fnv1a32:0x231bd64b vs expected ` + `1184/fnv1a32:0x231bd64b)`, [PATHS.report]],
  ['fabricated primary expectations', (r) => { r.verified = r.verified.map(
    (e) => ({ ...e, chars: 10, expected_chars: 10, checksum: 'fnv1a32:0x00000001', expected_checksum: 'fnv1a32:0x00000001' }) );
    return r; }, `assemble receipt checked ${PATHS.findings} against a foreign expectation (receipt says `
    + `10 chars/fnv1a32:0x00000001, the pipeline handed the writer 1184/fnv1a32:0x231bd64b)`, []],
  ['derived chars differ', (r) => { r.written[0].chars = 1196; return r; },
    `derived document ${PATHS.postReview} does not match the pipeline's own derivation `
    + `(receipt reports 1196 chars/fnv1a32:0x231bd64b, the pipeline derived `
    + `1184/fnv1a32:0x231bd64b) \u2014 the delivered payload is not what this run produced`, [PATHS.findings, PATHS.report]],
  ['checkpoint checksum forged', (r) => { r.written[1].checksum = 'fnv1a32:0xfeedface'; return r; },
    `derived document ${PATHS.checkpoints} does not match the pipeline's own derivation `
    + `(receipt reports 1403 chars/fnv1a32:0xfeedface, the pipeline derived `
    + `1403/fnv1a32:0xea7a545d) \u2014 the delivered payload is not what this run produced`, [PATHS.findings, PATHS.report]],
  ['duplicate-id script refusal', (r) => ({ ...r, ok: false, errors: ['duplicate id F1 in source'] }),
    'assemble script reported failure (duplicate id F1 in source)', [PATHS.findings, PATHS.report]],
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
  + 'to the writer (expected 1184 chars/checksum fnv1a32:0x231bd64b, got 1185/fnv1a32:0x0000dead)';
const ACCEPTED_RECEIPTS = [
  ['honest', (r) => r, [], [PATHS.findings, PATHS.report]],
  ['unknown proof with different numbers', (r) => { r.verified[0].chars = 'ten'; r.verified[0].content_proof = 'pending'; return r; },
    [], [PATHS.report]],
  ['mismatch with non-numeric numbers',
    (r) => { r.verified[0].chars = 'ten'; r.verified[0].checksum = 17; r.verified[0].content_proof = 'mismatch'; return r; }, [
    `artifact-content-proof: ${PATHS.findings} bytes on disk differ from the payload handed `
    + `to the writer (expected 1184 chars/checksum fnv1a32:0x231bd64b, got ten/17)`], [PATHS.report]],
  ['duplicate verified last match wins', (r) => { r.verified.unshift({ ...r.verified[0], chars: 9 }); return r; }, [],
    [PATHS.findings, PATHS.report]],
  ['findings content differs without retry', (r) => {
    r.verified[0] = { ...r.verified[0], chars: 1185, checksum: 'fnv1a32:0x0000dead', content_proof: 'mismatch' }; return r; }, [
    FINDINGS_MISMATCH_GAP], [PATHS.report]],
  ['two primary mismatches without retry', (r) => {
    r.verified[0] = { ...r.verified[0], chars: 1185, checksum: 'fnv1a32:0x0000dead', content_proof: 'mismatch' };
    r.verified[1] = { ...r.verified[1], chars: 40, checksum: 'fnv1a32:0x0000dead', content_proof: 'mismatch' };
    return r;
  }, [
    FINDINGS_MISMATCH_GAP,
    `artifact-content-proof: ${PATHS.report} bytes on disk differ from the payload handed `
    + `to the writer (expected 39 chars/checksum fnv1a32:0xf241f3a0, got `
    + `40/fnv1a32:0x0000dead)`,
  ], []],
  ['diverged source explains both derived differences', (r) => {
    r.verified[0] = { ...r.verified[0], chars: 1185, checksum: 'fnv1a32:0x0000dead', content_proof: 'mismatch' };
    r.written[0] = { ...r.written[0], chars: 1189, checksum: 'fnv1a32:0x0000beef' };
    r.written[1] = { ...r.written[1], chars: 1408, checksum: 'fnv1a32:0x0000beef' };
    return r;
  }, [
    FINDINGS_MISMATCH_GAP,
    `artifact-content-proof: derived document ${PATHS.postReview} does not match the `
    + `pipeline's own derivation (receipt reports 1189 chars/fnv1a32:0x0000beef, the pipeline `
    + `derived 1184/fnv1a32:0x231bd64b) \u2014 expected, since ${PATHS.findings} it was `
    + `derived from also diverged`,
    `artifact-content-proof: derived document ${PATHS.checkpoints} does not match the `
    + `pipeline's own derivation (receipt reports 1408 chars/fnv1a32:0x0000beef, the pipeline `
    + `derived 1403/fnv1a32:0xea7a545d) \u2014 expected, since ${PATHS.findings} it was `
    + `derived from also diverged`,
  ], [PATHS.report]],
  ['diverged source explains missing derived numbers', (r) => {
    r.verified[0].chars = 9;
    r.verified[0].content_proof = 'mismatch';
    r.written[0] = { path: PATHS.postReview };
    return r;
  }, [
    `artifact-content-proof: ${PATHS.findings} bytes on disk differ from the payload handed `
    + `to the writer (expected 1184 chars/checksum fnv1a32:0x231bd64b, got `
    + `9/fnv1a32:0x231bd64b)`,
    `artifact-content-proof: derived document ${PATHS.postReview} does not match the `
    + `pipeline's own derivation (receipt reports no chars/no checksum, the pipeline derived `
    + `1184/fnv1a32:0x231bd64b) \u2014 expected, since ${PATHS.findings} it was derived from `
    + `also diverged`,
  ], [PATHS.report]],
];
for (const [name, patch, gaps, proven] of ACCEPTED_RECEIPTS) {
  test(`accepted receipt: ${name}`, async () => {
    const refused = persistCtx({ receipt: (r) => ({ ...patch(r), ok: false, errors: ['refused'] }) });
    const salvage = await writeArtifacts(refused, persistInput());
    assert.equal(salvage.partial, true);
    assert.deepEqual(salvage.artifactPaths, {
      findings: proven.includes(PATHS.findings) ? PATHS.findings : null,
      report: proven.includes(PATHS.report) ? PATHS.report : null,
      postReview: null,
      checkpoints: null,
    });
    assert.deepEqual(labels(refused), ['artifact-writer', 'assemble-artifacts', 'artifact-writer', 'assemble-artifacts']);
    const ctx = persistCtx({ receipt: patch });
    assert.deepEqual(await writeArtifacts(ctx, persistInput()), expectedWith(SUCCESS_GOLDEN, { gaps }));
    assert.deepEqual(labels(ctx), ['artifact-writer', 'assemble-artifacts']);
  });
}
const SALVAGE_ONLY_ROWS = [
  ['last duplicate mismatch wins', (r) => { r.verified.push({ ...r.verified[0], content_proof: 'mismatch' }); return r; },
    { report: PATHS.report }],
  ['report checksum contradicts match', (r) => { r.verified[1].checksum = 'foreign'; return r; }, { findings: PATHS.findings }],
  ['numeric string actual chars', (r) => { r.verified[0].chars = '1184'; return r; }, { report: PATHS.report }],
  ['self-consistent foreign report', (r) => ({ ...r, verified: [{ path: PATHS.report, chars: 9, expected_chars: 9,
    checksum: 'fnv1a32:0x00000009', expected_checksum: 'fnv1a32:0x00000009', content_proof: 'match', }], }), {},],
];
for (const [name, patch, expected] of SALVAGE_ONLY_ROWS) {
  test(`primary salvage: ${name}`, async () => {
    const ctx = persistCtx({ receipt: (r) => ({ ...patch(r), ok: false, errors: ['refused'] }) });
    const out = await writeArtifacts(ctx, persistInput());
    assert.equal(out.partial, true);
    assert.deepEqual(out.artifactPaths, expectedWith(NULL_PATHS, expected));
    assert.deepEqual(labels(ctx), ['artifact-writer', 'assemble-artifacts', 'artifact-writer', 'assemble-artifacts']);
  });
}

const WRITER_FAILURE_ROWS = [
  ['derived null', {}, () => null, 'writer returned null'],
  ['derived non-array written', {}, () => ({ written: {} }), DERIVED_ECHO_REASON],
  ['derived empty written', {}, () => ({ written: [] }), DERIVED_ECHO_REASON],
  ['derived missing plan', {}, (entries) => ({ written: entries.slice(0, 2).map((e) => e.path) }),
    DERIVED_ECHO_REASON],
  ['derived escape', {}, (entries) => ({ written: entries.map((e) => e.path).concat('/tmp/evil.json') }),
    'path-escape: writer-reported paths outside outputDir (written[3]=/tmp/evil.json)'],
  ['legacy null', { persist: {} }, () => null, 'writer returned null'],
  ['legacy missing echo', { persist: {} }, () => ({}), LEGACY_ECHO_REASON],
  ['legacy empty echo', { persist: {} }, () => ({ artifactPaths: {} }),
    LEGACY_ECHO_REASON],
  ...['findings', 'report', 'postReview', 'checkpoints'].map((missing) => [`legacy missing ${missing}`, { persist: {} },
    () => ({ artifactPaths: Object.fromEntries(Object.entries(PATHS).filter(([key]) => key !== missing)) }),
    LEGACY_ECHO_REASON]),
  ['legacy escaping checkpoint', { persist: {} }, () => ({ artifactPaths: { ...PATHS, checkpoints: '/tmp/evil-checkpoints.json' } }),
    'path-escape: writer-reported paths outside outputDir (checkpoints=/tmp/evil-checkpoints.json)'],
  ['legacy explicit null', { persist: {} }, () => ({ artifactPaths: { ...PATHS, report: null } }),
    'path-escape: writer-reported paths outside outputDir (report=null)'],
];
for (const [name, input, writer, reason] of WRITER_FAILURE_ROWS) {
  test(`writer refusal: ${name}`, async () => {
    const ctx = persistCtx({ writer });
    const out = await writeArtifacts(ctx, persistInput(input));
    assert.deepEqual(out, expectedWith(WRITER_FAILURE_GOLDEN, {
      gaps: [`writeArtifacts: ${reason} \u2014 artifacts not persisted (partial-artifacts)`],
    }));
    assert.deepEqual(labels(ctx), ['artifact-writer']);
  });
}
for (const mode of ['derived', 'legacy']) {
  for (
    const [name, thrown, detail] of [
      ['message', { message: 'writer broke' }, 'writer broke'],
      ['null', null, 'unknown'],
      ['empty object', {}, 'unknown'],
      ['Error', new Error('disk on fire'), 'disk on fire']
    ]
  ) {
    test(`writer exception: ${mode} ${name}`, async () => {
      const ctx = persistCtx({ writer: () => { throw thrown; } });
      assert.deepEqual(await writeArtifacts(ctx, persistInput(mode === 'legacy' ? { persist: {} } : {})), {
        artifactPaths: NULL_PATHS, gaps: [
          `writeArtifacts: writer agent threw (${detail}) \u2014 artifacts not persisted (partial-artifacts)`
        ], partial: true,
      });
      assert.deepEqual(labels(ctx), ['artifact-writer']);
    });
  }
}
for (
  const [name, thrown, detail] of [
    ['message', { message: 'executor broke' }, 'executor broke'],
    ['null', null, 'unknown'],
    ['empty object', {}, 'unknown']
  ]
) {
  test(`executor exception: ${name}`, async () => {
    const ctx = persistCtx({ receipt: () => { throw thrown; } });
    assert.deepEqual(await writeArtifacts(ctx, persistInput()), {
      artifactPaths: NULL_PATHS, gaps: [
        `writeArtifacts: assemble executor threw (${detail}) \u2014 artifacts not persisted (partial-artifacts)`
      ], partial: true,
    });
    assert.deepEqual(labels(ctx), ['artifact-writer', 'assemble-artifacts']);
  });
}
const RECEIPT_FENCE_ROWS = [
  ['null verified row', (r) => ({ ...r, verified: [...r.verified, null] }),
    'path-escape: writer-reported paths outside outputDir (verified[2]=null)'],
  ['null written row', (r) => ({ ...r, written: [...r.written, null] }),
    'path-escape: writer-reported paths outside outputDir (receipt.written[2]=null)'],
  ['two replaced paths', (r) => ( { ...r, verified: [{ ...r.verified[0], path: '/escape-a' }, r.verified[1]],
    written: [{ ...r.written[0], path: '/escape-b' }, r.written[1]] } ),
    'path-escape: writer-reported paths outside outputDir (verified[0]=/escape-a, receipt.written[0]=/escape-b)'],
  ['two appended paths', (r) => ( { ...r, verified: [...r.verified, { path: '/tmp/evil-verified.json' }],
    written: [...r.written, { path: '/tmp/evil-written.json' }] } ), 'path-escape: writer-reported paths outside outputDir '
    + '(verified[2]=/tmp/evil-verified.json, receipt.written[2]=/tmp/evil-written.json)'],
  ['fence precedes malformed failure text',
    (r) => ({ ...r, ok: false, errors: [{ toString: null }], verified: [{ ...r.verified[0], path: '/escape-a' }, r.verified[1]] }),
    'path-escape: writer-reported paths outside outputDir (verified[0]=/escape-a)'],
];
for (const [name, receipt, reason] of RECEIPT_FENCE_ROWS) {
  test(`receipt fence: ${name}`, async () => {
    const ctx = persistCtx({ receipt });
    assert.deepEqual(await writeArtifacts(ctx, persistInput()), expectedWith(WRITER_FAILURE_GOLDEN, {
      gaps: [`writeArtifacts: ${reason} \u2014 artifacts not persisted (partial-artifacts)`],
    }));
    assert.deepEqual(labels(ctx), ['artifact-writer', 'assemble-artifacts']);
  });
}
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
  ['derived mismatch can recover', (r) => { r.written[0].checksum = 'fnv1a32:0x00000000'; return r; }, (r) => r, false, PATHS, [
    `artifact-persist-retry: the first derived persist attempt failed (derived document `
    + `${PATHS.postReview} does not match the pipeline's own derivation (receipt reports 1184 `
    + `chars/fnv1a32:0x00000000, the pipeline derived 1184/fnv1a32:0x231bd64b) \u2014 the `
    + `delivered payload is not what this run produced); a second artifact-writer dispatch `
    + `succeeded and the artifacts below are from that attempt`]],
  ['retry executor throw remains partial', REFUSED, () => { throw new Error('injected executor throw'); }, true, NULL_PATHS, [
    'writeArtifacts: assemble executor threw (injected executor throw) \u2014 retried once '
    + 'after the first attempt failed (assemble script reported failure (findings.json is not '
    + 'valid JSON (line 99))) \u2014 artifacts not persisted (partial-artifacts)']],

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
for (const [name, indices, artifactPaths, suffix] of [
  ['none', [], NULL_PATHS, ''],
  [
    'findings only',
    [0],
    { findings: PATHS.findings },
    ` \u2014 content-proven on disk and still usable: ${PATHS.findings}`
  ],
  [
    'report only',
    [1],
    { report: PATHS.report },
    ` \u2014 content-proven on disk and still usable: ${PATHS.report}`
  ],
  [
    'both primaries',
    [0, 1],
    { findings: PATHS.findings, report: PATHS.report, postReview: null, checkpoints: null },
    ` \u2014 content-proven on disk and still usable: ${PATHS.findings}, ${PATHS.report}`
  ],
]) {
  test(`refused assembly salvage: ${name}`, async () => {
    const ctx = persistCtx({ receipt: (r) => ({ ...r, ok: false, errors: ['refused'], verified: indices.map((i) => r.verified[i]) }) });
    assert.deepEqual(await writeArtifacts(ctx, persistInput()), {
      artifactPaths: expectedWith(NULL_PATHS, artifactPaths), gaps: [
        `writeArtifacts: assemble script reported failure (refused) \u2014 retried once after `
        + `the first attempt failed (assemble script reported failure (refused)) \u2014 artifacts `
        + `not persisted (partial-artifacts)${suffix}`
      ], partial: true,
    });
    assert.deepEqual(labels(ctx), ['artifact-writer', 'assemble-artifacts', 'artifact-writer', 'assemble-artifacts']);
  });
}
const LEGACY_WRITER_LINE = 'Persist these code-gauntlet artifacts to disk exactly as given '
  + `(the workflow has no disk access). Write the payload's findings (as pretty JSON) to ${PATHS.findings}, `
  + `the payload's report (verbatim markdown) to ${PATHS.report}, the payload's postReview `
  + `(the pre-selected delivery set, as pretty JSON) to ${PATHS.postReview}, and the payload's checkpoints `
  + `(as JSON) to ${PATHS.checkpoints}. Return { artifactPaths } echoing the paths you wrote. `
  + 'The payload is the single JSON line after the marker below.';
const LEGACY_ROWS = [
  ['absent persist', { persist: undefined }, []], ['empty persist', { persist: {} }, []],
  ['numeric script', { persist: { assembleScriptPath: 5 } }, []],
  ['omitted findings and policy', {
    outputDir: '/repo/.code-gauntlet', headShaShort: 'abc1234', report: 'No findings.',
    postReview: [], checkpoints: { persisted: true }, persist: {},
  }, [], true],
  ['duplicate ids', { findings: [makeFinding('DUP'), makeFinding('DUP')] }, [
    'writeArtifacts: derived persistence unavailable (duplicate finding id DUP) \u2014 ' + 'persisted the full by-value payload instead'
    ]],
  ['missing id',
    { findings: [makeFinding('F1'), Object.fromEntries(Object.entries(makeFinding('F2')).filter(([key]) => key !== 'id'))] }, [
    'writeArtifacts: derived persistence unavailable (finding at index 1 has no usable '
    + 'string id) \u2014 persisted the full by-value payload instead']],
  ['unsafe number', { findings: [makeFinding('F1', { confidence: 0.9 })] }, [
    'writeArtifacts: derived persistence unavailable (findings[0].confidence is not a '
    + 'JS-safe integer \u2014 the Python assembler cannot spell it byte-identically, so the '
    + 'derived artifact would diverge) \u2014 persisted the full by-value payload instead']],
  ['return cannot derive duplicate ids', { findings: [makeFinding('DUP'), makeFinding('DUP')], persist: { returnPrimaries: true } }, [
    'writeArtifacts: derived persistence unavailable (duplicate finding id DUP) \u2014 ' + 'persisted the full by-value payload instead'
    ]],
];
for (const [name, patch, gaps, directInput = false] of LEGACY_ROWS) {
  test(`legacy channel: ${name}`, async () => {
    const ctx = persistCtx();
    const input = directInput ? patch : persistInput(patch);
    const out = await writeArtifacts(ctx, input);
    assert.deepEqual(out, expectedWith(SUCCESS_GOLDEN, { gaps }));
    assert.deepEqual(labels(ctx), ['artifact-writer']);
    const payload = parseWriterPayload(ctx.calls[0].prompt);
    assert.equal(ctx.calls[0].prompt.split('\n')[0], LEGACY_WRITER_LINE);
    assert.ok(!Array.isArray(payload));
    assert.deepEqual(payload, writerPayload(input));
    if (directInput) {
      assert.deepEqual(payload, {
        findings: [], postReview: [], report: 'No findings.', checkpoints: { persisted: true },
      });
    }
    assert.equal(out.persistReturn, undefined);
  });
}
for (const [name, writer, reason] of [
  ['null writer', () => null, 'writer returned null'],
  ['empty echo', () => ({ artifactPaths: {} }), LEGACY_ECHO_REASON],
]) {
  test(`fallback retains disclosure: ${name}`, async () => {
    const ctx = persistCtx({ writer });
    assert.deepEqual(await writeArtifacts(ctx, persistInput({ findings: [makeFinding('DUP'), makeFinding('DUP')] })), {
      artifactPaths: NULL_PATHS, partial: true, gaps: [
        'writeArtifacts: derived persistence unavailable (duplicate finding id DUP) \u2014 persisted the full by-value payload instead',
        `writeArtifacts: ${reason} \u2014 artifacts not persisted (partial-artifacts)`,
      ],
    });
    assert.deepEqual(labels(ctx), ['artifact-writer']);
  });
}
const OUTER_FAILURE_ROWS = [
  ['id-integrity guard', { findings: [makeFinding('F1', { confidence: 2n })] }, /BigInt/,
    'writeArtifacts: persistence threw before any artifact was written (Do not know how to '
    + 'serialize a BigInt) \u2014 artifacts not persisted (partial-artifacts)'],
  ['plan serialization', { checkpoints: { counts: { challenge: 2n } } }, /partial-artifacts/],
  ['malformed findings', { findings: {} }, new RegExp(
    String.raw`^writeArtifacts: persistence threw before any artifact was written \(.*\.map is not a function\)`
    + String.raw` \u2014 artifacts not persisted \(partial-artifacts\)$`, )],
];
for (const [name, input, reason, exactGap] of OUTER_FAILURE_ROWS) {
  test(`pre-dispatch failure: ${name}`, async () => {
    const ctx = persistCtx();
    const out = await writeArtifacts(ctx, persistInput(input));
    assert.equal(out.partial, true);
    assert.deepEqual(out.artifactPaths, NULL_PATHS);
    assert.equal(out.gaps.length, 1);
    assert.match(out.gaps[0], reason);
    assert.match(out.gaps[0], /partial-artifacts/);
    if (exactGap) assert.deepEqual(out, { artifactPaths: NULL_PATHS, gaps: [exactGap], partial: true });
    assert.deepEqual(labels(ctx), []);
    const dispatched = await writeArtifacts(persistCtx({ writer: () => { throw new Error('writer broke'); } }), persistInput());
    assert.deepEqual(Object.keys(out).sort(), Object.keys(dispatched).sort());
    assert.deepEqual(out.artifactPaths, dispatched.artifactPaths);
    assert.equal(out.partial, dispatched.partial);
  });
}
for (const [name, input, message] of [
  ['null input', null, 'outputDir must be an absolute confined root (POSIX /-prefix, no .. or \\ segments); got undefined'],
  [
    'missing root',
    persistInput({ outputDir: undefined }),
    'outputDir must be an absolute confined root (POSIX /-prefix, no .. or \\ segments); got undefined'
  ],
  [
    'relative root',
    persistInput({ outputDir: '.code-gauntlet' }),
    'outputDir must be an absolute confined root (POSIX /-prefix, no .. or \\ segments); got ".code-gauntlet"'
  ],
]) {
  test(`hard refusal: ${name}`, async () => {
    const ctx = persistCtx();
    await assert.rejects(writeArtifacts(ctx, input), { name: 'Error', message });
    assert.deepEqual(labels(ctx), []);
  });
}
for (const [name, input, paths] of [
  ['JSON input', JSON.stringify(persistInput({ persist: {} })), PATHS],
  ['missing head', persistInput({ headShaShort: undefined, persist: {} }), {
    findings: '/repo/.code-gauntlet/code-gauntlet-findings-head.json', report: '/repo/.code-gauntlet/code-gauntlet-report-head.md',
    postReview: '/repo/.code-gauntlet/code-gauntlet-post-review-head.json',
    checkpoints: '/repo/.code-gauntlet/code-gauntlet-checkpoint-all-head.json',
  }],
]) {
  test(`input decoding: ${name}`, async () => {
    const ctx = persistCtx({ writer: () => ({ artifactPaths: paths }) });
    assert.deepEqual(await writeArtifacts(ctx, input), { artifactPaths: paths, gaps: [], partial: false });
    assert.deepEqual(labels(ctx), ['artifact-writer']);
  });
}
const DERIVED_WRITER_LINE = 'Persist these code-gauntlet artifacts to disk exactly as given '
  + '(the workflow has no disk access). For every entry in the payload, write its "text" VERBATIM to its "path" '
  + '\u2014 byte for byte, nothing before it and NOTHING AFTER THE FINAL BYTE (no trailing commentary, no tool-call markup). '
  + 'Do not reformat, re-indent, or re-serialize. Return { written } listing the paths you wrote. '
  + 'The payload is the single JSON line after the marker below.';
const ASSEMBLE_LINE = 'Run exactly this command, then return its single line of stdout JSON verbatim via the schema:';
const COMMAND_WORDS = ['python3', SCRIPT, '--plan', PLAN_PATH];
const PROMPT_ROWS = [
  ['plain command', {}, undefined, undefined, { plainCommand: true }],
  ['omitted findings and policy', {
    outputDir: '/repo/.code-gauntlet', headShaShort: 'abc1234', report: 'No findings.',
    postReview: [], checkpoints: {}, persist: { assembleScriptPath: SCRIPT },
  }, undefined, undefined, { explicitInput: true, omittedDefaults: true }],
  ['space and apostrophe paths', {
    outputDir: '/My Repo/.code-gauntlet',
    persist: { assembleScriptPath: "/plug in/scripts/o'brien/assemble_artifacts.py" }
  }, [
    'python3',
    "/plug in/scripts/o'brien/assemble_artifacts.py",
    '--plan',
    '/My Repo/.code-gauntlet/code-gauntlet-persist-plan-abc1234.json'
  ], {
    findings: '/My Repo/.code-gauntlet/code-gauntlet-findings-abc1234.json',
    report: '/My Repo/.code-gauntlet/code-gauntlet-report-abc1234.md',
    postReview: '/My Repo/.code-gauntlet/code-gauntlet-post-review-abc1234.json',
    checkpoints: '/My Repo/.code-gauntlet/code-gauntlet-checkpoint-all-abc1234.json',
  }],
  ['escaped finding prose', { findings: [
    makeFinding('F1', { description: 'gradeInputProof produces \\"the executor\'s receipt carried no input_checksum\\" here' })] },
    undefined, undefined, { escapedEntry: 0 }],
  ['escaped checkpoint prose',
    { checkpoints: { phases: { verify: { gaps: ['verify: the executor wrote \\"receipt\\" to C:\\tmp and stopped'] } } } }, undefined,
    undefined, { escapedEntry: 2 }],
];
for (const [name, input, commandWords = COMMAND_WORDS, expectedPaths = PATHS, checks = {}] of PROMPT_ROWS) {
  test(`derived prompt: ${name}`, async () => {
    const ctx = persistCtx();
    const inp = checks.explicitInput ? input : persistInput(input);
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
    assert.deepEqual(entries.map((e) => e.path), [expectedPaths.findings, expectedPaths.report, commandWords[3]]);
    const primaries = persistPrimaries(inp);
    assert.equal(entries[0].text, primaries.findingsJson);
    assert.equal(entries[1].text, primaries.reportMd);
    if (checks.omittedDefaults) {
      assert.deepEqual(out, { artifactPaths: expectedPaths, gaps: [], partial: false });
      assert.equal(entries[0].text, '[]');
      assert.equal(primaries.findingsJson, '[]');
    }
    const plan = JSON.parse(entries[2].text);
    assert.deepEqual(plan, persistPlan(inp, expectedPaths));
    const { planChecksum, ...body } = plan;
    assert.equal(fnv1a32(JSON.stringify(body, null, 2)), planChecksum);
    assert.deepEqual(shellSplit(ctx.calls[1].prompt.split('\n').at(-1)), commandWords);
    if (checks.plainCommand) {
      assert.ok(ctx.calls[1].prompt.includes(`python3 ${SCRIPT} --plan ${PLAN_PATH}`));
      assert.equal(ctx.calls[1].prompt.split('\n').at(-1).split(' ').length, 4);
      assert.doesNotMatch(ctx.calls[1].prompt.split('\n').at(-1), /[$`;|&><(){}]/);
      assert.equal(ctx.calls[0].prompt.split('finding F1').length - 1, 1);
      assert.equal(JSON.stringify(writerPayload(inp)).split('finding F1').length - 1, 3);
    }
    if (checks.escapedEntry !== undefined) {
      assert.ok(Math.max(...ctx.calls[0].prompt.match(/\\+/g).map((run) => run.length)) <= 3);
      const text = entries[checks.escapedEntry].text;
      assert.equal(Math.max(...text.match(/\\+/g).map((run) => run.length)), 1);
      if (checks.escapedEntry === 0) {
        const finding = JSON.parse(entries[0].text)[0];
        assert.equal(finding.description, inp.findings[0].description);
        assert.equal(finding.body, inp.findings[0].description);
      } else assert.ok(text.includes('receipt'));
    }
  });
}
test('size bound: derived prompt is smaller than the repeated by-value documents', async () => {
  const findings = Array.from({ length: 60 }, (_, i) => makeFinding(`F${i}`));
  const input = persistInput({ findings });
  const ctx = persistCtx();
  await writeArtifacts(ctx, input);
  const planText = parseWriterPayload(ctx.calls[0].prompt).at(-1).text;
  const held = writerPayload(input);
  assert.ok(ctx.calls[0].prompt.length < JSON.stringify(held).length);
  assert.ok(planText.length < JSON.stringify(held.postReview, null, 2).length + JSON.stringify(held.checkpoints, null, 2).length);
  assert.equal(planText.split('finding F0').length - 1, 0);
  assert.equal(ctx.calls[0].prompt.split('finding F0').length - 1, 1);
});

const EMPTY_PLAN_TEXT = `{
  "planVersion": 2,
  "expect": [
    {
      "path": "/repo/.code-gauntlet/code-gauntlet-findings-abc1234.json",
      "chars": 2,
      "checksum": "fnv1a32:0x741638a5"
    },
    {
      "path": "/repo/.code-gauntlet/code-gauntlet-report-abc1234.md",
      "chars": 0,
      "checksum": "fnv1a32:0x811c9dc5"
    }
  ],
  "derive": [
    {
      "path": "/repo/.code-gauntlet/code-gauntlet-post-review-abc1234.json",
      "chars": 2,
      "checksum": "fnv1a32:0x741638a5"
    },
    {
      "path": "/repo/.code-gauntlet/code-gauntlet-checkpoint-all-abc1234.json",
      "chars": 2,
      "checksum": "fnv1a32:0x5465b825"
    }
  ],
  "postReview": {
    "path": "/repo/.code-gauntlet/code-gauntlet-post-review-abc1234.json",
    "source": "/repo/.code-gauntlet/code-gauntlet-findings-abc1234.json",
    "ids": [],
    "wrapper": null
  },
  "checkpoint": {
    "path": "/repo/.code-gauntlet/code-gauntlet-checkpoint-all-abc1234.json",
    "source": "/repo/.code-gauntlet/code-gauntlet-findings-abc1234.json",
    "challengeFindingIds": [],
    "stripAliasFields": [
      "line",
      "end_line",
      "body"
    ],
    "skeleton": {}
  },
  "planChecksum": "fnv1a32:0xd9b8627a"
}`;
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
  ['scriptless return', { persist: { returnPrimaries: true } }, null],
  ['escaped plan bytes shared by both channels', { persist: { returnPrimaries: true }, checkpoints: { ...persistInput().checkpoints,
    phases: { challenge: { ...persistInput().checkpoints.phases.challenge,
    gaps: ['verify: the executor wrote \\"receipt\\" to C:\\tmp and stopped'] } } } }, null, { escapedPlan: true }],
  ['exact return budget', { ...EMPTY_RETURN_INPUT, report: 'r'.repeat(998439) }, null, { exactBudget: true }],
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
        { path: PATHS.findings, chars: 2 }, { path: PATHS.report, chars: 998439 }, { path: PLAN_PATH, chars: 1194 },
      ] }));
    } else {
      const writer = persistCtx();
      await writeArtifacts(writer, { ...input, persist: { assembleScriptPath: SCRIPT } });
      assert.deepEqual(out.persistReturn.entries, parseWriterPayload(writer.calls[0].prompt));
      const { findingsJson, reportMd } = persistPrimaries(input);
      assert.equal(out.persistReturn.entries[0].text, findingsJson);
      assert.equal(out.persistReturn.entries[1].text, reportMd);
      assert.equal(JSON.parse(out.persistReturn.entries[2].text).expect[0].checksum, fnv1a32(findingsJson));
      if (checks.escapedPlan) assert.match(JSON.stringify(persistPlan(input, PATHS)), /\\\\/);
    }
  });
}
const OVERSIZED_ROWS = [
  ['script fallback', { assembleScriptPath: SCRIPT, returnPrimaries: true }, null, ['artifact-writer', 'assemble-artifacts'], false],
  ['scriptless fallback', { returnPrimaries: true }, null, ['artifact-writer'], false],
  ['fallback retains failure gap', { assembleScriptPath: SCRIPT, returnPrimaries: true },
    (r) => ({ ...r, ok: false, errors: ['refused'], verified: [] }),
    ['artifact-writer', 'assemble-artifacts', 'artifact-writer', 'assemble-artifacts'], true],
];
for (const [name, persist, receipt, dispatched, partial] of OVERSIZED_ROWS) {
  test(`return budget: ${name}`, async () => {
    const ctx = persistCtx({ receipt });
    const out = await writeArtifacts(ctx, persistInput({ ...EMPTY_RETURN_INPUT, persist, report: 'r'.repeat(1000000) }));
    assert.equal(out.partial, partial);
    assert.equal(out.persistReturn, undefined);
    assert.deepEqual(labels(ctx), dispatched);
    assert.deepEqual(out.artifactPaths, partial ? NULL_PATHS : PATHS);
    assert.deepEqual(out.gaps, [
      'writeArtifacts: the persisted primaries serialize to 1001562 chars, over the '
      + '1000000-char return budget \u2014 persisted through the artifact-writer instead, which '
      + 'transcribes the bytes',
      ...(
        partial ? [
          'writeArtifacts: assemble script reported failure (refused) \u2014 retried once after '
          + 'the first attempt failed (assemble script reported failure (refused)) \u2014 artifacts '
          + 'not persisted (partial-artifacts)'
        ] : []
      ),
    ]);
  });
}
// Finding arrays also exercise repeated prose rather than a single oversized report string.
for (const [name, persist, expectedLabels] of [
  [
    'derived fallback for oversized findings',
    { assembleScriptPath: SCRIPT, returnPrimaries: true },
    ['artifact-writer', 'assemble-artifacts']
  ],
  ['scriptless fallback for oversized findings', { returnPrimaries: true }, ['artifact-writer']],
]) {
  test(`return budget: ${name}`, async () => {
    const findings = Array.from({ length: 40 }, (_, i) => makeFinding(`F${i}`, { description: 'z'.repeat(30000) }));
    const ctx = persistCtx();
    const out = await writeArtifacts(ctx, persistInput({ findings, postReview: findings, persist }));
    assert.equal(out.partial, false);
    assert.equal(out.persistReturn, undefined);
    assert.deepEqual(out.artifactPaths, PATHS);
    assert.deepEqual(labels(ctx), expectedLabels);
    assert.match(out.gaps[0], /over the 1000000-char return budget/);
    assert.match(out.gaps[0], /transcribes the bytes/);
  });
}
for (const [name, input, expected] of [
  ['no marker', 'ordinary prompt', null],
  ['two markers use the last', 'PAYLOAD_JSON:{"first":true}\nPAYLOAD_JSON:{"last":true}', { last: true }],
]) {
  test(`writer payload parser: ${name}`, () => assert.deepEqual(parseWriterPayload(input), expected));
}
