// build.test.js — the bundler's top-level identifier-collision detector, the
// build-time guard that turns a would-be runtime `Identifier 'X' has already been
// declared` SyntaxError (the SEVERITY_ORDER collision the live smoke run hit) into a
// loud build failure naming the duplicate.
import test from 'node:test';
import assert from 'node:assert/strict';
import { detectTopLevelCollisions, build } from '../build.js';

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
