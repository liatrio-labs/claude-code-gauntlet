// stages_verify_input_proof.test.js — the slice-input content proof (issue #25 req 4).
//
// The other two verify files own the ECHO direction: stages_verify.test.js the stage's
// orchestration contract, stages_verify_delta.test.js the delta echo's own guards. This
// file owns the INPUT direction — whether the file verify_findings.py read is the file
// the pipeline dispatched — which nothing checked before.
//
// WHY THIS EXISTS, in measurements rather than argument. Across every verify slice-input
// file this repo has retained: 4 of 31 (12.9%) are unparseable, and every one of them is
// a complete document followed by bytes the artifact-writer appended after its final byte
// (`}` x3, `</content></invoke>` x1). Each cost a whole slice its classification;
// bench/MEASUREMENT.md records "2 of 3 PRs — 23 findings lost". The pre-PR3 retry could
// not recover a single one, because it re-dispatches the EXECUTOR and never the WRITER:
// wf_3f640577-31c failed both of its attempts at the IDENTICAL byte offset.
//
// So there are two halves here, and both are load-bearing:
//   DETECTION  — a file whose content differs from what was dispatched is caught, where
//                before it was verified against silently.
//   RECOVERY   — an input-implicated failure re-materializes the file before retrying,
//                and trailing-byte corruption is accepted ONLY once its content proof
//                says the recovered document is the one we sent.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { verifyStage, sliceInputProofFor, parseWriterPayload } from '../src/stages.js';
import { deltaEnvelope } from './helpers/verifyDelta.js';

function makeFindings(n) {
  return Array.from({ length: n }, (_, k) => ({
    id: `F${k}`,
    file: `f${k}.js`,
    line_start: k + 1,
    line_end: k + 1,
    title: `t${k}`,
    description: `d${k}`,
    severity: 'high',
    confidence: 80,
    dimension: 'bug',
    origin: 'new',
    evidence: `e${k}`,
    cross_file_refs: [],
    agent: 'bug-detector',
  }));
}

function baseInput(findings, over = {}) {
  return {
    findings,
    nonce: 'n-1',
    headShaShort: 'abc123',
    limits: { verifySliceSize: findings.length },
    policy: {},
    verify: {
      scriptPath: '/plugin/scripts/verify_findings.py',
      inputPathBase: '/out/phase4-input-abc123',
      outputPathBase: '/out/phase4-output-abc123',
      baseBranch: 'main',
    },
    ...over,
  };
}

// A harness that models the WHOLE boundary: the writer puts a document on disk, and the
// executor answers about the document that is actually there. `writerImpl` decides what
// each slice's file ends up containing, so a test can corrupt the FILE rather than the
// receipt — which is the failure this feature exists for.
//
//   writerImpl(sliceIndex, dispatchedContent, attempt) -> the content that lands on disk,
//   or null to model a writer that failed to write anything.
function ctxFor(writerImpl, executorOver = () => ({})) {
  const calls = [];
  const onDisk = new Map();
  const writerAttempts = new Map();

  const agent = async (prompt, opts = {}) => {
    calls.push({ prompt, ...opts });
    const label = opts.label || '';

    if (label.startsWith('verify-input-')) {
      const entries = parseWriterPayload(prompt) || [];
      const written = [];
      for (const e of entries) {
        const i = Number(/\.slice(\d+)\.json$/.exec(e.path)[1]);
        const attempt = (writerAttempts.get(i) || 0) + 1;
        writerAttempts.set(i, attempt);
        const landed = writerImpl(i, e.content, attempt);
        if (landed === null) continue; // wrote nothing: no path to echo
        onDisk.set(i, landed);
        written.push(e.path);
      }
      return { written };
    }

    const m = /^verify-slice-(\d+)(-retry)?$/.exec(label);
    const i = m ? Number(m[1]) : -1;
    const nonce = (prompt.match(/--nonce (\S+)/) || [])[1];
    const doc = onDisk.get(i);
    const over = executorOver(i, m && m[2] ? 2 : 1) || {};

    // No file on disk -> the script cannot read it, and says so with a reason code.
    if (doc === undefined) {
      return { status: 'failed', exitCode: 1, stderr: 'Findings file not found', reason: 'input_unreadable' };
    }
    // The script's own view: it parses what is there and checksums THAT.
    if (doc.corrupt) {
      return { status: 'failed', exitCode: 1, stderr: 'Invalid JSON in findings file', reason: 'input_unparseable' };
    }
    return deltaEnvelope(doc.content.findings, {
      sha: 'abc123',
      nonce,
      n_in: doc.content.findings.length,
      inputChecksum: sliceInputProofFor(doc.content.findings, doc.content.base_branch),
      inputTrailingBytes: doc.trailingBytes || 0,
      ...over,
    });
  };

  return {
    calls,
    labels: () => calls.map((c) => c.label),
    agent,
    parallel: async (thunks) => Promise.all(thunks.map(async (t) => {
      try { return await t(); } catch { return null; }
    })),
  };
}

// The honest writer: whatever it was handed lands on disk, byte for byte.
const faithful = (_i, content) => ({ content });

const unverifiedGap = (gaps) => (gaps || []).find((g) => g.includes('verify: UNVERIFIED'));
const gapMatching = (gaps, re) => (gaps || []).find((g) => re.test(g));

// --- 1. The clean path is unchanged -----------------------------------------

test('a faithfully written slice input is PROVEN, and says nothing about it', async () => {
  const input = baseInput(makeFindings(3));
  const ctx = ctxFor(faithful);
  const out = await verifyStage(ctx, input);

  assert.equal(out.verified, true);
  assert.equal(out.findings.length, 3);
  assert.equal(out.gaps.length, 0, `a clean run stays silent, got: ${out.gaps}`);
  assert.deepEqual(out.inputProof, {
    slices: 1, proven: 1, unproven: 0, recovered: 0, rewritten: 0, degraded: 0,
  });
});

// --- 2. Detection: a file that is not the one we dispatched -------------------

test('a slice input whose CONTENT drifted is caught, and degrades after one re-materialize', async () => {
  const input = baseInput(makeFindings(3));
  // The writer mangles one evidence string on its first attempt — the silent class:
  // valid JSON, right ids, right count, wrong text. Every guard before this one passes it.
  const ctx = ctxFor((i, content, attempt) => {
    if (attempt > 1) return { content }; // ...and gets it right the second time
    const drifted = JSON.parse(JSON.stringify(content));
    drifted.findings[1].evidence = 'e1 but subtly different';
    return { content: drifted };
  });
  const out = await verifyStage(ctx, input);

  // Recovered by the re-materialize: the second write was faithful, so the slice keeps
  // its verification and the run states that it took two dispatches.
  assert.equal(out.verified, true);
  assert.ok(out.findings.every((f) => f.origin !== 'unknown'));
  assert.equal(out.inputProof.rewritten, 1);
  assert.equal(out.inputProof.proven, 1);
  assert.ok(gapMatching(out.gaps, /verify-slice-retry.*re-materialized/), `got: ${out.gaps}`);
  // The re-materialize really did dispatch a fresh writer for exactly that slice.
  assert.ok(ctx.labels().includes('verify-input-rewriter-0'));
});

test('content drift that SURVIVES the re-materialize degrades the slice — it is not delivered with its verdicts intact', async () => {
  const input = baseInput(makeFindings(3));
  // A writer with a persistent habit: both attempts drift.
  const ctx = ctxFor((i, content) => {
    const drifted = JSON.parse(JSON.stringify(content));
    drifted.findings[0].description = 'not what was dispatched';
    return { content: drifted };
  });
  const out = await verifyStage(ctx, input);

  // THE CALL THIS TEST PINS. verify_findings.py computed those verdicts from data known
  // not to be ours, so they are not delivered as if they were trustworthy. Every finding
  // survives — never-drop is untouched — carrying origin='unknown' like every other
  // untrusted slice, which is also the signal bench's G3 already watches. The rejected
  // alternative was keeping the verdicts behind a run-level flag; that would have made a
  // corrupted-input slice indistinguishable at the finding level from a clean one.
  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 3, 'never-drop holds');
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.equal(out.inputProof.degraded, 1);
  assert.match(unverifiedGap(out.gaps), /content proof mismatch/);
});

// --- 3. Recovery: the measured corruption class ------------------------------

test('trailing bytes after a complete document RECOVER, because the proof says the document is ours', async () => {
  const input = baseInput(makeFindings(2));
  // The measured signature: the document is intact, the writer appended one `}` after it.
  // The script's lenient parse takes the leading document and reports the trailing count.
  const ctx = ctxFor((i, content) => ({ content, trailingBytes: 1 }));
  const out = await verifyStage(ctx, input);

  assert.equal(out.verified, true);
  assert.ok(out.findings.every((f) => f.origin !== 'unknown'), 'no classification lost');
  assert.equal(out.inputProof.recovered, 1);
  assert.equal(out.inputProof.proven, 1);
  // Recovery is never silent, and the gap says WHY it was safe to accept.
  const gap = gapMatching(out.gaps, /verify-input-recovered/);
  assert.ok(gap, `expected a recovery disclosure, got: ${out.gaps}`);
  assert.match(gap, /1 byte\(s\) after a complete JSON document/);
  assert.match(gap, /content proof MATCHES/);
  // No rewrite was needed: the document was provably right the first time.
  assert.equal(out.inputProof.rewritten, 0);
  assert.ok(!ctx.labels().includes('verify-input-rewriter-0'));
});

test('an unparseable slice input is re-materialized before the retry — the retry alone could never fix it', async () => {
  const input = baseInput(makeFindings(2));
  const ctx = ctxFor((i, content, attempt) => (
    attempt > 1 ? { content } : { content, corrupt: true }
  ));
  const out = await verifyStage(ctx, input);

  assert.equal(out.verified, true);
  assert.ok(out.findings.every((f) => f.origin !== 'unknown'));
  assert.equal(out.inputProof.rewritten, 1);
  assert.ok(ctx.labels().includes('verify-input-rewriter-0'));
  assert.match(gapMatching(out.gaps, /verify-slice-retry/), /input_unparseable/);
});

test('when the re-materialize itself fails, the retry is SKIPPED — a second dispatch would read the same bytes', async () => {
  const input = baseInput(makeFindings(2));
  // The file is corrupt and the re-write cannot land. Re-dispatching the executor over
  // the unchanged file is exactly the wasted attempt wf_3f640577-31c measured.
  const ctx = ctxFor((i, content, attempt) => (attempt > 1 ? null : { content, corrupt: true }));
  const out = await verifyStage(ctx, input);

  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 2, 'never-drop holds');
  assert.ok(out.findings.every((f) => f.origin === 'unknown'));
  assert.equal(out.inputProof.degraded, 1);
  assert.match(unverifiedGap(out.gaps), /re-materializing this slice's input failed/);
  // The retry was not spent.
  assert.ok(!ctx.labels().includes('verify-slice-0-retry'), `retry must be skipped, got ${ctx.labels()}`);
});

// --- 4. The trigger stays narrow ---------------------------------------------

test('a NON-input failure takes the plain retry and never re-writes a file that was fine', async () => {
  const input = baseInput(makeFindings(2));
  // A dropped nonce echo: nothing to do with the file. Re-writing here is not free — the
  // writer's own transcription drifted on 3 of 10 measured runs — so a good file must be
  // left alone.
  const ctx = ctxFor(faithful, (i, attempt) => (attempt === 1 ? { nonce: 'WRONG' } : {}));
  const out = await verifyStage(ctx, input);

  assert.equal(out.verified, true);
  assert.equal(out.inputProof.rewritten, 0);
  assert.ok(!ctx.labels().includes('verify-input-rewriter-0'), 'a fine file must not be rewritten');
  assert.ok(ctx.labels().includes('verify-slice-0-retry'), 'the ordinary retry still runs');
});

// --- 5. Unproven is disclosed, never fatal -----------------------------------

test('a receipt with no input_checksum leaves the slice UNPROVEN: verified, disclosed, not degraded', async () => {
  const input = baseInput(makeFindings(2));
  const ctx = ctxFor(faithful, () => ({ inputChecksum: null }));
  const out = await verifyStage(ctx, input);

  // Unproven is the state EVERY run was in before this landed, so it cannot be a
  // degradation: hard-failing here would turn a stale plugin's older verify_findings.py
  // into a total loss of classification for the whole run.
  assert.equal(out.verified, true);
  assert.ok(out.findings.every((f) => f.origin !== 'unknown'));
  assert.equal(out.inputProof.unproven, 1);
  assert.equal(out.inputProof.proven, 0);
  const gap = gapMatching(out.gaps, /verify-input-unproven/);
  assert.ok(gap, `unproven must be disclosed, got: ${out.gaps}`);
  assert.ok(!/UNVERIFIED/.test(gap), 'unproven is not a degradation and must not claim to be');
  assert.match(gap, /older copy of that script/);
});

test('unproven slices are disclosed in ONE aggregated gap, not one per slice', async () => {
  const findings = makeFindings(6);
  const input = baseInput(findings, { limits: { verifySliceSize: 2 } });
  const ctx = ctxFor(faithful, () => ({ inputChecksum: null }));
  const out = await verifyStage(ctx, input);

  assert.equal(out.inputProof.slices, 3);
  assert.equal(out.inputProof.unproven, 3);
  const unproven = (out.gaps || []).filter((g) => g.includes('verify-input-unproven'));
  assert.equal(unproven.length, 1, 'three unproven slices, one message');
  assert.match(unproven[0], /3 of 3 verified slice\(s\) \(0, 1, 2\)/);
});

test('the unproven denominator counts GRADED slices, not slices that degraded', async () => {
  const findings = makeFindings(4);
  const input = baseInput(findings, { limits: { verifySliceSize: 2 } });
  // Slice 0 degrades outright (corrupt on both attempts, rewrite also fails); slice 1 is
  // written faithfully but its receipt carries no proof. Saying "1 of 2" would describe a
  // population that includes a slice which was never verified against anything at all.
  const ctx = ctxFor(
    (i, content, attempt) => (i === 0 ? (attempt > 1 ? null : { content, corrupt: true }) : { content }),
    (i) => (i === 1 ? { inputChecksum: null } : {}),
  );
  const out = await verifyStage(ctx, input);

  assert.equal(out.inputProof.slices, 2);
  assert.equal(out.inputProof.degraded, 1);
  assert.equal(out.inputProof.unproven, 1);
  const unproven = (out.gaps || []).find((g) => g.includes('verify-input-unproven'));
  assert.match(unproven, /1 of 1 verified slice\(s\) \(1\)/);
});

// --- 6. Blast radius stays per slice -----------------------------------------

test('one corrupted slice input does not cost the other slices their verification', async () => {
  const findings = makeFindings(6);
  const input = baseInput(findings, { limits: { verifySliceSize: 2 } });
  // Slice 1 is corrupt on both attempts; slices 0 and 2 are written faithfully.
  const ctx = ctxFor((i, content) => (i === 1 ? { content, corrupt: true } : { content }));
  const out = await verifyStage(ctx, input);

  assert.equal(out.verified, false);
  assert.equal(out.findings.length, 6, 'never-drop holds across slices');
  const unknown = out.findings.filter((f) => f.origin === 'unknown').map((f) => f.id);
  assert.deepEqual(unknown, ['F2', 'F3'], 'only the corrupted slice loses its classification');
  assert.equal(out.inputProof.degraded, 1);
  assert.equal(out.inputProof.proven, 2);
});

// --- 7. The expectation is computed from the dispatched document -------------

test('sliceInputProofFor is the SAME computation the writer payload carries — one document, one proof', async () => {
  const findings = makeFindings(3);
  const input = baseInput(findings);
  const ctx = ctxFor(faithful);
  await verifyStage(ctx, input);

  // Whatever the stage handed the writer must be exactly what the expectation covers.
  // A second construction site for this document is how a content proof turns into a
  // permanent false alarm, so the payload itself is the fixture.
  const writerCall = ctx.calls.find((c) => (c.label || '').startsWith('verify-input-writer'));
  const [entry] = parseWriterPayload(writerCall.prompt);
  assert.equal(
    sliceInputProofFor(entry.content.findings, entry.content.base_branch),
    sliceInputProofFor(findings, 'main'),
  );
});

test('a document carrying a number the two runtimes spell differently is unproven, never fatal', async () => {
  const findings = makeFindings(2);
  findings[0].confidence = 87.5; // pinNumericFields rounds this; force it past that guard
  const input = baseInput(findings);
  // The stage pins confidence to an integer before dispatch, so the proof is computable.
  assert.ok(sliceInputProofFor(findings.map((f) => ({ ...f, confidence: 88 })), 'main'));
  // ...but a value it does NOT pin (a float nested in an extra) makes the expectation
  // uncomputable, and that must cost the proof rather than the slice.
  findings[1].hidden_errors = [{ weight: 0.5 }];
  assert.equal(sliceInputProofFor(findings, 'main'), null);

  const ctx = ctxFor(faithful, () => ({ inputChecksum: null }));
  const out = await verifyStage(ctx, input);
  assert.equal(out.verified, true, 'an uncomputable proof never fails a run');
  assert.equal(out.inputProof.unproven, 1);
});
