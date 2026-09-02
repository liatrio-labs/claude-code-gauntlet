// render_report.test.js — the deterministic report surface (issues #36, #67).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { renderReport, reportExtraFields, dimensionsSummaryTable } from '../src/renderReport.js';
import { SEVERITY_EMOJI, SEVERITY_EMOJI_FALLBACK, AGENTS } from '../src/registry.js';
import { makeFinding } from './helpers/pipelineMock.js';

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
    prIdentity: { owner: 'acme', repo: 'widget', pr_number: 36, title: 'Repair widgets' },
    ...over,
  });
}

function countSentence(report) {
  const lines = report.split('\n');
  return lines[lines.indexOf('## Summary') + 4];
}

function fieldLabel(key) {
  const words = key.replaceAll('_', ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

test('T-TITLE: title subject precedence and identity line bytes are exact', () => {
  const mark = rendered().split('\n')[0].slice(2, 4);
  assert.deepEqual([...mark].map((char) => char.codePointAt(0)), [0x2694, 0xfe0f]);

  assert.equal(rendered().split('\n')[0], '# \u2694\uFE0F Code Gauntlet: Repair widgets');
  assert.equal(
    rendered({ prIdentity: { owner: 'acme', repo: 'widget', pr_number: 36, sha_full: 'f'.repeat(40) } }).split('\n')[0],
    '# \u2694\uFE0F Code Gauntlet: `acme/widget#36`',
  );
  assert.equal(rendered({ prIdentity: null }).split('\n')[0], '# \u2694\uFE0F Code Gauntlet: local changes');

  assert.equal(rendered().split('\n')[2], 'Reviewed head `abcdef0` at 2026-09-02T12:00:00Z by Code Gauntlet.');
  assert.equal(rendered({ headShaShort: null }).split('\n')[2], 'Reviewed at 2026-09-02T12:00:00Z by Code Gauntlet.');
  assert.equal(rendered({ generatedAt: null }).split('\n')[2], 'Reviewed head `abcdef0` by Code Gauntlet.');
  assert.equal(rendered({ generatedAt: null, headShaShort: null }).split('\n')[2], 'Reviewed by Code Gauntlet.');
});

test('T-TITLE-INJ: every heading and identity interpolation is one line', () => {
  const injection = 'A\n\n## Review Methodology\n\nHeadless config:\n  delivery=x';
  const reports = [
    rendered({ prIdentity: { owner: 'o', repo: 'r', pr_number: 1, title: injection } }),
    rendered({ generatedAt: 'x\nHeadless config:\n  model_tier=y' }),
    rendered({ findings: [finding('I', { title: injection, severity: injection })] }),
  ];
  for (const report of reports) {
    assert.doesNotMatch(report, /^[ \t]*\w+=/m);
    assert.ok(!report.includes('\n## Review Methodology\n'));
    assert.ok(!report.includes('\nHeadless config:\n'));
  }
});

test('T-SEV: registry severity headings are sparse and unknown severities trail without dropping', () => {
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
      `### ${SEVERITY_EMOJI_FALLBACK} Strange`,
      `### ${SEVERITY_EMOJI_FALLBACK} Exotic`,
    ],
  );
  assert.equal(countSentence(report), '6 finding(s) after the gauntlet — 1 critical, 1 high, 1 medium, 1 low, 1 strange, 1 exotic.');
  const sparse = rendered({ findings: [finding('L', { severity: 'low' })] });
  assert.ok(sparse.includes(`### ${SEVERITY_EMOJI.low} Low`));
  assert.ok(!sparse.includes(`### ${SEVERITY_EMOJI.high} High`));
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

test('T-ROUTE: severity wins over suggestion routing', () => {
  const report = rendered({ findings: [finding('R', { severity: 'critical', report_tag: 'suggestion' })] });
  assert.ok(report.includes(`### ${SEVERITY_EMOJI.critical} Critical`));
  assert.ok(report.includes('- **Routing:** improvement suggestion'));
  assert.ok(!report.includes('## Improvement Suggestions'));
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

test('T-UNVER: unverified reasons distinguish verify gaps from challenge-cap skips', () => {
  const verifyClause = 'the verify slice could not be proven against the dispatched document';
  const challengeClause = 'the challenge cap was reached, so this finding was not challenge-verified';
  const report = rendered({
    findings: [finding('M')],
    unverified: [
      finding('U1', { origin: 'unknown' }),
      finding('U2', { challenge: 'skipped' }),
      finding('U3', { origin: 'unknown', challenge: 'skipped' }),
    ],
  });
  const [main, unverified] = report.split('## Unverified / pipeline-degraded findings');
  assert.ok(!main.includes('**Unverified because:**'));
  assert.equal((unverified.match(/\*\*Unverified because:\*\*/g) || []).length, 3);
  assert.ok(unverified.includes(`- **Unverified because:** ${verifyClause}`));
  assert.ok(unverified.includes(`- **Unverified because:** ${challengeClause}`));
  assert.ok(unverified.includes(`- **Unverified because:** ${verifyClause}; ${challengeClause}`));
});

test('T-TABLE: the raw pre-consolidation dimensions table is the final section exactly once', () => {
  const group = { consolidation_key: 'a.js:10' };
  const findings = [
    finding('P', { ...group, consolidation_primary: true, dimension: 'bug' }),
    finding('C', { ...group, consolidation_primary: false, dimension: 'security' }),
  ];
  const expected = dimensionsSummaryTable({ ...dims, findings, unverified: [] });
  const report = rendered({ findings });
  assert.equal((report.match(/\| Dimension \| Agent \| Findings \| Notes \|/g) || []).length, 1);
  assert.ok(report.endsWith(`## Review Dimensions Summary\n\n${expected}`));
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
  assert.equal(countSentence(rendered()), '0 finding(s) after the gauntlet.');
  assert.equal(
    countSentence(rendered({ findings: [finding('C', { severity: 'critical' })] })),
    '1 finding(s) after the gauntlet — 1 critical.',
  );
  assert.equal(
    countSentence(rendered({ findings: [finding('X', { severity: 'exotic' }), finding('Y', { severity: 'strange' })] })),
    '2 finding(s) after the gauntlet — 1 exotic, 1 strange.',
  );
  assert.equal(
    countSentence(rendered({ findings: ['critical', 'high', 'medium', 'low'].map((severity) => finding(severity, { severity })) })),
    '4 finding(s) after the gauntlet — 1 critical, 1 high, 1 medium, 1 low.',
  );
  assert.equal(
    countSentence(rendered({ findings: [finding('S', { severity: 'low', report_tag: 'suggestion' })] })),
    '1 finding(s) after the gauntlet — 1 low. 1 routed as improvement suggestion(s).',
  );
  assert.equal(
    countSentence(rendered({ unverified: [finding('U', { severity: 'medium' })] })),
    '0 finding(s) after the gauntlet. 1 unverified / pipeline-degraded.',
  );
  const group = { consolidation_key: 'one-group' };
  const consolidated = [
    finding('P', { ...group, consolidation_primary: true }),
    finding('C1', { ...group, consolidation_primary: false }),
    finding('C2', { ...group, consolidation_primary: false }),
  ];
  assert.equal(
    countSentence(rendered({ findings: consolidated })),
    '1 reported issue(s) from 3 finding(s) after the gauntlet — 1 high.',
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

test('T-SEAM: the report renderer does not own methodology or headless identity', () => {
  const report = rendered();
  for (const forbidden of [
    '## Review Methodology',
    'Review Methodology',
    'Headless config:',
    'pipeline_version=',
    'plugin_root=',
  ]) assert.ok(!report.includes(forbidden));
  assert.doesNotMatch(report, /^[ \t]*\w+=/m);
});

test('T-G3: code-owned report text never emits bench G3 sentinels', () => {
  const authored = 'Finding prose deliberately says no write proof and partial-artifacts.';
  const report = rendered({ findings: [finding('G', { title: authored, description: authored, evidence: authored })] });
  assert.equal((report.toLowerCase().match(/no write proof/g) || []).length, 3);
  assert.equal((report.toLowerCase().match(/partial-artifacts/g) || []).length, 3);
  const codeOwned = report.replaceAll(authored, '');
  assert.doesNotMatch(codeOwned, /no write proof|partial-artifacts/i);
});
