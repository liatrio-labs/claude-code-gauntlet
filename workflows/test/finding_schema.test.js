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

// Capture every discovery dispatch's schema, keyed by agentType. `policy` defaults to {}
// (firstParty, no gateway) — parameterized (issue #218) so callers can capture the schema
// under a policy that gates the conditional per-dimension construct off (bedrock/vertex/
// foundry, or firstParty-with-gateway) as well as the default active case.
async function discoverySchemas(policy = {}) {
  const schemas = {};
  const ctx = {
    agent: async (_prompt, opts = {}) => {
      schemas[opts.agentType] = opts.schema;
      return { findings: [], complete: true, total_seen: 0 };
    },
    parallel: async (thunks) => Promise.all(thunks.map((t) => t())),
  };
  await discover(ctx, { changedFiles: ['a.js'], agentFlags: {}, limits: {}, policy });
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

test('required is FINDING_REQUIRED plus each dispatch\'s effective requiredExtra — exact per-agent table', async () => {
  // issue #66: a field required by ALL dimensions sharing a dispatch is promoted into that
  // dispatch's `required`; FINDING_REQUIRED itself stays the flat, per-dimension-free base
  // every dispatch's required list is built from.
  const schemas = await discoverySchemas();
  const expectedExtras = {
    'code-gauntlet:bug-detector': [],
    'code-gauntlet:security-reviewer': ['attack_vector'],
    'code-gauntlet:cross-file-impact': ['affected_consumers'],
    'code-gauntlet:test-analyzer': ['criticality', 'failure_scenario'],
    'code-gauntlet:conventions-and-intent': [],
    'code-gauntlet:type-design-analyzer': [],
    'code-gauntlet:code-simplifier': ['behavior_preserved'],
  };
  for (const spec of agentSpecs()) {
    const { required } = schemas[spec.agentType].properties.findings.items;
    assert.deepEqual(
      required.slice(0, FINDING_REQUIRED.length),
      FINDING_REQUIRED,
      `${spec.agentType}: FINDING_REQUIRED must be an exact prefix of the dispatched required list`,
    );
    assert.deepEqual(
      required,
      [...FINDING_REQUIRED, ...expectedExtras[spec.agentType]],
      `${spec.agentType}: required list does not match FINDING_REQUIRED + expected requiredExtra`,
    );
  }
});

test('every discovery dispatch closes the finding item schema to undeclared fields', async () => {
  // Issue #53 requirement 3. Before this, an undeclared property was permitted-but-unenforced
  // pass-through: the same field survived 0/8, 8/8 and 5/5 across three PRs of one smoke.
  // `additionalProperties: false` replaces that coin flip with a rejection the platform retries.
  const schemas = await discoverySchemas();
  for (const spec of agentSpecs()) {
    assert.equal(
      schemas[spec.agentType].properties.findings.items.additionalProperties,
      false,
      `${spec.agentType}: the finding item schema must be closed to undeclared fields`,
    );
  }
});

test('a CLOSED item schema declares every field it requires — no unsatisfiable schema', async () => {
  // `required` and `additionalProperties: false` are only safe TOGETHER. A required name that
  // `properties` does not declare is satisfiable while the schema is open (the value arrives as
  // an additional property) and UNSATISFIABLE once it is closed — every finding an agent emits
  // violates the schema, the platform retries to the cap, and the dimension degrades. Nothing
  // else in this file pins required ⊆ properties, and the first test's derived-set equality
  // cannot: it compares properties against the registry, never against FINDING_REQUIRED. Both
  // legs are asserted here, on the same schema object, so the pair cannot drift apart.
  const schemas = await discoverySchemas();
  for (const spec of agentSpecs()) {
    const items = schemas[spec.agentType].properties.findings.items;
    assert.equal(items.additionalProperties, false, `${spec.agentType}: item schema is not closed`);
    for (const field of items.required) {
      assert.ok(
        field in items.properties,
        `${spec.agentType}: required "${field}" is not declared in properties — a CLOSED schema ` +
          'that requires an undeclared field can never be satisfied',
      );
    }
    // issue #218: the same no-unsatisfiable guarantee extends to the conditional allOf's
    // `then.required` arrays — a then-required field the item schema never declares would be
    // just as unsatisfiable as a flat-required one, only triggered per-dimension instead of
    // on every finding.
    for (const clause of items.allOf || []) {
      for (const field of clause.then.required) {
        assert.ok(
          field in items.properties,
          `${spec.agentType}: conditionally-required "${field}" is not declared in properties`,
        );
      }
    }
  }
});

// --- Conditional per-dimension required (issue #218) ------------------------------

// Every policy under which the conditional allOf/if/then construct must NOT appear anywhere
// in any dispatched schema: any third-party provider, and firstParty explicitly paired with
// gateway:true. Contract prose stays the enforcement floor on every one of these.
const INACTIVE_POLICIES = [
  { provider: 'bedrock' },
  { provider: 'vertex' },
  { provider: 'foundry' },
  { provider: 'firstParty', gateway: true },
  { gateway: true }, // absent provider (firstParty by default) + gateway:true
];

test('firstParty/absent/null + no gateway: conventions-and-intent carries the exact allOf construct, dimension stays unpinned (no enum)', async () => {
  for (const policy of [{}, { provider: 'firstParty' }, { provider: null }, { provider: undefined, gateway: false }]) {
    const schemas = await discoverySchemas(policy);
    const items = schemas['code-gauntlet:conventions-and-intent'].properties.findings.items;
    assert.deepEqual(
      items.allOf,
      [
        { if: { properties: { dimension: { const: 'convention' } }, required: ['dimension'] }, then: { required: ['claude_md_rule'] } },
        { if: { properties: { dimension: { const: 'intent' } }, required: ['dimension'] }, then: { required: ['spec_text'] } },
      ],
      `active policy ${JSON.stringify(policy)}: allOf must be the exact measured spelling, both entries, sorted by dimension`,
    );
    // [delta round] NO enum on dimension, even on the active-policy dispatch that carries
    // the allOf construct: an enum would turn an observed, tolerated variant dimension
    // spelling (filterFindings.js's own normalization sets) into a whole-dispatch schema
    // violation. The case-variant escape this would have closed is an accepted fail-open risk.
    assert.deepEqual(
      items.properties.dimension,
      { type: 'string' },
      `active policy ${JSON.stringify(policy)}: dimension must stay unpinned — no enum`,
    );
  }
});

test('firstParty/no gateway: every OTHER agent\'s schema carries no allOf', async () => {
  const schemas = await discoverySchemas({});
  for (const spec of agentSpecs()) {
    if (spec.agentType === 'code-gauntlet:conventions-and-intent') continue;
    const items = schemas[spec.agentType].properties.findings.items;
    assert.equal(items.allOf, undefined, `${spec.agentType}: must carry no allOf`);
    assert.deepEqual(items.properties.dimension, { type: 'string' }, `${spec.agentType}: dimension must stay unpinned`);
  }
});

test('bedrock/vertex/foundry, and firstParty-with-gateway: no allOf anywhere', async () => {
  for (const policy of INACTIVE_POLICIES) {
    const schemas = await discoverySchemas(policy);
    for (const [agentType, schema] of Object.entries(schemas)) {
      const items = schema.properties.findings.items;
      assert.equal(items.allOf, undefined, `policy ${JSON.stringify(policy)}, ${agentType}: must carry no allOf`);
      assert.deepEqual(items.properties.dimension, { type: 'string' }, `policy ${JSON.stringify(policy)}, ${agentType}: dimension must stay unpinned`);
    }
  }
});

test('the verify echo declares NO finding item at all — the registry union stops at discovery', async () => {
  // This test used to assert the opposite: that the verify echo's item schema unioned every
  // dimension's extras, because the slice carried post-merge findings from all agents mixed
  // together and any extra it failed to declare was dropped when the executor transcribed
  // the findings "verbatim via the schema".
  //
  // Issue #25 PR2 removed the reason for that union. The executor no longer echoes findings —
  // it echoes a per-id DELTA of what verify_findings.py decided, and the workflow joins that
  // onto the findings it already holds. So there is no per-dimension extra to keep in sync at
  // this boundary any more, and the guarantee is stronger than the union ever was: a field
  // cannot be dropped in transcription if it is never transcribed. What must be pinned now is
  // that PROPERTY — that no finding-shaped item reappears here — because re-adding one would
  // silently re-open both the #47 field-dropping class and the withheld-`agent` constraint
  // (#25 requirement 1) in a single edit.
  //
  // Only the EXECUTOR dispatch's schema is under test here, so the mock is deliberately
  // minimal: the slice-input writer echoes its paths so the write-proof gate passes, and the
  // executor result is left unusable. A `null` result is untrusted, so the slice takes its
  // one deterministic retry (VERIFY_ATTEMPTS_PER_SLICE=2) before verifyStage degrades to
  // UNVERIFIED, which is fine — the schema was already handed to both dispatches by then.
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
  // A degraded slice dispatches its single deterministic retry (VERIFY_ATTEMPTS_PER_SLICE=2),
  // so a single slice whose executor result is untrusted on attempt 1 produces 2 dispatches.
  assert.equal(execSchemas.length, 2, 'a degraded slice dispatches its one deterministic retry');
  // The retry must carry the SAME schema as attempt 1 — the point of this test is that every
  // executor dispatch gets the same delta-only schema, not just the first one.
  assert.deepEqual(execSchemas[1], execSchemas[0], 'retry dispatch schema matches attempt 1');
  const schema = execSchemas[0];

  // No finding-shaped array anywhere in the echo — not under the old names, and not under
  // any new one: the only array the result declares is `deltas`, and its items are the six
  // scalar keys verify_findings.py emits. A per-dimension extra or a canonical finding field
  // appearing here would mean findings crossed the boundary again.
  const result = schema.properties.result.properties;
  assert.deepEqual(Object.keys(result), ['deltas'], 'the verify result declares deltas and nothing else');
  const deltaProps = result.deltas.items.properties;
  assert.deepEqual(
    Object.keys(deltaProps).sort(),
    ['confidence', 'elimination_reason', 'id', 'origin', 'severity', 'verified'],
    'the delta item declares exactly the keys verify_findings.py _DELTA_FIELDS emits (plus id/verified)',
  );

  const findingOnly = new Set(Object.keys(FINDING_PROP_TYPES));
  for (const d of DIMENSIONS) for (const k of Object.keys(d.schemaExtra || {})) findingOnly.add(k);
  // The four keys a delta legitimately shares with a finding are its whole point — they are
  // what the script re-decides. Everything else the registry declares must be absent.
  for (const k of ['id', 'origin', 'severity', 'confidence']) findingOnly.delete(k);
  for (const k of Object.keys(deltaProps)) {
    assert.ok(!findingOnly.has(k), `${k} is a finding field and must not ride the delta echo`);
  }
  // `agent` was never declarable here and must stay that way — but the constraint is now
  // enforced structurally by joinVerifyDeltas stripping it, pinned in stages_verify_delta.test.js.
  assert.equal(deltaProps.agent, undefined, 'the delta must not carry agent identity');
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

  // None of the five may enter the FLAT FINDING_REQUIRED list — criticality and
  // failure_scenario ARE dispatch-required (issue #66), but through test_coverage's
  // requiredExtra, never by touching this canonical, per-dimension-free base list.
  for (const field of ['suggestion', 'claude_md_rule', 'criticality', 'failure_scenario', 'spec_text']) {
    assert.ok(!FINDING_REQUIRED.includes(field), `${field} must stay out of FINDING_REQUIRED — per-dimension promotion goes through requiredExtra, not here`);
  }

  // Issue #63: suggested_fix_code is now a canonical field, instructed by all 7 discovery
  // contracts and declared in FINDING_PROP_TYPES — post_review.py gates it behind a
  // deterministic apply-check before ever rendering it as a committable fence, but the
  // dispatch schema declares it unconditionally like suggestion/claude_md_rule above.
  for (const spec of agentSpecs()) {
    assert.equal(props(spec.agentType).suggested_fix_code.type, 'string', `${spec.agentType}: suggested_fix_code`);
  }
  assert.ok(!FINDING_REQUIRED.includes('suggested_fix_code'),
    'suggested_fix_code must stay out of FINDING_REQUIRED — it is optional, OMIT-not-null');
});
