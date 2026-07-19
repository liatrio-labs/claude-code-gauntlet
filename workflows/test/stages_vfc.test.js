// stages_vfc.test.js — orchestration-contract tests for stages 5-7
// (Validate, Filter, Challenge). ctx is injected {agent, parallel}; the mock ctx
// is the testability seam (the runtime globals do not exist under node:test).
//
// Degradation contract under test:
//  - validate: a null validator member leaves its batch UNVALIDATED — those findings
//    are kept at face value (conservative) and marked validation='skipped'.
//  - filter: pure + deterministic (same input -> same output, no ctx).
//  - challenge: the blind prompt carries ONLY {title, description, code} (structural
//    guarantee); an unchallenged finding (cap overflow OR a null member) is marked
//    challenge='skipped' and NEVER enters the high-confidence bucket.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  validateStage, filterStage, challengeStage, blindChallengeFields,
} from '../src/stages.js';

// --- Phase 5: Validate ------------------------------------------------------

// parallel() returns one array of validations per batch; a nulled index models a
// failed member (parallel() null-isolation). agent() throws: validate uses parallel().
function validateCtx({ nullBatches = [], byBatch } = {}) {
  const calls = [];
  return {
    calls,
    parallel: async (tasks) => Promise.all(tasks.map(async (t, i) => {
      calls.push(t);
      if (nullBatches.includes(i)) return null;
      return byBatch ? byBatch(t, i) : [];
    })),
    agent: async () => { throw new Error('validateStage must use parallel(), not bare agent()'); },
  };
}

function vFinding(id, over = {}) {
  return { id, file: `${id}.js`, line_start: 1, title: `t-${id}`, description: `d-${id}`, severity: 'high', confidence: 80, dimension: 'bug', ...over };
}

test('validate batches findings at limits.validateBatch (ceil(n/batch) dispatches)', async () => {
  const findings = ['F1', 'F2', 'F3', 'F4', 'F5'].map((id) => vFinding(id));
  const ctx = validateCtx();
  const out = await validateStage(ctx, { findings, limits: { validateBatch: 2 }, policy: {} });
  assert.equal(ctx.calls.length, 3); // ceil(5/2)
  assert.equal(out.stats.batches_dispatched, 3);
  assert.deepEqual(ctx.calls.map((t) => t.label), ['validate-batch-0', 'validate-batch-1', 'validate-batch-2']);
  assert.equal(ctx.calls[0].agentType, 'deep-review:validator');
});

test('a null validator member marks its findings validation=skipped, kept conservatively', async () => {
  const findings = ['F1', 'F2', 'F3', 'F4'].map((id) => vFinding(id));
  // Batches: [F1,F2],[F3,F4]; batch 1 fails.
  const ctx = validateCtx({ nullBatches: [1] });
  const out = await validateStage(ctx, { findings, limits: { validateBatch: 2 }, policy: {} });
  const byId = Object.fromEntries(out.findings.map((f) => [f.id, f]));
  assert.equal(byId.F3.validation, 'skipped');
  assert.equal(byId.F4.validation, 'skipped');
  assert.equal(byId.F1.validation, 'validated');
  assert.equal(byId.F2.validation, 'validated');
  // Conservative: skipped findings are never dropped and keep their confidence.
  assert.equal(out.findings.length, 4);
  assert.equal(byId.F3.confidence, 80);
  assert.equal(out.stats.skipped, 2);
  assert.equal(out.stats.batches_completed, 1);
  assert.ok(out.gaps.some((g) => /validate-batch-1/.test(g)));
});

test('validated confidence adjustments are merged in place (applyValidations)', async () => {
  const findings = [vFinding('F1', { confidence: 90 }), vFinding('F2', { confidence: 80 })];
  const ctx = validateCtx({ byBatch: () => [{ id: 'F1', confidence: 50, justification: 'weak' }] });
  const out = await validateStage(ctx, { findings, limits: { validateBatch: 10 }, policy: {} });
  const f1 = out.findings.find((f) => f.id === 'F1');
  assert.equal(f1.confidence, 50);
  assert.equal(f1.original_confidence, 90);
  assert.equal(f1.validation_justification, 'weak');
  assert.equal(out.stats.adjusted, 1);
  assert.equal(out.stats.validated, 2); // batch succeeded -> both validated
});

test('validate: empty finding set dispatches nothing', async () => {
  const ctx = validateCtx();
  const out = await validateStage(ctx, { findings: [], limits: { validateBatch: 10 }, policy: {} });
  assert.equal(ctx.calls.length, 0);
  assert.deepEqual(out.findings, []);
  assert.equal(out.stats.batches_dispatched, 0);
});

// --- Phase 6: Filter --------------------------------------------------------

function freshFilterInput() {
  return {
    findings: [
      { id: 'F1', file: 'a.js', line_start: 10, title: 'real bug', description: 'a genuine correctness problem in the handler that drops writes', severity: 'high', confidence: 90, dimension: 'bug', agent: 'bug-detector' },
      { id: 'F2', file: 'b.js', line_start: 20, title: 'weak nit', description: 'a low confidence style nit that should be filtered by the threshold', severity: 'low', confidence: 30, dimension: 'convention', agent: 'conventions-and-intent' },
    ],
    reviewConfig: { confidence_threshold: 70, severity_threshold: 'low', ignore: [] },
    exclusionPatterns: [],
    generatedAt: '2026-07-18T00:00:00Z',
  };
}

test('filter is pure + deterministic: same input -> same output, no ctx', () => {
  const a = filterStage(freshFilterInput());
  const b = filterStage(freshFilterInput());
  assert.deepEqual(a, b);
  // generated_at is threaded from the args waist, not a wall clock.
  assert.equal(a.generated_at, '2026-07-18T00:00:00Z');
  // The below-threshold nit is eliminated; the real bug survives.
  assert.ok(a.filtered.some((f) => f.id === 'F1'));
  assert.ok(!a.filtered.some((f) => f.id === 'F2'));
});

// --- Phase 7: Challenge -----------------------------------------------------

function challengeCtx({ nulls = [], byIdx } = {}) {
  const calls = [];
  return {
    calls,
    parallel: async (tasks) => Promise.all(tasks.map(async (t, i) => {
      calls.push(t);
      if (nulls.includes(i)) return null;
      return byIdx ? byIdx(t, i) : { score: 80, justification: 'holds up' };
    })),
    agent: async () => { throw new Error('challengeStage must use parallel(), not bare agent()'); },
  };
}

function cFinding(id, sev, over = {}) {
  return { id, file: `${id}.js`, line_start: 5, title: `t-${id}`, description: `d-${id}`, code: `code-${id}`, severity: sev, confidence: 80, dimension: 'bug', ...over };
}

test('blindChallengeFields exposes EXACTLY {title, description, code} — structural blindness', () => {
  const finding = cFinding('F1', 'high', {
    evidence: 'SENTINEL_EVIDENCE_LEAK', origin: 'surfaced', cross_file_refs: ['SENTINEL_XREF_LEAK.js:9'],
    reasoning: 'SENTINEL_REASONING_LEAK', corroborated_by: ['bug-detector'],
  });
  assert.deepEqual(Object.keys(blindChallengeFields(finding)), ['title', 'description', 'code']);
});

test('challenge prompt carries only title/description/code — no evidence/reasoning content leaks', async () => {
  const finding = cFinding('F1', 'high', {
    title: 'SENTINEL_TITLE', description: 'SENTINEL_DESC', code: 'SENTINEL_CODE',
    evidence: 'SENTINEL_EVIDENCE_LEAK', origin: 'surfaced', cross_file_refs: ['SENTINEL_XREF_LEAK.js:9'],
    reasoning: 'SENTINEL_REASONING_LEAK',
  });
  const ctx = challengeCtx();
  await challengeStage(ctx, { findings: [finding], limits: { challengeCap: 40 }, policy: {} });
  const prompt = ctx.calls[0].prompt;
  assert.match(prompt, /SENTINEL_TITLE/);
  assert.match(prompt, /SENTINEL_DESC/);
  assert.match(prompt, /SENTINEL_CODE/);
  // No confirming context reaches the challenger.
  assert.doesNotMatch(prompt, /SENTINEL_EVIDENCE_LEAK/);
  assert.doesNotMatch(prompt, /SENTINEL_REASONING_LEAK/);
  assert.doesNotMatch(prompt, /SENTINEL_XREF_LEAK/);
  assert.doesNotMatch(prompt, /surfaced/);
  assert.equal(ctx.calls[0].agentType, 'deep-review:challenger');
});

test('cap overflow -> challenge=skipped, excluded from the high-confidence bucket', async () => {
  const findings = [cFinding('F1', 'critical'), cFinding('F2', 'high'), cFinding('F3', 'low')];
  const ctx = challengeCtx(); // every challenger returns score 80 (survives)
  const out = await challengeStage(ctx, { findings, limits: { challengeCap: 2 }, policy: {} });
  // Top 2 by rank (critical, high) are challenged and survive; the low-sev overflow skipped.
  assert.equal(ctx.calls.length, 2);
  const hiIds = out.findings.map((f) => f.id);
  assert.ok(hiIds.includes('F1'));
  assert.ok(hiIds.includes('F2'));
  assert.ok(!hiIds.includes('F3'));
  const skipped = out.unverified.find((f) => f.id === 'F3');
  assert.equal(skipped.challenge, 'skipped');
  assert.equal(out.stats.dispatched, 2);
  assert.equal(out.stats.completed, 2);
  assert.equal(out.stats.skipped, 1);
});

test('a null challenger member -> that finding challenge=skipped, not in high-confidence bucket', async () => {
  const findings = [cFinding('F1', 'critical'), cFinding('F2', 'low')];
  // Rank: [F1(critical), F2(low)]; member 1 (F2) nulled.
  const ctx = challengeCtx({ nulls: [1] });
  const out = await challengeStage(ctx, { findings, limits: { challengeCap: 40 }, policy: {} });
  assert.deepEqual(out.findings.map((f) => f.id), ['F1']);
  const skipped = out.unverified.find((f) => f.id === 'F2');
  assert.equal(skipped.challenge, 'skipped');
  assert.equal(out.stats.dispatched, 2);
  assert.equal(out.stats.completed, 1);
  assert.ok(out.gaps.some((g) => /F2/.test(g)));
});

test('an unscored (non-int) challenger result -> skipped, never high-confidence', async () => {
  const findings = [cFinding('F1', 'high')];
  const ctx = challengeCtx({ byIdx: () => ({ score: 'not-a-number', justification: 'x' }) });
  const out = await challengeStage(ctx, { findings, limits: { challengeCap: 40 }, policy: {} });
  assert.equal(out.findings.length, 0);
  assert.equal(out.unverified[0].id, 'F1');
  assert.equal(out.unverified[0].challenge, 'skipped');
});

test('challenge: low blind score removes a non-security finding (applyChallenges wired)', async () => {
  const findings = [cFinding('F1', 'high')];
  const ctx = challengeCtx({ byIdx: () => ({ score: 10, justification: 'does not hold' }) });
  const out = await challengeStage(ctx, { findings, limits: { challengeCap: 40 }, policy: {} });
  assert.equal(out.findings.length, 0); // removed by the < 25 blind-score rule
  assert.equal(out.stats.challenge_removed, 1);
  assert.equal(out.eliminated.length, 1);
});

test('challenge: generated_at threaded from args, empty set is trivial', async () => {
  const ctx = challengeCtx();
  const out = await challengeStage(ctx, { findings: [], limits: { challengeCap: 40 }, policy: {}, generatedAt: '2026-07-18T00:00:00Z' });
  assert.equal(ctx.calls.length, 0);
  assert.deepEqual(out.findings, []);
  assert.deepEqual(out.unverified, []);
  assert.equal(out.generated_at, '2026-07-18T00:00:00Z');
});
