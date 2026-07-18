// pipeline_entry.js — bundle entry. Emitted LAST by build.js (run() references
// every sibling above it); its `export const meta` + `export const PIPELINE_VERSION`
// are hoisted to the top of the bundle by the build. This file is import-free.

export const meta = { name: 'deep-review', version: '3.0.0-dev' };
export const PIPELINE_VERSION = '3.0.0-dev';

export default async function run(rawArgs) {
  const args = typeof rawArgs === 'string' ? JSON.parse(rawArgs) : rawArgs;
  // Full orchestration is wired in Tasks 9–13. Until then, echo the contract.
  return { ok: true, phaseReached: 'entry', pipelineVersion: PIPELINE_VERSION, args };
}
