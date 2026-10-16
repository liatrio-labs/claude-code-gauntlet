import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import * as outbound from '../src/outboundText.js';
import { corpus } from './helpers/reportFixtures.js';

const commentCases = corpus('outbound_comment_cases').cases;
const lineCases = [
  ...corpus('cross_runtime/outbound_line').cases,
  ...commentCases.filter((row) => ['single_line', 'location'].includes(row.field_class))
    .map((row) => ({ ...row, id: `corpus:${row.id}` })),
];
const LINE_IDS = new Set([
  "reference_issue", "reference_merge_request", "reference_gh", "reference_label", "reference_milestone",
  "reference_url_protection", "reference_urls", "reference_named_entities", "reference_epic",
  "reference_percent_parenthesized", "reference_backslash",
  "A-aliases", "A-bsol", "A-slashes", "A-trigger-control-close", "D-overflow-dec", "D-overlap2", "D-zero-dec", "N1",
  "comment_removal_before_decode", "email", "entity_comment_fixpoint", "footnote_opener_backslash_run",
  "footnote_opener_hat_entity", "footnote_opener_two_line", "image_inline", "image_named_excl", "image_named_lsqb",
  "invisible_bidi_embedding", "invisible_bidi_isolates", "invisible_bom", "invisible_c0", "invisible_c1", "invisible_cr",
  "invisible_del", "invisible_soft_hyphen", "invisible_word_joiner", "invisible_zero_width", "nbsp_amp_long_named",
  "nbsp_amp_named", "nbsp_amp_raw", "nbsp_hex", "numeric_ascii_digits", "numeric_non_ascii_digits", "reference_blank_label",
  "reference_escaped_label", "reference_escaped_opener", "reference_footnote", "reference_footnote_nested",
  "reference_midline", "reference_nested_bracket", "reference_nondefinition", "reference_spaced_colon", "tick_markup_bridge",
  "wikilink_code_definition_fixpoint", "wikilink_gitlab_redaction_bridge", "wikilink_named_lbrack"
]);
assert.equal(lineCases.filter((row) => LINE_IDS.has(row.id)).length, LINE_IDS.size);
for (const row of lineCases.filter((item) => LINE_IDS.has(item.id))) test(`line: ${row.id}`, () => {
  const actual = outbound.prepareLine(row.input);
  assert.equal(actual, row.expected);
  assert.equal(outbound.prepareLine(actual), actual);
  assertLineInvariant(actual);
});






const LARGE_ROWS = [
  ['nbsp-references', `[&${'\u00a0&nbsp;&NonBreakingSpace;'.repeat(10000)}](a b) tail`, `[&${' '.repeat(30000)}](a b) tail`],
  ['unclosed-destinations', '[`a`]('.repeat(60000), '[`a`]('.repeat(60000)],
];
const childScript = `
import { prepareLine } from './workflows/src/outboundText.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(prepareLine(source));
`;
for (const [name, input, expected] of LARGE_ROWS) test(`large: ${name}`, () => {
  // A deadline catches accidentally quadratic scans at the public boundary.
  const actual = execFileSync(process.execPath, ['--input-type=module', '-e', childScript], {
    input, encoding: 'utf8', timeout: 5000, maxBuffer: 4 * 1024 * 1024,
  });
  assert.equal(actual, expected);
  assert.equal(outbound.prepareLine(actual), actual);
});

function assertLineInvariant(output) {
  assert.doesNotMatch(output, /<!--/);
  assert.doesNotMatch(output, /[!\[]\\*\[/);
  if (/\]\\?\(/.test(output)) {
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



function containmentInvariants(input) {
  const alphabets = [
    '@<&#;`!?/0123456789abcdefghijklmnopqrstuvwxyz \n\r',
    '@<&#;`!?/0123456789abcdefghijklmnopqrstuvwxyz []():~|\\ \n\r',
  ];
  let state = input.seeds[0];
  const next = () => {
    state = Math.imul(state, 1664525) + 1013904223 | 0;
    return state >>> 0;
  };
  const sources = commentCases.map((row) => row.input);
  for (const [corpus, alphabet] of alphabets.entries()) {
    state = input.seeds[corpus];
    for (let index = 0; index < input.samples; index += 1) {
      const length = next() % input.width + 1;
      let source = '';
      for (let at = 0; at < length; at += 1) source += alphabet[next() % alphabet.length];
      sources.push(source);
    }
  }
  sources.push('```\n<!--\n\ncode-gauntlet-findings: poisoned\n```');
  const markupAtoms = [': ', '~ ', '[[', ']]', '|', 'a'.repeat(1000), '[critical]: u', '[[https://example.test/p.png]]', '[', ']: u'];
  for (let index = 0; index < input.samples; index += 1) {
    let source = '';
    for (let at = 0, count = next() % 8 + 1; at < count; at += 1) source += markupAtoms[next() % markupAtoms.length];
    sources.push(source);
  }
  for (const source of sources) {
    const prepared = outbound.prepareLine(source);
    assertLineInvariant(prepared);
    assert.equal(outbound.prepareLine(prepared), prepared);
  }
  return true;
}

const forty = Array.from({ length: 40 }, (_, index) => `line ${index + 1}`).join('\n');
const FOLD_ROWS = [
  { name: 'evidence exact char cap', input: 'e'.repeat(8000), expected: 'e'.repeat(8000) },
  { name: 'evidence exact line cap', input: forty, expected: forty },
  { name: 'evidence line cap plus one', input: forty + '\nline 41',
    expected: forty + '\n... [folded: 1 more lines]' },
];
for (const row of FOLD_ROWS) test(`fold: ${row.name}`, () => {
  assert.equal(outbound.foldEvidence(row.input), row.expected);
});

const DISPLAY_ROWS = [
  { name: 'absent', input: undefined, expected: '' },
  { name: 'null', input: null, expected: '' },
];
for (const row of DISPLAY_ROWS) test(`display: ${row.name}`, () => {
  assert.equal(outbound.reportAsText(row.input), row.expected);
});

const ENTITY_ROWS = [
  { api: 'normalizeOutboundText', input: '&custom;', expected: '@' },
  { api: 'outboundBase', input: '&custom;', expected: '@' },
  { api: 'prepareLine', input: '&custom;', expected: '＠' },
  { api: 'prepareSummaryTitle', input: '&custom;', expected: '＠' },
  { api: 'preparedCodeSpan', input: '&custom;', expected: '`@`' },
];
for (const row of ENTITY_ROWS) test(`injected entities: ${row.api}`, () => {
  const entities = {
    commat: '@', excl: '!', lbrack: '[', lsqb: '[', rsqb: ']', rbrack: ']', Hat: '^', colon: ':',
    lpar: '(', rpar: ')', bsol: '\\', nbsp: ' ', NonBreakingSpace: ' ', custom: '@',
  };
  const actual = ['prepareLine', 'preparedCodeSpan'].includes(row.api)
    ? outbound[row.api](row.input, false, entities) : outbound[row.api](row.input, entities);
  assert.equal(actual, row.expected);
});

const INVARIANT_ROWS = [
  { name: 'seeded_line_containment', input: { seeds: [1729, 414], samples: 5000, width: 64 },
    run: containmentInvariants, expected: true },
];
for (const row of INVARIANT_ROWS) test(row.name, () => assert.equal(row.run(row.input), row.expected));
