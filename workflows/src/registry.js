// registry.js — single point of extension. Adding a dimension = one entry here + one agent .md.
export const DIMENSIONS = [
  { dimension: 'bug', agentType: 'deep-review:bug-detector', conditionalFlag: null, schemaExtra: {}, modelOverride: null },
  { dimension: 'security', agentType: 'deep-review:security-reviewer', conditionalFlag: null, schemaExtra: {}, modelOverride: 'opus' },
  { dimension: 'cross_file_impact', agentType: 'deep-review:cross-file-impact', conditionalFlag: null, schemaExtra: {}, modelOverride: null },
  { dimension: 'test_coverage', agentType: 'deep-review:test-analyzer', conditionalFlag: null, schemaExtra: {}, modelOverride: null },
  { dimension: 'convention', agentType: 'deep-review:conventions-and-intent', conditionalFlag: null, schemaExtra: {}, modelOverride: null },
  { dimension: 'intent', agentType: 'deep-review:conventions-and-intent', conditionalFlag: null, schemaExtra: {}, modelOverride: null },
  { dimension: 'comment_accuracy', agentType: 'deep-review:conventions-and-intent', conditionalFlag: null, schemaExtra: {}, modelOverride: null },
  { dimension: 'type_design', agentType: 'deep-review:type-design-analyzer', conditionalFlag: null,
    schemaExtra: { encapsulation: 'number', invariants: 'number', enforcement: 'number', usefulness: 'number' }, modelOverride: null },
  { dimension: 'simplification', agentType: 'deep-review:code-simplifier', conditionalFlag: null,
    schemaExtra: { before: 'string', after: 'string' }, modelOverride: null },
];

export const AGENTS = [...new Set(DIMENSIONS.map((d) => d.agentType))];

// Deviations only. Frontmatter is the baseline model; this encodes S5 overrides.
const STAGE_DEFAULTS = { validator: 'sonnet', challenger: 'sonnet', executor: 'sonnet', report: 'sonnet' };

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
