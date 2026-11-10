// Read can truncate silently, so explicit plans prevent agents from mistaking partial context for the complete file.
// Both line and character limits constrain chunks because either platform cap can truncate a read.
export const READ_PLAN_MAX_LINES = 750;
export const READ_PLAN_MAX_CHARS = 30000;
// Expressing large plans as a rule bounds prompt size without losing coverage.
const READ_PLAN_MAX_ENUMERATED = 10;
// The allocation ceiling prevents fatal heap exhaustion that bypasses error handling and gap disclosure.
export const READ_PLAN_MAX_CHUNKS = 2000;

// The diskless workflow can check the writer's coverage and budgets, not remeasure the file.
export function validContextReadChunks(inp) {
  const chunks = inp.contextReadChunks;
  if (!Array.isArray(chunks) || chunks.length > READ_PLAN_MAX_CHUNKS
    || !Number.isSafeInteger(inp.contextLines) || inp.contextLines < 1
    || !Number.isSafeInteger(inp.contextChars) || inp.contextChars < inp.contextLines) return false;
  // An empty stamped plan discloses that the writer reached the allocation ceiling.
  if (chunks.length === 0) return true;
  let next = 1;
  let chars = 0;
  for (const chunk of chunks) {
    if (!chunk || typeof chunk !== 'object' || Array.isArray(chunk)
      || Object.keys(chunk).sort().join(',') !== 'chars,limit,offset'
      || chunk.offset !== next || !Number.isSafeInteger(chunk.limit)
      || chunk.limit < 1 || chunk.limit > READ_PLAN_MAX_LINES
      || !Number.isSafeInteger(chunk.chars) || chunk.chars < chunk.limit
      || (chunk.chars > READ_PLAN_MAX_CHARS && chunk.limit !== 1)) return false;
    next += chunk.limit;
    chars += chunk.chars;
  }
  return next === inp.contextLines + 1 && chars === inp.contextChars;
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
    return validContextReadChunks({ contextLines: lines, contextChars: chars, contextReadChunks: chunks })
      ? chunks.map(({ offset, limit }) => ({ offset, limit })) : [];
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
  if (!plan.every((p, i) => p.offset === 1 + i * span && (i === plan.length - 1 || p.limit === span))) {
    const pairs = plan.map((p) => `${p.offset}:${p.limit}`).join(', ');
    return `${prefix} It is ${total} lines, with ${plan.length} planned Read calls (offset:limit): ${pairs}. ${tail} `;
  }
  return `${prefix} It is ${total} lines, with ${plan.length} planned Read calls of limit=${span}, at offsets 1, ${1 + span}, ${1 + 2 * span}, … stepping by ${span} through line ${total}. ${tail} `;
}

export function setupContext(inp) {
  const plan = contextReadPlan(inp.contextLines, inp.contextChars, inp.contextMaxLineChars, inp.contextReadChunks);
  const contextLine = sharedContextLine(inp, plan);
  // Missing and unplannable counts both need disclosure because they lose the exact coverage plan.
  const contextSizeGap = inp.contextLines === undefined
    ? ['context_unmeasured: args.contextLines was not stamped, so the shared-context read plan could not be computed — '
      + 'every agent got the count-free "read until a Read returns no further content" wording instead of the exact '
      + 'Read calls covering the file. A Read can return part of a file '
      + 'with no truncation notice, and an agent that stops there reviews only what it saw. Stamp contextLines '
      + '(and contextChars and contextMaxLineChars) from the Phase 2 step that writes the context file.']
    : (plan.length === 0
      ? ['context_unplannable: args.contextLines was stamped (' + inp.contextLines + ') but exceeds the read-plan chunk '
        + 'ceiling, so the shared-context read plan could not be built — every agent got the count-free '
        + '"read until a Read returns no further content" wording instead of the exact Read calls covering the file. '
        + 'Shrink the shared context '
        + '(or raise READ_PLAN_MAX_CHUNKS with a matching prompt-size budget) so a plan can be computed.']
      : []);
  return { contextLine, contextSizeGap };
}
