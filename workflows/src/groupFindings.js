import { asText } from './wire.js';
import { modelFor } from './registry.js';

export function groupingPrompt(findings) {
  const fold = (text, limit) => {
    const value = String(text ?? '').replace(/\s+/g, ' ').trim();
    return limit === undefined ? value : value.slice(0, limit);
  };
  return 'Findings:\n' + findings.map((f) => {
    const range = f.line_end && f.line_end !== f.line_start ? `${f.line_start}-${f.line_end}` : f.line_start;
    return `- ${f.id} | ${fold(f.file)}:${range} | ${fold(f.severity)} | ${fold(f.title)} | ${fold(f.description, 600)} | Fix: ${fold(f.suggestion, 200)}`;
  }).join('\n');
}

export function groupingSchema(ids) {
  const id = { type: 'string', enum: ids };
  return { type: 'object', properties: { groups: { type: 'array', items: {
    type: 'object', properties: { finding_ids: { type: 'array', items: id } },
    required: ['finding_ids'], additionalProperties: false,
  } } }, required: ['groups'], additionalProperties: false };
}

export async function groupFindings(ctx, findings, policy = {}) {
  const grouping_ids = findings.map((f) => f.id);
  if (findings.length < 2) return { grouping_ids };
  try {
    const grouping_reply = await ctx.agent(groupingPrompt(findings), {
      label: 'group-findings', agentType: 'code-gauntlet:grouper',
      model: modelFor('code-gauntlet:grouper', policy), schema: groupingSchema(grouping_ids),
    });
    return { grouping_ids, grouping_reply: grouping_reply ?? null };
  } catch (error) {
    return { grouping_ids, grouping_reply: null, grouping_error: `dispatch failed: ${asText(error?.message || error) || 'unknown error'}` };
  }
}
