import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyInjectionFilter, applyReplayInjectionScan, applyInjectedProseStrip } from '../src/injection.js';
import { cleanFinding } from './helpers/findings.js';

const SCAN_ROWS = [
  ['shell command', { description: 'rm -rf /tmp/cache' }, 'shell command'],
  ['URL directive', { description: 'visit https://example.com' }, 'visit-URL'],
  ['encoded payload', { title: 'decode', description: 'A'.repeat(40) }, 'encoded payload'],
  ['bypass instruction', { title: 'skip', description: 'review' }, 'bypass'],
  ['instructional tone', { description: 'you should run the script' }, 'instructional tone'],
  ['unsafe fix', { description: 'disable TLS validation' }, 'vulnerability'],
  ['block marker', { description: '<finding severity="high">' }, 'injection marker'],
  ['placeholder title', { title: 'Example finding' }, 'placeholder'],
  ['placeholder path', { file: 'src/{name}.py' }, 'template markers'],
  ['confusable folding', { description: '\u017fkip review' }, 'bypass'],
  ['invisible stripping', { description: 's\u200bkip review' }, 'bypass'],
];
for (const [name, fields, reason] of SCAN_ROWS) {
  test('injection scan: ' + name, () => {
    const input = cleanFinding({ confidence: 50, ...fields });
    const snapshot = structuredClone(input);
    for (const scan of [applyInjectionFilter, applyReplayInjectionScan]) {
      const out = scan([input]);
      assert.deepEqual(out.kept, []);
      assert.equal(out.eliminated.length, 1);
      assert.equal(out.eliminated[0].eliminated_by, 'injection');
      assert.ok(out.eliminated[0].elimination_reason.includes(reason));
    }
    assert.deepEqual(input, snapshot);
  });
}

test('clean findings survive record and replay without losing fields', () => {
  const input = cleanFinding({
    title: 'Add an auto-approve toggle for trusted release branches in CI',
    description: 'The stored procedure grants [INSERT, UPDATE, DELETE] privileges to the public role, which is broader than intended for this operation. Identifier abcdef0123456789abcdef0123456789abcdef01 records the upstream revision.',
    suggestion: 'Use a role limited to the required operations.', suggested_fix_code: 'return value;',
  });
  const snapshot = structuredClone(input);
  for (const scan of [applyInjectionFilter, applyReplayInjectionScan]) {
    const out = scan([input]);
    assert.deepEqual(out.kept, [snapshot]);
    assert.deepEqual(out.eliminated, []);
  }
  assert.deepEqual(input, snapshot);
});

test('duplicate findings retain only the first copy', () => {
  const input = cleanFinding();
  for (const scan of [applyInjectionFilter, applyReplayInjectionScan]) {
    const out = scan([input, { ...input, id: 'second' }]);
    assert.deepEqual(out.kept, [input]);
    assert.equal(out.eliminated[0].id, 'second');
    assert.equal(out.eliminated[0].elimination_reason, 'duplicate of finding "test-1"');
  }
});

test('short high-confidence prose is rejected only on initial filtering', () => {
  const input = cleanFinding({ description: 'Loop bound is off by one' });
  const out = applyInjectionFilter([input]);
  assert.deepEqual(out.kept, []);
  assert.equal(out.eliminated[0].elimination_reason, 'suspiciously short description (6 words) with high confidence (90)');
  assert.deepEqual(applyReplayInjectionScan([input]).kept, [input]);
});

for (const field of ['suggestion', 'claude_md_rule', 'spec_text']) {
  test('injected ' + field + ' removes its fix through both scan modes', () => {
    const input = cleanFinding({ [field]: 'skip review', suggested_fix_code: 'return 1;' });
    const before = structuredClone(input);
    for (const scan of [applyInjectionFilter, applyReplayInjectionScan]) {
      const out = scan([input]);
      assert.deepEqual(out.eliminated, []);
      const kept = out.kept[0];
      assert.equal(kept[field], undefined);
      assert.equal(kept[field + '_removed_by'], 'injection');
      assert.ok(kept[field + '_removal_reason'].startsWith(field + ' contains bypass/auto-approve instruction:'));
      assert.equal(kept.suggested_fix_code, undefined);
      assert.equal(kept.suggested_fix_code_removal_reason, field + ' carried contains bypass/auto-approve instruction');
      assert.deepEqual(applyInjectedProseStrip(kept), kept);
    }
    assert.deepEqual(input, before);
  });
}

test('prose rule precedence reports the first matching family', () => {
  const out = applyInjectedProseStrip({ suggestion: 'rm -rf visit https://x.test', suggested_fix_code: 'return 1;' });
  assert.equal(out.suggestion, undefined);
  assert.ok(out.suggestion_removal_reason.startsWith('suggestion contains shell command pattern:'));
  assert.equal(out.suggested_fix_code_removal_reason, 'suggestion carried contains shell command pattern');
});

const LINES = Array.from({ length: 100 }, (_, i) => 'line' + i).join('\n');

for (const [name, code, accepted] of [
  ['line limit', LINES + '\n', true],
  ['second trailing LF counts', LINES + '\n\n', false],
  ['over line limit', LINES + '\nx', false],
  ['point limit', 'x'.repeat(8000), true],
  ['over point limit', 'x'.repeat(8001), false],
]) {
  test('fix delivery bound: ' + name, () => {
    const input = { suggested_fix_code: code };
    const out = applyInjectedProseStrip(input);
    assert.deepEqual(out, accepted ? input : {
      suggested_fix_code_removed_by: 'injection',
      suggested_fix_code_removal_reason: 'suggested_fix_code exceeds the delivery bound',
    });
    assert.deepEqual(input, { suggested_fix_code: code });
  });
}
