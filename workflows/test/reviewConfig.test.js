import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseReviewMd, buildReviewConfig, configForFile, loadExclusions, applyExclusions } from '../src/reviewConfig.js';
import { splitReviewLines, REVIEW_CONFIG_RULES } from '../src/reviewConfig.js';
import { REVIEW_SETTING_KEYS } from '../src/registry.js';
import { finding } from './helpers/findings.js';

const PARSE_ROWS = [
  { name: 'empty review returns only empty ignore', input: '', expected: { ignore: [] } },
  { name: 'rule keys bind each setting explicitly', checkKeys: true,
    input: 'confidence_threshold: 60\nsecurity_min_confidence: 80\nseverity_threshold: high',
    expected: { ignore: [], confidence_threshold: 60, security_min_confidence: 80, severity_threshold: 'high' } },

  {
    name: 'code-gauntlet fenced YAML wins over bare fallback',
    input: 'confidence_threshold: 40\n```yaml\n# code-gauntlet\nconfidence_threshold: 70\nseverity_threshold: high\n' +
      'security_min_confidence: 70\nignore:\n  - pattern one\n  - pattern two\n```\n',
    expected: { confidence_threshold: 70, severity_threshold: 'high', security_min_confidence: 70,
      ignore: ['pattern one', 'pattern two'] },
  },

  {
    name: 'code-gauntlet HTML comment wins over bare fallback',
    input: 'confidence_threshold: 40\n<!-- code-gauntlet-config\nconfidence_threshold: 85\nseverity_threshold: medium\n-->\n',
    expected: { confidence_threshold: 85, severity_threshold: 'medium', ignore: [] },
  },

  {
    name: 'CRLF marker parses as one newline',
    input: 'confidence_threshold: 40\n```yaml\r\n# code-gauntlet\r\nconfidence_threshold: 83\r\n```',
    expected: { confidence_threshold: 83, ignore: [] },
  },

  {
    name: 'copied parser rules change block precedence and numeric keys',
    input: 'confidence_threshold: 40\n<config>confidence_threshold: 72</config>',
    rules: REVIEW_CONFIG_RULES.map((row, index) => index === 0 ? { ...row, pattern: /<config>([^]*?)<\/config>/ } :
      row.key === 'confidence_threshold' ? { ...row, key: 'custom' } : { ...row }),
    expected: { custom: 72, ignore: [] },
  }
];
for (const row of PARSE_ROWS) test(`review parser: ${row.name}`, () => {
  if (row.checkKeys) {
    assert.deepEqual(REVIEW_CONFIG_RULES.filter((rule) => rule.key !== undefined).map((rule) => rule.key).sort(),
      [...REVIEW_SETTING_KEYS].sort());
  }
  assert.deepEqual(parseReviewMd(row.input, row.rules), row.expected);
});

const NEWLINE_ROWS = [
  { name: 'CR before CRLF is two breaks', input: 'a\r\r\nb', expected: ['a', '', 'b'] },

  { name: 'trailing CRLF leaves empty segment', input: 'a\r\n', expected: ['a', ''] },

  { name: 'trailing LF leaves empty segment', input: 'a\n', expected: ['a', ''] },

  { name: 'empty text is one empty segment', input: '', expected: [''] }
];
for (const row of NEWLINE_ROWS) test(`review newline: ${row.name}`, () => {
  assert.deepEqual(splitReviewLines(row.input), row.expected);
});

const BUILD_ROWS = [
  {
    name: 'builder keeps subtree settings and structural order',
    input: [
      { path: 'api/REVIEW.md', text: '```yaml code-gauntlet\nconfidence_threshold: 90\nignore:\n  - api-only\n```' },
      { path: 'REVIEW.md', text: '```yaml code-gauntlet\nconfidence_threshold: 60\nignore:\n  - root-only\n```' },
      { path: 'api/internal/REVIEW.md', text: '```yaml code-gauntlet\nseverity_threshold: high\nignore:\n  - internal-only\n```' },
      { path: 'web/REVIEW.md', text: '```yaml code-gauntlet\nsecurity_min_confidence: 50\n```' },
    ],
    expected: { confidence_threshold: 60, ignore: ['root-only'], scopes: [
      { dir: 'api', confidence_threshold: 90, ignore: ['api-only'] },
      { dir: 'web', security_min_confidence: 50, ignore: [] },
      { dir: 'api/internal', severity_threshold: 'high', ignore: ['internal-only'] },
    ] },
  },

  {
    name: 'builder forwards copied numeric keys to parsing and merging',
    input: [{ path: 'REVIEW.md', text: 'confidence_threshold: 42' },
      { path: 'api/REVIEW.md', text: 'confidence_threshold: 73' }],
    rules: REVIEW_CONFIG_RULES.map((row) => row.key === 'confidence_threshold' ? { ...row, key: 'custom' } : { ...row }),
    expected: { custom: 42, ignore: [], scopes: [{ dir: 'api', custom: 73, ignore: [] }] },
  },

  {
    name: 'same directory merges later settings and keeps equal-depth discovery order',
    input: [
      { path: 'web/REVIEW.md', text: 'confidence_threshold: 61\nignore:\n- web-first' },
      { path: 'api/REVIEW.md', text: 'confidence_threshold: 72' },
      { path: 'web/REVIEW.md', text: 'confidence_threshold: 83\nignore:\n- web-last' },
    ],
    expected: { ignore: [], scopes: [
      { dir: 'web', confidence_threshold: 83, ignore: ['web-first', 'web-last'] },
      { dir: 'api', confidence_threshold: 72, ignore: [] },
    ] },
  }
];
for (const row of BUILD_ROWS) test(`review build: ${row.name}`, () => {
  assert.deepEqual(buildReviewConfig(row.input, row.rules), row.expected);
});

const LOOKUP_ROWS = [
  ...[['prefix boundary', 'apiary/x.py'], ['directory itself', 'api']].map(([name, file]) => ({ name, file,
    input: { confidence_threshold: 60, ignore: ['root-only'],
      scopes: [{ dir: 'api', confidence_threshold: 90, ignore: ['api-only'] }] },
    expected: { confidence_threshold: 60, ignore: ['root-only'] },
  })),

  { name: 'non-string file', file: 42,
    input: { confidence_threshold: 50, ignore: ['root'], scopes: [
      { dir: 'api/internal', confidence_threshold: 80, ignore: ['deep'] },
      { dir: 'api', confidence_threshold: 60, ignore: ['shallow'] },
    ] }, expected: { confidence_threshold: 50, ignore: ['root'] } },

  ...[
    ['case sensitive', 'API', 'api/x.py'], ['non-string directory', 42, '42/x.py'], ['empty directory', '', '/x.py'],
  ].map(([name, dir, file]) => ({ name, file,
    input: { confidence_threshold: 70, ignore: ['root'], scopes: [{ dir, confidence_threshold: 90, ignore: ['scope'] }] },
    expected: { confidence_threshold: 70, ignore: ['root'] },
  })),

  {
    name: 'structural scope ordering and fresh ignore arrays', file: 'api/internal/x.py',
    input: { confidence_threshold: 50, ignore: ['root'], scopes: [
      { dir: 'api/internal', confidence_threshold: 80, ignore: ['deep'] },
      { dir: 'api', confidence_threshold: 60, ignore: ['shallow'] },
    ] },
    expected: { confidence_threshold: 80, ignore: ['root', 'shallow', 'deep'] },
  },

  ...[['diff prefix a', 'a/api/x.py']].map(([name, file]) => ({ name, file,
    input: { confidence_threshold: 70, ignore: ['root'], scopes: [{ dir: 'api', confidence_threshold: 90, ignore: ['api'] }] },
    expected: { confidence_threshold: 70, ignore: ['root'] },
  })),

  {
    name: 'copied lookup keys', file: 'api/x.py', keys: [...REVIEW_SETTING_KEYS, 'custom'],
    input: { custom: 30, scopes: [{ dir: 'api', custom: 90 }] }, expected: { ignore: [], custom: 90 },
  }
];
for (const row of LOOKUP_ROWS) test(`review lookup: ${row.name}`, () => {
  const snapshot = structuredClone(row.input);
  const first = configForFile(row.input, row.file, row.keys);
  const second = configForFile(row.input, row.file, row.keys);
  assert.deepEqual(first, row.expected);
  assert.deepEqual(second, row.expected);
  assert.deepEqual(row.input, snapshot);
  assert.notEqual(first.ignore, second.ignore);
});

const LOAD_ROWS = [
  { name: 'null source', input: null, expected: [] },

  { name: 'fenced lines exclude comments', input: '```text\n# comment\n  literal.*  \n\n```\n- ignored', expected: ['literal.*'] }
];
for (const row of LOAD_ROWS) test(`review exclusions load: ${row.name}`, () => {
  assert.deepEqual(loadExclusions(row.input), row.expected);
});

const EXCLUSION_ROWS = [
  { name: 'empty patterns pass all', input: [{}], patterns: [], expected: { kept: ['f1'], eliminated: [] } },

  {
    name: 'matching title pattern eliminates', input: [{ title: 'Missing test coverage for edge case' }],
    patterns: ['Missing test coverage'],
    expected: { kept: [], eliminated: [['f1', 'exclusion', 'matched exclusion pattern: "Missing test coverage"']] },
  },

  {
    name: 'scoped ignores follow each file', input: [
      { id: 'api-ignore', file: 'api/x.py', description: 'api-match' },
      { id: 'legacy-root', file: 'legacy/x.py', description: 'root-match' },
      { id: 'legacy-keep', file: 'legacy/x.py', description: 'api-match' },
    ], patterns: [], config: { ignore: ['root-match'], scopes: [{ dir: 'api', ignore: ['api-match'] }] },
    expected: { kept: ['legacy-keep'], eliminated: [
      ['api-ignore', 'exclusion', 'matched exclusion pattern: "api-match"'],
      ['legacy-root', 'exclusion', 'matched exclusion pattern: "root-match"'],
    ] },
  }
];
for (const row of EXCLUSION_ROWS) test(`review exclusions: ${row.name}`, () => {
  const input = row.input.map(finding);
  const snapshot = structuredClone(input);
  const out = applyExclusions(input, row.patterns, row.config, row.keys);
  assert.deepEqual({ kept: out.kept.map((item) => item.id),
    eliminated: out.eliminated.map((item) => [item.id, item.eliminated_by, item.elimination_reason]) }, row.expected);
  assert.deepEqual(input, snapshot);
});
