import { test } from 'node:test';
import assert from 'node:assert/strict';
import { build, buildFromSources, BUNDLE_MAX_BYTES, stripInertLines } from '../build.js';

test('bundle smoke: builds a compilable workflow within the host cap', () => {
  const bundle = build();
  const lines = bundle.split('\n');
  assert.match(lines[0], /^export const meta\b/);
  assert.match(lines[1], /^const PIPELINE_VERSION\b/);
  assert.ok(Buffer.byteLength(bundle, 'utf8') <= BUNDLE_MAX_BYTES);
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const body = bundle.replace(/^export const meta\b/m, 'const meta');
  assert.doesNotThrow(() => new AsyncFunction(body));
});

test('dependency cycle refuses the build', () => {
  const sources = new Map([
    ['pipeline_entry.js', "import { a } from './a.js';"],
    ['a.js', "import { b } from './b.js';"],
    ['b.js', "import { a } from './a.js';"],
  ]);
  assert.throws(() => buildFromSources(sources), /a\.js -> b\.js -> a\.js/);
});

test('build refuses duplicate top-level declarations across source modules', () => {
  const sources = new Map([
    ['pipeline_entry.js', 'import { a } from \'./a.js\';\nimport { b } from \'./b.js\';'],
    ['a.js', 'function helper() {}'],
    ['b.js', 'function helper() {}'],
  ]);
  assert.throws(() => buildFromSources(sources), /top-level identifier collision[\s\S]*'helper'/);
});

test('inert-line stripping keeps template literal contents byte-for-byte', () => {
  const body = 'const template = `first\n\n// not a comment\nlast`;\n// comment\n\nconst after = 1;';
  const result = stripInertLines(body);
  assert.equal(result.text, 'const template = `first\n\n// not a comment\nlast`;\nconst after = 1;');
  assert.deepEqual(result.dropped, ['// comment', '']);
  assert.deepEqual(result.kept, ['', '// not a comment']);
});
