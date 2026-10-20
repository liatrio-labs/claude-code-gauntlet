import { test } from 'node:test';
import assert from 'node:assert/strict';
import { extractSymbols } from '../src/verifySymbols.js';

const pythonWhitespace = '\t\r\x1C \u0085\u00A0\u1680\u2000\u2028\u2029\u202F\u205F\u3000';

// Expected tokens retain the Python oracle's boundary and fence semantics.
const rows = [
  ['fenced identifiers across fields and lines', '```lang\nAlphaType\n', 'BetaType\n```', ['AlphaType', 'BetaType', 'langAlphaTypeBetaType']],
  ['backtick spans in either field', '`os.path.join`', '`important_func`', ['important_func', 'join', 'path']],
  ['Python whitespace before underscore trim', `\`${pythonWhitespace}__func__${pythonWhitespace}\``, '', ['func']],
  ['dot and underscore trim preserves punctuation tokens', '`...__important_func__...`', '', ['__important_func__', 'important_func']],
  ['dot and call chains', 'os.path.join obj.method() Foo() AlphaType)BetaType', '', ['AlphaType', 'BetaType', 'Foo', 'join', 'method', 'obj', 'path']],
  ['namespace chain', 'Foo::bar', '', ['Foo', 'bar']],
  ['arrow chain', 'ptr->member', '', ['member', 'ptr']],
  ['hash and bracket chains', 'Foo#bar arr[index] AlphaType]BetaType', '', ['AlphaType', 'BetaType', 'Foo', 'arr', 'bar', 'index']],
  ['punctuation left boundary includes letters, numbers and underscores', 'na\u00EFve_func() \u{1D465}_func() \u{1D7DF}BetaType() 1_AlphaType()', '', []],
  ['snake case', 'get_user_data missing_symbol', '', ['get_user_data', 'missing_symbol']],
  ['snake left boundary includes letters, numbers and underscores', 'na\u00EFve_func \u{1D465}missing_symbol \u{1D7DF}new_panel __get_user_data', '', []],
  ['snake right boundary includes letters, numbers and underscores', 'new_panel\u{1D465} new_panel\u{1D7DF} new_panel_', '', []],
  ['stopwords and at most two characters are filtered', '`self` `None` `print` `x` `ab` `bar`', '', ['bar']],
  ['bare CamelCase prose has no symbols', 'Potential Regression in ConcreteType ErrorHandler', '', []],
  ['non-string inputs are empty', {}, ['`not_a_string`'], []],
];

for (const [name, description, evidence, expected] of rows) {
  test(name, () => {
    assert.deepEqual([...extractSymbols(description, evidence)].sort(), expected);
  });
}
