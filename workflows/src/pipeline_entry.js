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

// meta.name MUST NOT equal the skill's name (`code-gauntlet`). Both are registered by
// this plugin, so an identical name would make `/code-gauntlet:code-gauntlet` resolve to
// the WORKFLOW rather than the skill. The skill invokes this bundle by the registered
// name below with the Phases 1-2 args waist. Anything else refuses before dispatch.
export const meta = { name: 'code-gauntlet-pipeline', description: 'code-gauntlet v3 pipeline: phases 3-8 orchestration (Summarize, Discover, Merge, Verify, Validate, Filter, Challenge, Report) + artifact persistence', whenToUse: 'The code-gauntlet SKILL invokes this bundle by its registered name code-gauntlet:code-gauntlet-pipeline with the Phases 1-2 args waist. Invoked with anything else, it refuses before dispatch. Keep meta.name different from the skill name so slash-command resolution stays with the skill.', phases: ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report'] };
const PIPELINE_VERSION = '3.37.1';

// run(rawArgs) — the thin public entry. Builds the real-globals ctx and delegates to
// runWith (in stages.js), which owns arg validation, the top-level try/catch, the full
// stage sequence, checkpoint resume, and the compact return. Kept minimal so the
// orchestration is exercised through the importable, test-driven runWith seam.
async function run(rawArgs) {
  return runWith({ pipelineVersion: PIPELINE_VERSION }, rawArgs);
}

// parseEntryArgs THROWS on a refusal (absent args, a review-target reference like a bare
// PR number/URL, or any other non-waist shape) rather than returning — the only signal
// this platform renders as a visible failure (issue #27; see the doc comment on
// parseEntryArgs in args.js for the verified reasoning). runWith carries the identical
// wording for its own, throw-free seam, so a naked Workflow call and a programmatic
// runWith() caller see the same message either way.
const __args = parseEntryArgs(typeof args === 'undefined' ? undefined : args);
return await run(__args);
