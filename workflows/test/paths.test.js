import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  requireAbsoluteOutputDir,
  plannedArtifactPaths,
  persistPlanPath,
  writeArtifacts,
  PATH_ESCAPE_TOKEN,
  runWith,
} from '../src/stages.js';
import { normalizeAbsoluteRoot, pathUnderRoot, repoRelativeFindingPath } from '../src/paths.js';
import { makeFinding, validArgs, makeCtx } from './helpers/pipelineMock.js';

const ROOT = '/repo/.code-gauntlet';

// --- pathUnderRoot matrix ----------------------------------------------------

test('pathUnderRoot: path under root passes', () => {
  assert.equal(pathUnderRoot(ROOT, `${ROOT}/findings.json`), true);
});

test('pathUnderRoot: equality arm — path === root passes', () => {
  assert.equal(pathUnderRoot(ROOT, ROOT), true);
  assert.equal(pathUnderRoot(`${ROOT}/`, ROOT), true);
});

test('pathUnderRoot: prefix sibling (/tmp/out vs /tmp/out-evil) fails', () => {
  assert.equal(pathUnderRoot('/tmp/out', '/tmp/out-evil/x'), false);
});

test('pathUnderRoot: .. segment fails on path and on root', () => {
  assert.equal(pathUnderRoot(ROOT, `${ROOT}/../evil`), false);
  assert.equal(pathUnderRoot('/repo/../x', '/repo/../x/a'), false);
  assert.equal(normalizeAbsoluteRoot('/repo/../x'), null);
});

test('pathUnderRoot: backslash fails on path and on root', () => {
  assert.equal(pathUnderRoot(ROOT, `${ROOT}\\evil`), false);
  assert.equal(normalizeAbsoluteRoot('/repo\\out'), null);
});

test('pathUnderRoot: /./ collapses; // is deliberately allowed when under root', () => {
  assert.equal(pathUnderRoot(ROOT, `${ROOT}/./findings.json`), true);
  assert.equal(pathUnderRoot(`${ROOT}/./`, `${ROOT}/findings.json`), true);
  assert.equal(pathUnderRoot(ROOT, `${ROOT}//findings.json`), true);
});

test('pathUnderRoot: empty / null / undefined path is false (no TypeError)', () => {
  assert.equal(pathUnderRoot(ROOT, ''), false);
  assert.equal(pathUnderRoot(ROOT, null), false);
  assert.equal(pathUnderRoot(ROOT, undefined), false);
});

test('pathUnderRoot: relative or missing root is false', () => {
  assert.equal(pathUnderRoot('.code-gauntlet', '.code-gauntlet/x'), false);
  assert.equal(pathUnderRoot('', '/x'), false);
  assert.equal(pathUnderRoot(null, '/x'), false);
  assert.equal(pathUnderRoot('/', '/x'), true);
});

const REPO_ROOT = '/private/tmp/wt-410';
const REPO_RELATIVE_CASES = [
  { name: 'relative path stays relative', file: 'scripts/gauntlet/proc.py', expected: { file: 'scripts/gauntlet/proc.py' } },
  { name: 'leading dot slash is stripped', file: './scripts/gauntlet/proc.py', expected: { file: 'scripts/gauntlet/proc.py' } },
  { name: 'repeated leading dot slash is stripped', file: '././scripts/gauntlet/proc.py', expected: { file: 'scripts/gauntlet/proc.py' } },
  { name: 'absolute path under root becomes relative', file: `${REPO_ROOT}/scripts/gauntlet/proc.py`, expected: { file: 'scripts/gauntlet/proc.py' } },
  { name: 'absolute path under slash root becomes relative', root: '/', file: '/proc.py', expected: { file: 'proc.py' } },
  { name: 'absolute dot slash is normalized', file: `${REPO_ROOT}/./scripts/gauntlet/proc.py`, expected: { file: 'scripts/gauntlet/proc.py' } },
  { name: 'absolute path equal to root is rejected', file: REPO_ROOT, expected: { reason: 'absolute file path resolves to repoRoot' } },
  { name: 'absolute path outside root is rejected', file: '/private/tmp/wt-410-evil/proc.py', expected: { reason: 'absolute file path is outside repoRoot' } },
  { name: 'relative traversal is rejected', file: '../proc.py', expected: { reason: 'file path contains a .. segment' } },
  { name: 'absolute traversal is rejected', file: `${REPO_ROOT}/../proc.py`, expected: { reason: 'file path contains a .. segment' } },
  { name: 'backslash path is rejected', file: 'scripts\\proc.py', expected: { reason: 'file path contains a backslash' } },
  { name: 'invalid root is rejected for an absolute file', root: 'repo', file: '/repo/proc.py', expected: { reason: 'repoRoot must be an absolute path without .. segments or backslashes' } },
  { name: 'empty file is rejected', file: '', expected: { reason: 'file must be a non-empty string' } },
];
for (const c of REPO_RELATIVE_CASES) {
  test(`repoRelativeFindingPath: ${c.name}`, () => {
    assert.deepEqual(repoRelativeFindingPath(c.root ?? REPO_ROOT, c.file), c.expected);
  });
}

test('requireAbsoluteOutputDir: absolute ok; relative / missing throw distinct message', () => {
  assert.equal(requireAbsoluteOutputDir(ROOT), ROOT);
  assert.throws(
    () => requireAbsoluteOutputDir('.code-gauntlet'),
    (e) => /absolute confined root/.test(e.message) && !/planned artifact path escapes/.test(e.message),
  );
  assert.throws(
    () => requireAbsoluteOutputDir(undefined),
    (e) => /absolute confined root/.test(e.message),
  );
});

test('plannedArtifactPaths: stamps under root; sha with .. throws planned-path message', () => {
  const paths = plannedArtifactPaths(ROOT, 'abc1234');
  assert.ok(paths.findings.startsWith(`${ROOT}/`));
  assert.throws(
    () => plannedArtifactPaths(ROOT, 'abc/../evil'),
    (e) => /planned artifact path escapes outputDir/.test(e.message),
  );
});

test('persistPlanPath: requires absolute root', () => {
  assert.ok(persistPlanPath(ROOT, 'abc1234').startsWith(`${ROOT}/`));
  assert.throws(() => persistPlanPath('.code-gauntlet', 'abc1234'), /absolute confined root/);
});

// --- writeArtifacts: hard reject before outer try ---------------------------

test('writeArtifacts: relative outputDir throws (not a partial-artifacts gap)', async () => {
  const ctx = { agent: async () => ({}), parallel: async () => [] };
  await assert.rejects(
    () => writeArtifacts(ctx, {
      findings: [makeFinding('F1')],
      report: '# r',
      checkpoints: {},
      outputDir: '.code-gauntlet',
      headShaShort: 'abc1234',
    }),
    (e) => /absolute confined root/.test(e.message),
  );
});

test('writeArtifacts: missing outputDir throws', async () => {
  const ctx = { agent: async () => ({}), parallel: async () => [] };
  await assert.rejects(
    () => writeArtifacts(ctx, {
      findings: [makeFinding('F1')],
      report: '# r',
      checkpoints: {},
      headShaShort: 'abc1234',
    }),
    (e) => /absolute confined root/.test(e.message),
  );
});

test('writeArtifacts: writer echo outside fence → path-escape gap + partial-artifacts', async () => {
  const paths = plannedArtifactPaths(ROOT, 'abc1234');
  const ctx = {
    agent: async () => ({
      artifactPaths: {
        ...paths,
        checkpoints: '/tmp/evil-checkpoints.json',
      },
    }),
    parallel: async () => [],
  };
  const out = await writeArtifacts(ctx, {
    findings: [makeFinding('F1')],
    postReview: [],
    report: '# r',
    checkpoints: {},
    outputDir: ROOT,
    headShaShort: 'abc1234',
  });
  assert.equal(out.partial, true);
  assert.equal(out.artifactPaths.findings, null);
  assert.ok(
    out.gaps.some((g) => g.includes(PATH_ESCAPE_TOKEN) && /checkpoints=/.test(g) && /partial-artifacts/.test(g)),
    out.gaps,
  );
  // Names every escaped field in one gap (all-or-nothing partial).
  assert.equal(out.gaps.filter((g) => g.includes(PATH_ESCAPE_TOKEN)).length, 1);
});

test('writeArtifacts: writer echo with null path field → path-escape (not TypeError)', async () => {
  const paths = plannedArtifactPaths(ROOT, 'abc1234');
  const ctx = {
    agent: async () => ({
      artifactPaths: { ...paths, report: null },
    }),
    parallel: async () => [],
  };
  const out = await writeArtifacts(ctx, {
    findings: [makeFinding('F1')],
    postReview: [],
    report: '# r',
    checkpoints: {},
    outputDir: ROOT,
    headShaShort: 'abc1234',
  });
  assert.equal(out.partial, true);
  assert.ok(out.gaps.some((g) => g.includes(PATH_ESCAPE_TOKEN) && /report=null/.test(g)), out.gaps);
});

// --- runWith boundary: stamp/root failure is ok:false, not a disguise gap ---

test('runWith: outputDir with .. segment → ok:false with absolute-root error (not partial-artifacts success)', async () => {
  // Waist only requires /-prefix; Persist requireAbsoluteOutputDir rejects ...
  const args = validArgs({ outputDir: '/repo/../evil/.code-gauntlet' });
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.equal(out.ok, false);
  assert.match(out.error || '', /absolute confined root/);
  assert.ok(!(out.gaps || []).some((g) => /partial-artifacts/.test(g) && /persistence threw/.test(g)));
});
