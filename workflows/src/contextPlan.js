// Read can truncate silently, so explicit plans prevent agents from mistaking partial context for the complete file.
// The token cap needs a UTF-8 byte budget that also reserves line-number prefixes.
export const READ_PLAN_MAX_LINES = 750;
// Aggregate-only receipts retain their original character heuristic.
export const READ_PLAN_MAX_CHARS = 30000;
export const READ_PLAN_MAX_BYTES = 24000;
export const READ_PLAN_LINE_PREFIX_BYTES = 12;
// Full calls are short up to ten; compact pairs list at most 64 chunks (702 characters).
const READ_PLAN_MAX_ENUMERATED = 10;
const READ_PLAN_MAX_LISTED = 64;
// Uniform plans use a fixed-size rule; this separate ceiling bounds allocation and receipt size.
export const READ_PLAN_MAX_CHUNKS = 2000;

function chunkReadPlan(lines, chunks) {
  if (chunks === undefined) return { reason: 'contextReadChunks was not stamped' };
  if (!Array.isArray(chunks)) return { reason: 'contextReadChunks is not a line-count array' };
  if (chunks.length === 0) return { reason: 'empty contextReadChunks for a non-empty context' };
  if (chunks.length > READ_PLAN_MAX_CHUNKS) return { reason: 'contextReadChunks exceeds the allocation ceiling' };
  if (chunks.some((count) => !Number.isSafeInteger(count))) return { reason: 'contextReadChunks contains a non-integer line count' };
  if (chunks.some((count) => count < 1)) return { reason: 'contextReadChunks contains a non-positive line count' };
  if (chunks.some((count) => count > READ_PLAN_MAX_LINES)) return { reason: 'contextReadChunks exceeds the per-read line limit' };
  if (chunks.reduce((sum, count) => sum + count, 0) !== lines) return { reason: 'contextReadChunks does not cover contextLines' };
  const span = chunks[0];
  const uniform = chunks.every((count, i) => i === chunks.length - 1 ? count <= span : count === span);
  if (!uniform && chunks.length > READ_PLAN_MAX_LISTED) return { reason: 'variable contextReadChunks exceeds the 64-chunk prompt budget' };
  let offset = 1;
  return { plan: chunks.map((limit) => {
    const chunk = { offset, limit };
    offset += limit;
    return chunk;
  }) };
}

function lineWidthEstimate(maxLineChars, chars, total) {
  if (Number.isSafeInteger(maxLineChars) && maxLineChars > 0) return maxLineChars;
  if (Number.isSafeInteger(chars) && chars > 0) return chars / total;
  return 0;
}

// Offsets are one-based to match the Read tool.
// An unknown line count cannot justify a guessed coverage plan.
export function contextReadPlan(lines, chars, maxLineChars, chunks) {
  if (chunks !== undefined) {
    const measured = chunkReadPlan(lines, chunks);
    if (measured.plan) return measured.plan;
  }
  const total = Number.isSafeInteger(lines) && lines > 0 ? lines : 0;
  if (total === 0) return [];
  // Maximum line width bounds uneven chunks; older receipts supply only an average estimate.
  const spanFor = (width) => Math.max(1, Math.min(READ_PLAN_MAX_LINES,
    width > 0 ? Math.floor(READ_PLAN_MAX_CHARS / width) : READ_PLAN_MAX_LINES));
  let span = spanFor(lineWidthEstimate(maxLineChars, chars, total));
  // One oversized line must not erase a usable plan; continuation covers truncated chunks.
  if (Math.ceil(total / span) > READ_PLAN_MAX_CHUNKS && Number.isSafeInteger(chars) && chars > 0) {
    span = spanFor(lineWidthEstimate(undefined, chars, total));
  }
  if (Math.ceil(total / span) > READ_PLAN_MAX_CHUNKS) return [];
  const plan = [];
  for (let offset = 1; offset <= total; offset += span) {
    plan.push({ offset, limit: Math.min(span, total - offset + 1) });
  }
  return plan;
}

// Stages receive a prebuilt instruction, removing their ability to construct competing context-read plans.
export function sharedContextLine(inp, plan) {
  const path = inp && inp.contextPath;
  if (!path) return '';
  const prefix = `Read the shared context at ${path} first. Tagged blocks end only at the closing tag repeating their opening id; intervening tags are content.`;
  plan = plan || contextReadPlan(inp.contextLines, inp.contextChars, inp.contextMaxLineChars, inp.contextReadChunks);
  if (plan.length === 0) {
    // Without a usable count, fixed steps leave only end detection to agent judgment.
    const s = READ_PLAN_MAX_LINES;
    return `${prefix} Read(offset=1, limit=${s}), continuing after the last received line until a call returns no further content. `;
  }
  const total = inp.contextLines;
  if (plan.length === 1) {
    return `${prefix} It is ${total} lines: Read(offset=1, limit=${plan[0].limit}); continue after the last received line until line ${total}. `;
  }
  const tail = `If partial, continue after the last received line until line ${total}.`;
  if (plan.length <= READ_PLAN_MAX_ENUMERATED) {
    const calls = plan.map((p) => `Read(offset=${p.offset}, limit=${p.limit})`).join(', ');
    return `${prefix} It is ${total} lines, with ${plan.length} planned Read calls: ${calls}. ${tail} `;
  }
  const span = plan[0].limit;
  if (!plan.every((p, i) => p.offset === 1 + i * span && (i === plan.length - 1 ? p.limit <= span : p.limit === span))) {
    const pairs = plan.map((p) => `${p.offset}:${p.limit}`).join(', ');
    return `${prefix} It is ${total} lines, with ${plan.length} planned Read calls (offset:limit): ${pairs}. ${tail} `;
  }
  return `${prefix} It is ${total} lines, with ${plan.length} planned Read calls of limit=${span}, at offsets 1, ${1 + span}, ${1 + 2 * span}, … stepping by ${span} through line ${total}. ${tail} `;
}

export function setupContext(inp) {
  const contextSizeGap = [];
  const hasPlanReceipt = inp.contextReadPlanVersion !== undefined || inp.contextReadChunks !== undefined;
  let measured;
  if (hasPlanReceipt) {
    measured = inp.contextReadPlanVersion === 1
      ? chunkReadPlan(inp.contextLines, inp.contextReadChunks)
      : { reason: 'missing or unsupported contextReadPlanVersion' };
    if (measured.reason) contextSizeGap.push(`context_plan_fallback: ${measured.reason}; using the aggregate-based shared-context read plan.`);
  }
  const plan = measured?.plan || contextReadPlan(inp.contextLines, inp.contextChars, inp.contextMaxLineChars);
  const contextLine = sharedContextLine(inp, plan);
  if (inp.contextLines === undefined) {
    contextSizeGap.push('context_unmeasured: args.contextLines was not stamped, so the shared-context read plan could not be computed — '
      + 'every agent got the count-free "read until a Read returns no further content" wording instead of the exact '
      + 'Read calls covering the file. A Read can return part of a file '
      + 'with no truncation notice, and an agent that stops there reviews only what it saw. Stamp contextLines, '
      + 'contextChars, contextMaxLineChars, contextReadPlanVersion and contextReadChunks from the Phase 2 step that writes the context file.');
  } else if (plan.length === 0) {
    contextSizeGap.push('context_unplannable: args.contextLines was stamped (' + inp.contextLines + ') but exceeds the read-plan chunk '
      + 'ceiling, so the shared-context read plan could not be built — every agent got the count-free '
      + '"read until a Read returns no further content" wording instead of the exact Read calls covering the file. '
      + 'Shrink the shared context '
      + '(or raise READ_PLAN_MAX_CHUNKS with a matching prompt-size budget) so a plan can be computed.');
  }
  return { contextLine, contextSizeGap };
}
