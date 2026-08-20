// stages_dimensions.test.js — dimensionsSummaryTable (issue #89): the Review Dimensions
// Summary table computed in CODE as a pure function of pipeline stats, instead of asked
// of the Phase 8 model. Unit tests call dimensionsSummaryTable directly with synthetic
// inputs and assert exact rendered strings; the reportStage-level tests at the bottom
// reuse the ctx-mock pattern from pipeline_run.test.js to check the plumbing
// (reportPrompt / minimalReport) around it. Also covers stripReportExcludedFields and
// reportStage's report-path field strip (issues #220, #226).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { dimensionsSummaryTable, reportStage, stripReportExcludedFields, REPORT_EXCLUDED_FIELDS } from '../src/stages.js';
import { AGENTS, AGENT_LABELS } from '../src/registry.js';
import { makeFinding } from './helpers/pipelineMock.js';

// A rendered table's data rows (skipping the header + separator), each cell trimmed.
function tableRows(md) {
  return md.split('\n').slice(2).map((line) => {
    const [dimension, agent, findings, notes] = line.split('|').slice(1, -1).map((c) => c.trim());
    return { dimension, agent, findings, notes };
  });
}

const rowIndex = (agentType) => AGENTS.indexOf(agentType);

// --- Shape ---------------------------------------------------------------------

test('header row matches the report-format.md column set exactly', () => {
  const md = dimensionsSummaryTable({});
  const lines = md.split('\n');
  assert.equal(lines[0], '| Dimension | Agent | Findings | Notes |');
  assert.equal(lines[1], '|-----------|-------|----------|-------|');
});

test('no leading heading — the table starts at the header row (placement is the caller\'s concern)', () => {
  const md = dimensionsSummaryTable({});
  assert.ok(!/^#/.test(md), `table must not open with a heading: ${md.slice(0, 40)}`);
});

test('one row per registry AGENT, in registry order — derived from AGENTS, nothing hardcoded', () => {
  const md = dimensionsSummaryTable({});
  const rows = tableRows(md);
  assert.equal(rows.length, AGENTS.length);
  assert.deepEqual(rows.map((r) => r.agent), AGENTS.map((a) => a.split(':').pop()));
  assert.deepEqual(rows.map((r) => r.dimension), AGENTS.map((a) => AGENT_LABELS[a]));
});

// --- Classification rules 1-6 ----------------------------------------------------

test('rule 1: not dispatched -> Findings em-dash, "Skipped" note', () => {
  const md = dimensionsSummaryTable({ dispatched: [], degraded: [], findings: [], unverified: [] });
  for (const r of tableRows(md)) {
    assert.equal(r.findings, '—');
    assert.equal(r.notes, 'Skipped — not dispatched in this run');
  }
});

test('rule 2: dispatched + degraded + zero findings -> Findings em-dash, "No results" note', () => {
  const md = dimensionsSummaryTable({ dispatched: AGENTS, degraded: ['bug'], findings: [], unverified: [] });
  const row = tableRows(md)[rowIndex('code-gauntlet:bug-detector')];
  assert.equal(row.findings, '—');
  assert.equal(row.notes, 'No results — agent did not complete');
});

test('rule 3: degraded but findings survived anyway -> Findings shows the surviving counts, "Partial" note', () => {
  const findings = [makeFinding('B1', { dimension: 'bug', severity: 'high' })];
  const unverified = [makeFinding('B2', { dimension: 'bug', severity: 'low' })];
  const md = dimensionsSummaryTable({ dispatched: AGENTS, degraded: ['bug'], findings, unverified });
  const row = tableRows(md)[rowIndex('code-gauntlet:bug-detector')];
  assert.equal(row.findings, '1 (+1 unverified)');
  assert.equal(row.notes, 'Partial — agent may not have completed');
});

test('rule 3: degraded with surviving high-confidence findings and zero unverified -> plain "N", no unverified suffix', () => {
  const findings = [makeFinding('B1', { dimension: 'bug', severity: 'high' })];
  const md = dimensionsSummaryTable({ dispatched: AGENTS, degraded: ['bug'], findings, unverified: [] });
  const row = tableRows(md)[rowIndex('code-gauntlet:bug-detector')];
  assert.equal(row.findings, '1');
  assert.equal(row.notes, 'Partial — agent may not have completed');
});

test('rule 4: dispatched, not degraded, zero findings -> Findings "0", "Clean" note', () => {
  const md = dimensionsSummaryTable({ dispatched: AGENTS, degraded: [], findings: [], unverified: [] });
  for (const r of tableRows(md)) {
    assert.equal(r.findings, '0');
    assert.equal(r.notes, 'Clean — no findings returned');
  }
});

test('rule 5: N findings, not degraded -> Findings "N", Notes is a severity breakdown in fixed order', () => {
  const findings = [
    makeFinding('S1', { dimension: 'security', severity: 'high' }),
    makeFinding('S2', { dimension: 'security', severity: 'critical' }),
    makeFinding('S3', { dimension: 'security', severity: 'high' }),
  ];
  const md = dimensionsSummaryTable({ dispatched: AGENTS, degraded: [], findings, unverified: [] });
  const row = tableRows(md)[rowIndex('code-gauntlet:security-reviewer')];
  assert.equal(row.findings, '3');
  assert.equal(row.notes, '1 critical, 2 high'); // fixed order, not insertion order
});

test('rule 5: N findings + M unverified -> Findings "N (+M unverified)"', () => {
  const findings = [makeFinding('S1', { dimension: 'security', severity: 'high' })];
  const unverified = [
    makeFinding('S2', { dimension: 'security', severity: 'low' }),
    makeFinding('S3', { dimension: 'security', severity: 'low' }),
  ];
  const md = dimensionsSummaryTable({ dispatched: AGENTS, degraded: [], findings, unverified });
  const row = tableRows(md)[rowIndex('code-gauntlet:security-reviewer')];
  assert.equal(row.findings, '1 (+2 unverified)');
  // Severity breakdown is over the HIGH-CONFIDENCE findings only, not the unverified ones.
  assert.equal(row.notes, '1 high');
});

test('rule 5: a severity outside SEVERITY_ORDER trails the known ones in first-seen order, never dropped', () => {
  const findings = [
    makeFinding('S1', { dimension: 'security', severity: 'exotic' }),
    makeFinding('S2', { dimension: 'security', severity: 'low' }),
    makeFinding('S3', { dimension: 'security', severity: 'weird' }),
  ];
  const md = dimensionsSummaryTable({ dispatched: AGENTS, degraded: [], findings, unverified: [] });
  const row = tableRows(md)[rowIndex('code-gauntlet:security-reviewer')];
  assert.equal(row.findings, '3');
  assert.equal(row.notes, '1 low, 1 exotic, 1 weird');
});

test('rule 5: no finding in the row carries a severity value -> empty Notes', () => {
  const findings = [makeFinding('S1', { dimension: 'security', severity: undefined })];
  const md = dimensionsSummaryTable({ dispatched: AGENTS, degraded: [], findings, unverified: [] });
  const row = tableRows(md)[rowIndex('code-gauntlet:security-reviewer')];
  assert.equal(row.findings, '1');
  assert.equal(row.notes, '');
});

test('rule 6: zero high-confidence but unverified findings exist -> Findings "0 (+M unverified)", "Unverified findings only" note', () => {
  const unverified = [makeFinding('S1', { dimension: 'security', severity: 'low' })];
  const md = dimensionsSummaryTable({ dispatched: AGENTS, degraded: [], findings: [], unverified });
  const row = tableRows(md)[rowIndex('code-gauntlet:security-reviewer')];
  assert.equal(row.findings, '0 (+1 unverified)');
  assert.equal(row.notes, 'Unverified findings only — see secondary section');
});

// --- Multi-dimension aggregation (conventions-and-intent) -----------------------

test('multi-dimension aggregation: convention + comment_accuracy findings roll into ONE row', () => {
  const findings = [
    makeFinding('C1', { dimension: 'convention', severity: 'medium' }),
    makeFinding('C2', { dimension: 'comment_accuracy', severity: 'medium' }),
  ];
  const md = dimensionsSummaryTable({ dispatched: AGENTS, degraded: [], findings, unverified: [] });
  const rows = tableRows(md).filter((r) => r.agent === 'conventions-and-intent');
  assert.equal(rows.length, 1, 'exactly one row for the multi-dimension agent');
  assert.equal(rows[0].findings, '2');
  assert.equal(rows[0].notes, '2 medium');
});

test('multi-dimension aggregation: degraded if only ONE of the agent\'s dimensions is degraded', () => {
  const findings = [makeFinding('I1', { dimension: 'intent', severity: 'medium' })];
  const md = dimensionsSummaryTable({ dispatched: AGENTS, degraded: ['convention'], findings, unverified: [] });
  const row = tableRows(md)[rowIndex('code-gauntlet:conventions-and-intent')];
  assert.equal(row.notes, 'Partial — agent may not have completed');
});

// --- Robustness -------------------------------------------------------------------

test('a finding with an unknown or missing dimension is ignored for every count', () => {
  const findings = [
    makeFinding('X1', { dimension: 'not-a-real-dimension' }),
    makeFinding('X2', { dimension: undefined }),
  ];
  const md = dimensionsSummaryTable({ dispatched: AGENTS, degraded: [], findings, unverified: [] });
  for (const r of tableRows(md)) assert.equal(r.findings, '0');
});

test('all fields absent renders a full table of Skipped rows without throwing', () => {
  assert.doesNotThrow(() => {
    const md = dimensionsSummaryTable();
    const rows = tableRows(md);
    assert.equal(rows.length, AGENTS.length);
    assert.ok(rows.every((r) => r.notes === 'Skipped — not dispatched in this run'));
  });
});

test('dimensionsSummaryTable() called with no argument at all does not throw', () => {
  assert.doesNotThrow(() => dimensionsSummaryTable());
});

// --- reportStage plumbing (ctx-mock pattern, see pipeline_run.test.js) ------------

// The Results JSON body reportPrompt embeds between "Results JSON:\n" and "\nReturn
// { report }". Parsed rather than substring-matched so the assertions read the same
// structured value the report-writer itself would.
function resultsBody(prompt) {
  const m = /Results JSON:\n([\s\S]*)\nReturn \{ report \}/.exec(prompt);
  assert.ok(m, `prompt did not carry a Results JSON body: ${prompt.slice(0, 200)}`);
  return JSON.parse(m[1]);
}

test('reportStage: the unsegmented (segment-0) prompt carries the rendered table verbatim + the paste-verbatim instruction', async () => {
  let capturedPrompt = null;
  const ctx = {
    agent: async (prompt) => { capturedPrompt = prompt; return { report: '# r' }; },
    parallel: async () => [],
  };
  const findings = [makeFinding('B1', { dimension: 'bug', severity: 'high' })];
  const dims = { dispatched: AGENTS, degraded: [] };
  await reportStage(ctx, { findings, unverified: [], stats: {}, dimensions: dims });

  assert.equal(typeof capturedPrompt, 'string', 'the dispatch contract requires a STRING prompt');
  const expectedTable = dimensionsSummaryTable({ ...dims, findings, unverified: [] });
  const body = resultsBody(capturedPrompt);
  assert.equal(body.dimensionsTable, expectedTable);
  assert.match(capturedPrompt, /paste it verbatim, unmodified/);
  assert.match(capturedPrompt, /Review Dimensions Summary/);
});

test('reportStage: on a segmented report, only segment 0 carries the table and the instruction', async () => {
  const prompts = [];
  const ctx = {
    agent: async (prompt, opts) => { prompts.push({ label: opts.label, prompt }); return { report: `body ${opts.label}` }; },
    parallel: async (thunks) => Promise.all(thunks.map((t) => t())),
  };
  const big = Array.from({ length: 80 }, (_, i) => makeFinding(`F${i}`, { description: 'x'.repeat(2000), dimension: 'bug', severity: 'high' }));
  await reportStage(ctx, { findings: big, unverified: [], stats: {}, dimensions: { dispatched: AGENTS, degraded: [] } });

  assert.ok(prompts.length > 1, 'fixture must segment for this test to be meaningful');
  const seg0 = prompts.find((p) => p.label === 'report-writer-0');
  const seg1 = prompts.find((p) => p.label === 'report-writer-1');
  assert.ok(seg0 && seg1, 'both segment 0 and segment 1 must have dispatched');

  const body0 = resultsBody(seg0.prompt);
  const body1 = resultsBody(seg1.prompt);
  assert.ok('dimensionsTable' in body0, 'segment 0 carries the table field');
  assert.ok(!('dimensionsTable' in body1), 'segment 1 must NOT carry the table field');
  assert.match(seg0.prompt, /paste it verbatim, unmodified/);
  assert.ok(!/paste it verbatim, unmodified/.test(seg1.prompt), 'segment 1 must not carry the paste instruction');
});

test('reportStage: a writer throw degrades to the minimal report, which still carries the table', async () => {
  const ctx = {
    agent: async () => { throw new Error('boom'); },
    parallel: async () => [],
  };
  const findings = [makeFinding('B1', { dimension: 'bug', severity: 'high' })];
  const dims = { dispatched: AGENTS, degraded: [] };
  const out = await reportStage(ctx, { findings, unverified: [], stats: {}, dimensions: dims });

  const expectedTable = dimensionsSummaryTable({ ...dims, findings, unverified: [] });
  assert.match(out.report, /## Review Dimensions Summary/);
  assert.ok(out.report.includes(expectedTable), 'the minimal-report fallback carries the exact rendered table');
});

test('reportStage: when parallel() null-isolates segment 0, the outer minimal-report fallback still carries the table exactly once', async () => {
  const ctx = {
    agent: async (prompt, opts) => ({ report: `body ${opts.label}` }),
    parallel: async (thunks) => Promise.all(thunks.map((t, i) => (i === 0 ? Promise.resolve(null) : t()))),
  };
  const big = Array.from({ length: 80 }, (_, i) => makeFinding(`F${i}`, { description: 'x'.repeat(2000), dimension: 'bug', severity: 'high' }));
  const dims = { dispatched: AGENTS, degraded: [] };
  const out = await reportStage(ctx, { findings: big, unverified: [], stats: {}, dimensions: dims });

  const parts = out.report.split('## Report segment ');
  assert.ok(parts.length > 2, 'fixture must segment into more than one chunk for this test to be meaningful');
  assert.match(parts[1], /## Review Dimensions Summary/, 'segment 1 (index 0, null-isolated) falls back to the minimal report and carries the table');
  assert.ok(!/## Review Dimensions Summary/.test(parts[2]), 'segment 2 (index 1) must not carry the table');
});

// --- reportStage delivery consolidation (#22 D2) ---------------------------

test('reportStage: a consolidation group folds the non-primary member into `corroborations` on the primary; findings without a key pass through unchanged', async () => {
  let capturedPrompt = null;
  const ctx = {
    agent: async (prompt) => { capturedPrompt = prompt; return { report: '# r' }; },
    parallel: async () => [],
  };
  const primary = makeFinding('P', { dimension: 'bug', severity: 'high', consolidation_key: 'f.js:0', consolidation_primary: true });
  const corroborator = makeFinding('C', { dimension: 'security', severity: 'medium', agent: 'security-reviewer', confidence: 70, consolidation_key: 'f.js:0', consolidation_primary: false });
  const unrelated = makeFinding('U', { dimension: 'bug', severity: 'low' });
  const findings = [primary, corroborator, unrelated];
  await reportStage(ctx, { findings, unverified: [], stats: {}, dimensions: { dispatched: AGENTS, degraded: [] } });

  const body = resultsBody(capturedPrompt);
  assert.equal(body.findings.length, 2, 'the corroborator is folded in, not listed as a separate finding');
  const renderedPrimary = body.findings.find((f) => f.id === 'P');
  assert.ok(renderedPrimary, 'the primary is present');
  assert.deepEqual(renderedPrimary.corroborations, [
    { agent: 'security-reviewer', dimension: 'security', confidence: 70, title: corroborator.title, description: corroborator.description },
  ]);
  const renderedUnrelated = body.findings.find((f) => f.id === 'U');
  assert.deepEqual(renderedUnrelated, unrelated, 'an unstamped finding passes through byte-identical');
});

test('reportStage: a second consolidation_primary in a group is demoted to a corroborator, not dropped', async () => {
  let capturedPrompt = null;
  const ctx = {
    agent: async (prompt) => { capturedPrompt = prompt; return { report: '# r' }; },
    parallel: async () => [],
  };
  const p1 = makeFinding('P1', { dimension: 'bug', severity: 'high', consolidation_key: 'f.js:0', consolidation_primary: true });
  const c1 = makeFinding('C1', { dimension: 'security', severity: 'medium', agent: 'security-reviewer', confidence: 70, consolidation_key: 'f.js:0', consolidation_primary: false });
  const p2 = makeFinding('P2', { dimension: 'test', severity: 'low', agent: 'test-analyzer', confidence: 60, consolidation_key: 'f.js:0', consolidation_primary: true });
  const findings = [p1, c1, p2];
  await reportStage(ctx, { findings, unverified: [], stats: {}, dimensions: { dispatched: AGENTS, degraded: [] } });

  const body = resultsBody(capturedPrompt);
  assert.equal(body.findings.length, 1, 'all three findings fold into one group, not two');
  const renderedPrimary = body.findings[0];
  assert.equal(renderedPrimary.id, 'P1', 'the first-seen primary wins the anchor');
  assert.deepEqual(renderedPrimary.corroborations.map((c) => c.title), [c1.title, p2.title], 'both the corroborator and the demoted second primary are present');
});

test('reportStage: the dimensions table counts RAW findings, unaffected by report-list consolidation', async () => {
  let capturedPrompt = null;
  const ctx = {
    agent: async (prompt) => { capturedPrompt = prompt; return { report: '# r' }; },
    parallel: async () => [],
  };
  const primary = makeFinding('P', { dimension: 'bug', severity: 'high', consolidation_key: 'f.js:0', consolidation_primary: true });
  const corroborator = makeFinding('C', { dimension: 'security', severity: 'medium', consolidation_key: 'f.js:0', consolidation_primary: false });
  const findings = [primary, corroborator];
  const dims = { dispatched: AGENTS, degraded: [] };
  await reportStage(ctx, { findings, unverified: [], stats: {}, dimensions: dims });

  const expectedTable = dimensionsSummaryTable({ ...dims, findings, unverified: [] });
  const body = resultsBody(capturedPrompt);
  assert.equal(body.dimensionsTable, expectedTable, 'the table must still count both findings by their own dimension, not the post-grouping list of 1');
});

test('reportStage: a writer throw degrades to the minimal report, which renders the corroboration nested under its primary', async () => {
  const ctx = {
    agent: async () => { throw new Error('boom'); },
    parallel: async () => [],
  };
  const primary = makeFinding('P', { dimension: 'bug', severity: 'high', consolidation_key: 'f.js:0', consolidation_primary: true });
  const corroborator = makeFinding('C', { dimension: 'security', severity: 'medium', agent: 'security-reviewer', confidence: 70, consolidation_key: 'f.js:0', consolidation_primary: false });
  const out = await reportStage(ctx, { findings: [primary, corroborator], unverified: [], stats: {}, dimensions: { dispatched: AGENTS, degraded: [] } });

  const lines = out.report.split('\n');
  const primaryIdx = lines.findIndex((l) => l.includes(primary.title));
  assert.ok(primaryIdx >= 0, 'the primary is rendered as a top-level bullet');
  assert.match(lines[primaryIdx + 1], /Corroborating: security-reviewer \(security, confidence 70\)/);
  assert.ok(!out.report.includes(`- [MEDIUM] ${corroborator.title}`), 'the corroborator is not rendered as its own top-level bullet');
});

// --- stripReportExcludedFields (issues #220, #226) ----------------------------
//
// The report-writer is a sampled model: no apply-check oracle exists at report time,
// so suggested_fix_code must never reach the report path — and its two removal
// stamps are dangling metadata for a field the report-writer never sees either way.
// stripReportExcludedFields is the copy-based mechanism reportStage rebinds its
// input through before any consumer reads it, iterating REPORT_EXCLUDED_FIELDS so a
// future report-excluded field is a one-line list edit, not a second copy operation.

test('stripReportExcludedFields: strips suggested_fix_code from a finding that carries it', () => {
  const f = makeFinding('F1', { suggested_fix_code: 'const x = 1;' });
  const [out] = stripReportExcludedFields([f]);
  assert.equal(out.suggested_fix_code, undefined);
  assert.ok(!('suggested_fix_code' in out), 'the key itself must be gone, not just falsy');
});

test('stripReportExcludedFields: strips all three REPORT_EXCLUDED_FIELDS and leaves the caller\'s object untouched', () => {
  const f = makeFinding('F1', {
    suggested_fix_code: 'const x = 1;',
    suggested_fix_code_removed_by: 'injection',
    suggested_fix_code_removal_reason: 'suggested_fix_code is not a string',
  });
  const [out] = stripReportExcludedFields([f]);
  for (const key of REPORT_EXCLUDED_FIELDS) {
    assert.ok(!(key in out), `${key} must be gone from the copy`);
    assert.ok(key in f, `${key} must survive on the caller's original object`);
  }
  assert.notEqual(out, f, 'a stripped finding must be a copy, never the mutated original');
});

test('stripReportExcludedFields: a stamps-only finding (the shape filterFindings leaves after it stripped the patch) is copied and both stamps are removed', () => {
  // filterFindings.js always deletes suggested_fix_code when it writes the stamps, so
  // the realistic report-path input carries the stamps WITHOUT the field.
  const f = { id: 'x', title: 't', suggested_fix_code_removed_by: 'injection', suggested_fix_code_removal_reason: 'r' };
  const [out] = stripReportExcludedFields([f]);
  assert.notStrictEqual(out, f);
  assert.ok(!('suggested_fix_code_removed_by' in out));
  assert.ok(!('suggested_fix_code_removal_reason' in out));
  assert.equal(out.title, 't');
  assert.equal(f.suggested_fix_code_removed_by, 'injection');
});

test('stripReportExcludedFields: the two removal-stamp fields are stripped by NAME, not merely by list membership', () => {
  // Iterating REPORT_EXCLUDED_FIELDS (as the test above does) checks the
  // implementation against ITSELF: if the exported list ever shrinks back to
  // just suggested_fix_code, that test's own loop would shrink with it and
  // stay green. Pin the two removal-stamp field NAMES as string literals here
  // so a shrunk REPORT_EXCLUDED_FIELDS list is caught independently of it.
  const f = makeFinding('F1', {
    suggested_fix_code: 'const x = 1;',
    suggested_fix_code_removed_by: 'injection',
    suggested_fix_code_removal_reason: 'suggested_fix_code is not a string',
  });
  const [out] = stripReportExcludedFields([f]);
  assert.ok(!('suggested_fix_code_removed_by' in out), 'suggested_fix_code_removed_by must be gone from the copy');
  assert.ok(!('suggested_fix_code_removal_reason' in out), 'suggested_fix_code_removal_reason must be gone from the copy');
  assert.equal(f.suggested_fix_code_removed_by, 'injection', "the caller's original object keeps the stamp");
  assert.equal(f.suggested_fix_code_removal_reason, 'suggested_fix_code is not a string', "the caller's original object keeps the stamp");
});

test('stripReportExcludedFields: a finding without any excluded field is returned as the SAME object (no copy made)', () => {
  const f = makeFinding('F1');
  const [out] = stripReportExcludedFields([f]);
  assert.equal(out, f, 'no excluded field present -> identity passthrough, not a copy');
});

test('stripReportExcludedFields: a finding carrying the field is copied — the original object is untouched', () => {
  const f = makeFinding('F1', { suggested_fix_code: 'const x = 1;' });
  const [out] = stripReportExcludedFields([f]);
  assert.notEqual(out, f, 'a stripped finding must be a copy, never the mutated original');
  assert.equal(f.suggested_fix_code, 'const x = 1;', 'the caller\'s original object keeps the field — delivery reads the same objects reportStage was called with');
});

test('stripReportExcludedFields: every other field survives untouched on the copy', () => {
  const f = makeFinding('F1', { suggested_fix_code: 'const x = 1;', suggestion: 'do this instead' });
  const [out] = stripReportExcludedFields([f]);
  assert.equal(out.id, 'F1');
  assert.equal(out.suggestion, 'do this instead');
  assert.equal(out.file, f.file);
  assert.equal(out.description, f.description);
});

test('stripReportExcludedFields: absent/empty input does not throw', () => {
  assert.deepEqual(stripReportExcludedFields(undefined), []);
  assert.deepEqual(stripReportExcludedFields([]), []);
});

test('stripReportExcludedFields: primitive/null members pass through unchanged and do not throw', () => {
  // 'x' in 'str' throws TypeError — a primitive element in a replayed checkpoint's
  // findings must not turn stripReportExcludedFields's guard into an unhandled throw.
  assert.deepEqual(stripReportExcludedFields([null, 'oops', 7]), [null, 'oops', 7]);
});

// --- reportStage strips suggested_fix_code before any consumer reads it (#220) ----

test('reportStage: the dispatched prompt carries no suggested_fix_code occurrence, from either the findings or the unverified bucket', async () => {
  let capturedPrompt = null;
  const ctx = {
    agent: async (prompt) => { capturedPrompt = prompt; return { report: '# r' }; },
    parallel: async () => [],
  };
  const findings = [makeFinding('B1', {
    dimension: 'bug',
    severity: 'high',
    suggested_fix_code: 'const patched = true;',
    suggested_fix_code_removed_by: 'injection',
    suggested_fix_code_removal_reason: 'x',
  })];
  const unverified = [makeFinding('B2', { dimension: 'bug', severity: 'low', suggested_fix_code: 'const alsoPatched = true;' })];
  await reportStage(ctx, { findings, unverified, stats: {}, dimensions: { dispatched: AGENTS, degraded: [] } });

  assert.equal(typeof capturedPrompt, 'string');
  // R10: every REPORT_EXCLUDED_FIELDS key — suggested_fix_code itself (no apply-check
  // oracle exists at report time) plus its two removal stamps (dangling metadata for a
  // field the report-writer never sees either way) — is stripped before ANY report-path
  // consumer reads a finding. A field-NAME check alone would pass even if the stripped
  // CODE PAYLOAD leaked, so both are asserted below.
  assert.ok(!capturedPrompt.includes('const patched = true;'), 'the report-writer prompt must not contain the stripped fix-code payload');
  assert.ok(!capturedPrompt.includes('const alsoPatched = true;'), 'the report-writer prompt must not contain the stripped unverified fix-code payload');
  const body = resultsBody(capturedPrompt);
  assert.ok(!('suggested_fix_code' in body.findings[0]), 'the findings bucket in the parsed prompt body must not carry the field');
  assert.ok(!('suggested_fix_code_removed_by' in body.findings[0]), 'the findings bucket must not carry the removal-stamp field name either');
  assert.ok(!('suggested_fix_code_removal_reason' in body.findings[0]), 'the findings bucket must not carry the removal-reason field name either');
  assert.ok(!('suggested_fix_code' in body.unverified[0]), 'the unverified bucket in the parsed prompt body must not carry the field');
});

test('reportStage: the caller\'s original finding objects still carry suggested_fix_code after the stage returns (delivery invariance)', async () => {
  const ctx = {
    agent: async () => ({ report: '# r' }),
    parallel: async () => [],
  };
  const finding = makeFinding('B1', { dimension: 'bug', severity: 'high', suggested_fix_code: 'const patched = true;' });
  const unverifiedFinding = makeFinding('B2', { dimension: 'bug', severity: 'low', suggested_fix_code: 'const alsoPatched = true;' });
  const findings = [finding];
  const unverified = [unverifiedFinding];
  await reportStage(ctx, { findings, unverified, stats: {}, dimensions: { dispatched: AGENTS, degraded: [] } });

  assert.equal(finding.suggested_fix_code, 'const patched = true;', 'the caller\'s findings array is unmutated — selectDelivery/writerPayload read this same object');
  assert.equal(unverifiedFinding.suggested_fix_code, 'const alsoPatched = true;', 'the caller\'s unverified array is unmutated');
});

test('reportStage: a segmented report strips suggested_fix_code from every segment\'s prompt', async () => {
  const prompts = [];
  const ctx = {
    agent: async (prompt, opts) => { prompts.push(prompt); return { report: `body ${opts.label}` }; },
    parallel: async (thunks) => Promise.all(thunks.map((t) => t())),
  };
  const big = Array.from({ length: 80 }, (_, i) => makeFinding(`F${i}`, {
    description: 'x'.repeat(2000), dimension: 'bug', severity: 'high', suggested_fix_code: `const patch${i} = true;`,
  }));
  await reportStage(ctx, { findings: big, unverified: [], stats: {}, dimensions: { dispatched: AGENTS, degraded: [] } });

  assert.ok(prompts.length > 1, 'fixture must segment for this test to be meaningful');
  for (const p of prompts) {
    // Distinctive PATCH PAYLOAD substring, not the field name (see #220 over-pin note above).
    assert.ok(!p.includes('const patch'), 'every segment prompt must be free of the stripped fix-code payload');
    for (const f of resultsBody(p).findings) {
      assert.ok(!('suggested_fix_code' in f), 'no finding in any segment\'s parsed prompt body may carry the field');
    }
  }
});
