// Deterministic shared-context read plans and prompt wording.

// --- Shared context file: the deterministic read plan ---------------
//
// A `Read` returns only PART of a large file and says nothing about it. Measured on
// run wf_cef39739-577: all 7 discovery agents' first Read of the 95,057-byte /
// 2,028-line shared context file returned 58,145 chars ending at line 1083, with no
// truncation notice in ANY of the 7 tool results. Six agents inferred the cutoff and
// paginated on; security-reviewer did not, and reviewed roughly the first half of the
// diff while reporting as if it had seen all of it.
//
// The fix is arithmetic, not instruction. The skill stamps the file's size
// (contextLines/contextChars, measured after it writes the file); these helpers turn
// that into the EXACT list of Read calls that covers it, and the prompt enumerates
// them. The agent's job is "make these listed calls", not "notice an invisible
// truncation and drive a loop" — a judgment it demonstrably gets wrong 1 time in 7.
//
// Both bounds below are observed platform behavior, not a documented contract, so the
// planner stays well under each: the Read tool documents a 2,000-line default cap, and
// the run above puts a character cap near 58,000. A plan chunk is sized by whichever
// binds first, from the stamped chars-per-line.
const READ_PLAN_MAX_LINES = 750;
const READ_PLAN_MAX_CHARS = 30000;
// Above this many chunks, the prompt states the plan as its generating rule (first
// offset, span, final line) instead of listing every call — identical information,
// bounded prompt size. Four chunks covers the profiled file; the rule form is for
// pathological inputs (very many very short lines), never the normal path.
const READ_PLAN_MAX_ENUMERATED = 10;
// Hard ceiling on the plan's SIZE, checked before a single element is allocated. Without
// it the loop below allocates one object per chunk for whatever `lines` it is handed, and
// the waist accepts any positive safe integer: contextLines = Number.MAX_SAFE_INTEGER
// OOM-kills the node process — a V8 fatal error, not a catchable throw, so runWith's
// top-level catch never runs, no gap is recorded, and nothing is dispatched. 2000 chunks
// covers a 1.5M-line context file; past that the plan degrades to the count-free
// read-to-end wording, because a review context that large is already beyond triage.
const READ_PLAN_MAX_CHUNKS = 2000;

// contextReadPlan(lines, chars) -> [{ offset, limit }, ...] covering lines 1..lines.
// PURE arithmetic. Offsets are 1-based to match the Read tool's `cat -n` numbering
// (the profiled agents' own follow-up Reads used 1-based offsets). An unusable or
// absent line count yields [] — the caller then falls back to the count-free wording
// rather than emitting a plan built from a guess.
export function contextReadPlan(lines, chars) {
  const total = Number.isSafeInteger(lines) && lines > 0 ? lines : 0;
  if (total === 0) return [];
  // Chars is advisory: absent/unusable just means the line cap binds alone.
  const perLine = Number.isSafeInteger(chars) && chars > 0 ? chars / total : 0;
  const byChars = perLine > 0 ? Math.floor(READ_PLAN_MAX_CHARS / perLine) : READ_PLAN_MAX_LINES;
  const span = Math.max(1, Math.min(READ_PLAN_MAX_LINES, byChars));
  // Bound the ALLOCATION, not just the input: computed before the loop so an absurd size
  // degrades instead of exhausting the heap (see READ_PLAN_MAX_CHUNKS).
  if (Math.ceil(total / span) > READ_PLAN_MAX_CHUNKS) return [];
  const plan = [];
  for (let offset = 1; offset <= total; offset += span) {
    plan.push({ offset, limit: Math.min(span, total - offset + 1) });
  }
  return plan;
}

// sharedContextLine(inp) -> the leading context-read sentence, or '' when no context
// path was threaded. Called ONCE, by runWith, which threads the resulting STRING to the
// stages that need it. The stages never receive the path, so no stage is ABLE to build a
// context-read instruction of its own — the property that used to be defended by scanning
// this file's source text for hand-rolled copies. Capability removed rather than guarded:
// an adversarial review defeated the text-scan version in one edit by rewording.
function formatSharedContextLine(inp, plan) {
  const path = inp && inp.contextPath;
  if (!path) return '';
  const head = `Read the shared context at ${path} first — it has the diff, project rules, and risk classification.`;
  // A partial Read is INVISIBLE: it carries no truncation notice and looks exactly
  // like a complete file. Stated on every path, plan or no plan.
  const warn = 'A single Read of this file returns only PART of it and gives NO truncation notice, so one Read is never the whole file.';
  const prefix = `${head} ${warn}`;
  plan = plan || contextReadPlan(inp.contextLines, inp.contextChars);
  if (plan.length === 0) {
    // No measurement (or one too large to plan): the terminus is unknown, so end-detection
    // is unavoidable here. The STEPPING still is not — spelling out fixed-size steps keeps
    // the only judgment "did that call return anything", instead of the open-ended "have I
    // read enough yet" that an agent answered wrong.
    const s = READ_PLAN_MAX_LINES;
    return `${prefix} Read it in ${s}-line steps — Read(offset=1, limit=${s}), then Read(offset=${1 + s}, limit=${s}), then Read(offset=${1 + 2 * s}, limit=${s}), continuing to step by ${s} until a call returns no further content. Do not stop before that. `;
  }
  const total = inp.contextLines;
  if (plan.length === 1) {
    return `${prefix} It is ${total} lines: read it with Read(offset=1, limit=${plan[0].limit}), and if that call does not reach line ${total}, continue with offset set past the last line you received until it does. `;
  }
  const tail = `Make ALL of them — stopping early means you review only the part you saw. If any call does not land where the plan says, continue with offset set past the last line you received until you reach line ${total}.`;
  if (plan.length <= READ_PLAN_MAX_ENUMERATED) {
    const calls = plan.map((p) => `Read(offset=${p.offset}, limit=${p.limit})`).join(', ');
    return `${prefix} It is ${total} lines, which takes exactly ${plan.length} Read calls: ${calls}. ${tail} `;
  }
  const span = plan[0].limit;
  return `${prefix} It is ${total} lines, which takes exactly ${plan.length} Read calls of limit=${span}, at offsets 1, ${1 + span}, ${1 + 2 * span}, … stepping by ${span} through line ${total}. ${tail} `;
}

export function sharedContextLine(inp, plan = undefined) {
  return formatSharedContextLine(inp, plan);
}

export function setupContext(inp) {
  const plan = contextReadPlan(inp.contextLines, inp.contextChars);
  const contextLine = sharedContextLine(inp, plan);
  // Missing and unplannable counts use the same count-free fallback, and both need disclosure.
  const contextSizeGap = inp.contextLines === undefined
    ? ['context_unmeasured: args.contextLines was not stamped, so the shared-context read plan could not be computed — '
      + 'every agent got the count-free "read until a Read returns no further content" wording instead of the exact '
      + 'Read calls covering the file. That restores the failure mode of issue #48: a Read returns part of a large file '
      + 'with no truncation notice, and an agent that stops there reviews only what it saw. Stamp contextLines '
      + '(and contextChars) from the Phase 2 step that writes the context file.']
    : (plan.length === 0
      ? ['context_unplannable: args.contextLines was stamped (' + inp.contextLines + ') but exceeds the read-plan chunk '
        + 'ceiling, so the shared-context read plan could not be built — every agent got the count-free '
        + '"read until a Read returns no further content" wording instead of the exact Read calls covering the file. '
        + 'That restores the failure mode of issue #48 for an oversized context. Shrink the shared context '
        + '(or raise READ_PLAN_MAX_CHUNKS with a matching prompt-size budget) so a plan can be computed.']
      : []);
  return { contextLine, contextSizeGap };
}
