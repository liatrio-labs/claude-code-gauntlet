import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classifyReviewTarget, parseEntryArgs, entryArgs, makeArgsRejectEnvelope, SKILL_RECOVERY_LINE } from '../src/entry.js';
import { runWith } from '../src/run.js';
import { interactiveArgs } from './helpers/argsFixtures.js';

const CLASSIFIER_ROWS = [
  { name: "number", input: 45,
    expected: { kind: 'pr_number', number: '45', ref: "45" } },
  { name: "number upper bound", input: 9999999,
    expected: { kind: 'pr_number', number: '9999999', ref: "9999999" } },
  { name: "trimmed number", input: " 45 ",
    expected: { kind: 'pr_number', number: '45', ref: "45" } },
  { name: "https suffixes", input: "https://github.com/o/r/pull/45/files?tab=files#L1",
    expected: { kind: 'pr_url', number: '45', ref: "https://github.com/o/r/pull/45/files?tab=files#L1" } },
  { name: "http URL", input: "http://github.com/o/r/pull/45/",
    expected: { kind: 'pr_url', number: '45', ref: "http://github.com/o/r/pull/45/" } },
  { name: "schemeless URL", input: "github.com/o/r/pull/45",
    expected: { kind: 'pr_url', number: '45', ref: "github.com/o/r/pull/45" } },
  { name: "subgroup MR", input: "https://gitlab.example/g/sub/p/-/merge_requests/89",
    expected: { kind: 'mr_url', number: '89', ref: "https://gitlab.example/g/sub/p/-/merge_requests/89" } },
  { name: "repo reference", input: "liatrio-forge/pet-clinic#45",
    expected: { kind: 'repo_ref', number: '45', ref: "liatrio-forge/pet-clinic#45" } },
  { name: "hash shorthand", input: "#310",
    expected: { kind: 'pr_shorthand', number: '310', ref: "#310" } },
  { name: "bang shorthand", input: "!89",
    expected: { kind: 'pr_shorthand', number: '89', ref: "!89" } },
  { name: "PR word", input: "pr 310",
    expected: { kind: 'pr_shorthand', number: '310', ref: "pr 310" } },
  { name: "MR word", input: "MR#7",
    expected: { kind: 'pr_shorthand', number: '7', ref: "MR#7" } },
  { name: "pull request", input: "pull request 45",
    expected: { kind: 'pr_shorthand', number: '45', ref: "pull request 45" } },
  { name: "merge request", input: "merge request 89",
    expected: { kind: 'pr_shorthand', number: '89', ref: "merge request 89" } },
  { name: "pull word", input: "pull 45",
    expected: { kind: 'pr_shorthand', number: '45', ref: "pull 45" } },
  { name: 'non-string', input: {}, expected: null },
  { name: 'null', input: null, expected: null },
  { name: 'empty', input: '   ', expected: null },
  { name: 'long', input: 'x'.repeat(201), expected: null },
  { name: 'unmatched', input: 'review my changes', expected: null },
  { name: 'bare repo URL', input: 'https://github.com/o/r', expected: null },
  { name: 'git ref', input: 'main..feature', expected: null },
  { name: 'source path', input: 'src/args.js', expected: null },
  { name: 'array', input: [], expected: null },
  { name: 'NaN', input: NaN, expected: null },
  { name: 'issues URL', input: 'https://github.com/o/r/issues/45', expected: null },
  { name: 'blob anchor URL', input: 'https://github.com/o/r/blob/main/x.js#L45', expected: null },
  { name: 'compare URL', input: 'https://github.com/o/r/compare/main...feature', expected: null },
  { name: 'ISO date', input: '2026-07-28', expected: null },
  { name: 'semver', input: '3.2.4', expected: null },
  { name: 'string zero', input: '0', expected: null },
  { name: 'leading zero', input: '0045', expected: null },
  { name: 'eight digits', input: '12345678', expected: null },
  { name: 'negative', input: -1, expected: null },
  { name: 'fraction', input: 1.5, expected: null },
  { name: 'number above bound', input: 10000000, expected: null },
  { name: 'pathological', input: 'a/'.repeat(100), expected: null },
 ];
for (const { name, input, expected } of CLASSIFIER_ROWS) {
  test(`classify: ${name}`, () => {
    const start = Date.now();
    assert.deepEqual(classifyReviewTarget(input), expected);
    assert.ok(Date.now() - start < 1000);
  });
}

const REFUSAL_ROWS = [
  { name: 'undefined', input: undefined,
    expected: "args never arrived: the code-gauntlet workflow was invoked with no args at all (got undefined). This " +
    "workflow builds nothing itself \u2014 Phases 1-2 of the code-gauntlet skill resolve and check out the target, " +
    "write the diff and shared context, and assemble the argsVersion:1 waist object it consumes. Run the skill " +
    "instead: Skill(\"code-gauntlet:code-gauntlet\", args=\"<PR number or URL \u2014 omit to auto-detect this branch's " +
    "PR, else local changes>\"). To resume an interrupted run, re-invoke with the SAME args object plus " +
    "resumeFromRunId." },
  { name: 'null', input: null,
    expected: "args never arrived: the code-gauntlet workflow was invoked with no args at all (got null). This workflow " +
    "builds nothing itself \u2014 Phases 1-2 of the code-gauntlet skill resolve and check out the target, write the " +
    "diff and shared context, and assemble the argsVersion:1 waist object it consumes. Run the skill instead: " +
    "Skill(\"code-gauntlet:code-gauntlet\", args=\"<PR number or URL \u2014 omit to auto-detect this branch's PR, else " +
    "local changes>\"). To resume an interrupted run, re-invoke with the SAME args object plus resumeFromRunId." },
  { name: 'PR URL', input: 'https://github.com/o/r/pull/45',
    expected: "args was a GitHub PR URL (https://github.com/o/r/pull/45), not the assembled argsVersion:1 waist object \u2014 do" +
    " not invoke this workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this " +
    "instead: Skill(\"code-gauntlet:code-gauntlet\", args=\"https://github.com/o/r/pull/45\")" },
  { name: 'MR URL', input: 'https://gitlab.com/g/p/-/merge_requests/89',
    expected: "args was a GitLab MR URL (https://gitlab.com/g/p/-/merge_requests/89), not the assembled argsVersion:1 waist" +
    " object \u2014 do not invoke this workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Run" +
    " this instead: Skill(\"code-gauntlet:code-gauntlet\", args=\"https://gitlab.com/g/p/-/merge_requests/89\")" },
  { name: 'repo ref', input: 'liatrio-forge/pet-clinic#45',
    expected: "args was an owner/repo PR reference (liatrio-forge/pet-clinic#45), not the assembled argsVersion:1 waist " +
    "object \u2014 do not invoke this workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Run " +
    "this instead: Skill(\"code-gauntlet:code-gauntlet\", args=\"liatrio-forge/pet-clinic#45\")" },
  { name: 'numeric string', input: '310',
    expected: "args was a bare PR/MR number (310), not the assembled argsVersion:1 waist object \u2014 do not invoke this " +
    "workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this instead: " +
    "Skill(\"code-gauntlet:code-gauntlet\", args=\"310\")" },
  { name: 'raw number', input: 45,
    expected: "args was a bare PR/MR number (45), not the assembled argsVersion:1 waist object \u2014 do not invoke this " +
    "workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this instead: " +
    "Skill(\"code-gauntlet:code-gauntlet\", args=\"45\")" },
  { name: 'shorthand', input: 'PR 310',
    expected: "args was a PR/MR reference (PR 310), not the assembled argsVersion:1 waist object \u2014 do not invoke this " +
    "workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this instead: " +
    "Skill(\"code-gauntlet:code-gauntlet\", args=\"PR 310\")" },
  { name: 'encoded terminal', input: JSON.stringify('PR 310'),
    expected: "args was a PR/MR reference (PR 310), not the assembled argsVersion:1 waist object \u2014 do not invoke this " +
    "workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this instead: " +
    "Skill(\"code-gauntlet:code-gauntlet\", args=\"PR 310\")" },
  { name: 'malformed', input: 'not valid json {{{',
    expected: "args was a raw string: \"not valid json {{{\", not the assembled argsVersion:1 waist object \u2014 do not invoke " +
    "this workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this instead: " +
    "Skill(\"code-gauntlet:code-gauntlet\", args=\"<your PR number or URL>\")" },
  { name: 'escaped echo', input: '{\n\t',
    expected: "args was a raw string: \"{\\n\\t\", not the assembled argsVersion:1 waist object \u2014 do not invoke this workflow " +
    "directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this instead: " +
    "Skill(\"code-gauntlet:code-gauntlet\", args=\"<your PR number or URL>\")" },
  { name: 'truncated echo', input: 'x'.repeat(81),
    expected: "args was a raw string: \"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\u2026\", " +
    "not the assembled argsVersion:1 waist object \u2014 do not invoke this workflow directly; run the code-gauntlet " +
    "skill (Phases 1-2 build the args). Run this instead: Skill(\"code-gauntlet:code-gauntlet\", args=\"<your PR " +
    "number or URL>\")" },
  { name: 'boolean', input: true,
    expected: "args was a boolean: true, not the assembled argsVersion:1 waist object \u2014 do not invoke this workflow " +
    "directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this instead: " +
    "Skill(\"code-gauntlet:code-gauntlet\", args=\"<your PR number or URL>\")" },
  { name: 'array', input: [1, 2],
    expected: "args was an array, not the assembled argsVersion:1 waist object \u2014 do not invoke this workflow directly; run " +
    "the code-gauntlet skill (Phases 1-2 build the args). Run this instead: Skill(\"code-gauntlet:code-gauntlet\", " +
    "args=\"<your PR number or URL>\")" },
  { name: 'large number', input: 1e99,
    expected: "args was a number: 1e+99, not the assembled argsVersion:1 waist object \u2014 do not invoke this workflow " +
    "directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this instead: " +
    "Skill(\"code-gauntlet:code-gauntlet\", args=\"<your PR number or URL>\")" },
  { name: 'unrecognized', input: () => 1,
    expected: "args was an unrecognized value, not the assembled argsVersion:1 waist object \u2014 do not invoke this workflow " +
    "directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this instead: " +
    "Skill(\"code-gauntlet:code-gauntlet\", args=\"<your PR number or URL>\")" },
  { name: 'quote guard', input: 'o"r/repo#45',
    expected: "args was a raw string: \"o\\\"r/repo#45\", not the assembled argsVersion:1 waist object \u2014 do not invoke this " +
    "workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this instead: " +
    "Skill(\"code-gauntlet:code-gauntlet\", args=\"<your PR number or URL>\")" },
  { name: 'backslash guard', input: 'https://github.com/o/r/pull/45/x\\',
    expected: "args was a raw string: \"https://github.com/o/r/pull/45/x\\\\\", not the assembled argsVersion:1 waist object \u2014 " +
    "do not invoke this workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this " +
    "instead: Skill(\"code-gauntlet:code-gauntlet\", args=\"<your PR number or URL>\")" },
 ];
for (const { name, input, expected } of REFUSAL_ROWS) {
  test(`refuse: ${name}`, async () => {
    assert.throws(() => parseEntryArgs(input), { message: expected });
    const envelope = {
      ok: false, error: expected, phaseReached: 'args', failingPhase: 'args', artifactPaths: {}, stats: {}, gaps: [expected],
    };
    assert.deepEqual(entryArgs(input), { ok: false, envelope });
    const calls = [];
    const ctx = {
      agent: async () => { calls.push('agent'); },
      parallel: async () => { calls.push('parallel'); },
    };
    assert.deepEqual(await runWith(ctx, input), envelope);
    assert.deepEqual(await runWith(undefined, input), envelope);
    assert.deepEqual(calls, []);
  });
}

const DECODE_ROWS = [
  { name: 'object', depth: 0, input: { argsVersion: 1, reviewConfig: null, persist: null },
    expected: { argsVersion: 1, reviewConfig: null, persist: null } },
  { name: 'JSON', depth: 1, input: { argsVersion: 1 }, expected: { argsVersion: 1 } },
  { name: 'double JSON', depth: 2, input: { argsVersion: 1 }, expected: { argsVersion: 1 } },
  { name: 'triple JSON', depth: 3, input: { argsVersion: 1 }, expected: { argsVersion: 1 } },
  { name: 'four JSON', depth: 4, input: { argsVersion: 1 }, expected: { argsVersion: 1 } },
  { name: 'five JSON', depth: 5, input: {}, expected: false },
];
for (const { name, depth, input, expected } of DECODE_ROWS) {
  test(`decode: ${name}`, () => {
    let raw = input;
    for (let i = 0; i < depth; i++) raw = JSON.stringify(raw);
    if (expected === false) {
      assert.equal(entryArgs(raw).ok, false);
      assert.throws(() => parseEntryArgs(raw), /code-gauntlet skill/);
    } else {
      assert.deepEqual(parseEntryArgs(raw), expected);
      assert.deepEqual(entryArgs(raw), { ok: true, waist: expected });
    }
  });
}

const ENVELOPE_ROWS = [
  { name: 'factory', input: ['x', ['gap']],
    expected: { ok: false, error: 'x', phaseReached: 'args', failingPhase: 'args', artifactPaths: {}, stats: {}, gaps: ['gap'] } },
];
for (const { name, input, expected } of ENVELOPE_ROWS) {
  test(`envelope: ${name}`, () => assert.deepEqual(makeArgsRejectEnvelope(...input), expected));
}

const WIRED_ROWS = [
  { name: 'decoded validation', input: () => JSON.stringify(JSON.stringify(interactiveArgs({ mode: 'bogus' }))),
    expected: { error: 'invalid mode: bogus', recovery:
      'The code-gauntlet skill assembles this object in Phases 1-2 \u2014 ' +
      'run it instead of invoking the workflow directly: ' +
      'Skill("code-gauntlet:code-gauntlet", args="<PR number or URL>")' } },
  { name: 'plain object cascade', input: () => ({}), expected: {
    error: 'missing required field: mode', recovery: 'The code-gauntlet skill assembles this object in Phases 1-2 \u2014 ' +
      'run it instead of invoking the workflow directly: ' +
      'Skill("code-gauntlet:code-gauntlet", args="<PR number or URL>")',
  } },
];
for (const { name, input, expected } of WIRED_ROWS) {
  test(`entry wiring: ${name}`, async () => {
    const calls = [];
    const out = await runWith({ agent: async () => calls.push('agent'), parallel: async () => calls.push('parallel') }, input());
    assert.equal(out.ok, false);
    assert.equal(out.failingPhase, 'args');
    assert.ok(out.error.includes(expected.error));
    assert.ok(out.error.includes(expected.recovery));
    assert.deepEqual(calls, []);
  });
}

const WIRED_ENVELOPE_ROWS = [
  { name: 'validation refusal', input: () => interactiveArgs({ argsVersion: 2 }), expected: {
    ok: false,
    error: 'invalid args: unsupported argsVersion 2 (expected 1). The code-gauntlet skill assembles this object ' +
      'in Phases 1-2 \u2014 run it instead of invoking the workflow directly: ' +
      'Skill("code-gauntlet:code-gauntlet", args="<PR number or URL>")',
    phaseReached: 'args', failingPhase: 'args', artifactPaths: {}, stats: {},
    gaps: ['unsupported argsVersion 2 (expected 1)'],
  } },
];
for (const { name, input, expected } of WIRED_ENVELOPE_ROWS) {
  test(`entry envelope: ${name}`, async () => {
    assert.deepEqual(await runWith(undefined, input()), expected);
  });
}

const RECOVERY_ROWS = [
  { name: 'skill command', input: SKILL_RECOVERY_LINE,
    expected: "The code-gauntlet skill assembles this object in Phases 1-2 \u2014 run it instead of invoking the workflow " +
    "directly: Skill(\"code-gauntlet:code-gauntlet\", args=\"<PR number or URL>\")" },
];
for (const { name, input, expected } of RECOVERY_ROWS) {
  test(`recovery: ${name}`, () => assert.equal(input, expected));
}
