// pipeline_entry.js — bundle entry. Emitted LAST by build.js; its `export const meta`
// literal and plain `const PIPELINE_VERSION` are hoisted to the top of the bundle.
// This file is import-free IN THE BUNDLE (build.js strips the source-only imports and
// relies on concat order — stages.js/args.js are emitted above). The workflow runtime
// rejects every `export` keyword except the meta literal (confirmed empirically:
// `export default` raises "SyntaxError: Unexpected keyword 'export'") and executes the
// bundle body as a wrapped async function — top-level `await`/`return` are the entry
// contract, and args arrive via the runtime-injected `args` global, not a parameter.
import { runWith } from './stages.js';
import { parseEntryArgs } from './args.js';

export const meta = { name: 'code-gauntlet', description: 'code-gauntlet v3 pipeline: phases 3-8 orchestration (Summarize, Discover, Merge, Verify, Validate, Filter, Challenge, Report) + artifact persistence', phases: ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report'] };
const PIPELINE_VERSION = '3.1.2';

// run(rawArgs) — the thin public entry. Builds the real-globals ctx and delegates to
// runWith (in stages.js), which owns arg validation, the top-level try/catch, the full
// stage sequence, checkpoint resume, and the compact return. Kept minimal so the
// orchestration is exercised through the importable, test-driven runWith seam.
async function run(rawArgs) {
  return runWith(undefined, rawArgs);
}

const __args = parseEntryArgs(typeof args === 'undefined' ? undefined : args);
return await run(__args);
