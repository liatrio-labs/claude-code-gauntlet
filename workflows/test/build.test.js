// build.test.js — the bundler's top-level identifier-collision detector and ORDER
// completeness guard: the build-time guards that turn a would-be runtime
// `Identifier 'X' has already been declared` SyntaxError (the SEVERITY_ORDER
// collision the live smoke run hit) or a silently incomplete bundle into loud
// build failures naming the duplicate or the missing file — plus the import guard
// that refuses any specifier strip() cannot safely drop.
import test from 'node:test';
import assert from 'node:assert/strict';
import { detectTopLevelCollisions, build, orderMismatches, unsafeImports } from '../build.js';

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

test('unsafeImports accepts relative sibling imports', () => {
  const source = [
    "import { dedupById } from './findingDedup.js';",
    "import './sideEffect.js';",
    'const x = 1;',
  ].join('\n');
  assert.deepEqual(unsafeImports(source), []);
});

test('unsafeImports flags a node: builtin import with file line and specifier', () => {
  const violations = unsafeImports(['const a = 1;', "import { inspect } from 'node:util';"].join('\n'));
  assert.equal(violations.length, 1);
  assert.equal(violations[0].line, 2);
  assert.equal(violations[0].specifier, 'node:util');
  assert.match(violations[0].reason, /undefined reference/);
});

test('unsafeImports flags bare, parent-relative and side-effect non-relative specifiers', () => {
  const source = [
    "import lodash from 'lodash';",
    "import { x } from '../outside.js';",
    "import 'node:fs';",
  ].join('\n');
  assert.deepEqual(unsafeImports(source).map((v) => v.specifier), ['lodash', '../outside.js', 'node:fs']);
});

test('unsafeImports flags a multi-line import, whose specifier strip() never sees', () => {
  // strip()'s regex is single-line, so this statement survives into the bundle verbatim.
  const violations = unsafeImports(['import {', "  thing,", "} from './registry.js';"].join('\n'));
  assert.equal(violations.length, 1);
  assert.equal(violations[0].line, 1);
  assert.equal(violations[0].specifier, null);
  assert.match(violations[0].reason, /multi-line import/);
});

test('unsafeImports ignores import.meta and dynamic import()', () => {
  const source = ['const here = import.meta.url;', "const m = await import('./lazy.js');"].join('\n');
  assert.deepEqual(unsafeImports(source), []);
});

test('the real src tree has no unsafe imports (build() succeeds)', () => {
  assert.doesNotThrow(() => build());
});
