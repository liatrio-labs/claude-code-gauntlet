// build.test.js — source ordering, bundle guards, and comment stripping.
import test from 'node:test';
import assert from 'node:assert/strict';
import { stripTypeScriptTypes } from 'node:module';
import {
  BUNDLE_MAX_BYTES,
  BUNDLE_HEADROOM,
  WORKFLOW_SCRIPT_CAP,
  build,
  buildFromSources,
  checkBundleSize,
  checkRawLineTerminators,
  detectTopLevelCollisions,
  moduleOrder,
  stripInertLines,
  unsafeImports,
} from '../build.js';

const sourceMap = (entries) => new Map(entries);

test('moduleOrder emits dependencies before their importers', () => {
  const sources = sourceMap([
    ['pipeline_entry.js', "import { middle } from './middle.js';"],
    ['middle.js', "import { leaf } from './leaf.js';"],
    ['leaf.js', 'const leaf = 1;'],
  ]);
  assert.deepEqual(moduleOrder(sources), ['leaf.js', 'middle.js', 'pipeline_entry.js']);
});

test('moduleOrder emits pipeline_entry.js last', () => {
  const sources = sourceMap([
    ['pipeline_entry.js', "import { dependency } from './dependency.js';"],
    ['dependency.js', 'const dependency = 1;'],
  ]);
  assert.equal(moduleOrder(sources).at(-1), 'pipeline_entry.js');
});

test('moduleOrder follows imports in source-line order', () => {
  const sources = sourceMap([
    ['pipeline_entry.js', "import { second } from './second.js';\nimport { first } from './first.js';"],
    ['first.js', 'const first = 1;'],
    ['second.js', 'const second = 1;'],
  ]);
  assert.deepEqual(moduleOrder(sources), ['second.js', 'first.js', 'pipeline_entry.js']);
});

test('moduleOrder names the path when imports form a cycle', () => {
  const sources = sourceMap([
    ['pipeline_entry.js', "import { a } from './a.js';"],
    ['a.js', "import { b } from './b.js';"],
    ['b.js', "import { a } from './a.js';"],
  ]);
  assert.throws(() => moduleOrder(sources), /a\.js -> b\.js -> a\.js/);
});

test('moduleOrder names files unreachable from pipeline_entry.js', () => {
  const sources = sourceMap([
    ['pipeline_entry.js', 'const entry = 1;'],
    ['orphan.js', 'const orphan = 1;'],
  ]);
  assert.throws(() => moduleOrder(sources), /unreachable.*orphan\.js/);
});

test('moduleOrder names a missing sibling import and its source line', () => {
  const sources = sourceMap([
    ['pipeline_entry.js', "const entry = 1;\nimport { missing } from './missing.js';"],
  ]);
  assert.throws(
    () => moduleOrder(sources),
    /src\/pipeline_entry\.js:2 imports missing sibling '\.\/missing\.js'/,
  );
});

test('moduleOrder reports unsafe imports with file and line', () => {
  const sources = sourceMap([
    ['pipeline_entry.js', "const entry = 1;\nimport { x } from 'lodash';"],
  ]);
  assert.throws(() => moduleOrder(sources), /src\/pipeline_entry\.js:2:.*lodash/);
});

test('detectTopLevelCollisions catches top-level names and ignores nested declarations', () => {
  const text = [
    'const DUP = 1;',
    'function nested() {',
    '  const DUP = 2;',
    '}',
    'let DUP = 3;',
    'function widget() {}',
    'class widget {}',
  ].join('\n');
  assert.deepEqual(detectTopLevelCollisions(text), [
    { name: 'DUP', lines: [1, 5] },
    { name: 'widget', lines: [6, 7] },
  ]);
});

test('unsafeImports accepts sibling imports and ignores dynamic imports', () => {
  const source = [
    "import { value } from './value.js';",
    "import registry from './registry.js';",
    'const here = import.meta.url;',
    "const lazy = await import('./lazy.js');",
  ].join('\n');
  assert.deepEqual(unsafeImports(source), []);
});

test('unsafeImports rejects import lines that cannot be stripped safely', () => {
  const cases = [
    ["import './sideEffect.js';", null, /single-line/],
    ["import { inspect } from 'node:util';", 'node:util', /not relative/],
    ["import lodash from 'lodash';", 'lodash', /not relative/],
    ["import { x } from '../outside.js';", '../outside.js', /not relative/],
    ["import {\n  thing,\n} from './registry.js';", null, /single-line/],
  ];
  for (const [source, specifier, reason] of cases) {
    const [violation] = unsafeImports(source);
    assert.equal(violation.specifier, specifier);
    assert.match(violation.reason, reason);
  }
});

test('the real bundle builds without top-level collisions', () => {
  assert.deepEqual(detectTopLevelCollisions(build()), []);
});

test('stripInertLines drops only parser-proven inert candidates', () => {
  const body = [
    'const code = 1;', '   ', '\t\t', '', '// code comment',
    'const expression = `${1', '  // expression comment', '  + 2}`;',
    '/// slash comment', 'class Example {', '  // class comment', '}',
    'const object = {', '  // object comment', '  value: 1,', '};',
    'const arrow = () => {', '  // arrow comment', '  return 1;', '};',
    'const trailing = code; // trailing comment', "const quoted = '//';",
    'const url = "http://example.test";', 'const templateInterior = `template',
    '  // template interior', '', '   ', 'end`;', 'const block = /* block',
    '  // block interior', '  // closing */ 1;', "const continued = 'start\\",
    '  // middle\\', "  // tail';", 'const templateClosing = `open',
    '  // closes`', 'const nested = `${`inner', '  // nested closes`}', 'outer`;',
  ].join('\n');
  const lines = body.split('\n');
  const droppedIndexes = new Set([1, 2, 3, 4, 6, 8, 10, 13, 17]);
  const result = stripInertLines(body, 'synthetic.js');

  assert.deepEqual(result.dropped, [
    '   ', '\t\t', '', '// code comment', '  // expression comment',
    '/// slash comment', '  // class comment', '  // object comment', '  // arrow comment',
  ]);
  assert.deepEqual(result.kept, [
    '  // template interior', '', '   ', '  // block interior',
    '  // closing */ 1;', '  // middle\\', "  // tail';", '  // closes`',
    '  // nested closes`}',
  ]);
  assert.equal(result.text, lines.filter((_, index) => !droppedIndexes.has(index)).join('\n'));
});

test('stripInertLines names a module that is not parseable', () => {
  assert.throws(
    () => stripInertLines('const = 1;', 'synthetic-invalid.js'),
    /synthetic-invalid\.js is not parseable as an async function body/,
  );
});

test('checkBundleSize enforces the cap and measures UTF-8 bytes', () => {
  assert.equal(WORKFLOW_SCRIPT_CAP, 524_288);
  assert.equal(BUNDLE_HEADROOM, 65_536);
  assert.equal(BUNDLE_MAX_BYTES, 458_752);
  assert.doesNotThrow(() => checkBundleSize('a'.repeat(BUNDLE_MAX_BYTES)));
  assert.throws(() => checkBundleSize('a'.repeat(BUNDLE_MAX_BYTES + 1)), /bundle is 458753 bytes/);
  const multibyte = 'é'.repeat(229_377);
  assert.ok(multibyte.length < BUNDLE_MAX_BYTES);
  assert.throws(() => checkBundleSize(multibyte), /bundle is 458754 bytes/);
});

test('build wires the size limit to the generated bundle', () => {
  const bytes = Buffer.byteLength(build(), 'utf8');
  assert.throws(
    () => build({ maxBytes: 1000 }),
    (error) => error.message.includes(`bundle is ${bytes} bytes`)
      && error.message.includes('limit is 1000 bytes'),
  );
});

test('buildFromSources defaults to the headroom-adjusted cap', () => {
  const sources = sourceMap([
    ['pipeline_entry.js', "import { x } from './big.js';"],
    ['big.js', `const x = '${'a'.repeat(460_000)}';\n`],
  ]);
  assert.throws(() => buildFromSources(sources), /limit is 458752 bytes/);
});

test('the real bundle preserves only generated comments after inert-line stripping', () => {
  const bundle = build();
  const generated = '// GENERATED by workflows/build.js — do not edit by hand.';
  const lines = bundle.split('\n');
  const body = lines.slice(lines.indexOf(generated)).join('\n');
  const result = stripInertLines(body, 'pipeline.js');
  const separators = result.dropped.filter((line) => /^\/\/ --- \S+ ---$/.test(line));
  assert.equal(result.dropped.length, separators.length + 1);
  assert.ok(Buffer.byteLength(bundle, 'utf8') <= BUNDLE_MAX_BYTES);
});

test('independent parser output agrees for stripped and unstripped bundles', () => {
  const wrap = (bundle) => `async function __w(){${bundle.replace(/^export const meta\b/m, 'const meta')}}`;
  const unstripped = build({ dropComments: false, maxBytes: Infinity });
  const stripped = build();
  assert.equal(
    stripTypeScriptTypes(wrap(unstripped), { mode: 'transform' }),
    stripTypeScriptTypes(wrap(stripped), { mode: 'transform' }),
  );
  const fixture = ['const literal = `one', '  // inside', 'two`;', ''].join('\n');
  assert.equal(
    stripTypeScriptTypes(wrap(fixture), { mode: 'transform' }),
    stripTypeScriptTypes(wrap(stripInertLines(fixture, 'literal-fixture.js').text), { mode: 'transform' }),
  );
});

test('checkRawLineTerminators names each forbidden code point and its line', () => {
  const files = ['clean.js', 'bad.js'];
  const sources = sourceMap([
    ['clean.js', 'const clean = 1;'],
    ['bad.js', 'const first = 1;\r\nconst second = 2;\u2028const third = 3;\u2029'],
  ]);
  assert.throws(
    () => checkRawLineTerminators(files, sources),
    (error) => ['src/bad.js:1: raw U+000D', 'src/bad.js:2: raw U+2028', 'src/bad.js:2: raw U+2029']
      .every((detail) => error.message.includes(detail))
      && !error.message.includes('src/clean.js'),
  );
});

test('buildFromSources checks raw line terminators before module ordering', () => {
  const sources = sourceMap([['bad.js', 'const first = 1;\r\nconst second = 2;']]);
  assert.throws(() => buildFromSources(sources), /src\/bad\.js:1: raw U\+000D/);
});
