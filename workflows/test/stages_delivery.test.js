// stages_delivery.test.js — the deterministic Phase 8 delivery selection
// (selectDelivery) and its wiring into the persisted post-review payload.
//
// Contract under test (owner-authorized delivery-policy redesign):
//  - selectDelivery(survivors, cap) ranks with rankFindings and truncates to `cap`.
//    It NEVER filters by report_tag — a 'suggestion'-tagged survivor is included on the
//    same footing as a 'main'-tagged one (the tag is presentation metadata, not an
//    inclusion filter). This is the fix for the 12->8 post-challenge delivery loss.
//  - runWith builds the post-review payload from EVERY challenge-survivor (both tags),
//    rank-ordered, capped by limits.deliveryCap, and persists it via writerPayload so the
//    live agent consumes it verbatim (never re-filters/re-ranks). Challenge-removed and
//    challenge-skipped findings stay excluded exactly as before.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { selectDelivery, writerPayload, runWith } from '../src/stages.js';
import { makeFinding, validArgs, makeCtx } from './helpers/pipelineMock.js';

function dFinding(id, over = {}) {
  return {
    id, file: `${id}.js`, line_start: 5, line_end: 5, title: `t-${id}`,
    description: `d-${id}`, severity: 'high', confidence: 80, dimension: 'bug',
    report_tag: 'main', report_destination: 'main', ...over,
  };
}

// --- selectDelivery: ranking ------------------------------------------------

test('selectDelivery ranks by severity then confidence (reuses rankFindings)', () => {
  const survivors = [
    dFinding('LOW', { severity: 'low', confidence: 99 }),
    dFinding('CRIT', { severity: 'critical', confidence: 60 }),
    dFinding('HIGH_A', { severity: 'high', confidence: 70 }),
    dFinding('HIGH_B', { severity: 'high', confidence: 90 }),
  ];
  const out = selectDelivery(survivors, undefined);
  // critical first, then the two highs by descending confidence, low last.
  assert.deepEqual(out.map((f) => f.id), ['CRIT', 'HIGH_B', 'HIGH_A', 'LOW']);
});

// --- selectDelivery: cap binding --------------------------------------------

test('selectDelivery caps to the top-cap by rank when cap < count', () => {
  const survivors = [
    dFinding('C', { severity: 'critical', confidence: 90 }),
    dFinding('H', { severity: 'high', confidence: 90 }),
    dFinding('M', { severity: 'medium', confidence: 90 }),
  ];
  const out = selectDelivery(survivors, 2);
  assert.deepEqual(out.map((f) => f.id), ['C', 'H']);
});

test('selectDelivery returns all survivors when cap >= count', () => {
  const survivors = [dFinding('A'), dFinding('B')];
  assert.equal(selectDelivery(survivors, 25).length, 2);
});

test('selectDelivery with cap 0 delivers nothing (Math.max(0,...) floor, no throw)', () => {
  assert.deepEqual(selectDelivery([dFinding('A')], 0), []);
});

test('selectDelivery with an absent cap (null/undefined) delivers every survivor', () => {
  const survivors = [dFinding('A'), dFinding('B'), dFinding('C')];
  assert.equal(selectDelivery(survivors, undefined).length, 3);
  assert.equal(selectDelivery(survivors, null).length, 3);
});

test('selectDelivery tolerates an empty/undefined survivor list', () => {
  assert.deepEqual(selectDelivery([], 5), []);
  assert.deepEqual(selectDelivery(undefined, 5), []);
});

// --- selectDelivery: tag inclusion (the fix) --------------------------------

test('selectDelivery includes suggestion-tagged survivors — tag is never an inclusion filter', () => {
  // A suggestion outranks a lower-severity main finding; the OLD main-only policy would
  // have dropped it. selectDelivery keeps it purely on rank.
  const survivors = [
    dFinding('MAIN_CRIT', { severity: 'critical', report_tag: 'main', report_destination: 'main' }),
    dFinding('SUGG_HIGH', { severity: 'high', report_tag: 'suggestion', report_destination: 'suggestion' }),
    dFinding('MAIN_MED', { severity: 'medium', report_tag: 'main', report_destination: 'main' }),
  ];
  const out = selectDelivery(survivors, 2);
  const ids = out.map((f) => f.id);
  assert.ok(ids.includes('SUGG_HIGH'), 'suggestion-tagged survivor delivered when it outranks a main one');
  assert.ok(!ids.includes('MAIN_MED'), 'the lower-ranked main finding is dropped by the cap, not the suggestion');
  // The delivered suggestion still carries its tag as metadata (presentation, not exclusion).
  assert.equal(out.find((f) => f.id === 'SUGG_HIGH').report_tag, 'suggestion');
});

test('selectDelivery does not mutate its input array or elements', () => {
  const survivors = [dFinding('B', { severity: 'low' }), dFinding('A', { severity: 'critical' })];
  const snapshot = JSON.stringify(survivors);
  selectDelivery(survivors, 1);
  assert.equal(JSON.stringify(survivors), snapshot, 'input untouched (pure)');
});

// --- selectDelivery: delivery tier ------------------------------------------

function mixedSurvivors() {
  return [
    dFinding('MAIN_CRIT', { severity: 'critical', report_tag: 'main', report_destination: 'main' }),
    dFinding('SUGG_HIGH', { severity: 'high', report_tag: 'suggestion', report_destination: 'suggestion' }),
    dFinding('MAIN_MED', { severity: 'medium', report_tag: 'main', report_destination: 'main' }),
    dFinding('SUGG_LOW', { severity: 'low', report_tag: 'suggestion', report_destination: 'suggestion' }),
  ];
}

test("selectDelivery tier 'all' delivers every survivor regardless of tag", () => {
  const out = selectDelivery(mixedSurvivors(), undefined, 'all');
  assert.deepEqual(out.map((f) => f.id).sort(), ['MAIN_CRIT', 'MAIN_MED', 'SUGG_HIGH', 'SUGG_LOW']);
});

test("selectDelivery an unspecified tier (undefined/null) defaults to 'all' — no silent narrowing", () => {
  assert.equal(selectDelivery(mixedSurvivors(), undefined, undefined).length, 4);
  assert.equal(selectDelivery(mixedSurvivors(), undefined, null).length, 4);
});

test("selectDelivery tier 'main_only' keeps main-tagged survivors, drops suggestions", () => {
  const out = selectDelivery(mixedSurvivors(), undefined, 'main_only');
  assert.deepEqual(out.map((f) => f.id), ['MAIN_CRIT', 'MAIN_MED'], 'ranked main only, suggestions withheld');
  assert.ok(!out.some((f) => f.report_tag === 'suggestion'));
});

test("selectDelivery tier 'main_only' still honors the cap and ranking", () => {
  const out = selectDelivery(mixedSurvivors(), 1, 'main_only');
  assert.deepEqual(out.map((f) => f.id), ['MAIN_CRIT']);
});

test("selectDelivery tier 'main_only' falls back to report_destination when report_tag is absent", () => {
  const survivors = [
    dFinding('D_MAIN', { report_tag: undefined, report_destination: 'main' }),
    dFinding('D_SUGG', { report_tag: undefined, report_destination: 'suggestion' }),
  ];
  assert.deepEqual(selectDelivery(survivors, undefined, 'main_only').map((f) => f.id), ['D_MAIN']);
});

// --- writerPayload: carries the post-review set, v2-aliased ------------------

test('writerPayload carries postReview v2-aliased with the tag preserved', () => {
  const pr = [{ id: 'D1', line_start: 7, line_end: 9, description: 'body text', report_tag: 'suggestion' }];
  const out = writerPayload({ findings: [], unverified: [], postReview: pr });
  assert.equal(out.postReview[0].line, 7, 'v2 line alias');
  assert.equal(out.postReview[0].end_line, 9, 'v2 end_line alias');
  assert.equal(out.postReview[0].body, 'body text', 'v2 body alias');
  assert.equal(out.postReview[0].report_tag, 'suggestion', 'tag preserved as metadata');
});

test('writerPayload postReview defaults to an empty array', () => {
  const out = writerPayload({ findings: [], unverified: [] });
  assert.deepEqual(out.postReview, []);
});

// --- runWith: persists the post-review payload from challenge survivors ------

// A challenge checkpoint lets us pin the exact survivor set (both tags, distinct
// severities) that the delivery selection must consume.
function challengeCheckpoint() {
  return {
    findings: [
      makeFinding('M1', { severity: 'critical', confidence: 95, report_tag: 'main', report_destination: 'main' }),
      makeFinding('S1', { severity: 'high', confidence: 90, report_tag: 'suggestion', report_destination: 'suggestion' }),
      makeFinding('M2', { severity: 'medium', confidence: 80, report_tag: 'main', report_destination: 'main' }),
    ],
    unverified: [],
    eliminated: [],
    gaps: [],
    stats: { total_input: 3, dispatched: 3, completed: 3, skipped: 0, final_count: 3 },
    generated_at: '2026-07-18T00:00:00Z',
  };
}

test('runWith persists postReview built from every challenge-survivor, ranked and capped', async () => {
  const args = validArgs({
    checkpoints: { challenge: challengeCheckpoint() },
    limits: { validateBatch: 25, verifySliceSize: 100, challengeCap: 40, summarizeBucketSize: 20, deliveryCap: 2 },
  });
  let persisted = null;
  const ctx = makeCtx(args, { onPersist: (payload) => { persisted = payload; } });
  const out = await runWith(ctx, args);

  assert.equal(out.ok, true);
  assert.ok(persisted, 'writer received the payload');
  // Cap 2 over the ranked [M1(critical), S1(high, suggestion), M2(medium)] keeps the top two.
  assert.deepEqual(persisted.postReview.map((f) => f.id), ['M1', 'S1'],
    'delivery = ranked top-cap of ALL survivors; the suggestion is delivered over the lower main finding');
  // Delivered findings are v2-aliased so post_review.py consumes them unchanged.
  assert.equal(persisted.postReview[0].line, persisted.postReview[0].line_start);
});

test('runWith with no deliveryCap and no tier delivers every challenge-survivor (both tags, default all)', async () => {
  const args = validArgs({
    checkpoints: { challenge: challengeCheckpoint() },
    // no deliveryCap in limits, no delivery.tier -> default 'all'
  });
  let persisted = null;
  const ctx = makeCtx(args, { onPersist: (payload) => { persisted = payload; } });
  await runWith(ctx, args);
  assert.deepEqual(persisted.postReview.map((f) => f.id), ['M1', 'S1', 'M2']);
  assert.ok(persisted.postReview.some((f) => f.report_tag === 'suggestion'), 'suggestions included by default');
});

test("runWith threads args.delivery.tier='main_only' into selectDelivery — suggestions withheld from delivery", async () => {
  const args = validArgs({
    checkpoints: { challenge: challengeCheckpoint() },
    delivery: { tier: 'main_only' },
  });
  let persisted = null;
  const ctx = makeCtx(args, { onPersist: (payload) => { persisted = payload; } });
  await runWith(ctx, args);
  // M1 + M2 are main-tagged; S1 (suggestion) stays in the report but is not in the delivery set.
  assert.deepEqual(persisted.postReview.map((f) => f.id), ['M1', 'M2']);
  assert.ok(!persisted.postReview.some((f) => f.report_tag === 'suggestion'), 'suggestion withheld under main_only');
  // The full findings artifact still carries every survivor (the report renders suggestions).
  assert.equal(persisted.findings.length, 3);
});

test('runWith exposes the persisted post-review artifact path', async () => {
  const args = validArgs();
  const out = await runWith(makeCtx(args), args);
  assert.equal(out.ok, true);
  assert.equal(typeof out.artifactPaths.postReview, 'string', 'post-review artifact path returned');
  assert.match(out.artifactPaths.postReview, /post-review/);
  assert.match(out.artifactPaths.postReview, /abc1234/);
});
