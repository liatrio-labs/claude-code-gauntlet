import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fnv1a32, normalizeForChecksum } from '../src/stages.js';
import { matchesRule } from '../src/args.js';
import { foldInline, foldProse, openProseFence, proseFenceCloser } from '../src/renderReport.js';

const families = ['fnv1a32', 'json_spelling', 'outbound_fold', 'config_rule'];
const foldOperations = new Set(['fold', 'closer', 'fence_state', 'fold_review_py', 'fold_inline_py', 'fold_prose_js', 'fold_inline_js']);
const vectors = new Map(families.map((family) => {
  const payload = JSON.parse(readFileSync(new URL(`../../tests/fixtures/cross_runtime/${family}.json`, import.meta.url), 'utf8'));
  assert.equal(payload.algorithm, family);
  if (family === 'outbound_fold') {
    for (const row of payload.cases) assert.ok(foldOperations.has(row.operation), `unknown fold operation: ${row.operation}`);
    return [family, payload.cases.filter((row) => !row.operation.endsWith('_py'))];
  }
  return [family, payload.cases];
}));

for (const [family, cases] of vectors) {
  for (const row of cases) {
    test(`${family}: ${row.id}`, () => {
      let actual;
      if (family === 'fnv1a32') actual = fnv1a32(row.input);
      else if (family === 'json_spelling') {
        if (row.id === 'safe_integer_bounds') assert.equal(Object.is(row.input[1], -0), true);
        actual = row.operation === 'normalize' ? normalizeForChecksum(row.input) : JSON.stringify(row.input, null, 2);
      } else if (family === 'config_rule') {
        const { rule, value, mode } = row.input;
        actual = matchesRule(rule, value, mode);
      } else if (row.operation === 'closer') actual = proseFenceCloser(row.input);
      else if (row.operation === 'fence_state') actual = openProseFence(row.input);
      else if (['fold', 'fold_prose_js', 'fold_inline_js'].includes(row.operation)) {
        const source = row.input;
        const text = source.prefix + source.repeat.repeat(source.count);
        actual = row.operation === 'fold_inline_js' ? foldInline(text, row.js_limit) : foldProse(text, row.js_limit);
      } else assert.fail(`unknown fold operation: ${row.operation}`);
      assert.deepEqual(actual, family === 'outbound_fold' ? row.expected_js : row.expected);
    });
  }
}
