import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeFieldNames, applyThresholdFilter, detectDisagreement } from '../src/filterFindings.js';
import { routeByDimension, consolidateCrossAgent, tagFindings, applyFilterPipeline } from '../src/filterFindings.js';
import { FIELD_RENAMES, SEVERITY_ORDER, INJECTION_STRIPPED_PROSE_FIELDS } from '../src/registry.js';
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
    name: "mixed findings count only changed rows",
    input: [
             { id: "a", description: "good", line_start: 1 },
             { id: "b", body: "legacy", line_start: 2 },
             { id: "c", description: "good", line: 3 }
           ],
    expected: {
                count: 2,
                findings: [
                            { id: "a", description: "good", line_start: 1 },
                            { id: "b", description: "legacy", line_start: 2 },
                            { id: "c", description: "good", line_start: 3 }
                          ]
              }
  },
  {
    name: "copied renames",
    input: [{ legacy: "x" }],
    renames: { ...FIELD_RENAMES, legacy: "custom" },
    expected: { count: 1, findings: [{ custom: "x" }] }
  },
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

  { name: "configured security 65 fails at 70", input: [{ dimension: "security", confidence: 65 }],
    config: { confidence_threshold: 70, security_min_confidence: 70 }, expected: { kept: [], eliminated: 1, contested: 0 } },
  { name: "critical under high severity bar", input: [{ severity: "critical" }],
    config: { confidence_threshold: 70, severity_threshold: "high" }, expected: { kept: ["f1"], eliminated: 0, contested: 0 } },
  { name: "high under high severity bar", input: [{ severity: "high" }], config: { confidence_threshold: 70, severity_threshold: "high" },
    expected: { kept: ["f1"], eliminated: 0, contested: 0 } },
  { name: "medium under high severity bar", input: [{ severity: "medium" }],
    config: { confidence_threshold: 70, severity_threshold: "high" }, expected: { kept: [], eliminated: 1, contested: 0 } },
  { name: "25 point confidence drop is not contested", input: [{ confidence: 75, original_confidence: 100 }],
    config: { confidence_threshold: 70 }, expected: { kept: ["f1"], eliminated: 0, contested: 0 } },
  { name: "26 point drop contests above threshold", input: [{ confidence: 74, original_confidence: 100 }],
    config: { confidence_threshold: 70 }, expected: { kept: ["f1"], eliminated: 0, contested: 1 } },
  { name: "nonsecurity fallback 54 fails before 55", input: [{ dimension: "bug", confidence: 54 }], config: {  },
    expected: { kept: [], eliminated: 1, contested: 0 } },
  { name: "nonsecurity fallback 55 passes", input: [{ dimension: "bug", confidence: 55 }], config: {  },
    expected: { kept: ["f1"], eliminated: 0, contested: 0 } },
  { name: "security minimum 69 fails before 70", input: [{ dimension: "security", confidence: 69 }], config: { confidence_threshold: 100 },
    expected: { kept: [], eliminated: 1, contested: 0 } },
  { name: "security minimum 70 passes", input: [{ dimension: "security", confidence: 70 }], config: { confidence_threshold: 100 },
    expected: { kept: ["f1"], eliminated: 0, contested: 0 } },
  { name: "numeric fractional confidence passes exact bar", input: [{ confidence: 72.5 }], config: { confidence_threshold: 72.5 },
    expected: { kept: ["f1"], eliminated: 0, contested: 0 } },
  { name: "numeric zero passes zero bar", input: [{ confidence: 0 }], config: { confidence_threshold: 0 },
    expected: { kept: ["f1"], eliminated: 0, contested: 0 } },
  { name: "25 point drop stays below default bar", input: [{ confidence: 40, original_confidence: 65 }], config: {  },
    expected: { kept: [], eliminated: 1, contested: 0 } },
  { name: "26 point drop contests below default bar", input: [{ confidence: 39, original_confidence: 65 }], config: {  },
    expected: { kept: ["f1"], eliminated: 0, contested: 1 } },
  { name: "security explicit confidence 55", input: [ { id: "security-60", dimension: "security", confidence: 60 },
             { id: "security-54", dimension: "security", confidence: 54 } ], config: { confidence_threshold: 55 },
    expected: { kept: ["security-60"], eliminated: 1, contested: 0 },
    reasons: [["security-54", "threshold", "confidence 54 < threshold 55"]] },
  { name: "two contested drops accumulate", input: [ { id: "c1", confidence: 40, original_confidence: 90 },
             { id: "c2", confidence: 30, original_confidence: 80 }, { id: "c3", confidence: 75 } ], config: { confidence_threshold: 70 },
    expected: { kept: ["c1", "c2", "c3"], eliminated: 0, contested: 2 } },
  { name: "null confidence", input: [{ confidence: null }], config: { confidence_threshold: 70 },
    expected: { kept: [], eliminated: 1, contested: 0 } },
  { name: "string confidence", input: [{ confidence: "90" }], config: { confidence_threshold: 70 },
    expected: { kept: [], eliminated: 1, contested: 0 } },
  { name: "boolean confidence", input: [{ confidence: true }], config: { confidence_threshold: 1 },
    expected: { kept: [], eliminated: 1, contested: 0 } },
  { name: "NaN confidence", input: [{ confidence: NaN }], config: { confidence_threshold: 1 },
    expected: { kept: [], eliminated: 1, contested: 0 } },
  { name: "null severity defaults low", input: [{ severity: null }], config: { confidence_threshold: 70 },
    expected: { kept: ["f1"], eliminated: 0, contested: 0 } },
  { name: "scoped confidence follows file",
    input: [{ id: "api-low", file: "api/x.py", confidence: 80 }, { id: "legacy-low", file: "legacy/x.py", confidence: 80 }], config: {
              confidence_threshold: 60, ignore: ["root-match"], scopes: [{ dir: "api", confidence_threshold: 90, ignore: ["api-match"] }]
            }, expected: { kept: ["legacy-low"], eliminated: 1, contested: 0 } },
  { name: 'security confidence fallback 69 fails before 70', input: [{ dimension: 'security', confidence: 69 }],
    config: { security_min_confidence: 100 }, expected: { kept: [], eliminated: 1, contested: 0 },
    reasons: [['f1', 'threshold', 'confidence 69 < threshold 70']] },
  { name: 'security confidence fallback 70 passes', input: [{ dimension: 'security', confidence: 70 }],
    config: { security_min_confidence: 100 }, expected: { kept: ['f1'], eliminated: 0, contested: 0 } },
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
  { name: "cross file impact routes main", input: { dimension: "cross_file_impact" }, expected: "main" },
  { name: "convention with functional violation promotes main", input: { dimension: "convention",
             title: "Error handling convention violation",
             description: "Violates error handling convention, causing silent data loss in production" }, expected: "main" },
  { name: "type design runtime error promotes main", input: { dimension: "type_design", title: "Type cast error",
             description: "ClassCastException at runtime when processing polymorphic types" }, expected: "main" },
  { name: "absent dimension falls through", input: { dimension: undefined }, expected: null },
  { name: "unknown dimension falls through", input: { dimension: "some_new_dimension" }, expected: null },
  { name: "uppercase bug routes main", input: { dimension: "BUG" }, expected: "main" },
  { name: "mixed case convention suggestion",
    input: { dimension: "Convention", title: "Style issue", description: "Does not follow naming convention" }, expected: "suggestion" },
  { name: "correctness pattern 0", input: { dimension: "test_coverage", title: "race condition", description: "" }, expected: "main" },
  { name: "correctness pattern 1", input: { dimension: "test_coverage", title: "always passes", description: "" }, expected: "main" },
  { name: "correctness pattern 2", input: { dimension: "test_coverage", title: "always-pass", description: "" }, expected: "main" },
  { name: "correctness pattern 3", input: { dimension: "test_coverage", title: "never fails", description: "" }, expected: "main" },
  { name: "correctness pattern 4", input: { dimension: "test_coverage", title: "vacuously", description: "" }, expected: "main" },
  { name: "correctness pattern 5", input: { dimension: "test_coverage", title: "tautology", description: "" }, expected: "main" },
  { name: "correctness pattern 6", input: { dimension: "test_coverage", title: "assertion is never reached", description: "" },
    expected: "main" },
  { name: "correctness pattern 7", input: { dimension: "test_coverage", title: "deadlock", description: "" }, expected: "main" },
  { name: "correctness pattern 8", input: { dimension: "test_coverage", title: "data race", description: "" }, expected: "main" },
  { name: "correctness pattern 9", input: { dimension: "test_coverage", title: "thread unsafe", description: "" }, expected: "main" },
  { name: "correctness pattern 10", input: { dimension: "test_coverage", title: "test actually checks nothing", description: "" },
    expected: "main" },
  { name: "correctness pattern 11", input: { dimension: "test_coverage", title: "false positive test", description: "" },
      expected: "main" },
  { name: "correctness pattern 12", input: { dimension: "test_coverage", title: "incorrectly verify", description: "" }, expected: "main" },
  { name: "correctness pattern 13", input: { dimension: "test_coverage", title: "wrong output", description: "" }, expected: "main" },
  { name: "correctness pattern 14", input: { dimension: "test_coverage", title: "local variable is never read", description: "" },
    expected: "main" },
  { name: "correctness pattern 15", input: { dimension: "test_coverage", title: "assertion snapshot", description: "" }, expected: "main" },
  { name: "correctness pattern 16", input: { dimension: "test_coverage", title: "compare different object", description: "" },
    expected: "main" },
  { name: "correctness pattern 17", input: { dimension: "test_coverage", title: "test doesn't wait", description: "" }, expected: "main" },
  { name: "correctness pattern 18", input: { dimension: "test_coverage", title: "reader thread not waited", description: "" },
    expected: "main" },
  { name: "correctness pattern 19", input: { dimension: "test_coverage", title: "flaky test", description: "" }, expected: "main" },
  { name: "correctness pattern 20", input: { dimension: "test_coverage", title: "assertion always succeed", description: "" },
    expected: "main" },
  { name: "correctness pattern 21", input: { dimension: "test_coverage", title: "assert always true", description: "" }, expected: "main" },
  { name: "correctness pattern 22", input: { dimension: "test_coverage", title: "test always succeed", description: "" },
      expected: "main" },
  { name: "correctness pattern 23", input: { dimension: "test_coverage", title: "logic error", description: "" }, expected: "main" },
  { name: "correctness pattern 24", input: { dimension: "test_coverage", title: "incorrect behaviour", description: "" },
      expected: "main" },
  { name: "typed null prose defaults suggestion", input: { dimension: "convention", title: null, description: null }, expected: "suggestion"
  },
  { name: "typed array title cannot promote", input: { dimension: "test_coverage", title: ["race condition detected"], description: "" },
    expected: "suggestion" },
];
for (const row of ROUTE_ROWS) test('dimension route: ' + row.name, () => {
  assert.equal(routeByDimension(finding(row.input), row.routes), row.expected);
});

const TAG_ROWS = [
  { name: "bug detector agent routes main", input: { dimension: undefined, agent: "bug-detector" },
    expected: ["main", "main", undefined, undefined, 1, 0] },
  { name: "convention overrides main agent", input: { dimension: "convention", agent: "type-design-analyzer", title: "Style concern",
             description: "Naming does not follow project convention" },
    expected: ["suggestion", "suggestion", "dimension", undefined, 0, 1] },
  { name: "unknown dimension falls back to agent", input: { dimension: "some_new_thing", agent: "code-simplifier" },
    expected: ["suggestion", "suggestion", undefined, undefined, 0, 1] },
  { name: "test analyzer promotion", input: { agent: "test-analyzer", dimension: undefined, title: null,
             description: "the retry counter always passes even under concurrent load, masking a race condition in the worker pool" },
    expected: ["main", "main", undefined, "test-analyzer", 1, 0] },
  { name: "test analyzer array does not form joined keyword", input: { agent: "test-analyzer", dimension: undefined, title: ["logic"],
             description: "error is not caught anywhere in this path, so it silently swallows exceptions" },
    expected: ["suggestion", "suggestion", undefined, undefined, 0, 1] },
];
for (const row of TAG_ROWS) test('tag: ' + row.name, () => {
  const out = tagFindings([finding(row.input)], row.routes);
  const item = out.tagged[0];
  assert.deepEqual([item.report_destination, item.report_tag, item.routed_by, item.promoted_from,
    out.mainCount, out.suggestionCount], row.expected);
});

const CONSOLIDATION_ROWS = [
  { name: "different files do not consolidate", input: [{ id: "f1" }, { id: "f2", file: "b.py", agent: "test-analyzer" }],
    expected: { count: 0, primary: [], stamped: 0, ids: ["f1", "f2"] } },
  { name: "distant lines do not consolidate", input: [{ id: "f1" }, { id: "f2", line_start: 100, agent: "test-analyzer" }],
    expected: { count: 0, primary: [], stamped: 0, ids: ["f1", "f2"] } },
  { name: "null confidence loses to sixty", input: [
             { id: "f1", line_start: 20, agent: "test-analyzer", dimension: "test_coverage", confidence: null },
             { id: "f2", line_start: 21, agent: "code-simplifier", dimension: "simplification", confidence: 60 } ],
    expected: { count: 2, primary: ["f2"], stamped: 2, ids: ["f1", "f2"] } },
  { name: "three agents retain all and core confidence wins", input: [
             { id: "sec-1", line_start: 20, agent: "security-reviewer", dimension: "security", confidence: 75 },
             { id: "bug-1", line_start: 21, confidence: 80 },
             { id: "test-1", line_start: 22, agent: "test-analyzer", dimension: "test_coverage", confidence: 95 } ],
    expected: { count: 3, primary: ["bug-1"], stamped: 3, ids: ["sec-1", "bug-1", "test-1"] } },
  { name: "same-agent group has no stamps", input: [{ id: "f1" }, { id: "f2", line_start: 11 }],
    expected: { count: 0, primary: [], stamped: 0, ids: ["f1", "f2"] } },
  { name: "singleton clears stale stamps", input: [{ id: "only-1", consolidation_key: "old", consolidation_primary: true }],
    expected: { count: 0, primary: [], stamped: 0, ids: ["only-1"] } },
  { name: "idless winner is skipped", input: [ { id: undefined, confidence: 95 },
             { id: "has-id", line_start: 11, agent: "test-analyzer", dimension: "test_coverage", confidence: 50 } ],
    expected: { count: 1, primary: ["has-id"], stamped: 1, ids: [undefined, "has-id"] } },
  { name: "array description loses text tie", input: [ { id: "jf1", file: "n.js", line_start: 20, agent: "test-analyzer",
               dimension: "test_coverage", confidence: 60, description: ["a", "b", "c", "d", "e"] }, { id: "jf2", file: "n.js",
               line_start: 21, agent: "code-simplifier", dimension: "simplification", confidence: 60, description: "tiny" } ],
    expected: { count: 2, primary: ["jf2"], stamped: 2, ids: ["jf1", "jf2"] } },
  { name: "null confidence falls through to description", input: [ { id: "jf3", file: "n.js", line_start: 20, agent: "test-analyzer",
               dimension: "test_coverage", confidence: null, description: "tiny" }, { id: "jf4", file: "n.js", line_start: 21,
               agent: "code-simplifier", dimension: "simplification", confidence: 0,
               description: "a much longer description that should win the tie-break" } ],
    expected: { count: 2, primary: ["jf4"], stamped: 2, ids: ["jf3", "jf4"] } },
];
for (const row of CONSOLIDATION_ROWS) test('consolidate: ' + row.name, () => {
  const out = consolidateCrossAgent(row.input.map(consolidationFinding));
  assert.deepEqual({ count: out.consolidatedCount,
    primary: out.findings.filter((item) => item.consolidation_primary).map((item) => item.id),
    stamped: out.findings.filter((item) => Object.hasOwn(item, 'consolidation_key')).length,
    ids: out.findings.map((item) => item.id) }, row.expected);
});

const BUCKET_ROWS = [
  { name: "line coercion 0", input: "\u001c12", expected: { key: "src/x.py:10", confidence: 100, bucket10: 10 } },
  { name: "line coercion 1", input: "\u001d12", expected: { key: "src/x.py:10", confidence: 100, bucket10: 10 } },
  { name: "line coercion 2", input: "\u001e12", expected: { key: "src/x.py:10", confidence: 100, bucket10: 10 } },
  { name: "line coercion 3", input: "\u001f12", expected: { key: "src/x.py:10", confidence: 100, bucket10: 10 } },
  { name: "line coercion 4", input: "\u{85}12", expected: { key: "src/x.py:10", confidence: 100, bucket10: 10 } },
  { name: "line coercion 5", input: "\u{feff}12", expected: { key: "src/x.py:10", confidence: 100, bucket10: 10 } },
  { name: "line coercion 6", input: "\u001c99", expected: { key: "src/x.py:100", confidence: 100, bucket10: 100 } },
  { name: "line coercion 7", input: "\u{661}\u{662}", expected: { key: "src/x.py:0", confidence: 100, bucket10: 0 } },
  { name: "line coercion 8", input: "\u{ff11}\u{ff12}", expected: { key: "src/x.py:0", confidence: 100, bucket10: 0 } },
  { name: "line coercion 9", input: "1_2", expected: { key: "src/x.py:0", confidence: 100, bucket10: 0 } },
  { name: "line coercion 10", input: 25.7, expected: { key: "src/x.py:25", confidence: 100, bucket10: 20 } },
  { name: "line coercion 11", input: 20, expected: { key: "src/x.py:20", confidence: 100, bucket10: 20 } },
  { name: "line coercion 12", input: null, expected: { key: "src/x.py:0", confidence: 100, bucket10: 0 } },
  { name: "line coercion 13", input: true, expected: { key: "src/x.py:0", confidence: 100, bucket10: 0 } },
  { name: "line coercion 14", input: false, expected: { key: "src/x.py:0", confidence: 100, bucket10: 0 } },
  { name: 'positive odd half rounds upward', input: 15,
    expected: { key: 'src/x.py:15', confidence: 100, bucket10: 20 } },
  { name: 'negative even half rounds downward', input: -15,
    expected: { key: 'src/x.py:-15', confidence: 100, bucket10: -20 } },
  { name: 'negative odd half rounds upward', input: -5,
    expected: { key: 'src/x.py:-5', confidence: 100, bucket10: 0 } },
  { name: 'above-half bucket boundary rounds upward', input: 13,
    expected: { key: 'src/x.py:15', confidence: 100, bucket10: 10 } },
];
for (const row of BUCKET_ROWS) test('bucket callers: ' + row.name, () => {
  const consolidated = consolidateCrossAgent([finding({ line_start: row.input }),
    finding({ id: 'other', line_start: row.input, agent: 'security-reviewer' })]);
  assert.equal(consolidated.findings[0].consolidation_key, row.expected.key);
  const disagreement = detectDisagreement([finding({ line_start: row.input }),
    finding({ id: 'other', line_start: row.expected.bucket10, agent: 'security-reviewer' })]);
  assert.equal(disagreement.active[0].confidence, row.expected.confidence);
});

const DISAGREEMENT_ROWS = [
  { name: "present null ids collide during suppression", input: [ { id: null, agent: "bug-detector" }, { id: null, agent: "test-analyzer" },
             { id: "convention", agent: "conventions-and-intent", description: "intentional generated" } ], fields: ["id"], expected: {
                active: [["convention"]], suppressed: [ [ null,
                                "suppressed:intentional",
                                "conventions-and-intent confirms behaviour at src/x.py:10 is intentional" ] ], boosted: 0 } },
  { name: "first suppression rule wins for shared id", input: [ { id: "shared", agent: "bug-detector" },
             { id: "shared", agent: "test-analyzer" },
             { id: "convention", agent: "conventions-and-intent", description: "intentional generated" } ], fields: ["id"], expected: {
                active: [["convention"]], suppressed: [ [
                                "shared",
                                "suppressed:intentional",
                                "conventions-and-intent confirms behaviour at src/x.py:10 is intentional" ] ], boosted: 0 } },
  { name: "generated suppression pattern", input: [ { id: "test", agent: "test-analyzer" },
             { id: "convention", agent: "conventions-and-intent", description: "generated scaffolding" } ], fields: ["id"], expected: {
                active: [["convention"]], suppressed: [ [
                                "test",
                                "suppressed:generated",
                                "conventions-and-intent confirms code at src/x.py:10 is generated/scaffolding" ] ], boosted: 0 } },
  { name: "array conventions title cannot suppress", input: [ { id: "jbug-2" }, { id: "jconv-2", agent: "conventions-and-intent",
               title: ["by design"], description: "unrelated commentary about code style choices" } ], fields: ["id"],
    expected: { active: [["jbug-2"], ["jconv-2"]], suppressed: [], boosted: 2 } },
  { name: "all verified", input: [ { id: "V1", origin: "verified", confidence: 70 },
             { id: "V2", origin: "verified", line_start: 11, agent: "security-reviewer", confidence: 60 } ],
    fields: ["confidence", "consensus_count"], expected: { active: [[80, 2], [70, 2]], suppressed: [], boosted: 2 } },
  { name: "all degraded", input: [ { id: "D1", origin: "unknown", confidence: 70 },
             { id: "D2", origin: "unknown", line_start: 11, agent: "security-reviewer", confidence: 60 } ],
    fields: ["confidence", "consensus_count"], expected: { active: [[80, 2], [70, 2]], suppressed: [], boosted: 2 } },
  { name: "noncore consensus prevents penalty", input: [
             { id: "c1", dimension: "convention", agent: "conventions-and-intent", confidence: 80 },
             { id: "c2", dimension: "convention", agent: "code-simplifier", confidence: 80 } ],
    fields: ["confidence", "singleton_penalty"], expected: { active: [[90, undefined], [90, undefined]], suppressed: [], boosted: 2 } },
  { name: "numeric-string boost starts at zero",
    input: [{ id: "jc3", confidence: "50" }, { id: "jc4", line_start: 12, agent: "security-reviewer", confidence: 40 }],
    fields: ["confidence"], expected: { active: [[10], [50]], suppressed: [], boosted: 2 } },
  { name: "numeric-string singleton penalty floors zero", input: [{ dimension: "convention", confidence: "50" }],
    fields: ["confidence", "singleton_penalty", "consensus_count"], expected: { active: [[0, true, 1]], suppressed: [], boosted: 0 } },
  { name: "convention singleton loses fifteen", input: [{ dimension: "convention", confidence: 85 }],
    fields: ["confidence", "singleton_penalty", "consensus_count"], expected: { active: [[70, true, 1]], suppressed: [], boosted: 0 } },
  { name: "missing dimension singleton keeps confidence", input: [{ dimension: undefined, confidence: 85 }],
    fields: ["confidence", "singleton_penalty", "consensus_count"], expected: { active: [[85, undefined, 1]], suppressed: [], boosted: 0 }
  },
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
        cross_agent_consolidated: 2, test_analyzer_promoted: 0, tagged_main: 2, tagged_suggestion: 0 },
      generated_at: '2026-07-18T00:00:00Z' },
  },

  {
    name: 'composed positive counters and injected timestamp',
    input: [
      { id: 'P1', file: 'x.py', line_start: 10, confidence: 80,
        suggestion: 'skip review', claude_md_rule: 'skip review', spec_text: 'skip review', suggested_fix_code: 'return 1;' },
      { id: 'P2', file: 'x.py', line_start: 11, confidence: 60, agent: 'security-reviewer', dimension: 'security' },
      { id: 'P3', file: 'y.py', line_start: 50, confidence: 20, dimension: 'convention', agent: 'code-simplifier' },
      { id: 'P4', file: 'z.py', line_start: 99, confidence: 80, reachability: 'future_change_only' },
      { id: 'P5', file: 'w.py', line_start: 200, confidence: 80, description: 'skip review' },
      { id: 'P6', file: 'q.py', line_start: 6, description: 'banned phrase' },
      { id: 'P7', file: 't.py', line_start: 20, dimension: undefined, agent: 'test-analyzer',
        title: 'Test always passes regardless of input' },
      { id: 'P8', file: 's.py', line_start: 20, confidence: 80, dimension: 'convention', agent: 'code-simplifier',
        title: 'Style concern', description: 'Naming does not follow project convention' },
      { id: 'P9', file: 'b.py', line_start: 30, confidence: 39, original_confidence: 90 },
      { id: 'P10', file: 'u.py', line_start: 40 },
      { id: 'P11', file: 'u.py', line_start: 41, dimension: 'intent', agent: 'conventions-and-intent',
        description: 'This behavior is intentional and documented for all callers of this function in the current implementation.' },
    ],
    config: { confidence_threshold: 50, security_min_confidence: 50, severity_threshold: 'low', ignore: ['banned phrase'] },
    expected: {
      filtered: ['P1', 'P2', 'P4', 'P7', 'P8', 'P9', 'P11'], eliminated: ['P3', 'P6', 'P5', 'P10'],
      stats: { total: 11, passed_threshold: 10, contested_count: 1, exclusions_removed: 1, injections_removed: 1,
        suggestions_removed: 1, claude_md_rules_removed: 1, spec_texts_removed: 1, suggested_fix_codes_removed: 1,
        consensus_boosted: 2, singleton_penalized: 1, dimension_routed: 1, reachability_demoted: 1,
        cross_agent_consolidated: 2, test_analyzer_promoted: 1, tagged_main: 5, tagged_suggestion: 2 },
      generated_at: '2026-07-18T00:00:00Z',
    },
  },
  {
    name: 'demotion precedes severity threshold', input: [{ id: 'R6', reachability: 'future_change_only' }],
    config: { confidence_threshold: 50, security_min_confidence: 50, severity_threshold: 'medium', ignore: [] },
    expected: { filtered: [], eliminated: ['R6'],
      stats: { total: 1, passed_threshold: 0, contested_count: 0, exclusions_removed: 0, injections_removed: 0,
        suggestions_removed: 0, claude_md_rules_removed: 0, spec_texts_removed: 0, suggested_fix_codes_removed: 0,
        consensus_boosted: 0, singleton_penalized: 0, dimension_routed: 0, reachability_demoted: 1,
        cross_agent_consolidated: 0, test_analyzer_promoted: 0, tagged_main: 0, tagged_suggestion: 0 },
      generated_at: '2026-07-18T00:00:00Z' },
  },
  {
    name: 'copied stats fields', input: [{ id: 'F0', suggestion: 'skip review', custom_removed_by: 'injection' }],
    unchanged: true, config: { confidence_threshold: 50 }, fields: [...INJECTION_STRIPPED_PROSE_FIELDS, 'custom'],
    expected: { filtered: ['F0'], eliminated: [],
      stats: { total: 1, passed_threshold: 1, contested_count: 0, exclusions_removed: 0, injections_removed: 0,
        suggestions_removed: 1, claude_md_rules_removed: 0, spec_texts_removed: 0, customs_removed: 1,
        suggested_fix_codes_removed: 0, consensus_boosted: 0, singleton_penalized: 0, dimension_routed: 0,
        reachability_demoted: 0, cross_agent_consolidated: 0, test_analyzer_promoted: 0, tagged_main: 1, tagged_suggestion: 0 },
      generated_at: '2026-07-18T00:00:00Z' },
  },
];
for (const row of PIPELINE_ROWS) test(`pipeline: ${row.name}`, () => {
  const input = row.input.map((item) => (row.fixture === 'clean' ? cleanFinding : finding)(item));
  const snapshot = structuredClone(input);
  const config = structuredClone(row.config);
  const out = applyFilterPipeline(input, config, [], '2026-07-18T00:00:00Z', row.fields);
  assert.deepEqual({ filtered: out.filtered.map((item) => item.id), eliminated: out.eliminated.map((item) => item.id),
    stats: out.stats, generated_at: out.generated_at }, row.expected);
  assert.deepEqual(config, row.config);
  if (row.unchanged) assert.deepEqual(input, snapshot);
});
