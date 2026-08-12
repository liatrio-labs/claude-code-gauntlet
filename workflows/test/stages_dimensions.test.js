// stages_dimensions.test.js — dimensionsSummaryTable (issue #89): the Review Dimensions
// Summary table computed in CODE as a pure function of pipeline stats, instead of asked
// of the Phase 8 model. Unit tests call dimensionsSummaryTable directly with synthetic
// inputs and assert exact rendered strings; the reportStage-level tests at the bottom
// reuse the ctx-mock pattern from pipeline_run.test.js to check the plumbing
// (reportPrompt / minimalReport) around it.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { dimensionsSummaryTable, reportStage } from '../src/stages.js';
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
