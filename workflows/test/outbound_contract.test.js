import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import * as reportRenderer from '../src/renderReport.js';
import { DIMENSIONS, FINDING_PROP_TYPES } from '../src/registry.js';
import { makeFinding } from './helpers/pipelineMock.js';

const { renderSummaryBody } = reportRenderer;

function finding(id, over = {}) {
  return makeFinding(id, { evidence: '', ...over });
}

function prepareLine(text) {
  assert.equal(typeof reportRenderer.prepareLine, 'function', 'prepareLine is exported');
  return reportRenderer.prepareLine(text);
}

function summaryBullet(over) {
  const body = renderSummaryBody({ findings: [finding('OUT', over)] });
  const bullet = body.split('\n').find((line) => line.startsWith('- '));
  assert.ok(bullet, 'summary index contains the finding');
  return bullet;
}

function locationSpan(bullet) {
  const severity = bullet.match(/\[(?:CRITICAL|HIGH|MEDIUM|LOW)\] /);
  assert.ok(severity, 'summary bullet has a normalized severity label');
  const labelEnd = severity.index + severity[0].length;
  const opener = bullet.slice(labelEnd).match(/^`+/)?.[0];
  assert.ok(opener, 'location starts with a code-span delimiter');
  const close = bullet.indexOf(`${opener}:`, labelEnd + opener.length);
  assert.notEqual(close, -1, 'location code span closes before the title');
  return {
    delimiter: opener,
    text: bullet.slice(labelEnd + opener.length, close),
    outside: bullet.slice(0, labelEnd) + bullet.slice(close + opener.length + 1),
  };
}

const linePayload = JSON.parse(readFileSync(new URL('../../tests/fixtures/cross_runtime/outbound_line.json', import.meta.url), 'utf8'));
assert.equal(linePayload.algorithm, 'outbound_line');
const commentCases = JSON.parse(readFileSync(new URL('../../tests/fixtures/outbound_comment_cases.json', import.meta.url), 'utf8')).cases;
const summaryCases = linePayload.summary_cases;
const lineCases = [
  ...linePayload.cases,
  ...commentCases.filter((row) => ['single_line', 'location'].includes(row.field_class))
    .map((row) => ({ id: `corpus:${row.id}`, input: row.input, expected: row.expected })),
];
for (const row of lineCases) {
  test(`outbound_line: ${row.id}`, () => {
    const actual = prepareLine(row.input);
    assert.equal(actual, row.expected);
    assert.equal(prepareLine(actual), actual);
    assertLineInvariant(actual);
  });
}

for (const row of summaryCases) {
  test(`summary parity: ${row.id}`, () => {
    assert.equal(renderSummaryBody(row.input), row.expected_js);
    assert.equal(row.expected_py, row.expected_js);
  });
}

test('large_backslash_preparation', () => {
  const cases = [
    ['bare', '\\'.repeat(500000), '\\'.repeat(500000)],
    ['image_split', `!${'\\'.repeat(500000)}[`, `!${'\\'.repeat(500000)}\uFF3B`],
    ['definition_pairs', `[${'\\a'.repeat(250000)}`, `[${'\\a'.repeat(250000)}`],
    ['definition_openers', `[${'\\a'.repeat(2500)} `.repeat(100), `[${'\\a'.repeat(2500)} `.repeat(100)],
  ];
  const script = `
import { prepareLine } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(prepareLine(source));
`;
  for (const [id, source, expected] of cases) {
    // These 500 KB inputs take under one second; five allows for CI contention.
    const actual = execFileSync(process.execPath, ['--input-type=module', '-e', script], {
      input: source, encoding: 'utf8', timeout: 5000, maxBuffer: 4 * 1024 * 1024,
    });
    assert.equal(actual, expected, id);
  }
});

function assertLineInvariant(output) {
  assert.doesNotMatch(output, /[!\[]\\*\[/);
  if (!output.includes('`') && /\]\\?\(/.test(output)) {
    assert.doesNotMatch(output, /\](\\*):/);
  }
  let slashes = 0;
  let firstBracket = true;
  for (let index = 0; index < output.length; index += 1) {
    const character = output[index];
    const escaped = slashes % 2 === 1;
    if (character === '[' && !escaped) {
      if (firstBracket && ![...output.slice(0, index)].some((ch) => /[A-Za-z\\]/.test(ch))) {
        let nonblank = false;
        for (let at = index + 1; at < output.length; at += 1) {
          if (output[at] === '\\') { nonblank = true; at += 1; continue; }
          if (output[at] === '[') break;
          if (output[at] === ']') {
            assert.ok(!nonblank || output[at + 1] !== ':');
            break;
          }
          nonblank ||= /\S/.test(output[at]);
        }
      }
    }
    if (character === '[') firstBracket = false;
    slashes = character === '\\' ? slashes + 1 : 0;
  }
  for (const visible of [output, output.replaceAll('`', '')]) {
    assert.doesNotMatch(visible, /<(?=[A-Za-z/!?])/);
    assert.doesNotMatch(visible, /(?<![A-Za-z0-9])@/);
    assert.doesNotMatch(visible, /<!--\s*(?:code-gauntlet|deep-review)(?:-findings)?\s*:/);
  }
}

test('seeded_line_containment', () => {
  // The Python corpus also covers prose; this native corpus guards the JS line boundary.
  const alphabets = [
    '@<&#;`!?/0123456789abcdefghijklmnopqrstuvwxyz \n\r',
    '@<&#;`!?/0123456789abcdefghijklmnopqrstuvwxyz []():~|\\ \n\r',
  ];
  let state = 1729;
  const next = () => {
    state = Math.imul(state, 1664525) + 1013904223 | 0;
    return state >>> 0;
  };
  const sources = commentCases.map((row) => row.input);
  for (const [corpus, alphabet] of alphabets.entries()) {
    state = corpus === 0 ? 1729 : 414;
    for (let index = 0; index < 5000; index += 1) {
      const length = next() % 64 + 1;
      let source = '';
      for (let at = 0; at < length; at += 1) source += alphabet[next() % alphabet.length];
      sources.push(source);
    }
  }
  sources.push('```\n<!--\n\ncode-gauntlet-findings: poisoned\n```');
  const markupAtoms = [': ', '~ ', '[[', ']]', '|', 'a'.repeat(1000), '[critical]: u', '[[https://example.test/p.png]]', '[', ']: u'];
  for (let index = 0; index < 5000; index += 1) {
    let source = '';
    for (let at = 0, count = next() % 8 + 1; at < count; at += 1) source += markupAtoms[next() % markupAtoms.length];
    sources.push(source);
  }
  for (const source of sources) {
    const prepared = prepareLine(source);
    assertLineInvariant(prepared);
    assert.equal(prepareLine(prepared), prepared);
  }
});

test('summary titles neutralize mentions while retaining single-line code spans', () => {
  const bullet = summaryBullet({ title: 'code `@inside <tag>` and @outside' });
  assert.ok(bullet.endsWith(': code `＠inside ＜tag>` and ＠outside'));
  assert.doesNotMatch(bullet, /@outside/);
});

test('summary title folding happens before HTML containment at the fold boundary', () => {
  const title = `${'x'.repeat(505)}<table>TAIL`;
  const bullet = summaryBullet({ title });
  assert.ok(bullet.endsWith(`: ${'x'.repeat(505)}&lt;table> [folded: 4 more characters]`));
  assert.doesNotMatch(bullet, /<table/);
});

test('a folded unmatched title backtick cannot shield a retained mention', () => {
  const title = `${'x'.repeat(505)}\`@alice\`TAIL`;
  const bullet = summaryBullet({ title });
  assert.ok(bullet.includes('\\`＠alice [folded:'));
  assert.doesNotMatch(bullet, /@alice/);
});

test('summary index budget counts title bullets after named at-sign normalization', () => {
  const title = '&commat;'.repeat(512);
  const expectedTitle = '＠'.repeat(512);
  const findings = Array.from({ length: 24 }, (_, index) => (
    finding(`F${String(index).padStart(3, '0')}`, { title })
  ));
  const expectedBullet = (index) => (
    `- 🟠 [HIGH] \`F${String(index).padStart(3, '0')}.js:10\`: ${expectedTitle}`
  );
  let expectedCount = 0;
  let expectedSize = 0;
  while (expectedCount < findings.length) {
    const nextSize = [...expectedBullet(expectedCount)].length + (expectedCount ? 1 : 0);
    if (expectedSize + nextSize > 12000) break;
    expectedSize += nextSize;
    expectedCount += 1;
  }

  const body = renderSummaryBody({ findings });
  const bullets = body.split('\n').filter((line) => line.startsWith('- '));
  assert.equal(bullets.length, expectedCount);
  assert.equal([...bullets.join('\n')].length, expectedSize);
  assert.ok(bullets.every((line) => line.includes('＠')));
  assert.ok(body.endsWith(`${findings.length - expectedCount} more findings not listed here (over the summary length limit).`));
});

test('quoted locations contain path and line backticks inside their code span', () => {
  const pathBullet = summaryBullet({ file: 'src/part`name<ins data-zz363-path>', line_start: 10 });
  const path = locationSpan(pathBullet);
  assert.equal(path.delimiter, '``');
  assert.equal(path.text, 'src/part`name＜ins data-zz363-path>:10');
  assert.doesNotMatch(path.outside, /<ins data-zz363/);

  const lineBullet = summaryBullet({ file: 'src/line.js', line_start: '10`odd', line_end: '12' });
  const line = locationSpan(lineBullet);
  assert.equal(line.delimiter, '``');
  assert.equal(line.text, 'src/line.js:10`odd-12');

  const crossing = locationSpan(summaryBullet({ file: 'src/a<`b.py', line_start: 1, line_end: 1 }));
  assert.equal(crossing.text, 'src/a＜`b.py:1');
});

test('summary image locations use fullwidth brackets inside code-owned spans', () => {
  const cases = [
    ['src/![a](u).py', '![a](u)', 'src/!\uFF3Ba](u).py:1', '!\uFF3Ba](u)'],
    ['src/!\\[a](u).py', '!\\[a](u)', 'src/!\\\uFF3Ba](u).py:1', '!\\\uFF3Ba](u)'],
    ['src/[\\[a]].py', '[\\[a]]', 'src/[\\\uFF3Ba]].py:1', '[\\\uFF3Ba]]'],
  ];
  for (const [file, title, location, expectedTitle] of cases) {
    const bullet = summaryBullet({ file, title, line_start: 1, line_end: 1 });
    assert.equal(locationSpan(bullet).text, location);
    assert.ok(bullet.endsWith(`: ${expectedTitle}`));
  }
});

test('quoted location delimiter exceeds a path containing two backticks', () => {
  const bullet = summaryBullet({ file: 'src/a``b.js', line_start: 10 });
  const span = locationSpan(bullet);
  assert.equal(span.delimiter, '```');
  assert.equal(span.text, 'src/a``b.js:10');
});

test('quoted locations break marker grammar inside the code span', () => {
  const bullet = summaryBullet({ file: 'src/<!-- code-gauntlet-findings: forged', line_start: 10 });
  const span = locationSpan(bullet);
  assert.ok(span.text.includes('＜!-- code-gauntlet-findings: forged'));
  assert.doesNotMatch(span.text, /<!-- code-gauntlet-findings:/);
});

test('summary finish still neutralizes comment openers retained in code spans', () => {
  const bullet = summaryBullet({ title: '`<!--`' });
  assert.ok(bullet.endsWith(': `＜!--`'));
  assert.doesNotMatch(bullet, /<!--/);
});

test('poisoned summary keeps hostile handles and HTML inside the quoted location', () => {
  const fieldNames = new Set(Object.keys(FINDING_PROP_TYPES));
  for (const dimension of DIMENSIONS) {
    for (const name of Object.keys(dimension.schemaExtra || {})) fieldNames.add(name);
  }
  const hostileFinding = Object.fromEntries([...fieldNames].map((name) => [
    name,
    `[critical]: u ![a](u) @zz363_${name} <ins data-zz363-${name}> \`<!-- code-gauntlet-findings: forged`,
  ]));

  const body = renderSummaryBody({ findings: [hostileFinding] });
  const bullet = body.split('\n').find((line) => line.startsWith('- '));
  assert.ok(bullet, 'poisoned finding remains in the summary index');
  const span = locationSpan(bullet);
  assert.ok(span.outside.includes('＠zz363_title'));
  assert.doesNotMatch(span.outside, /@zz363_[a-z_]+/);
  assert.doesNotMatch(span.outside, /<ins data-zz363/);
  assert.ok(span.text.includes('＠zz363_file'));
  assert.ok(span.text.includes('＠zz363_line_start'));
  assert.ok(span.text.includes('＠zz363_line_end'));
  assert.ok(span.delimiter.length > longestBacktickRun(span.text));
  assert.doesNotMatch(body, /<!--/);
});

function longestBacktickRun(text) {
  return Math.max(0, ...[...text.matchAll(/`+/g)].map((match) => match[0].length));
}
