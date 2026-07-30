// stages_health_banner.test.js — the deterministic degradation banner (issue #25 reqs 7-9).
//
// THE DEFECT THIS CLOSES, twice on the record: a run whose every delivered finding carried
// origin=unknown shipped a report saying it had "0 unverified/pipeline-degraded findings".
// The number was not wrong about what it measured — the challenge-skipped bucket really was
// empty — it was a bucket count wearing a whole-pipeline health claim. Two things had to
// change: the bucket had to stop making that claim (req 8), and the claim had to be made
// somewhere that can actually compute it (reqs 7, 9).
//
// The banner is derived in code from the delivered findings themselves, never asked of the
// report-writer, because the writer is not given the verify/discover outcomes at all — any
// health sentence it writes is composed from data that does not contain the answer.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { reviewHealth, healthBanner, applyHealthBanner, runWith } from '../src/stages.js';
import { makeCtx, validArgs } from './helpers/pipelineMock.js';

const f = (id, over = {}) => ({
  id, file: `${id}.js`, line_start: 1, line_end: 1, title: id, description: id,
  severity: 'high', confidence: 90, dimension: 'bug', origin: 'new', evidence: 'e',
  cross_file_refs: [], ...over,
});

// --- 1. What counts as unclassified -----------------------------------------

test('a fully classified review is healthy and renders NO banner', () => {
  const health = reviewHealth({
    delivered: [f('F1'), f('F2', { origin: 'surfaced' })],
    notChallenged: [f('F3')],
  });
  assert.equal(health.unclassified, 0);
  assert.equal(health.degraded, false);
  assert.equal(healthBanner(health), '');
  // A banner that fires on healthy runs is a banner nobody reads.
  assert.equal(applyHealthBanner('# Report\n\nbody', health), '# Report\n\nbody');
});

test("origin='unknown' counts as unclassified", () => {
  const health = reviewHealth({ delivered: [f('F1', { origin: 'unknown' }), f('F2')] });
  assert.equal(health.unclassified, 1);
  assert.equal(health.degraded, true);
});

test('a MISSING origin counts as unclassified too — the case an ===unknown check would miss', () => {
  // Reachable in production: `origin` is not in VERIFY_SCHEMA's delta `required` list, and
  // joinVerifyDeltas only copies a key the echo actually carried, so an executor that omits
  // origin leaves the finding with the one it had before verify — and no discovery agent
  // sets one. applyChallenges already guards for absent origin for the same reason.
  const noOrigin = f('F1');
  delete noOrigin.origin;
  const health = reviewHealth({ delivered: [noOrigin, f('F2')] });
  assert.equal(health.unclassified, 1, 'an absent origin is not a classification');
  assert.equal(health.degraded, true);
});

test('the not-blind-challenged bucket is counted too — it is delivered content', () => {
  const health = reviewHealth({
    delivered: [f('F1')],
    notChallenged: [f('F2', { origin: 'unknown' })],
  });
  assert.equal(health.unclassified, 1);
  assert.equal(health.delivered, 1);
  assert.equal(health.notChallenged, 1);
});

// --- 2. The impossibility property (requirement 7) ---------------------------

test('IMPOSSIBILITY: any state with an unclassified finding renders a banner saying so', () => {
  // Requirement 7 asks that "0 unverified/pipeline-degraded findings" be impossible while
  // findings carry origin=unknown. Asserted as a property over the whole state space that
  // matters rather than one example, so a future branch cannot carve out a silent case.
  for (const originValue of ['unknown', undefined, '', 'UNKNOWN', 'garbage', null]) {
    const finding = f('F1');
    if (originValue === undefined) delete finding.origin; else finding.origin = originValue;
    const health = reviewHealth({ delivered: [finding, f('F2')] });
    assert.equal(health.degraded, true, `origin=${JSON.stringify(originValue)} must degrade`);
    const out = applyHealthBanner('# Report\n\n0 problems here', health);
    assert.match(out, /This review is degraded/, `origin=${JSON.stringify(originValue)}`);
    assert.match(out, /1 of 2 finding\(s\) in this report were never classified/);
  }
});

test('the banner states what was NOT lost, so it cannot read as "findings were dropped"', () => {
  const health = reviewHealth({ delivered: [f('F1', { origin: 'unknown' })] });
  assert.match(healthBanner(health), /No finding was dropped/);
});

// --- 3. Idempotency, and why it is load-bearing ------------------------------

test('applying the banner twice yields exactly one banner', () => {
  const health = reviewHealth({ delivered: [f('F1', { origin: 'unknown' })] });
  const once = applyHealthBanner('# Report', health);
  const twice = applyHealthBanner(once, health);
  assert.equal(twice, once);
  assert.equal(twice.split('This review is degraded').length - 1, 1);
});

test('a STALE banner from a previous run is replaced, not stacked', () => {
  // The resume path replays a previous run's report body verbatim. That body may already
  // carry that run's banner, and a stale banner is worse than none: it would describe a
  // health state this run never measured.
  const old = applyHealthBanner('# Report\n\nbody', reviewHealth({
    delivered: [f('A', { origin: 'unknown' }), f('B', { origin: 'unknown' }), f('C', { origin: 'unknown' })],
  }));
  assert.match(old, /3 of 3 finding\(s\)/);
  const fresh = applyHealthBanner(old, reviewHealth({ delivered: [f('A', { origin: 'unknown' }), f('B')] }));
  assert.match(fresh, /1 of 2 finding\(s\)/);
  assert.ok(!/3 of 3 finding\(s\)/.test(fresh), 'the previous run\'s numbers must not survive');
  assert.equal(fresh.split('This review is degraded').length - 1, 1);
});

test('a run that RECOVERS clears a previous run\'s banner entirely', () => {
  const degraded = applyHealthBanner('# Report\n\nbody', reviewHealth({ delivered: [f('A', { origin: 'unknown' })] }));
  const healthy = applyHealthBanner(degraded, reviewHealth({ delivered: [f('A')] }));
  assert.equal(healthy, '# Report\n\nbody');
});

test('an UNTERMINATED begin sentinel is left alone rather than eating the report', () => {
  // Deleting to end-of-document on a malformed marker would lose the whole review to a
  // stray comment — a far worse failure than a visible artifact. Asserted while a banner
  // is actually being PREPENDED (degraded health), so the strip and the prepend are both
  // live: a healthy fixture would exercise neither.
  const body = `<!-- code-gauntlet:health:begin -->\n# Report\n\nreal content`;
  const out = applyHealthBanner(body, reviewHealth({ delivered: [f('A', { origin: 'unknown' })] }));
  assert.match(out, /real content/, 'the report survived');
  assert.match(out, /This review is degraded/, 'and the fresh banner was still added');
});

test('SELF-REFERENCE: a finding quoting the sentinels cannot splice out the report', () => {
  // This tool reviews its own repository, where the sentinel literals appear in source —
  // so a finding whose evidence quotes them is not hypothetical. An unanchored strip would
  // delete everything between the quoted begin and the next end. The strip is anchored at
  // position 0, which puts report CONTENT structurally out of its reach.
  const body = [
    '# Report',
    '',
    '## F1: banner sentinels are not escaped',
    'evidence: the code emits `<!-- code-gauntlet:health:begin -->` at the top',
    '',
    'IMPORTANT MIDDLE CONTENT THAT MUST SURVIVE',
    '',
    'and closes with `<!-- code-gauntlet:health:end -->` afterwards.',
    '',
    '## F2: a second real finding',
  ].join('\n');
  const out = applyHealthBanner(body, reviewHealth({ delivered: [f('A', { origin: 'unknown' })] }));
  assert.match(out, /IMPORTANT MIDDLE CONTENT THAT MUST SURVIVE/);
  assert.match(out, /F2: a second real finding/);
  assert.match(out, /banner sentinels are not escaped/);
  // ...and it is still banded exactly once, at the front.
  assert.match(out, /^<!-- code-gauntlet:health:begin -->/);
  assert.equal(out.split('This review is degraded').length - 1, 1);
});

// --- 4. Resume: report only what this run can evidence ------------------------

test('on a REPLAYED delivery the origins still count but stage-level detail is withheld', () => {
  // A resume re-runs discover/verify over a freshly rediscovered population it then
  // DISCARDS, delivering the replayed challenge output instead. Quoting that recomputation
  // over these findings would print one population's health above another's results.
  const health = reviewHealth({
    delivered: [f('F1', { origin: 'unknown' })],
    verify: { verified: false, inputProof: { degraded: 7, unproven: 3, recovered: 1 } },
    dimensionsLost: ['security'],
    evidenceIsFresh: false,
  });
  assert.equal(health.degraded, true, 'the findings themselves are still evidence');
  assert.equal(health.unclassified, 1);
  assert.equal(health.verifySlicesDegraded, undefined, 'not measured for THIS delivered set');
  assert.deepEqual(health.dimensionsLost, [], 'a rediscovered population is not evidence here');
  const banner = healthBanner(health);
  assert.match(banner, /replayed a previous run's findings/);
  assert.ok(!/7 finding-slice/.test(banner), 'must not quote the orphaned recomputation');
});

// --- 5. Signal discipline: no crying wolf ------------------------------------

test('UNPROVEN slice inputs alone do NOT raise the banner', () => {
  // PR3 established unproven as the pre-existing baseline, not a degradation. Raising the
  // banner for it would fire on ordinary runs and train readers to ignore it.
  const health = reviewHealth({
    delivered: [f('F1')],
    verify: { verified: true, inputProof: { degraded: 0, unproven: 2, recovered: 0 } },
  });
  assert.equal(health.degraded, false);
  assert.equal(healthBanner(health), '');
});

test('...but unproven inputs ARE named once the banner is firing for another reason', () => {
  const health = reviewHealth({
    delivered: [f('F1', { origin: 'unknown' })],
    verify: { verified: false, inputProof: { degraded: 1, unproven: 2, recovered: 1 } },
  });
  const banner = healthBanner(health);
  assert.match(banner, /2 slice input\(s\) could not be proven/);
  assert.match(banner, /1 corrupted slice input\(s\) were recovered/);
});

test('a lost review DIMENSION degrades the run even when every finding is classified', () => {
  // Zero coverage for a dimension is a health fact no finding can carry: the evidence is
  // the absence of findings, which is exactly what a reader would otherwise read as "clean".
  const health = reviewHealth({ delivered: [f('F1')], dimensionsLost: ['security'] });
  assert.equal(health.degraded, true);
  assert.match(healthBanner(health), /produced no results at all.*security/s);
  assert.match(healthBanner(health), /not evidence of their absence in the code/);
});

// --- 6. End to end, against the shape of the live incident --------------------

test('END TO END: a degraded verify slice puts the banner on the PERSISTED report', async () => {
  const args = validArgs();
  let persisted = null;
  const ctx = makeCtx(args, {
    verifySliceFailIndex: 0,
    onPersist: (payload) => { persisted = payload && payload.report; },
  });
  const out = await runWith(ctx, args);

  assert.equal(out.stats.verified, false);
  assert.equal(out.stats.health.degraded, true);
  assert.equal(out.stats.health.unclassified, out.stats.health.delivered);
  // The banner is on the bytes that reach disk and the reader, not merely in the envelope.
  assert.match(persisted, /^<!-- code-gauntlet:health:begin -->/);
  assert.match(persisted, /This review is degraded/);
  // And the run says so structurally too, for anything reading gaps rather than markdown.
  assert.ok((out.gaps || []).some((g) => g.startsWith('review_degraded:')), `got: ${out.gaps}`);
});

test('a review that found NOTHING but lost a dimension is still banded — the worst false-clean', async () => {
  // The most dangerous shape this banner exists for: zero findings reads as "all clear",
  // and it is indistinguishable from "the agent that would have found them never ran".
  // There is no finding here to carry an origin, so the findings-based signal is silent
  // and the dimension signal is the only thing standing between a reader and a false
  // clean bill of health.
  const health = reviewHealth({ delivered: [], notChallenged: [], dimensionsLost: ['security'] });
  assert.equal(health.degraded, true);
  assert.equal(health.unclassified, 0, 'no finding is unclassified — there are no findings');
  const out = applyHealthBanner('# Code Gauntlet\n\nNo issues found.', health);
  assert.match(out, /This review is degraded/);
  assert.match(out, /produced no results at all/);
});

test('END TO END: the banner also rides the PR review summary, the other delivery surface', async () => {
  // A pr_comments-only delivery never shows report.md to anyone: the reader gets inline
  // comments plus this summary body. A banner that lived only on the report would be
  // unmissable exactly where nobody was looking.
  const args = validArgs({ delivery: { prIdentity: { owner: 'o', repo: 'r', pr_number: 7, sha_full: 'a'.repeat(40) } } });
  let payload = null;
  const ctx = makeCtx(args, { verifySliceFailIndex: 0, onPersist: (p) => { payload = p; } });
  const out = await runWith(ctx, args);

  assert.equal(out.stats.health.degraded, true);
  const wrapper = payload && payload.postReview;
  assert.ok(wrapper && typeof wrapper.review_body === 'string', `expected a post-review wrapper, got ${JSON.stringify(payload && payload.postReview)}`);
  assert.match(wrapper.review_body, /This review is degraded/);
});

test('END TO END: a clean run leaves the PR review summary empty', async () => {
  const args = validArgs({ delivery: { prIdentity: { owner: 'o', repo: 'r', pr_number: 7, sha_full: 'a'.repeat(40) } } });
  let payload = null;
  const ctx = makeCtx(args, { onPersist: (p) => { payload = p; } });
  const out = await runWith(ctx, args);
  assert.equal(out.stats.health.degraded, false);
  assert.equal(payload.postReview.review_body, '', 'a healthy run adds nothing to the summary');
});

test('END TO END: a lost discovery dimension reaches the banner through runWith', async () => {
  // The dimensionsLost wiring (discoverOut.degraded -> reviewHealth) exercised through the
  // real orchestration rather than by handing reviewHealth an array directly.
  const args = validArgs();
  let persisted = null;
  const ctx = makeCtx(args, {
    // A discovery agent that THROWS is a terminal dispatch failure: that dimension
    // produced nothing, and its silence is indistinguishable from "found nothing" —
    // which is exactly the false-clean the dimension signal exists to catch.
    agentThrowLabel: 'code-gauntlet:security-reviewer',
    onPersist: (p) => { persisted = p && p.report; },
  });
  const out = await runWith(ctx, args);

  assert.ok(out.stats.health.dimensionsLost.length > 0, `expected a lost dimension, got ${JSON.stringify(out.stats.health)}`);
  assert.equal(out.stats.health.degraded, true);
  assert.match(persisted, /produced no results at all/);
});

test('END TO END: a RESUME bands from the replayed findings and withholds the orphaned stage detail', async () => {
  // The evidenceIsFresh wiring (checkpoints.challenge -> reviewHealth) through the real
  // orchestration. A resume replays the delivered set and re-runs discover/verify over a
  // population it then discards, so the banner must speak from the replayed findings'
  // own origins and stay silent about the recomputation.
  const first = validArgs();
  let persistedCheckpoints = null;
  await runWith(makeCtx(first, {
    verifySliceFailIndex: 0, // the delivered set is unclassified when it is checkpointed
    onPersist: (p) => { persistedCheckpoints = p && p.checkpoints; },
  }), first);
  assert.ok(persistedCheckpoints, 'the first run persisted a checkpoint to resume from');

  const args = validArgs({ checkpoints: persistedCheckpoints });
  let persisted = null;
  const ctx = makeCtx(args, { onPersist: (p) => { persisted = p && p.report; } });
  const out = await runWith(ctx, args);

  assert.equal(out.stats.health.evidenceIsFresh, false, 'the delivery was replayed');
  assert.equal(out.stats.health.degraded, true, 'the replayed findings are still evidence');
  assert.match(persisted, /This review is degraded/);
  assert.match(persisted, /replayed a previous run's findings/);
  // The freshly re-run verify describes a set this run discards; quoting it here would
  // print one population's health above another's results.
  assert.equal(out.stats.health.verifySlicesDegraded, undefined);
});

test('END TO END: a clean run persists a report with no banner and no degradation gap', async () => {
  const args = validArgs();
  let persisted = null;
  const ctx = makeCtx(args, { onPersist: (payload) => { persisted = payload && payload.report; } });
  const out = await runWith(ctx, args);

  assert.equal(out.stats.health.degraded, false);
  assert.ok(!/code-gauntlet:health/.test(persisted), 'a healthy run stays quiet');
  assert.ok(!(out.gaps || []).some((g) => g.startsWith('review_degraded:')));
});
