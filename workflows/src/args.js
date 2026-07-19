// args.js — the pipeline args waist: ARGS_VERSION, normalizeArgs, validateArgs.
// Single producer of the waist shape that bench and the pipeline entry both consume.
export const ARGS_VERSION = 1;
const REQUIRED = ['mode', 'repoRoot', 'outputDir', 'headShaShort', 'nonce', 'generatedAt', 'diffPath', 'changedFilesPath', 'agentFlags', 'policy', 'limits'];

export function normalizeArgs(raw) {
  return typeof raw === 'string' ? JSON.parse(raw) : raw;
}

export function validateArgs(args) {
  const errors = [];
  if (!args || typeof args !== 'object') return { ok: false, errors: ['args is not an object'] };
  if (args.argsVersion !== ARGS_VERSION) errors.push(`unsupported argsVersion ${args.argsVersion} (expected ${ARGS_VERSION})`);
  for (const k of REQUIRED) if (args[k] === undefined) errors.push(`missing required field: ${k}`);
  if (args.mode && !['interactive', 'headless'].includes(args.mode)) errors.push(`invalid mode: ${args.mode}`);
  // frontier:true demands an explicit full model-id string (Fable alias unconfirmed — no silent fallback).
  if (args.policy && args.policy.frontier === true && !args.policy.frontierModelId) {
    errors.push('policy.frontier is true but policy.frontierModelId is missing (a full model-id string is required)');
  }
  return { ok: errors.length === 0, errors };
}
