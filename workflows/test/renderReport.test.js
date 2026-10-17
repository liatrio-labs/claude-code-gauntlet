import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as report from '../src/renderReport.js';
import { AGENTS, DIMENSIONS } from '../src/registry.js';
import { normalizeArgs, validateArgs } from '../src/args.js';
import { validArgs } from './helpers/argsFixtures.js';
import { corpus, finding, reportInput } from './helpers/reportFixtures.js';

const rendered = (input) => report.renderReport(reportInput(input));
const { dimensionsSummaryTable } = report;
const identity = {
  owner: 'o', repo: 'r', pr_number: 7, sha_full: '0123456789abcdef0123456789abcdef01234567',
  platform: 'github', web_origin: 'https://github.com',
};
const group = { consolidation_key: 'g' };
const primary = finding('P', { ...group, consolidation_primary: true, title: 'Primary' });
const child = finding('C', { ...group, consolidation_primary: false, agent: 'security-reviewer', dimension: 'security',
  title: 'Corroborator', description: 'Corroborator description.' });
const secondPrimary = finding('P2', { ...group, consolidation_primary: true, agent: 'test-analyzer', dimension: 'test_coverage',
  title: 'Second primary', description: 'Second-primary description.' });

function surface(text, row) {
  const lines = text.split('\n');
  if (row.surface === 'count') return lines[lines.indexOf('## Summary') + 2];
  if (row.surface === 'first') return lines[0];
  if (row.surface === 'identity') return lines[2];
  if (row.surface === 'headings') return lines.filter((line) => line.startsWith('### '));
  if (row.surface === 'inventory') return lines.filter((line) => line.startsWith('## '));
  if (row.surface === 'receipt') return text.match(/```text\n([\s\S]*?)\n```/)[1];
  if (row.surface === 'bullets') return lines.filter((line) => line.startsWith('- '));
  if (row.surface === 'firstLine') return lines[0];
  if (row.surface === 'lastLine') return lines.at(-1);
  if (row.start) {
    const start = text.indexOf(row.start);
    assert.ok(start >= 0, row.start);
    const end = text.indexOf(row.end, start + row.start.length);
    assert.ok(end >= 0, row.end);
    return text.slice(start + row.start.length, end);
  }
  if (row.prefix) {
    const selected = lines.filter((line) => line.startsWith(row.prefix));
    return row.all ? selected : row.last ? selected.at(-1) : selected[0];
  }
  return text;
}

const discoveryArgs = validArgs({
  mode: 'interactive', reviewMd: [{ path: 'api/REVIEW.md', text: 'subtree' }], reviewConfigPath: null,
  limits: { deliveryCap: null }, configEcho: reportInput().configEcho,
});
delete discoveryArgs.configEcho.review_md;
const discoveredEcho = normalizeArgs(discoveryArgs).configEcho;

const REPORT_ROWS = [
  { name: 'path arrays retain the whole-field prose fold', input: { findings: [finding('A', {
    cross_file_refs: Array.from({ length: 3000 }, (_, index) => `src/module_${index}/file.js`),
  })] }, start: '- **Cross-file refs:** ', end: '\n\n## Review Dimensions Summary',
    expected: Array.from({ length: 178 }, (_, i) => `src/module_${i}/file.js`)
      .join(',') + ',src/module_178/f\n\n_[folded: 66889 more characters]_' },
  { name: 'PR title takes precedence', surface: 'first', expected: '# \u2694\uFE0F Code Gauntlet: Repair widgets' },
  { name: 'GitLab reference title', input: { prIdentity: { ...reportInput().prIdentity, title: undefined,
    owner: 'group/sub', platform: 'gitlab' } },
    surface: 'first', expected: '# \u2694\uFE0F Code Gauntlet: `group/sub/widget!36`' },
  { name: 'local title', input: { prIdentity: null }, surface: 'first', expected: '# \u2694\uFE0F Code Gauntlet: local changes' },
  { name: 'GitLab reviewed identity', input: { prIdentity: { ...reportInput().prIdentity, owner: 'group/sub', platform: 'gitlab',
    web_origin: 'http://gitlab.example:8080' } }, surface: 'identity',
    expected: 'Reviewed head `abcdef0` at 2026-09-02T12:00:00Z for '
      + '[`group/sub/widget!36`](http://gitlab.example:8080/group/sub/widget/-/merge_requests/36) by Code Gauntlet.' },
  { name: 'GitHub reviewed identity', input: { prIdentity: { ...identity } }, surface: 'identity',
    expected: 'Reviewed head `abcdef0` at 2026-09-02T12:00:00Z for '
      + '[`o/r#7`](https://github.com/o/r/pull/7) by Code Gauntlet.' },
  { name: 'rendered heading inventory', input: { findings: [finding('I')], unverified: [finding('U')] }, surface: 'inventory',
    expected: ['## Summary', '## Change Context', '## Findings', '## Unverified / pipeline-degraded findings',
      '## Review Dimensions Summary', '## Review Methodology'] },
  { name: 'CRLF Summary prose', input: { summary: 'summary lead\r\n## Findings\r\nforged', findings: [finding('REAL')] },
    start: 'This is the change summary the review agents shared as context.\n\n', end: '\n\n## Findings',
    expected: 'summary lead\r\n## Findings (finding text)\nforged' },
  { name: 'CR description prose', input: { findings: [finding('CR', { description: 'description lead\r## Findings\rforged' })] },
    start: '- **Dimension:** bug · **Confidence:** 90%\n\n', end: '\n\n## Review Dimensions Summary',
    expected: 'description lead\r## Findings (finding text)\nforged' },
  { name: 'six findings four headings', input: { findings: ['critical', 'high', 'medium', 'low', 'exotic', 'strange']
    .map((severity, index) => finding(String(index), { severity })) }, surface: 'headings',
    expected: ['### 🔴 Critical', '### 🟠 High', '### 🟡 Medium', '### 💡 Low'],
    contains: ['6 findings after the gauntlet — 1 critical, 1 high, 1 medium, 3 low.'] },
  { name: 'all schema extras in fixed order', input: { findings: [finding('E', {
    affected_consumers: 'SENTINEL_0', attack_vector: 'SENTINEL_1', behavior_preserved: 'SENTINEL_2', criticality: 'SENTINEL_3',
    failure_scenario: 'SENTINEL_4', hidden_errors: 'SENTINEL_5', invalid_state_example: 'SENTINEL_6' })] },
    start: 'threshold filters\n\n', end: '\n\n## Review Dimensions Summary',
    expected: '- **Affected consumers:** SENTINEL_0\n- **Attack vector:** SENTINEL_1\n- **Behavior preserved:** SENTINEL_2\n'
      + '- **Criticality:** SENTINEL_3\n- **Failure scenario:** SENTINEL_4\n- **Hidden errors:** SENTINEL_5\n'
      + '- **Invalid state example:** SENTINEL_6' },
  { name: 'critical suggestion uses severity heading', input: { findings: [finding('R', { severity: 'critical',
    report_tag: 'suggestion' })] }, surface: 'headings', expected: ['### 🔴 Critical'],
    contains: ['- **Routing:** improvement suggestion'], absent: ['## Improvement Suggestions'] },
  { name: 'reachability stamp', input: { findings: [finding('R', { demoted_by: 'reachability', report_tag: 'suggestion' })] },
    prefix: '- **Reachability:', expected: '- **Reachability:** only under a future change (severity demoted to low)' },
  { name: 'consolidation preserves corroborators and raw cross-dimension counts', input: { findings: [primary, child, secondPrimary] },
    prefix: '#### ', all: true, expected: ['#### Primary'], contains: [
      '**Corroborated by** `security-reviewer` (`security`, confidence 90) — Corroborator',
      '  Corroborator description.', '— Second primary', '  Second-primary description.',
      '| Correctness & Error Handling | bug-detector | 1 | 1 high |', '| Security | security-reviewer | 1 | 1 high |',
      '| Test Coverage | test-analyzer | 1 | 1 high |', '1 reported issue from 3 findings after the gauntlet — 1 high.',
    ] },
  { name: 'surfaced origin', input: { findings: [finding('S', { origin: 'surfaced' })] }, prefix: '- **Origin:',
    expected: '- **Origin:** surfaced — pre-existing, surfaced by this change' },
  { name: 'unverified reasons', input: { findings: [finding('M')], unverified: [finding('U1', { origin: 'unknown' }),
    finding('U2', { challenge: 'skipped' }), finding('U3', { origin: 'unknown', challenge: 'skipped' }), finding('U4')] },
    prefix: '- **Unverified because:', all: true, expected: [
      '- **Unverified because:** the challenge cap was reached, so this finding was not challenge-verified',
      '- **Unverified because:** a pipeline stage was skipped or failed',
      '- **Unverified because:** the verify slice could not be proven against the dispatched document',
      '- **Unverified because:** the verify slice could not be proven against the dispatched document; '
        + 'the challenge cap was reached, so this finding was not challenge-verified',
    ] },
  { name: 'interactive receipt', surface: 'receipt', expected: 'Resolved config:\n  model_tier=optimized (fixed)\n'
    + '  pr_comment_cap=null (default)\n  delivery_tier=all (default)\n  review_md=absent (discovery)\n'
    + '  pipeline_version=3.26.0 (bundle)\n  plugin_root=/absolute/plugin (resolved)', ends: 'orchestrator at delivery.' },
  { name: 'present discovery receipt', input: { configEcho: discoveredEcho }, prefix: '  review_md=',
    expected: '  review_md=present (discovery)', absent: ['  review_md=absent (discovery)'], acceptedArgs: discoveryArgs },
  { name: 'gateway schema disabled', input: { policy: { tier: 'optimized', provider: 'firstParty', gateway: true } },
    prefix: '| Provider |', expected: '| Provider | provider=firstParty; gateway=true; conditional schema active=false |' },
  { name: 'empty Change Context', input: { summary: '  ', findings: [finding('T')] },
    start: '## Change Context\n\n', end: '\n\n## Findings', expected: 'No change summary was produced for this run.' },
  { name: 'summary fold cap plus one', input: { summary: 's'.repeat(12001), findings: [finding('T')] },
    start: 'This is the change summary the review agents shared as context.\n\n', end: '\n\n## Findings',
    expected: 's'.repeat(12000) + '\n\n_[folded: 1 more characters]_' },
  { name: 'description folded fence wiring', input: { findings: [finding('FOLD', {
    description: '```\n' + 'x'.repeat(4996), evidence: 'e'.repeat(20000) })] },
    start: '- **Dimension:** bug · **Confidence:** 90%\n\n', end: '\n\n**Evidence:**',
    expected: '```\n' + 'x'.repeat(3996) + '\n```\n\n_[folded: 1000 more characters]_',
    contains: ['... [folded: 12000 more characters]\n```', '\n## Review Methodology\n'] },
];
for (const row of REPORT_ROWS) test(`report: ${row.name}`, () => {
  const input = reportInput(row.input);
  const before = JSON.stringify(input);
  const text = report.renderReport(input);
  assert.deepEqual(surface(text, row), row.expected);
  for (const expected of row.contains ?? []) assert.ok(text.includes(expected), expected);
  for (const absent of row.absent ?? []) assert.ok(!text.includes(absent), absent);
  if (row.ends) assert.ok(text.endsWith(row.ends));
  if (row.acceptedArgs) assert.deepEqual(validateArgs(normalizeArgs(row.acceptedArgs)), { ok: true, errors: [] });
  assert.equal(JSON.stringify(input), before);
});

const OUTSIDE_SUMMARY_ROWS = [
  { name: 'repository path arrays prepare each element before joining', finding: {
    affected_consumers: ['a.js', 'docs/<img src=x onerror=alert(1)>![c](http://evil.example)@team.js'],
    cross_file_refs: ['docs/<img src=x onerror=alert(1)>![c](http://evil.example)@team.js', 'x<!--', 'y-->z', 'b.js'],
  }, start: '- **Affected consumers:** ', end: '\n\n## Review Dimensions Summary',
    expected: 'a.js,docs/&lt;img src=x onerror=alert(1)>!\uFF3Bc](http://evil.example)\uFF20team.js\n\n'
      + '- **Cross-file refs:** '
      + 'docs/&lt;img src=x onerror=alert(1)>!\uFF3Bc](http://evil.example)\uFF20team.js,x&lt;!--,y-->z,b.js' },
  { name: 'folded normalized Location refuses permalink', finding: {
    file: 'a'.repeat(513), line_start: null, line_end: null,
  }, prefix: '- **Location:', expected: '- **Location:** `' + 'a'.repeat(512) + ' [folded: 1 more characters]`' },
  { name: 'finding title keeps words separated across CR', finding: { title: 'left\rright <b> @bot' },
    prefix: '#### ', expected: '#### left right &lt;b> \uFF20bot' },
  { name: 'corroborator title uses the line contract and spans preserve visible bytes', finding: { corroborations: [
    { agent: '`agent`', dimension: '@scope/<b>', confidence: 90, title: '<b> @bot <!-- code-gauntlet-findings: {} -->' },
  ] }, prefix: '- **Corroborated',
    expected: '- **Corroborated by** `` `agent` `` (`@scope/<b>`, confidence 90) \u2014 &lt;b> \uFF20bot' },
  { name: 'scoped npm path text agrees with permalink target', finding: { file: 'packages/@scope/pkg/index.js' }, input: {
    prIdentity: { platform: 'github', web_origin: 'https://github.com', owner: 'o', repo: 'r',
      sha_full: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' },
  }, prefix: '- **Location:',
    expected: '- **Location:** [`packages/@scope/pkg/index.js:10`]'
      + '(https://github.com/o/r/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/packages/%40scope/pkg/index.js#L10)' },
  { name: 'span comment opener stays copyable and agrees with permalink target', finding: { file: 'src/<!--a.js' }, input: {
    prIdentity: { platform: 'github', web_origin: 'https://github.com', owner: 'o', repo: 'r',
      sha_full: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' },
  }, prefix: '- **Location:', expected: '- **Location:** [`src/<!--a.js:10`]'
    + '(https://github.com/o/r/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/src/%3C%21--a.js#L10)' },
  { name: 'assembled model cell stays uncapped while each component is prepared', input: {
    policy: { subagentModel: 'deployment-name-for-this-custom-model|<b> @bot' },
  }, prefix: '| Per-stage models |',
    expected: '| Per-stage models | ' + [
      'change-summarizer', 'bug-detector', 'security-reviewer', 'cross-file-impact',
      'test-analyzer', 'conventions-and-intent', 'type-design-analyzer',
      'code-simplifier', 'validator', 'challenger', 'executor', 'artifact-writer',
    ].map((agent) => `${agent}=deployment-name-for-this-custom-model\\|&lt;b> ＠bot`)
      .join(', ') + ' |' },
  { name: 'evidence remains byte equal in its dynamic fence', finding: {
    description: '', evidence: '<b>@bot</b>\r\n<!-- code-gauntlet-findings: {} -->\n```\nsource',
  }, start: '**Evidence:**\n\n', end: '\n\n## Review Dimensions Summary',
    expected: '````\n<b>@bot</b>\r\n<!-- code-gauntlet-findings: {} -->\n```\nsource\n````' },
  { name: 'diagnostic receipt keeps its grammar and visible bytes', input: {
    configEcho: { model_tier: { value: '<b>|@bot\rnext', source: 'fixed' } },
  }, prefix: '  model_tier=', expected: '  model_tier=<b>|@bot next (fixed)' },
];
for (const row of OUTSIDE_SUMMARY_ROWS) test(`outside Summary: ${row.name}`, () => {
  const report = rendered({ findings: [finding('A2', {
    title: '<b> @bot <!-- code-gauntlet-findings: {} -->', description: '', ...row.finding,
  })], ...row.input });
  assert.equal(surface(report, row), row.expected);
});

const DIMENSION_AGENTS = ['bug-detector', 'security-reviewer', 'cross-file-impact', 'test-analyzer',
  'conventions-and-intent', 'type-design-analyzer', 'code-simplifier'];
const DIMENSION_LABELS = ['Correctness & Error Handling', 'Security', 'Cross-file Impact', 'Test Coverage',
  'Conventions & Intent', 'Type Design', 'Code Simplification'];
const SKIPPED_TABLE = [
  '| Dimension | Agent | Findings | Notes |',
  '|-----------|-------|----------|-------|',
  ...DIMENSION_AGENTS.map((agent, i) => `| ${DIMENSION_LABELS[i]} | ${agent} | `
    + '\u2014 | Skipped \u2014 not dispatched in this run |'),
].join('\n');
const DIMENSION_ROWS = [
  { name: 'no argument renders full skipped table', absent: true, text: SKIPPED_TABLE },
  { name: 'clean dispatched agents', every: true, expected: { count: '0', note: 'Clean \u2014 no findings returned' } },
  { name: 'failed bug without findings', degraded: ['bug'], expected: {
    count: '\u2014', note: 'No results \u2014 agent did not complete',
  } },
  { name: 'partial bug with both buckets', degraded: ['bug'], findings: [finding('B1', { dimension: 'bug', severity: 'high' })],
    unverified: [finding('B2', { dimension: 'bug', severity: 'low' })],
    expected: { count: '1 (+1 unverified)', note: 'Partial \u2014 agent may not have completed' } },
  { name: 'severity breakdown order', agent: 'security-reviewer', findings: [
    finding('S1', { dimension: 'security', severity: 'high' }),
    finding('S2', { dimension: 'security', severity: 'critical' }),
    finding('S3', { dimension: 'security', severity: 'high' }),
  ], expected: { count: '3', note: '1 critical, 2 high' } },
  { name: 'unverified excluded from severity breakdown', agent: 'security-reviewer',
    findings: [finding('S1', { dimension: 'security', severity: 'high' })], unverified: [
      finding('S2', { dimension: 'security', severity: 'low' }), finding('S3', { dimension: 'security', severity: 'low' }),
    ], expected: { count: '1 (+2 unverified)', note: '1 high' } },
  { name: 'only unverified', agent: 'security-reviewer',
    unverified: [finding('S1', { dimension: 'security', severity: 'low' })],
    expected: { count: '0 (+1 unverified)', note: 'Unverified findings only \u2014 see secondary section' } },
  { name: 'multiple dimensions aggregate into one agent', agent: 'conventions-and-intent', findings: [
    finding('C1', { dimension: 'convention', severity: 'medium' }),
    finding('C2', { dimension: 'comment_accuracy', severity: 'medium' }),
  ], expected: { count: '2', note: '2 medium' } },
  { name: 'one failed dimension degrades the shared agent', agent: 'conventions-and-intent', degraded: ['convention'],
    findings: [finding('I1', { dimension: 'intent', severity: 'medium' })],
    expected: { count: '1', note: 'Partial \u2014 agent may not have completed' } },
];
for (const row of DIMENSION_ROWS) test(`dimension table: ${row.name}`, () => {
  const md = dimensionsSummaryTable(row.absent ? undefined : { dispatched: row.dispatched ?? AGENTS,
    degraded: row.degraded ?? [], findings: row.findings ?? [], unverified: row.unverified ?? [] });
  if (row.text) assert.equal(md, row.text);
  const lines = md.split('\n');
  assert.equal(lines[0], '| Dimension | Agent | Findings | Notes |');
  assert.equal(lines[1], '|-----------|-------|----------|-------|');
  const data = lines.slice(2).map((line) => line.split('|').slice(1, -1).map((cell) => cell.trim()));
  assert.equal(data.length, 7);
  assert.deepEqual(data.map((cells) => cells[0]), DIMENSION_LABELS);
  assert.deepEqual(data.map((cells) => cells[1]), DIMENSION_AGENTS);
  if (!row.text) {
    const expected = row.expected;
    const selected = row.every ? data : data.filter((cells) => cells[1] === (row.agent ?? 'bug-detector'));
    assert.equal(selected.length, row.every ? 7 : 1);
    for (const cells of selected) assert.deepEqual(cells.slice(2), [expected.count, expected.note]);
  }
});

const LOCATION_ROWS = [
  { name: 'GitHub range', finding: { file: 'src/a.js', line_start: 10, line_end: 12 },
    expected: '- **Location:** [`src/a.js:10-12`]'
      + '(https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/src/a.js#L10-L12)' },
  { name: 'multiline path refuses link', finding: { file: 'src/a.js\r\nforged' },
    expected: '- **Location:** `src/a.js forged:10`' },
  { name: 'redacted path refuses link', finding: { file: 'src/ghp_' + 'A'.repeat(36) + '.js' },
    expected: '- **Location:** `src/[REDACTED].js:10`' },
  { name: 'reverse range has no anchor', finding: { file: 'src/a.js', line_start: 12, line_end: 10 },
    expected: '- **Location:** [`src/a.js:12-10`]'
      + '(https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/src/a.js)' },
  { name: 'missing platform refuses link', finding: { file: 'src/a.js' }, identity: { platform: undefined },
    expected: '- **Location:** `src/a.js:10`' },
  { name: 'parent segment refuses link', finding: { file: 'src/../a.js' }, expected: '- **Location:** `src/../a.js:10`' },
];
for (const row of LOCATION_ROWS) test(`location: ${row.name}`, () => {
  const text = rendered({ prIdentity: { ...identity, ...row.identity }, findings: [finding('P', row.finding)] });
  assert.equal(surface(text, { prefix: '- **Location:' }), row.expected);
});

const detector = { previously_reviewed: true, sha_resolvable: true, sha_is_ancestor: true, head_advanced: true, error: null };
const SCOPE_ROWS = [
  { name: 'no prior review', detector: { previously_reviewed: false }, expected: 'no prior review recorded' },
  { name: 'unresolvable SHA', detector: { sha_resolvable: false }, expected: 'recorded SHA not resolvable' },
  { name: 'head unchanged', detector: { head_advanced: false }, expected: 'head has not advanced' },
  { name: 'rewritten history', detector: { sha_is_ancestor: false },
    expected: 'history rewritten (recorded SHA is not an ancestor)' },
  { name: 'error beats facts', detector: { previously_reviewed: false, sha_resolvable: false, head_advanced: false,
    sha_is_ancestor: false, error: 'detector unavailable' }, expected: 'detection failed: detector unavailable' },
  { name: 'full scope', input: { requested: 'full', kind: 'full', detector: null }, expected: null },
];
for (const row of SCOPE_ROWS) test(`scope: ${row.name}`, () => {
  const input = row.input ?? { requested: 'incremental', kind: 'full', detector: row.detector === null
    ? null : { ...detector, ...row.detector } };
  assert.equal(report.reviewScopeFallbackReason(input), row.expected);
});

// Shared fixtures also serve direct line tests; retain only independent Summary-path cases here.
const SUMMARY_CASE_IDS = new Set([
  "colon_tick_footnote_summary",
]);
const SUMMARY_ROWS = [
  ...corpus('cross_runtime/outbound_line').summary_cases
    .filter((row) => SUMMARY_CASE_IDS.has(row.id)).map((row) => ({ name: row.id, input: row.input,
    expected: row.expected_js })),
  { name: 'two suggestions first line', input: { findings: [finding('A', { report_tag: 'suggestion' }),
    finding('B', { report_tag: 'suggestion' })] }, surface: 'firstLine',
    expected: '2 findings after the gauntlet — 2 high. 2 routed as improvement suggestions.' },
  { name: 'code mentions', input: { findings: [finding('OUT', { title: 'code `@inside <tag>` and @outside' })] },
    surface: 'lastLine', expected: '- 🟠 [HIGH] `OUT.js:10`: code `＠inside ＜tag>` and ＠outside' },
  { name: 'crossing HTML tick', input: { findings: [finding('OUT', { file: 'src/a<`b.py', line_start: 1, line_end: 1 })] },
    surface: 'lastLine', expected: '- 🟠 [HIGH] ``src/a＜`b.py:1``: finding OUT' },
  { name: 'escaped image location', input: { findings: [finding('OUT', { file: 'src/!\\[a](u).py', title: '!\\[a](u)',
    line_start: 1, line_end: 1 })] }, surface: 'lastLine', expected: '- 🟠 [HIGH] `src/!\\［a](u).py:1`: !\\［a](u)' },
  { name: 'empty findings headline', input: { findings: [] }, surface: 'firstLine',
    expected: '0 findings after the gauntlet.' },
];
const tilde = corpus('outbound_text').cases.find((row) => row.id === 'tilde_odd');
SUMMARY_ROWS.push({ name: tilde.id, input: { findings: [finding('OUT', {
  file: 'a.py', line_start: 3, line_end: 3, title: tilde.input,
})] }, surface: 'lastLine', expected: '- 🟠 [HIGH] `a.py:3`: ' + tilde.expected });
for (const row of SUMMARY_ROWS) test(`summary: ${row.name}`, () => {
  const actual = report.renderSummaryBody(row.input);
  assert.deepEqual(surface(actual, row), row.expected);
});

test('contested finding stamp renders only for true', () => {
  const contested = rendered({ findings: [finding('CONTESTED', { challenge_contested: true })] });
  const uncontested = rendered({ findings: [finding('UNCONTESTED', { challenge_contested: false })] });
  const bullet = '- **Contested:** the challenger could not confirm the cited location';
  assert.ok(contested.includes(bullet));
  assert.ok(!uncontested.includes(bullet));
});

test('spec text without a claude.md rule uses the fallback cited rule label', () => {
  const text = rendered({ findings: [finding('SPEC', { dimension: 'intent', spec_text: 'The specification text.',
    rule_source: 'repo_precedent' })] });
  assert.ok(text.includes('**Cited rule:**\n\n> The specification text.'));
  assert.ok(!text.includes('Repo precedent'));
});

test('schema extras include a newly declared field and exclude fix code', () => {
  const dimensions = [...DIMENSIONS, {
    ...DIMENSIONS[0], schemaExtra: { custom_field: { type: 'string' }, suggested_fix_code: { type: 'string' } },
  }];
  assert.deepEqual(report.reportExtraFields(dimensions), ['affected_consumers', 'attack_vector', 'behavior_preserved', 'criticality', 'custom_field',
    'failure_scenario', 'hidden_errors', 'invalid_state_example']);
});

const first = finding('duplicate', { title: 'first title', file: 'first.js' });
const middle = finding('M', { severity: 'high', report_tag: 'main' });
const suggestion = finding('S', { severity: 'low', report_tag: 'suggestion' });
const rankedPrimary = finding('P', { ...group, consolidation_primary: true, severity: 'critical' });
const lowChild = finding('C', { ...group, consolidation_primary: false, severity: 'low', report_tag: 'suggestion' });
const UNITS_ROWS = [
  { name: 'withheld primary uses first delivered child', input: { findings: [finding('P', { ...group,
    consolidation_primary: true, confidence: 10 }), finding('A', { ...group, consolidation_primary: false,
    title: 'lower rank', confidence: 60 }), finding('B', { ...group, consolidation_primary: false,
    title: 'higher rank', confidence: 95 })] }, selected: [2, 1], surface: 'bullets',
    expected: ['- 🟠 [HIGH] `B.js:10`: higher rank'], absent: ['not listed here', 'lower rank'] },
  { name: 'partial group order', input: { findings: [rankedPrimary, lowChild, middle, suggestion],
    delivered: [lowChild, middle], deliveryCap: 2 }, surface: 'bullets', expected: [
      '- 💡 [LOW] `C.js:10` (improvement suggestion): finding C', '- 🟠 [HIGH] `M.js:10`: finding M',
    ], starts: '3 reported issues from 4 findings after the gauntlet — 1 critical, 1 high, 1 low. '
      + '1 routed as improvement suggestion.',
    ends: '1 more reported issue not listed here (over the delivery cap of 2 findings).' },
  { name: 'delivered primary wins over child', input: { findings: [rankedPrimary, lowChild, middle, suggestion],
    delivered: [lowChild, rankedPrimary, middle, suggestion] }, surface: 'bullets', expected: [
      '- 🔴 [CRITICAL] `P.js:10`: finding P', '- 🟠 [HIGH] `M.js:10`: finding M',
      '- 💡 [LOW] `S.js:10` (improvement suggestion): finding S',
    ], absent: ['not listed', 'finding C'] },
  { name: 'tier and cap reasons', input: { findings: [middle, suggestion], delivered: [], deliveryTier: 'main_only',
    deliveryCap: 0 }, surface: 'lastLine', expected: '2 more findings not listed here (over the delivery cap of 0 findings; '
      + 'improvement suggestions held back by delivery tier main_only).' },
  { name: 'delivered findings cut by Summary length', input: (() => {
    const findings = Array.from({ length: 100 }, (_, index) => finding(`L${index}`, { title: 'x'.repeat(300) }));
    return { findings, delivered: findings, deliveryCap: 100 };
  })(), surface: 'lastLine', reason: 'over the summary length limit' },
  { name: 'main only cap does not claim suggestions were withheld', input: {
    findings: [middle, finding('M2', { report_tag: 'main' })], delivered: [middle], deliveryTier: 'main_only', deliveryCap: 1,
  }, surface: 'lastLine', expected: '1 more finding not listed here (over the delivery cap of 1 finding).' },
  { name: 'main only reports an actually withheld suggestion', input: {
    findings: [middle, suggestion], delivered: [middle], deliveryTier: 'main_only', deliveryCap: 2,
  }, surface: 'lastLine', expected: '1 more finding not listed here (improvement suggestions held back by delivery tier main_only).' },
  { name: 'tier only reason', input: { findings: [suggestion], delivered: [], deliveryTier: 'main_only', deliveryCap: 6 },
    surface: 'lastLine', expected: '1 more finding not listed here (improvement suggestions held back by delivery tier main_only).' },
  { name: 'destination alias', input: { findings: [finding('S', { severity: 'low', report_tag: undefined,
    report_destination: 'suggestion' })] }, surface: 'lastLine',
    expected: '- 💡 [LOW] `S.js:10` (improvement suggestion): finding S' },
  { name: 'ordinary remainder many', input: { findings: [first, finding('duplicate',
    { file: 'second.js' }), middle], delivered: [first], deliveryCap: null },
    surface: 'lastLine', expected: '2 more findings not listed here (not selected for delivery).' },
];
for (const row of UNITS_ROWS) test(`units: ${row.name}`, () => {
  const input = row.selected ? { ...row.input, delivered: row.selected.map((index) => row.input.findings[index]) } : row.input;
  const text = report.renderSummaryBody(input);
  if (row.reason) assert.equal(surface(text, row).match(/\((.*)\)\.$/)?.[1], row.reason);
  else assert.deepEqual(surface(text, row), row.expected);
  if (row.starts) assert.ok(text.startsWith(row.starts));
  if (row.ends) assert.ok(text.endsWith(row.ends));
  for (const absent of row.absent ?? []) assert.ok(!text.includes(absent), absent);
});

const BUDGET_ROWS = [
  { name: 'exact budget', tailChars: 351, expected: { count: 94, chars: 12000 }, absent: 'not listed here' },
  { name: 'stop at first oversized bullet', tailChars: 352, extra: true, expected: { count: 93, chars: 11624 },
    ends: '2 more findings not listed here (over the summary length limit).', absent: ': z\n' },
];
for (const row of BUDGET_ROWS) test(`budget: ${row.name}`, () => {
  const findings = [...Array.from({ length: 93 }, (_, index) => finding(String(index).padStart(3, '0'), { title: 'x'.repeat(100) })),
    finding('093', { title: 'z'.repeat(row.tailChars) })];
  if (row.extra) findings.push(finding('ZZZ', { title: 'z', severity: 'low' }));
  const input = { findings };
  const text = report.renderSummaryBody(input);
  const bullets = text.split('\n').filter((line) => line.startsWith('- '));
  assert.deepEqual({ count: bullets.length, chars: [...bullets.join('\n')].length }, row.expected);
  if (row.ends) assert.ok(text.endsWith(row.ends));
  if (row.absent) assert.ok(!text.includes(row.absent));
});

const RECEIPT_ROWS = [
  { name: 'headless receipt', input: { mode: 'headless', configEcho: {
    model_tier: { value: 'optimized', source: 'env' }, delivery: { value: 'pr_comments,markdown', source: 'env' },
    post_mode: { value: 'dry-run', source: 'env' }, pr_comment_cap: { value: '25', source: 'env' },
    delivery_tier: { value: 'main_only', source: 'env' }, draft_policy: { value: 'review', source: 'env' },
    reviewed_policy: { value: 'full', source: 'env' }, pr_not_found_policy: { value: 'error', source: 'env' },
    trivial_scope: { value: 'full', source: 'env' },
  }, pluginRoot: '/absolute/path/to/claude-code-gauntlet', deliveryTier: 'main_only', deliveryCap: 25 },
    surface: 'receipt', expected: 'Headless config:\n  model_tier=optimized (env)\n  delivery=pr_comments,markdown (env)\n'
      + '  post_mode=dry-run (env)\n  pr_comment_cap=25 (env)\n  delivery_tier=main_only (env)\n  draft_policy=review (env)\n'
      + '  reviewed_policy=full (env)\n  pr_not_found_policy=error (env)\n  trivial_scope=full (env)\n'
      + '  pipeline_version=3.26.0 (bundle)\n  plugin_root=/absolute/path/to/claude-code-gauntlet (resolved)' },
  { name: 'first-party model IDs', input: { policy: { provider: 'firstParty' } }, prefix: '| Per-stage models |',
    expected: '| Per-stage models | change-summarizer=claude-sonnet-5-5, bug-detector=claude-sonnet-5-5, '
      + 'security-reviewer=claude-opus-5-5, cross-file-impact=claude-sonnet-5-5, test-analyzer=claude-sonnet-5-5, '
      + 'conventions-and-intent=claude-sonnet-5-5, type-design-analyzer=claude-sonnet-5-5, code-simplifier=claude-sonnet-5-5, '
      + 'validator=claude-sonnet-5-5, challenger=claude-sonnet-5-5, executor=claude-sonnet-5-5, artifact-writer=claude-sonnet-5-5 |' },
  { name: 'provider model aliases', input: { policy: { provider: 'bedrock' } }, prefix: '| Per-stage models |',
    expected: '| Per-stage models | change-summarizer=sonnet, bug-detector=sonnet, security-reviewer=opus, '
      + 'cross-file-impact=sonnet, test-analyzer=sonnet, conventions-and-intent=sonnet, type-design-analyzer=sonnet, '
      + 'code-simplifier=sonnet, validator=sonnet, challenger=sonnet, executor=sonnet, artifact-writer=sonnet |' },
  { name: 'pipeline counts and table escaping', input: { reviewScope: { kind: 'incremental', since: 'abc-1', commits: null },
    policy: { subagentModel: 'model|override' }, stats: { discovered: 4, validate: { accepted: 3, rejected: 1 },
      filter: { accepted: 2, rejected: 1 }, challenge: { accepted: 1, rejected: 1 }, merge: {
        findings_per_channel: { ndjson: 3, text_fallback: 1 }, duplicates_resolved: 1, dropped_no_id: 1,
        truncation_warnings: 1, validation_warnings: 2,
      } }, gapCount: 2 }, prefix: '| Findings pipeline |',
    expected: '| Findings pipeline | discovered=4; validate: accepted=3, rejected=1; filter: accepted=2, rejected=1; '
      + 'challenge: accepted=1, rejected=1; merge: per-channel: ndjson=3, text_fallback=1; duplicates resolved=1; '
      + 'dropped-no-id=1; truncation warnings=1; validation warnings=2 |',
    contains: ['| Subagent model override | EVERY stage uses model\\|override |',
      '| Review scope | Incremental since abc-1 (commits unknown) |', '| Gaps | 2 |'] },
];
for (const row of RECEIPT_ROWS) test(`methodology: ${row.name}`, () => {
  const text = rendered(row.input);
  assert.equal(surface(text, row), row.expected);
  for (const expected of row.contains ?? []) assert.ok(text.includes(expected), expected);
});

const METHODOLOGY_ROWS = [
  { name: 'mapped alias changes first-party dispatch', input: {
    policy: { provider: 'firstParty', mappedAliases: ['sonnet', 'haiku'] },
  }, expected: ['| User tier mapping | sonnet, haiku |', 'bug-detector=sonnet, security-reviewer=claude-opus-5-5'] },
  { name: 'run facts with full fallback and degraded agent', input: {
    pipelineVersion: '3.26.0', deliveryTier: 'main_only', deliveryCap: null,
    reviewScope: { requested: 'incremental', kind: 'full', since: null, commits: null,
      detector: { previously_reviewed: true, sha_resolvable: true, sha_is_ancestor: true,
        head_advanced: false, incremental_safe: false, error: null } },
    dimensions: { dispatched: AGENTS, degraded: ['bug'] },
  }, expected: [
    '| Version | 3.26.0 (plugin and pipeline) |',
    '| Delivery selection | selection policy: tier=main_only, cap=uncapped |',
    '| Review scope | Full (incremental requested; head has not advanced) |',
    '| Dimensions | dispatched=7; degraded=1 |',
  ] },
  { name: 'default full scope', input: {}, expected: ['| Review scope | Full |'] },
  { name: 'incremental scope commit count', input: {
    reviewScope: { requested: 'incremental', kind: 'incremental', since: 'abc123', commits: 3, detector: null },
  }, expected: ['| Review scope | Incremental since abc123 (3 commits) |'] },
];
for (const row of METHODOLOGY_ROWS) test(`methodology facts: ${row.name}`, () => {
  const text = rendered(row.input);
  for (const expected of row.expected) assert.ok(text.includes(expected), expected);
});

for (const policy of [{ mappedAliases: [] },
  { provider: 'bedrock', mappedAliases: ['sonnet'] }]) {
  test(`methodology omits ineffective mapping: ${JSON.stringify(policy)}`, () => {
    assert.equal(rendered({ policy }).includes('| User tier mapping |'), false);
  });
}

const RULE_ROWS = [
  ['code_comment', '**Cited comment:**'],
  ['repo_precedent', '**Repo precedent:**'],
  ['self_inconsistency', '**Inconsistency:**'],
  ['constructor', '**Cited rule:**'],
];
for (const [input, expected] of RULE_ROWS) test(`rule source: ${input}`, () => {
  const text = rendered({ findings: [finding('RS', { claude_md_rule: 'The cited text.', rule_source: input })] });
  assert.equal(text.split('\n').find((line) => /^\*\*(?:Cited|Repo|Inconsistency)/.test(line)), expected);
  assert.ok(!text.includes(input));
});

const f4 = '````py\nkeep';
const FENCE_ROWS = [
  { name: 'context fence wiring', input: { summary: f4 + '\nDROP'.repeat(3000), findings: [finding('A')] },
    start: 'This is the change summary the review agents shared as context.\n\n', end: '\n\n## Findings',
    expected: '````py\nkeep' + '\nDROP'.repeat(2397) + '\n````\n\n_[folded: 3015 more characters]_' },
  { name: 'suggestion fence wiring', input: { findings: [finding('A', { description: '', suggestion: f4 + '\nDROP'.repeat(1000) })] },
    start: '**Suggested fix:**\n\n', end: '\n\n## Review Dimensions Summary',
    expected: '````py\nkeep' + '\nDROP'.repeat(797) + '\n````\n\n_[folded: 1015 more characters]_' },
  { name: 'quoted rule fence wiring', input: { findings: [finding('A', { description: '', claude_md_rule: f4 + '\nDROP'.repeat(1000) })] },
    start: '**Cited rule:**\n\n', end: '\n\n## Review Dimensions Summary',
    expected: '> ````py\n> keep' + '\n> DROP'.repeat(797) + '\n> ````\n> \n> _[folded: 1015 more characters]_' },
  { name: 'corroborator fence wiring', input: { findings: [finding('A', { description: '', corroborations: [{ agent: 'a',
    dimension: 'security', confidence: 80, title: 'x', description: f4 + '\nDROP'.repeat(1000) }] })] },
    start: '- **Corroborated by** `a` (`security`, confidence 80) — x\n', end: '\n\n## Review Dimensions Summary',
    expected: '  ````py\n  keep' + '\n  DROP'.repeat(797) + '\n  ````\n  \n  _[folded: 1015 more characters]_' },
];
for (const row of FENCE_ROWS) test(`fence: ${row.name}`, () => assert.equal(surface(rendered(row.input), row), row.expected));

const HEADINGS = ['## Summary', '## Change Context', '## Findings', '## Unverified / pipeline-degraded findings',
  '## Review Dimensions Summary', '## Review Methodology'];
const FORGERY_ROWS = [
  ['## Summary', '## Summary (finding text)'],
];
for (const [input, expected] of FORGERY_ROWS) test(`forgery: ${input}`, () => {
  const text = rendered({ summary: 'summary\n' + input, findings: [finding('I', { title: 'title',
    description: 'description\n' + input, suggestion: 'suggestion\n' + input, evidence: 'evidence\n' + input })],
    unverified: [finding('U', { description: 'unverified' })] });
  assert.deepEqual(text.replace(/```[\s\S]*?```/g, '').split('\n').filter((line) => line.startsWith('## ')
    && !line.endsWith('(finding text)')), HEADINGS);
  assert.equal(text.split('\n').filter((line) => line === expected).length, 3);
  assert.ok(text.includes('evidence\n' + input + '\n```'));
});

const a = finding('A', { title: 'same', severity: 'low', report_tag: 'suggestion', file: 'src/a b.js', line_start: 7, line_end: 9 });
const b = finding('B', { title: 'same', severity: 'critical', file: 'b.js' });
const c = finding('C', { title: 'same', severity: 'high' });
const LINK_ROWS = [
  { name: 'GitHub Summary range', input: { findings: [a], prIdentity: { platform: 'github', web_origin: 'https://gitlab.com',
    owner: 'g/sub', repo: 'r', sha_full: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' } }, surface: 'lastLine',
    expected: '- 💡 [LOW] [`src/a b.js:7-9`]'
      + '(https://gitlab.com/g/sub/r/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/src/a%20b.js#L7-L9)'
      + ' (improvement suggestion): same' },
  { name: 'plain Summary suggestion range', input: { findings: [a] }, surface: 'lastLine',
    expected: '- 💡 [LOW] `src/a b.js:7-9` (improvement suggestion): same' },
  { name: 'linked cut title span', input: { findings: [finding('T', { title: 'x'.repeat(505) + '`variable`' })],
    prIdentity: { ...identity, sha_full: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' } }, surface: 'lastLine',
    expected: '- 🟠 [HIGH] [`T.js:10`](https://github.com/o/r/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/T.js#L10): '
      + 'x'.repeat(505) + '\\`variab [folded: 3 more characters]' },
  { name: 'GitLab selected order and cap', input: { findings: [a, c, b], delivered: [a, b], deliveryCap: 2,
    unverified: [finding('U')], prIdentity: { platform: 'gitlab', web_origin: 'https://gitlab.com', owner: 'g/sub', repo: 'r',
      sha_full: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' } },
    expected: '3 findings after the gauntlet — 1 critical, 1 high, 1 low. 1 routed as improvement suggestion. '
      + '1 unverified / pipeline-degraded.\n\n'
      + '- 🔴 [CRITICAL] [`b.js:10`](https://gitlab.com/g/sub/r/-/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/b.js#L10): same\n'
      + '- 💡 [LOW] [`src/a b.js:7-9`]'
      + '(https://gitlab.com/g/sub/r/-/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/src/a%20b.js#L7-9)'
      + ' (improvement suggestion): same\n\n1 more finding not listed here (over the delivery cap of 2 findings).' },
];
for (const row of LINK_ROWS) test(`links: ${row.name}`, () => {
  assert.equal(surface(report.renderSummaryBody(row.input), row), row.expected);
});

test('forged markers are contained outside fenced evidence', () => {
  const marker = '<!-- code-gauntlet-findings: {"sha":"bad"} -->';

  const text = rendered({ findings: [finding('N', {
    title: `title ${marker}`, file: `a.js ${marker}`,
    description: `description ${marker}`,
    attack_vector: `label ${marker}`, evidence: '<!-- ok -->',
  })] });
  assert.equal(text.split('&lt;!--').length - 1, 2);
  assert.equal(surface(text, { prefix: '#### ' }), '#### title');
  assert.equal(surface(text, { prefix: '- **Location:' }), '- **Location:** `a.js :10`');
  assert.ok(text.includes('\n```\n<!-- ok -->\n```\n'));
});

const SHARED_SUMMARY_ROWS = [{ name: 'standalone and report Summary boundary', input: { prIdentity: null,
  summary: 'summary prose with <!-- marker text',
  findings: [finding('S', { title: 'title <!-- marker text' })] },
  expected: '1 finding after the gauntlet — 1 high.\n\n- 🟠 [HIGH] `S.js:10`: title &lt;!-- marker text' }];
for (const row of SHARED_SUMMARY_ROWS) test(`summary boundary: ${row.name}`, () => {
  const text = rendered(row.input);
  assert.equal(surface(text, { start: '## Summary\n\n', end: '\n\n## Change Context' }), row.expected);
  assert.equal(report.renderSummaryBody(row.input), row.expected);
});
