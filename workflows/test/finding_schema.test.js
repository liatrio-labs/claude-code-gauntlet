// finding_schema.test.js — the DISPATCHED finding schema, checked against the registry
// that declares it (issue #47).
//
// tests/test_dimensions_registry.py owns the other half of this loop: it pins the registry
// against the seven agent .md output contracts and against the prose in CLAUDE.md and
// report-format.md. What Python cannot see is what actually reaches the platform — the
// schema object handed to agent({schema}) at each dispatch. That is what these tests read,
// off a mock ctx, exactly as stages_discover/stages_verify already do.
//
// Why both directions matter: a field the registry declares but findingItemSchema drops on
// the way to the dispatch is invisible to the Python guard (the two prose lists would still
// agree with each other while the wire carried neither), and a field the schema declares
// that the registry never named would be equally invisible. So these assertions are DERIVED
// from the registry rather than hand-listed — with one deliberate exception, the named
// regression pin at the bottom, because a derived test cannot notice a field being deleted
// from both sides at once.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { DIMENSIONS, FINDING_PROP_TYPES, FINDING_REQUIRED } from '../src/registry.js';
import { agentSpecs, discover, verifyStage, parseWriterPayload } from '../src/stages.js';

// The property NAME set findingItemSchema must produce for one agent: the canonical map
// unioned with every dimension that agent covers.
function expectedFieldsFor(agentType) {
  const fields = new Set(Object.keys(FINDING_PROP_TYPES));
  for (const d of DIMENSIONS) {
    if (d.agentType === agentType) for (const k of Object.keys(d.schemaExtra || {})) fields.add(k);
  }
  return fields;
}

// 'string' | { type: 'array', ... } -> 'string' | 'array'
const typeName = (v) => (typeof v === 'string' ? v : v.type);

function declaredType(agentType, field) {
  for (const d of DIMENSIONS) {
    if (d.agentType === agentType && (d.schemaExtra || {})[field] !== undefined) {
      return typeName(d.schemaExtra[field]);
    }
  }
  return typeName(FINDING_PROP_TYPES[field]);
}

// Capture every discovery dispatch's schema, keyed by agentType.
async function discoverySchemas() {
  const schemas = {};
  const ctx = {
    agent: async (_prompt, opts = {}) => {
      schemas[opts.agentType] = opts.schema;
      return { findings: [], complete: true, total_seen: 0 };
    },
    parallel: async (thunks) => Promise.all(thunks.map((t) => t())),
  };
  await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy: {} });
  return schemas;
}

test('every discovery dispatch declares exactly the fields the registry names for that agent', async () => {
  const schemas = await discoverySchemas();
  const specs = agentSpecs();
  assert.equal(Object.keys(schemas).length, specs.length, 'one dispatch per unique agent');

  for (const spec of specs) {
    const props = schemas[spec.agentType].properties.findings.items.properties;
    assert.deepEqual(
      new Set(Object.keys(props)),
      expectedFieldsFor(spec.agentType),
      `${spec.agentType}: dispatched schema fields differ from the registry declaration`,
    );
  }
});

test('every declared field reaches the dispatch with the registry-declared type', async () => {
  const schemas = await discoverySchemas();
  for (const spec of agentSpecs()) {
    const props = schemas[spec.agentType].properties.findings.items.properties;
    for (const field of expectedFieldsFor(spec.agentType)) {
      assert.equal(
        props[field].type,
        declaredType(spec.agentType, field),
        `${spec.agentType}.${field}: dispatched type differs from the registry`,
      );
      // An array-valued field is only usable if it declares `items` — the platform's schema
      // validator rejects a bare {type:'array'} before dispatch.
      if (props[field].type === 'array') {
        assert.ok(props[field].items, `${spec.agentType}.${field}: array without items`);
      }
    }
  }
});

test('required is the flat FINDING_REQUIRED — no per-dimension extra sneaks into it', async () => {
  const schemas = await discoverySchemas();
  // A field required for ONE dimension cannot be marked required here: `required` is shared
  // by every dispatch AND by the verify echo, which carries all agents' findings mixed
  // together, so marking (say) criticality required would reject every non-test finding.
  const extras = new Set(DIMENSIONS.flatMap((d) => Object.keys(d.schemaExtra || {})));
  for (const spec of agentSpecs()) {
    const { required } = schemas[spec.agentType].properties.findings.items;
    assert.deepEqual(required, FINDING_REQUIRED, `${spec.agentType}: required list drifted`);
    for (const field of required) {
      assert.ok(!extras.has(field), `${field} is a per-dimension extra and must not be required`);
    }
  }
});

test('the verify echo schema unions EVERY dimension extra, not just one agent set', async () => {
  // The verify slice carries post-merge findings from all agents mixed together, so its echo
  // item schema must declare every agent's extras or the executor drops a field belonging to
  // an agent other than the one it happens to be transcribing.
  //
  // Only the EXECUTOR dispatch's schema is under test here, so the mock is deliberately
  // minimal: the slice-input writer echoes its paths so the write-proof gate passes, and the
  // executor result is left unusable. verifyStage then degrades to UNVERIFIED, which is fine
  // — the schema was already handed to the dispatch by then.
  const execSchemas = [];
  const ctx = {
    agent: async (prompt, opts = {}) => {
      if ((opts.label || '').startsWith('verify-input-writer')) {
        return { written: (parseWriterPayload(prompt) || []).map((e) => e.path) };
      }
      execSchemas.push(opts.schema);
      return null;
    },
    parallel: async (thunks) => Promise.all(thunks.map((t) => t())),
  };
  await verifyStage(ctx, {
    findings: [{ id: 'F1', file: 'a.js', line_start: 1, origin: 'new', dimension: 'bug', cross_file_refs: [] }],
    nonce: 'n-1',
    headShaShort: 'abc123',
    limits: { verifySliceSize: 200 },
    policy: {},
    verify: {
      scriptPath: '/plugin/scripts/verify_findings.py',
      inputPathBase: '/out/phase4-input-abc123',
      outputPathBase: '/out/phase4-output-abc123',
      baseBranch: 'main',
      diffPath: '/out/code-gauntlet-diff-abc123.patch',
    },
  });
  assert.equal(execSchemas.length, 1, 'exactly one executor dispatch for a single slice');
  const schema = execSchemas[0];

  const everyField = new Set(Object.keys(FINDING_PROP_TYPES));
  for (const d of DIMENSIONS) for (const k of Object.keys(d.schemaExtra || {})) everyField.add(k);
  // elimination_reason is verify-only: run_verification always stamps it on a real
  // elimination, so it must be declarable or the fidelity gate false-fires.
  everyField.add('elimination_reason');

  for (const arr of ['verified', 'eliminated']) {
    const props = schema.properties.result.properties[arr].items.properties;
    assert.deepEqual(new Set(Object.keys(props)), everyField,
      `verify echo (${arr}) does not declare exactly the registry union + elimination_reason`);
  }
});

test('the fields issue #47 added are declared, on the right agents, with the right types', async () => {
  // The one hand-written pin here. Everything above is derived from the registry, so it stays
  // green if a field is deleted from the registry AND the contracts together — which is
  // exactly the regression that would silently re-open #47. These five names were instructed
  // by the agent contracts for the whole life of the v3 pipeline and declared by nothing, so
  // every finding lost them at the StructuredOutput boundary before merge ever ran.
  const schemas = await discoverySchemas();
  const props = (agentType) => schemas[agentType].properties.findings.items.properties;

  for (const spec of agentSpecs()) {
    // suggestion + claude_md_rule are canonical: instructed by all 7 contracts.
    assert.equal(props(spec.agentType).suggestion.type, 'string', `${spec.agentType}: suggestion`);
    assert.equal(props(spec.agentType).claude_md_rule.type, 'string', `${spec.agentType}: claude_md_rule`);
  }

  const testAnalyzer = props('code-gauntlet:test-analyzer');
  assert.equal(testAnalyzer.criticality.type, 'number', 'criticality is a NUMBER (1-10 impact)');
  assert.equal(testAnalyzer.criticality.minimum, 1, 'criticality lower bound is 1');
  assert.equal(testAnalyzer.criticality.maximum, 10, 'criticality upper bound is 10');
  assert.equal(testAnalyzer.failure_scenario.type, 'string');

  const conventions = props('code-gauntlet:conventions-and-intent');
  assert.equal(conventions.spec_text.type, 'string', 'intent -> spec_text');

  // None of the five may be required: required is flat across all dimensions.
  for (const field of ['suggestion', 'claude_md_rule', 'criticality', 'failure_scenario', 'spec_text']) {
    assert.ok(!FINDING_REQUIRED.includes(field), `${field} must stay schema-optional`);
  }

  // suggested_fix_code is deliberately NOT implemented: post_review.py renders it if a caller
  // supplies it, but no agent emits it and no schema declares it. report-format.md documents
  // it under "Delivery-side fields" and tests/test_dimensions_registry.py fails if it ever
  // becomes declared without the docs moving with it.
  assert.ok(!('suggested_fix_code' in props('code-gauntlet:bug-detector')),
    'suggested_fix_code must not be declared without implementing it end to end');
});
