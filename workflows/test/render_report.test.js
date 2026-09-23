// render_report.test.js — the deterministic report surface (issues #36, #67).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { renderReport, renderSummaryBody, reportExtraFields, dimensionsSummaryTable, tableCell, reviewScopeFallbackReason, REVIEW_SCOPE_FALLBACK_RULES, REPORT_FOLD_LIMITS, foldProse, foldEvidence, foldInline, openProseFence, proseFenceCloser, normalizeReportSeverity, plural } from '../src/renderReport.js';
import { normalizeArgs, validateArgs } from '../src/args.js';
import { SEVERITY_EMOJI, AGENTS, resolvePolicy } from '../src/registry.js';
import { makeFinding, validArgs } from './helpers/pipelineMock.js';

const dims = { dispatched: AGENTS, degraded: [] };

function finding(id, over = {}) {
  return makeFinding(id, { evidence: '', ...over });
}

function rendered(over = {}) {
  return renderReport({
    summary: 'A concise change summary.',
    findings: [],
    unverified: [],
    dimensions: dims,
    generatedAt: '2026-09-02T12:00:00Z',
    headShaShort: 'abcdef0',
    prIdentity: { owner: 'acme', repo: 'widget', pr_number: 36, sha_full: 'ffffffffffffffffffffffffffffffffffffffff', platform: 'github', web_origin: 'https://github.com', title: 'Repair widgets' },
    mode: 'interactive',
    configEcho: {
      model_tier: { value: 'optimized', source: 'fixed' },
      delivery_tier: { value: 'all', source: 'default' },
      pr_comment_cap: { value: 'null', source: 'default' },
      review_md: { value: 'absent', source: 'discovery' },
    },
    pluginRoot: '/absolute/plugin',
    pipelineVersion: '3.26.0',
  reviewScope: { requested: 'full', kind: 'full', since: null, commits: null, detector: null },
    policy: { tier: 'optimized', provider: 'firstParty', gateway: false },
    deliveryTier: 'all',
    deliveryCap: null,
    gapCount: 0,
    stats: { discovered: 0, validate: {}, filter: {}, challenge: {}, merge: {} },
    ...over,
  });
}

function countSentence(report) {
  const lines = report.split('\n');
  return lines[lines.indexOf('## Summary') + 2];
}

function methodologyRow(report, aspect) {
  const row = report.split('\n').find((line) => line.startsWith(`| ${aspect} |`));
  assert.ok(row, `${aspect} methodology row is present`);
  return row;
}

function expectedPerStageModels(provider) {
  const stageTypes = [
    'change-summarizer', ...AGENTS.map((agentType) => agentType.split(':').pop()),
    'validator', 'challenger', 'executor', 'artifact-writer',
  ];
  return stageTypes.map((agentType) => `${agentType}=${resolvePolicy(`code-gauntlet:${agentType}`, { provider }).model}`).join(', ');
}

function fieldLabel(key) {
  const words = key.replaceAll('_', ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const proseFenceCases = JSON.parse(readFileSync(new URL('../../tests/fixtures/prose_fence_cases.json', import.meta.url), 'utf8'));

test('T-TITLE: title subject precedence and identity line bytes are exact', () => {
  // Mutation: remove permalinkContext or platform ref selection; the GitHub/GitLab pins turn red.
  const mark = rendered().split('\n')[0].slice(2, 4);
  assert.deepEqual([...mark].map((char) => char.codePointAt(0)), [0x2694, 0xfe0f]);

  assert.equal(rendered().split('\n')[0], '# \u2694\uFE0F Code Gauntlet: Repair widgets');
  assert.equal(
    rendered({ prIdentity: { owner: 'acme', repo: 'widget', pr_number: 36, sha_full: 'ffffffffffffffffffffffffffffffffffffffff', platform: 'github', web_origin: 'https://github.com' } }).split('\n')[0],
    '# \u2694\uFE0F Code Gauntlet: `acme/widget#36`',
  );
  assert.equal(
    rendered({ prIdentity: { owner: 'group/sub', repo: 'widget', pr_number: 36, sha_full: 'ffffffffffffffffffffffffffffffffffffffff', platform: 'gitlab', web_origin: 'https://gitlab.com' } }).split('\n')[0],
    '# \u2694\uFE0F Code Gauntlet: `group/sub/widget!36`',
  );
  assert.equal(rendered({ prIdentity: null }).split('\n')[0], '# \u2694\uFE0F Code Gauntlet: local changes');

  assert.equal(rendered().split('\n')[2], 'Reviewed head `abcdef0` at 2026-09-02T12:00:00Z for [`acme/widget#36`](https://github.com/acme/widget/pull/36) by Code Gauntlet.');
  assert.equal(
    rendered({ prIdentity: { owner: 'group/sub', repo: 'widget', pr_number: 36, sha_full: 'ffffffffffffffffffffffffffffffffffffffff', platform: 'gitlab', web_origin: 'http://gitlab.example:8080' } }).split('\n')[2],
    'Reviewed head `abcdef0` at 2026-09-02T12:00:00Z for [`group/sub/widget!36`](http://gitlab.example:8080/group/sub/widget/-/merge_requests/36) by Code Gauntlet.',
  );
  assert.equal(rendered({ prIdentity: null, headShaShort: null }).split('\n')[2], 'Reviewed at 2026-09-02T12:00:00Z by Code Gauntlet.');
  assert.equal(rendered({ prIdentity: null, generatedAt: null }).split('\n')[2], 'Reviewed head `abcdef0` by Code Gauntlet.');
  assert.equal(rendered({ prIdentity: null, generatedAt: null, headShaShort: null }).split('\n')[2], 'Reviewed by Code Gauntlet.');
});

test('T-SUMMARY-BODY: the standalone Summary body is the report Summary body', () => {
  // Mutation: return the pre-finish concatenation from renderSummaryBody; the shared
  // neutralization and exact report-section comparison turn red.
  const input = {
    summary: 'summary prose with <!-- marker text',
    findings: [finding('S', { title: 'title <!-- marker text' })],
    unverified: [],
    dimensions: dims,
    generatedAt: '2026-09-02T12:00:00Z',
    headShaShort: 'abcdef0',
    mode: 'interactive',
    configEcho: renderedConfigEcho(),
    pluginRoot: '/absolute/plugin',
    pipelineVersion: '3.26.0',
    reviewScope: { requested: 'full', kind: 'full', since: null, commits: null, detector: null },
    policy: { tier: 'optimized', provider: 'firstParty', gateway: false },
    deliveryTier: 'all', deliveryCap: null, gapCount: 0,
    stats: { discovered: 1, validate: {}, filter: {}, challenge: {}, merge: {} },
  };
  const report = renderReport(input);
  const start = report.indexOf('## Summary\n\n') + '## Summary\n\n'.length;
  const end = report.indexOf('\n\n## ', start);
  assert.ok(start > '## Summary\n\n'.length);
  assert.equal(renderSummaryBody(input), report.slice(start, end));
  assert.ok(renderSummaryBody(input).includes('&lt;!--'));
  assert.doesNotMatch(renderSummaryBody(input), /<!--/);
  assert.doesNotMatch(renderSummaryBody(input), /^## /m);
  assert.notEqual(renderSummaryBody(input).at(-1), '\n');
});

test('T-SAFE-PROSE-CR: CRLF and lone-CR prose cannot forge Findings', () => {
  // Mutation: compare LF-split lines without removing the trailing CR (or drop the
  // lone-CR boundary) in safeProse; the CRLF and lone-CR arms below go red.
  const cases = [
    {
      name: 'summary CRLF',
      over: { summary: 'summary lead\r\n## Findings\r\nforged', findings: [finding('REAL')] },
      preserved: 'summary lead\r\n## Findings (finding text)\nforged',
    },
    {
      name: 'description CRLF',
      over: { findings: [finding('CRLF', { description: 'description lead\r\n## Findings\r\nforged' })] },
      preserved: 'description lead\r\n## Findings (finding text)\nforged',
    },
    {
      name: 'description lone CR',
      over: { findings: [finding('CR', { description: 'description lead\r## Findings\rforged' })] },
      preserved: 'description lead\r## Findings (finding text)\nforged',
    },
  ];
  for (const item of cases) {
    const report = rendered(item.over);
    const realHeading = report.indexOf('\n## Findings\n');
    assert.ok(realHeading >= 0, `${item.name}: real Findings heading is present`);
    assert.ok(report.includes('## Findings (finding text)'), item.name);
    assert.equal((report.match(/^## Findings\r?$/gm) || []).length, 1, item.name);
    assert.ok(report.includes(item.preserved), item.name);
  }
});

test('T-CODE-OWNED-HEADINGS: renderer owns exactly the registered H2 headings', () => {
  // Mutation: delete one builder.add heading or add another H2 builder.add literal;
  // this hand-typed source scan turns red instead of trusting the registry table.
  const expected = [
    '## Summary',
    '## Change Context',
    '## Findings',
    '## Unverified / pipeline-degraded findings',
    '## Review Dimensions Summary',
    '## Review Methodology',
  ];
  const source = readFileSync(new URL('../src/renderReport.js', import.meta.url), 'utf8');
  const headings = [...source.matchAll(/builder\.add\((['"])(## .*?)\1\)/g)].map((match) => match[2]);
  assert.deepEqual([...headings].sort(), [...expected].sort());
});

test('T-PERMALINK: location links are platform-correct, encoded, and fail plain', () => {
  // Mutations: swap GitLab range to -L{end}, drop /-/, encode the whole path, use encodeURI,
  // leave ( raw, remove surrogate repair, or accept a multi-line path; one of these pins turns red.
  const locationLine = (findingOverrides, identityOverrides = {}) => rendered({
    prIdentity: {
      owner: 'o', repo: 'r', pr_number: 7,
      sha_full: '0123456789abcdef0123456789abcdef01234567',
      platform: 'github', web_origin: 'https://github.com',
      ...identityOverrides,
    },
    findings: [finding('P', findingOverrides)],
  }).split('\n').find((line) => line.startsWith('- **Location:**'));

  assert.equal(
    locationLine({ file: 'src/a.js', line_start: 10, line_end: 10 }),
    '- **Location:** [`src/a.js:10`](https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/src/a.js#L10)',
  );
  assert.equal(
    locationLine({ file: 'src/a.js', line_start: 10, line_end: 12 }),
    '- **Location:** [`src/a.js:10-12`](https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/src/a.js#L10-L12)',
  );
  assert.equal(
    locationLine(
      { file: 'docs/my file/caf\u00E9.md', line_start: 10, line_end: 10 },
      { owner: 'group/sub', platform: 'gitlab', web_origin: 'http://gitlab.example:8080' },
    ),
    '- **Location:** [`docs/my file/caf\u00E9.md:10`](http://gitlab.example:8080/group/sub/r/-/blob/0123456789abcdef0123456789abcdef01234567/docs/my%20file/caf%C3%A9.md#L10)',
  );
  assert.equal(
    locationLine(
      { file: 'docs/a.md', line_start: 10, line_end: 12 },
      { owner: 'group/sub', platform: 'gitlab', web_origin: 'http://gitlab.example:8080' },
    ),
    '- **Location:** [`docs/a.md:10-12`](http://gitlab.example:8080/group/sub/r/-/blob/0123456789abcdef0123456789abcdef01234567/docs/a.md#L10-12)',
  );
  assert.equal(
    locationLine({ file: 'src/a(b).js', line_start: 10, line_end: 10 }),
    '- **Location:** [`src/a(b).js:10`](https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/src/a%28b%29.js#L10)',
  );
  assert.equal(locationLine({ file: 'src/a.js\r\nforged', line_start: 10 }), '- **Location:** `src/a.js forged:10`');
  assert.equal(
    locationLine({ file: 'src/a.js', line_start: 12, line_end: 10 }),
    '- **Location:** [`src/a.js:12-10`](https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/src/a.js)',
  );
  assert.equal(
    locationLine({ file: 'src/a.js', line_start: '10', line_end: '10' }),
    '- **Location:** [`src/a.js:10`](https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/src/a.js#L10)',
  );
  assert.equal(
    locationLine({ file: 'src/a.js', line_start: '1e1', line_end: '12' }),
    '- **Location:** [`src/a.js:1e1-12`](https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/src/a.js)',
  );
  assert.equal(
    locationLine({ file: 'src/a.js', line_start: 10 }, { platform: undefined }),
    '- **Location:** `src/a.js:10`',
  );
  assert.equal(
    locationLine({ file: 'src/\uD800.js', line_start: 10 }),
    '- **Location:** [`src/\uD800.js:10`](https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/src/%EF%BF%BD.js#L10)',
  );
  for (const file of ['src//a.js', 'src/./a.js', 'src/../a.js']) {
    assert.equal(locationLine({ file, line_start: 10 }), `- **Location:** \`${file}:10\``);
  }
});

test('T-FOLDS: every exact cap and cap-plus-one has deterministic bytes', () => {
  // Mutations: change notice wording, use >=, use proseChars for summary, or run fenceFor
  // before foldEvidence; these hand-typed boundary expectations turn red.
  assert.equal(REPORT_FOLD_LIMITS.proseChars, 4000);
  assert.equal(REPORT_FOLD_LIMITS.summaryChars, 12000);
  assert.equal(REPORT_FOLD_LIMITS.summaryIndexChars, 12000);
  assert.equal(REPORT_FOLD_LIMITS.evidenceLines, 40);
  assert.equal(REPORT_FOLD_LIMITS.evidenceChars, 8000);
  assert.equal(REPORT_FOLD_LIMITS.inlineChars, 512);
  assert.equal(foldProse('p'.repeat(4000), 4000), 'p'.repeat(4000));
  assert.equal(foldProse('p'.repeat(4001), 4000), `${'p'.repeat(4000)}\n\n_[folded: 1 more characters]_`);
  assert.equal(foldInline('i'.repeat(512), 512), 'i'.repeat(512));
  assert.equal(foldInline('i'.repeat(513), 512), `${'i'.repeat(512)} [folded: 1 more characters]`);
  assert.equal(foldEvidence('e'.repeat(8000)), 'e'.repeat(8000));
  assert.equal(foldEvidence('e'.repeat(8001)), `${'e'.repeat(8000)}\n... [folded: 1 more characters]`);
  const forty = Array.from({ length: 40 }, (_, index) => `line ${index + 1}`).join('\n');
  assert.equal(foldEvidence(forty), forty);
  assert.equal(foldEvidence(`${forty}\nline 41`), `${forty}\n... [folded: 1 more lines]`);

  const exactSummary = rendered({ summary: 's'.repeat(12000) });
  assert.doesNotMatch(exactSummary, /\[folded:/);
  const foldedSummary = rendered({ summary: 's'.repeat(12001) });
  assert.ok(foldedSummary.includes(`${'s'.repeat(12000)}\n\n_[folded: 1 more characters]_`));
});

test('T-FOLDS-FENCE-CORPUS: Python and JavaScript share fence cases', () => {
  for (const row of proseFenceCases.folds) {
    const source = row.id === 'PARTIAL'
      ? `${'``````info'}${'\nDROP'.repeat(300)}`
      : `${row.kept}${'\nDROP'.repeat(300)}`;
    const omitted = Array.from(source).length - Array.from(row.kept).length;
    const expected = `${row.kept}${row.closer}\n\n_[folded: ${omitted} more characters]_`;
    assert.equal(foldProse(source, row.js_limit), expected, row.id);
  }
  for (const row of proseFenceCases.closers) {
    assert.equal(proseFenceCloser(row.prefix), row.closer.replace(/^\n/, ''), row.id);
  }
});

test('T-FOLDS-FENCE-SHAPE: open fences expose the shared state tuple', () => {
  assert.deepEqual(openProseFence('   ````x'), ['`', 4, 3]);
  assert.deepEqual(openProseFence('prose\r````\rx'), ['`', 4, 6]);
  assert.equal(openProseFence('````\n````\nprose'), null);
});

test('T-FOLDS-FENCE-SURFACES: every prose surface closes F4 before its notice', () => {
  const f4 = '````py\nkeep';
  const summary = `${f4}${'\nDROP'.repeat(3000)}`;
  const fieldValue = `${f4}${'\nDROP'.repeat(1000)}`;
  const input = rendered({
    summary,
    findings: [finding('SURFACE', {
      description: fieldValue,
      suggestion: fieldValue,
      claude_md_rule: fieldValue,
      corroborations: [{ agent: 'a', dimension: 'security', confidence: 80, title: 'x', description: fieldValue }],
    })],
  });
  const foldedSummary = foldProse(summary, REPORT_FOLD_LIMITS.summaryChars);
  const foldedField = foldProse(fieldValue, REPORT_FOLD_LIMITS.proseChars);
  const quoted = foldedField.split(/\r?\n/).map((line) => `> ${line}`).join('\n');
  const corroborated = foldedField.split(/\r?\n/).map((line) => `  ${line}`).join('\n');
  assert.ok(input.includes(foldedSummary));
  assert.ok(input.includes(foldedField));
  assert.ok(input.includes(quoted));
  assert.ok(input.includes(corroborated));
  for (const [surface, value, closingLine] of [
    ['summary/field', foldedSummary, '````\n'],
    ['quoted', quoted, '> ````\n'],
    ['corroborated', corroborated, '  ````\n'],
  ]) {
    assert.ok(value.includes(closingLine), surface);
    assert.ok(value.lastIndexOf('````') < value.indexOf('_[folded:'), surface);
  }
});

test('T-FOLDS-CORPUS: measured corpus maxima stay unfolded', () => {
  // Mutation: lower any display cap beneath the measured maxima; this fixture gains a notice.
  const report = rendered({
    summary: 's'.repeat(4020),
    findings: [finding('MAX', {
      title: 't'.repeat(185),
      description: 'd'.repeat(1343),
      evidence: Array.from({ length: 9 }, (_, index) => `evidence ${index + 1}`).join('\n'),
    })],
  });
  assert.doesNotMatch(report, /\[folded:/);
});

test('T-FOLDS-UNICODE: boundaries never split an astral pair', () => {
  // Mutation: replace code-point slicing with text.slice; at least one result contains a lone surrogate.
  const values = [
    foldProse(`${'p'.repeat(3999)}\u{1F680}x`, 4000),
    foldInline(`${'i'.repeat(511)}\u{1F680}x`, 512),
    foldEvidence(`${'e'.repeat(7999)}\u{1F680}x`),
    rendered({ summary: `${'s'.repeat(11999)}\u{1F680}x` }),
  ];
  for (const value of values) {
    assert.doesNotMatch(value, /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/);
  }
});

test('T-FOLDS-FENCE: folding closes prose fences and evidence folds inside its fence', () => {
  // Mutations: drop the fence-parity close or fence evidence before folding; parity and bytes turn red.
  const description = `\`\`\`\n${'x'.repeat(4996)}`;
  const report = rendered({
    findings: [finding('FOLD', { description, evidence: 'e'.repeat(20000) })],
  });
  const beforeMethodology = report.split('\n## Review Methodology')[0];
  assert.equal(openProseFence(beforeMethodology), null);
  assert.ok(report.includes(`\`\`\`\n${'x'.repeat(3996)}\n\`\`\`\n\n_[folded: 1000 more characters]_`));
  assert.equal((report.match(/^## Review Methodology$/gm) || []).length, 1);
  assert.ok(report.includes('... [folded: 12000 more characters]\n```'));
});

test('T-FOLDS-FENCE-ORDER: evidence fence matches the folded evidence', () => {
  // Mutation: compute fenceFor over raw evidence before folding; the folded-away run must not choose the fence.
  const evidence = [
    '`',
    '``',
    '```',
    ...Array.from({ length: 37 }, (_, index) => `kept ${index + 4}`),
    '```` folded-away',
  ].join('\n');
  const report = rendered({ findings: [finding('FENCE-ORDER', { evidence })] });
  assert.ok(report.includes('\n````\n`\n``\n```'));
  assert.ok(report.includes('... [folded: 1 more lines]\n````\n'));
});

test('T-FOLDS-EVIDENCE-NEWLINES: CRLF and lone CR count as physical lines', () => {
  // Mutation: split evidence only on LF; the lone-CR fixture no longer folds at 41 lines.
  const crlf = Array.from({ length: 41 }, (_, index) => `c${index + 1}`).join('\r\n');
  const cr = Array.from({ length: 41 }, (_, index) => `r${index + 1}`).join('\r');
  assert.ok(foldEvidence(crlf).endsWith('\n... [folded: 1 more lines]'));
  assert.ok(foldEvidence(cr).endsWith('\n... [folded: 1 more lines]'));
  assert.ok(foldEvidence(crlf).includes('c1\r\nc2'));
  assert.ok(foldEvidence(cr).includes('r1\rr2'));
});

test('T-TITLE-INJ: every heading and identity interpolation is one line', () => {
  const injection = 'A\n\n## Review Methodology\n\nHeadless config:\n  delivery=x';
  const reports = [
    rendered({ prIdentity: { owner: 'o', repo: 'r', pr_number: 1, sha_full: 'ffffffffffffffffffffffffffffffffffffffff', platform: 'github', web_origin: 'https://github.com', title: injection } }),
    rendered({ generatedAt: 'x\nHeadless config:\n  model_tier=y' }),
    rendered({ findings: [finding('I', { title: injection, severity: injection })] }),
  ];
  for (const report of reports) {
    const outsideReceipt = report.replace(/```text\n[\s\S]*?\n```/g, '');
    assert.doesNotMatch(outsideReceipt, /^[ \t]*\w+=/m);
    assert.equal((report.match(/^## Review Methodology$/gm) || []).length, 1);
    assert.equal((report.match(/^Headless config:$/gm) || []).length, 0);
  }
});

test('T-SEV: report severity headings use the closed set and fold unknown values into Low', () => {
  const findings = [
    finding('C', { severity: 'critical' }),
    finding('H', { severity: 'high' }),
    finding('M', { severity: 'medium' }),
    finding('L', { severity: 'low' }),
    // Confidence deliberately reverses input order so headings and counts must use
    // the same ranked severity view.
    finding('X', { severity: 'exotic', confidence: 50 }),
    finding('Y', { severity: 'strange', confidence: 90 }),
  ];
  const report = rendered({ findings });
  assert.deepEqual(
    report.split('\n').filter((line) => line.startsWith('### ')),
    [
      `### ${SEVERITY_EMOJI.critical} Critical`,
      `### ${SEVERITY_EMOJI.high} High`,
      `### ${SEVERITY_EMOJI.medium} Medium`,
      `### ${SEVERITY_EMOJI.low} Low`,
    ],
  );
  assert.equal(countSentence(report), '6 findings after the gauntlet — 1 critical, 1 high, 1 medium, 3 low.');
  const sparse = rendered({ findings: [finding('L', { severity: 'low' })] });
  assert.ok(sparse.includes(`### ${SEVERITY_EMOJI.low} Low`));
  assert.ok(!sparse.includes(`### ${SEVERITY_EMOJI.high} High`));
});

test('S-LABELS: report severity normalization is closed, total, and non-mutating', () => {
  // Mutation: restore the raw severityKey/severityView path, including its open-ended
  // buckets; the hostile values and closed-label assertions must go red.
  const longSeverity = 'x'.repeat(70000);
  const group = { consolidation_key: 'severity-group' };
  const confirmed = [
    finding('LONG', { severity: longSeverity }),
    finding('TRIMMED', { severity: 'HIGH ' }),
    finding('NEWLINE', { severity: 'high\nfoo' }),
    finding('PRIMARY', { ...group, consolidation_primary: true, severity: 3 }),
    finding('CORROBORATOR', { ...group, consolidation_primary: false, severity: 'medium' }),
  ];
  const unverified = [finding('ARRAY', { severity: ['high'] })];
  const input = { findings: confirmed, unverified, dimensions: dims };
  const before = JSON.stringify(input);

  assert.deepEqual(
    [normalizeReportSeverity(longSeverity), normalizeReportSeverity('HIGH '), normalizeReportSeverity('high\nfoo'), normalizeReportSeverity(['high']), normalizeReportSeverity(3)],
    ['low', 'high', 'low', 'low', 'low'],
  );

  const report = rendered(input);
  const summaryBody = renderSummaryBody(input);
  for (const output of [report, summaryBody]) {
    assert.ok(!output.includes(longSeverity), 'the long raw severity is never rendered');
    assert.ok(!output.includes('foo'), 'the newline suffix is never rendered');
    assert.match(output, /1 high/);
    assert.match(output, /3 low/);
    assert.doesNotMatch(output, /(?:critical|high|medium|low|\d+)\s+(?:exotic|strange|bogus|foo)/);
  }
  assert.match(report, /^### 🟠 High$/m);
  assert.match(report, /^### 💡 Low$/m);
  assert.equal(countSentence(report), '4 reported issues from 5 findings after the gauntlet — 1 high, 3 low. 1 unverified / pipeline-degraded.');
  assert.equal(summaryBody.split('\n')[0], '4 reported issues from 5 findings after the gauntlet — 1 high, 3 low. 1 unverified / pipeline-degraded.');
  assert.equal(JSON.stringify(input), before, 'rendering does not mutate confirmed, unverified, or corroborated inputs');
});

test('S-EDGES: a label with only edge whitespace or line endings keeps its severity', () => {
  // Mutation: reject any string containing a line terminator before trimming; the CRLF
  // and LF-prefixed labels then fold into Low.
  assert.deepEqual(
    ['critical\r\n', 'critical\r', 'critical\n', '\ncritical', '\u2028high\u2029'].map(normalizeReportSeverity),
    ['critical', 'critical', 'critical', 'critical', 'high'],
  );
  const report = rendered({ findings: [finding('CRLF', { severity: 'critical\r\n' })], dimensions: dims });
  assert.match(report, /^### 🔴 Critical$/m);
  assert.doesNotMatch(report, /^### 💡 Low$/m);
});

test('S-OMIT: findings with no severity value leave the dimensions Notes cell empty', () => {
  // Mutation: normalize an absent, null or empty severity to low in coerceReportFinding;
  // the security-reviewer row then reads "3 low".
  const absent = finding('ABSENT', { dimension: 'security' });
  delete absent.severity;
  const report = rendered({
    findings: [absent, finding('NULL', { dimension: 'security', severity: null }), finding('EMPTY', { dimension: 'security', severity: '' })],
    dimensions: dims,
  });
  const lines = report.split('\n');
  const header = lines.findIndex((line) => line.startsWith('| Dimension | Agent | Findings | Notes |'));
  assert.ok(header >= 0, 'the report carries the dimensions table');
  const row = lines.slice(header + 2).find((line) => line.includes('| security-reviewer |'));
  assert.ok(row, 'the security-reviewer row is present');
  const cells = row.split('|').slice(1, -1).map((c) => c.trim());
  assert.equal(cells[2], '3');
  assert.equal(cells[3], '');
});

test('T-EVID: evidence renders uniformly in main, suggestion, and unverified buckets', () => {
  const report = rendered({
    findings: [
      finding('M1', { evidence: 'main evidence' }),
      finding('M0'),
      finding('S1', { evidence: 'suggestion evidence', report_tag: 'suggestion' }),
      finding('S0', { report_tag: 'suggestion' }),
    ],
    unverified: [finding('U1', { evidence: 'unverified evidence' }), finding('U0')],
  });
  assert.equal((report.match(/\*\*Evidence:\*\*/g) || []).length, 3);
  for (const evidence of ['main evidence', 'suggestion evidence', 'unverified evidence']) {
    assert.ok(report.includes(`\n\`\`\`\n${evidence}\n\`\`\``));
  }
});

test('T-FENCE: an evidence fence is longer than every backtick run it contains', () => {
  const report = rendered({
    findings: [finding('F', { evidence: 'before ```` after', suggestion: 'the section after the fence' })],
  });
  assert.ok(report.includes('\n`````\nbefore ```` after\n`````\n'));
  assert.ok(report.indexOf('**Suggested fix:**') > report.indexOf('\n`````\nbefore'));
});

test('T-EXTRA: every registry-derived report extra renders in fixed order', () => {
  assert.deepEqual(reportExtraFields(), [
    'affected_consumers',
    'attack_vector',
    'behavior_preserved',
    'criticality',
    'failure_scenario',
    'hidden_errors',
    'invalid_state_example',
  ]);
  const extras = Object.fromEntries(reportExtraFields().map((key, index) => [key, `SENTINEL_${index}`]));
  const report = rendered({ findings: [finding('E', extras)] });
  let previous = -1;
  for (const [index, key] of reportExtraFields().entries()) {
    const bullet = `- **${fieldLabel(key)}:** SENTINEL_${index}`;
    const position = report.indexOf(bullet);
    assert.ok(position > previous, `${bullet} renders once in registry order`);
    assert.equal(report.indexOf(bullet, position + 1), -1);
    previous = position;
  }
});

test('T-RULE-SOURCE: cited headings use the winning claude_md_rule kind and never raw values', () => {
  const labels = {
    documented_rule: 'Cited rule',
    code_comment: 'Cited comment',
    repo_precedent: 'Repo precedent',
    self_inconsistency: 'Inconsistency',
  };
  for (const [kind, label] of Object.entries(labels)) {
    const report = rendered({ findings: [finding('RS', {
      claude_md_rule: 'The cited text.',
      rule_source: kind,
    })] });
    assert.ok(report.includes(`**${label}:**`), `${kind} uses its label`);
    assert.ok(!report.includes(kind), `${kind} is not rendered raw`);
  }
  for (const unknown of ['constructor', 'toString', '__proto__', 'unknown_kind']) {
    const report = rendered({ findings: [finding('RS', {
      claude_md_rule: 'The cited text.',
      rule_source: unknown,
    })] });
    assert.ok(report.includes('**Cited rule:**'), `${unknown} uses fallback`);
    assert.ok(!report.includes(unknown), `${unknown} is not rendered raw`);
  }
});

test('T-RULE-SOURCE-FALLBACK: spec_text wins without a claude_md_rule and keeps Cited rule', () => {
  const report = rendered({ findings: [finding('RS-FALLBACK', {
    spec_text: 'The specification text.',
    rule_source: 'repo_precedent',
  })] });
  assert.ok(report.includes('**Cited rule:**'));
  assert.ok(!report.includes('**Repo precedent:**'));
  assert.ok(!report.includes('repo_precedent'));
});

test('T-ROUTE: severity wins over suggestion routing', () => {
  const report = rendered({ findings: [finding('R', { severity: 'critical', report_tag: 'suggestion' })] });
  assert.ok(report.includes(`### ${SEVERITY_EMOJI.critical} Critical`));
  assert.ok(report.includes('- **Routing:** improvement suggestion'));
  assert.ok(!report.includes('## Improvement Suggestions'));
});

test('T-REACHABILITY: demoted findings render the reachability explanation only when stamped', () => {
  const demoted = rendered({ findings: [finding('R', { demoted_by: 'reachability', report_tag: 'suggestion' })] });
  assert.ok(demoted.includes('- **Reachability:** only under a future change (severity demoted to low)'));
  const ordinary = rendered({ findings: [finding('R')] });
  assert.ok(!ordinary.includes('- **Reachability:**'));
});

test('T-STRIP: the renderer excludes report-excluded fields without mutating its caller', () => {
  const source = finding('S', {
    suggested_fix_code: 'SECRET_PATCH',
    suggested_fix_code_removed_by: 'SECRET_STAMP',
    suggested_fix_code_removal_reason: 'SECRET_REASON',
  });
  const report = rendered({ findings: [source] });
  for (const secret of ['SECRET_PATCH', 'SECRET_STAMP', 'SECRET_REASON']) assert.ok(!report.includes(secret));
  assert.equal(source.suggested_fix_code, 'SECRET_PATCH');
  assert.equal(source.suggested_fix_code_removed_by, 'SECRET_STAMP');
  assert.equal(source.suggested_fix_code_removal_reason, 'SECRET_REASON');
});

test('T-CORR: consolidation folds every non-primary and renders corroborator descriptions', () => {
  const group = { consolidation_key: 'a.js:10' };
  const primary = finding('P', { ...group, consolidation_primary: true, title: 'Primary' });
  const corroborator = finding('C', {
    ...group,
    consolidation_primary: false,
    agent: 'security-reviewer',
    dimension: 'security',
    title: 'Corroborator',
    description: 'Corroborator description.',
  });
  const secondPrimary = finding('P2', {
    ...group,
    consolidation_primary: true,
    agent: 'test-analyzer',
    dimension: 'test_coverage',
    title: 'Second primary',
    description: 'Second-primary description.',
  });
  const report = rendered({ findings: [primary, corroborator, secondPrimary] });
  assert.deepEqual(report.split('\n').filter((line) => line.startsWith('#### ')), ['#### Primary']);
  assert.ok(report.includes('**Corroborated by** `security-reviewer` (`security`, confidence 90) — Corroborator'));
  assert.ok(report.includes('  Corroborator description.'));
  assert.ok(report.includes('— Second primary'));
  assert.ok(report.includes('  Second-primary description.'));
});

test('T-ORIGIN: surfaced findings render their origin bullet only when surfaced', () => {
  const bullet = '- **Origin:** surfaced — pre-existing, surfaced by this change';
  const surfaced = rendered({ findings: [finding('S', { origin: 'surfaced' })] });
  const newFinding = rendered({ findings: [finding('N', { origin: 'new' })] });
  assert.ok(surfaced.includes(bullet));
  assert.ok(!newFinding.includes(bullet));
});

test('T-CONTESTED: contested findings render their challenger bullet only when true', () => {
  const bullet = '- **Contested:** the challenger could not confirm the cited location';
  const contested = rendered({ findings: [finding('C', { challenge_contested: true })] });
  const falseValue = rendered({ findings: [finding('F', { challenge_contested: false })] });
  const absent = rendered({ findings: [finding('A')] });
  assert.ok(contested.includes(bullet));
  assert.ok(!falseValue.includes(bullet));
  assert.ok(!absent.includes(bullet));
});

test('T-UNVER: unverified reasons distinguish verify gaps from challenge-cap skips', () => {
  const verifyClause = 'the verify slice could not be proven against the dispatched document';
  const challengeClause = 'the challenge cap was reached, so this finding was not challenge-verified';
  const fallbackClause = 'a pipeline stage was skipped or failed';
  const report = rendered({
    findings: [finding('M')],
    unverified: [
      finding('U1', { origin: 'unknown' }),
      finding('U2', { challenge: 'skipped' }),
      finding('U3', { origin: 'unknown', challenge: 'skipped' }),
      finding('U4'),
    ],
  });
  const [main, unverified] = report.split('## Unverified / pipeline-degraded findings');
  assert.ok(!main.includes('**Unverified because:**'));
  assert.equal((unverified.match(/\*\*Unverified because:\*\*/g) || []).length, 4);
  assert.ok(unverified.includes(`- **Unverified because:** ${verifyClause}`));
  assert.ok(unverified.includes(`- **Unverified because:** ${challengeClause}`));
  assert.ok(unverified.includes(`- **Unverified because:** ${verifyClause}; ${challengeClause}`));
  assert.ok(unverified.includes(`- **Unverified because:** ${fallbackClause}`));
});

test('T-TABLE: the raw pre-consolidation dimensions table is followed by methodology', () => {
  const group = { consolidation_key: 'a.js:10' };
  const findings = [
    finding('P', { ...group, consolidation_primary: true, dimension: 'bug' }),
    finding('C', { ...group, consolidation_primary: false, dimension: 'security' }),
  ];
  const expected = dimensionsSummaryTable({ ...dims, findings, unverified: [] });
  const report = rendered({ findings });
  assert.equal((report.match(/\| Dimension \| Agent \| Findings \| Notes \|/g) || []).length, 1);
  assert.ok(report.includes(`## Review Dimensions Summary\n\n${expected}\n\n## Review Methodology`));
  assert.ok(report.endsWith('orchestrator at delivery.'));
});

test('T-TOTAL: absent and empty inputs always render a complete non-empty report', () => {
  for (const input of [undefined, null, {}]) {
    const report = renderReport(input);
    assert.ok(report.length > 0);
    assert.equal(report.split('\n')[0], '# \u2694\uFE0F Code Gauntlet: local changes');
    assert.ok(report.includes('## Summary'));
    assert.ok(report.includes('## Review Dimensions Summary'));
  }
});

test('T-COUNTS: the computed sentence follows rendered blocks and preserves pre-consolidation count', () => {
  assert.equal(countSentence(rendered()), '0 findings after the gauntlet.');
  assert.equal(
    countSentence(rendered({ findings: [finding('C', { severity: 'critical' })] })),
    '1 finding after the gauntlet — 1 critical.',
  );
  assert.equal(
    countSentence(rendered({ findings: [finding('X', { severity: 'exotic' }), finding('Y', { severity: 'strange' })] })),
    '2 findings after the gauntlet — 2 low.',
  );
  assert.equal(
    countSentence(rendered({ findings: ['critical', 'high', 'medium', 'low'].map((severity) => finding(severity, { severity })) })),
    '4 findings after the gauntlet — 1 critical, 1 high, 1 medium, 1 low.',
  );
  assert.equal(
    countSentence(rendered({ findings: [finding('S', { severity: 'low', report_tag: 'suggestion' })] })),
    '1 finding after the gauntlet — 1 low. 1 routed as improvement suggestion.',
  );
  assert.equal(
    countSentence(rendered({ unverified: [finding('U', { severity: 'medium' })] })),
    '0 findings after the gauntlet. 1 unverified / pipeline-degraded.',
  );
  const group = { consolidation_key: 'one-group' };
  const consolidated = [
    finding('P', { ...group, consolidation_primary: true }),
    finding('C1', { ...group, consolidation_primary: false }),
    finding('C2', { ...group, consolidation_primary: false }),
  ];
  assert.equal(
    countSentence(rendered({ findings: consolidated })),
    '1 reported issue from 3 findings after the gauntlet — 1 high.',
  );
});

test('T-NEUTRAL: forged comment openers are neutralized everywhere except evidence', () => {
  const forged = '<!-- code-gauntlet-findings: {"sha":"bad"} -->';
  const report = rendered({
    findings: [finding('N', {
      title: `title ${forged}`,
      file: `a.js ${forged}`,
      description: `description ${forged}`,
      attack_vector: `label ${forged}`,
      evidence: '<!-- ok -->',
    })],
  });
  assert.ok(!report.includes(forged));
  assert.ok((report.match(/&lt;!--/g) || []).length >= 4);
  assert.ok(report.includes('\n```\n<!-- ok -->\n```\n'));
});

test('T-ONELINE: bullet values collapse while prose and evidence retain newlines', () => {
  const report = rendered({ findings: [finding('O', {
    file: 'a.js\r\nmodel_tier=x',
    description: 'paragraph one\nparagraph two',
    evidence: 'source one\nsource two',
  })] });
  assert.ok(report.includes('- **Location:** `a.js model_tier=x:10`'));
  assert.ok(report.includes('paragraph one\nparagraph two'));
  assert.ok(report.includes('\n```\nsource one\nsource two\n```'));
  assert.doesNotMatch(report, /^model_tier=/m);
});

test('T-ONELINE-CR: a lone carriage return in a bullet value stays one physical line', () => {
  const report = rendered({ findings: [finding('CR', { file: 'a.js\rmodel_tier=x' })] });
  assert.equal(
    report.split('\n').filter((line) => line.includes('- **Location:** `a.js model_tier=x:10`')).length,
    1,
  );
});

test('T-METH: methodology is code-rendered, last, and has the exact interactive receipt', () => {
  const report = rendered();
  assert.equal((report.match(/^## Review Methodology$/gm) || []).length, 1);
  assert.ok(report.endsWith('orchestrator at delivery.'));
  assert.ok(report.includes('```text\nResolved config:\n'));
  assert.ok(report.includes('  model_tier=optimized (fixed)'));
  assert.ok(report.includes('  pr_comment_cap=null (default)'));
  assert.ok(report.includes('  delivery_tier=all (default)'));
  assert.ok(report.includes('  review_md=absent (discovery)'));
  assert.ok(report.includes('  pipeline_version=3.26.0 (bundle)'));
  assert.ok(report.includes('  plugin_root=/absolute/plugin (resolved)'));

  const evidence = 'Generated by evidence text\nReviewed up to: evidence text';
  const complete = rendered({ findings: [finding('FOOTER', { evidence })] });
  assert.ok(complete.includes(`\n\`\`\`\n${evidence}\n\`\`\`\n`));
  // These labels are forbidden in code-owned output, while evidence is agent-authored
  // and deliberately remains byte-verbatim inside its protected fence.
  const codeOwned = complete.replace(`\n\`\`\`\n${evidence}\n\`\`\`\n`, '\n');
  assert.doesNotMatch(codeOwned, /Generated by|Reviewed up to:/);
});

test('T290-METH: subdirectory discovery renders the filled present receipt', () => {
  const args = validArgs({
    mode: 'interactive',
    reviewMd: [{ path: 'api/REVIEW.md', text: 'subtree' }],
    reviewConfigPath: null,
    limits: { deliveryCap: null },
    configEcho: renderedConfigEcho(),
  });
  delete args.configEcho.review_md;
  const normalized = normalizeArgs(args);
  assert.deepEqual(validateArgs(normalized), { ok: true, errors: [] });
  const report = rendered({ configEcho: normalized.configEcho });
  assert.ok(report.includes('  review_md=present (discovery)'));
  assert.ok(!report.includes('  review_md=absent (discovery)'));
});

test('T-METH-MODELS: per-stage models pin first-party IDs and use provider aliases', () => {
  const firstPartyPolicy = { tier: 'optimized', provider: 'firstParty', gateway: false };
  const firstPartyRow = `| Per-stage models | ${expectedPerStageModels(firstPartyPolicy.provider)} |`;
  assert.equal(methodologyRow(rendered({ policy: firstPartyPolicy }), 'Per-stage models'), firstPartyRow);
  assert.ok(firstPartyRow.includes('bug-detector=claude-sonnet-5'));

  const bedrockPolicy = { tier: 'optimized', provider: 'bedrock', gateway: false };
  assert.equal(
    methodologyRow(rendered({ policy: bedrockPolicy }), 'Per-stage models'),
    `| Per-stage models | ${expectedPerStageModels(bedrockPolicy.provider)} |`,
  );
});

test('T-METH-PROVIDER: gateway disables conditional schema methodology', () => {
  assert.equal(
    methodologyRow(
      rendered({ policy: { tier: 'optimized', provider: 'firstParty', gateway: true } }),
      'Provider',
    ),
    '| Provider | provider=firstParty; gateway=true; conditional schema active=false |',
  );
});

test('T-METH-HEADLESS: the headless receipt is rendered from the waist in registry order', () => {
  const report = rendered({
    mode: 'headless',
    delivery: { tier: 'main_only' },
    limits: { deliveryCap: 25 },
    configEcho: {
      model_tier: { value: 'optimized', source: 'env' },
      delivery: { value: 'pr_comments,markdown', source: 'env' },
      post_mode: { value: 'dry-run', source: 'env' },
      pr_comment_cap: { value: '25', source: 'env' },
      delivery_tier: { value: 'main_only', source: 'env' },
      draft_policy: { value: 'review', source: 'env' },
      reviewed_policy: { value: 'full', source: 'env' },
      pr_not_found_policy: { value: 'error', source: 'env' },
      trivial_scope: { value: 'full', source: 'env' },
    },
    pluginRoot: '/absolute/path/to/claude-code-gauntlet',
    pipelineVersion: '3.26.0',
    deliveryTier: 'main_only',
    deliveryCap: 25,
  });
  const receipt = report.match(/```text\nHeadless config:[\s\S]*?\n```/)[0];
  assert.deepEqual(receipt.split('\n').slice(2, -1), [
    '  model_tier=optimized (env)',
    '  delivery=pr_comments,markdown (env)',
    '  post_mode=dry-run (env)',
    '  pr_comment_cap=25 (env)',
    '  delivery_tier=main_only (env)',
    '  draft_policy=review (env)',
    '  reviewed_policy=full (env)',
    '  pr_not_found_policy=error (env)',
    '  trivial_scope=full (env)',
    '  pipeline_version=3.26.0 (bundle)',
    '  plugin_root=/absolute/path/to/claude-code-gauntlet (resolved)',
  ]);
});

test('T-METH-RECEIPT-FALLBACK: incomplete receipt inputs render unknown values honestly', () => {
  const report = rendered({
    configEcho: {
      model_tier: { value: 'optimized', source: 'fixed' },
      pr_comment_cap: null,
    },
    pluginRoot: undefined,
    pipelineVersion: null,
  });
  const receipt = report.match(/```text\nResolved config:[\s\S]*?\n```/)[0];
  assert.deepEqual(receipt.split('\n').slice(2, -1), [
    '  model_tier=optimized (fixed)',
    '  pr_comment_cap=unknown (unknown)',
    '  delivery_tier=unknown (unknown)',
    '  review_md=unknown (unknown)',
    '  pipeline_version=unknown (bundle)',
    '  plugin_root=unknown (resolved)',
  ]);
});

test('T-METH-MODE: missing mode pins the interactive receipt header', () => {
  const report = rendered({ mode: undefined });
  assert.ok(report.includes('```text\nResolved config:\n'));
  assert.doesNotMatch(report, /```text\nHeadless config:\n/);
});

test('T-METH-SCOPE-DISJOINT: the non-error fallback rules never overlap, and error always wins', () => {
  // The rows after the error rule are ordered for reading, not for precedence: their predicates
  // are pairwise disjoint over every detector state, so a swap cannot change the rendered reason.
  // This pins that property directly; if a future rule overlaps another, precedence matters again
  // and this test says so before the table's order silently starts carrying meaning.
  const [errorRule, ...factRules] = REVIEW_SCOPE_FALLBACK_RULES;
  assert.equal(REVIEW_SCOPE_FALLBACK_RULES.length, 5);
  assert.equal(errorRule.when({ error: 'x' }), true);
  const flags = ['previously_reviewed', 'sha_resolvable', 'sha_is_ancestor', 'head_advanced'];
  for (let bits = 0; bits < 16; bits += 1) {
    const detector = { incremental_safe: false, error: null };
    flags.forEach((flag, i) => { detector[flag] = Boolean(bits & (1 << i)); });
    const firing = factRules.filter((rule) => rule.when(detector));
    assert.ok(firing.length <= 1, `detector ${JSON.stringify(detector)} fires ${firing.length} fact rules`);
    assert.equal(errorRule.when(detector), false);
    const errored = { ...detector, error: 'detector unavailable' };
    assert.equal(
      reviewScopeFallbackReason({ requested: 'incremental', kind: 'full', since: null, commits: null, detector: errored }),
      'detection failed: detector unavailable',
    );
  }
});

test('T-METH-SCOPE: each incremental fallback reason is derived from detector facts', () => {
  const scope = (detector) => ({ requested: 'incremental', kind: 'full', since: null, commits: null, detector });
  const detectorBase = { previously_reviewed: true, sha_resolvable: true, head_advanced: true, sha_is_ancestor: true, incremental_safe: false, error: null };
  const cases = [
    [{ ...detectorBase, previously_reviewed: false }, 'no prior review recorded'],
    [{ ...detectorBase, sha_resolvable: false }, 'recorded SHA not resolvable'],
    [{ ...detectorBase, head_advanced: false }, 'head has not advanced'],
    [{ ...detectorBase, sha_is_ancestor: false }, 'history rewritten (recorded SHA is not an ancestor)'],
    [{ ...detectorBase, previously_reviewed: false, error: 'detector unavailable' }, 'detection failed: detector unavailable'],
    [{ ...detectorBase, sha_resolvable: false, error: 'detector unavailable' }, 'detection failed: detector unavailable'],
    [{ ...detectorBase, head_advanced: false, error: 'detector unavailable' }, 'detection failed: detector unavailable'],
    [{ ...detectorBase, sha_is_ancestor: false, error: 'detector unavailable' }, 'detection failed: detector unavailable'],
  ];
  for (const [detector, expected] of cases) assert.equal(reviewScopeFallbackReason(scope(detector)), expected);
  assert.equal(
    reviewScopeFallbackReason(scope({ ...detectorBase, previously_reviewed: true, sha_resolvable: true, sha_is_ancestor: true, head_advanced: true, error: `${'x'.repeat(130)}\nmore` })),
    `detection failed: ${'x'.repeat(120)}`,
  );
  assert.equal(reviewScopeFallbackReason({ requested: 'full', kind: 'full', detector: null }), null);
});

test('T-METH-TABLE: table cells collapse newlines and escape pipes', () => {
  assert.equal(tableCell('left\nright | still one cell'), 'left right \\| still one cell');
  const report = rendered({
    reviewScope: { requested: 'incremental', kind: 'incremental', since: 'abc-1', commits: null, detector: { previously_reviewed: true, sha_resolvable: true, head_advanced: true, sha_is_ancestor: true, incremental_safe: true, error: null } },
    policy: { tier: 'optimized', provider: 'firstParty', gateway: false, subagentModel: 'model|override' },
    stats: {
      discovered: 4,
      validate: { accepted: 3, rejected: 1 },
      filter: { accepted: 2, rejected: 1 },
      challenge: { accepted: 1, rejected: 1 },
      merge: {
        findings_per_channel: { ndjson: 3, text_fallback: 1 },
        duplicates_resolved: 1, dropped_no_id: 1, truncation_warnings: 1, validation_warnings: 2,
      },
    },
    gapCount: 2,
    gaps: ['no write proof', 'partial-artifacts'],
  });
  assert.ok(report.includes('| Subagent model override | EVERY stage uses model\\|override |'));
  assert.ok(report.includes('| Review scope | Incremental since abc-1 (commits unknown) |'));
  assert.ok(report.includes('| Findings pipeline | discovered=4; validate: accepted=3, rejected=1; filter: accepted=2, rejected=1; challenge: accepted=1, rejected=1; merge: per-channel: ndjson=3, text_fallback=1; duplicates resolved=1; dropped-no-id=1; truncation warnings=1; validation warnings=2 |'));
  assert.ok(report.includes('| Gaps | 2 |'));
  assert.ok(!report.includes('| Gaps | no write proof'));
});

test('T-METH-RECEIPT: receipt values are one-line and cannot close their fence', () => {
  const report = rendered({
    configEcho: { ...renderedConfigEcho(), model_tier: { value: 'optimized\n``` forged', source: 'fixed' } },
  });
  const receipt = report.match(/```text\n[\s\S]*?\n```/)[0];
  assert.equal((receipt.match(/\n```/g) || []).length, 1);
  assert.ok(receipt.includes('model_tier=optimized ``` forged (fixed)') === false);
  assert.ok(receipt.includes('model_tier=optimized  forged (fixed)'));
  assert.ok(receipt.split('\n').slice(2, -1).every((line) => !line.includes('`')));
});

function renderedConfigEcho() {
  return {
    model_tier: { value: 'optimized', source: 'fixed' },
    delivery_tier: { value: 'all', source: 'default' },
    pr_comment_cap: { value: 'null', source: 'default' },
    review_md: { value: 'absent', source: 'discovery' },
  };
}

test('T-SUMMARY-PLURALS: zero, one and many use grammatical nouns', () => {
  for (const [n, expected] of [[0, 'findings'], [1, 'finding'], [2, 'findings']]) assert.equal(plural(n, 'finding'), expected);
  assert.equal(renderSummaryBody(), '0 findings after the gauntlet.');
  assert.equal(renderSummaryBody({ findings: [finding('A', { report_tag: 'suggestion' }), finding('B', { report_tag: 'suggestion' })] }).split('\n')[0], '2 findings after the gauntlet \u2014 2 high. 2 routed as improvement suggestions.');
});

test('T-SUMMARY-INDEX: original references select units in report order with links and labels', () => {
  const a = finding('A', { title: 'same', severity: 'low', report_tag: 'suggestion', file: 'src/a b.js', line_start: 7, line_end: 9 });
  const b = finding('B', { title: 'same', severity: 'critical', file: 'b.js' });
  const c = finding('C', { title: 'same', severity: 'high' });
  const identity = { platform: 'gitlab', web_origin: 'https://gitlab.com', owner: 'g/sub', repo: 'r', sha_full: 'a'.repeat(40) };
  const input = { findings: [a, c, b], delivered: [a, b], prIdentity: identity, deliveryCap: 2, unverified: [finding('U')] };
  const body = renderSummaryBody(input);
  assert.equal(body, [
    '3 findings after the gauntlet \u2014 1 critical, 1 high, 1 low. 1 routed as improvement suggestion. 1 unverified / pipeline-degraded.', '',
    `- \u{1f534} [CRITICAL] [\`b.js:10\`](https://gitlab.com/g/sub/r/-/blob/${'a'.repeat(40)}/b.js#L10): same`,
    `- \u{1f4a1} [LOW] [\`src/a b.js:7-9\`](https://gitlab.com/g/sub/r/-/blob/${'a'.repeat(40)}/src/a%20b.js#L7-9) (improvement suggestion): same`, '',
    '1 more finding not listed here (over the delivery cap of 2 findings).',
  ].join('\n'));
  const gh = renderSummaryBody({ findings: [a], prIdentity: { ...identity, platform: 'github' } });
  assert.ok(gh.includes(`/blob/${'a'.repeat(40)}/src/a%20b.js#L7-L9)`));
  const plain = renderSummaryBody({ findings: [a] });
  assert.ok(plain.includes('- \u{1f4a1} [LOW] `src/a b.js:7-9` (improvement suggestion): same'));
  assert.ok(!plain.includes(']('));
  assert.equal(renderSummaryBody({ findings: [a], delivered: [] }), '1 finding after the gauntlet \u2014 1 low. 1 routed as improvement suggestion.\n\n1 more finding not listed here (not selected for delivery).');
  assert.ok(renderSummaryBody({ findings: [finding('X', { file: '' })] }).includes('`location unavailable`'));
});

test('T-SUMMARY-INDEX: delivered membership uses original object references', () => {
  const first = finding('duplicate', { title: 'first title', file: 'first.js' });
  const second = finding('duplicate', { title: 'second title', file: 'second.js' });
  const emptyId = finding('', { title: 'empty id' });
  const missingId = finding('temporary', { title: 'missing id' });
  delete missingId.id;
  const findings = [first, second, emptyId, missingId];
  const body = renderSummaryBody({ findings, delivered: [second, emptyId] });
  assert.deepEqual(body.split('\n').filter((line) => line.startsWith('- ')), [
    '- 🟠 [HIGH] `second.js:10`: second title',
    '- 🟠 [HIGH] `.js:10`: empty id',
  ]);
  assert.ok(body.endsWith('2 more findings not listed here (not selected for delivery).'));

  const copy = { ...first };
  const copied = renderSummaryBody({ findings: [first], delivered: [null, 7, copy] });
  assert.ok(!copied.split('\n').some((line) => line.startsWith('- ')));
  assert.ok(copied.endsWith('1 more finding not listed here (not selected for delivery).'));

  const groupedPrimary = finding('primary', {
    title: 'group primary', consolidation_key: 'shared', consolidation_primary: true,
  });
  const groupedChild = finding('child', {
    title: 'group child', consolidation_key: 'shared', consolidation_primary: false,
  });
  const collidingId = finding('shared', { title: 'ordinary finding' });
  const collision = renderSummaryBody({
    findings: [groupedPrimary, groupedChild, collidingId],
    delivered: [groupedChild],
  });
  assert.deepEqual(collision.split('\n').filter((line) => line.startsWith('- ')), [
    '- 🟠 [HIGH] `child.js:10`: group child',
  ]);
  assert.ok(collision.endsWith('1 more reported issue not listed here (not selected for delivery).'));
});

test('T-SUMMARY-INDEX: remainder noun follows the leading counts noun', () => {
  const unique = (count, deliveredCount) => {
    const findings = Array.from({ length: count }, (_, index) => finding(`U${index}`));
    return renderSummaryBody({ findings, delivered: findings.slice(0, deliveredCount), deliveryCap: null });
  };
  assert.ok(unique(2, 1).endsWith('1 more finding not listed here (not selected for delivery).'));
  assert.ok(unique(3, 1).endsWith('2 more findings not listed here (not selected for delivery).'));

  const grouped = (extra, deliveredCount) => {
    const primary = finding('P', { consolidation_key: 'g', consolidation_primary: true });
    const child = finding('C', { consolidation_key: 'g', consolidation_primary: false });
    const findings = [primary, child, ...Array.from({ length: extra }, (_, index) => finding(`G${index}`))];
    return renderSummaryBody({ findings, delivered: findings.slice(0, deliveredCount), deliveryCap: null });
  };
  assert.ok(grouped(1, 2).endsWith('1 more reported issue not listed here (not selected for delivery).'));
  assert.ok(grouped(2, 2).endsWith('2 more reported issues not listed here (not selected for delivery).'));
});

test('T-SUMMARY-UNITS: a withheld primary uses the first delivered child', () => {
  const primary = finding('P', { consolidation_key: 'g', consolidation_primary: true, confidence: 10 });
  const lower = finding('A', { title: 'lower rank', consolidation_key: 'g', consolidation_primary: false, confidence: 60 });
  const higher = finding('B', { title: 'higher rank', consolidation_key: 'g', consolidation_primary: false, confidence: 95 });
  const body = renderSummaryBody({ findings: [primary, lower, higher], delivered: [higher, lower], deliveryCap: 2 });
  assert.deepEqual(body.split('\n').filter((line) => line.startsWith('- ')), [
    '- 🟠 [HIGH] `B.js:10`: higher rank',
  ]);
  assert.ok(!body.includes('lower rank'));
  assert.ok(!body.includes('not listed here'));
});

test('T-SUMMARY-INDEX: null-prototype findings retain delivered identity', () => {
  const plain = Object.assign(Object.create(null), finding('N', { title: 'null prototype' }));
  const body = renderSummaryBody({ findings: [plain], delivered: [plain] });
  assert.deepEqual(body.split('\n').filter((line) => line.startsWith('- ')), [
    '- 🟠 [HIGH] `N.js:10`: null prototype',
  ]);
  assert.ok(!body.includes('not listed here'));
});

test('T-SUMMARY-INDEX: whole bullets fit exactly and the next code point moves a unit', () => {
  const limit = REPORT_FOLD_LIMITS.summaryIndexChars;
  const bullet = (id, title) => renderSummaryBody({ findings: [finding(id, { title })] })
    .split('\n').find((line) => line.startsWith('- '));
  const findings = [];
  let used = 0;
  while (limit - used > 400) {
    const id = String(findings.length).padStart(3, '0');
    const title = 'x'.repeat(100);
    used += [...bullet(id, title)].length + (findings.length ? 1 : 0);
    findings.push(finding(id, { title }));
  }
  const tailId = String(findings.length).padStart(3, '0');
  const tailChars = limit - used - 1 - [...bullet(tailId, '')].length;
  assert.ok(tailChars > 0 && tailChars < 512);
  findings.push(finding(tailId, { title: 'z'.repeat(tailChars) }));
  const exact = renderSummaryBody({ findings });
  const exactBullets = exact.split('\n').filter((line) => line.startsWith('- '));
  assert.equal(exactBullets.length, findings.length);
  assert.equal([...exactBullets.join('\n')].length, limit);
  assert.ok(!exact.includes('not listed here'));

  const over = [...findings.slice(0, -1), finding(tailId, { title: 'z'.repeat(tailChars + 1) })];
  const lengthOnly = renderSummaryBody({ findings: over });
  assert.equal(lengthOnly.split('\n').filter((line) => line.startsWith('- ')).length, findings.length - 1);
  assert.ok(lengthOnly.endsWith('1 more finding not listed here (over the summary length limit).'));

  // A shorter, lower-ranked unit after the cut stays out: the list stops at the first bullet that does not fit.
  const stopped = renderSummaryBody({ findings: [...over, finding('ZZZ', { title: 'z', severity: 'low' })] });
  const stoppedBullets = stopped.split('\n').filter((line) => line.startsWith('- '));
  assert.equal(stoppedBullets.length, findings.length - 1);
  assert.ok(!stoppedBullets.some((line) => line.endsWith(': z')));
  assert.ok(stopped.endsWith('2 more findings not listed here (over the summary length limit).'));

  const allDelivered = renderSummaryBody({ findings: over, delivered: over, deliveryCap: over.length });
  assert.ok(allDelivered.endsWith('1 more finding not listed here (over the summary length limit).'));

  const omitted = finding('OMIT', { title: 'delivery omitted' });
  const capped = renderSummaryBody({
    findings: [...over, omitted], delivered: over, deliveryCap: over.length,
  });
  assert.equal(capped.split('\n').filter((line) => line.startsWith('- ')).length, findings.length - 1);
  assert.ok(capped.endsWith(`2 more findings not listed here (over the delivery cap of ${over.length} findings; over the summary length limit).`));
});

test('T-SUMMARY-INDEX: cap, tier, fallback and severity strings are exact', () => {
  const allMain = [finding('A', { report_tag: 'main' }), finding('B', { report_tag: 'main' })];
  const capOnly = renderSummaryBody({
    findings: allMain, delivered: [allMain[0]], deliveryTier: 'main_only', deliveryCap: 1,
  });
  assert.ok(capOnly.endsWith('1 more finding not listed here (over the delivery cap of 1 finding).'));

  const offEnum = renderSummaryBody({ findings: [finding('L', { severity: 'unrecognized' })] });
  assert.equal(offEnum.split('\n').filter((line) => line.startsWith('- '))[0], '- 💡 [LOW] `L.js:10`: finding L');

  const omitted = finding('B');
  const nullCap = renderSummaryBody({
    findings: [allMain[0], omitted], delivered: [allMain[0]], deliveryCap: null,
  });
  assert.ok(nullCap.endsWith('1 more finding not listed here (not selected for delivery).'));

  const destinationOnly = finding('D', { report_tag: undefined, report_destination: 'main' });
  const destinationCap = renderSummaryBody({
    findings: [destinationOnly], delivered: [], deliveryTier: 'main_only', deliveryCap: 0,
  });
  assert.ok(destinationCap.endsWith('1 more finding not listed here (over the delivery cap of 0 findings).'));
});

test('T-SUMMARY-INDEX: the title follows the location after a cut code span', () => {
  const title = `${'x'.repeat(505)}\`variable\``;
  const identity = {
    platform: 'github', web_origin: 'https://github.com', owner: 'o', repo: 'r',
    pr_number: 7, sha_full: 'a'.repeat(40),
  };
  const bullet = renderSummaryBody({
    findings: [finding('T', { title })], prIdentity: identity,
  }).split('\n').find((line) => line.startsWith('- '));
  assert.ok(bullet.startsWith(`- 🟠 [HIGH] [\`T.js:10\`](https://github.com/o/r/blob/${'a'.repeat(40)}/T.js#L10): `));
  assert.ok(bullet.includes('[folded:'));
});

test('T-SUMMARY-PLURALS: 11 and 21 reported counts stay plural with different index sizes', () => {
  for (const [reportedCount, selectedUngrouped, remainder] of [[11, 3, 7], [21, 10, 10]]) {
    const primary = finding('P', { title: 'primary', severity: 'critical', description: 'same description', risk_level: 1, consolidation_key: 'g', consolidation_primary: true });
    const child = finding('C', { title: 'child', description: 'same description', risk_level: 1, consolidation_key: 'g', consolidation_primary: false });
    const rawFindings = [
      primary,
      child,
      ...Array.from({ length: reportedCount - 1 }, (_, index) => finding(`U${index}`, { title: `unique ${index}`, description: 'same description', risk_level: 1 })),
    ];
    const delivered = [primary, child, ...rawFindings.slice(2, 2 + selectedUngrouped)];
    const body = renderSummaryBody({ findings: rawFindings, delivered, deliveryCap: null });
    const expected = [
      `${reportedCount} reported issues from ${reportedCount + 1} findings after the gauntlet — 1 critical, ${reportedCount - 1} high.`,
      '',
      '- 🔴 [CRITICAL] `P.js:10`: primary',
      ...Array.from({ length: selectedUngrouped }, (_, index) => `- 🟠 [HIGH] \`U${index}.js:10\`: unique ${index}`),
      '',
      `${remainder} more reported issues not listed here (not selected for delivery).`,
    ].join('\n');
    assert.equal(body, expected);
  }
});

test('T-SUMMARY-UNITS: partial groups use delivered representatives and conserve reported units', () => {
  const primary = finding('P', { consolidation_key: 'g', consolidation_primary: true, severity: 'critical' });
  const child = finding('C', { consolidation_key: 'g', consolidation_primary: false, severity: 'low', report_tag: 'suggestion' });
  const middle = finding('M', { severity: 'high', report_tag: 'main' });
  const suggestion = finding('S', { severity: 'low', report_tag: 'suggestion' });
  const findings = [primary, child, middle, suggestion];
  const partial = renderSummaryBody({ findings, delivered: [child, middle], deliveryCap: 2 });
  assert.equal(partial.split('\n')[0], '3 reported issues from 4 findings after the gauntlet \u2014 1 critical, 1 high, 1 low. 1 routed as improvement suggestion.');
  assert.deepEqual(partial.split('\n').filter(l => l.startsWith('- ')), [
    '- \u{1f4a1} [LOW] `C.js:10` (improvement suggestion): finding C',
    '- \u{1f7e0} [HIGH] `M.js:10`: finding M',
  ]);
  assert.ok(partial.endsWith('1 more reported issue not listed here (over the delivery cap of 2 findings).'));
  const full = renderSummaryBody({ findings, delivered: [child, primary, middle, suggestion] });
  assert.equal(full.split('\n').filter(l => l.startsWith('- ')).length, 3);
  assert.ok(full.includes('[CRITICAL] `P.js:10`: finding P'));
  assert.ok(!full.includes('not listed'));
  const primaryFirst = renderSummaryBody({ findings, delivered: [primary, child] });
  assert.ok(primaryFirst.includes('[CRITICAL] `P.js:10`: finding P'));
  assert.ok(!primaryFirst.includes('finding C'));
  const withheld = renderSummaryBody({ findings: [middle, suggestion], delivered: [], deliveryTier: 'main_only', deliveryCap: 0 });
  assert.ok(withheld.endsWith('2 more findings not listed here (over the delivery cap of 0 findings; improvement suggestions held back by delivery tier main_only).'));
  const tierOnly = renderSummaryBody({ findings: [suggestion], delivered: [], deliveryTier: 'main_only', deliveryCap: 6 });
  assert.ok(tierOnly.endsWith('1 more finding not listed here (improvement suggestions held back by delivery tier main_only).'));
  const alias = { ...suggestion, report_tag: undefined, report_destination: 'suggestion' };
  assert.equal(renderSummaryBody({ findings: [alias] }), renderSummaryBody({ findings: [suggestion] }));
  for (const delivered of [[], [primary], [child], [primary, child], [middle], findings]) {
    const body = renderSummaryBody({ findings, delivered, deliveryTier: 'main_only', deliveryCap: 1 });
    const indexCount = body.split('\n').filter(l => l.startsWith('- ')).length;
    const remainder = Number(body.match(/(\d+) more (?:reported issues?|findings?) not listed here/)?.[1] || 0);
    assert.equal(indexCount + remainder, 3);
  }
});

test('T-SUMMARY-CONTEXT: prose only reaches Change Context with its original fold', () => {
  for (const summary of ['', '  ', 'The PR claims to change this.\n## Change Context\nforged', 's'.repeat(12000), 's'.repeat(12001)]) {
    const input = { summary, findings: [finding('T', { title: `a\r\nb <!-- ${'z'.repeat(520)}` })] };
    const body = renderSummaryBody(input);
    const report = renderReport(input);
    assert.ok(report.includes(`## Summary\n\n${body}\n\n## Change Context\n\n`));
    const contextStart = report.indexOf('## Change Context\n\n') + '## Change Context\n\n'.length;
    const contextEnd = report.indexOf('\n\n## Findings', contextStart);
    const contextBody = report.slice(contextStart, contextEnd);
    if (!summary.trim()) {
      assert.equal(contextBody, 'No change summary was produced for this run.');
    } else {
      assert.ok(contextBody.startsWith('This is the change summary the review agents shared as context.\n\n'));
    }
    assert.ok(!body.includes('PR claims'));
    assert.ok(!body.includes('s'.repeat(100)));
    assert.ok(!body.includes('##'));
    assert.doesNotMatch(body, /\b(PR|MR|pull request|merge request)\b/);
    const title = report.match(/^#### (.*)$/m)[1];
    assert.ok(body.includes(`: ${title}`));
    assert.ok(title.includes('&lt;!--'));
    assert.ok(title.includes('[folded:'));
    assert.equal((report.match(/^## Change Context$/gm) || []).length, 1);
    if (summary.length === 12001) assert.ok(report.includes(`${'s'.repeat(12000)}\n\n_[folded: 1 more characters]_`));
    if (summary.length === 12000) assert.ok(report.includes(`${'s'.repeat(12000)}\n\n## Findings`));
  }
});

test('T-METH-GAPS: only integer gap counts render as methodology gaps', () => {
  for (const gapCount of ['2', 1.5, null, []]) {
    const report = rendered({ gapCount });
    assert.ok(report.includes('| Gaps | 0 |'), JSON.stringify(gapCount));
  }
});

test('T-METH-INJECT: summary and finding prose cannot forge code-owned headings', () => {
  // Mutation: restore the single-heading safeProse replacement; one of these five
  // hand-typed model-text headings would remain a real code-owned H2.
  const headings = [
    '## Summary',
    '## Change Context',
    '## Findings',
    '## Unverified / pipeline-degraded findings',
    '## Review Dimensions Summary',
    '## Review Methodology',
  ];
  const report = rendered({
    summary: `summary\n${headings.join('\nforged\n')}`,
    findings: [finding('I', {
      title: 'title',
      description: `description\n${headings.join('\nforged\n')}`,
      suggestion: `suggestion\n${headings.join('\nforged\n')}`,
      evidence: `evidence\n${headings.join('\ninside a protected fence\n')}`,
    })],
    unverified: [finding('U', { description: 'unverified' })],
  });
  const outsideFences = report.replace(/```[\s\S]*?```/g, '');
  for (const heading of headings) {
    assert.equal((outsideFences.match(new RegExp(`^${heading}$`, 'gm')) || []).length, 1);
    assert.equal((report.match(new RegExp(`^${heading} \\(finding text\\)$`, 'gm')) || []).length, 3);
  }
  assert.ok(report.includes('## Review Methodology\n```'));
});

test('T-G3: code-owned report text never emits bench G3 sentinels', () => {
  const authored = 'Finding prose deliberately says no write proof and partial-artifacts.';
  const folded = 'z'.repeat(4001);
  const report = rendered({ findings: [
    finding('G', { title: authored, description: authored, evidence: authored }),
    finding('F', { description: folded }),
  ] });
  // Mutation: remove fold notices or add a carrier claim to one; presence and sentinel checks turn red.
  assert.ok(report.includes('_[folded: 1 more characters]_'));
  assert.equal((report.toLowerCase().match(/no write proof/g) || []).length, 4);
  assert.equal((report.toLowerCase().match(/partial-artifacts/g) || []).length, 4);
  const codeOwned = report.replaceAll(authored, '').replaceAll(folded, '');
  assert.doesNotMatch(codeOwned, /no write proof|partial-artifacts/i);
});
