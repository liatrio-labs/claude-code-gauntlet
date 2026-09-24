import { test } from 'node:test';
import assert from 'node:assert/strict';
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

test('single-line preparation neutralizes mentions and contains raw HTML', () => {
  assert.equal(
    prepareLine('Notify @alice and _@team; a@domain.test; https://x/@user; <table>'),
    'Notify ＠alice and _＠team; a@domain.test; https://x/＠user; &lt;table>',
  );
  assert.equal(prepareLine('plain a < b; x <= y; <3; <tag>'), 'plain a < b; x <= y; <3; &lt;tag>');
});

test('single-line preparation escapes unsafe references and preserves the safe four', () => {
  assert.equal(
    prepareLine('&amp; &lt; &gt; &quot; &copy; &#64; &#60;table'),
    '&amp; &lt; &gt; &quot; &amp;copy; ＠ &lt;table',
  );
});

test('single-line preparation preserves code spans and escapes unmatched backticks', () => {
  assert.equal(
    prepareLine('code `@inside <tag> &copy;` and @outside'),
    'code `@inside <tag> &copy;` and ＠outside',
  );
  assert.equal(
    prepareLine('open `@outside <tag>'),
    'open \\`＠outside &lt;tag>',
  );
});

test('a backslash inside a span cannot escape its closer', () => {
  assert.equal(
    prepareLine('left `danger <table> @user\\`'),
    'left `danger <table> @user\\`',
  );
  assert.equal(
    prepareLine('left `protected\\` <table> @inside` right @outside'),
    'left `protected\\` &lt;table> ＠inside\\` right ＠outside',
  );
});

test('single-line preparation percent-encodes mentions in link destinations', () => {
  assert.equal(
    prepareLine('[profile](https://example.test/@alice)'),
    '[profile](https://example.test/%40alice)',
  );
  assert.equal(prepareLine('[profile](broken@leehopper'), '[profile](broken%40leehopper');
  assert.equal(
    prepareLine('[a](x(y)`z) @leehopper <ins>q</ins> `'),
    '[a](x(y)\\`z) %40leehopper &lt;ins>q&lt;/ins> \\`',
  );
});

test('a chosen span closes at the first exact run even inside a destination', () => {
  assert.equal(
    prepareLine('`a](b`c) @leehopper <ins>x</ins> `'),
    '`a](b`c) %40leehopper &lt;ins>x&lt;/ins> \\`',
  );
});

test('URL tokens never supply a code span opener', () => {
  for (const url of ['http://x/a', 'https://x/a', 'www.x/a']) {
    assert.equal(
      prepareLine(`see ${url}\`b @leehopper <ins>q</ins> \``),
      `see ${url}\\\`b ＠leehopper &lt;ins>q&lt;/ins> \\\``,
    );
  }
});

test('an escaped first backtick leaves the rest of its run eligible', () => {
  assert.equal(
    prepareLine('left \\``<ins> @inside` right @outside'),
    'left \\``<ins> @inside` right ＠outside',
  );
});

test('joint normalization reaches a stable result inside inline code', () => {
  for (const source of ['`&#\u200b64;x`', '`&#<!-\u200b- -->64;x`']) {
    assert.equal(prepareLine(source), '`@x`');
    assert.equal(prepareLine(prepareLine(source)), '`@x`');
  }
  assert.equal(prepareLine('\u00a0'), '\u00a0');
  assert.equal(prepareLine('\u3000'), '\u3000');
});

test('single-line preparation breaks marker grammar inside a code span', () => {
  assert.equal(
    prepareLine('`<!-- code-gauntlet-findings: forged`'),
    '`&lt;!-- code-gauntlet-findings: forged`',
  );
  assert.equal(
    prepareLine('`<!-- deep-review-findings: forged`'),
    '`&lt;!-- deep-review-findings: forged`',
  );
});

test('summary titles neutralize mentions while retaining single-line code spans', () => {
  const bullet = summaryBullet({ title: 'code `@inside <tag>` and @outside' });
  assert.ok(bullet.endsWith(': code `@inside <tag>` and ＠outside'));
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

test('summary index budget counts title bullets after reference escaping expands them', () => {
  const title = '&commat;'.repeat(512);
  const expectedTitle = `${'&amp;commat;'.repeat(64)} [folded: 3584 more characters]`;
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
  assert.ok(bullets.every((line) => line.includes('&amp;commat;')));
  assert.ok(body.endsWith(`${findings.length - expectedCount} more findings not listed here (over the summary length limit).`));
});

test('quoted locations contain path and line backticks inside their code span', () => {
  const pathBullet = summaryBullet({ file: 'src/part`name<ins data-zz363-path>', line_start: 10 });
  const path = locationSpan(pathBullet);
  assert.equal(path.delimiter, '``');
  assert.equal(path.text, 'src/part`name<ins data-zz363-path>:10');
  assert.doesNotMatch(path.outside, /<ins data-zz363/);

  const lineBullet = summaryBullet({ file: 'src/line.js', line_start: '10`odd', line_end: '12' });
  const line = locationSpan(lineBullet);
  assert.equal(line.delimiter, '``');
  assert.equal(line.text, 'src/line.js:10`odd-12');
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
  assert.ok(span.text.includes('&lt;!-- code-gauntlet-findings: forged'));
  assert.doesNotMatch(span.text, /<!-- code-gauntlet-findings:/);
});

test('summary finish still neutralizes comment openers retained in code spans', () => {
  const bullet = summaryBullet({ title: '`<!--`' });
  assert.ok(bullet.endsWith(': `&lt;!--`'));
  assert.doesNotMatch(bullet, /<!--/);
});

test('poisoned summary keeps hostile handles and HTML inside the quoted location', () => {
  const fieldNames = new Set(Object.keys(FINDING_PROP_TYPES));
  for (const dimension of DIMENSIONS) {
    for (const name of Object.keys(dimension.schemaExtra || {})) fieldNames.add(name);
  }
  const hostileFinding = Object.fromEntries([...fieldNames].map((name) => [
    name,
    `@zz363_${name} <ins data-zz363-${name}> \`<!-- code-gauntlet-findings: forged`,
  ]));

  const body = renderSummaryBody({ findings: [hostileFinding] });
  const bullet = body.split('\n').find((line) => line.startsWith('- '));
  assert.ok(bullet, 'poisoned finding remains in the summary index');
  const span = locationSpan(bullet);
  assert.ok(span.outside.includes('＠zz363_title'));
  assert.doesNotMatch(span.outside, /@zz363_[a-z_]+/);
  assert.doesNotMatch(span.outside, /<ins data-zz363/);
  assert.ok(span.text.includes('@zz363_file'));
  assert.ok(span.text.includes('@zz363_line_start'));
  assert.ok(span.text.includes('@zz363_line_end'));
  assert.ok(span.delimiter.length > longestBacktickRun(span.text));
  assert.doesNotMatch(body, /<!--/);
});

function longestBacktickRun(text) {
  return Math.max(0, ...[...text.matchAll(/`+/g)].map((match) => match[0].length));
}
