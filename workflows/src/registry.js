// registry.js — single point of extension. Adding a dimension = one entry here + one agent .md.
//
// `promptExtra` is an optional per-agent prompt-extension string appended verbatim to that
// agent's discoverPrompt (see stages.js). It is scoped by agentType, not by dimension —
// every DIMENSIONS row for a multi-dimension agent (conventions-and-intent) must carry the
// SAME value (agentSpecs() unions them; a mismatch would make the dispatched prompt depend
// on dimension iteration order). Hill-climb iter 5 uses it for two discovery-breadth
// sweeps grounded in the subset diagnosis (~21 never-discovered goldens): a security sweep
// on security-reviewer, and a typo/naming sweep on bug-detector and conventions-and-intent.
const SECURITY_SWEEP_PROMPT_EXTRA = 'Additionally sweep explicitly for: SSRF and unvalidated-URL fetches (user-influenced URLs reaching http/request/fetch clients without allowlist validation); frame and embedding policy gaps (missing X-Frame-Options or frame-ancestors, clickjacking exposure); postMessage handlers that do not validate event.origin or check it with weak substring matching; and string-matching bypass patterns where a security decision uses containment checks (indexOf/includes/startsWith/contains) on a host, origin, path, or scheme instead of exact parsing — these are bypassable (e.g. a host "evil.com/trusted.com" still contains "trusted.com").';
const TYPO_NAMING_SWEEP_PROMPT_EXTRA = 'Additionally run an explicit typo and naming sweep: identifier misspellings; typos in user-facing strings, messages, and log output; case-sensitivity mistakes in string comparisons (comparing mixed-case values without normalizing case); and copy-paste plural/singular or off-by-one naming mismatches (a field, key, or variable named for one thing but holding another).';

// `conditionalFlag` is the SCOPE-GATING key for a dimension (consumed by agentActive in
// stages.js). Semantics are OPT-OUT, never opt-in, so the default full-scope run is
// unchanged when the caller stamps no flags:
//   - null   => UNGATEABLE. The dimension is always on and cannot be disabled by any
//               agentFlags entry. The two CORE dimensions (bug, security) carry null so a
//               light-scope run always still includes them ("bugs+security only").
//   - 'name' => gated on agentFlags['name']. agentActive treats a MISSING key or any value
//               other than the literal `false` as ON — so absent/empty agentFlags leaves
//               every gated dimension enabled (byte-identical to the pre-flag behavior).
//               A light-scope run disables the dimension by stamping agentFlags['name'] = false.
// The seven extended dimensions share the single 'deep' flag: light scope stamps
// { deep: false } to drop them, full scope stamps {} (or omits the key) to keep them.
// Finer scopes later = introduce additional flag tokens here; no agentActive change needed.
const DEEP = 'deep';

// --- The canonical finding schema ------------------------------------------
//
// FINDING_PROP_TYPES + FINDING_REQUIRED + each DIMENSIONS row's `schemaExtra` are, together,
// the WHOLE declaration of what a finding may carry. They live in this one file on purpose
// (issue #47): the canonical half used to sit in stages.js, the per-dimension half here, and
// `cross_file_refs` was a third hardcoded special case inside findingItemSchema — three places
// to add a finding field, which is how `suggestion`/`claude_md_rule` (instructed by all 7
// discovery contracts) and `spec_text`/`criticality`/`failure_scenario` (one contract each)
// went years declared by no schema and silently dropped at the StructuredOutput boundary.
// Adding a finding field is now ONE entry here plus the owning agent's .md output contract,
// and tests/test_dimensions_registry.py fails the build when those two drift apart.
//
// A value is EITHER a type-name shorthand string ('string'/'number', expanded by
// findingItemSchema to { type: <name> }) OR a full JSON-Schema fragment used verbatim — which
// is how array-valued fields (cross_file_refs, cross_file_impact's affected_consumers) are
// declared, since the platform's schema validator requires `items` on an array.
//
// `origin` is the one canonical field NO agent emits: verify_findings.py stamps it during
// blame classification. Everything else here must appear in every discovery contract's output
// block, and the lockstep test asserts exactly that (declared − instructed == {origin}).
export const FINDING_PROP_TYPES = {
  // confidence is a NUMBER end-to-end: agents emit a numeric 0-100 score per their .md
  // contracts, so declaring it `number` here makes StructuredOutput return the number at
  // EVERY by-value boundary (discovery included) — the string form "85" the schema used to
  // declare simply never exists, so the filter's consensus `+` boost can never
  // string-concatenate ("85"+10 -> "8510"). pinNumericFields stays as defense-in-depth for
  // legacy/checkpoint-resume findings that predate this pin.
  id: 'string', file: 'string', line_start: 'number', line_end: 'number',
  title: 'string', description: 'string', severity: 'string', confidence: 'number',
  dimension: 'string', origin: 'string', evidence: 'string',
  // Instructed by all 7 discovery contracts, declared by none of them until issue #47.
  // `suggestion` is the prose fix advice report-format.md renders and post_review.py posts;
  // `claude_md_rule` is the cited project rule that justifies a finding (REQUIRED non-null
  // for convention findings per agents/conventions-and-intent.md — prompt-enforced, see the
  // FINDING_REQUIRED note below). Both are OPTIONAL here and NOT nullable: a not-applicable
  // value must be OMITTED, never emitted as null (see the nullability note under DIMENSIONS).
  suggestion: 'string', claude_md_rule: 'string',
  // Instructed by all 7 discovery contracts (issue #63). OPTIONAL and NOT nullable, same
  // OMIT-not-null discipline as suggestion/claude_md_rule above: a not-applicable value is
  // omitted, never null. Declaring it here is only half the story — delivery
  // (scripts/post_review.py) runs a deterministic apply-check before ever rendering it as a
  // committable ```suggestion fence, and downgrades to the prose `suggestion` on any failure
  // (non-string, stale/no-op, wrong range, wrong anchor, oversized, ...). A finding surviving
  // to delivery with this field set is not a guarantee the fence ships. The pipeline also
  // strips the field itself (stripReportExcludedFields in renderReport.js), so delivery
  // is the only surface it is ever rendered on. The read-only
  // report-side apply-check (scripts/report_patches.py) renders the KEPT patches into a
  // sibling artifact instead — see report-format.md.
  suggested_fix_code: 'string',
  cross_file_refs: { type: 'array', items: { type: 'string' } },
};

// FINDING_REQUIRED stays the flat canonical list shared by every dispatch schema — it names
// only fields every dimension emits, so it can never itself carry a single-dimension name.
// Per-dimension requirements live one level down, on each DIMENSIONS row's `requiredExtra`
// (issue #66): agentSpecs() intersects across a multi-dimension agent's rows, so a field
// required for only ONE dimension of a shared dispatch (conventions-and-intent mixes
// convention/intent/comment_accuracy in one schema) can never be enforced there — union
// semantics would force fabrication on the sibling dimensions. A TOP-LEVEL
// `oneOf`/`allOf`/`anyOf` in input_schema is rejected outright (API 400, measured
// 2026-08-18); `if`/`then` nested inside `items` was accepted AND enforced by the retry loop
// in that same measurement, but only on the first-party API — unmeasured on
// Bedrock/Vertex/Foundry, so requiredExtra sticks to the provider-portable flat `required`
// list only. So the promotion rule for requiredExtra is narrow: only a field whose owning
// contract emits it UNCONDITIONALLY (no "OMIT this field" branch) may be listed there; a
// genuinely conditional field (hidden_errors, invalid_state_example) stays contract-enforced
// everywhere, the same retry-storm-avoidance class the OMIT-not-null rule already governs.
// claude_md_rule and spec_text are a THIRD case — conditional across dimensions but
// unconditional WITHIN their own dimension — and go through `requiredWhenDimension` below,
// which does spend the measured nested if/then construct, gated behind
// conditionalSchemaActive so it never reaches an unmeasured provider.
export const FINDING_REQUIRED = ['id', 'file', 'line_start', 'title', 'description', 'severity', 'confidence', 'dimension'];

// `schemaExtra` declares the per-dimension finding fields BEYOND the canonical schema above —
// the extras each agent's .md output contract actually emits (findingItemSchema in stages.js
// unions them onto that agent's discovery item schema — the only boundary that carries finding
// items by value; the verify executor echoes per-id deltas, never findings). Each row MUST
// match its contract (agents/<agent>.md output block): the item schema is CLOSED
// (`additionalProperties: false`), so an extra a contract instructs but no row declares is
// rejected at dispatch and costs a schema retry: bug -> hidden_errors,
// security -> attack_vector, cross_file_impact -> affected_consumers (ARRAY), intent ->
// spec_text, test_coverage -> criticality (NUMBER, a 1-10 impact scale distinct from
// confidence's 0-100 certainty) + failure_scenario, type_design -> invalid_state_example,
// simplification -> behavior_preserved. The pre-reconciliation declarations (type_design
// encapsulation/invariants/enforcement/usefulness; simplification before/after) named fields
// no agent ever emitted top-level and no code consumes — pure schema noise now removed.
// Extras are optional by default — a row promotes one to dispatch-required only through
// `requiredExtra` below, never by touching FINDING_REQUIRED — and NOT nullable: the
// platform schema contract pins `type` to a single string (no union types), so a
// not-applicable extra must be OMITTED, never emitted as null — the agent contracts say
// "OMIT this field", and a null here is the same retry-storm class as string confidence.
// A multi-dimension agent (conventions-and-intent) dispatches ONCE with the UNION of its
// rows' extras (agentSpecs in stages.js), so scoping spec_text to the `intent` row still
// makes it declarable on that agent's convention and comment_accuracy findings — the
// per-dimension scoping is documentation of ownership, not an emission restriction.
//
// `requiredExtra` (issue #66) names the subset of a row's OWN `schemaExtra` — never a
// canonical FINDING_PROP_TYPES field — that dimension's owning contract emits UNCONDITIONALLY —
// no OMIT branch — so the schema can safely require them without ever rejecting a compliant
// finding. Narrowed to schemaExtra deliberately: a canonical field, by definition, is one every
// dimension emits unconditionally already, so its unconditional-ness belongs in
// FINDING_REQUIRED directly, not re-declared piecemeal on a row-by-row requiredExtra — a
// canonical promotion routed through here would be documented nowhere the Canonical fields
// table's Required-column lockstep test looks. Populated by a full audit of
// the seven contracts: security -> attack_vector, cross_file_impact -> affected_consumers,
// test_coverage -> criticality + failure_scenario, simplification -> behavior_preserved. Every
// other row is `[]`: bug's hidden_errors, conventions-and-intent's claude_md_rule/spec_text,
// and type_design's invalid_state_example all carry an explicit OMIT branch in their contract
// (conventions-and-intent additionally can't promote a single-dimension field at all — see the
// agentSpecs comment in stages.js), so they stay contract-enforced. findingItemSchema
// (stages.js) appends a spec's effective requiredExtra onto FINDING_REQUIRED per dispatch.
// `requiredWhenDimension` (issue #218) names fields the owning contract emits
// UNCONDITIONALLY within THIS row's dimension while every sibling dimension on the same
// agentType is told to OMIT them — the exact shape `requiredExtra` cannot hold (its
// sibling-parity guard demands every row of an agentType agree, and its intersection would
// either force every sibling to fabricate the field or silently drop it). Each entry is
// EITHER a key of this row's OWN schemaExtra (spec_text on the intent row) OR a canonical
// FINDING_PROP_TYPES field that is not in FINDING_REQUIRED (claude_md_rule on the convention
// row) — never a canonical field already unconditional everywhere, which belongs in
// FINDING_REQUIRED directly. `[]` on every other row.
//
// Unlike requiredExtra this cannot become a flat `required` entry on the dispatch schema: a
// TOP-LEVEL oneOf/allOf/anyOf in input_schema is rejected outright (API 400, measured
// 2026-08-18), but the same if/then nested inside `items` was accepted AND enforced on that
// same measurement — first-party only, unmeasured on Bedrock/Vertex/Foundry. So
// findingItemSchema (stages.js) emits the nested allOf/if/then construct ONLY when
// conditionalSchemaActive(policy) is true (first-party-direct, no gateway) — third-party
// providers and gateway sessions keep today's flat schema, contract prose is the floor
// everywhere. See agentSpecs' conditionalRequired derivation in stages.js.
export const DIMENSIONS = [
  { dimension: 'bug', agentType: 'code-gauntlet:bug-detector', conditionalFlag: null, schemaExtra: { hidden_errors: 'string' }, requiredExtra: [], requiredWhenDimension: [], modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'security', agentType: 'code-gauntlet:security-reviewer', conditionalFlag: null, schemaExtra: { attack_vector: 'string' }, requiredExtra: ['attack_vector'], requiredWhenDimension: [], modelOverride: 'opus', promptExtra: SECURITY_SWEEP_PROMPT_EXTRA },
  { dimension: 'cross_file_impact', agentType: 'code-gauntlet:cross-file-impact', conditionalFlag: DEEP,
    schemaExtra: { affected_consumers: { type: 'array', items: { type: 'string' } } }, requiredExtra: ['affected_consumers'], requiredWhenDimension: [], modelOverride: null, promptExtra: null },
  { dimension: 'test_coverage', agentType: 'code-gauntlet:test-analyzer', conditionalFlag: DEEP,
    // criticality is a 1-10 IMPACT scale (agents/test-analyzer.md); bound it in the schema
    // fragment so StructuredOutput rejects 0/-5/999 the same way items is required on arrays.
    // confidence stays unbound here and is clamped later — validators adjust it at runtime.
    schemaExtra: { criticality: { type: 'number', minimum: 1, maximum: 10 }, failure_scenario: 'string' },
    requiredExtra: ['criticality', 'failure_scenario'], requiredWhenDimension: [], modelOverride: null, promptExtra: null },
  { dimension: 'convention', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP, schemaExtra: {}, requiredExtra: [], requiredWhenDimension: ['claude_md_rule'], modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'intent', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP,
    schemaExtra: { spec_text: 'string' }, requiredExtra: [], requiredWhenDimension: ['spec_text'], modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'comment_accuracy', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP, schemaExtra: {}, requiredExtra: [], requiredWhenDimension: [], modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'type_design', agentType: 'code-gauntlet:type-design-analyzer', conditionalFlag: DEEP,
    schemaExtra: { invalid_state_example: 'string' }, requiredExtra: [], requiredWhenDimension: [], modelOverride: null, promptExtra: null },
  { dimension: 'simplification', agentType: 'code-gauntlet:code-simplifier', conditionalFlag: DEEP,
    schemaExtra: { behavior_preserved: 'string' }, requiredExtra: ['behavior_preserved'], requiredWhenDimension: [], modelOverride: null, promptExtra: null },
];

export const AGENTS = [...new Set(DIMENSIONS.map((d) => d.agentType))];

// Per-agent display label for the Review Dimensions Summary table (issue #89):
// dimensionsSummaryTable (renderReport.js) renders ONE row per discovery agent — a
// multi-dimension agent (conventions-and-intent) gets a single label for its whole
// aggregated row, not one per dimension — so this is keyed by agentType directly
// rather than living on DIMENSIONS rows, which would force every one of a
// multi-dimension agent's rows to repeat the identical value (the trap promptExtra's
// comment above already documents for a genuinely per-agent value). This map is the
// single source of truth for the display strings — the renderer appends the table as
// the report's last section. Extending: one
// entry here when AGENTS gains a member — registry.test.js pins the key set to AGENTS.
export const AGENT_LABELS = {
  'code-gauntlet:bug-detector': 'Correctness & Error Handling',
  'code-gauntlet:security-reviewer': 'Security',
  'code-gauntlet:cross-file-impact': 'Cross-file Impact',
  'code-gauntlet:test-analyzer': 'Test Coverage',
  'code-gauntlet:conventions-and-intent': 'Conventions & Intent',
  'code-gauntlet:type-design-analyzer': 'Type Design',
  'code-gauntlet:code-simplifier': 'Code Simplification',
};

// --- Product identity -------------------------------------------------------
// The ONE hand-authored copy of the brand mark, the display name, and the severity
// emoji map. Every other copy is GENERATED from here by
// scripts/generate_contract_requirements.py (--check in CI): the Python mirror in
// scripts/post_review.py and the legends in references/report-format.md and
// references/delivery-guide.md. Do not hand-edit a mirror.
//
// PRODUCT ("code-gauntlet", scripts/review_marker.py:89) is deliberately NOT here and is
// NOT a mirror of BRAND_NAME: that is a machine-parsed wire slug pinned by
// docs/machine-parsed-strings.md; this is presentation. A product rename moves both,
// separately, on purpose.
export const BRAND_MARK = '\u2694\uFE0F';   // CROSSED SWORDS U+2694 + VS16 U+FE0F
export const BRAND_NAME = 'Code Gauntlet';
export const SEVERITY_EMOJI = {
  critical: '\u{1F534}', high: '\u{1F7E0}', medium: '\u{1F7E1}', low: '\u{1F4A1}',
};
// The mark rendered for a severity the schema does not forbid (`severity` is declared
// `string`, not an enum) — a constant, not a repeated literal. Pinned by
// tests/test_post_review.py::test_unknown_severity_falls_back_to_bulb.
export const SEVERITY_EMOJI_FALLBACK = SEVERITY_EMOJI.low;

// The stage agents' models, restating each one's `model:` frontmatter explicitly so a
// dispatch pins a full model ID instead of inheriting the session variant (see MODEL_IDS
// below). No entry currently deviates from its frontmatter, and all four match
// resolvePolicy's own 'sonnet' fallback — this is the one place to change when one should.
// Keys are matched against `agentType.split(':').pop()`, so they must be the FULL
// suffix — 'artifact-writer', not 'artifact' — or the tunable never binds.
const STAGE_DEFAULTS = {
  validator: 'sonnet', challenger: 'sonnet', executor: 'sonnet',
  'artifact-writer': 'sonnet',
};

// Explicit full model IDs. Aliases like 'sonnet' resolve against the SESSION's model
// variant at dispatch time — a child session pinned to 'sonnet[1m]' cascades the [1m]
// variant into every agent whose policy says 'sonnet' (measured: cache reads 15.6M→28.7M,
// zero plain-sonnet rows). Pinning full IDs makes agent pins immune to the orchestrator's
// session model. Model migrations update this one map.
//
// FIRST-PARTY ONLY. These are Anthropic API model names; Bedrock / Vertex / Foundry use
// provider-specific deployment IDs and pass any other string through UNCHECKED to the
// provider, where these names 400 as invalid model identifiers (observed live: a Bedrock
// run degraded all discovery dimensions in 2s). On those providers the bare alias is the
// only provider-portable spelling — the harness resolves 'sonnet'/'opus' through the
// deployment mapping (ANTHROPIC_DEFAULT_*_MODEL). The [1m]-cascade the pin exists to stop
// was measured on first-party variants; on a third-party provider alias resolution is the
// correct behavior, not the bug. So: pin full IDs first-party, emit bare aliases elsewhere.
const MODEL_IDS = { sonnet: 'claude-sonnet-5', opus: 'claude-opus-4-8', haiku: 'claude-haiku-4-5-20251001' };
// policy.provider === 'firstParty' (or absent — older waists predate the field) pins;
// ANY other value emits the alias untouched. Unknown values are deliberately not an error
// here: the alias is the one spelling that resolves on every provider, so alias-through is
// the safe arm, and the waist (args.js) enum-rejects a typo'd provider before dispatch.
const pinsModelIds = (provider) => provider === undefined || provider === null || provider === 'firstParty';
const toModelId = (m, provider) => (pinsModelIds(provider) ? (MODEL_IDS[m] || m) : m);

// conditionalSchemaActive(policy) -> whether the nested allOf/if/then conditional-required
// block (findingItemSchema, stages.js) may ride the conventions-and-intent dispatch (issue
// #218). NOT an alias for pinsModelIds: a gateway (policy.gateway, stamped from
// ANTHROPIC_BASE_URL) still gets the first-party model-ID pin — a gateway proxies the
// Anthropic API and expects standard Claude model names — but forwards input_schema verbatim
// to whatever backend it fronts, so the unmeasured-third-party risk the schema gate exists to
// avoid survives a gateway hop even though the model-pin risk does not. So this predicate
// takes its own compound signal (first-party provider AND no gateway) rather than being read
// off the model-pin predicate alone.
export const conditionalSchemaActive = (policy) => {
  const p = policy || {};
  return pinsModelIds(p.provider) && !p.gateway;
};

export function resolvePolicy(agentType, opts = {}) {
  if (opts.subagentModelEnv) { // sourced from args.policy.subagentModel by the pipeline dispatch sites (see args.js)
    // The override maps through the same full-ID pin: a bare alias pins the plain full ID
    // (it can no longer inherit the session variant — intended; see headless-mode.md).
    // On a third-party provider it passes through untouched — an explicit deployment ID
    // (us.anthropic.…) is exactly what the operator escaped to this knob for.
    // Override provenance is carried structurally by the run envelope's
    // resolvedPolicy.subagentModel (null = no override), not restated here.
    return { model: toModelId(opts.subagentModelEnv, opts.provider) };
  }
  const dim = DIMENSIONS.find((d) => d.agentType === agentType);
  // Single benchmarked policy: discovery on sonnet with security-reviewer's opus
  // override, stage agents per STAGE_DEFAULTS. Alternate model modes (fable) are
  // roadmap work (issue #17 V3.2) and land behind their own paired measurement.
  const model = toModelId(dim?.modelOverride || STAGE_DEFAULTS[agentType.split(':').pop()] || 'sonnet', opts.provider);
  return { model };
}
