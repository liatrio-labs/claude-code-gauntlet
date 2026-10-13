import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import * as outbound from '../src/outboundText.js';
import { REPORT_FOLD_LIMITS } from '../src/registry.js';
import { corpus } from './helpers/reportFixtures.js';

const commentCases = corpus('outbound_comment_cases').cases;
const lineCases = [
  ...corpus('cross_runtime/outbound_line').cases,
  ...commentCases.filter((row) => ['single_line', 'location'].includes(row.field_class))
    .map((row) => ({ ...row, id: `corpus:${row.id}` })),
];
const LINE_IDS = new Set([
  "empty", "email", "html", "link_destination", "inline_code", "unclosed_code", "unequal_delimiter", "named_reference",
  "literal_comparisons", "literal_underscore_mention", "literal_broken_link", "numeric_ascii_digits", "numeric_non_ascii_digits",
  "tick_markup_bridge", "span_backslash_final_closer", "span_backslash_middle_closer", "span_www_url", "escaped_first_tick",
  "entity_invisible_fixpoint", "entity_comment_fixpoint", "credential_comment_bridge", "span_finding_marker", "span_legacy_marker",
  "image_inline", "image_even_slashes", "image_numeric_bang", "image_comment_bridge", "image_invisible_bridge", "image_inline_code",
  "image_span_destination", "image_redaction_bridge", "reference_definition", "reference_tab", "reference_wide_prefix",
  "reference_footnote", "reference_escaped_label", "reference_even_label_slashes", "reference_escaped_inner_bracket",
  "reference_already_escaped", "reference_escaped_opener", "reference_spaced_colon", "reference_midline", "reference_nondefinition",
  "reference_empty_label", "reference_blank_label", "reference_nested_bracket", "reference_escaped_closer", "reference_inline_code",
  "reference_numeric_bracket", "reference_comment_bridge", "reference_redaction_bridge", "reference_angle_title",
  "reference_astral_limit", "reference_tick_label_limit", "wikilink_bare_opener", "wikilink_escaped_inner_bracket",
  "wikilink_inner_closing_bracket", "wikilink_gitlab_redaction_bridge", "comment_removal_before_decode", "wikilink_inline_code",
  "image_span_boundary", "image_unclosed_span", "wikilink_unclosed_span", "openers_code_backslashes",
  "wikilink_code_definition_fixpoint", "image_backslash_split", "image_named_lbrack", "image_named_excl", "wikilink_named_lbrack",
  "A-mid", "A-escaped-outer", "A-footnote", "A-trigger-escape", "A-trigger-no-space", "A-aliases", "A-bsol", "A-numeric",
  "A-redaction", "A-two", "A-code-control", "A-label-control", "A-link-control", "A-trigger-control-close", "A-trigger-control-space",
  "A-trigger-control-two-slashes", "F-line-control", "D-overlap1", "D-overlap2", "D-late1", "D-late2", "D-restored-overlap", "N1",
  "N2", "D-join", "D-unknown", "D-zero-dec", "D-overflow-dec", "T-suffix", "T-escaped", "nbsp_amp_raw", "nbsp_amp_named",
  "nbsp_amp_long_named", "nbsp_lt_raw", "nbsp_lt_named", "nbsp_gt_raw", "nbsp_gt_long_named", "nbsp_decimal", "nbsp_hex",
  "nbsp_nested_named", "colon_suffix_short", "colon_tick_footnote", "colon_tick_link_title", "colon_tick_angle_destination",
  "colon_tick_bare_url", "colon_tick_www_url", "colon_tick_math", "colon_tick_display_math", "invisible_c0", "invisible_del",
  "invisible_c1", "invisible_soft_hyphen", "invisible_zero_width", "invisible_bom", "invisible_word_joiner",
  "invisible_bidi_embedding", "invisible_bidi_isolates", "invisible_cr", "footnote_opener_backslash", "footnote_opener_basic",
  "footnote_opener_code_span", "footnote_opener_hat_entity", "footnote_opener_nested_bracket", "footnote_opener_spaced_control",
  "footnote_opener_two_line", "footnote_opener_broken_code_span", "footnote_opener_fullwidth_caret_control",
  "corpus:single_line_newlines", "corpus:location_unmatched_tick", "corpus:location_double_ticks", "corpus:unequal_double_single",
  "corpus:fuzz_title_hash_ticks", "corpus:fuzz_title_ins_ticks", "corpus:fuzz_title_newline_ticks", "corpus:fuzz_title_mixed_runs",
]);
assert.equal(lineCases.filter((row) => LINE_IDS.has(row.id)).length, LINE_IDS.size);
for (const row of lineCases.filter((item) => LINE_IDS.has(item.id))) test(`line: ${row.id}`, () => {
  const actual = outbound.prepareLine(row.input);
  assert.equal(actual, row.expected);
  assert.equal(outbound.prepareLine(actual), actual);
  assertLineInvariant(actual);
});

const NORMALIZE_ROWS = [
    ['<!-->tail', '<!-->tail'],
    ['<!--->tail', '<!--->tail'],
    ['<!---->tail', 'tail'],
    ['<!-->x-->y', 'y'],
    ['<!--->x-->y', 'y'],
    ['<!-<!--x-->->tail', '<!-->tail'],
    ['<!--&#38;#45;&#38;#45;&#38;#62;z-->tail', 'z-->tail'],
    ['<!--x--\u200b>y-->z', 'y-->z'],
    ['<!-<!--x-->- y --<!--z-->> w', '> w'],
    ['<!-<!--x-->- a --&#6<!--y-->2; b -->', '2; b -->'],
 ];
for (const [input, expected] of NORMALIZE_ROWS) test(`normalize: ${input}`, () => {
  const actual = outbound.normalizeOutboundText(input);
  assert.equal(actual, expected);
  assert.equal(outbound.normalizeOutboundText(actual), actual);
  assert.equal(hasCompleteNonoverlappingComment(actual), false);
});

function normalizerInvariants(input) {
  const alphabet = ['<!', '--', '>', '&', '#38;', '&#64;', '\u200b', 'x', '\u00a0', '&Hat;', '&nbsp;', '&NonBreakingSpace;'];
  let state = input.seed;
  for (let sample = 0; sample < input.samples; sample += 1) {
    let source = '';
    for (let index = 0; index < input.width; index += 1) {
      state = (state * 1664525 + 1013904223) >>> 0;
      source += alphabet[state % alphabet.length];
    }
    const normalized = outbound.normalizeOutboundText(source);
    assert.equal(outbound.normalizeOutboundText(normalized), normalized, source);
    assert.equal(hasCompleteNonoverlappingComment(normalized), false, source);
    assert.doesNotMatch(normalized, /&#(?:[0-9]+|[xX][0-9a-fA-F]+);/);
    assert.doesNotMatch(normalized, /&(?:commat|excl|lbrack|lsqb|rsqb|rbrack|Hat|colon|lpar|rpar|bsol|nbsp|NonBreakingSpace);/);
    assert.doesNotMatch(normalized, /\u00a0/);
    const forbidden = new RegExp('[\\u0000-\\u0008\\u000b-\\u000d\\u000e-\\u001f\\u007f-\\u009f\\u00ad'
      + '\\u200b-\\u200d\\ufeff\\u2060\\u202a-\\u202e\\u2066-\\u2069]');
    assert.doesNotMatch(normalized, forbidden);
  }
  return true;
}

function hasCompleteNonoverlappingComment(value) {
  let openerEnd = -1;
  for (let index = 0; index < value.length; index += 1) {
    if (openerEnd < 0 && value.startsWith('<!--', index)) {
      openerEnd = index + 4;
      index += 3;
    } else if (openerEnd >= 0 && value.startsWith('-->', index)) {
      if (index >= openerEnd) return true;
      index += 2;
    }
  }
  return false;
}

const LARGE_ROWS = [
    ['bare', '\\'.repeat(500000), '\\'.repeat(500000)],
    ['image_split', `!${'\\'.repeat(500000)}[`, `!${'\\'.repeat(500000)}\uFF3B`],
    ['definition_pairs', `[${'\\a'.repeat(250000)}`, `[${'\\a'.repeat(250000)}`],
    ['definition_openers', `[${'\\a'.repeat(2500)} `.repeat(100), `[${'\\a'.repeat(2500)} `.repeat(100)],
    ['footnote_label', `[^${'[\\a'.repeat(100000)}]: hidden text`, `\uFF3B^${'[\\a'.repeat(100000)}]: hidden text`],
    ['unclosed_footnote', `[^${'[\\a'.repeat(100000)}`, `\uFF3B^${'[\\a'.repeat(100000)}`],


  ['comment-openers', '<!--'.repeat(125000), '&lt;!--'.repeat(125000)],
  ['entity-built-comment-openers', '<&excl;--'.repeat(62500), '&lt;!--'.repeat(62500)],
  ['nested-entities', `&#38;${'#38;'.repeat(124999)}#64;`, '＠'],
  ['comment-deletion-waves', `${'<!'.repeat(60000)}<!-- x -->${'-- y -->'.repeat(60000)}`, ''],
  ['tick-run', `x${'`'.repeat(350000)}`, `x${'\\`'.repeat(350000)}`],
  ['unequal-width-runs',
    `x ${Array.from({ length: 750 }, (_, index) => '`'.repeat(index + 1)).join(' ')}`,
    `x ${Array.from({ length: 750 }, (_, index) => '\\`'.repeat(index + 1)).join(' ')}`],
  ['nbsp-references', `[&${'\u00a0&nbsp;&NonBreakingSpace;'.repeat(10000)}](a b) tail`, `[&${' '.repeat(30000)}](a b) tail`],
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

function rejectsInvariant(input) {
  assert.throws(() => assertLineInvariant(input), assert.AssertionError);
  return true;
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
  { name: 'inline exact cap', api: 'foldInline', input: 'x'.repeat(512), expected: 'x'.repeat(512) },
  { name: 'inline cap plus one', api: 'foldInline', input: 'x'.repeat(513),
    expected: 'x'.repeat(512) + ' [folded: 1 more characters]' },
  { name: 'inline null cap', api: 'foldInline', input: 'abc', limit: null, expected: ' [folded: 3 more characters]' },
  { name: 'inline zero cap', api: 'foldInline', input: 'abc', limit: 0, expected: ' [folded: 3 more characters]' },
  { name: 'prose zero cap', api: 'foldProse', input: 'abc', limit: 0, expected: '\n\n_[folded: 3 more characters]_' },
  { name: 'prose negative cap', api: 'foldProse', input: 'abc', limit: -1, expected: 'ab\n\n_[folded: 1 more characters]_' },
  { name: 'evidence exact char cap', api: 'foldEvidence', input: 'e'.repeat(8000), expected: 'e'.repeat(8000) },
  { name: 'evidence cap plus one', api: 'foldEvidence', input: 'e'.repeat(8001),
    expected: 'e'.repeat(8000) + '\n... [folded: 1 more characters]' },
  { name: 'evidence astral boundary', api: 'foldEvidence', input: 'e'.repeat(7999) + '\u{1F680}x',
    expected: 'e'.repeat(7999) + '\u{1F680}\n... [folded: 1 more characters]' },
  { name: 'evidence exact line cap', api: 'foldEvidence', input: forty, expected: forty },
  { name: 'evidence line cap plus one', api: 'foldEvidence', input: forty + '\nline 41',
    expected: forty + '\n... [folded: 1 more lines]' },
  { name: 'evidence CRLF separators', api: 'foldEvidence', input: 'c\r\n'.repeat(40) + 'c',
    expected: 'c\r\n'.repeat(39) + 'c\n... [folded: 1 more lines]' },
  { name: 'evidence CR separators', api: 'foldEvidence', input: 'r\r'.repeat(40) + 'r',
    expected: 'r\r'.repeat(39) + 'r\n... [folded: 1 more lines]' },
  { name: 'evidence chars beat lines', api: 'foldEvidence', input: 'abc\ndef\nghi',
    limits: { evidenceLines: 2, evidenceChars: 4 }, expected: 'abc\n\n... [folded: 7 more characters]' },
  { name: 'evidence lines beat chars', api: 'foldEvidence', input: 'a\nb\ncdefgh',
    limits: { evidenceLines: 2, evidenceChars: 5 }, expected: 'a\nb\n... [folded: 1 more lines]' },
  { name: 'inline copied limit', api: 'foldInline', input: 'abc', limits: { inlineChars: 2 },
    expected: 'ab [folded: 1 more characters]' },
];
for (const row of FOLD_ROWS) test(`fold: ${row.name}`, () => {
  const limits = row.limits ? { ...REPORT_FOLD_LIMITS, ...row.limits } : undefined;
  const actual = row.api === 'foldEvidence' ? outbound.foldEvidence(row.input, limits)
    : outbound[row.api](row.input, row.limit, limits);
  assert.equal(actual, row.expected);
});

const DISPLAY_ROWS = [
  { name: 'absent', api: 'reportAsText', input: undefined, expected: '' },
  { name: 'null', api: 'reportAsText', input: null, expected: '' },
  { name: 'string', api: 'reportAsText', input: 'plain title', expected: 'plain title' },
  { name: 'number', api: 'reportAsText', input: 42, expected: '42' },
  { name: 'throwing coercion', api: 'reportAsText', input: { toString() { throw new Error('hostile'); } }, expected: '' },
  { name: 'line whitespace', api: 'oneLine', input: '  left\r\n right  ', expected: 'left right' },
  { name: 'span plain', api: 'preparedCodeSpan', input: 'src/a.js', expected: '`src/a.js`' },
  { name: 'span edge ticks', api: 'preparedCodeSpan', input: '`src/``a.js`', expected: '``` `src/``a.js` ```' },
  { name: 'span CR and mentions', api: 'preparedCodeSpan', input: 'left\rright @bot', expected: '`left right @bot`' },
  { name: 'span visible substitution', api: 'preparedCodeSpan', input: 'src/![x]<b>@bot', flag: true,
    expected: '`src/!\uFF3Bx]＜b>＠bot`' },
  { name: 'forced definition colon', api: 'prepareLine', input: 'lead [x]: y', flag: true, expected: 'lead [x]： y' },
  { name: 'title normalizes before fold', api: 'prepareSummaryTitle', input: '<!-- ' + 'z'.repeat(520) + ' --> visible',
    expected: 'visible' },
  { name: 'title fold before containment', api: 'prepareSummaryTitle', input: 'x'.repeat(505) + '<table>TAIL',
    expected: 'x'.repeat(505) + '&lt;table> [folded: 4 more characters]' },
  { name: 'title cut span', api: 'prepareSummaryTitle', input: 'x'.repeat(505) + '`@alice`TAIL',
    expected: 'x'.repeat(505) + '\\`＠alice [folded: 5 more characters]' },
];
for (const row of DISPLAY_ROWS) test(`display: ${row.name}`, () => {
  assert.equal(outbound[row.api](row.input, row.flag), row.expected);
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
  { name: 'normalize: seeded fixpoints', input: { seed: 0x471, samples: 256, width: 16 },
    run: normalizerInvariants, expected: true },
  { name: 'line invariant rejects an overlapping live comment opener', input: '<!-->tail', run: rejectsInvariant, expected: true },
  { name: 'seeded_line_containment', input: { seeds: [1729, 414], samples: 5000, width: 64 },
    run: containmentInvariants, expected: true },
];
for (const row of INVARIANT_ROWS) test(row.name, () => assert.equal(row.run(row.input), row.expected));
