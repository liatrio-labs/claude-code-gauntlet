import { pythonArgv, shellWord } from './wire.js';
import { modelFor, TRUSTED_SCRIPT_BASENAMES, VERDICT_PROPERTIES } from './registry.js';
import { trustedScriptPath } from './paths.js';

// The executor runs the checker; a validator cannot self-certify its own source citation.
export async function checkRefutations(ctx, input, entries, label) {
  const citations = entries.filter((e) => e.claim === 'refuted' && VERDICT_PROPERTIES.refutation_kind.enum.includes(e.refutation_kind)).map((e) => ({ id: e.id, refutation: e.refutation }));
  for (const entry of entries) delete entry.refutation_check;
  if (!citations.length) return;
  const request = JSON.stringify({ citations });
  let result;
  if (input.repoRoot && input.pluginRoot && input.diffPath && input.headShaShort && input.targetKind) {
    const command = [...pythonArgv(input.python), trustedScriptPath(input.pluginRoot, TRUSTED_SCRIPT_BASENAMES.refutations),
      '--request', request, '--repo-root', input.repoRoot, '--head-sha', input.headShaShort,
      '--target-kind', input.targetKind, '--diff-file', input.diffPath].map(shellWord).join(' ');
    const results = await ctx.parallel([() => ctx.agent(`Run exactly once and return the complete JSON stdout unchanged:\n${command}`, {
      label, agentType: 'code-gauntlet:executor', model: modelFor('code-gauntlet:executor', input.policy || {}),
      schema: { type: 'object', properties: { request: { type: 'string' }, checks: { type: 'array', items: {
        type: 'object', properties: { id: { type: 'string' }, verified: { type: 'boolean' }, reason: { type: 'string' } },
        required: ['id', 'verified', 'reason'], additionalProperties: false,
      } } }, required: ['request', 'checks'], additionalProperties: false },
    })]);
    result = results[0];
  }
  for (const entry of entries.filter((e) => e.claim === 'refuted')) {
    const checks = result?.request === request && Array.isArray(result.checks) ? result.checks.filter((row) => row.id === entry.id) : [];
    entry.refutation_check = checks.length === 1 && typeof checks[0].verified === 'boolean' && typeof checks[0].reason === 'string'
      ? { verified: checks[0].verified, reason: checks[0].reason } : { verified: false, reason: 'unverified_citation' };
  }
}
