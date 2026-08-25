// registry.test.js — DIMENSIONS registry + resolvePolicy (S5) unit tests.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DIMENSIONS, AGENTS, AGENT_LABELS, FINDING_PROP_TYPES, FINDING_REQUIRED, resolvePolicy, conditionalSchemaActive } from '../src/registry.js';
import { intersectRequiredExtra, agentSpecs } from '../src/stages.js';

test('7 unique discovery agents', () => { assert.equal(AGENTS.length, 7); });
test('conventions-and-intent covers 3 dimensions', () => {
  const dims = DIMENSIONS.filter((d) => d.agentType === 'code-gauntlet:conventions-and-intent').map((d) => d.dimension);
  assert.deepEqual(dims.sort(), ['comment_accuracy', 'convention', 'intent']);
});
test('security-reviewer default is opus (S5 deviation)', () => {
  assert.equal(resolvePolicy('code-gauntlet:security-reviewer', {}).model, 'claude-opus-4-8');
});
test('discovery default is sonnet', () => {
  assert.equal(resolvePolicy('code-gauntlet:bug-detector', {}).model, 'claude-sonnet-5');
});
test('challenger resolves sonnet — the single benchmarked policy; no mode flag changes it (fable mode is roadmap #17 V3.2)', () => {
  assert.equal(resolvePolicy('code-gauntlet:challenger', {}).model, 'claude-sonnet-5');
  // A stray legacy frontier flag in opts must be ignored, not resurrect an upgrade path.
  assert.equal(resolvePolicy('code-gauntlet:challenger', { frontier: true, frontierModelId: 'claude-fable-5' }).model, 'claude-sonnet-5');
});
test('CLAUDE_CODE_SUBAGENT_MODEL overrides everything', () => {
  const r = resolvePolicy('code-gauntlet:bug-detector', { subagentModelEnv: 'claude-haiku-4-5' });
  assert.equal(r.model, 'claude-haiku-4-5');
});
test('resolvePolicy returns only { model } — provenance lives in resolvedPolicy.subagentModel (#114)', () => {
  // Both branches: the env-override return and the policy-table return.
  assert.deepEqual(Object.keys(resolvePolicy('code-gauntlet:bug-detector', { subagentModelEnv: 'claude-haiku-4-5' })), ['model']);
  assert.deepEqual(Object.keys(resolvePolicy('code-gauntlet:bug-detector', {})), ['model']);
});
test('report-writer / artifact-writer suffixes bind to STAGE_DEFAULTS (not the bare "report" key)', () => {
  // The split(':').pop() suffix is the full 'report-writer'/'artifact-writer', so the
  // tunable must be keyed by that or it silently never binds. Both resolve to sonnet.
  assert.equal(resolvePolicy('code-gauntlet:report-writer', {}).model, 'claude-sonnet-5');
  assert.equal(resolvePolicy('code-gauntlet:artifact-writer', {}).model, 'claude-sonnet-5');
});

// V3.1 orchestrator-model waist: resolvePolicy pins explicit FULL model IDs so no agent
// pin can cascade the orchestrator session's model variant (measured on bench: a child
// session pinned to 'sonnet[1m]' cascaded the [1m] variant into every agent whose policy
// said the bare alias 'sonnet' — zero plain-sonnet rows in the per-model usage table).
test('resolvePolicy pins full model IDs — no bare aliases can cascade a session variant', () => {
  assert.equal(resolvePolicy('code-gauntlet:bug-detector').model, 'claude-sonnet-5');
  assert.equal(resolvePolicy('code-gauntlet:security-reviewer').model, 'claude-opus-4-8');
  assert.equal(resolvePolicy('code-gauntlet:executor').model, 'claude-sonnet-5');
});
test('subagentModelEnv override maps through the same full-ID pin', () => {
  // A bare alias in CLAUDE_CODE_SUBAGENT_MODEL now pins the plain full ID instead of
  // inheriting the session variant (intended behavior change, documented in headless-mode);
  // an explicit full/dated ID passes through untouched.
  assert.equal(resolvePolicy('code-gauntlet:bug-detector', { subagentModelEnv: 'sonnet' }).model, 'claude-sonnet-5');
  assert.equal(resolvePolicy('code-gauntlet:bug-detector', { subagentModelEnv: 'claude-haiku-4-5-20251001' }).model, 'claude-haiku-4-5-20251001');
});

// Bedrock live failure (2026-08-11 transcript): third-party providers use deployment-
// specific model IDs and pass first-party names (claude-sonnet-5, claude-opus-4-8) through
// UNCHECKED to the provider, which 400s them — every discovery agent degraded in one 2s
// run. On any provider other than firstParty the bare alias is the only spelling the
// harness's deployment mapping resolves, so resolvePolicy must emit it untouched.
test('non-firstParty provider dispatches bare aliases, never first-party full IDs', () => {
  assert.equal(resolvePolicy('code-gauntlet:bug-detector', { provider: 'bedrock' }).model, 'sonnet');
  assert.equal(resolvePolicy('code-gauntlet:security-reviewer', { provider: 'bedrock' }).model, 'opus');
  assert.equal(resolvePolicy('code-gauntlet:executor', { provider: 'vertex' }).model, 'sonnet');
  // Unknown provider strings are waist-rejected upstream; the registry's safe arm for any
  // non-firstParty value is still the alias (it resolves on every provider).
  assert.equal(resolvePolicy('code-gauntlet:bug-detector', { provider: 'someday-provider' }).model, 'sonnet');
});
test('firstParty / absent / null provider keeps the full-ID pin (the [1m]-cascade guard)', () => {
  assert.equal(resolvePolicy('code-gauntlet:bug-detector', { provider: 'firstParty' }).model, 'claude-sonnet-5');
  assert.equal(resolvePolicy('code-gauntlet:bug-detector', { provider: null }).model, 'claude-sonnet-5');
  assert.equal(resolvePolicy('code-gauntlet:security-reviewer', {}).model, 'claude-opus-4-8');
});
test('subagentModelEnv passes through unmapped on a third-party provider', () => {
  // An operator's explicit deployment ID (the Bedrock escape hatch) must survive verbatim,
  // and a bare alias must NOT be rewritten to a first-party full ID there.
  assert.equal(resolvePolicy('code-gauntlet:bug-detector', { subagentModelEnv: 'us.anthropic.claude-sonnet-4-6', provider: 'bedrock' }).model, 'us.anthropic.claude-sonnet-4-6');
  assert.equal(resolvePolicy('code-gauntlet:bug-detector', { subagentModelEnv: 'sonnet', provider: 'bedrock' }).model, 'sonnet');
});

// S7 model bump: security-reviewer's agent frontmatter says opus; assert the registry's
// modelOverride actually binds through resolvePolicy AND that security-reviewer is the ONLY
// discovery agent bumped off the sonnet default (the deviation Task 8 review confirmed).
test('S7: resolvePolicy routes security-reviewer to opus, the sole opus discovery agent', () => {
  assert.equal(DIMENSIONS.find((d) => d.dimension === 'security').modelOverride, 'opus');
  assert.equal(resolvePolicy('code-gauntlet:security-reviewer', {}).model, 'claude-opus-4-8');
  const opusAgents = AGENTS.filter((a) => resolvePolicy(a, {}).model === 'claude-opus-4-8');
  assert.deepEqual(opusAgents, ['code-gauntlet:security-reviewer']);
});

// Hill-climb iter 5 (discovery breadth): per-agent promptExtra sweeps live in the registry.
test('promptExtra: security sweep on security-reviewer, typo/naming on bug + conventions, none elsewhere', () => {
  const byDim = (dim) => DIMENSIONS.find((d) => d.dimension === dim);
  assert.match(byDim('security').promptExtra, /SSRF/);
  assert.match(byDim('bug').promptExtra, /typo and naming sweep/);
  // conventions-and-intent is multi-dimension; every one of its rows must carry the SAME
  // value (agentSpecs unions them, so a mismatch would be iteration-order-dependent).
  const convRows = DIMENSIONS.filter((d) => d.agentType === 'code-gauntlet:conventions-and-intent');
  assert.ok(convRows.every((d) => d.promptExtra === byDim('convention').promptExtra));
  assert.match(byDim('convention').promptExtra, /typo and naming sweep/);
  // bug-detector and conventions share the one typo/naming sweep string.
  assert.equal(byDim('bug').promptExtra, byDim('intent').promptExtra);
  // Agents without a sweep carry null.
  for (const dim of ['cross_file_impact', 'test_coverage', 'type_design', 'simplification']) {
    assert.equal(byDim(dim).promptExtra, null);
  }
});

// Issue #89 sync guard: AGENT_LABELS is keyed by agentType, not by DIMENSIONS row, so
// nothing else fails the build the day AGENTS gains an 8th member without a matching
// label — this is the guard that would.
test('AGENT_LABELS key set is exactly AGENTS — a new agent needs a label too', () => {
  assert.deepEqual(Object.keys(AGENT_LABELS).sort(), [...AGENTS].sort());
});

// --- requiredExtra (issue #66) ----------------------------------------------------

test('every DIMENSIONS row declares requiredExtra as an array of field-name strings', () => {
  for (const d of DIMENSIONS) {
    assert.ok(Array.isArray(d.requiredExtra), `${d.dimension}: requiredExtra must be an array`);
    for (const field of d.requiredExtra) {
      assert.equal(typeof field, 'string', `${d.dimension}: requiredExtra entries must be strings`);
    }
  }
});

test('every requiredExtra entry is a key of its own row\'s schemaExtra — never a canonical FINDING_PROP_TYPES field', () => {
  // F9: narrowed deliberately. A canonical field is, by definition, already emitted
  // unconditionally by every dimension — its promotion belongs in FINDING_REQUIRED directly,
  // not smuggled in piecemeal through one row's requiredExtra, where the Canonical fields
  // table's Required-column lockstep test would never see it.
  for (const d of DIMENSIONS) {
    const declared = new Set(Object.keys(d.schemaExtra || {}));
    for (const field of d.requiredExtra) {
      assert.ok(declared.has(field), `${d.dimension}: requiredExtra names "${field}", which is not a key of this row's own schemaExtra`);
      assert.ok(!(field in FINDING_PROP_TYPES), `${d.dimension}: requiredExtra names "${field}", a canonical field — promote it via FINDING_REQUIRED, not requiredExtra`);
    }
  }
});

test('requiredExtra entries are disjoint from FINDING_REQUIRED', () => {
  for (const d of DIMENSIONS) {
    for (const field of d.requiredExtra) {
      assert.ok(!FINDING_REQUIRED.includes(field), `${d.dimension}: "${field}" is already in FINDING_REQUIRED — do not duplicate it in requiredExtra`);
    }
  }
});

test('rows sharing an agentType declare identical requiredExtra sets — a silent per-row drop must fail the build', () => {
  // agentSpecs() intersects, but the registry itself should never author a divergence in the
  // first place: a row whose requiredExtra differs from its siblings would have that field
  // silently dropped by the intersection, declared but unenforced with no signal anywhere.
  const seen = new Map();
  for (const d of DIMENSIONS) {
    const key = [...d.requiredExtra].sort().join(',');
    if (seen.has(d.agentType)) {
      assert.equal(
        key,
        seen.get(d.agentType),
        `${d.agentType}: rows disagree on requiredExtra (${d.dimension} says [${key}]) — the ` +
          'intersection would silently drop the difference instead of failing loud',
      );
    } else {
      seen.set(d.agentType, key);
    }
  }
});

test('intersectRequiredExtra: a field required by only one of an agent\'s rows is not enforced', () => {
  assert.deepEqual(intersectRequiredExtra([{ requiredExtra: ['x'] }, { requiredExtra: [] }]), []);
});

test('intersectRequiredExtra: a field required by every row of an agent stays enforced', () => {
  assert.deepEqual(intersectRequiredExtra([{ requiredExtra: ['x'] }, { requiredExtra: ['x'] }]), ['x']);
});

test('intersectRequiredExtra: empty rows list yields empty result', () => {
  assert.deepEqual(intersectRequiredExtra([]), []);
});

// agentSpecs(dims) end-to-end (F2): the call site that actually threads intersectRequiredExtra
// into a dispatch spec is worth testing directly — a regression that replaces
// `intersectRequiredExtra(spec.rows)` with e.g. first-row-verbatim leaves every OTHER test in
// this suite green today, because every real agentType's rows already agree (the sibling-parity
// test above enforces that on the live registry, making the intersection an identity there).
test('agentSpecs(dims) default call matches AGENTS order exactly', () => {
  assert.deepEqual(agentSpecs().map((s) => s.agentType), AGENTS);
});

test('agentSpecs(dims): a field required by only one of two synthetic rows sharing an agentType is NOT in the spec\'s requiredExtra', () => {
  const dims = [
    { agentType: 'synthetic:agent', dimension: 'dim_a', conditionalFlag: null, schemaExtra: {}, requiredExtra: ['x'], promptExtra: null },
    { agentType: 'synthetic:agent', dimension: 'dim_b', conditionalFlag: null, schemaExtra: {}, requiredExtra: [], promptExtra: null },
  ];
  const specs = agentSpecs(dims);
  assert.equal(specs.length, 1);
  assert.deepEqual(specs[0].requiredExtra, []);
});

test('agentSpecs(dims): a field required by every row sharing an agentType IS in the spec\'s requiredExtra', () => {
  const dims = [
    { agentType: 'synthetic:agent', dimension: 'dim_a', conditionalFlag: null, schemaExtra: {}, requiredExtra: ['x'], promptExtra: null },
    { agentType: 'synthetic:agent', dimension: 'dim_b', conditionalFlag: null, schemaExtra: {}, requiredExtra: ['x'], promptExtra: null },
  ];
  const specs = agentSpecs(dims);
  assert.equal(specs.length, 1);
  assert.deepEqual(specs[0].requiredExtra, ['x']);
});

test('agentSpecs(dims): order is derived from dims itself, not the module-level AGENTS constant', () => {
  // A synthetic agentType AGENTS never heard of must still produce a real spec (not
  // undefined) — the bug F2 guards against: mapping over the module-level AGENTS here would
  // look this agentType up and return undefined.
  const dims = [{ agentType: 'synthetic:only-here', dimension: 'dim_z', conditionalFlag: null, schemaExtra: {}, requiredExtra: [], promptExtra: null }];
  const specs = agentSpecs(dims);
  assert.deepEqual(specs.map((s) => s.agentType), ['synthetic:only-here']);
  assert.ok(specs[0], 'spec must be a real object, not undefined');
});

// --- requiredWhenDimension (issue #218) -------------------------------------------

test('every DIMENSIONS row declares requiredWhenDimension as an array of field-name strings', () => {
  for (const d of DIMENSIONS) {
    assert.ok(Array.isArray(d.requiredWhenDimension), `${d.dimension}: requiredWhenDimension must be an array`);
    for (const field of d.requiredWhenDimension) {
      assert.equal(typeof field, 'string', `${d.dimension}: requiredWhenDimension entries must be strings`);
    }
  }
});

test('every requiredWhenDimension entry is EITHER a key of its own row\'s schemaExtra OR a canonical field not in FINDING_REQUIRED', () => {
  // [v2] red-team finding 1: unlike requiredExtra, a requiredWhenDimension entry may be a
  // canonical FINDING_PROP_TYPES field (claude_md_rule) as long as it is not already
  // unconditional (FINDING_REQUIRED) — that promotion belongs in FINDING_REQUIRED directly.
  for (const d of DIMENSIONS) {
    const declared = new Set(Object.keys(d.schemaExtra || {}));
    for (const field of d.requiredWhenDimension) {
      const ownSchemaExtra = declared.has(field);
      const canonicalNotRequired = field in FINDING_PROP_TYPES && !FINDING_REQUIRED.includes(field);
      assert.ok(
        ownSchemaExtra || canonicalNotRequired,
        `${d.dimension}: requiredWhenDimension names "${field}", which is neither a key of ` +
          'this row\'s own schemaExtra nor a canonical field outside FINDING_REQUIRED',
      );
    }
  }
});

test('requiredWhenDimension is disjoint from the row\'s own requiredExtra', () => {
  for (const d of DIMENSIONS) {
    for (const field of d.requiredWhenDimension) {
      assert.ok(!d.requiredExtra.includes(field), `${d.dimension}: "${field}" is in both requiredExtra and requiredWhenDimension`);
    }
  }
});

test('requiredWhenDimension is non-empty only on rows whose agentType has multiple rows', () => {
  const rowCounts = new Map();
  for (const d of DIMENSIONS) rowCounts.set(d.agentType, (rowCounts.get(d.agentType) || 0) + 1);
  for (const d of DIMENSIONS) {
    if (d.requiredWhenDimension.length) {
      assert.ok(
        rowCounts.get(d.agentType) > 1,
        `${d.dimension}: requiredWhenDimension is non-empty on a single-row agentType — use requiredExtra instead`,
      );
    }
  }
});

test('the live registry: only convention (claude_md_rule) and intent (spec_text) carry a requiredWhenDimension entry', () => {
  const byDim = (dim) => DIMENSIONS.find((d) => d.dimension === dim);
  assert.deepEqual(byDim('convention').requiredWhenDimension, ['claude_md_rule']);
  assert.deepEqual(byDim('intent').requiredWhenDimension, ['spec_text']);
  for (const dim of ['bug', 'security', 'cross_file_impact', 'test_coverage', 'comment_accuracy', 'type_design', 'simplification']) {
    assert.deepEqual(byDim(dim).requiredWhenDimension, [], `${dim} must carry no requiredWhenDimension entry`);
  }
});

test('agentSpecs(): conventions-and-intent carries the sorted conditionalRequired derivation', () => {
  const spec = agentSpecs().find((s) => s.agentType === 'code-gauntlet:conventions-and-intent');
  assert.deepEqual(spec.conditionalRequired, [
    { dimension: 'convention', required: ['claude_md_rule'] },
    { dimension: 'intent', required: ['spec_text'] },
  ]);
});

test('agentSpecs(): every single-dimension spec has an empty conditionalRequired', () => {
  for (const spec of agentSpecs()) {
    if (spec.agentType === 'code-gauntlet:conventions-and-intent') continue;
    assert.deepEqual(spec.conditionalRequired, [], `${spec.agentType}: expected no conditionalRequired entries`);
  }
});

test('agentSpecs(dims): a synthetic multi-dimension agent derives conditionalRequired sorted by dimension', () => {
  const dims = [
    { agentType: 'synthetic:agent', dimension: 'dim_b', conditionalFlag: null, schemaExtra: { y: 'string' }, requiredExtra: [], requiredWhenDimension: ['y'], promptExtra: null },
    { agentType: 'synthetic:agent', dimension: 'dim_a', conditionalFlag: null, schemaExtra: { x: 'string' }, requiredExtra: [], requiredWhenDimension: ['x'], promptExtra: null },
  ];
  const specs = agentSpecs(dims);
  assert.equal(specs.length, 1);
  assert.deepEqual(specs[0].conditionalRequired, [
    { dimension: 'dim_a', required: ['x'] },
    { dimension: 'dim_b', required: ['y'] },
  ]);
});

test('agentSpecs(dims): rows WITHOUT a requiredWhenDimension key do not throw and yield no conditionalRequired', () => {
  // Mirrors the pre-existing synthetic-dims tests above (F2): a row shaped like the ones this
  // file already builds for requiredExtra coverage must not need updating for the new key.
  const dims = [{ agentType: 'synthetic:only-here', dimension: 'dim_z', conditionalFlag: null, schemaExtra: {}, requiredExtra: [], promptExtra: null }];
  assert.doesNotThrow(() => agentSpecs(dims));
  const specs = agentSpecs(dims);
  assert.deepEqual(specs[0].conditionalRequired, []);
});

test('conditionalSchemaActive: firstParty/absent/null provider with no gateway is active', () => {
  assert.equal(conditionalSchemaActive({}), true);
  assert.equal(conditionalSchemaActive({ provider: 'firstParty' }), true);
  assert.equal(conditionalSchemaActive({ provider: null }), true);
  assert.equal(conditionalSchemaActive({ provider: undefined }), true);
});

test('conditionalSchemaActive: any non-firstParty provider is inactive regardless of gateway', () => {
  for (const provider of ['bedrock', 'vertex', 'foundry']) {
    assert.equal(conditionalSchemaActive({ provider }), false);
    assert.equal(conditionalSchemaActive({ provider, gateway: false }), false);
  }
});

test('conditionalSchemaActive: gateway:true on an otherwise first-party policy is inactive', () => {
  assert.equal(conditionalSchemaActive({ gateway: true }), false);
  assert.equal(conditionalSchemaActive({ provider: 'firstParty', gateway: true }), false);
});

test('conditionalSchemaActive: gateway:false or absent on first-party stays active', () => {
  assert.equal(conditionalSchemaActive({ gateway: false }), true);
  assert.equal(conditionalSchemaActive({}), true);
});
