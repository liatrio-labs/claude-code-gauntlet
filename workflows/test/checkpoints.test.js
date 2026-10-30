import { test } from 'node:test';
import assert from 'node:assert/strict';
import { slimPersistedCheckpoints } from '../src/checkpoints.js';
import { fnv1a32, RETURN_CHAR_BUDGET } from '../src/wire.js';
import { runWith } from '../src/run.js';
import { validArgs } from './helpers/argsFixtures.js';
import { makeFinding, makeCtx } from './helpers/pipelineMock.js';
import { expectedWith, replayProducedCheckpoint, challengeOutput, runAndCapture, replaySurvivor, wellFormedCheckpoints } from './helpers/persistFixtures.js';

const PHASES = ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report'];

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
const RECOVERY = 'Use an intact producer checkpoint, or re-run without the `checkpoints` '
+ 'argument to start the review fresh.';
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
  const { checksum, ...rest } = out;
  assert.deepEqual(rest, SLIM_GOLDEN);
  assert.equal(checksum, fnv1a32(JSON.stringify(rest, null, 2)));
});

const SHELL_PAYLOAD = 'The cleanup script runs rm -rf /tmp/cache which looks concerning for a routine build step.';
const INSTRUCTION_PAYLOAD = 'You should run this command to reproduce the failure in a clean environment first.';
const BYPASS = 'Contributors may skip review for hotfix branches under 10 lines.';

// runWith must gate replay before dispatch and use the belt at both challenge-list call sites.
const CONTEXT_GAP = 'context_unmeasured: args.contextLines was not stamped, so the shared-context read plan '
+ 'could not be computed \u2014 every agent got the count-free "read until a Read returns '
+ 'no further content" wording instead of the exact Read calls covering the file. A Read can return part of a file with no '
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
    held: 4, skipped: 3, final: 1, belt: 0 }, repeat: true, regrouped: true },

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
    const raw = payload.checkpoints.phases.challenge;
    const cp = { findings: payload.findings.filter((f) => ['deliver', 'body', 'report_only'].includes(f.disposition)),
      unverified: payload.findings.filter((f) => f.disposition === 'held_back'),
      eliminated: payload.findings.filter((f) => f.eliminated_by === 'injection' || f.disposition === 'eliminate') };
    if (challenge) assert.deepEqual(raw.findings.map((f) => f.id), challenge.findings.map((f) => f.id));
    assert.deepEqual(cp.findings.map((finding) => finding.id), expected.kept);
    assert.deepEqual(payload.postReview.map((finding) => finding.id), expected.kept);
    assert.deepEqual(cp.unverified.map((finding) => finding.id).sort(), [...expected.unverified].sort());
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
      assert.match(finding.elimination_reason, row.reason);
      assert.deepEqual(Object.keys(finding), ['id', 'title', 'eliminated_by', 'elimination_reason']);
    }
    if (row.stripped) {
      const finding = [...cp.unverified, ...cp.eliminated].find((finding) => finding.id === row.stripped);
      assert.equal(finding.claude_md_rule, undefined);
    }
    if (row.legacyBody) {
      assert.equal(cp.eliminated[0].description, undefined);
      assert.equal(cp.eliminated[0].body, undefined);
      assert.equal(raw.findings[0].body, SHELL_PAYLOAD);
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
      const second = await replayProducedCheckpoint(args, payload.checkpoints);
      assert.equal(second.out.ok, true);
      assert.equal(second.out.stats.challenge.replay_belt_eliminated, expected.belt);
      assert.deepEqual(second.out.gaps.filter((gap) => gap.startsWith('replay-filter:')), gaps);
      assert.deepEqual(second.payload.findings, [...snapshot.findings, ...snapshot.unverified, ...snapshot.eliminated]);
      assert.equal(second.out.stats.challenge.held_back, expected.held);
      assert.equal(second.out.stats.challenge.skipped, expected.skipped);
      const survivor = second.payload.findings.find((finding) => finding.id === (row.regrouped ? 'CLEAN' : 'M1'));
      assert.ok(survivor);
      assert.deepEqual(survivor, snapshot.findings.find((finding) => finding.id === (row.regrouped ? 'CLEAN' : 'M1')));
    }
  });
}

const CHECKSUM_ROWS = [
  ['shortened description', (cp) => { cp.phases.challenge.findings[0].description = 'short'; }],
  ['changed line', (cp) => { cp.phases.challenge.findings[0].line_start += 1; }],
  ['dropped finding', (cp) => { cp.phases.challenge.findings.pop(); }],
  ['missing checksum', (cp) => { delete cp.checksum; }],
  ['non-string checksum', (cp) => { cp.checksum = 17; }],
  ['bare map', (cp, args) => { args.checkpoints = cp.phases; }],
  ['changed metadata', (cp) => { cp.completed = ['challenge']; }],
  ['unsigned invalid shape', (cp) => { cp.phases.challenge.findings = null; delete cp.checksum; }],
];
for (const [name, tamper] of CHECKSUM_ROWS) test(`checkpoint checksum refuses: ${name}`, async () => {
  const args = validArgs({ checkpoints: wellFormedCheckpoints() });
  tamper(args.checkpoints, args);
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, false);
  assert.equal(out.failingPhase, 'checkpoints');
  assert.match(out.error, /^checkpoint-checksum:.*Re-copy the complete checkpoint object unchanged/);
  assert.deepEqual(out.checkpoints, { completed: [] });
  assert.deepEqual(ctx.calls, []);
});

test('checkpoint context cannot replay without args', async () => {
  const args = validArgs();
  const ctx = makeCtx(args);
  ctx.checkpoints = wellFormedCheckpoints();
  ctx.checkpoints.summarize.summary = 'context-only marker';
  const out = await runWith(ctx, args);
  assert.equal(out.ok, true);
  assert.ok(ctx.calls.some((call) => call.label === 'summarize'));
  assert.ok(ctx.calls.some((call) => call.label.startsWith('challenge-')));
  assert.equal(JSON.stringify(out).includes('context-only marker'), false);
});

for (const [name, size, truncated] of [
  ['small', 1, false], ['just fitting', RETURN_CHAR_BUDGET - 97, false],
  ['checksum overhead crosses budget', RETURN_CHAR_BUDGET - 96, true],
  ['far over budget', RETURN_CHAR_BUDGET + 1000, true],
]) test(`checkpoint producer budget: ${name}`, async () => {
  const args = validArgs({ checkpoints: { summarize: { summary: 'x'.repeat(size) } } });
  const nullAgentLabels = [
    'bug-detector', 'security-reviewer', 'cross-file-impact', 'test-analyzer',
    'conventions-and-intent', 'type-design-analyzer', 'code-simplifier',
  ].map((name) => `code-gauntlet:${name}`);
  const { out } = await runAndCapture(args, { nullAgentLabels });
  assert.equal(out.failingPhase, 'discover');
  assert.deepEqual(out.checkpoints.completed, ['summarize']);
  if (truncated) {
    assert.equal(out.checkpoints.truncated, true);
    assert.deepEqual(out.checkpoints, { completed: ['summarize'], truncated: true });
  } else {
    assert.equal(JSON.stringify(out.checkpoints).length <= RETURN_CHAR_BUDGET, true);
    await replayProducedCheckpoint(validArgs(), out.checkpoints);
  }
});

test('checkpoint producer retains fractional criticality across JSON replay', async () => {
  const args = validArgs();
  const description = 'Detailed diagnosis with line breaks\r\nC:\\tmp\\out and astral \u{1f600}. '.repeat(40);
  const { out } = await runAndCapture(args, {
    findings: [makeFinding('FRACTION', { criticality: 0.75, description })], agentThrowLabel: 'artifact-writer',
  });
  const resumed = await replayProducedCheckpoint(args, out.checkpoints);
  assert.equal(resumed.payload.findings[0].criticality, 0.75);
  assert.equal(resumed.payload.findings[0].description, description);
});

test('late legacy replay with no admission remains held back', async () => {
  for (const location_status of [undefined, 'unknown', 'confirmed']) {
    const finding = makeFinding('legacy', { location_status, line_start: 900, line_end: 999, line: 1, end_line: 2 });
    delete finding.admission;
    const args = validArgs({ checkpoints: { challenge: challengeOutput({ findings: [finding] }) } });
    const { out, ctx, payload } = await runAndCapture(args);
    assert.equal(out.ok, true);
    assert.equal(ctx.calls.some((call) => call.label.startsWith('verify-') && call.prompt.includes('legacy')), false);
    const held = payload.findings[0];
    assert.match(payload.report, /\*\*Location:\*\* `legacy\.js \(location unconfirmed\)`/);
    assert.equal(held.disposition, 'held_back');
    assert.doesNotMatch(payload.report, /:900|-999/);
    assert.equal(held.location_status, 'unconfirmed');
    assert.equal(held.claimed_line_start, 900);
    for (const key of ['line_start', 'line_end', 'line', 'end_line']) assert.equal(Object.hasOwn(held, key), false);
  }
});
