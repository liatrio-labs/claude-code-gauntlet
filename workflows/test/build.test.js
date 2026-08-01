// build.test.js — the bundler's top-level identifier-collision detector and ORDER
// completeness guard: the build-time guards that turn a would-be runtime
// `Identifier 'X' has already been declared` SyntaxError (the SEVERITY_ORDER
// collision the live smoke run hit) or a silently incomplete bundle into loud
// build failures naming the duplicate or the missing file.
import test from 'node:test';
import assert from 'node:assert/strict';
import { detectTopLevelCollisions, build, orderMismatches } from '../build.js';

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
