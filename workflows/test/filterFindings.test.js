import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeFieldNames, applyThresholdFilter, detectDisagreement } from '../src/filterFindings.js';
import { routeByDimension, consolidateCrossAgent, tagFindings, applyFilterPipeline } from '../src/filterFindings.js';
import { FIELD_RENAMES, SEVERITY_ORDER } from '../src/registry.js';
import { finding, cleanFinding, consolidationFinding } from './helpers/findings.js';

const NORMALIZE_ROWS = [
  {
    name: "existing description retains body",
    input: [{ id: "n2", body: "old body", description: "canonical desc" }],
    expected: { count: 0, findings: [{ id: "n2", body: "old body", description: "canonical desc" }] }
  },

  {
    name: "all three legacy fields count once",
    input: [{ id: "n7", body: "explanation", line: 99, blame_tag: "new" }],
    expected: { count: 1, findings: [{ id: "n7", description: "explanation", line_start: 99, origin: "new" }] }
  },

  {
    name: "copied renames",
    input: [{ legacy: "x" }],
    renames: { ...FIELD_RENAMES, legacy: "custom" },
    expected: { count: 1, findings: [{ custom: "x" }] }
  }
];
for (const row of NORMALIZE_ROWS) test('normalize: ' + row.name, () => {
  const input = structuredClone(row.input);
  const count = normalizeFieldNames(input, row.renames);
  assert.deepEqual({ count, findings: input }, row.expected);
});

const THRESHOLD_ROWS = [
  { name: 'copied severity order reaches threshold comparison', input: [{ severity: 'medium' }],
    config: { confidence_threshold: 70, severity_threshold: 'high' }, severityOrder: [...SEVERITY_ORDER].reverse(),
    expected: { kept: ['f1'], eliminated: 0, contested: 0 } },

  { name: 'invalid severity defaults low before severity bar', input: [{ severity: 'unrecognized' }],
    config: { confidence_threshold: 70, severity_threshold: 'medium' }, expected: { kept: [], eliminated: 1, contested: 0 },
    reasons: [['f1', 'threshold', "severity 'low' is below threshold 'medium'"]] },

  { name: 'null confidence setting uses public fallback', input: [{ confidence: 50 }],
    config: { confidence_threshold: null }, expected: { kept: [], eliminated: 1, contested: 0 } },

  { name: "26 point drop contests above threshold", input: [{ confidence: 74, original_confidence: 100 }],
    config: { confidence_threshold: 70 }, expected: { kept: ["f1"], eliminated: 0, contested: 1 } },

  { name: "security minimum 70 passes", input: [{ dimension: "security", confidence: 70 }], config: { confidence_threshold: 100 },
    expected: { kept: ["f1"], eliminated: 0, contested: 0 } },

  { name: "security explicit confidence 55", input: [ { id: "security-60", dimension: "security", confidence: 60 },
             { id: "security-54", dimension: "security", confidence: 54 } ], config: { confidence_threshold: 55 },
    expected: { kept: ["security-60"], eliminated: 1, contested: 0 },
    reasons: [["security-54", "threshold", "confidence 54 < threshold 55"]] },

  { name: "NaN confidence", input: [{ confidence: NaN }], config: { confidence_threshold: 1 },
    expected: { kept: [], eliminated: 1, contested: 0 } },

  { name: 'security confidence fallback 69 fails before 70', input: [{ dimension: 'security', confidence: 69 }],
    config: { security_min_confidence: 100 }, expected: { kept: [], eliminated: 1, contested: 0 },
    reasons: [['f1', 'threshold', 'confidence 69 < threshold 70']] }
];
for (const row of THRESHOLD_ROWS) test('threshold: ' + row.name, () => {
  const out = applyThresholdFilter(row.input.map(finding), row.config, row.severityOrder);
  assert.deepEqual({ kept: out.kept.map((item) => item.id), eliminated: out.eliminated.length,
    contested: out.contestedCount }, row.expected);
  if (row.expected.contested) assert.equal(out.kept[0].contested, true);
  if (row.reasons) assert.deepEqual(out.eliminated.map((item) =>
    [item.id, item.eliminated_by, item.elimination_reason]), row.reasons);
});

const ROUTE_ROWS = [
  { name: 'cross file impact routes main', input: { dimension: 'cross_file_impact' }, expected: 'main' },
  { name: 'convention with wrong result promotes main', input: { dimension: 'convention', title: 'Incorrect return value',
    description: 'The function returns wrong result for edge cases' }, expected: 'main' },
];
for (const row of ROUTE_ROWS) test('dimension route: ' + row.name, () => {
  assert.equal(routeByDimension(finding(row.input), row.routes), row.expected);
});

const TAG_ROWS = [
  { name: "bug detector agent routes main", input: { dimension: undefined, agent: "bug-detector" },
    expected: ["main", "main", undefined, undefined, 1, 0] },

  { name: "unknown dimension falls back to agent", input: { dimension: "some_new_thing", agent: "code-simplifier" },
    expected: ["suggestion", "suggestion", undefined, undefined, 0, 1] },

];
for (const row of TAG_ROWS) test('tag: ' + row.name, () => {
  const out = tagFindings([finding(row.input)], row.routes);
  const item = out.tagged[0];
  assert.deepEqual([item.report_destination, item.report_tag, item.routed_by, item.promoted_from,
    out.mainCount, out.suggestionCount], row.expected);
});


const BUCKET_ROWS = [
  { name: "line coercion 6", input: "\u001c99", expected: { key: "src/x.py:100", confidence: 100, bucket10: 100 } },

  { name: "line coercion 7", input: "\u{661}\u{662}", expected: { key: "src/x.py:0", confidence: 100, bucket10: 0 } },

  { name: "line coercion 10", input: 25.7, expected: { key: "src/x.py:25", confidence: 100, bucket10: 20 } },

  { name: "line coercion 12", input: null, expected: { key: "src/x.py:0", confidence: 100, bucket10: 0 } },

  { name: 'positive odd half rounds upward', input: 15,
    expected: { key: 'src/x.py:15', confidence: 100, bucket10: 20 } },

  { name: 'above-half bucket boundary rounds upward', input: 13,
    expected: { key: 'src/x.py:15', confidence: 100, bucket10: 10 } }
];
for (const row of BUCKET_ROWS) test('bucket callers: ' + row.name, () => {
  const consolidated = consolidateCrossAgent([finding({ line_start: row.input }),
    finding({ id: 'other', line_start: row.input, agent: 'security-reviewer' })]);
  assert.equal(consolidated.findings[0].consolidation_key, Number.isSafeInteger(row.input) && row.input > 0 ? row.expected.key : undefined);
  const disagreement = detectDisagreement([finding({ line_start: row.input }),
    finding({ id: 'other', line_start: row.expected.bucket10, agent: 'security-reviewer' })]);
  assert.equal(disagreement.active[0].confidence, Number.isSafeInteger(row.input) && row.input > 0 ? row.expected.confidence : 90);
});

const DISAGREEMENT_ROWS = [
  { name: "present null ids collide during suppression", input: [ { id: null, agent: "bug-detector" }, { id: null, agent: "test-analyzer" },
             { id: "convention", agent: "conventions-and-intent", description: "intentional generated" } ], fields: ["id"], expected: {
                active: [["convention"]], suppressed: [ [ null,
                                "suppressed:intentional",
                                "conventions-and-intent confirms behaviour at src/x.py:10 is intentional" ] ], boosted: 0 } },

  { name: "generated suppression pattern", input: [ { id: "test", agent: "test-analyzer" },
             { id: "convention", agent: "conventions-and-intent", description: "generated scaffolding" } ], fields: ["id"], expected: {
                active: [["convention"]], suppressed: [ [
                                "test",
                                "suppressed:generated",
                                "conventions-and-intent confirms code at src/x.py:10 is generated/scaffolding" ] ], boosted: 0 } },

  { name: "numeric-string singleton penalty floors zero", input: [{ dimension: "convention", confidence: "50" }],
    fields: ["confidence", "singleton_penalty", "consensus_count"], expected: { active: [[0, true, 1]], suppressed: [], boosted: 0 } },

  { name: "convention singleton loses fifteen", input: [{ dimension: "convention", confidence: 85 }],
    fields: ["confidence", "singleton_penalty", "consensus_count"], expected: { active: [[70, true, 1]], suppressed: [], boosted: 0 } }
];
for (const row of DISAGREEMENT_ROWS) test(`disagreement: ${row.name}`, () => {
  const out = detectDisagreement(row.input.map(finding), row.rules);
  assert.deepEqual({ active: out.active.map((item) => row.fields.map((field) => item[field])),
    suppressed: out.suppressed.map((item) => [item.id, item.eliminated_by, item.elimination_reason]),
    boosted: out.boostedCount }, row.expected);
});

const PIPELINE_ROWS = [
  {
    name: 'null severity reaches exact-location escalation', fixture: 'clean',
    input: [{ id: 'sev-null', file: 'a.js', line_start: 10, agent: 'bug-detector', dimension: 'bug',
        severity: null, title: 'Null deref risk' },
      { id: 'sev-other', file: 'a.js', line_start: 10, agent: 'security-reviewer', dimension: 'security',
        title: 'Escalated security concern' }],
    config: { confidence_threshold: 50, security_min_confidence: 50, severity_threshold: 'low', ignore: [] },
    expected: { filtered: ['sev-null', 'sev-other'], eliminated: [],
      stats: { total: 2, passed_threshold: 2, contested_count: 0, exclusions_removed: 0, injections_removed: 0,
        suggestions_removed: 0, claude_md_rules_removed: 0, spec_texts_removed: 0, suggested_fix_codes_removed: 0,
        consensus_boosted: 2, singleton_penalized: 0, dimension_routed: 0, reachability_demoted: 0,
        test_analyzer_promoted: 0, tagged_main: 2, tagged_suggestion: 0 },
      generated_at: '2026-07-18T00:00:00Z' },
  },

];
for (const row of PIPELINE_ROWS) test(`pipeline: ${row.name}`, () => {
  const input = row.input.map((item) => (row.fixture === 'clean' ? cleanFinding : finding)(item));
  const config = structuredClone(row.config);
  const out = applyFilterPipeline(input, config, [], '2026-07-18T00:00:00Z');
  assert.deepEqual({ filtered: out.filtered.map((item) => item.id), eliminated: out.eliminated.map((item) => item.id),
    stats: out.stats, generated_at: out.generated_at }, row.expected);
  assert.deepEqual(config, row.config);
});

test('unconfirmed locations do not affect spatial policy', () => {
  const list = [
    cleanFinding({ id: 'bug', location_status: 'unconfirmed', agent: 'bug-detector', dimension: 'bug', severity: 'critical' }),
    cleanFinding({ id: 'conv', location_status: 'unconfirmed', agent: 'conventions-and-intent', dimension: 'convention', severity: 'low', description: 'This intentional behavior is by design and has enough words to describe the claim.' }),
    cleanFinding({ id: 'same-title', location_status: 'unconfirmed', agent: 'test-analyzer', dimension: 'test_coverage' }),
  ];
  for (const f of list) Object.assign(f, { consensus_count: 9, corroborated_by: ['stale'], consolidation_key: 'stale', consolidation_primary: true, contradiction: true });
  const out = applyFilterPipeline(list, {}, [], 'fixed');
  assert.equal(out.filtered.length, 3);
  for (const f of out.filtered) {
    assert.equal(f.consensus_count, 1);
    assert.equal(f.consensus_boost, 0);
    assert.deepEqual(f.corroborated_by, []);
    assert.equal(f.contradiction, false);
    assert.equal(f.consolidation_key, undefined);
    assert.equal(f.confidence, f.id === 'bug' ? 90 : 75);
  }
  assert.equal(out.filtered.find((f) => f.id === 'conv').singleton_penalty, true);
});
