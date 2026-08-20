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
// this plugin, so an identical name made `/code-gauntlet:code-gauntlet` resolve to the
// WORKFLOW rather than the skill — and the workflow, invoked by name, receives the raw
// user string ("PR 87") where it requires the Phases 1-2 args waist. Measured 2026-07-30:
// the first task of that session failed by construction, and recovering from it the model
// went hunting for the plugin root with `find /` and ran an OLD cached version. The name
// is otherwise cosmetic here — the skill invokes this bundle by `scriptPath`, never by
// name — so it is free to disambiguate. `whenToUse` says the same thing to whoever reads
// the workflow list instead of this comment.
export const meta = { name: 'code-gauntlet-pipeline', description: 'code-gauntlet v3 pipeline: phases 3-8 orchestration (Summarize, Discover, Merge, Verify, Validate, Filter, Challenge, Report) + artifact persistence', whenToUse: 'Never invoke by name — the code-gauntlet SKILL runs this bundle by scriptPath after Phases 1-2 build the args waist. Invoked by name it receives a raw user string instead of args and fails immediately.', phases: ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report'] };
const PIPELINE_VERSION = '3.17.0';

// run(rawArgs) — the thin public entry. Builds the real-globals ctx and delegates to
// runWith (in stages.js), which owns arg validation, the top-level try/catch, the full
// stage sequence, checkpoint resume, and the compact return. Kept minimal so the
// orchestration is exercised through the importable, test-driven runWith seam.
async function run(rawArgs) {
  return runWith(undefined, rawArgs);
}

// parseEntryArgs THROWS on a refusal (absent args, a review-target reference like a bare
// PR number/URL, or any other non-waist shape) rather than returning — the only signal
// this platform renders as a visible failure (issue #27; see the doc comment on
// parseEntryArgs in args.js for the verified reasoning). runWith carries the identical
// wording for its own, throw-free seam, so a naked Workflow call and a programmatic
// runWith() caller see the same message either way.
const __args = parseEntryArgs(typeof args === 'undefined' ? undefined : args);
return await run(__args);
