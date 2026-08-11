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
  cross_file_refs: { type: 'array', items: { type: 'string' } },
};

// The subset findingItemSchema marks `required`. Deliberately NOT extended by this fix:
// `required` is ONE flat list shared by every agent's dispatch schema (all nine dimensions —
// a multi-dimension agent's findings arrive mixed in one dispatch), so a field that is
// required for one dimension — claude_md_rule for convention, spec_text for intent,
// criticality and failure_scenario for test_coverage — cannot be marked required here
// without making it required for all nine. Those stay contract-required/schema-optional, enforced by the agent
// .md prose, the same bucket hidden_errors and invalid_state_example already sit in. Adding a
// per-dimension `requiredExtra` mechanism is tracked separately; do not fake it by appending
// a single-dimension field to this array.
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
// Extras are OPTIONAL by construction (never in FINDING_REQUIRED) and NOT nullable: the
// platform schema contract pins `type` to a single string (no union types), so a
// not-applicable extra must be OMITTED, never emitted as null — the agent contracts say
// "OMIT this field", and a null here is the same retry-storm class as string confidence.
// A multi-dimension agent (conventions-and-intent) dispatches ONCE with the UNION of its
// rows' extras (agentSpecs in stages.js), so scoping spec_text to the `intent` row still
// makes it declarable on that agent's convention and comment_accuracy findings — the
// per-dimension scoping is documentation of ownership, not an emission restriction.
export const DIMENSIONS = [
  { dimension: 'bug', agentType: 'code-gauntlet:bug-detector', conditionalFlag: null, schemaExtra: { hidden_errors: 'string' }, modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'security', agentType: 'code-gauntlet:security-reviewer', conditionalFlag: null, schemaExtra: { attack_vector: 'string' }, modelOverride: 'opus', promptExtra: SECURITY_SWEEP_PROMPT_EXTRA },
  { dimension: 'cross_file_impact', agentType: 'code-gauntlet:cross-file-impact', conditionalFlag: DEEP,
    schemaExtra: { affected_consumers: { type: 'array', items: { type: 'string' } } }, modelOverride: null, promptExtra: null },
  { dimension: 'test_coverage', agentType: 'code-gauntlet:test-analyzer', conditionalFlag: DEEP,
    // criticality is a 1-10 IMPACT scale (agents/test-analyzer.md); bound it in the schema
    // fragment so StructuredOutput rejects 0/-5/999 the same way items is required on arrays.
    // confidence stays unbound here and is clamped later — validators adjust it at runtime.
    schemaExtra: { criticality: { type: 'number', minimum: 1, maximum: 10 }, failure_scenario: 'string' },
    modelOverride: null, promptExtra: null },
  { dimension: 'convention', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP, schemaExtra: {}, modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'intent', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP,
    schemaExtra: { spec_text: 'string' }, modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'comment_accuracy', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP, schemaExtra: {}, modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'type_design', agentType: 'code-gauntlet:type-design-analyzer', conditionalFlag: DEEP,
    schemaExtra: { invalid_state_example: 'string' }, modelOverride: null, promptExtra: null },
  { dimension: 'simplification', agentType: 'code-gauntlet:code-simplifier', conditionalFlag: DEEP,
    schemaExtra: { behavior_preserved: 'string' }, modelOverride: null, promptExtra: null },
];

export const AGENTS = [...new Set(DIMENSIONS.map((d) => d.agentType))];

// The stage agents' models, restating each one's `model:` frontmatter explicitly so a
// dispatch pins a full model ID instead of inheriting the session variant (see MODEL_IDS
// below). No entry currently deviates from its frontmatter, and all five match
// resolvePolicy's own 'sonnet' fallback — this is the one place to change when one should.
// Keys are matched against `agentType.split(':').pop()`, so they must be the FULL
// suffix — 'report-writer'/'artifact-writer', not 'report' — or the tunable never binds.
const STAGE_DEFAULTS = {
  validator: 'sonnet', challenger: 'sonnet', executor: 'sonnet',
  'report-writer': 'sonnet', 'artifact-writer': 'sonnet',
};

// Explicit full model IDs. Aliases like 'sonnet' resolve against the SESSION's model
// variant at dispatch time — a child session pinned to 'sonnet[1m]' cascades the [1m]
// variant into every agent whose policy says 'sonnet' (measured: cache reads 15.6M→28.7M,
// zero plain-sonnet rows). Pinning full IDs makes agent pins immune to the orchestrator's
// session model. Model migrations update this one map.
const MODEL_IDS = { sonnet: 'claude-sonnet-5', opus: 'claude-opus-4-8', haiku: 'claude-haiku-4-5-20251001' };
const toModelId = (m) => MODEL_IDS[m] || m;

export function resolvePolicy(agentType, opts = {}) {
  if (opts.subagentModelEnv) { // sourced from args.policy.subagentModel by the pipeline dispatch sites (see args.js)
    // The override maps through the same full-ID pin: a bare alias pins the plain full ID
    // (it can no longer inherit the session variant — intended; see headless-mode.md).
    // Override provenance is carried structurally by the run envelope's
    // resolvedPolicy.subagentModel (null = no override), not restated here.
    return { model: toModelId(opts.subagentModelEnv) };
  }
  const dim = DIMENSIONS.find((d) => d.agentType === agentType);
  // Single benchmarked policy: discovery on sonnet with security-reviewer's opus
  // override, stage agents per STAGE_DEFAULTS. Alternate model modes (fable) are
  // roadmap work (issue #17 V3.2) and land behind their own paired measurement.
  const model = toModelId(dim?.modelOverride || STAGE_DEFAULTS[agentType.split(':').pop()] || 'sonnet');
  return { model };
}
