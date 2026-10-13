import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as report from '../src/renderReport.js';
import { AGENTS, DIMENSIONS, FINDING_PROP_TYPES, SEVERITY_ORDER } from '../src/registry.js';
import { KNOB_REGISTRY } from '../src/knobs.js';
import { normalizeArgs, validateArgs } from '../src/args.js';
import { validArgs } from './helpers/argsFixtures.js';
import { corpus, finding, reportInput } from './helpers/reportFixtures.js';

const rendered = (input, knobs) => report.renderReport(reportInput(input), knobs);
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
  { name: 'PR title takes precedence', surface: 'first', expected: '# \u2694\uFE0F Code Gauntlet: Repair widgets' },
  { name: 'GitLab reference title', input: { prIdentity: { ...reportInput().prIdentity, title: undefined,
    owner: 'group/sub', platform: 'gitlab' } },
    surface: 'first', expected: '# \u2694\uFE0F Code Gauntlet: `group/sub/widget!36`' },
  { name: 'local title', input: { prIdentity: null }, surface: 'first', expected: '# \u2694\uFE0F Code Gauntlet: local changes' },
  { name: 'GitLab reviewed identity', input: { prIdentity: { ...reportInput().prIdentity, owner: 'group/sub', platform: 'gitlab',
    web_origin: 'http://gitlab.example:8080' } }, surface: 'identity',
    expected: 'Reviewed head `abcdef0` at 2026-09-02T12:00:00Z for '
      + '[`group/sub/widget!36`](http://gitlab.example:8080/group/sub/widget/-/merge_requests/36) by Code Gauntlet.' },
  { name: 'identity without head', input: { prIdentity: null, headShaShort: null }, surface: 'identity',
    expected: 'Reviewed at 2026-09-02T12:00:00Z by Code Gauntlet.' },
  { name: 'identity without timestamp', input: { prIdentity: null, generatedAt: null }, surface: 'identity',
    expected: 'Reviewed head `abcdef0` by Code Gauntlet.' },
  { name: 'identity without metadata', input: { prIdentity: null, generatedAt: null, headShaShort: null }, surface: 'identity',
    expected: 'Reviewed by Code Gauntlet.' },
  { name: 'rendered heading inventory', input: { findings: [finding('I')], unverified: [finding('U')] }, surface: 'inventory',
    expected: ['## Summary', '## Change Context', '## Findings', '## Unverified / pipeline-degraded findings',
      '## Review Dimensions Summary', '## Review Methodology'] },
  { name: 'code-owned heading source inventory', input: { findings: [finding('I')], unverified: [finding('U')] },
    surface: 'inventory', sourceHeadings: true,
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
  { name: 'sparse severity heading', input: { findings: [finding('L', { severity: 'low' })] }, surface: 'headings',
    expected: ['### 💡 Low'] },
  { name: 'closed labels and nonmutation', input: { findings: [finding('LONG', { severity: 'x'.repeat(70000) }),
    finding('TRIMMED', { severity: 'HIGH ' }), finding('NEWLINE', { severity: 'high\nfoo' }),
    finding('PRIMARY', { ...group, consolidation_primary: true, severity: 3 }),
    finding('CORROBORATOR', { ...group, consolidation_primary: false, severity: 'medium' })],
    unverified: [finding('ARRAY', { severity: ['high'] })] }, surface: 'count',
    expected: '4 reported issues from 5 findings after the gauntlet — 1 high, 3 low. 1 unverified / pipeline-degraded.',
    absent: ['x'.repeat(70000), 'foo', '### 🟡 Medium'] },
  { name: 'missing severities omit dimensions notes', input: { findings: [finding('A', { dimension: 'security', severity: undefined }),
    finding('N', { dimension: 'security', severity: null }), finding('E', { dimension: 'security', severity: '' })] },
    prefix: '| Security |', expected: '| Security | security-reviewer | 3 |  |' },
  { name: 'dynamic evidence fence', input: { findings: [finding('F', { evidence: 'before ```` after',
    suggestion: 'the section after the fence' })] }, start: '**Evidence:**\n\n', end: '\n\n**Suggested fix:**',
    expected: '`````\nbefore ```` after\n`````' },
  { name: 'all schema extras in fixed order', input: { findings: [finding('E', {
    affected_consumers: 'SENTINEL_0', attack_vector: 'SENTINEL_1', behavior_preserved: 'SENTINEL_2', criticality: 'SENTINEL_3',
    failure_scenario: 'SENTINEL_4', hidden_errors: 'SENTINEL_5', invalid_state_example: 'SENTINEL_6' })] },
    start: 'threshold filters\n\n', end: '\n\n## Review Dimensions Summary',
    expected: '- **Affected consumers:** SENTINEL_0\n- **Attack vector:** SENTINEL_1\n- **Behavior preserved:** SENTINEL_2\n'
      + '- **Criticality:** SENTINEL_3\n- **Failure scenario:** SENTINEL_4\n- **Hidden errors:** SENTINEL_5\n'
      + '- **Invalid state example:** SENTINEL_6' },
  { name: 'spec fallback ignores rule source', input: { findings: [finding('RS', { spec_text: 'The specification text.',
    rule_source: 'repo_precedent' })] }, prefix: '**Cited', expected: '**Cited rule:**', absent: ['Repo precedent', 'repo_precedent'] },
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
  { name: 'contested true', input: { findings: [finding('C', { challenge_contested: true })] }, prefix: '- **Contested:',
    expected: '- **Contested:** the challenger could not confirm the cited location' },
  { name: 'unverified reasons', input: { findings: [finding('M')], unverified: [finding('U1', { origin: 'unknown' }),
    finding('U2', { challenge: 'skipped' }), finding('U3', { origin: 'unknown', challenge: 'skipped' }), finding('U4')] },
    prefix: '- **Unverified because:', all: true, expected: [
      '- **Unverified because:** the challenge cap was reached, so this finding was not challenge-verified',
      '- **Unverified because:** a pipeline stage was skipped or failed',
      '- **Unverified because:** the verify slice could not be proven against the dispatched document',
      '- **Unverified because:** the verify slice could not be proven against the dispatched document; '
        + 'the challenge cap was reached, so this finding was not challenge-verified',
    ] },
  { name: 'dimensions table before methodology', input: { findings: [primary, child] }, surface: 'inventory',
    expected: ['## Summary', '## Change Context', '## Findings', '## Review Dimensions Summary', '## Review Methodology'],
    contains: ['| Code Simplification | code-simplifier | 0 | Clean — no findings returned |\n\n## Review Methodology'],
    ends: 'orchestrator at delivery.' },
  { name: 'dimensions heading then table header', input: { findings: [finding('D')] },
    start: '## Review Dimensions Summary\n', end: '\n| Correctness',
    expected: '\n| Dimension | Agent | Findings | Notes |\n|-----------|-------|----------|-------|' },
  { name: 'one count', input: { findings: [finding('C', { severity: 'critical' })] }, surface: 'count',
    expected: '1 finding after the gauntlet — 1 critical.' },
  { name: 'suggestion count', input: { findings: [finding('S', { severity: 'low', report_tag: 'suggestion' })] }, surface: 'count',
    expected: '1 finding after the gauntlet — 1 low. 1 routed as improvement suggestion.' },
  { name: 'unverified count', input: { unverified: [finding('U', { severity: 'medium' })] }, surface: 'count',
    expected: '0 findings after the gauntlet. 1 unverified / pipeline-degraded.' },
  { name: 'interactive receipt', surface: 'receipt', expected: 'Resolved config:\n  model_tier=optimized (fixed)\n'
    + '  pr_comment_cap=null (default)\n  delivery_tier=all (default)\n  review_md=absent (discovery)\n'
    + '  pipeline_version=3.26.0 (bundle)\n  plugin_root=/absolute/plugin (resolved)', ends: 'orchestrator at delivery.' },
  { name: 'missing mode receipt fallback', input: { mode: undefined, configEcho: {
    model_tier: { value: 'optimized', source: 'fixed' }, pr_comment_cap: null }, pluginRoot: undefined, pipelineVersion: null },
    surface: 'receipt', expected: 'Resolved config:\n  model_tier=optimized (fixed)\n  pr_comment_cap=unknown (unknown)\n'
      + '  delivery_tier=unknown (unknown)\n  review_md=unknown (unknown)\n  pipeline_version=unknown (bundle)\n'
      + '  plugin_root=unknown (resolved)', absent: ['Headless config:'] },
  { name: 'present discovery receipt', input: { configEcho: discoveredEcho }, prefix: '  review_md=',
    expected: '  review_md=present (discovery)', absent: ['  review_md=absent (discovery)'], acceptedArgs: discoveryArgs },
  { name: 'receipt cannot close fence', input: { configEcho: { ...reportInput().configEcho,
    model_tier: { value: 'optimized\n``` forged', source: 'fixed' } } }, prefix: '  model_tier=',
    expected: '  model_tier=optimized  forged (fixed)' },
  { name: 'gateway schema disabled', input: { policy: { tier: 'optimized', provider: 'firstParty', gateway: true } },
    prefix: '| Provider |', expected: '| Provider | provider=firstParty; gateway=true; conditional schema active=false |' },
  { name: 'integer gaps only', input: { gapCount: 1.5 }, prefix: '| Gaps |', expected: '| Gaps | 0 |' },
  { name: 'string gaps are not counted', input: { gapCount: '2' }, prefix: '| Gaps |', expected: '| Gaps | 0 |' },
  { name: 'null gaps are not counted', input: { gapCount: null }, prefix: '| Gaps |', expected: '| Gaps | 0 |' },
  { name: 'array gaps are not counted', input: { gapCount: [] }, prefix: '| Gaps |', expected: '| Gaps | 0 |' },
  { name: 'empty Change Context', input: { summary: '  ', findings: [finding('T')] },
    start: '## Change Context\n\n', end: '\n\n## Findings', expected: 'No change summary was produced for this run.' },
  { name: 'summary exact fold cap', input: { summary: 's'.repeat(12000), findings: [finding('T')] },
    start: 'This is the change summary the review agents shared as context.\n\n', end: '\n\n## Findings',
    expected: 's'.repeat(12000) },
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
  const text = report.renderReport(input, row.knobs);
  assert.deepEqual(surface(text, row), row.expected);
  for (const expected of row.contains ?? []) assert.ok(text.includes(expected), expected);
  for (const absent of row.absent ?? []) assert.ok(!text.includes(absent), absent);
  if (row.ends) assert.ok(text.endsWith(row.ends));
  if (row.acceptedArgs) assert.deepEqual(validateArgs(normalizeArgs(row.acceptedArgs)), { ok: true, errors: [] });
  if (row.sourceHeadings) {
    const source = readFileSync(new URL('../src/renderReport.js', import.meta.url), 'utf8');
    const headings = [...source.matchAll(/builder\.add\((['"])(## .*?)\1\)/g)].map((match) => match[2]);
    assert.deepEqual([...headings].sort(), [...row.expected].sort());
  }
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
  { name: 'normalized PR reference refuses permalink', input: {
    prIdentity: { ...identity, owner: 'glpat\u002Daaaaaaaaaaaaaaaaaaaa', platform: 'gitlab', web_origin: 'https://gitlab.com' },
  }, prefix: 'Reviewed ', expected: 'Reviewed head `abcdef0` at 2026-09-02T12:00:00Z for '
    + '`[REDACTED]/r!7` by Code Gauntlet.' },
  { name: 'folded normalized Location refuses permalink', finding: {
    file: 'a'.repeat(513), line_start: null, line_end: null,
  }, prefix: '- **Location:', expected: '- **Location:** `' + 'a'.repeat(512) + ' [folded: 1 more characters]`' },
  { name: 'corroborator agent folds after normalization', finding: { corroborations: [
    { agent: '<!-- cut -->' + 'a'.repeat(513), dimension: 'bug', confidence: 90, title: 'support' },
  ] }, prefix: '- **Corroborated', expected: '- **Corroborated by** `' + 'a'.repeat(512)
    + ' [folded: 1 more characters]` (`bug`, confidence 90) \u2014 support' },
  { name: 'corroborator dimension folds after normalization', finding: { corroborations: [
    { agent: 'agent', dimension: '<!-- cut -->' + 'd'.repeat(513), confidence: 90, title: 'support' },
  ] }, prefix: '- **Corroborated', expected: '- **Corroborated by** `agent` (`' + 'd'.repeat(512)
    + ' [folded: 1 more characters]`, confidence 90) \u2014 support' },
  { name: 'finding title keeps words separated across CR', finding: { title: 'left\rright <b> @bot' },
    prefix: '#### ', expected: '#### left right &lt;b> \uFF20bot' },
  { name: 'finding title normalizes before folding',
    finding: { title: `<!-- ${'z'.repeat(520)} --> visible` }, prefix: '#### ',
    expected: '#### visible' },
  { name: 'unverified title uses the line contract', input: {
    unverified: [finding('U', { title: '<b> @bot <!-- code-gauntlet-findings: {} -->' })],
  }, prefix: '#### ', last: true, expected: '#### &lt;b> \uFF20bot' },
  { name: 'PR title uses the line contract', input: { prIdentity: { title: '<b> @bot <!-- code-gauntlet-findings: {} -->' } },
    prefix: '# ', expected: '# \u2694\uFE0F Code Gauntlet: &lt;b> \uFF20bot' },
  { name: 'metadata uses the line contract', input: { generatedAt: '<b> @bot <!-- code-gauntlet-findings: {} -->' },
    prefix: 'Reviewed ', expected: 'Reviewed head `abcdef0` at &lt;b> \uFF20bot for '
      + '[`acme/widget#36`](https://github.com/acme/widget/pull/36) by Code Gauntlet.' },
  { name: 'classification uses the line contract', finding: { dimension: '<b> @bot <!-- code-gauntlet-findings: {} -->' },
    prefix: '- **Dimension:', expected: '- **Dimension:** &lt;b> \uFF20bot \u00B7 **Confidence:** 90%' },
  { name: 'corroborator title uses the line contract and spans preserve visible bytes', finding: { corroborations: [
    { agent: '`agent`', dimension: '@scope/<b>', confidence: 90, title: '<b> @bot <!-- code-gauntlet-findings: {} -->' },
  ] }, prefix: '- **Corroborated',
    expected: '- **Corroborated by** `` `agent` `` (`@scope/<b>`, confidence 90) \u2014 &lt;b> \uFF20bot' },
  { name: 'file-only backtick run uses dynamic padded delimiters', finding: { file: '`src/``a.js`', line_start: null },
    input: { prIdentity: null }, prefix: '- **Location:', expected: '- **Location:** ``` `src/``a.js` ```' },
  { name: 'location backtick run uses dynamic delimiters and a matching permalink',
    finding: { file: 'src/``a.js', line_start: 10 }, input: { prIdentity: {
      platform: 'github', web_origin: 'https://github.com', owner: 'o', repo: 'r', sha_full: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    } }, prefix: '- **Location:',
    expected: '- **Location:** [```src/``a.js:10```]'
      + '(https://github.com/o/r/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/src/%60%60a.js#L10)' },
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
  { name: 'identity uses padded dynamic spans without visible substitution', input: {
    prIdentity: { owner: '`@scope`', repo: '<b>', pr_number: 7 },
  }, prefix: '# ', expected: '# \u2694\uFE0F Code Gauntlet: `` `@scope`/<b>#7 ``' },
  { name: 'head identity uses padded dynamic spans', input: { headShaShort: '`@head`' }, prefix: 'Reviewed ',
    expected: 'Reviewed head `` `@head` `` at 2026-09-02T12:00:00Z for '
      + '[`acme/widget#36`](https://github.com/acme/widget/pull/36) by Code Gauntlet.' },
  { name: 'version cell prepares its repository component', input: { pipelineVersion: '<b>|@bot' }, prefix: '| Version |',
    expected: '| Version | &lt;b>\\|\uFF20bot (plugin and pipeline) |' },
  { name: 'provider cell prepares its component', input: { policy: { provider: '<b>|@bot' } }, prefix: '| Provider |',
    expected: '| Provider | provider=&lt;b>\\|\uFF20bot; gateway=false; conditional schema active=false |' },
  { name: 'model override cell prepares and escapes its component', input: { policy: { subagentModel: '<b>|@bot' } },
    prefix: '| Subagent model override |', expected: '| Subagent model override | EVERY stage uses &lt;b>\\|\uFF20bot |' },
  { name: 'assembled model cell stays uncapped while each component is prepared', input: {
    policy: { subagentModel: 'deployment-name-for-this-custom-model|<b> @bot' },
  }, prefix: '| Per-stage models |',
    expected: '| Per-stage models | change-summarizer=deployment-name-for-this-custom-model\\|&lt;b> \uFF20bot, '
      + 'bug-detector=deployment-name-for-this-custom-model\\|&lt;b> \uFF20bot, '
      + 'security-reviewer=deployment-name-for-this-custom-model\\|&lt;b> \uFF20bot, '
      + 'cross-file-impact=deployment-name-for-this-custom-model\\|&lt;b> \uFF20bot, '
      + 'test-analyzer=deployment-name-for-this-custom-model\\|&lt;b> \uFF20bot, '
      + 'conventions-and-intent=deployment-name-for-this-custom-model\\|&lt;b> \uFF20bot, '
      + 'type-design-analyzer=deployment-name-for-this-custom-model\\|&lt;b> \uFF20bot, '
      + 'code-simplifier=deployment-name-for-this-custom-model\\|&lt;b> \uFF20bot, '
      + 'validator=deployment-name-for-this-custom-model\\|&lt;b> \uFF20bot, '
      + 'challenger=deployment-name-for-this-custom-model\\|&lt;b> \uFF20bot, '
      + 'executor=deployment-name-for-this-custom-model\\|&lt;b> \uFF20bot, '
      + 'artifact-writer=deployment-name-for-this-custom-model\\|&lt;b> \uFF20bot |' },
  { name: 'delivery cell prepares the tier component', input: { deliveryTier: '<b>|@bot' }, prefix: '| Delivery selection |',
    expected: '| Delivery selection | selection policy: tier=&lt;b>\\|\uFF20bot, cap=uncapped |' },
  { name: 'incremental cell prepares repository since and commits components', input: {
    reviewScope: { kind: 'incremental', since: '<b>|@bot', commits: '<i>|@other' },
  }, prefix: '| Review scope |', expected: '| Review scope | Incremental since &lt;b>\\|\uFF20bot (&lt;i>\\|\uFF20other commits) |' },
  { name: 'fallback cell prepares detector error', input: {
    reviewScope: { kind: 'full', requested: 'incremental', detector: { error: '<b>|@bot\rnext' } },
  }, prefix: '| Review scope |',
    expected: '| Review scope | Full (incremental requested; detection failed: &lt;b>\\|\uFF20bot next) |' },
  { name: 'pipeline cell prepares count keys and values and path phases once', input: { stats: {
    discovered: '<b>|@bot', validate: { '<i>|@key': '<u>|@value' }, filter: { '<i>|@key': 2 },
    challenge: { '<i>|@key': ['a', 'b', 'c'] }, pathNormalization: { '<i>|@phase': { '<u>|@key': '<b>|@value' } },
    merge: { findings_per_channel: { ndjson: '<b>|@n', text_fallback: '<b>|@t' }, duplicates_resolved: '<b>|@d',
      dropped_no_id: '<b>|@i', truncation_warnings: '<b>|@w', validation_warnings: '<b>|@v' },
  } }, prefix: '| Findings pipeline |',
    expected: '| Findings pipeline | discovered=&lt;b>\\|\uFF20bot; validate: &lt;i>\\|\uFF20key=&lt;u>\\|\uFF20value; '
      + 'filter: &lt;i>\\|\uFF20key=2; challenge: &lt;i>\\|\uFF20key=3; merge: per-channel: '
      + 'ndjson=&lt;b>\\|\uFF20n, text_fallback=&lt;b>\\|\uFF20t; duplicates resolved=&lt;b>\\|\uFF20d; '
      + 'dropped-no-id=&lt;b>\\|\uFF20i; truncation warnings=&lt;b>\\|\uFF20w; validation warnings=&lt;b>\\|\uFF20v; '
      + '&lt;i>\\|\uFF20phase paths: &lt;u>\\|\uFF20key=&lt;b>\\|\uFF20value |' },
  { name: 'description keeps HTML and CR as local prose', finding: { description: '<b>@bot</b>\rnext\n```\nopen' },
    start: '- **Dimension:** bug \u00B7 **Confidence:** 90%\n\n', end: '\n\n## Review Dimensions Summary',
    expected: '<b>@bot</b>\rnext\n```\nopen' },
  { name: 'Change Context keeps HTML and open fences as local prose', input: { summary: '<b>@bot</b>\rnext\n```\nopen' },
    start: 'This is the change summary the review agents shared as context.\n\n', end: '\n\n## Findings',
    expected: '<b>@bot</b>\rnext\n```\nopen' },
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
  const lines = report.split('\n');
  let actual;
  if (row.prefix) {
    const matching = lines.filter((line) => line.startsWith(row.prefix));
    actual = row.last ? matching.at(-1) : matching[0];
  } else {
    const start = report.indexOf(row.start);
    assert.ok(start >= 0);
    const end = report.indexOf(row.end, start + row.start.length);
    assert.ok(end >= 0);
    actual = report.slice(start + row.start.length, end);
  }
  assert.equal(actual, row.expected);
});

const DIMENSION_AGENTS = ['bug-detector', 'security-reviewer', 'cross-file-impact', 'test-analyzer',
  'conventions-and-intent', 'type-design-analyzer', 'code-simplifier'];
const DIMENSION_LABELS = ['Correctness & Error Handling', 'Security', 'Cross-file Impact', 'Test Coverage',
  'Conventions & Intent', 'Type Design', 'Code Simplification'];
const SKIPPED_TABLE = '| Dimension | Agent | Findings | Notes |\n|-----------|-------|----------|-------|\n| '
  + 'Correctness & Error Handling | bug-detector | \u2014 | Skipped \u2014 not dispatched in this run |\n| Security | '
  + 'security-reviewer | \u2014 | Skipped \u2014 not dispatched in this run |\n| Cross-file Impact | cross-file-impact '
  + '| \u2014 | Skipped \u2014 not dispatched in this run |\n| Test Coverage | test-analyzer | \u2014 | Skipped \u2014 '
  + 'not dispatched in this run |\n| Conventions & Intent | conventions-and-intent | \u2014 | Skipped \u2014 not '
  + 'dispatched in this run |\n| Type Design | type-design-analyzer | \u2014 | Skipped \u2014 not dispatched in this '
  + 'run |\n| Code Simplification | code-simplifier | \u2014 | Skipped \u2014 not dispatched in this run |';
const DIMENSION_ROWS = [
  { name: 'no argument renders full skipped table', absent: true, text: SKIPPED_TABLE },
  { name: 'clean dispatched agents', every: true, expected: { count: '0', note: 'Clean \u2014 no findings returned' } },
  { name: 'failed bug without findings', degraded: ['bug'], expected: {
    count: '\u2014', note: 'No results \u2014 agent did not complete',
  } },
  { name: 'partial bug with both buckets', degraded: ['bug'], findings: [finding('B1', { dimension: 'bug', severity: 'high' })],
    unverified: [finding('B2', { dimension: 'bug', severity: 'low' })],
    expected: { count: '1 (+1 unverified)', note: 'Partial \u2014 agent may not have completed' } },
  { name: 'partial bug without unverified suffix', degraded: ['bug'],
    findings: [finding('B1', { dimension: 'bug', severity: 'high' })],
    expected: { count: '1', note: 'Partial \u2014 agent may not have completed' } },
  { name: 'severity breakdown order', agent: 'security-reviewer', findings: [
    finding('S1', { dimension: 'security', severity: 'high' }),
    finding('S2', { dimension: 'security', severity: 'critical' }),
    finding('S3', { dimension: 'security', severity: 'high' }),
  ], expected: { count: '3', note: '1 critical, 2 high' } },
  { name: 'unverified excluded from severity breakdown', agent: 'security-reviewer',
    findings: [finding('S1', { dimension: 'security', severity: 'high' })], unverified: [
      finding('S2', { dimension: 'security', severity: 'low' }), finding('S3', { dimension: 'security', severity: 'low' }),
    ], expected: { count: '1 (+2 unverified)', note: '1 high' } },
  { name: 'closed severity labels with throwing fields', agent: 'security-reviewer', findings: [
    finding('S1', { dimension: 'security', severity: 'Critical' }),
    finding('S2', { dimension: 'security', severity: ' medium ' }),
    finding('S3', { dimension: 'security', severity: 'bogus' }),
    finding('S4', { dimension: 'security', severity: ['high'] }),
    finding('S5', { dimension: 'security', severity: 'high\nx' }),
    finding('S6', { dimension: 'security', severity: '' }),
    finding('S7', { dimension: 'security', severity: undefined }),
    finding('S8', { dimension: 'security', severity: 0 }),
    finding('S9', { dimension: 'security', severity: false }),
    Object.defineProperty(finding('S10', { dimension: 'security' }), 'severity', { get() { throw new Error('boom'); } }),
    Object.defineProperty(finding('S11'), 'dimension', { get() { throw new Error('boom'); } }),
  ], expected: { count: '10', note: '1 critical, 1 medium, 5 low' } },
  { name: 'absent severity leaves empty note', agent: 'security-reviewer',
    findings: [finding('S1', { dimension: 'security', severity: undefined })], expected: { count: '1', note: '' } },
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
  { name: 'unknown and absent dimensions count nowhere', every: true, findings: [
    finding('X1', { dimension: 'not-a-real-dimension' }), finding('X2', { dimension: undefined }),
  ], expected: { count: '0', note: 'Clean \u2014 no findings returned' } },
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
  { name: 'multiline path refuses link', finding: { file: 'src/a.js\r\nforged' },
    expected: '- **Location:** `src/a.js forged:10`' },
  { name: 'comment-normalized path refuses link', finding: { file: 'src/<!-- x -->a.js' },
    expected: '- **Location:** `src/a.js:10`' },
  { name: 'entity-normalized path refuses link', finding: { file: 'src/&#64;a.js' },
    expected: '- **Location:** `src/@a.js:10`' },
  { name: 'redacted path refuses link', finding: { file: 'src/ghp_' + 'A'.repeat(36) + '.js' },
    expected: '- **Location:** `src/[REDACTED].js:10`' },
  { name: 'reverse range has no anchor', finding: { file: 'src/a.js', line_start: 12, line_end: 10 },
    expected: '- **Location:** [`src/a.js:12-10`]'
      + '(https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/src/a.js)' },
  { name: 'exponent string has no anchor', finding: { file: 'src/a.js', line_start: '1e1', line_end: '12' },
    expected: '- **Location:** [`src/a.js:1e1-12`]'
      + '(https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/src/a.js)' },
  { name: 'missing platform refuses link', finding: { file: 'src/a.js' }, identity: { platform: undefined },
    expected: '- **Location:** `src/a.js:10`' },
  { name: 'surrogate repair in URL', finding: { file: 'src/\uD800.js' },
    expected: '- **Location:** [`src/\uD800.js:10`]'
      + '(https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/src/%EF%BF%BD.js#L10)' },
  { name: 'empty segment refuses link', finding: { file: 'src//a.js' }, expected: '- **Location:** `src//a.js:10`' },
  { name: 'dot segment refuses link', finding: { file: 'src/./a.js' }, expected: '- **Location:** `src/./a.js:10`' },
  { name: 'parent segment refuses link', finding: { file: 'src/../a.js' }, expected: '- **Location:** `src/../a.js:10`' },
];
for (const row of LOCATION_ROWS) test(`location: ${row.name}`, () => {
  const text = rendered({ prIdentity: { ...identity, ...row.identity }, findings: [finding('P', row.finding)] });
  assert.equal(surface(text, { prefix: '- **Location:' }), row.expected);
});

const SEVERITY_ROWS = [];
for (const [name, input, expected] of SEVERITY_ROWS) test(`severity: ${name}`, () => {
  assert.equal(report.normalizeReportSeverity(input), expected);
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
  { name: 'error fold cap', detector: { error: 'x'.repeat(130) + '\nmore' }, expected: 'detection failed: ' + 'x'.repeat(120) },
  { name: 'unknown detector', detector: {}, expected: 'detection failed: unavailable' },
  { name: 'full scope', input: { requested: 'full', kind: 'full', detector: null }, expected: null },
];
for (const row of SCOPE_ROWS) test(`scope: ${row.name}`, () => {
  const input = row.input ?? { requested: 'incremental', kind: 'full', detector: row.detector === null
    ? null : { ...detector, ...row.detector } };
  assert.equal(report.reviewScopeFallbackReason(input), row.expected);
});

// Shared fixtures also serve direct line tests; retain only independent Summary-path cases here.
const SUMMARY_CASE_IDS = new Set([
  "A-title-summary-src_bxbc_py-linked",
  "colon_tick_footnote_summary",
  "tilde_interior_linked",
]);
const SUMMARY_ROWS = [
  { name: 'joined comment delimiters normalize once', input: { findings: [finding('O1', {
    title: 'location', file: 'src/a.js', line_start: '<!-', line_end: 'x-->',
  })] }, expected: '1 finding after the gauntlet \u2014 1 high.\n\n- \uD83D\uDFE0 [HIGH] `src/a.js:\uFF1C!--x-->`: location' },
  { name: 'joined GitLab token normalizes once', input: { findings: [finding('O1', {
    title: 'location', file: 'src/a.js', line_start: 'glpat', line_end: 'AAAAAAAAAAAAAAAAAAAA',
  })] }, expected: '1 finding after the gauntlet \u2014 1 high.\n\n'
    + '- \uD83D\uDFE0 [HIGH] `src/a.js:glpat\u002DAAAAAAAAAAAAAAAAAAAA`: location' },
  { name: 'joined file and line comment normalizes once', input: { findings: [finding('O1', {
    title: 'location', file: 'a<!--', line_start: 'b-->c', line_end: null,
  })] }, expected: '1 finding after the gauntlet \u2014 1 high.\n\n- \uD83D\uDFE0 [HIGH] `a\uFF1C!--:b-->c`: location' },
  ...corpus('cross_runtime/outbound_line').summary_cases
    .filter((row) => SUMMARY_CASE_IDS.has(row.id)).map((row) => ({ name: row.id, input: row.input,
    expected: row.expected_js })),
  { name: 'no argument', input: undefined, noArgument: true, expected: '0 findings after the gauntlet.' },
  { name: 'two suggestions first line', input: { findings: [finding('A', { report_tag: 'suggestion' }),
    finding('B', { report_tag: 'suggestion' })] }, surface: 'firstLine',
    expected: '2 findings after the gauntlet — 2 high. 2 routed as improvement suggestions.' },
  { name: 'summary omits PR or MR wording', input: {
    summary: 'The PR and MR mention a pull request and merge request.', findings: [finding('T')],
  }, expected: '1 finding after the gauntlet — 1 high.\n\n- 🟠 [HIGH] `T.js:10`: finding T',
  forbidden: /\b(?:PR|MR|pull request|merge request)\b/ },
  { name: 'code mentions', input: { findings: [finding('OUT', { title: 'code `@inside <tag>` and @outside' })] },
    surface: 'lastLine', expected: '- 🟠 [HIGH] `OUT.js:10`: code `＠inside ＜tag>` and ＠outside' },
  { name: 'HTML fold boundary', input: { findings: [finding('OUT', { title: 'x'.repeat(505) + '<table>TAIL' })] },
    surface: 'lastLine', expected: '- 🟠 [HIGH] `OUT.js:10`: ' + 'x'.repeat(505) + '&lt;table> [folded: 4 more characters]' },
  { name: 'crossing HTML tick', input: { findings: [finding('OUT', { file: 'src/a<`b.py', line_start: 1, line_end: 1 })] },
    surface: 'lastLine', expected: '- 🟠 [HIGH] ``src/a＜`b.py:1``: finding OUT' },
  { name: 'escaped image location', input: { findings: [finding('OUT', { file: 'src/!\\[a](u).py', title: '!\\[a](u)',
    line_start: 1, line_end: 1 })] }, surface: 'lastLine', expected: '- 🟠 [HIGH] `src/!\\［a](u).py:1`: !\\［a](u)' },
  { name: 'missing location', input: { findings: [finding('X', { file: '' })] }, surface: 'lastLine',
    expected: '- 🟠 [HIGH] `location unavailable`: finding X' },
];
// Keep the selected tilde cases that exercise the rendered Summary path.
const SUMMARY_TILDE_IDS = new Set([
  "tilde_even",
  "tilde_fixed",
  "tilde_interior_linked",
  "tilde_odd",
  "tilde_short_control",
  "tilde_suffix_code",
]);
for (const row of corpus('outbound_text').cases.filter((item) => SUMMARY_TILDE_IDS.has(item.id))) {
  SUMMARY_ROWS.push({ name: row.id, input: { findings: [finding('OUT', { file: 'a.py', line_start: 3, line_end: 3,
    severity: 'high', title: row.input })] }, surface: 'lastLine', expected: '- 🟠 [HIGH] `a.py:3`: ' + row.expected });
}
for (const row of SUMMARY_ROWS) test(`summary: ${row.name}`, () => {
  const actual = row.noArgument ? report.renderSummaryBody() : report.renderSummaryBody(row.input);
  assert.deepEqual(surface(actual, row), row.expected);
  if (row.forbidden) assert.doesNotMatch(actual, row.forbidden);
});

const PLURAL_ROWS = [
  [11, 'findings'],
];
for (const [input, expected] of PLURAL_ROWS) test(`plural: ${input}`, () => assert.equal(report.plural(input, 'finding'), expected));

const COPY_ROWS = [
  { name: 'severity table', api: 'normalizeReportSeverity', input: 'CUSTOM', tables: [...SEVERITY_ORDER, 'custom'],
    expected: 'custom' },
  { name: 'coercion forwards severity table', api: 'coerceReportFindings', input: [{ severity: 'CUSTOM' }],
    tables: [...SEVERITY_ORDER, 'custom'], expected: [{ severity: 'custom' }] },
  { name: 'extra schema table', api: 'reportExtraFields', tables: [...DIMENSIONS, {
    ...DIMENSIONS[0], schemaExtra: { custom_field: { type: 'string' }, suggested_fix_code: { type: 'string' } },
  }], expected: ['affected_consumers', 'attack_vector', 'behavior_preserved', 'criticality', 'custom_field',
    'failure_scenario', 'hidden_errors', 'invalid_state_example'] },
  { name: 'scope table', api: 'reviewScopeFallbackReason', input: { requested: 'incremental', kind: 'full',
    detector: { previously_reviewed: false } }, tables: report.REVIEW_SCOPE_FALLBACK_RULES.map((row) => row.reason
      ? { ...row, reason: 'custom reason' } : { ...row }), expected: 'custom reason' },
  { name: 'receipt table', api: 'renderReport', input: reportInput({ configEcho: { custom: { value: 'literal', source: 'fixed' } } }),
    tables: KNOB_REGISTRY.map((row) => ({ ...row, key: 'custom', modes: ['headless'] })).concat({
      ...KNOB_REGISTRY[0], key: 'custom', modes: ['interactive'],
    }), surface: 'receipt', expected: 'Resolved config:\n  custom=literal (fixed)\n  pipeline_version=3.26.0 (bundle)\n'
      + '  plugin_root=/absolute/plugin (resolved)' },
  { name: 'dimension table', api: 'dimensionsSummaryTable', input: { dispatched: [AGENTS[0]], findings: [finding('A')] },
    tables: [{ ...DIMENSIONS[0], dimension: 'custom' }], expected: '| Dimension | Agent | Findings | Notes |\n'
      + '|-----------|-------|----------|-------|\n'
      + '| Correctness & Error Handling | bug-detector | 0 | Clean — no findings returned |' },
];
for (const row of COPY_ROWS) test(`copied table: ${row.name}`, () => {
  const actual = row.api === 'reportExtraFields' ? report.reportExtraFields(row.tables)
    : report[row.api](row.input, row.tables);
  assert.deepEqual(row.surface ? surface(actual, row) : actual, row.expected);
});

const first = finding('duplicate', { title: 'first title', file: 'first.js' });
const second = finding('duplicate', { title: 'second title', file: 'second.js' });
const middle = finding('M', { severity: 'high', report_tag: 'main' });
const suggestion = finding('S', { severity: 'low', report_tag: 'suggestion' });
const rankedPrimary = finding('P', { ...group, consolidation_primary: true, severity: 'critical' });
const lowChild = finding('C', { ...group, consolidation_primary: false, severity: 'low', report_tag: 'suggestion' });
const nullPrototype = Object.assign(Object.create(null), finding('N', { title: 'null prototype' }));
const UNITS_ROWS = [
  { name: 'group key does not collide with id', input: { findings: [primary, child, finding('g', { title: 'ordinary finding' })],
    delivered: [child] }, surface: 'bullets', expected: ['- 🟠 [HIGH] `C.js:10`: Corroborator'],
    ends: '1 more reported issue not listed here (not selected for delivery).' },
  { name: 'withheld primary uses first delivered child', input: { findings: [finding('P', { ...group,
    consolidation_primary: true, confidence: 10 }), finding('A', { ...group, consolidation_primary: false,
    title: 'lower rank', confidence: 60 }), finding('B', { ...group, consolidation_primary: false,
    title: 'higher rank', confidence: 95 })] }, selected: [2, 1], surface: 'bullets',
    expected: ['- 🟠 [HIGH] `B.js:10`: higher rank'], absent: ['not listed here', 'lower rank'] },
  { name: 'null prototype delivered', input: { findings: [nullPrototype], delivered: [nullPrototype] }, surface: 'bullets',
    expected: ['- 🟠 [HIGH] `N.js:10`: null prototype'], absent: ['not listed here'] },
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
  { name: 'tier only reason', input: { findings: [suggestion], delivered: [], deliveryTier: 'main_only', deliveryCap: 6 },
    surface: 'lastLine', expected: '1 more finding not listed here (improvement suggestions held back by delivery tier main_only).' },
  { name: 'destination alias', input: { findings: [finding('S', { severity: 'low', report_tag: undefined,
    report_destination: 'suggestion' })] }, surface: 'lastLine',
    expected: '- 💡 [LOW] `S.js:10` (improvement suggestion): finding S' },
  { name: 'destination alias count', input: { findings: [finding('S', { severity: 'low', report_tag: undefined,
    report_destination: 'suggestion' })] }, surface: 'firstLine',
    expected: '1 finding after the gauntlet — 1 low. 1 routed as improvement suggestion.' },
  { name: 'main destination cap zero', input: { findings: [finding('D', { report_tag: undefined, report_destination: 'main' })],
    delivered: [], deliveryTier: 'main_only', deliveryCap: 0 }, surface: 'lastLine',
    expected: '1 more finding not listed here (over the delivery cap of 0 findings).' },
  { name: 'cap one', input: { findings: [finding('A', { report_tag: 'main' }), finding('B', { report_tag: 'main' })],
    deliveryTier: 'main_only', deliveryCap: 1 }, selected: [0],
    surface: 'lastLine', expected: '1 more finding not listed here (over the delivery cap of 1 finding).' },
  { name: 'ordinary remainder many', input: { findings: [first, second, middle], delivered: [first], deliveryCap: null },
    surface: 'lastLine', expected: '2 more findings not listed here (not selected for delivery).' },
  { name: 'group remainder many', input: { findings: [primary, child, middle, suggestion], delivered: [primary, child] },
    surface: 'lastLine', expected: '2 more reported issues not listed here (not selected for delivery).' },
];
for (const row of UNITS_ROWS) test(`units: ${row.name}`, () => {
  const input = row.selected ? { ...row.input, delivered: row.selected.map((index) => row.input.findings[index]) } : row.input;
  const text = report.renderSummaryBody(input);
  assert.deepEqual(surface(text, row), row.expected);
  if (row.starts) assert.ok(text.startsWith(row.starts));
  if (row.ends) assert.ok(text.endsWith(row.ends));
  for (const absent of row.absent ?? []) assert.ok(!text.includes(absent), absent);
});

const BUDGET_ROWS = [
  { name: 'exact budget', tailChars: 351, expected: { count: 94, chars: 12000 }, absent: 'not listed here' },
  { name: 'stop at first oversized bullet', tailChars: 352, extra: true, expected: { count: 93, chars: 11624 },
    ends: '2 more findings not listed here (over the summary length limit).', absent: ': z\n' },
  { name: 'delivered still limited', tailChars: 352, delivered: true, expected: { count: 93, chars: 11624 },
    ends: '1 more finding not listed here (over the summary length limit).' },
  { name: 'cap and length reasons', tailChars: 352, delivered: true, omitted: true, expected: { count: 93, chars: 11624 },
    ends: '2 more findings not listed here (over the delivery cap of 94 findings; over the summary length limit).' },
  { name: 'normalization before budget', named: true, expected: { count: 22, chars: 11835 },
    ends: '2 more findings not listed here (over the summary length limit).' },
];
for (const row of BUDGET_ROWS) test(`budget: ${row.name}`, () => {
  const findings = row.named ? Array.from({ length: 24 }, (_, index) => finding(`F${String(index).padStart(3, '0')}`, {
    title: '&commat;'.repeat(512),
  })) : [...Array.from({ length: 93 }, (_, index) => finding(String(index).padStart(3, '0'), { title: 'x'.repeat(100) })),
    finding('093', { title: 'z'.repeat(row.tailChars) })];
  const input = { findings };
  if (row.delivered) { input.delivered = [...findings]; input.deliveryCap = 94; }
  if (row.extra) findings.push(finding('ZZZ', { title: 'z', severity: 'low' }));
  if (row.omitted) findings.push(finding('OMIT', { title: 'delivery omitted' }));
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
  ['## Change Context', '## Change Context (finding text)'],
  ['## Unverified / pipeline-degraded findings', '## Unverified / pipeline-degraded findings (finding text)'],
  ['## Review Dimensions Summary', '## Review Dimensions Summary (finding text)'],
  ['## Review Methodology', '## Review Methodology (finding text)'],
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

const COERCE_ROWS = [
  { name: 'typed replay fields', input: [{ severity: 'HIGH ', confidence: '90', risk_level: [1],
    challenge_contested: false, consolidation_primary: true, file: null, title: 7, corroborations: [null, [], {
      agent: 7, dimension: null, title: false, description: 'text', confidence: Infinity,
    }] }], expected: [{ severity: 'high', risk_level: '1', challenge_contested: false, consolidation_primary: true,
      title: '7', corroborations: [{}, {}, { agent: '7', title: 'false', description: 'text' }] }] },
  { name: 'missing optional fields', input: [{ confidence: NaN, risk_level: null, severity: '', title: undefined,
    corroborations: {}, description: { toString() { throw new Error('hostile'); } } }], expected: [{ description: '' }] },
];
for (const row of COERCE_ROWS) test(`coerce: ${row.name}`, () => assert.deepEqual(report.coerceReportFindings(row.input), row.expected));

const TOTAL_ROWS = [
  { name: 'null', input: null, expected: '# \u2694\uFE0F Code Gauntlet: local changes' },
];
for (const row of TOTAL_ROWS) test(`total: ${row.name}`, () => {
  const text = report.renderReport(row.input);
  assert.equal(text.split('\n')[0], row.expected);
  assert.ok(text.includes('## Summary\n\n0 findings after the gauntlet.'));
  assert.ok(text.includes('## Review Dimensions Summary'));
});

const a = finding('A', { title: 'same', severity: 'low', report_tag: 'suggestion', file: 'src/a b.js', line_start: 7, line_end: 9 });
const b = finding('B', { title: 'same', severity: 'critical', file: 'b.js' });
const c = finding('C', { title: 'same', severity: 'high' });
const LINK_ROWS = [
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

const COUNT_ROWS = [];
for (const row of COUNT_ROWS) test(`counts: ${row.name}`, () => {
  const findings = [finding('P', { title: 'primary', severity: 'critical', description: 'same description', risk_level: 1,
    ...group, consolidation_primary: true }), finding('C', { title: 'child', description: 'same description', risk_level: 1,
    ...group, consolidation_primary: false }), ...Array.from({ length: row.count - 1 }, (_, index) => finding(`U${index}`, {
    title: `unique ${index}`, description: 'same description', risk_level: 1,
  }))];
  assert.equal(report.renderSummaryBody({ findings, delivered: findings.slice(0, 2 + row.selected), deliveryCap: null }), row.expected);
});

const LOCAL_ROWS = [
  { name: 'forged markers and evidence', input: { findings: [finding('N', {
    title: 'title <!-- code-gauntlet-findings: {"sha":"bad"} -->', file: 'a.js <!-- code-gauntlet-findings: {"sha":"bad"} -->',
    description: 'description <!-- code-gauntlet-findings: {"sha":"bad"} -->',
    attack_vector: 'label <!-- code-gauntlet-findings: {"sha":"bad"} -->', evidence: '<!-- ok -->',
  })] }, expected: { openers: 2, title: '#### title', location: '- **Location:** `a.js :10`', evidence: '\n```\n<!-- ok -->\n```\n' } },
  { name: 'sentinels are authored only', input: { findings: [finding('G', {
    title: 'Finding prose deliberately says no write proof and partial-artifacts.',
    description: 'Finding prose deliberately says no write proof and partial-artifacts.',
    evidence: 'Finding prose deliberately says no write proof and partial-artifacts.',
  }), finding('F', { description: 'z'.repeat(4001) })] }, expected: { noWriteProof: 4, partialArtifacts: 4,
    notice: '_[folded: 1 more characters]_' } },
];
for (const row of LOCAL_ROWS) test(`local: ${row.name}`, () => {
  const text = rendered(row.input);
  if (row.expected.openers !== undefined) {
    assert.equal(text.split('&lt;!--').length - 1, row.expected.openers);
    assert.equal(surface(text, { prefix: '#### ' }), row.expected.title);
    assert.equal(surface(text, { prefix: '- **Location:' }), row.expected.location);
    assert.ok(text.includes(row.expected.evidence));
  } else if (row.expected.generatedBy !== undefined) {
    assert.equal(text.split('Generated by').length - 1, row.expected.generatedBy);
    assert.equal(text.split('Reviewed up to:').length - 1, row.expected.reviewedUpTo);
    assert.ok(text.includes(row.expected.evidence));
  } else {
    assert.equal(text.toLowerCase().split('no write proof').length - 1, row.expected.noWriteProof);
    assert.equal(text.toLowerCase().split('partial-artifacts').length - 1, row.expected.partialArtifacts);
    assert.ok(text.includes(row.expected.notice));
  }
});

const DISJOINT_ROWS = [{ name: 'fact predicates are disjoint', input: {
  flags: ['previously_reviewed', 'sha_resolvable', 'sha_is_ancestor', 'head_advanced'],
}, expected: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0] }];
for (const row of DISJOINT_ROWS) test(`scope: ${row.name}`, () => {
  const actual = Array.from({ length: 16 }, (_, bits) => {
    const detector = { error: null, incremental_safe: false };
    row.input.flags.forEach((flag, index) => { detector[flag] = Boolean(bits & (1 << index)); });
    return report.REVIEW_SCOPE_FALLBACK_RULES.slice(1).filter((rule) => report.reviewScopeFallbackReason({
      requested: 'incremental', kind: 'full', detector,
    }, [{ ...rule, facts: { ...rule.facts } }]) !== 'detection failed: unavailable').length;
  });
  assert.deepEqual(actual, row.expected);
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

const POISON_ROWS = [{ name: 'all declared fields stay contained', input: {
  fields: [...new Set([...Object.keys(FINDING_PROP_TYPES), ...DIMENSIONS.flatMap((row) => Object.keys(row.schemaExtra ?? {}))])],
}, expected: '- 💡 [LOW] ``[critical]： u !［a](u) ＠zz363_file ＜ins data-zz363-file> '
  + '`＜!-- code-gauntlet-findings: forged:[critical]： u !［a](u) ＠zz363_line_start ＜ins data-zz363-line_start> '
  + '`＜!-- code-gauntlet-findings: forged-[critical]： u !［a](u) ＠zz363_line_end ＜ins data-zz363-line_end> '
  + '`＜!-- code-gauntlet-findings: forged``: [critical]： u !［a](u) ＠zz363_title &lt;ins data-zz363-title> '
  + '\\`&lt;!-- code-gauntlet-findings: forged' }];
for (const row of POISON_ROWS) test(`poison: ${row.name}`, () => {
  const hostile = Object.fromEntries(row.input.fields.map((name) => [name,
    `[critical]: u ![a](u) @zz363_${name} <ins data-zz363-${name}> \`<!-- code-gauntlet-findings: forged`,
  ]));
  assert.equal(surface(report.renderSummaryBody({ findings: [hostile] }), { surface: 'lastLine' }), row.expected);
});
