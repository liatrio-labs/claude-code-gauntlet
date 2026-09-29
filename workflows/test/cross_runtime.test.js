import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fnv1a32, normalizeForChecksum } from '../src/stages.js';
import { matchesRule } from '../src/args.js';
import { prepareLine, foldProse, proseFenceCloser } from '../src/renderReport.js';

const families = ['fnv1a32', 'json_spelling', 'outbound_line', 'outbound_fold', 'config_rule'];
const vectors = new Map(families.map((family) => {
  const payload = JSON.parse(readFileSync(new URL(`../../tests/fixtures/cross_runtime/${family}.json`, import.meta.url), 'utf8'));
  assert.equal(payload.algorithm, family);
  return [family, payload.cases];
}));

for (const [family, cases] of vectors) {
  for (const row of cases) {
    test(`${family}: ${row.id}`, () => {
      let actual;
      if (family === 'fnv1a32') actual = fnv1a32(row.input);
      else if (family === 'json_spelling') {
        actual = row.operation === 'normalize' ? normalizeForChecksum(row.input) : JSON.stringify(row.input, null, 2);
      } else if (family === 'outbound_line') actual = prepareLine(row.input);
      else if (family === 'config_rule') {
        const { rule, value, mode } = row.input;
        actual = matchesRule(rule, value, mode);
      } else if (row.operation === 'closer') actual = proseFenceCloser(row.input);
      else {
        const source = row.input;
        actual = foldProse(source.prefix + source.repeat.repeat(source.count), row.js_limit);
      }
      assert.equal(actual, family === 'outbound_fold' ? row.expected_js : row.expected);
      if (family === 'outbound_line') assert.equal(prepareLine(actual), actual);
    });
  }
}
