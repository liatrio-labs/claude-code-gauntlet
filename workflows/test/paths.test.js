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
  assert.equal(pathUnderRoot('/repo', '//repo/src/a.js'), true);
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
  { name: 'dot double slash file stays relative', file: './/x', expected: { file: 'x' } },
  { name: 'dot double slash root-like relative path is trusted', file: './/Users/lee/x.js', expected: { file: 'Users/lee/x.js' } },
  { name: 'dot double slash source path is normalized', file: './/src/a.js', expected: { file: 'src/a.js' } },
  { name: 'dot double slash root is rejected', file: './/', expected: { reason: 'file path does not name a repository file' } },
  { name: 'relative interior dot slash is collapsed', file: 'scripts/./gauntlet/proc.py', expected: { file: 'scripts/gauntlet/proc.py' } },
  { name: 'relative repeated slashes are collapsed', file: 'scripts//gauntlet///proc.py', expected: { file: 'scripts/gauntlet/proc.py' } },
  { name: 'relative double slash is collapsed', file: 'a//b', expected: { file: 'a/b' } },
  { name: 'relative trailing slash is stripped', file: 'scripts/gauntlet/', expected: { file: 'scripts/gauntlet' } },
  { name: 'relative trailing dot is stripped', file: 'scripts/gauntlet/.', expected: { file: 'scripts/gauntlet' } },
  { name: 'root-prefixed relative path is preserved', root: '/Users/lee/repo', file: 'Users/lee/repo/a.js', expected: { file: 'Users/lee/repo/a.js' } },
  { name: 'app relative path under app root is preserved', root: '/app', file: 'app/models/user.rb', expected: { file: 'app/models/user.rb' } },
  { name: 'src relative path under src root is preserved', root: '/src', file: 'src/index.js', expected: { file: 'src/index.js' } },
  { name: 'packages relative path under src root is preserved', root: '/src', file: 'packages/web/src/index.js', expected: { file: 'packages/web/src/index.js' } },
  { name: 'relative path does not require an absolute root', root: 'repo', file: 'src/index.js', expected: { file: 'src/index.js' } },
  { name: 'dynamic route is accepted', file: 'app/[id]/page.tsx', expected: { file: 'app/[id]/page.tsx' } },
  { name: 'group and catch-all route is accepted', file: 'app/(group)/[...slug]/page.tsx', expected: { file: 'app/(group)/[...slug]/page.tsx' } },
  { name: 'filename with spaces is accepted', file: 'my file.js', expected: { file: 'my file.js' } },
  { name: 'tilde filename is accepted', file: '~notes/x.md', expected: { file: '~notes/x.md' } },
  { name: 'home anchor is rejected', file: '~/x.md', expected: { reason: 'file path starts with a home anchor' } },
  { name: 'bare home anchor is rejected', file: '~', expected: { reason: 'file path starts with a home anchor' } },
  { name: 'home anchor after dot slash is rejected', file: './~/x.md', expected: { reason: 'file path starts with a home anchor' } },
  { name: 'interior tilde segment is accepted', file: 'docs/~/x.md', expected: { file: 'docs/~/x.md' } },
  { name: 'Unicode filename is accepted', file: 'src/café.js', expected: { file: 'src/café.js' } },
  ...['docs/می‌خواهم.md', 'img/❤️.png', 'a/👨‍💻.md', 'a/e\u0301.md'].map((file) => ({
    name: `interior shaping characters are accepted in ${file}`, file, expected: { file },
  })),
  ...[0x2800, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x200B, 0x034F, 0xFE0F, 0x0301, 0x20DD].flatMap((code) => {
    const invisible = String.fromCodePoint(code);
    return [
      { name: `segment-leading U+${code.toString(16)} is rejected`, file: `a/${invisible}b`, expected: { reason: 'file path contains a disallowed character' } },
      { name: `interior U+${code.toString(16)} is accepted`, file: `a/b${invisible}c`, expected: { file: `a/b${invisible}c` } },
    ];
  }),
  ...[0x0301, 0x20DD].map((code) => ({
    name: `combining U+${code.toString(16)} host-like prefix is rejected`,
    file: `${String.fromCodePoint(code)}/Users/lee/x.js`,
    expected: { reason: 'file path contains a disallowed character' },
  })),
  { name: 'Braille blank host-like prefix is rejected', file: '\u2800/Users/lee/x', expected: { reason: 'file path contains a disallowed character' } },
  ...[0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x2028, 0x2029].flatMap((code) => {
    const control = String.fromCodePoint(code);
    return [
      { name: `leading U+${code.toString(16)} control is rejected`, file: `${control}/x`, expected: { reason: 'file path contains a disallowed character' } },
      { name: `interior U+${code.toString(16)} control is rejected`, file: `a${control}b`, expected: { reason: 'file path contains a disallowed character' } },
    ];
  }),
  { name: 'absolute path under root becomes relative', file: `${REPO_ROOT}/scripts/gauntlet/proc.py`, expected: { file: 'scripts/gauntlet/proc.py' } },
  { name: 'absolute path under slash root becomes relative', root: '/', file: '/proc.py', expected: { file: 'proc.py' } },
  { name: 'absolute dot slash is normalized', file: `${REPO_ROOT}/./scripts/gauntlet/proc.py`, expected: { file: 'scripts/gauntlet/proc.py' } },
  { name: 'absolute repeated slashes are collapsed', file: `${REPO_ROOT}//scripts///proc.py/`, expected: { file: 'scripts/proc.py' } },
  { name: 'absolute path equal to root is rejected', file: REPO_ROOT, expected: { reason: 'absolute file path resolves to repoRoot' } },
  { name: 'absolute path outside root is rejected', file: '/private/tmp/wt-410-evil/proc.py', expected: { reason: 'absolute file path is outside repoRoot' } },
  { name: 'relative traversal is rejected', file: '../proc.py', expected: { reason: 'file path contains a .. segment' } },
  { name: 'absolute traversal is rejected', file: `${REPO_ROOT}/../proc.py`, expected: { reason: 'file path contains a .. segment' } },
  { name: 'interior backslash path is accepted', file: 'scripts\\proc.py', expected: { file: 'scripts\\proc.py' } },
  { name: 'invalid root is rejected for an absolute file', root: 'repo', file: '/repo/proc.py', expected: { reason: 'repoRoot must be an absolute path without .. segments or backslashes' } },
  { name: 'empty file is rejected', file: '', expected: { reason: 'file must be a non-empty string' } },
  { name: 'non-string file is rejected', file: null, expected: { reason: 'file must be a non-empty string' } },
  { name: 'dot file is rejected', file: '.', expected: { reason: 'file path does not name a repository file' } },
  { name: 'dot slash file is rejected', file: './', expected: { reason: 'file path does not name a repository file' } },
  { name: 'file URI with triple slash is rejected', file: 'file:///x', expected: { reason: 'file path starts with a URI scheme or drive letter' } },
  { name: 'file URI with single slash is rejected', file: 'file:/Users/a', expected: { reason: 'file path starts with a URI scheme or drive letter' } },
  { name: 'web URI is rejected', file: 'https://x/a', expected: { reason: 'file path starts with a URI scheme or drive letter' } },
  { name: 'leading whitespace is rejected', file: ' x', expected: { reason: 'file path has leading or trailing whitespace' } },
  { name: 'leading newline host path is rejected', file: '\n/Users/x', expected: { reason: 'file path contains a disallowed character' } },
  { name: 'whitespace segment after dot slash is rejected', file: './ /Users', expected: { reason: 'file path has leading or trailing whitespace' } },
  { name: 'interior ASCII control is rejected', file: 'src/a\tb.js', expected: { reason: 'file path contains a disallowed character' } },
  { name: 'trailing space is rejected', file: 'src/a.js ', expected: { reason: 'file path has leading or trailing whitespace' } },
  { name: 'leading drive letter is rejected', file: 'C:/x', expected: { reason: 'file path starts with a URI scheme or drive letter' } },
  { name: 'bare drive letter is rejected', file: 'C:', expected: { reason: 'file path starts with a URI scheme or drive letter' } },
  { name: 'leading drive letter after dot slash is rejected', file: './C:/file.js', expected: { reason: 'file path starts with a URI scheme or drive letter' } },
  { name: 'drive-like colon filename is accepted', file: './C:file.js', expected: { file: 'C:file.js' } },
  { name: 'dotted URI scheme is rejected', file: 'git.foo+bar-baz:/x', expected: { reason: 'file path starts with a URI scheme or drive letter' } },
  ...['Makefile:build', 'Dockerfile:12:5-14:2', 'Makefile:12,14'].map((file) => ({
    name: `colon filename ${file} is accepted`, file, expected: { file },
  })),
  { name: 'colon after first segment is accepted', file: 'src/a:b.js', expected: { file: 'src/a:b.js' } },
  ...['README.md:10', 'README.md:10-20', 'README.md:10:5', 'README.md:L10', 'src/a.js:L3-L9'].map((file) => ({
    name: `location suffix ${file} is preserved`, file, expected: { file },
  })),
  { name: 'dotted filename suffix README.md:x stays a literal relative path', file: 'README.md:x', expected: { file: 'README.md:x' } },
  { name: 'absolute column location becomes relative with its suffix', file: `${REPO_ROOT}/src/a.js:10:5`, expected: { file: 'src/a.js:10:5' } },
  { name: 'location dir/:12/ is normalized', file: 'dir/:12/', expected: { file: 'dir:12' } },
  { name: 'location dir/./:L3/. is normalized', file: 'dir/./:L3/.', expected: { file: 'dir:L3' } },
  { name: 'location ./:4/ without a file is rejected', file: './:4/', expected: { reason: 'file path does not name a repository file' } },
  { name: 'location without a path is rejected', file: ':10', expected: { reason: 'file path does not name a repository file' } },
  { name: 'absolute path with colon after root is rejected', file: `${REPO_ROOT}/file:/a.js`, expected: { reason: 'file path starts with a URI scheme or drive letter' } },
  { name: 'NEL prefix is rejected', file: '\u0085/x', expected: { reason: 'file path contains a disallowed character' } },
  { name: 'zero-width prefix is rejected', file: '\u200B/x', expected: { reason: 'file path contains a disallowed character' } },
  { name: 'Hangul filler prefix is rejected', file: '\u3164/x', expected: { reason: 'file path contains a disallowed character' } },
  { name: 'bidi prefix is rejected', file: '\u202E/x', expected: { reason: 'file path contains a disallowed character' } },
  { name: 'line-separator prefix is rejected', file: '\u2028src/a.js', expected: { reason: 'file path contains a disallowed character' } },
  { name: 'paragraph-separator prefix is rejected', file: '\u2029/x', expected: { reason: 'file path contains a disallowed character' } },
  { name: 'interior backslash is accepted', file: 'a\\b', expected: { file: 'a\\b' } },
  { name: 'escaped POSIX unit name is accepted', file: String.raw`units/foo\x2dbar.service`, expected: { file: String.raw`units/foo\x2dbar.service` } },
  { name: 'absolute interior backslash is accepted', file: `${REPO_ROOT}/units/foo\\x2dbar.service`, expected: { file: String.raw`units/foo\x2dbar.service` } },
  { name: 'UNC backslash is rejected', file: String.raw`\\host\share\x`, expected: { reason: 'file path starts with a backslash' } },
  { name: 'rooted Windows backslash is rejected', file: String.raw`\root\x`, expected: { reason: 'file path starts with a backslash' } },
  { name: 'Windows drive backslash is rejected', file: String.raw`C:\root\x`, expected: { reason: 'file path starts with a URI scheme or drive letter' } },
  { name: 'traversal is rejected', file: '../x', expected: { reason: 'file path contains a .. segment' } },
];
for (const c of REPO_RELATIVE_CASES) {
  test(`repoRelativeFindingPath: ${c.name}`, () => {
    assert.deepEqual(repoRelativeFindingPath(c.root ?? REPO_ROOT, c.file), c.expected);
  });
}

test('repoRelativeFindingPath is idempotent over every table row', () => {
  for (const c of [...REPO_RELATIVE_CASES, { file: 'dir/:1/:2' }, { file: 'dir/:1/:2/:3/:4' }]) {
    const root = c.root ?? REPO_ROOT;
    const once = repoRelativeFindingPath(root, c.file);
    assert.deepEqual(repoRelativeFindingPath(root, once.file ?? c.file), once, `idempotence: ${c.file}`);
  }
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
