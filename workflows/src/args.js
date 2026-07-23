// args.js — the pipeline args waist: ARGS_VERSION, normalizeArgs, validateArgs.
// Single producer of the waist shape that bench and the pipeline entry both consume.
//
// policy shape: { tier, subagentModel } — tier records the resolved model_tier knob
// (its only valid value today is "optimized"; alternate modes are roadmap #17 V3.2).
//   - policy.subagentModel is passed to registry.js's resolvePolicy() as opts.subagentModelEnv.
//     This is a RENAME, not a passthrough — dispatch sites must map the field name.
//   - policy.tier is carried through the waist but is not read by resolvePolicy today.
export const ARGS_VERSION = 1;
// REQUIRED mirrors consumption: changedFiles/changedLines feed summarize bucketing and
// the agent-count guard; changedFilesPath is on-disk provenance the workflow never opens.
const REQUIRED = ['mode', 'repoRoot', 'outputDir', 'headShaShort', 'nonce', 'generatedAt', 'diffPath', 'changedFiles', 'changedLines', 'agentFlags', 'policy', 'limits'];

// The nonce is interpolated into the verify executor command argv (the verify stage
// derives one per slice as `${nonce}.${i}`), so it must be a single AST-safe,
// non-splitting token: word chars plus `.` `_` `-` only — no whitespace or shell
// metacharacters that could split argv or break AST-safe emission.
const NONCE_RE = /^[A-Za-z0-9._-]+$/;

// The optional Phase 8 delivery selector: { tier }. Absent is fine (the workflow defaults
// the tier to 'all' — post every challenge-survivor). A present tier must be a known value.
const DELIVERY_TIERS = ['all', 'main_only'];

export function normalizeArgs(raw) {
  return typeof raw === 'string' ? JSON.parse(raw) : raw;
}

export function validateArgs(args) {
  const errors = [];
  if (!args || typeof args !== 'object') return { ok: false, errors: ['args is not an object'] };
  if (args.argsVersion !== ARGS_VERSION) errors.push(`unsupported argsVersion ${args.argsVersion} (expected ${ARGS_VERSION})`);
  for (const k of REQUIRED) if (args[k] === undefined) errors.push(`missing required field: ${k}`);
  if (args.mode && !['interactive', 'headless'].includes(args.mode)) errors.push(`invalid mode: ${args.mode}`);
  // Only charset-check a present nonce (absence is already a REQUIRED error above).
  if (args.nonce !== undefined && (typeof args.nonce !== 'string' || !NONCE_RE.test(args.nonce))) {
    errors.push(`invalid nonce: must match ${NONCE_RE} (AST-safe, non-splitting — interpolated into the verify command argv per slice)`);
  }
  // Type-check the consumed by-value fields (absence is already a REQUIRED error above).
  if (args.changedFiles !== undefined && !Array.isArray(args.changedFiles))
    errors.push('changedFiles must be an array of repo-relative paths');
  if (args.changedLines !== undefined && typeof args.changedLines !== 'number')
    errors.push('changedLines must be a number');
  // Optional delivery selector. Absence is fine; when present it must be an object, and a
  // present tier must be a known value — an unknown tier would otherwise fall through to the
  // 'all' default in selectDelivery, silently ignoring an operator's narrowing intent.
  if (args.delivery !== undefined) {
    if (args.delivery === null || typeof args.delivery !== 'object' || Array.isArray(args.delivery)) {
      errors.push('delivery must be an object of the form { tier } when present');
    } else if (args.delivery.tier !== undefined && !DELIVERY_TIERS.includes(args.delivery.tier)) {
      errors.push(`invalid delivery.tier: ${args.delivery.tier} (expected one of ${DELIVERY_TIERS.join(', ')})`);
    }
  }
  return { ok: errors.length === 0, errors };
}
