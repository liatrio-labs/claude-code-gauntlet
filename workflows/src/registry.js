// registry.js — single point of extension. Adding a dimension = one entry here + one agent .md.
//
// Hill-climb iter 3: `promptExtra` is an optional per-agent prompt-extension string,
// appended verbatim to that agent's discoverPrompt (see stages.js). It is scoped by
// agentType, not by dimension — every DIMENSIONS row for a multi-dimension agent must
// carry the SAME promptExtra (agentSpecs() unions them; a mismatch would make the
// dispatched prompt depend on dimension iteration order). Iter 2 found that applying a
// candidate-calibration paragraph to all 7 agents caused a recall regression (candidate
// flood displaced goldens in cap-bound delivery); iter 3 scopes it to bug-detector only,
// pending evidence it generalizes.
const CALIBRATION_PROMPT_EXTRA = 'This pipeline deterministically verifies, independently validates, threshold-filters, and blind-challenges every finding downstream — do not pre-filter borderline candidates yourself. Report every finding you judge plausible after investigation: your genuine assessment, with honest confidence values. Downstream stages remove what does not survive scrutiny. total_seen must equal the number of candidates you actually evaluated; the gap between total_seen and findings emitted should be near zero, except for candidates you conclusively refuted during investigation.';

export const DIMENSIONS = [
  { dimension: 'bug', agentType: 'deep-review:bug-detector', conditionalFlag: null, schemaExtra: {}, modelOverride: null, promptExtra: CALIBRATION_PROMPT_EXTRA },
  { dimension: 'security', agentType: 'deep-review:security-reviewer', conditionalFlag: null, schemaExtra: {}, modelOverride: 'opus', promptExtra: null },
  { dimension: 'cross_file_impact', agentType: 'deep-review:cross-file-impact', conditionalFlag: null, schemaExtra: {}, modelOverride: null, promptExtra: null },
  { dimension: 'test_coverage', agentType: 'deep-review:test-analyzer', conditionalFlag: null, schemaExtra: {}, modelOverride: null, promptExtra: null },
  { dimension: 'convention', agentType: 'deep-review:conventions-and-intent', conditionalFlag: null, schemaExtra: {}, modelOverride: null, promptExtra: null },
  { dimension: 'intent', agentType: 'deep-review:conventions-and-intent', conditionalFlag: null, schemaExtra: {}, modelOverride: null, promptExtra: null },
  { dimension: 'comment_accuracy', agentType: 'deep-review:conventions-and-intent', conditionalFlag: null, schemaExtra: {}, modelOverride: null, promptExtra: null },
  { dimension: 'type_design', agentType: 'deep-review:type-design-analyzer', conditionalFlag: null,
    schemaExtra: { encapsulation: 'number', invariants: 'number', enforcement: 'number', usefulness: 'number' }, modelOverride: null, promptExtra: null },
  { dimension: 'simplification', agentType: 'deep-review:code-simplifier', conditionalFlag: null,
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
