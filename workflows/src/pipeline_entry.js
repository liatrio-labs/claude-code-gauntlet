// pipeline_entry.js — bundle entry. Emitted LAST by build.js; its `export const meta`
// literal and plain `const PIPELINE_VERSION` are hoisted to the top of the bundle.
// This file is import-free. The workflow runtime rejects every `export` keyword
// except the meta literal (confirmed empirically: `export default` raises
// "SyntaxError: Unexpected keyword 'export'") and executes the bundle body as a
// wrapped async function — top-level `await`/`return` are the entry contract, and
// args arrive via the runtime-injected `args` global, not a function parameter.

export const meta = { name: 'deep-review', description: 'deep-review v3 pipeline: phases 3-7 orchestration (Summarize, Discover, Merge, Verify, Validate, Filter, Challenge, Report)', version: '3.0.0-dev', phases: ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report'] };
const PIPELINE_VERSION = '3.0.0-dev';

async function run(rawArgs) {
  const parsedArgs = typeof rawArgs === 'string' ? JSON.parse(rawArgs) : rawArgs;
  // Full orchestration is wired in Tasks 9–13. Until then, echo the contract.
  return { ok: true, phaseReached: 'entry', pipelineVersion: PIPELINE_VERSION, args: parsedArgs };
}

const __args = typeof args === 'string' ? JSON.parse(args) : args;
return await run(__args);
