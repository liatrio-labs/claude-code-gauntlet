import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fnv1a32, normalizeForChecksum } from '../src/stages.js';
import { matchesRule } from '../src/args.js';
import { prepareLine, foldProse, proseFenceCloser } from '../src/renderReport.js';

const families = ['fnv1a32', 'json_spelling', 'outbound_line', 'outbound_fold', 'config_rule'];
const outboundCorpus = JSON.parse(readFileSync(new URL('../../tests/fixtures/outbound_comment_cases.json', import.meta.url), 'utf8')).cases
  .filter((row) => row.field_class === 'single_line' || row.field_class === 'location')
  .map((row) => ({ id: `corpus:${row.id}`, input: row.input, expected: row.expected }));
const vectors = new Map(families.map((family) => {
  const payload = JSON.parse(readFileSync(new URL(`../../tests/fixtures/cross_runtime/${family}.json`, import.meta.url), 'utf8'));
  assert.equal(payload.algorithm, family);
  return [family, family === 'outbound_line' ? [...payload.cases, ...outboundCorpus] : payload.cases];
}));

for (const [family, cases] of vectors) {
  for (const row of cases) {
    test(`${family}: ${row.id}`, () => {
      let actual;
      if (family === 'fnv1a32') actual = fnv1a32(row.input);
      else if (family === 'json_spelling') {
        if (row.id === 'safe_integer_bounds') assert.equal(Object.is(row.input[1], -0), true);
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
