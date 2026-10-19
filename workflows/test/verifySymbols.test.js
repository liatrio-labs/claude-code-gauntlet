import { test } from 'node:test';
import assert from 'node:assert/strict';
import { extractSymbols } from '../src/verifySymbols.js';
import { loadCases } from './helpers/goldenCases.js';

for (const fixture of loadCases('symbol_extractor')) {
  test(`frozen Python symbol oracle: ${fixture.name}`, () => {
    assert.equal(fixture.input.cases.length, fixture.expected.length);
    fixture.input.cases.forEach((row, index) => {
      assert.deepEqual([...extractSymbols(row.description, row.evidence)].sort(), fixture.expected[index], row.name);
    });
  });
}

test('extracts definite code tokens and accepts non-string inputs', () => {
  const rows = [
    ['BareCamel prose', '`os.path.join` snake_case', ['join', 'path', 'snake_case']],
    ['```lang\nAlphaType BetaType\n```', '', ['AlphaType', 'BetaType', 'langAlphaTypeBetaType']],
    ['na\u00EFve_func \u{1D465}missing_symbol', null, []],
    [false, '`\u0085\u001C__alpha__`', ['alpha']],
    [{}, ['`not_a_string`'], []],
    [undefined, 42, []],
  ];
  for (const [description, evidence, expected] of rows) {
    assert.deepEqual([...extractSymbols(description, evidence)].sort(), expected);
  }
});
