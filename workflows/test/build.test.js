// build.test.js — the bundler's top-level identifier-collision detector and ORDER
// completeness guard: the build-time guards that turn a would-be runtime
// `Identifier 'X' has already been declared` SyntaxError (the SEVERITY_ORDER
// collision the live smoke run hit) or a silently incomplete bundle into loud
// build failures naming the duplicate or the missing file — plus the import guard
// that refuses any specifier strip() cannot safely drop.
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
  checkUnsafeImports,
  detectTopLevelCollisions,
  orderMismatches,
  stripInertLines,
  unsafeImports,
} from '../build.js';

test('detectTopLevelCollisions flags a duplicated top-level declaration', () => {
  const text = [
    'const DUP = 1;',
    'function f() {',
    '  const DUP = 2;', // indented → nested scope, NOT a top-level collision
    '}',
    'let DUP = 3;', // top-level again → collides with line 1
  ].join('\n');
  const collisions = detectTopLevelCollisions(text);
  assert.equal(collisions.length, 1);
  assert.equal(collisions[0].name, 'DUP');
  assert.deepEqual(collisions[0].lines, [1, 5]); // the two top-level lines, not the nested one
});

test('detectTopLevelCollisions ignores nested (indented) redeclarations', () => {
  const text = [
    'const only = 1;',
    'function g() {',
    '  const only = 2;', // shadows in a nested scope — legal, not a collision
    '}',
  ].join('\n');
  assert.deepEqual(detectTopLevelCollisions(text), []);
});

test('detectTopLevelCollisions spans const/let/var/function/class', () => {
  const text = ['function widget() {}', 'class widget {}'].join('\n');
  const collisions = detectTopLevelCollisions(text);
  assert.equal(collisions.length, 1);
  assert.equal(collisions[0].name, 'widget');
});

test('the real bundle build() produces no top-level collisions', () => {
  // build() itself throws on collision; assert it succeeds AND its output is clean.
  const bundle = build();
  assert.deepEqual(detectTopLevelCollisions(bundle), []);
});

test('orderMismatches flags a file on disk that is missing from ORDER', () => {
  const result = orderMismatches(['a.js', 'b.js'], ['b.js', 'a.js', 'stray.js']);
  assert.deepEqual(result.missingFromOrder, ['stray.js']);
  assert.deepEqual(result.missingFromDisk, []);
});

test('orderMismatches flags a name in ORDER that is missing from disk', () => {
  const result = orderMismatches(['a.js', 'gone.js', 'b.js'], ['b.js', 'a.js']);
  assert.deepEqual(result.missingFromOrder, []);
  assert.deepEqual(result.missingFromDisk, ['gone.js']);
});

test('orderMismatches reports both directions at once, sorted', () => {
  // Input order is deliberately unsorted so the test proves sorting of returns only.
  const result = orderMismatches(
    ['z.js', 'a.js', 'orphan-order.js'],
    ['stray-b.js', 'a.js', 'stray-a.js', 'z.js'],
  );
  assert.deepEqual(result.missingFromOrder, ['stray-a.js', 'stray-b.js']);
  assert.deepEqual(result.missingFromDisk, ['orphan-order.js']);
});

test('orderMismatches returns empty arrays when sets are equal (including both empty)', () => {
  assert.deepEqual(orderMismatches(['a.js', 'b.js'], ['b.js', 'a.js']), {
    missingFromOrder: [],
    missingFromDisk: [],
    duplicatedInOrder: [],
  });
  assert.deepEqual(orderMismatches([], []), {
    missingFromOrder: [],
    missingFromDisk: [],
    duplicatedInOrder: [],
  });
});

test('orderMismatches flags a name listed twice in ORDER', () => {
  // Set equality is blind to this: the repeat collapses, so both directions are
  // clean while the bundler would concatenate the module twice.
  const result = orderMismatches(['a.js', 'b.js', 'a.js'], ['b.js', 'a.js']);
  assert.deepEqual(result.duplicatedInOrder, ['a.js']);
  assert.deepEqual(result.missingFromOrder, []);
  assert.deepEqual(result.missingFromDisk, []);
});

test('orderMismatches reports each duplicate once, sorted, beside the other directions', () => {
  const result = orderMismatches(
    ['z.js', 'a.js', 'z.js', 'a.js', 'z.js', 'orphan-order.js'],
    ['a.js', 'stray.js', 'z.js'],
  );
  assert.deepEqual(result.duplicatedInOrder, ['a.js', 'z.js']); // deduped and sorted
  assert.deepEqual(result.missingFromOrder, ['stray.js']);
  assert.deepEqual(result.missingFromDisk, ['orphan-order.js']);
});

// unsafeImports — only a relative sibling import may be stripped. A `node:`/bare
// specifier inlines nothing, so stripping it ships an undefined reference that
// lint, the bundle-fresh check and the suites all call clean, and that throws on
// the first live dispatch (the sandbox has no Node builtins).

test('unsafeImports accepts single-line relative sibling imports', () => {
  const source = [
    "import { dedupById } from './findingDedup.js';",
    "import registry from './registry.js';",
    'const x = 1;',
  ].join('\n');
  assert.deepEqual(unsafeImports(source), []);
});

test('unsafeImports flags a side-effect import even when the specifier is relative', () => {
  // strip() matches only `import … from …`, so this line ships into the bundle
  // verbatim — unsafe for the same reason a multi-line import is, despite './'.
  const violations = unsafeImports("import './sideEffect.js';");
  assert.equal(violations.length, 1);
  assert.equal(violations[0].specifier, null);
  assert.match(violations[0].reason, /single-line `from` clause/);
});

test('unsafeImports flags a node: builtin import with file line and specifier', () => {
  const violations = unsafeImports(['const a = 1;', "import { inspect } from 'node:util';"].join('\n'));
  assert.equal(violations.length, 1);
  assert.equal(violations[0].line, 2);
  assert.equal(violations[0].specifier, 'node:util');
  assert.match(violations[0].reason, /undefined reference/);
});

test('unsafeImports flags bare and parent-relative specifiers', () => {
  // '../' is not a sibling: ORDER inlines nothing for it, so it is as unsafe as 'lodash'.
  const source = ["import lodash from 'lodash';", "import { x } from '../outside.js';"].join('\n');
  assert.deepEqual(unsafeImports(source).map((v) => v.specifier), ['lodash', '../outside.js']);
});

test('unsafeImports flags a multi-line import, whose `from` clause is on another line', () => {
  // strip()'s regex is single-line, so this statement survives into the bundle verbatim.
  const violations = unsafeImports(['import {', "  thing,", "} from './registry.js';"].join('\n'));
  assert.equal(violations.length, 1);
  assert.equal(violations[0].line, 1);
  assert.equal(violations[0].specifier, null);
  assert.match(violations[0].reason, /single-line `from` clause/);
});

test('unsafeImports ignores import.meta and dynamic import()', () => {
  const source = ['const here = import.meta.url;', "const m = await import('./lazy.js');"].join('\n');
  assert.deepEqual(unsafeImports(source), []);
});

test('the real src tree has no unsafe imports (build() succeeds)', () => {
  assert.doesNotThrow(() => build());
});

// checkUnsafeImports is the exact function build() calls at step 0 — this exercises the
// throw wiring itself (the `{ ...v, file }` spread and the multi-line Error format), not
// just the pure unsafeImports() scan it's built on.
test('checkUnsafeImports throws naming file, line and reason for violations spanning two files', () => {
  const files = ['a.js', 'b.js'];
  const sources = new Map([
    ['a.js', "const ok = 1;\nimport { x } from 'lodash';"],
    ['b.js', "import './sideEffect.js';"],
  ]);
  assert.throws(
    () => checkUnsafeImports(files, sources),
    (err) => {
      assert.match(err.message, /unsafe import\(s\) in workflows\/src/);
      assert.match(err.message, /src\/a\.js:2:.*lodash/);
      assert.match(err.message, /src\/b\.js:1:.*sideEffect\.js/);
      return true;
    },
  );
});

test('checkUnsafeImports does not throw when every file is clean', () => {
  const files = ['a.js'];
  const sources = new Map([['a.js', "import { x } from './sibling.js';"]]);
  assert.doesNotThrow(() => checkUnsafeImports(files, sources));
});

test('stripInertLines drops only parser-proven inert candidates', () => {
  const body = [
    'const code = 1;',
    '   ',
    '\t\t',
    '',
    '// code comment',
    'const expression = `${1',
    '  // expression comment',
    '  + 2}`;',
    '/// slash comment',
    'class Example {',
    '  // class comment',
    '}',
    'const object = {',
    '  // object comment',
    '  value: 1,',
    '};',
    'const arrow = () => {',
    '  // arrow comment',
    '  return 1;',
    '};',
    'const trailing = code; // trailing comment',
    "const quoted = '//';",
    'const url = "http://example.test";',
    'const templateInterior = `template',
    '  // template interior',
    '',
    '   ',
    'end`;',
    'const block = /* block',
    '  // block interior',
    '  // closing */ 1;',
    "const continued = 'start\\",
    '  // middle\\',
    "  // tail';",
    'const templateClosing = `open',
    '  // closes`',
    'const nested = `${`inner',
    '  // nested closes`}',
    'outer`;',
  ].join('\n');
  const lines = body.split('\n');
  const droppedIndexes = new Set([1, 2, 3, 4, 6, 8, 10, 13, 17]);
  const result = stripInertLines(body, 'synthetic.js');

  assert.deepEqual(result.dropped, [
    '   ',
    '\t\t',
    '',
    '// code comment',
    '  // expression comment',
    '/// slash comment',
    '  // class comment',
    '  // object comment',
    '  // arrow comment',
  ]);
  assert.deepEqual(result.kept, [
    '  // template interior',
    '',
    '   ',
    '  // block interior',
    '  // closing */ 1;',
    '  // middle\\',
    "  // tail';",
    '  // closes`',
    '  // nested closes`}',
  ]);
  assert.equal(
    result.text,
    lines.filter((_, index) => !droppedIndexes.has(index)).join('\n'),
  );
});

test('stripInertLines rejects a baseline-unparseable module by name', () => {
  assert.throws(
    () => stripInertLines('const = 1;', 'synthetic-invalid.js'),
    /synthetic-invalid\.js is not parseable as an async function body/,
  );
});

test('checkBundleSize pins the byte cap, headroom, and UTF-8 measurement', () => {
  assert.equal(WORKFLOW_SCRIPT_CAP, 524_288);
  assert.equal(BUNDLE_HEADROOM, 65_536);
  assert.equal(BUNDLE_MAX_BYTES, 458_752);
  assert.doesNotThrow(() => checkBundleSize('a'.repeat(458_752)));
  assert.throws(
    () => checkBundleSize('a'.repeat(458_753)),
    (error) => {
      assert.match(error.message, /458753/);
      assert.match(error.message, /458752/);
      assert.match(error.message, /524288/);
      assert.match(error.message, /65536/);
      assert.match(error.message, /bundle must shrink/);
      return true;
    },
  );
  const multibyte = 'é'.repeat(229_377);
  assert.ok(multibyte.length < 458_752);
  assert.throws(() => checkBundleSize(multibyte), /bundle is 458754 bytes/);
});

test('build wires the bundle size check to the real bundle', () => {
  const bundle = build();
  const bytes = Buffer.byteLength(bundle, 'utf8');
  assert.throws(
    () => build({ maxBytes: 1000 }),
    (error) => {
      assert.match(error.message, new RegExp(`bundle is ${bytes} bytes`));
      assert.match(error.message, /limit is 1000 bytes/);
      return true;
    },
  );
  assert.throws(
    () => build({ maxBytes: 0 }),
    (error) => {
      assert.match(error.message, new RegExp(`bundle is ${bytes} bytes`));
      assert.match(error.message, /limit is 0 bytes/);
      return true;
    },
  );
});

test('buildFromSources defaults the bundle limit to the headroom-adjusted cap', () => {
  const sources = new Map([
    ['big.js', `const x = '${'a'.repeat(460_000)}';\n`],
  ]);
  assert.throws(
    () => buildFromSources(['big.js'], sources),
    /limit is 458752 bytes/,
  );
});

test('the real bundle contains only generated comments after inert-line stripping', () => {
  const bundle = build();
  const generated = '// GENERATED by workflows/build.js — do not edit by hand.';
  const bundleLines = bundle.split('\n');
  const generatedAt = bundleLines.findIndex((line) => line === generated);
  assert.notEqual(generatedAt, -1);
  const body = bundleLines.slice(generatedAt).join('\n');
  const result = stripInertLines(body, 'pipeline.js');
  const separator = /^\/\/ --- \S+ ---$/;
  const generatedLines = result.dropped.filter((line) => line === generated);
  const separators = result.dropped.filter((line) => separator.test(line));

  assert.equal(generatedLines.length, 1);
  // Kept lines are literal content and remain kept when the bundle is stripped again.
  // Completeness is checked by counting generated headers and module separators in dropped.
  assert.equal(result.dropped.length, generatedLines.length + separators.length);
  assert.ok(
    Buffer.byteLength(bundle, 'utf8') <= BUNDLE_MAX_BYTES,
  );
});

// This parser API is experimental; the pinned Node version decides its behavior.
test('independent parser output agrees for stripped and unstripped bundles', () => {
  const wrap = (bundle) =>
    `async function __w(){${bundle.replace(/^export const meta\b/m, 'const meta')}}`;
  const unstripped = build({ dropComments: false, maxBytes: Infinity });
  const stripped = build();
  assert.equal(
    stripTypeScriptTypes(wrap(unstripped), { mode: 'transform' }),
    stripTypeScriptTypes(wrap(stripped), { mode: 'transform' }),
  );

  const fixture = ['const literal = `one', '  // inside', 'two`;', ''].join('\n');
  const strippedFixture = stripInertLines(fixture, 'literal-fixture.js').text;
  assert.equal(
    stripTypeScriptTypes(wrap(fixture), { mode: 'transform' }),
    stripTypeScriptTypes(wrap(strippedFixture), { mode: 'transform' }),
  );
});

test('checkRawLineTerminators names every forbidden code point and its line', () => {
  for (const [name, terminator] of [
    ['U+000D', '\r'],
    ['U+2028', '\u2028'],
    ['U+2029', '\u2029'],
  ]) {
    const files = ['bad.js'];
    const sources = new Map([
      ['bad.js', `const first = 1;\nconst second = 2;${terminator}const third = 3;`],
    ]);
    assert.throws(
      () => checkRawLineTerminators(files, sources),
      (error) => {
        assert.match(error.message, /src\/bad\.js:2/);
        assert.match(error.message, new RegExp(name.replace('+', '\\+')));
        return true;
      },
    );
  }

  const files = ['first.js', 'second.js', 'third.js'];
  const sources = new Map([
    ['first.js', 'const first = 1;\nconst second = 2;'],
    ['second.js', 'const first = 1;\nconst second = 2;'],
    ['third.js', 'const first = 1;\nconst second = 2;\u2028const third = 3;'],
  ]);
  assert.throws(
    () => checkRawLineTerminators(files, sources),
    (error) => {
      assert.match(error.message, /src\/third\.js:2/);
      assert.doesNotMatch(error.message, /src\/(?:first|second)\.js/);
      return true;
    },
  );
});

test('build source assembly invokes the raw line-terminator guard', () => {
  const files = ['bad.js'];
  const sources = new Map([['bad.js', 'const first = 1;\r\nconst second = 2;']]);
  assert.throws(
    () => buildFromSources(files, sources),
    /src\/bad\.js:1: raw U\+000D/,
  );
});
