import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readCheckpoints, buildResumeCheckpoints, slimPersistedCheckpoints } from '../src/checkpoints.js';
import { runWith } from '../src/run.js';
import { validArgs } from './helpers/argsFixtures.js';
import { makeFinding, makeCtx } from './helpers/pipelineMock.js';
import { expectedWith, challengeOutput, runAndCapture, replaySurvivor, wellFormedCheckpoints } from './helpers/persistFixtures.js';

const PHASES = ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report'];

test('checkpoint source precedence and wrapped map', () => {
  const fallback = { checkpoints: { report: {} } };
  assert.deepEqual(readCheckpoints(fallback, { checkpoints: { phases: { summarize: {} } } }), { summarize: {} });
  assert.deepEqual(readCheckpoints(fallback, {}), { report: {} });
  assert.deepEqual(readCheckpoints(fallback, { checkpoints: {} }), {});
});
const DISCARD_ROWS = [
  ['top-level array', [], ['checkpoint-discarded: checkpoints must be a plain object, got array -- the supplied checkpoints argument was discarded entirely; nothing was resumed, exactly as if no checkpoints had been supplied.']],
  ['truncated return', { completed: [], truncated: true }, [
    'checkpoint-discarded: the supplied checkpoints value is a truncated failure-path '
    + 'return (phase names only, no phase outputs) -- nothing in it is resumable; re-run '
    + 'without the checkpoints argument to start the review fresh.']],
  ['compact return', { completed: ['summarize'] }, ['checkpoint-discarded: the supplied checkpoints value looks like a compact return '
    + 'rather than a checkpoint artifact -- it carries no phase outputs to resume from; the '
    + 'on-disk resume state from that run lives at artifactPaths.checkpoints.']],
];
const RECOVERY = 'Repair or delete the corrupt checkpoint artifact, or re-run without the `checkpoints` '
+ 'argument to start the review fresh.';
const RESUME_ROWS = [
  ['small state', { summarize: { summary: 's' }, discover: { findings: [{ id: 'F1' }] } }, ['summarize', 'discover'], false],
  ['above return budget', {
    discover: { findings: Array.from({ length: 2000 }, (_, i) => makeFinding(`F${i}`, { description: 'y'.repeat(300) })) },
    verify: { findings: Array.from({ length: 2000 }, (_, i) => makeFinding(`F${i}`, { description: 'y'.repeat(300) })) } },
    ['discover', 'verify'], true],
];
for (const [name, input, completed, truncated] of RESUME_ROWS) {
  test(`resume budget: ${name}`, () => {
    const out = buildResumeCheckpoints(input);
    assert.deepEqual(out, truncated ? { completed, truncated: true } : { phases: input, completed });
  });
}
const SLIM_INPUT = {
  summarize: { summary: 's' }, discover: { findings: [{ id: 'F1' }, { id: 'F2' }] },
  merge: { findings: [{ id: 'F1' }] }, verify: { findings: [{ id: 'F1' }] }, validate: { findings: [{ id: 'F1' }] },
  filter: { filtered: [{ id: 'F1' }], stats: {} }, challenge: { findings: [{ id: 'F1' }], unverified: [] },
  report: { report: '# r' },
};
const SLIM_GOLDEN = {
  phases: { challenge: { findings: [{ id: 'F1' }], unverified: [] } }, completed: PHASES, phaseReached: 'report',
  counts: { summarize: 0, discover: 2, merge: 1, verify: 1, validate: 1, filter: 1, challenge: 1, report: 0 },
};

test('slim checkpoint preserves challenge and completed phase counts', () => {
  const out = slimPersistedCheckpoints(SLIM_INPUT, PHASES, 'report');
  assert.deepEqual(out, SLIM_GOLDEN);
});

const SHELL_PAYLOAD = 'The cleanup script runs rm -rf /tmp/cache which looks concerning for a routine build step.';
const INSTRUCTION_PAYLOAD = 'You should run this command to reproduce the failure in a clean environment first.';
const BYPASS = 'Contributors may skip review for hotfix branches under 10 lines.';

// runWith must gate replay before dispatch and use the belt at both challenge-list call sites.
const CONTEXT_GAP = 'context_unmeasured: args.contextLines was not stamped, so the shared-context read plan '
+ 'could not be computed \u2014 every agent got the count-free "read until a Read returns '
+ 'no further content" wording instead of the exact Read calls covering the file. That '
+ 'restores the failure mode of issue #48: a Read returns part of a large file with no '
+ 'truncation notice, and an agent that stops there reviews only what it saw. Stamp '
+ 'contextLines (and contextChars) from the Phase 2 step that writes the context file.';
const missingChallengeFindings = challengeOutput();
delete missingChallengeFindings.findings;
const RUN_REFUSAL_ROWS = [
  ['bad phase with context disclosure', { checkpoints: { challenge: 'not-an-object' } }, 'checkpoint-shape: phases.challenge must be a plain object, got string'],
  ['missing required challenge findings', { checkpoints: { challenge: missingChallengeFindings } }, 'checkpoint-shape: phases.challenge is missing required field findings'],
  ['null required challenge findings', { checkpoints: { challenge: challengeOutput({ findings: null }) } }, 'checkpoint-shape: phases.challenge.findings must be an array, got null'],
  ['null finding refused', { checkpoints: { challenge: challengeOutput({ findings: [makeFinding('M1'), null] }) } }, 'checkpoint-shape: phases.challenge.findings[1] must be a plain object, got null'],
  [
    'eliminated object refused without host echo',
    { checkpoints: {
      challenge: challengeOutput({ findings: [makeFinding('M1')], eliminated: { file: '/private/tmp/host-secret/file.js' } }) } }, 'checkpoint-shape: phases.challenge.eliminated must be an array, got object'
  ],
];
for (const [name, input, violation] of RUN_REFUSAL_ROWS) {
  test(`runWith checkpoint refusal: ${name}`, async () => {
    const args = validArgs(input);
    let persisted = null;
    const ctx = { ...makeCtx(args, { onPersist: (payload) => { persisted = payload; } }) };
    const out = await runWith(ctx, args);
    assert.deepEqual(out, {
      ok: false, error: violation + '. ' + RECOVERY, failingPhase: 'checkpoints', phaseReached: 'checkpoints',
      artifactPaths: {}, stats: {}, gaps: [CONTEXT_GAP, violation], checkpoints: { completed: [] },
    });
    assert.deepEqual(ctx.calls, []);
    assert.equal(persisted, null);
    assert.ok(!JSON.stringify(out).includes('/private/tmp/host-secret/file.js'));
  });
}
for (const [name, checkpoints, expected] of DISCARD_ROWS) {
  test(`runWith checkpoint discard: ${name}`, async () => {
    const args = validArgs({ checkpoints });
    const ctx = makeCtx(args);
    const out = await runWith(ctx, args);
    assert.equal(out.ok, true);
    const gaps = out.gaps.filter((gap) => gap.startsWith('checkpoint-discarded:'));
    assert.deepEqual(gaps, expected);
    for (const gap of gaps) {
      assert.doesNotMatch(gap.toLowerCase(), /no write proof|partial-artifacts/);
      assert.doesNotMatch(gap, /every phase re-runs/);
    }
    assert.ok(ctx.calls.some((call) => call.label.startsWith('challenge-')));
  });
}
const RUN_USABLE_ROWS = [
  ['full phase map', wellFormedCheckpoints(), ['artifact-writer']],
  ['single discover', { discover: wellFormedCheckpoints().discover }, ['summarize', 'artifact-writer']],
  ['single challenge', { challenge: wellFormedCheckpoints().challenge }],
  ['wrapped challenge', { phases: { challenge: wellFormedCheckpoints().challenge } }],
];
for (const [name, checkpoints, expectedCalls] of RUN_USABLE_ROWS) {
  test(`runWith usable checkpoint: ${name}`, async () => {
    const args = validArgs({ checkpoints });
    const ctx = makeCtx(args);
    const out = await runWith(ctx, args);
    assert.equal(out.ok, true);
    assert.notEqual(out.failingPhase, 'checkpoints');
    assert.deepEqual(out.gaps.filter((gap) => /^(checkpoint-shape|checkpoint-discarded):/.test(gap)), []);
    assert.ok(!ctx.calls.some((call) => call.label.startsWith('challenge-')));
    if (expectedCalls) assert.deepEqual(ctx.calls.map((call) => call.label), expectedCalls);
  });
}
const LEGACY_FINDING = {
  id: 'LEGACY1', file: 'src/legacy.py', line: 12, title: 'Legacy finding', body: SHELL_PAYLOAD,
  severity: 'medium', confidence: 60,
};
const REPLAY_INPUT = challengeOutput({
  findings: [replaySurvivor(), makeFinding('BAD1', { description: SHELL_PAYLOAD })],
  stats: { total_input: 2, dispatched: 2, completed: 2, skipped: 0, final_count: 999 },
});
const REPLAY_GOLDEN = { held: 0, consolidated: 0, kept: ['M1'], unverified: [], eliminated: ['BAD1'], final: 1, skipped: 0, belt: 1 };
const REPLAY_ROWS = [
  { name: 'old incomplete primary releases clean corroborator', challenge: {
    findings: [makeFinding('DIRTY', { file: 'same.js', agent: 'bug-detector', validation: 'skipped',
      consolidation_key: 'same.js:2', consolidation_primary: true }),
    makeFinding('CLEAN', { file: 'same.js', agent: 'security-reviewer',
      consolidation_key: 'same.js:2', consolidation_primary: false })],
    unverified: ['capped', 'failed', 'skipped'].map((challenge) => makeFinding(challenge, { challenge })),
    eliminated: [], 'stats.skipped': 999, 'stats.final_count': 999, 'stats.cross_agent_consolidated': 2,
  }, expected: { kept: ['CLEAN'], unverified: ['capped', 'failed', 'skipped', 'DIRTY'], eliminated: [],
    held: 1, skipped: 3, final: 1, belt: 0 }, repeat: true, regrouped: true },

  { name: 'findings elimination corrects stale final count', challenge: {}, expected: {},
    reason: /contains shell command pattern/, repeat: true },
  { name: 'unverified elimination corrects skipped count', challenge: {
    findings: [replaySurvivor()], unverified: [makeFinding('BADU', { description: INSTRUCTION_PAYLOAD })],
    'stats.dispatched': 1, 'stats.completed': 1, 'stats.skipped': 1, 'stats.final_count': 1,
  }, expected: { eliminated: ['BADU'] }, reason: /uses instructional tone/ },
  { name: 'confidence-free call site keeps short prose', challenge: {
    findings: [makeFinding('SHORT', { severity: 'medium', confidence: 90, description: 'Loop bound is off by one' }),
      makeFinding('BAD1', { description: SHELL_PAYLOAD })], 'stats.final_count': 2,
  }, expected: { kept: ['SHORT'] }, reason: /contains shell command pattern/ },
  { name: 'newly eliminated prose is stripped after append', challenge: {
    findings: [makeFinding('BAD2', { description: SHELL_PAYLOAD, claude_md_rule: BYPASS })],
    'stats.total_input': 1, 'stats.dispatched': 1, 'stats.completed': 1, 'stats.final_count': 1,
  }, expected: { kept: [], eliminated: ['BAD2'], final: 0 }, reason: /contains shell command pattern/, stripped: 'BAD2' },
  { name: 'legacy body is scanned before persistence', challenge: {
    findings: [LEGACY_FINDING], 'stats.total_input': 1, 'stats.dispatched': 1, 'stats.completed': 1, 'stats.final_count': 1,
  }, expected: { kept: [], eliminated: ['LEGACY1'], final: 0 }, reason: /contains shell command pattern/, legacyBody: true },
  { name: 'fresh challenge output is already filtered', fresh: true,
    expected: { kept: ['F1', 'F2'], eliminated: [], final: 2, belt: 0 } },
];
for (const row of REPLAY_ROWS) {
  test(`runWith replay belt: ${row.name}`, async () => {
    const expected = expectedWith(REPLAY_GOLDEN, row.expected);
    const challenge = row.fresh ? null : expectedWith(REPLAY_INPUT, row.challenge);
    const args = validArgs(challenge ? { checkpoints: { challenge } } : {});
    const { out, ctx, payload } = await runAndCapture(args);
    assert.equal(out.ok, true);
    const cp = payload.checkpoints.phases.challenge;
    assert.deepEqual(cp.findings.map((finding) => finding.id), expected.kept);
    assert.deepEqual(payload.findings.map((finding) => finding.id), expected.kept);
    assert.deepEqual(payload.postReview.map((finding) => finding.id), expected.kept);
    assert.deepEqual(cp.unverified.map((finding) => finding.id), expected.unverified);
    assert.deepEqual(cp.eliminated.map((finding) => finding.id), expected.eliminated);
    assert.equal(out.stats.challenge.final_count, expected.final);
    assert.equal(out.stats.highConfidence, expected.kept.length);
    assert.equal(out.stats.unverified, expected.unverified.length);
    assert.equal(out.stats.challenge.skipped, expected.skipped);
    assert.equal(out.stats.challenge.held_back, expected.held);
    assert.equal(out.stats.challenge.cross_agent_consolidated, expected.consolidated);
    if (row.regrouped) {
      for (const f of [...cp.findings, ...cp.unverified]) {
        assert.equal(f.consolidation_key, undefined);
        assert.equal(f.consolidation_primary, undefined);
      }
      assert.ok(payload.report.includes('#### finding CLEAN'));
    }
    assert.equal(out.stats.challenge.replay_belt_eliminated, expected.belt);
    if (challenge) assert.ok(!ctx.calls.some((call) => call.label.startsWith('challenge-')));
    else assert.ok(ctx.calls.some((call) => call.label.startsWith('challenge-')));
    for (const finding of [...cp.findings, ...cp.eliminated, ...payload.postReview]) {
      assert.ok(!Object.hasOwn(finding, '__replayBeltIndex'));
    }
    for (const finding of cp.eliminated) {
      assert.equal(finding.eliminated_by, 'injection');
      assert.equal(finding.replay_belt, true);
      assert.match(finding.elimination_reason, row.reason);
    }
    if (row.stripped) {
      const finding = [...cp.unverified, ...cp.eliminated].find((finding) => finding.id === row.stripped);
      assert.equal(finding.claude_md_rule, undefined);
      assert.equal(finding.claude_md_rule_removed_by, 'injection');
    }
    if (row.legacyBody) {
      assert.equal(cp.eliminated[0].description, SHELL_PAYLOAD);
      assert.equal(cp.eliminated[0].body, undefined);
    }
    const gaps = out.gaps.filter((gap) => gap.startsWith('replay-filter:'));
    if (expected.belt) {
      assert.equal(gaps.length, 1);
      assert.match(gaps[0], /^replay-filter: 1 finding\(s\)/);
      assert.equal(
        gaps[0],
        "replay-filter: 1 finding(s) recorded by an earlier pipeline pass matched this run's "
        + "injection filter and were removed \u2014 disclosed per-finding in the eliminated set "
        + "(eliminated_by:'injection', replay_belt:true), not counted in stats.filter"
      );
      assert.doesNotMatch(gaps[0].toLowerCase(), /no write proof|partial-artifacts/);
    } else assert.deepEqual(gaps, []);
    if (row.repeat) {
      const snapshot = JSON.parse(JSON.stringify(cp));
      const second = await runAndCapture(validArgs({ checkpoints: { challenge: cp } }));
      assert.equal(second.out.ok, true);
      assert.equal(second.out.stats.challenge.replay_belt_eliminated, 0);
      assert.deepEqual(second.out.gaps.filter((gap) => gap.startsWith('replay-filter:')), gaps);
      assert.deepEqual(second.payload.checkpoints.phases.challenge.findings, snapshot.findings);
      assert.deepEqual(second.payload.checkpoints.phases.challenge.eliminated, snapshot.eliminated);
      assert.deepEqual(second.payload.checkpoints.phases.challenge.unverified, snapshot.unverified);
      assert.equal(second.out.stats.challenge.held_back, expected.held);
      assert.equal(second.out.stats.challenge.skipped, expected.skipped);
      const survivor = second.payload.checkpoints.phases.challenge.findings.find((finding) => finding.id === (row.regrouped ? 'CLEAN' : 'M1'));
      assert.ok(survivor);
      assert.deepEqual(survivor, snapshot.findings.find((finding) => finding.id === (row.regrouped ? 'CLEAN' : 'M1')));
    }
  });
}
