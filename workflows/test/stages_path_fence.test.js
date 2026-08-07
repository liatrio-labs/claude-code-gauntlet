// stages_path_fence.test.js — issue #148 Persist outputDir-prefix fence.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  pathUnderOutputDir,
  requireAbsoluteOutputDir,
  normalizeOutputDirRoot,
  plannedArtifactPaths,
  persistPlanPath,
  writeArtifacts,
  PATH_ESCAPE_TOKEN,
  runWith,
} from '../src/stages.js';
import { makeFinding, validArgs, makeCtx } from './helpers/pipelineMock.js';

const ROOT = '/repo/.code-gauntlet';

// --- pathUnderOutputDir matrix ----------------------------------------------

test('pathUnderOutputDir: path under root passes', () => {
  assert.equal(pathUnderOutputDir(ROOT, `${ROOT}/findings.json`), true);
});

test('pathUnderOutputDir: equality arm — path === root passes', () => {
  assert.equal(pathUnderOutputDir(ROOT, ROOT), true);
  assert.equal(pathUnderOutputDir(`${ROOT}/`, ROOT), true);
});

test('pathUnderOutputDir: prefix sibling (/tmp/out vs /tmp/out-evil) fails', () => {
  assert.equal(pathUnderOutputDir('/tmp/out', '/tmp/out-evil/x'), false);
});

test('pathUnderOutputDir: .. segment fails on path and on root', () => {
  assert.equal(pathUnderOutputDir(ROOT, `${ROOT}/../evil`), false);
  assert.equal(pathUnderOutputDir('/repo/../x', '/repo/../x/a'), false);
  assert.equal(normalizeOutputDirRoot('/repo/../x'), null);
});

test('pathUnderOutputDir: backslash fails on path and on root', () => {
  assert.equal(pathUnderOutputDir(ROOT, `${ROOT}\\evil`), false);
  assert.equal(normalizeOutputDirRoot('/repo\\out'), null);
});

test('pathUnderOutputDir: /./ collapses; // is deliberately allowed when under root', () => {
  assert.equal(pathUnderOutputDir(ROOT, `${ROOT}/./findings.json`), true);
  assert.equal(pathUnderOutputDir(`${ROOT}/./`, `${ROOT}/findings.json`), true);
  assert.equal(pathUnderOutputDir(ROOT, `${ROOT}//findings.json`), true);
});

test('pathUnderOutputDir: empty / null / undefined path is false (no TypeError)', () => {
  assert.equal(pathUnderOutputDir(ROOT, ''), false);
  assert.equal(pathUnderOutputDir(ROOT, null), false);
  assert.equal(pathUnderOutputDir(ROOT, undefined), false);
});

test('pathUnderOutputDir: relative or missing root is false', () => {
  assert.equal(pathUnderOutputDir('.code-gauntlet', '.code-gauntlet/x'), false);
  assert.equal(pathUnderOutputDir('', '/x'), false);
  assert.equal(pathUnderOutputDir(null, '/x'), false);
});

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
