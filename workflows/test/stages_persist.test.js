// stages_persist.test.js — the persistence redesign (issue #38, D3).
//
// The artifact-writer now persists only the UNIQUE content (findings.json,
// report.md, the persist plan); the two PROJECTIONS (post-review delivery set,
// resume checkpoint) are derived on disk by scripts/assemble_artifacts.py, run
// by the executor, which returns a content-proof receipt.
//
// What these tests pin:
//   1. persistPlan is a pure, directly-testable projection description.
//   2. The fnv1a32-over-UTF-16-code-units checksum matches the Python twin
//      (the constants below were produced by scripts/assemble_artifacts.py;
//      tests/test_assemble_artifacts.py runs the live node-vs-python parity).
//   3. writeArtifacts' PUBLIC contract is unchanged: same return shape, the same
//      four artifactPaths keys, the same partial-artifacts degradation.
//   4. A structural failure degrades (partial); a content-proof MISMATCH does not
//      — it derives from on-disk truth and raises a loud gap. Never-fabricate.
//   5. The id-integrity guard falls back to the legacy full by-value writer
//      prompt rather than degrading a run on pathological input.
//   6. IN-RUN BYTE IDENTITY (issue #38 requirement 2's verification clause):
//      applying the plan's derivation rules to the primaries reproduces
//      writerPayload(inp).postReview and .checkpoints EXACTLY.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  writeArtifacts, writerPayload, plannedArtifactPaths, persistPlanPath,
  persistPlan, persistPrimaries, persistDerivable, fnv1a32, normalizeForChecksum,
  parseWriterPayload, runWith, hardenEscapeRuns, provenPrimaryPaths,
} from '../src/stages.js';
import { validateArgs } from '../src/args.js';
import { makeFinding, validArgs, makeCtx } from './helpers/pipelineMock.js';

const OUT_DIR = '.code-gauntlet';
const SHA = 'abc1234';
const PATHS = plannedArtifactPaths(OUT_DIR, SHA);
const PLAN_PATH = `${OUT_DIR}/code-gauntlet-persist-plan-${SHA}.json`;
const SCRIPT = '/plugin/scripts/assemble_artifacts.py';

// A persisted-shape input: findings, the delivery subset, the slim checkpoint whose
// challenge phase carries the SAME finding objects (exactly what runWith hands over).
function persistInput(over = {}) {
  const findings = over.findings || [makeFinding('F1'), makeFinding('F2')];
  const base = {
    findings,
    postReview: findings,
    report: '# Code Gauntlet\n\nfindings rendered here',
    checkpoints: {
      phases: {
        challenge: {
          findings,
          unverified: [],
          eliminated: [],
          gaps: [],
          stats: { final_count: findings.length },
          generated_at: '2026-07-27T00:00:00Z',
        },
      },
      completed: ['summarize', 'discover', 'challenge'],
      phaseReached: 'report',
      counts: { challenge: findings.length },
    },
    outputDir: OUT_DIR,
    headShaShort: SHA,
    policy: {},
    persist: { assembleScriptPath: SCRIPT },
  };
  return { ...base, ...over, findings };
}

// The receipt an HONEST assemble_artifacts.py run would return for `plan`: it echoes the
// plan's own checksum and the plan's own expectations. Built from the plan the writer was
// actually handed, because trustAssembleReceipt now grades the receipt against the values
// the PIPELINE computed — a hard-coded receipt is exactly the fabrication it rejects.
//
// The `written` entries carry the chars/checksum the script computes over the documents it
// actually derived; an honest run reproduces the plan's `derive[]` expectations exactly (the
// script re-derives from the same findings.json with the same pretty printer), so this mock
// reads them straight off the plan rather than inventing numbers.
function receiptFor(plan) {
  const proof = (e) => ({
    path: e.path,
    chars: e.chars,
    expected_chars: e.chars,
    checksum: e.checksum,
    expected_checksum: e.checksum,
    content_proof: 'match',
  });
  return {
    ok: true,
    planVersion: 2,
    planChecksum: plan.planChecksum,
    verified: plan.expect.map(proof),
    written: plan.derive.map((e) => ({ path: e.path, chars: e.chars, checksum: e.checksum })),
    errors: [],
  };
}

// Mock ctx for the persist path. The artifact-writer honours BOTH payload shapes:
// the derived one (entries carrying `text`, echoed via { written }) and the legacy
// full by-value one (echoed via { artifactPaths }), so a test can tell which path ran.
//
// `opts.receiptFrom(honest)` patches the receipt the mock executor returns, starting from
// the honest one derived from the plan the writer was handed (drop a path, flip ok, stamp
// a mismatch, forge a checksum...). `opts.receipt` still forces a literal value.
function persistCtx(opts = {}) {
  const calls = [];
  let seenPlan = null;
  const agent = async (prompt, dispatch = {}) => {
    const label = dispatch.label || '';
    calls.push({ prompt, ...dispatch });
    if (opts.throwOn === label) throw new Error(`injected throw on ${label}`);
    if (label === 'artifact-writer') {
      const payload = parseWriterPayload(prompt);
      if (Array.isArray(payload)) {
        seenPlan = JSON.parse(payload[payload.length - 1].text);
        if (opts.writtenEcho) return { written: opts.writtenEcho(payload) };
        return { written: payload.map((e) => e.path) };
      }
      return { artifactPaths: plannedArtifactPaths(OUT_DIR, SHA) };
    }
    if (label === 'assemble-artifacts') {
      if ('receipt' in opts) return opts.receipt;
      const honest = receiptFor(seenPlan);
      return opts.receiptFrom ? opts.receiptFrom(honest) : honest;
    }
    return null;
  };
  return { calls, agent, parallel: async (thunks) => Promise.all(thunks.map((t) => t())) };
}

const labels = (ctx) => ctx.calls.map((c) => c.label);

// --- Checksum ---------------------------------------------------------------

// Vectors produced by scripts/assemble_artifacts.py's fnv1a32 (the Python twin).
// tests/test_assemble_artifacts.py::TestCrossRuntimeChecksumParity runs the live
// node-vs-python comparison over the same class of inputs; these constants make a
// drift on EITHER side fail here too, without a subprocess.
const CHECKSUM_VECTORS = [
  ['', 'fnv1a32:0x811c9dc5'],
  ['a', 'fnv1a32:0xe40c292c'],
  ['hello world', 'fnv1a32:0xd58b3fa7'],
  ['café — naïve', 'fnv1a32:0xee9e4013'],
  ['日本語のテキスト', 'fnv1a32:0xa12d849c'],
  ['😀🎉', 'fnv1a32:0x07bbe09f'],
  ['𝕏 astral', 'fnv1a32:0xb9ade380'],
];

test('fnv1a32 matches the Python twin for ascii, CJK and astral-plane input', () => {
  for (const [input, expected] of CHECKSUM_VECTORS) {
    assert.equal(fnv1a32(input), expected, `checksum drift for ${JSON.stringify(input)}`);
  }
});

test('fnv1a32 walks UTF-16 code units (surrogate pairs count as two)', () => {
  // Same code units, different codepoints: proves charCodeAt semantics, and that an
  // emoji is not silently collapsed to one unit (the surrogate-pair trap).
  assert.notEqual(fnv1a32('😀'), fnv1a32('ab'));
  assert.equal('😀'.length, 2);
  assert.match(fnv1a32('anything'), /^fnv1a32:0x[0-9a-f]{8}$/);
});

test('fnv1a32 needs no host globals (no TextEncoder/Buffer in the sandbox)', () => {
  const saved = { TextEncoder: globalThis.TextEncoder, Buffer: globalThis.Buffer };
  delete globalThis.TextEncoder;
  delete globalThis.Buffer;
  try {
    assert.equal(fnv1a32('hello world'), 'fnv1a32:0xd58b3fa7');
  } finally {
    globalThis.TextEncoder = saved.TextEncoder;
    globalThis.Buffer = saved.Buffer;
  }
});

test('normalizeForChecksum strips a BOM and AT MOST one trailing newline', () => {
  assert.equal(normalizeForChecksum('﻿abc'), 'abc');
  assert.equal(normalizeForChecksum('abc\n'), 'abc');
  assert.equal(normalizeForChecksum('abc\r\n'), 'abc');
  assert.equal(normalizeForChecksum('abc\n\n'), 'abc\n');
  assert.equal(normalizeForChecksum('abc'), 'abc');
});

// --- persistPlan (pure) -----------------------------------------------------

test('persistPlan describes the two primaries with chars + checksum over the exact strings', () => {
  const inp = persistInput();
  const plan = persistPlan(inp, PATHS);
  const { findingsJson, reportMd } = persistPrimaries(inp);
  assert.equal(plan.planVersion, 2);
  assert.deepEqual(plan.expect.map((e) => e.path), [PATHS.findings, PATHS.report]);
  assert.equal(plan.expect[0].chars, findingsJson.length);
  assert.equal(plan.expect[0].checksum, fnv1a32(findingsJson));
  assert.equal(plan.expect[1].chars, normalizeForChecksum(reportMd).length);
  assert.equal(plan.expect[1].checksum, fnv1a32(normalizeForChecksum(reportMd)));
});

test('persistPlan projects postReview by ORDERED id out of findings.json', () => {
  const findings = [makeFinding('A'), makeFinding('B'), makeFinding('C')];
  const inp = persistInput({ findings, postReview: [findings[2], findings[0]] });
  const plan = persistPlan(inp, PATHS);
  assert.deepEqual(plan.postReview.ids, ['C', 'A']);
  assert.equal(plan.postReview.source, PATHS.findings);
  assert.equal(plan.postReview.path, PATHS.postReview);
});

test('persistPlan emits wrapper:null without a PR identity and the envelope with one', () => {
  const bare = persistPlan(persistInput(), PATHS);
  assert.equal(bare.postReview.wrapper, null);

  const id = { owner: 'o', repo: 'r', pr_number: 7, sha_full: 'deadbeefcafe' };
  const wrapped = persistPlan(persistInput({ prIdentity: id }), PATHS);
  assert.deepEqual(wrapped.postReview.wrapper, {
    owner: 'o', repo: 'r', pr_number: 7, sha: 'deadbeefcafe', review_body: '',
  });
  // Key ORDER is the wire contract: post_review.py's envelope, findings appended last.
  assert.deepEqual(Object.keys(wrapped.postReview.wrapper), ['owner', 'repo', 'pr_number', 'sha', 'review_body']);
});

test('persistPlan skeleton is the checkpoint MINUS the challenge findings, key order intact', () => {
  const inp = persistInput();
  const plan = persistPlan(inp, PATHS);
  const challenge = plan.checkpoint.skeleton.phases.challenge;
  assert.deepEqual(challenge.findings, [], 'findings emptied — they are derived from findings.json');
  assert.deepEqual(
    Object.keys(challenge),
    ['findings', 'unverified', 'eliminated', 'gaps', 'stats', 'generated_at'],
    'the placeholder keeps its position so the derived checkpoint is key-order-identical',
  );
  assert.deepEqual(plan.checkpoint.challengeFindingIds, ['F1', 'F2']);
  assert.deepEqual(plan.checkpoint.stripAliasFields, ['line', 'end_line', 'body']);
  assert.deepEqual(plan.checkpoint.skeleton.completed, ['summarize', 'discover', 'challenge']);
  assert.equal(plan.checkpoint.skeleton.phaseReached, 'report');
});

test('persistPlan never mutates its input checkpoint', () => {
  const inp = persistInput();
  const before = JSON.stringify(inp.checkpoints);
  persistPlan(inp, PATHS);
  assert.equal(JSON.stringify(inp.checkpoints), before);
});

// --- The plan's self-proof (issue #38 L1-2) ---------------------------------

test('persistPlan appends planChecksum LAST, over the plan MINUS that field', () => {
  const plan = persistPlan(persistInput(), PATHS);
  const keys = Object.keys(plan);
  assert.equal(keys[keys.length - 1], 'planChecksum', 'appended last so Python can delete it and recover this object');
  const { planChecksum, ...body } = plan;
  assert.equal(planChecksum, fnv1a32(JSON.stringify(body, null, 2)));
  assert.match(planChecksum, /^fnv1a32:0x[0-9a-f]{8}$/);
});

test('planChecksum covers the id lists — an elided delivery id changes it', () => {
  // The failure this closes: a writer that transcribes the plan with two entries
  // missing from postReview.ids yields a silently SMALLER delivered set with an
  // ok:true receipt. The proof has to move when the id list moves.
  const findings = [makeFinding('A'), makeFinding('B'), makeFinding('C')];
  const full = persistPlan(persistInput({ findings }), PATHS);
  const elided = JSON.parse(JSON.stringify(full));
  elided.postReview.ids = ['A', 'B'];
  delete elided.planChecksum;
  assert.notEqual(fnv1a32(JSON.stringify(elided, null, 2)), full.planChecksum);

  const reordered = JSON.parse(JSON.stringify(full));
  reordered.postReview.ids = ['C', 'B', 'A'];
  delete reordered.planChecksum;
  assert.notEqual(fnv1a32(JSON.stringify(reordered, null, 2)), full.planChecksum, 'order into the delivered artifact is covered too');

  const cpElided = JSON.parse(JSON.stringify(full));
  cpElided.checkpoint.challengeFindingIds = ['A'];
  delete cpElided.planChecksum;
  assert.notEqual(fnv1a32(JSON.stringify(cpElided, null, 2)), full.planChecksum);
});

// --- The DERIVED documents' content proof (issue #38 F1-persist-1/F4-4) ------

test('persistPlan carries a chars+checksum expectation for BOTH derived documents', () => {
  // Path presence was the only gate on the two documents that actually reach the user.
  // The expectation is computed from writerPayload — the same source the legacy by-value
  // path persists — through the same normalizeForChecksum + fnv1a32 as the primaries.
  const inp = persistInput();
  const plan = persistPlan(inp, PATHS);
  const held = writerPayload(inp);
  assert.deepEqual(plan.derive.map((e) => e.path), [PATHS.postReview, PATHS.checkpoints]);

  const postText = JSON.stringify(held.postReview, null, 2);
  assert.equal(plan.derive[0].chars, normalizeForChecksum(postText).length);
  assert.equal(plan.derive[0].checksum, fnv1a32(normalizeForChecksum(postText)));

  const cpText = JSON.stringify(held.checkpoints, null, 2);
  assert.equal(plan.derive[1].chars, normalizeForChecksum(cpText).length);
  assert.equal(plan.derive[1].checksum, fnv1a32(normalizeForChecksum(cpText)));
});

test('the derived expectation costs the dispatch only a checksum, never the documents', () => {
  // The whole point of the derived path: the two projections' CONTENT still never crosses
  // the writer prompt. A ~40-byte proof each is what this buys the byte-identity claim.
  const findings = Array.from({ length: 40 }, (_, i) => makeFinding(`F${i}`));
  const inp = persistInput({ findings });
  const plan = persistPlan(inp, PATHS);
  const planText = JSON.stringify(plan, null, 2);
  const held = writerPayload(inp);
  const documents = JSON.stringify(held.postReview, null, 2) + JSON.stringify(held.checkpoints, null, 2);
  assert.ok(planText.length < documents.length, 'the whole plan stays smaller than the documents it proves');
  // Each finding's prose still crosses the plan zero times (findings.json owns it).
  assert.equal(planText.split(makeFinding('F0').title).length - 1, 0);
});

test('the derived expectation matches what an INDEPENDENT derivation produces', () => {
  // deriveFromPlan mirrors scripts/assemble_artifacts.py. If the plan's own expectation did
  // not agree with it, every honest run would degrade — this is the guard against a proof
  // that is precise but wrong.
  const inp = persistInput({ prIdentity: { owner: 'o', repo: 'r', pr_number: 7, sha_full: 'deadbeefcafe' } });
  const plan = persistPlan(inp, PATHS);
  const { findingsJson } = persistPrimaries(inp);
  const derived = deriveFromPlan(plan, findingsJson);
  const texts = [
    JSON.stringify(derived.postReview, null, 2),
    JSON.stringify(derived.checkpoints, null, 2),
  ];
  plan.derive.forEach((want, i) => {
    assert.equal(normalizeForChecksum(texts[i]).length, want.chars);
    assert.equal(fnv1a32(normalizeForChecksum(texts[i])), want.checksum);
  });
});

test('receipt gate: a derived document whose CONTENT differs degrades to partial-artifacts', async () => {
  // A derived mismatch is STRUCTURAL, unlike a primary mismatch: the primaries are still on
  // disk to derive from, but a derived document that disagrees with the pipeline has no
  // second copy — the derivation itself is what went wrong.
  const ctx = persistCtx({
    receiptFrom: (r) => ({ ...r, written: [{ ...r.written[0], chars: r.written[0].chars + 12 }, r.written[1]] }),
  });
  const out = await writeArtifacts(ctx, persistInput());
  assert.equal(out.partial, true);
  assert.equal(out.artifactPaths.postReview, null);
  assert.ok(out.gaps.some((g) => /does not match the pipeline's own derivation/.test(g)), out.gaps);
});

test('receipt gate: a FORGED derived checksum degrades to partial-artifacts', async () => {
  const ctx = persistCtx({
    receiptFrom: (r) => ({ ...r, written: [r.written[0], { ...r.written[1], checksum: 'fnv1a32:0xfeedface' }] }),
  });
  const out = await writeArtifacts(ctx, persistInput());
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => /derived document/.test(g) && /checkpoint-all/.test(g)), out.gaps);
});

test('a derived difference EXPLAINED by a diverged findings.json stays non-fatal', async () => {
  // Both derived documents project findings.json alone. When findings.json's own content
  // proof mismatched — a case this pipeline deliberately keeps non-fatal — the derived
  // documents faithfully project the divergent on-disk bytes and MUST differ. Failing there
  // would turn the tolerated primary mismatch into a lost run.
  const ctx = persistCtx({
    receiptFrom: (r) => ({
      ...r,
      verified: [asMismatch(r.verified[0]), r.verified[1]],
      written: r.written.map((e) => ({ ...e, chars: e.chars + 5, checksum: 'fnv1a32:0x0000beef' })),
    }),
  });
  const out = await writeArtifacts(ctx, persistInput());
  assert.equal(out.partial, false, 'the run keeps its artifacts');
  assert.deepEqual(out.artifactPaths, PATHS);
  assert.equal(out.gaps.length, 3, `one primary + two derived gaps, got: ${out.gaps}`);
  assert.ok(out.gaps[0].includes(PATHS.findings), 'the primary mismatch is reported first');
  assert.ok(out.gaps.slice(1).every((g) => /also diverged/.test(g)), out.gaps);
});

test('receipt gate: a written entry with NO content numbers at all degrades', async () => {
  // The pre-F1-persist-1 receipt: a path and nothing else. It must no longer pass.
  const ctx = persistCtx({ receiptFrom: (r) => ({ ...r, written: r.written.map((e) => ({ path: e.path })) }) });
  const out = await writeArtifacts(ctx, persistInput());
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => /does not match the pipeline's own derivation/.test(g)), out.gaps);
});

test('persistPlanPath matches the Phase 2 stale-file glob code-gauntlet-*-<sha>.*', () => {
  assert.equal(persistPlanPath(OUT_DIR, SHA), PLAN_PATH);
  assert.match(persistPlanPath(OUT_DIR, SHA), /\/code-gauntlet-.*-abc1234\..+$/);
});

// --- IN-RUN BYTE IDENTITY (issue #38 requirement 2) -------------------------

// An INDEPENDENT reimplementation of the plan's derivation rules — deliberately not
// shared with the production code, so this test proves the rules themselves, not that
// one function equals itself. Mirrors scripts/assemble_artifacts.py exactly.
function deriveFromPlan(plan, findingsJson) {
  const source = JSON.parse(findingsJson);
  const byId = new Map(source.map((f) => [f.id, f]));
  const projected = plan.postReview.ids.map((id) => byId.get(id));
  const postReview = plan.postReview.wrapper === null
    ? projected
    : { ...plan.postReview.wrapper, findings: projected };
  const strip = new Set(plan.checkpoint.stripAliasFields);
  const checkpoints = JSON.parse(JSON.stringify(plan.checkpoint.skeleton));
  const challenge = checkpoints.phases && checkpoints.phases.challenge;
  if (challenge) {
    challenge.findings = plan.checkpoint.challengeFindingIds.map((id) => {
      const out = {};
      for (const [k, v] of Object.entries(byId.get(id))) if (!strip.has(k)) out[k] = v;
      return out;
    });
  }
  return { postReview, checkpoints };
}

test('in-run byte identity: the derived artifacts EQUAL the strings the pipeline holds', () => {
  const inp = persistInput();
  const { findingsJson } = persistPrimaries(inp);
  const derived = deriveFromPlan(persistPlan(inp, PATHS), findingsJson);
  const held = writerPayload(inp);
  assert.equal(
    JSON.stringify(derived.postReview, null, 2),
    JSON.stringify(held.postReview, null, 2),
    'derived post-review must be byte-identical to writerPayload().postReview',
  );
  assert.equal(
    JSON.stringify(derived.checkpoints, null, 2),
    JSON.stringify(held.checkpoints, null, 2),
    'derived checkpoint must be byte-identical to writerPayload().checkpoints',
  );
  assert.equal(findingsJson, JSON.stringify(held.findings, null, 2));
});

test('in-run byte identity holds with the PR-identity wrapper too', () => {
  const inp = persistInput({ prIdentity: { owner: 'o', repo: 'r', pr_number: 7, sha_full: 'deadbeefcafe' } });
  const { findingsJson } = persistPrimaries(inp);
  const derived = deriveFromPlan(persistPlan(inp, PATHS), findingsJson);
  const held = writerPayload(inp);
  assert.equal(JSON.stringify(derived.postReview, null, 2), JSON.stringify(held.postReview, null, 2));
});

test('in-run byte identity holds for a capped delivery subset and non-ascii prose', () => {
  const findings = [
    makeFinding('A', { description: '日本語の説明 😀 with an astral 𝕏 character' }),
    makeFinding('B'),
    makeFinding('C', { title: '中文标题 🎉' }),
  ];
  const inp = persistInput({ findings, postReview: [findings[1]] });
  const { findingsJson } = persistPrimaries(inp);
  const derived = deriveFromPlan(persistPlan(inp, PATHS), findingsJson);
  const held = writerPayload(inp);
  assert.equal(JSON.stringify(derived.postReview, null, 2), JSON.stringify(held.postReview, null, 2));
  assert.equal(JSON.stringify(derived.checkpoints, null, 2), JSON.stringify(held.checkpoints, null, 2));
});

// --- persistDerivable (the id-integrity guard) ------------------------------

test('persistDerivable accepts a well-formed persist input', () => {
  assert.deepEqual(persistDerivable(persistInput()), { ok: true });
});

test('persistDerivable rejects a missing id', () => {
  const findings = [makeFinding('F1'), makeFinding('F2')];
  delete findings[1].id;
  const out = persistDerivable(persistInput({ findings }));
  assert.equal(out.ok, false);
  assert.match(out.reason, /id/);
});

test('persistDerivable rejects duplicate ids', () => {
  const out = persistDerivable(persistInput({ findings: [makeFinding('F1'), makeFinding('F1')] }));
  assert.equal(out.ok, false);
  assert.match(out.reason, /duplicate/);
});

test('persistDerivable rejects a postReview entry with no matching finding', () => {
  const findings = [makeFinding('F1')];
  const out = persistDerivable(persistInput({ findings, postReview: [makeFinding('GHOST')] }));
  assert.equal(out.ok, false);
  assert.match(out.reason, /GHOST/);
});

test('persistDerivable rejects a postReview entry that DIFFERS from its findings twin', () => {
  const findings = [makeFinding('F1')];
  const out = persistDerivable(persistInput({
    findings,
    postReview: [{ ...findings[0], severity: 'low' }],
  }));
  assert.equal(out.ok, false);
  assert.match(out.reason, /differs/);
});

test('persistDerivable rejects a checkpoint challenge entry with no matching finding', () => {
  const findings = [makeFinding('F1')];
  const inp = persistInput({ findings });
  inp.checkpoints.phases.challenge.findings = [makeFinding('PHANTOM')];
  const out = persistDerivable(inp);
  assert.equal(out.ok, false);
  assert.match(out.reason, /PHANTOM/);
});

test('persistDerivable rejects a finding that already carries a v2 alias field', () => {
  // toV2Aliased only ADDS an alias when absent, so a pre-existing `line` that
  // disagrees with line_start would be LOST by the checkpoint's alias strip.
  // The projection is not reversible -> fall back rather than derive wrongly.
  const findings = [makeFinding('F1', { line: 999 })];
  const inp = persistInput({ findings });
  const out = persistDerivable(inp);
  assert.equal(out.ok, false);
  assert.match(out.reason, /alias/);
});

// --- persistDerivable: the JS/Python number-spelling precondition (L1-3) ----

test('persistDerivable accepts every number the pipeline actually produces (integers)', () => {
  const findings = [makeFinding('F1', { line_start: 0, line_end: 9007199254740991, confidence: 90 })];
  assert.deepEqual(persistDerivable(persistInput({ findings })), { ok: true });
});

test('persistDerivable refuses numbers Python cannot spell the way JSON.stringify does', () => {
  // 1e-7 -> "1e-7" vs "1e-07"; 0.000001 -> "0.000001" vs "1e-06"; 90.5 -> "90.5" but
  // 90.0 -> "90" vs "90.0"; NaN -> "null" vs "NaN". Rejecting is not a degradation: the
  // run falls back to the legacy by-value writer.
  for (const value of [1e-7, 0.000001, 90.5, NaN, Infinity, -Infinity, 9007199254740992]) {
    const findings = [makeFinding('F1', { confidence: value })];
    const out = persistDerivable(persistInput({ findings }));
    assert.equal(out.ok, false, `confidence ${value} must be refused`);
    assert.match(out.reason, /findings\[0\]\.confidence is not a JS-safe integer/);
  }
});

test('persistDerivable also scans the checkpoint skeleton and the PR identity', () => {
  const inp = persistInput();
  inp.checkpoints.phases.challenge.stats.rate = 0.5;
  assert.match(persistDerivable(inp).reason, /checkpoints\.phases\.challenge\.stats\.rate/);

  const inp2 = persistInput({ prIdentity: { owner: 'o', repo: 'r', pr_number: 7.5, sha_full: 'd' } });
  assert.match(persistDerivable(inp2).reason, /prIdentity\.pr_number/);
});

test('persistDerivable reports the FIRST unsafe field among sibling object keys, not the last', () => {
  // Both `rate` (inserted first) and `count` (inserted second) are unsafe; the scan
  // must report `rate` — the earlier key in insertion order — matching the array
  // branch's own forward-order guarantee.
  const inp = persistInput();
  inp.checkpoints.phases.challenge.stats = { rate: 0.5, count: 90.5 };
  assert.match(persistDerivable(inp).reason, /checkpoints\.phases\.challenge\.stats\.rate/);
});

test('a non-integer number falls back to the legacy by-value writer, naming the reason', async () => {
  const ctx = persistCtx();
  const findings = [makeFinding('F1', { confidence: 0.9 })];
  const out = await writeArtifacts(ctx, persistInput({ findings }));
  assert.equal(out.partial, false, 'the fallback PERSISTS — refusing must not cost the run its artifacts');
  assert.deepEqual(out.artifactPaths, PATHS);
  assert.deepEqual(labels(ctx), ['artifact-writer'], 'no assembler runs on the fallback path');
  assert.ok(out.gaps.some((g) => /JS-safe integer/.test(g)), out.gaps);
});

// --- writeArtifacts: the derived path ---------------------------------------

test('derived path: writes the three primaries, runs the assembler, returns all four paths', async () => {
  const ctx = persistCtx();
  const out = await writeArtifacts(ctx, persistInput());

  assert.equal(out.partial, false);
  assert.deepEqual(out.gaps, []);
  assert.deepEqual(out.artifactPaths, PATHS);
  assert.deepEqual(Object.keys(out.artifactPaths), ['findings', 'report', 'postReview', 'checkpoints']);
  assert.deepEqual(labels(ctx), ['artifact-writer', 'assemble-artifacts']);

  const writer = ctx.calls[0];
  assert.equal(writer.agentType, 'code-gauntlet:artifact-writer');
  const entries = parseWriterPayload(writer.prompt);
  assert.deepEqual(entries.map((e) => e.path), [PATHS.findings, PATHS.report, PLAN_PATH]);
  for (const e of entries) assert.equal(typeof e.text, 'string');
  // The two derived artifacts' CONTENT never crosses the writer prompt — that is the
  // whole point. Each finding's prose appears exactly ONCE (in findings.json); the plan
  // references the other two artifacts by id and path only.
  // (`title` and not `description`: the union schema deliberately duplicates description
  // into `body` INSIDE findings.json, which is a different, required duplication.)
  const title = makeFinding('F1').title;
  const count = (s) => s.split(title).length - 1;
  assert.equal(count(writer.prompt), 1, 'each finding must cross the writer prompt exactly once');
  assert.equal(
    count(writeArtifactsLegacyPrompt(persistInput())), 3,
    'the legacy payload carries each finding three times (findings + postReview + checkpoint)',
  );
});

// The legacy prompt's payload, reconstructed from the exported writerPayload — used
// only to quantify the duplication the derived path removes.
function writeArtifactsLegacyPrompt(inp) {
  return JSON.stringify(writerPayload(inp));
}

test('derived path: the writer prompt carries the primaries VERBATIM', async () => {
  const ctx = persistCtx();
  const inp = persistInput();
  await writeArtifacts(ctx, inp);
  const entries = parseWriterPayload(ctx.calls[0].prompt);
  const { findingsJson, reportMd } = persistPrimaries(inp);
  assert.equal(entries[0].text, findingsJson);
  assert.equal(entries[1].text, reportMd);
  assert.deepEqual(JSON.parse(entries[2].text), persistPlan(inp, PATHS));
});

test('derived path: the executor gets a single pinned python3 command of plain word tokens', async () => {
  const ctx = persistCtx();
  await writeArtifacts(ctx, persistInput());
  const exec = ctx.calls[1];
  assert.equal(exec.agentType, 'code-gauntlet:executor');
  assert.ok(exec.prompt.includes(`python3 ${SCRIPT} --plan ${PLAN_PATH}`), exec.prompt);
  const command = exec.prompt.split('\n').filter((l) => l.startsWith('python3'))[0];
  assert.equal(command.split(' ').length, 4, 'four plain word tokens');
  assert.ok(!/[$`;|&><(){}]/.test(command), `command must be AST-safe: ${command}`);
});

test('derived path: the writer payload is dramatically smaller than the legacy by-value one', async () => {
  const findings = Array.from({ length: 60 }, (_, i) => makeFinding(`F${i}`));
  const inp = persistInput({ findings });
  const ctx = persistCtx();
  await writeArtifacts(ctx, inp);
  const derivedPrompt = ctx.calls[0].prompt;
  const legacyPayload = JSON.stringify(writerPayload(inp));
  assert.ok(
    derivedPrompt.length < legacyPayload.length,
    `derived prompt (${derivedPrompt.length}) must be smaller than the legacy payload (${legacyPayload.length})`,
  );
});

// --- writeArtifacts: the receipt gates --------------------------------------

test('receipt gate: ok:false degrades to partial-artifacts (never a fabricated success)', async () => {
  const ctx = persistCtx({ receiptFrom: (r) => ({ ...r, ok: false, errors: ['duplicate id F1 in source'] }) });
  const out = await writeArtifacts(ctx, persistInput());
  assert.equal(out.partial, true);
  assert.equal(out.artifactPaths.postReview, null, 'nothing derived them, so they are never salvaged');
  assert.equal(out.artifactPaths.checkpoints, null);
  assert.ok(out.gaps.some((g) => /partial-artifacts/.test(g) && /duplicate id F1/.test(g)), out.gaps);
  // The refusal is about DERIVATION, and this receipt still content-proves both primaries
  // against the pipeline's own expectations — so they stay reachable (provenPrimaryPaths).
  assert.equal(out.artifactPaths.findings, PATHS.findings);
  assert.equal(out.artifactPaths.report, PATHS.report);
});

test('receipt gate: a missing WRITTEN path degrades to partial-artifacts', async () => {
  // checkpoint never written
  const out = await writeArtifacts(persistCtx({ receiptFrom: (r) => ({ ...r, written: [r.written[0]] }) }), persistInput());
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => /partial-artifacts/.test(g)), out.gaps);
});

test('receipt gate: a missing VERIFIED path degrades to partial-artifacts', async () => {
  // report never verified
  const out = await writeArtifacts(persistCtx({ receiptFrom: (r) => ({ ...r, verified: [r.verified[0]] }) }), persistInput());
  assert.equal(out.partial, true);
});

test('receipt gate: a null receipt degrades to partial-artifacts', async () => {
  const out = await writeArtifacts(persistCtx({ receipt: null }), persistInput());
  assert.equal(out.partial, true);
});

test('receipt gate: an executor throw degrades to partial-artifacts (non-fatal)', async () => {
  const out = await writeArtifacts(persistCtx({ throwOn: 'assemble-artifacts' }), persistInput());
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => /injected throw/.test(g)), out.gaps);
});

test('writer gate: an echo that misses the plan path degrades to partial-artifacts', async () => {
  const ctx = persistCtx({ writtenEcho: (payload) => payload.slice(0, 2).map((e) => e.path) });
  const out = await writeArtifacts(ctx, persistInput());
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => /no write proof/.test(g)), out.gaps);
  assert.deepEqual(labels(ctx), ['artifact-writer'], 'the assembler must not run without write proof');
});

test('writer gate: an empty {written:[]} echo degrades to partial-artifacts', async () => {
  const out = await writeArtifacts(persistCtx({ writtenEcho: () => [] }), persistInput());
  assert.equal(out.partial, true);
});

test('writer gate: a writer throw degrades to partial-artifacts', async () => {
  const out = await writeArtifacts(persistCtx({ throwOn: 'artifact-writer' }), persistInput());
  assert.equal(out.partial, true);
  assert.equal(out.artifactPaths.checkpoints, null);
});

// --- ONE deterministic retry on a STRUCTURAL assemble failure ---------------
//
// Live smoke evidence (smoke-20260727-205454-f99d948): on discourse-graphite#6 the
// artifact-writer emitted unparseable JSON (it over-escaped \" while transcribing),
// assemble_artifacts.py correctly refused to derive, and the run lost its artifacts
// entirely. A fresh writer dispatch is a fresh sample — the other 2 of 3 runs on the
// same commit produced parseable JSON — so the derived persist is retried exactly once
// before degrading. Never on a TOLERATED content-proof mismatch (that is a success),
// never in a loop, and never by falling back to the legacy by-value writer.

// A persist ctx whose ASSEMBLE receipt differs per attempt. `receiptsFrom` is an array of
// patch functions, one per assemble dispatch, each starting from the honest receipt for
// the plan the writer was handed on THAT attempt.
function retryCtx(receiptsFrom, opts = {}) {
  const calls = [];
  let seenPlan = null;
  let attempt = 0;
  const agent = async (prompt, dispatch = {}) => {
    const label = dispatch.label || '';
    calls.push({ prompt, ...dispatch });
    if (label === 'artifact-writer') {
      const payload = parseWriterPayload(prompt);
      seenPlan = JSON.parse(payload[payload.length - 1].text);
      return { written: payload.map((e) => e.path) };
    }
    if (label === 'assemble-artifacts') {
      if (opts.throwOnAssembleAttempt === attempt) { attempt += 1; throw new Error('injected executor throw'); }
      const patch = receiptsFrom[attempt] || ((r) => r);
      attempt += 1;
      return patch(receiptFor(seenPlan));
    }
    return null;
  };
  return { calls, agent, parallel: async (thunks) => Promise.all(thunks.map((t) => t())) };
}

const refuse = (r) => ({ ...r, ok: false, errors: ['findings.json is not valid JSON (line 99)'] });

test('retry: a structural assemble refusal is retried ONCE and can succeed', async () => {
  const ctx = retryCtx([refuse, (r) => r]);
  const out = await writeArtifacts(ctx, persistInput());

  assert.equal(out.partial, false, `gaps: ${out.gaps}`);
  assert.deepEqual(out.artifactPaths, PATHS);
  assert.deepEqual(
    labels(ctx),
    ['artifact-writer', 'assemble-artifacts', 'artifact-writer', 'assemble-artifacts'],
    'the retry re-dispatches the WRITER (a fresh sample), then the assembler',
  );
  // Both attempts disclosed: a run that only succeeded on the second try must say so.
  assert.ok(
    out.gaps.some((g) => /retr/i.test(g) && /not valid JSON/.test(g)),
    `expected a retry-disclosure gap naming the first failure, got: ${out.gaps}`,
  );
});

test('retry: a second structural refusal degrades, naming BOTH attempts', async () => {
  const ctx = retryCtx([refuse, (r) => ({ ...r, ok: false, errors: ['duplicate id F1 in source'] })]);
  const out = await writeArtifacts(ctx, persistInput());

  assert.equal(out.partial, true);
  assert.deepEqual(
    out.artifactPaths,
    { findings: PATHS.findings, report: PATHS.report, postReview: null, checkpoints: null },
    'the second receipt content-proved both primaries; only the DERIVED documents are lost',
  );
  assert.equal(labels(ctx).filter((l) => l === 'artifact-writer').length, 2, 'exactly one retry, never a loop');
  assert.equal(labels(ctx).filter((l) => l === 'assemble-artifacts').length, 2);
  const gap = out.gaps.find((g) => /partial-artifacts/.test(g));
  assert.ok(gap, out.gaps);
  assert.ok(/duplicate id F1/.test(gap), `names the second failure: ${gap}`);
  assert.ok(/retr/i.test(gap) && /not valid JSON/.test(gap), `names the retry + the first failure: ${gap}`);
});

test('retry: NEVER falls back to the legacy by-value writer', async () => {
  // Deliberate: the legacy path carries no content proof, so falling back to it would
  // convert a VISIBLE failure into a silent one — the smoke run proved the by-value
  // writer diverges from its payload on every run.
  const ctx = retryCtx([refuse, refuse]);
  await writeArtifacts(ctx, persistInput());
  for (const c of ctx.calls.filter((x) => x.label === 'artifact-writer')) {
    assert.ok(Array.isArray(parseWriterPayload(c.prompt)), 'every writer dispatch is the derived (entries) payload');
  }
});

test('retry: a TOLERATED primary content-proof mismatch is NOT retried', async () => {
  // A tolerated mismatch is a successful persist with a disclosed divergence, not a
  // failure — the artifacts are on disk and self-consistent with them.
  const diverged = (r) => ({
    ...r,
    verified: r.verified.map((e) => (e.path === PATHS.findings
      ? { ...e, chars: e.chars + 16, checksum: 'fnv1a32:0xdeadbeef', content_proof: 'mismatch' }
      : e)),
  });
  const ctx = retryCtx([diverged, diverged]);
  const out = await writeArtifacts(ctx, persistInput());

  assert.equal(out.partial, false);
  assert.deepEqual(labels(ctx), ['artifact-writer', 'assemble-artifacts'], 'no retry on a tolerated mismatch');
  assert.ok(out.gaps.some((g) => /artifact-content-proof/.test(g)), out.gaps);
  assert.ok(!out.gaps.some((g) => /retr/i.test(g)), out.gaps);
});

test('retry: a derived-document mismatch (structural) IS retried', async () => {
  const forge = (r) => ({ ...r, written: r.written.map((e, i) => (i === 0 ? { ...e, checksum: 'fnv1a32:0x00000000' } : e)) });
  const ctx = retryCtx([forge, (r) => r]);
  const out = await writeArtifacts(ctx, persistInput());
  assert.equal(out.partial, false, `gaps: ${out.gaps}`);
  assert.equal(labels(ctx).filter((l) => l === 'artifact-writer').length, 2);
});

test('retry: the retry itself never throws out of writeArtifacts', async () => {
  const ctx = retryCtx([refuse], { throwOnAssembleAttempt: 1 });
  const out = await writeArtifacts(ctx, persistInput());
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => /partial-artifacts/.test(g)), out.gaps);
});

// --- ANY throw inside writeArtifacts is non-fatal (issue #38 F1-persist-4) ---

test('a throw in the PRE-DISPATCH plan computation degrades to partial-artifacts, not an exception', async () => {
  // The derived path computes the plan and the primaries BEFORE writeArtifactsDerived's try,
  // so a throw there escaped the stage entirely and hit runWith's top-level catch — ending
  // the run. That contradicts both this function's own contract comment and SKILL.md's
  // "writer failure is NON-FATAL". A BigInt in the checkpoint makes persistPlan's deepClone
  // (a JSON round trip) throw while persistDerivable still passes, so the throw lands exactly
  // in the unguarded window.
  const inp = persistInput();
  inp.checkpoints.counts = { challenge: 2n };
  const ctx = persistCtx();
  const out = await writeArtifacts(ctx, inp);

  assert.equal(out.partial, true);
  assert.deepEqual(out.artifactPaths, { findings: null, report: null, postReview: null, checkpoints: null });
  assert.ok(out.gaps.some((g) => /partial-artifacts/.test(g)), out.gaps);
  assert.deepEqual(labels(ctx), [], 'the throw happened before any dispatch');
});

test('a throw in the id-integrity guard is isolated too (nothing pre-dispatch may escape)', async () => {
  // persistDerivable runs before either persistence path and outside every inner try.
  const inp = persistInput();
  Object.defineProperty(inp.checkpoints, 'boom', {
    enumerable: true,
    get() { throw new Error('kaboom'); },
  });
  const out = await writeArtifacts(persistCtx(), inp);
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => /kaboom/.test(g) && /partial-artifacts/.test(g)), out.gaps);
});

test('the partial-artifacts return shape is identical whichever pre-dispatch step threw', async () => {
  // writeArtifacts' public contract is the same four null paths + a partial-artifacts gap on
  // EVERY failure, so a caller (runWith) needs no new branch for this class.
  const inp = persistInput();
  inp.checkpoints.counts = { challenge: 2n };
  const thrown = await writeArtifacts(persistCtx(), inp);
  const dispatched = await writeArtifacts(persistCtx({ throwOn: 'artifact-writer' }), persistInput());
  assert.deepEqual(Object.keys(thrown).sort(), Object.keys(dispatched).sort());
  assert.deepEqual(thrown.artifactPaths, dispatched.artifactPaths);
  assert.equal(thrown.partial, dispatched.partial);
});

// --- The receipt may not grade itself (issue #38 L1-2) ----------------------

test('receipt gate: a receipt echoing a FOREIGN plan checksum degrades to partial-artifacts', async () => {
  const out = await writeArtifacts(persistCtx({ receiptFrom: (r) => ({ ...r, planChecksum: 'fnv1a32:0xfeedface' }) }), persistInput());
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => /did not run this persist plan/.test(g)), out.gaps);
});

test('receipt gate: a receipt with NO plan checksum degrades to partial-artifacts', async () => {
  const out = await writeArtifacts(persistCtx({ receiptFrom: ({ planChecksum, ...r }) => r }), persistInput());
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => /plan checksum none/.test(g)), out.gaps);
});

test('receipt gate: a wholly self-consistent FABRICATED receipt is rejected', async () => {
  // The pre-L1-2 hole: every expected_* value came from the receipt, so a receipt that
  // agreed with itself ("expected 10 chars, got 10 chars, match") was believed even
  // though 10 is not what the pipeline handed the writer. Grade against the plan.
  const forged = (r) => ({
    ...r,
    verified: r.verified.map((e) => ({
      ...e, chars: 10, expected_chars: 10,
      checksum: 'fnv1a32:0x00000001', expected_checksum: 'fnv1a32:0x00000001',
      content_proof: 'match',
    })),
  });
  const out = await writeArtifacts(persistCtx({ receiptFrom: forged }), persistInput());
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => /foreign expectation/.test(g)), out.gaps);
});

test('receipt gate: an INCOHERENT content_proof degrades to partial-artifacts', async () => {
  // content_proof is derived by the script as chars===expected && checksum===expected.
  // A "match" whose own numbers disagree is a broken relay, not a proof.
  const out = await writeArtifacts(
    persistCtx({ receiptFrom: (r) => ({ ...r, verified: [{ ...r.verified[0], chars: r.verified[0].chars + 1 }, r.verified[1]] }) }),
    persistInput(),
  );
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => /incoherent/.test(g)), out.gaps);
});

// --- CONTENT-PROOF MISMATCH: loud gap, NOT a degradation --------------------

// What the script reports when the bytes on disk differ from the payload: the EXPECTED
// side still echoes the plan (that is what it compared against), only the actual
// chars/checksum move.
const asMismatch = (e) => ({
  ...e,
  chars: e.expected_chars + 1,
  checksum: 'fnv1a32:0x0000dead',
  content_proof: 'mismatch',
});

test('content-proof mismatch: findings still delivered, artifacts still persisted, gap is loud', async () => {
  const inp = persistInput();
  const expected = persistPlan(inp, PATHS).expect[0];
  const out = await writeArtifacts(
    persistCtx({ receiptFrom: (r) => ({ ...r, verified: [asMismatch(r.verified[0]), r.verified[1]] }) }),
    inp,
  );

  assert.equal(out.partial, false, 'a mismatch must NOT lose the run its artifacts');
  assert.deepEqual(out.artifactPaths, PATHS);
  assert.equal(out.gaps.length, 1);
  assert.equal(
    out.gaps[0],
    `artifact-content-proof: ${PATHS.findings} bytes on disk differ from the payload handed to the writer (expected ${expected.chars} chars/checksum ${expected.checksum}, got ${expected.chars + 1}/fnv1a32:0x0000dead)`,
  );
});

test('content-proof mismatch: one gap per mismatching entry', async () => {
  const out = await writeArtifacts(persistCtx({ receiptFrom: (r) => ({ ...r, verified: r.verified.map(asMismatch) }) }), persistInput());
  assert.equal(out.partial, false);
  assert.equal(out.gaps.length, 2);
  assert.ok(out.gaps.every((g) => g.startsWith('artifact-content-proof: ')), out.gaps);
});

// --- The id-integrity fallback ----------------------------------------------

test('id fallback: a duplicate id falls back to the legacy full by-value writer prompt', async () => {
  const ctx = persistCtx();
  const out = await writeArtifacts(ctx, persistInput({ findings: [makeFinding('F1'), makeFinding('F1')] }));

  assert.equal(out.partial, false, 'the fallback PERSISTS — it must not degrade the run');
  assert.deepEqual(out.artifactPaths, PATHS);
  assert.deepEqual(labels(ctx), ['artifact-writer'], 'no assembler runs on the fallback path');
  const payload = parseWriterPayload(ctx.calls[0].prompt);
  assert.ok(!Array.isArray(payload), 'the fallback uses the legacy object payload');
  assert.ok(Array.isArray(payload.findings) && payload.postReview && 'checkpoints' in payload);
  assert.ok(out.gaps.some((g) => /duplicate/.test(g)), out.gaps);
});

test('id fallback: a missing id also falls back, and names the reason in a gap', async () => {
  const findings = [makeFinding('F1'), makeFinding('F2')];
  delete findings[1].id;
  const ctx = persistCtx();
  const out = await writeArtifacts(ctx, persistInput({ findings }));
  assert.equal(out.partial, false);
  assert.deepEqual(labels(ctx), ['artifact-writer']);
  assert.ok(out.gaps.some((g) => /derived persistence/.test(g)), out.gaps);
});

test('id fallback still enforces the four-path write proof', async () => {
  const ctx = persistCtx({ writtenEcho: () => [] }); // irrelevant: legacy echoes artifactPaths
  const bad = { calls: [], agent: async (p, d) => { ctx.calls.push({ prompt: p, ...d }); return { artifactPaths: {} }; }, parallel: ctx.parallel };
  const out = await writeArtifacts(bad, persistInput({ findings: [makeFinding('F1'), makeFinding('F1')] }));
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => /all four planned artifact paths/.test(g)), out.gaps);
});

// --- No persist waist: the legacy path is untouched -------------------------

test('no persist waist: writeArtifacts takes the legacy by-value path with NO gap', async () => {
  const ctx = persistCtx();
  const inp = persistInput();
  delete inp.persist;
  const out = await writeArtifacts(ctx, inp);

  assert.equal(out.partial, false);
  assert.deepEqual(out.gaps, [], 'a clean, documented degradation for older callers — no gap');
  assert.deepEqual(out.artifactPaths, PATHS);
  assert.deepEqual(labels(ctx), ['artifact-writer']);
  const payload = parseWriterPayload(ctx.calls[0].prompt);
  assert.deepEqual(payload, writerPayload(inp));
});

test('no persist waist: an empty persist object is treated as absent', async () => {
  const ctx = persistCtx();
  const out = await writeArtifacts(ctx, persistInput({ persist: {} }));
  assert.equal(out.partial, false);
  assert.deepEqual(out.gaps, []);
  assert.deepEqual(labels(ctx), ['artifact-writer']);
});

// --- The args waist (D3.4) --------------------------------------------------

test('args waist: persist is OPTIONAL — absence validates', () => {
  assert.equal(validateArgs(validArgs()).ok, true);
});

test('args waist: a well-formed persist object validates', () => {
  const check = validateArgs(validArgs({ persist: { assembleScriptPath: SCRIPT } }));
  assert.equal(check.ok, true, check.errors.join('; '));
});

test('args waist: a malformed persist fails loud BEFORE anything is dispatched', () => {
  for (const bad of [null, 'scripts/assemble_artifacts.py', [], 3]) {
    const check = validateArgs(validArgs({ persist: bad }));
    assert.equal(check.ok, false, `persist: ${JSON.stringify(bad)} must be rejected`);
    assert.ok(check.errors.some((e) => /persist must be an object/.test(e)), check.errors);
  }
});

test('args waist: a non-string / empty assembleScriptPath is rejected', () => {
  for (const bad of ['', 42, {}, null]) {
    const check = validateArgs(validArgs({ persist: { assembleScriptPath: bad } }));
    assert.equal(check.ok, false, `assembleScriptPath: ${JSON.stringify(bad)} must be rejected`);
    assert.ok(check.errors.some((e) => /persist.assembleScriptPath/.test(e)), check.errors);
  }
});

// --- runWith wiring ---------------------------------------------------------

test('runWith threads args.persist into writeArtifacts (assembler runs, four paths returned)', async () => {
  const args = validArgs({ persist: { assembleScriptPath: SCRIPT } });
  const ctx = makeCtx(args);
  // Teach the shared mock the two new dispatch shapes without changing its defaults.
  const inner = ctx.agent;
  let seenPlan = null;
  ctx.agent = async (prompt, dispatch = {}) => {
    const label = dispatch.label || '';
    if (label === 'artifact-writer') {
      const payload = parseWriterPayload(prompt);
      if (Array.isArray(payload)) {
        ctx.calls.push({ prompt, ...dispatch });
        seenPlan = JSON.parse(payload[payload.length - 1].text);
        return { written: payload.map((e) => e.path) };
      }
    }
    if (label === 'assemble-artifacts') {
      ctx.calls.push({ prompt, ...dispatch });
      return receiptFor(seenPlan);
    }
    return inner(prompt, dispatch);
  };
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.equal(out.phaseReached, 'report');
  assert.deepEqual(out.artifactPaths, PATHS);
  assert.ok(ctx.calls.some((c) => c.label === 'assemble-artifacts'), 'the assembler ran');
  assert.ok(!out.gaps.some((g) => /partial-artifacts/.test(g)), out.gaps);
});

test('sandbox parity: the derived persist path runs with node-only globals removed', async () => {
  // structuredClone/Buffer/TextEncoder/... exist under node:test but NOT in the workflow
  // runtime sandbox; a reference throws on the first live dispatch. The checksum in
  // particular must never reach for TextEncoder.
  const absent = ['structuredClone', 'Buffer', 'TextEncoder', 'TextDecoder', 'setTimeout', 'queueMicrotask'];
  const saved = {};
  for (const name of absent) { saved[name] = globalThis[name]; delete globalThis[name]; }
  try {
    const out = await writeArtifacts(persistCtx(), persistInput());
    assert.equal(out.partial, false, `gaps: ${out.gaps}`);
    assert.deepEqual(out.artifactPaths, PATHS);
  } finally {
    for (const name of absent) globalThis[name] = saved[name];
  }
});

test('runWith without args.persist never dispatches the assembler', async () => {
  const args = validArgs();
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.ok(!ctx.calls.some((c) => c.label === 'assemble-artifacts'));
});

// --- escape-run hardening: the 2026-07-30 lost-artifacts run ------------------
//
// The shape that broke run wf_adc1a803-912: a challenger's prose carried ONE literal
// backslash ahead of a quote. findings.json escapes it to `\\\"`, and the PAYLOAD_JSON
// wire line escapes THAT again to a run of seven — which the artifact-writer collapsed at
// all 18 sites in the document, yielding `\\"` = escaped-backslash + an unterminated
// string. assemble_artifacts.py refused, the retry (a fresh sample) reproduced the
// corruption byte-for-byte, and the run lost every artifact.
//
// These go RED without hardenEscapeRuns: verified by mutation (make it the identity
// function and the run-length assertions below fail, not just the unit test).

// One literal backslash before each quote — exactly the recorded prose.
const BACKSLASH_PROSE = 'gradeInputProof produces \\"the executor\'s receipt carried no input_checksum\\" here';

function longestBackslashRun(text) {
  let longest = 0;
  for (const run of text.match(/\\+/g) || []) longest = Math.max(longest, run.length);
  return longest;
}

test('hardenEscapeRuns respells every escaped backslash and leaves no run of two', () => {
  const json = JSON.stringify({ d: BACKSLASH_PROSE }, null, 2);
  assert.equal(longestBackslashRun(json), 3, 'precondition: raw JSON.stringify emits a run per literal backslash');

  const hardened = hardenEscapeRuns(json);
  assert.equal(longestBackslashRun(hardened), 1, 'no two backslashes are ever adjacent afterwards');
  assert.deepEqual(JSON.parse(hardened), { d: BACKSLASH_PROSE }, 'same value — this is a respelling, not an edit');
  assert.equal(hardenEscapeRuns(hardened), hardened, 'idempotent: nothing left to harden');
  assert.equal(hardenEscapeRuns(json).includes('\\u005c'), true);
});

test('hardenEscapeRuns never splits an escape: \\\\\\" becomes \\u005c\\" and re-parses', () => {
  // The dangerous adjacency. A naive per-backslash replace would emit `\` for the
  // THIRD backslash too, orphaning the quote and breaking the document.
  const json = JSON.stringify({ d: '\\"' });
  assert.equal(json, '{"d":"\\\\\\""}');
  assert.equal(hardenEscapeRuns(json), '{"d":"\\u005c\\""}');
  assert.equal(JSON.parse(hardenEscapeRuns(json)).d, '\\"');
});

test('hardenEscapeRuns bounds run length INDEPENDENTLY of the data', () => {
  // Not "7 became 3" — any number of literal backslashes becomes that many separate
  // \ escapes instead of one run of 2k. This is the durable property.
  for (const k of [1, 2, 3, 8, 40]) {
    const value = '\\'.repeat(k);
    const hardened = hardenEscapeRuns(JSON.stringify({ d: value }, null, 2));
    assert.equal(longestBackslashRun(hardened), 1, `k=${k}`);
    assert.equal(JSON.parse(hardened).d, value, `k=${k} round-trips`);
  }
});

test('persistPrimaries hands the writer a findings.json with no backslash run', () => {
  const finding = { ...makeFinding('F1'), description: BACKSLASH_PROSE };
  const { findingsJson } = persistPrimaries(persistInput({ findings: [finding], postReview: [finding] }));

  assert.equal(longestBackslashRun(findingsJson), 1);
  const parsed = JSON.parse(findingsJson);
  assert.equal(parsed[0].description, BACKSLASH_PROSE, 'the finding text is unchanged');
  assert.equal(parsed[0].body, BACKSLASH_PROSE, 'the v2 alias too');
});

test('persistPlan checksums the HARDENED findings string — one text, both runtimes', () => {
  const finding = { ...makeFinding('F1'), description: BACKSLASH_PROSE };
  const inp = persistInput({ findings: [finding], postReview: [finding] });
  const { findingsJson } = persistPrimaries(inp);
  const plan = persistPlan(inp, PATHS);
  // If the plan checksummed the UNHARDENED string, every honest writer would now fail
  // the content proof — the fix would trade a corrupt document for a permanent mismatch.
  assert.equal(plan.expect[0].chars, findingsJson.length);
  assert.equal(plan.expect[0].checksum, fnv1a32(findingsJson));
});

test('the dispatched writer prompt carries no run longer than an ordinary escaped quote', async () => {
  const finding = { ...makeFinding('F1'), description: BACKSLASH_PROSE };
  const ctx = persistCtx();
  const out = await writeArtifacts(ctx, persistInput({ findings: [finding], postReview: [finding] }));
  assert.equal(out.partial, false, `gaps: ${out.gaps}`);

  const writer = ctx.calls.find((c) => c.label === 'artifact-writer');
  // 3 is the everyday spelling of an escaped quote inside the wire line — measured
  // transcribed correctly 45/45 on the same run that lost 18/18 of the 7-runs.
  assert.ok(longestBackslashRun(writer.prompt) <= 3, `writer prompt run length: ${longestBackslashRun(writer.prompt)}`);

  const entries = parseWriterPayload(writer.prompt);
  const findingsEntry = entries.find((e) => e.path === PATHS.findings);
  assert.equal(JSON.parse(findingsEntry.text)[0].description, BACKSLASH_PROSE);
});

test('the persist PLAN is hardened on the wire without moving its own checksum', async () => {
  // The plan's proof is VALUE-level: assemble_artifacts.py parses, drops planChecksum and
  // re-serializes. So hardening its bytes must be invisible to it — if this ever stops
  // holding, every derived persist hard-fails on a plan-checksum mismatch.
  // The plan's `skeleton` EMPTIES challenge.findings, so the backslash has to ride in on a
  // phase the skeleton actually keeps — otherwise this test proves nothing about the plan.
  const inp = persistInput();
  inp.checkpoints = { ...inp.checkpoints, phases: { ...inp.checkpoints.phases, verify: { gaps: [BACKSLASH_PROSE] } } };
  const ctx = persistCtx();
  await writeArtifacts(ctx, inp);

  const entries = parseWriterPayload(ctx.calls.find((c) => c.label === 'artifact-writer').prompt);
  const planText = entries[entries.length - 1].text;
  assert.ok(planText.includes('receipt carried no input_checksum'), 'precondition: the prose reached the plan');
  assert.equal(longestBackslashRun(planText), 1);

  const parsed = JSON.parse(planText);
  const declared = parsed.planChecksum;
  delete parsed.planChecksum;
  assert.equal(fnv1a32(JSON.stringify(parsed, null, 2)), declared, 'recomputed from the PARSED plan, as Python does');
});

// --- provenPrimaryPaths: salvage, graded exactly like the success path --------

const provenPlan = () => persistPlan(persistInput(), PATHS);
const provenReceipt = (plan, over = {}) => ({
  ok: false,
  planChecksum: plan.planChecksum,
  verified: [
    { path: PATHS.findings, chars: plan.expect[0].chars, expected_chars: plan.expect[0].chars, checksum: plan.expect[0].checksum, expected_checksum: plan.expect[0].checksum, content_proof: 'match' },
    { path: PATHS.report, chars: plan.expect[1].chars, expected_chars: plan.expect[1].chars, checksum: plan.expect[1].checksum, expected_checksum: plan.expect[1].checksum, content_proof: 'match' },
  ],
  ...over,
});

test('provenPrimaryPaths returns the primaries a refused receipt still content-proved', () => {
  const plan = provenPlan();
  assert.deepEqual(provenPrimaryPaths(provenReceipt(plan), PATHS, plan), [PATHS.findings, PATHS.report]);
});

test('provenPrimaryPaths salvages NOTHING from a receipt that cannot be trusted', () => {
  const plan = provenPlan();
  const only = (over) => provenPrimaryPaths(provenReceipt(plan, over), PATHS, plan);

  assert.deepEqual(only({ planChecksum: 'fnv1a32:0xdeadbeef' }), [], 'a receipt from another plan proves nothing');
  assert.deepEqual(provenPrimaryPaths(null, PATHS, plan), [], 'no receipt at all');
  assert.deepEqual(
    only({ verified: [{ path: PATHS.report, chars: 9, expected_chars: 9, checksum: 'fnv1a32:0x00000009', expected_checksum: 'fnv1a32:0x00000009', content_proof: 'match' }] }),
    [],
    'a FOREIGN expectation: self-consistent, but not what the pipeline handed the writer',
  );
  const mismatched = provenReceipt(plan);
  mismatched.verified[1] = { ...mismatched.verified[1], chars: 1, checksum: 'fnv1a32:0x00000001', content_proof: 'mismatch' };
  assert.deepEqual(provenPrimaryPaths(mismatched, PATHS, plan), [PATHS.findings], 'a diverged primary is not salvaged');

  const incoherent = provenReceipt(plan);
  incoherent.verified[0] = { ...incoherent.verified[0], chars: 1, checksum: 'fnv1a32:0x00000001' };
  assert.deepEqual(provenPrimaryPaths(incoherent, PATHS, plan), [PATHS.report], 'content_proof:"match" contradicting its own numbers');
});
