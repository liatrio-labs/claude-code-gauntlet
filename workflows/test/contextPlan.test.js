import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { contextReadPlan, sharedContextLine } from '../src/contextPlan.js';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const PLAN_ROWS = [
  { lines: 1, counts: [1, 1, 1, 1], firsts: [1, 1, 1, 1] },
  { lines: 2, counts: [1, 1, 1, 1], firsts: [2, 2, 2, 2] },
  { lines: 749, counts: [1, 1, 2, 10], firsts: [749, 749, 638, 75] },
  { lines: 750, counts: [1, 1, 2, 10] },
  { lines: 751, counts: [2, 2, 2, 11] }, { lines: 1499, counts: [2, 2, 3, 20] },
  { lines: 1500, counts: [2, 2, 3, 20] }, { lines: 1501, counts: [3, 3, 3, 21] },
  { lines: 2028, counts: [3, 3, 4, 28] }, { lines: 5000, counts: [7, 7, 8, 67] },
  { lines: 100000, counts: [134, 134, 157, 1334] },
];
const FIRST_SPANS = [750, 750, 638, 75];
const CHAR_AXES = [{ perLine: 0 }, { perLine: 1 }, { perLine: 47 }, { perLine: 400 }];
for (const row of PLAN_ROWS) for (const [index, axis] of CHAR_AXES.entries()) {
  test(`plan coverage: ${row.lines} lines at ${axis.perLine} chars per line`, () => {
    const plan = contextReadPlan(row.lines, axis.perLine ? row.lines * axis.perLine : undefined);
    assert.equal(plan.length, row.counts[index]);
    assert.equal(plan[0].limit, (row.firsts ?? FIRST_SPANS)[index]);
    let next = 1;
    for (const { offset, limit } of plan) {
      assert.equal(offset, next);
      assert.ok(limit > 0 && limit <= 750);
      next = offset + limit;
    }
    assert.equal(next - 1, row.lines);
  });
}
const EXACT_PLAN_ROWS = [
  { name: 'profiled character bound', lines: 2028, chars: 94784,
    expected: [{ offset: 1, limit: 641 }, { offset: 642, limit: 641 },
      { offset: 1283, limit: 641 }, { offset: 1924, limit: 105 }] },
  { name: 'one line exceeds character budget', lines: 3, chars: 180000,
    expected: [{ offset: 1, limit: 1 }, { offset: 2, limit: 1 }, { offset: 3, limit: 1 }] },
];
for (const row of EXACT_PLAN_ROWS) test(`exact plan: ${row.name}`, () => {
  const plan = contextReadPlan(row.lines, row.chars);
  assert.deepEqual(plan, row.expected);
  for (const { limit } of plan) {
    assert.ok(limit <= 750);
    if (row.name === 'profiled character bound') assert.ok(limit * (94784 / 2028) < 58145);
  }
});
const BAD_LINES = [undefined, null, 0, -1, 1.5, NaN, Infinity, '2028', {}, Number.MAX_SAFE_INTEGER + 2,
  Number.MAX_SAFE_INTEGER, 10000000000, 1500001];
for (const [index, lines] of BAD_LINES.entries()) test(`unplannable measurement: ${index}`, () => {
  assert.deepEqual(contextReadPlan(lines, 94784), []);
});
const ADVISORY_CHARS = [undefined, null, 0, -1, 2.5, NaN, 'x'];
for (const [index, chars] of ADVISORY_CHARS.entries()) test(`advisory characters: ${index}`, () => {
  assert.deepEqual(contextReadPlan(1000, chars), [{ offset: 1, limit: 750 }, { offset: 751, limit: 250 }]);
});
const CEILING_ROWS = [{ lines: 1500000, count: 2000 }, { lines: 1500001, count: 0 }];
for (const row of CEILING_ROWS) test(`allocation ceiling: ${row.lines}`, () => {
  assert.equal(contextReadPlan(row.lines).length, row.count);
});

const NO_PATH_ROWS = [{}, { contextLines: 2028 }, null];
for (const [index, input] of NO_PATH_ROWS.entries()) test(`no context path: ${index}`, () => {
  assert.equal(sharedContextLine(input), '');
});

const SOURCE_GUARDS = [
  { name: 'no stage input carries the context path', kind: 'capability', expected: [] },
  { name: 'setupContext has one build point', kind: 'build point', expected: [1, 1] },
];
for (const row of SOURCE_GUARDS) test(`source guard: ${row.name}`, () => {
  if (row.kind === 'capability') {
    const offenders = readdirSync(SRC).filter((name) => name.endsWith('.js')).sort()
      .flatMap((name) => readFileSync(join(SRC, name), 'utf8').split('\n')
        .map((line, i) => ({ line: line.trim(), n: `${name}:${i + 1}` })))
      .filter(({ line }) => !line.startsWith('//') && !line.startsWith('*'))
      .filter(({ line }) => /(^|[\s{(])contextPath\s*(,|:\s*\w)/.test(line))
      .filter(({ line }) => !/const contextPath =/.test(line))
      .filter(({ line }) => !/contextPath, contextLines:/.test(line))
      .map(({ n, line }) => `${n}: ${line}`);
    assert.deepEqual(offenders, row.expected);
  } else {
    const sources = readdirSync(SRC, { recursive: true, withFileTypes: true })
      .filter((entry) => entry.isFile())
      .map((entry) => ({ path: join(entry.parentPath, entry.name), text: readFileSync(join(entry.parentPath, entry.name), 'utf8') }));
    const code = sources.map(({ path, text }) => {
      const content = text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
      return path === join(SRC, 'contextPlan.js')
        ? content.replace(/\bfunction\s+(setupContext|sharedContextLine)\s*\(/g, 'function (')
        : content;
    }).join('\n');
    assert.deepEqual(['setupContext', 'sharedContextLine'].map((name) =>
      (code.match(new RegExp(`\\b${name}\\s*\\(`, 'g')) ?? []).length), row.expected);
  }
});
