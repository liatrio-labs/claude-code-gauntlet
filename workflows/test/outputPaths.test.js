import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  PATH_ESCAPE_MARKER,
  requireAbsoluteOutputDir,
  pathEscapeReason,
  plannedArtifactPaths,
  checkpointPath,
  persistPlanPath
} from '../src/outputPaths.js';
import { expectedWith } from './helpers/persistFixtures.js';

const ROOT_ROWS = [
  ['C:/', 'C:/'],
  ['/repo/.code-gauntlet', '/repo/.code-gauntlet'],
  ['/repo//.code-gauntlet/', '/repo/.code-gauntlet'],
  ['/My Repo/.code-gauntlet', '/My Repo/.code-gauntlet'],
];
for (const [input, expected] of ROOT_ROWS) {
  test(`output root: ${input}`, () => assert.equal(requireAbsoluteOutputDir(input), expected));
}
const REFUSED_ROOTS = [
  ['relative', '.code-gauntlet', '".code-gauntlet"'], ['parent segment', '/repo/../evil', '"/repo/../evil"'],
  ['backslash', '/repo\\evil', '"/repo\\\\evil"'], ['empty', '', '""'],
  ['null', null, 'null'], ['missing', undefined, 'undefined'], ['number', 3, '3'], ['array', [], '[]'],
];
for (const [name, input, spelling] of REFUSED_ROOTS) {
  test(`refused root: ${name}`, () => {
    for (
      const call of [
        () => requireAbsoluteOutputDir(input),
        () => plannedArtifactPaths(input, 'abc1234'),
        () => persistPlanPath(input, 'abc1234')
      ]
    ) {
      assert.throws(
        call,
        {
          name: 'Error',
          message: `outputDir must be an absolute confined root (no .. or \\ segments); got ${spelling}`
        }
      );
    }
  });
}
const STAMP_GOLDEN = {
  paths: {
    findings: '/repo/.code-gauntlet/code-gauntlet-findings-abc1234.json',
    report: '/repo/.code-gauntlet/code-gauntlet-report-abc1234.md',
    postReview: '/repo/.code-gauntlet/code-gauntlet-post-review-abc1234.json',
    checkpoints: '/repo/.code-gauntlet/code-gauntlet-checkpoint-all-abc1234.json',
  },
  plan: '/repo/.code-gauntlet/code-gauntlet-persist-plan-abc1234.json',
};
const STAMP_ROWS = [
  { name: 'four ordered destinations', input: {}, expected: {}, glob: /\/code-gauntlet-.*-abc1234\..+$/ },
  { name: 'normalized root and alternate sha', input: { root: '/out//', sha: 'def5678' }, expected: {
    'paths.findings': '/out/code-gauntlet-findings-def5678.json',
    'paths.report': '/out/code-gauntlet-report-def5678.md',
    'paths.postReview': '/out/code-gauntlet-post-review-def5678.json',
    'paths.checkpoints': '/out/code-gauntlet-checkpoint-all-def5678.json',
    plan: '/out/code-gauntlet-persist-plan-def5678.json',
  }, glob: /\/code-gauntlet-.*-def5678\..+$/ },
];
for (const row of STAMP_ROWS) {
  test(`path stamps: ${row.name}`, () => {
    const { root, sha } = { root: '/repo/.code-gauntlet', sha: 'abc1234', ...row.input };
    const expected = expectedWith(STAMP_GOLDEN, row.expected);
    const paths = plannedArtifactPaths(root, sha);
    assert.deepEqual(paths, expected.paths);
    assert.deepEqual(Object.keys(paths), ['findings', 'report', 'postReview', 'checkpoints']);
    assert.equal(persistPlanPath(root, sha), expected.plan);
    assert.match(expected.plan, row.glob);
  });
}
for (const [name, call, expected] of [
  [
    'artifact escape',
    () => plannedArtifactPaths('/out', 'abc/../evil'),
    'planned artifact path escapes outputDir: /out/code-gauntlet-findings-abc/../evil.json'
  ],
  [
    'plan escape',
    () => persistPlanPath('/out', 'abc/../evil'),
    'planned artifact path escapes outputDir: /out/code-gauntlet-persist-plan-abc/../evil.json'
  ],
]) {
  test(name, () => assert.throws(call, { name: 'Error', message: expected }));
}
for (const [phase, sha, expected] of [
  ['all', 'abc1234', 'code-gauntlet-checkpoint-all-abc1234.json'],
  ['discover', 'def5678', 'code-gauntlet-checkpoint-discover-def5678.json'],
]) {
  test(`checkpoint filename: ${phase}`, () => assert.equal(checkpointPath(phase, sha), expected));
}
const FENCE_ROWS = [
  ['absent fields', [{ name: 'report', path: undefined, present: false }], null],
  ['confined path', [{ name: 'report', path: '/out/report.md', present: true }], null],
  ['all escaping fields',
    [{ name: 'report', path: '/else/report.md', present: true }, { name: 'checkpoints', path: null, present: true }],
    'path-escape: writer-reported paths outside outputDir (report=/else/report.md, checkpoints=null)'],
  ['empty path', [{ name: 'report', path: '', present: true }], 'path-escape: writer-reported paths outside outputDir (report=)'],
  ['explicit undefined', [{ name: 'report', path: undefined, present: true }],
    'path-escape: writer-reported paths outside outputDir (report=undefined)'],
];
for (const [name, fields, expected] of FENCE_ROWS) {
  test(`path fence: ${name}`, () => {
    assert.equal(PATH_ESCAPE_MARKER, 'path-escape');
    assert.equal(pathEscapeReason('/out', fields), expected);
  });
}
