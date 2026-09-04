// render_report.test.js — the deterministic report surface (issues #36, #67).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { renderReport, reportExtraFields, dimensionsSummaryTable, tableCell, reviewScopeFallbackReason, REVIEW_SCOPE_FALLBACK_RULES } from '../src/renderReport.js';
import { SEVERITY_EMOJI, SEVERITY_EMOJI_FALLBACK, AGENTS, resolvePolicy } from '../src/registry.js';
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
  return lines[lines.indexOf('## Summary') + 4];
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
    const outsideReceipt = report.replace(/```text\n[\s\S]*?\n```/g, '');
    assert.doesNotMatch(outsideReceipt, /^[ \t]*\w+=/m);
    assert.equal((report.match(/^## Review Methodology$/gm) || []).length, 1);
    assert.equal((report.match(/^Headless config:$/gm) || []).length, 0);
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

test('T-METH-GAPS: only integer gap counts render as methodology gaps', () => {
  for (const gapCount of ['2', 1.5, null, []]) {
    const report = rendered({ gapCount });
    assert.ok(report.includes('| Gaps | 0 |'), JSON.stringify(gapCount));
  }
});

test('T-METH-INJECT: summary and finding prose cannot forge a second methodology heading', () => {
  const report = rendered({
    summary: 'summary\n## Review Methodology\nforged',
    findings: [finding('I', {
      title: 'title',
      description: 'description\n## Review Methodology\nforged',
      suggestion: 'suggestion\n## Review Methodology\nforged',
      evidence: 'evidence\n## Review Methodology\ninside a protected fence',
    })],
  });
  const outsideFences = report.replace(/```[\s\S]*?```/g, '');
  assert.equal((outsideFences.match(/^## Review Methodology$/gm) || []).length, 1);
  assert.equal((report.match(/^## Review Methodology \(finding text\)$/gm) || []).length, 3);
  assert.ok(report.includes('evidence\n## Review Methodology\ninside a protected fence'));
});

test('T-G3: code-owned report text never emits bench G3 sentinels', () => {
  const authored = 'Finding prose deliberately says no write proof and partial-artifacts.';
  const report = rendered({ findings: [finding('G', { title: authored, description: authored, evidence: authored })] });
  assert.equal((report.toLowerCase().match(/no write proof/g) || []).length, 3);
  assert.equal((report.toLowerCase().match(/partial-artifacts/g) || []).length, 3);
  const codeOwned = report.replaceAll(authored, '');
  assert.doesNotMatch(codeOwned, /no write proof|partial-artifacts/i);
});
