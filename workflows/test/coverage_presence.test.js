import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const testDir = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(testDir, '../..');
const tool = path.join(testDir, 'tools/check_coverage_presence.mjs');
const scopePath = path.join(testDir, 'tools/coverage_scope.json');

function runTool(lcovPath, overrideScopePath) {
  const args = [tool, lcovPath];
  if (overrideScopePath) args.push(overrideScopePath);
  return spawnSync(process.execPath, args, { cwd: repo, encoding: 'utf8' });
}

function trackedFiles() {
  const result = spawnSync('git', ['ls-files', '-z'], { cwd: repo, encoding: 'buffer' });
  assert.equal(result.status, 0, result.stderr?.toString());
  return result.stdout.toString('utf8').split('\0').filter(Boolean);
}

function matchesScope(pattern, file) {
  if (!pattern.includes('*')) return file === pattern;
  const parts = pattern.split('/');
  const fileParts = file.split('/');
  if (parts.length !== fileParts.length) return false;
  return parts.every((part, index) => {
    if (!part.includes('*')) return part === fileParts[index];
    const [prefix, suffix] = part.split('*');
    return part.indexOf('*') === part.lastIndexOf('*')
      && fileParts[index].startsWith(prefix)
      && fileParts[index].endsWith(suffix);
  });
}

function expectedFiles(scope) {
  const exempt = new Set(scope.exempt);
  return trackedFiles()
    .filter((file) => scope.includes.some((pattern) => matchesScope(pattern, file)))
    .filter((file) => !exempt.has(file))
    .sort();
}

function withTempDir(fn) {
  const dir = mkdtempSync(path.join(tmpdir(), 'coverage-presence-'));
  try {
    fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test('accepts lcov containing every file expanded from the production scope', () => {
  withTempDir((dir) => {
    const scope = JSON.parse(readFileSync(scopePath, 'utf8'));
    const expected = expectedFiles(scope);
    assert.ok(expected.length > 0, 'production scope fixture must expand to tracked files');
    const lcovPath = path.join(dir, 'lcov.info');
    writeFileSync(lcovPath, `${expected.map((file) => `SF:${file}`).join('\n')}\n`);

    const result = runTool(lcovPath);

    assert.equal(result.status, 0, result.stderr);
  });
});

test('exits 1 when a required source file is missing from lcov', () => {
  withTempDir((dir) => {
    const scope = JSON.parse(readFileSync(scopePath, 'utf8'));
    const expected = expectedFiles(scope);
    assert.ok(expected.length > 1, 'missing-file fixture needs at least two expected files');
    const omitted = expected[0];
    const lcovPath = path.join(dir, 'lcov.info');
    writeFileSync(lcovPath, `${expected.slice(1).map((file) => `SF:${file}`).join('\n')}\n`);

    const result = runTool(lcovPath);

    assert.equal(result.status, 1, result.stderr);
    assert.match(result.stderr, /missing from lcov:/);
    assert.match(result.stderr, new RegExp(omitted.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  });
});

test('exits 2 when scope includes expand to no tracked files', () => {
  withTempDir((dir) => {
    const lcovPath = path.join(dir, 'lcov.info');
    const emptyScopePath = path.join(dir, 'coverage_scope.json');
    writeFileSync(lcovPath, '');
    writeFileSync(emptyScopePath, JSON.stringify({
      includes: ['workflows/src/no_such_pattern_zzz.js'],
      exempt: [],
    }));

    const result = runTool(lcovPath, emptyScopePath);

    assert.equal(result.status, 2, result.stderr);
    assert.match(result.stderr, /includes matched no tracked files/);
    assert.match(result.stderr, /workflows\/src\/no_such_pattern_zzz\.js/);
  });
});

test('exits 2 when an exempt entry is not a tracked file', () => {
  withTempDir((dir) => {
    const scope = JSON.parse(readFileSync(scopePath, 'utf8'));
    const expected = expectedFiles(scope);
    const lcovPath = path.join(dir, 'lcov.info');
    const staleScopePath = path.join(dir, 'coverage_scope.json');
    writeFileSync(lcovPath, `${expected.map((file) => `SF:${file}`).join('\n')}\n`);
    writeFileSync(staleScopePath, JSON.stringify({
      includes: scope.includes,
      exempt: ['workflows/src/renamed_away_pipeline_entry.js'],
    }));

    const result = runTool(lcovPath, staleScopePath);

    assert.equal(result.status, 2, result.stderr);
    assert.match(result.stderr, /exempt entries are not git-tracked:/);
    assert.match(result.stderr, /workflows\/src\/renamed_away_pipeline_entry\.js/);
  });
});
