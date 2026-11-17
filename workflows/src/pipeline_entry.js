import { runWith } from './run.js';
import { parseEntryArgs } from './entry.js';

// A distinct meta.name keeps slash-command resolution on the skill.
export const meta = { name: 'code-gauntlet-pipeline', description: 'Code Gauntlet review pipeline: discovery and gap hunter, merge, verify, validate, filter, admission, blind challenge, grouping and report', whenToUse: 'The code-gauntlet SKILL invokes this bundle by its registered name code-gauntlet:code-gauntlet-pipeline with the Phases 1-2 args waist. Invoked with anything else, it refuses before dispatch. Keep meta.name different from the skill name so slash-command resolution stays with the skill.', phases: ['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report'] };
const PIPELINE_VERSION = '4.1.4';

async function run(rawArgs) {
  return runWith({ pipelineVersion: PIPELINE_VERSION }, rawArgs);
}

// The runtime renders refusals as visible failures only when the entry throws.
const __args = parseEntryArgs(typeof args === 'undefined' ? undefined : args);
return await run(__args);
