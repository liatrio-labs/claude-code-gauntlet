// check_coverage_presence.mjs — require every coverage_scope include (minus exempt)
// to appear as SF: in the lcov the gated suite just wrote. Stdlib Node only.
//
// Single-level globs on purpose; a subdirectory under workflows/src/ is a visited
// design choice, not an accident. Exempt frees presence only — never filter lcov.
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, '../../..');
const scope = JSON.parse(readFileSync(path.join(here, 'coverage_scope.json'), 'utf8'));

const lcovPath = process.argv[2];
if (!lcovPath) {
  process.stderr.write('usage: node check_coverage_presence.mjs <lcov-path>\n');
  process.exit(2);
}

function gitLsFiles() {
  const r = spawnSync('git', ['ls-files', '-z'], { cwd: REPO, encoding: 'buffer' });
  if (r.status !== 0) {
    process.stderr.write(r.stderr?.toString() || 'git ls-files failed\n');
    process.exit(2);
  }
  return r.stdout.toString('utf8').split('\0').filter(Boolean);
}

/** Single-level only: 'a/b/*.js' or exact 'a/b/c.js'. Rejects '**' and nested matches. */
function matchInclude(pattern, file) {
  if (!pattern.includes('*')) return file === pattern;
  const parts = pattern.split('/');
  const fileParts = file.split('/');
  if (parts.length !== fileParts.length) return false;
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    const f = fileParts[i];
    if (p === '*') continue;
    if (p.includes('*')) {
      // one * segment like *.js
      const [pre, post] = p.split('*');
      if (p.indexOf('*') !== p.lastIndexOf('*')) return false; // multi-star: refuse
      if (!f.startsWith(pre) || !f.endsWith(post)) return false;
      continue;
    }
    if (p !== f) return false;
  }
  return true;
}

function expectedSet() {
  const tracked = gitLsFiles();
  const exempt = new Set(scope.exempt);
  const out = new Set();
  for (const pattern of scope.includes) {
    for (const file of tracked) {
      if (matchInclude(pattern, file) && !exempt.has(file)) out.add(file);
    }
  }
  return out;
}

function repoRelative(sf) {
  const abs = path.isAbsolute(sf) ? sf : path.resolve(process.cwd(), sf);
  const rel = path.relative(REPO, abs);
  if (!rel || rel.startsWith('..') || path.isAbsolute(rel)) {
    throw new Error(`SF path outside repo: ${sf}`);
  }
  return rel.split(path.sep).join('/');
}

function actualSet(lcovText) {
  const out = new Set();
  for (const line of lcovText.split(/\r?\n/)) {
    if (!line.startsWith('SF:')) continue;
    out.add(repoRelative(line.slice(3)));
  }
  return out;
}

const expected = expectedSet();
const actual = actualSet(readFileSync(lcovPath, 'utf8'));
const exempt = new Set(scope.exempt);
const missing = [...expected].filter((f) => !actual.has(f)).sort();
const unexpected = [...actual].filter((f) => !expected.has(f) && !exempt.has(f)).sort();
if (missing.length || unexpected.length) {
  if (missing.length) process.stderr.write(`missing from lcov:\n${missing.map((m) => `  ${m}`).join('\n')}\n`);
  if (unexpected.length) process.stderr.write(`unexpected in lcov:\n${unexpected.map((m) => `  ${m}`).join('\n')}\n`);
  process.exit(1);
}
process.exit(0);
