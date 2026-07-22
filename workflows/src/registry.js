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

export const DIMENSIONS = [
  { dimension: 'bug', agentType: 'code-gauntlet:bug-detector', conditionalFlag: null, schemaExtra: {}, modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'security', agentType: 'code-gauntlet:security-reviewer', conditionalFlag: null, schemaExtra: {}, modelOverride: 'opus', promptExtra: SECURITY_SWEEP_PROMPT_EXTRA },
  { dimension: 'cross_file_impact', agentType: 'code-gauntlet:cross-file-impact', conditionalFlag: null, schemaExtra: {}, modelOverride: null, promptExtra: null },
  { dimension: 'test_coverage', agentType: 'code-gauntlet:test-analyzer', conditionalFlag: null, schemaExtra: {}, modelOverride: null, promptExtra: null },
  { dimension: 'convention', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: null, schemaExtra: {}, modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'intent', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: null, schemaExtra: {}, modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'comment_accuracy', agentType: 'code-gauntlet:conventions-and-intent', conditionalFlag: null, schemaExtra: {}, modelOverride: null, promptExtra: TYPO_NAMING_SWEEP_PROMPT_EXTRA },
  { dimension: 'type_design', agentType: 'code-gauntlet:type-design-analyzer', conditionalFlag: null,
    schemaExtra: { encapsulation: 'number', invariants: 'number', enforcement: 'number', usefulness: 'number' }, modelOverride: null, promptExtra: null },
  { dimension: 'simplification', agentType: 'code-gauntlet:code-simplifier', conditionalFlag: null,
    schemaExtra: { before: 'string', after: 'string' }, modelOverride: null, promptExtra: null },
];

export const AGENTS = [...new Set(DIMENSIONS.map((d) => d.agentType))];

// Deviations only. Frontmatter is the baseline model; this encodes S5 overrides.
// Keys are matched against `agentType.split(':').pop()`, so they must be the FULL
// suffix — 'report-writer'/'artifact-writer', not 'report' — or the tunable never binds.
const STAGE_DEFAULTS = {
  validator: 'sonnet', challenger: 'sonnet', executor: 'sonnet',
  'report-writer': 'sonnet', 'artifact-writer': 'sonnet',
};

export function resolvePolicy(agentType, opts = {}) {
  if (opts.subagentModelEnv) { // sourced from args.policy.subagentModel by the pipeline dispatch sites (see args.js)
    return { model: opts.subagentModelEnv, note: 'CLAUDE_CODE_SUBAGENT_MODEL override — model policy bypassed' };
  }
  const dim = DIMENSIONS.find((d) => d.agentType === agentType);
  let model = dim?.modelOverride || STAGE_DEFAULTS[agentType.split(':').pop()] || 'sonnet';
  let note = '';
  if (opts.frontier && (agentType.endsWith('challenger'))) { // frontier stage set: challenger (dormant; research-pending others)
    model = opts.frontierModelId || model; // full model-id string only (Fable alias Phase-0 test 8 deferred)
    note = 'frontier upgrade';
  }
  return { model, note };
}
