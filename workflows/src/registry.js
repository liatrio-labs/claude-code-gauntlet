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

// `schemaExtra` declares the per-dimension finding fields BEYOND the canonical schema —
// the extras each agent's .md output contract actually emits (findingItemSchema in stages.js
// unions them onto that agent's discovery item schema, and the verify echo item schema unions
// them ALL). A value is EITHER a type-name shorthand string ('string'/'number', expanded to
// { type: <name> }) OR a full JSON-Schema fragment used verbatim, which is how array-valued
// extras (cross_file_impact's affected_consumers) are declared. Each row MUST match its
// contract (agents/<agent>.md output line) or the executor drops the field when transcribing
// findings "verbatim via the schema": bug -> hidden_errors, security -> attack_vector,
// cross_file_impact -> affected_consumers (ARRAY), type_design -> invalid_state_example,
// simplification -> behavior_preserved. The pre-reconciliation declarations (type_design
// encapsulation/invariants/enforcement/usefulness; simplification before/after) named fields
// no agent ever emitted top-level and no code consumes — pure schema noise now removed.
export const DIMENSIONS = [
  { dimension: 'bug', agentType: 'code-gauntlet:bug-detector', conditionalFlag: null, schemaExtra: { hidden_errors: 'string' }, modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'security', agentType: 'code-gauntlet:security-reviewer', conditionalFlag: null, schemaExtra: { attack_vector: 'string' }, modelOverride: 'opus', promptExtra: SECURITY_SWEEP_PROMPT_EXTRA },
  { dimension: 'cross_file_impact', agentType: 'code-gauntlet:cross-file-impact', conditionalFlag: DEEP,
    schemaExtra: { affected_consumers: { type: 'array', items: { type: 'string' } } }, modelOverride: null, promptExtra: null },
  { dimension: 'test_coverage', agentType: 'code-gauntlet:test-analyzer', conditionalFlag: DEEP, schemaExtra: {}, modelOverride: null, promptExtra: null },
  { dimension: 'convention', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP, schemaExtra: {}, modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'intent', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP, schemaExtra: {}, modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'comment_accuracy', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: DEEP, schemaExtra: {}, modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'type_design', agentType: 'code-gauntlet:type-design-analyzer', conditionalFlag: DEEP,
    schemaExtra: { invalid_state_example: 'string' }, modelOverride: null, promptExtra: null },
  { dimension: 'simplification', agentType: 'code-gauntlet:code-simplifier', conditionalFlag: DEEP,
    schemaExtra: { behavior_preserved: 'string' }, modelOverride: null, promptExtra: null },
];

export const AGENTS = [...new Set(DIMENSIONS.map((d) => d.agentType))];

// Deviations only. Frontmatter is the baseline model; this encodes S5 overrides.
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
    return { model: toModelId(opts.subagentModelEnv), note: 'CLAUDE_CODE_SUBAGENT_MODEL override — model policy bypassed' };
  }
  const dim = DIMENSIONS.find((d) => d.agentType === agentType);
  // Single benchmarked policy: discovery on sonnet with security-reviewer's opus
  // override, stage agents per STAGE_DEFAULTS. Alternate model modes (fable) are
  // roadmap work (issue #17 V3.2) and land behind their own paired measurement.
  const model = toModelId(dim?.modelOverride || STAGE_DEFAULTS[agentType.split(':').pop()] || 'sonnet');
  return { model, note: '' };
}
