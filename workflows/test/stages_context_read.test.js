// stages_context_read.test.js — the shared-context read-completeness contract (issue #48).
//
// WHAT BROKE. On run wf_cef39739-577 every one of the 7 discovery agents' FIRST Read of
// the 95,057-byte / 2,028-line shared context file returned 58,145 chars ending at line
// 1083. No tool result carried a truncation notice. Six agents inferred the cutoff and
// paginated to the file's end; security-reviewer did not, and reviewed roughly the first
// half of the diff while returning `{findings: [], complete: true}` — a silent under-read
// indistinguishable, from every artifact the run produced, from a clean empty result.
//
// WHAT THESE TESTS PIN. The fix is arithmetic, not instruction: the skill measures the
// file, contextReadPlan turns the measurement into the exact Read calls that cover it, and
// the prompt enumerates them. So the tests pin (a) that the plan covers every line exactly
// once for any input, (b) that each prompt carries the plan, (c) that the count-free
// degradation still says read-to-end, and (d) STRUCTURALLY that no stage can hand-roll the
// context sentence and miss all of the above — the guard requirement #48 asks for, so a
// future context file large enough to truncate cannot silently ship against a contract
// that stops after one call.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { contextReadPlan, sharedContextLine, discover, validateStage, runWith } from '../src/stages.js';
import { makeCtx, validArgs } from './helpers/pipelineMock.js';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');

// The profiled file, measured on disk 2026-07-27 with the SAME formula SKILL.md tells the
// skill to use, so the fixture is what Phase 2 would actually stamp for it:
//   contextLines = content.count("\n") + (0 if content.endswith("\n") else 1)  -> 2028
//   contextChars = len(content)                                                 -> 94784
// Issue #48 quotes 95,057 for the same file; that is `wc -c` BYTES. The file holds
// multi-byte UTF-8, so its byte count exceeds its character count by 273. contextChars is
// characters, because that is the unit a Read result is truncated in.
const PROFILED_LINES = 2028;
const PROFILED_CHARS = 94784;

// --- contextReadPlan: pure arithmetic ---------------------------------------

// Coverage is the whole point: a plan that skips a line is the bug wearing a plan's
// clothes. Asserted as a property over a wide spread of sizes rather than a few cases.
function assertCoversExactlyOnce(plan, total, label) {
  assert.ok(plan.length > 0, `${label}: expected a non-empty plan`);
  assert.equal(plan[0].offset, 1, `${label}: plan must start at line 1 (Read offsets are 1-based)`);
  let next = 1;
  for (const { offset, limit } of plan) {
    assert.equal(offset, next, `${label}: chunk starts at ${offset}, expected ${next} — gap or overlap`);
    assert.ok(limit > 0, `${label}: non-positive limit ${limit}`);
    next = offset + limit;
  }
  assert.equal(next - 1, total, `${label}: plan ends at line ${next - 1}, file has ${total}`);
}

test('contextReadPlan: covers every line exactly once across a wide size sweep', () => {
  const sizes = [1, 2, 749, 750, 751, 1499, 1500, 1501, PROFILED_LINES, 5000, 100000];
  for (const lines of sizes) {
    // chars=0/absent (line cap binds) and three chars-per-line regimes (char cap binds).
    for (const perLine of [0, 1, 47, 400]) {
      const chars = perLine === 0 ? undefined : lines * perLine;
      const plan = contextReadPlan(lines, chars);
      assertCoversExactlyOnce(plan, lines, `lines=${lines} perLine=${perLine}`);
    }
  }
});

test('contextReadPlan: the profiled file resolves to a 4-call plan under both caps', () => {
  const plan = contextReadPlan(PROFILED_LINES, PROFILED_CHARS);
  // 94784/2028 = 46.74 chars/line -> floor(30000/46.74) = 641 lines, under the 750 cap.
  assert.deepEqual(plan, [
    { offset: 1, limit: 641 },
    { offset: 642, limit: 641 },
    { offset: 1283, limit: 641 },
    { offset: 1924, limit: 105 },
  ]);
  // Every chunk stays under BOTH observed platform bounds: the documented 2000-line Read
  // cap, and the ~58,145-char return the profiled run actually hit.
  const perLine = PROFILED_CHARS / PROFILED_LINES;
  for (const { limit } of plan) {
    assert.ok(limit <= 750, `chunk of ${limit} lines exceeds the line bound`);
    assert.ok(limit * perLine < 58145, `chunk of ${limit} lines is ~${Math.round(limit * perLine)} chars — at/over the observed truncation point`);
  }
});

test('contextReadPlan: the char cap binds when lines are long, the line cap when they are short', () => {
  // 400 chars/line -> floor(30000/400) = 75 lines per call, well under the 750 line cap.
  assert.equal(contextReadPlan(1000, 400000)[0].limit, 75);
  // 1 char/line -> 30000 by chars, so the 750 line cap binds instead.
  assert.equal(contextReadPlan(10000, 10000)[0].limit, 750);
  // No chars stamped at all -> line cap alone.
  assert.equal(contextReadPlan(10000)[0].limit, 750);
});

test('contextReadPlan: a line longer than the whole char budget still yields a usable chunk', () => {
  // 60,000 chars/line would floor to 0 lines per call — clamped to 1, never an empty
  // or infinite plan. (A 0-line chunk would loop forever building the plan.)
  const plan = contextReadPlan(3, 180000);
  assert.deepEqual(plan, [{ offset: 1, limit: 1 }, { offset: 2, limit: 1 }, { offset: 3, limit: 1 }]);
});

test('contextReadPlan: unusable sizes yield [] rather than a plan built from a guess', () => {
  for (const bad of [undefined, null, 0, -1, 1.5, NaN, Infinity, '2028', {}, Number.MAX_SAFE_INTEGER + 2]) {
    assert.deepEqual(contextReadPlan(bad, PROFILED_CHARS), [], `lines=${String(bad)}`);
  }
  // A bad CHARS value is advisory only — it must not destroy an otherwise good plan.
  for (const badChars of [undefined, null, 0, -1, 2.5, NaN, 'x']) {
    assertCoversExactlyOnce(contextReadPlan(1000, badChars), 1000, `chars=${String(badChars)}`);
  }
});

test('contextReadPlan: an absurd size degrades to no plan instead of exhausting the heap', () => {
  // Found by the adversarial review of this change. The plan allocates one entry per chunk
  // and `lines` was unbounded, so contextLines = Number.MAX_SAFE_INTEGER OOM-killed the node
  // PROCESS — a V8 fatal error, not a catchable throw, so runWith's top-level catch never
  // ran, no gap was recorded, and nothing was dispatched. The ceiling is checked before the
  // first allocation. Degrading here is correct: a 1.5M-line review context is past triage,
  // and the count-free read-to-end wording is still a truthful instruction.
  assert.deepEqual(contextReadPlan(Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER), []);
  assert.deepEqual(contextReadPlan(10000000000, 470000000000), []);
  // The boundary itself: 2000 chunks x 750 lines is the largest plan still built.
  assert.equal(contextReadPlan(2000 * 750).length, 2000);
  assert.deepEqual(contextReadPlan(2000 * 750 + 1), []);
});

// --- sharedContextLine: what actually reaches the model ---------------------

test('sharedContextLine: no contextPath -> empty string, no dangling instruction', () => {
  assert.equal(sharedContextLine({}), '');
  assert.equal(sharedContextLine({ contextLines: 2028 }), '');
  assert.equal(sharedContextLine(null), '');
});

test('sharedContextLine: enumerates the exact Read calls when the size is stamped', () => {
  const line = sharedContextLine({ contextPath: '/abs/ctx.md', contextLines: PROFILED_LINES, contextChars: PROFILED_CHARS });
  assert.match(line, /Read the shared context at \/abs\/ctx\.md first/);
  assert.match(line, /exactly 4 Read calls/);
  for (const call of ['Read(offset=1, limit=641)', 'Read(offset=642, limit=641)',
    'Read(offset=1283, limit=641)', 'Read(offset=1924, limit=105)']) {
    assert.ok(line.includes(call), `missing enumerated call: ${call}`);
  }
  assert.match(line, /2028/); // the terminal line number, as a checkable target
  assert.match(line, /Make ALL of them/);
});

test('sharedContextLine: every variant states that a partial Read is unannounced', () => {
  // Requirement 2: the fix must not depend on the Read tool emitting a truncation
  // notice. None of the 7 profiled first-reads carried one, so the prompt says so.
  const variants = [
    sharedContextLine({ contextPath: '/c.md' }),
    sharedContextLine({ contextPath: '/c.md', contextLines: 300 }),
    sharedContextLine({ contextPath: '/c.md', contextLines: PROFILED_LINES, contextChars: PROFILED_CHARS }),
    sharedContextLine({ contextPath: '/c.md', contextLines: 100000, contextChars: 100000 }),
  ];
  for (const line of variants) {
    assert.match(line, /NO truncation notice/, `missing the unannounced-truncation warning in: ${line.slice(0, 90)}`);
    assert.match(line, /one Read is never the whole file/);
    assert.ok(line.endsWith(' '), 'the sentence must end with the separator space the callers rely on');
  }
});

test('sharedContextLine: no size stamped -> read-to-end wording, never a fabricated count', () => {
  const line = sharedContextLine({ contextPath: '/abs/ctx.md' });
  assert.match(line, /until a Read returns no further content/);
  assert.doesNotMatch(line, /undefined|NaN|null|Infinity/);
  assert.doesNotMatch(line, /Read\(offset=/); // no plan invented from a missing measurement
});

test('sharedContextLine: a one-call file still carries the reach-the-end check', () => {
  const line = sharedContextLine({ contextPath: '/c.md', contextLines: 300, contextChars: 9000 });
  assert.match(line, /Read\(offset=1, limit=300\)/);
  assert.match(line, /does not reach line 300/);
});

test('sharedContextLine: a pathological plan degrades to its generating rule, not a wall of calls', () => {
  // 100,000 single-char lines -> 134 chunks. Enumerating them would dwarf the prompt;
  // the arithmetic rule carries identical information in bounded space.
  const line = sharedContextLine({ contextPath: '/c.md', contextLines: 100000, contextChars: 100000 });
  assert.match(line, /exactly 134 Read calls of limit=750/);
  assert.match(line, /stepping by 750 through line 100000/);
  assert.ok((line.match(/Read\(offset=/g) || []).length === 0, 'the rule form must not also enumerate');
  assert.ok(line.length < 800, `rule form should stay compact, got ${line.length} chars`);
});

test('sharedContextLine: no prompt variant leaks undefined/NaN', () => {
  const inputs = [
    { contextPath: '/c.md' },
    { contextPath: '/c.md', contextLines: 1 },
    { contextPath: '/c.md', contextLines: 2028 },
    { contextPath: '/c.md', contextLines: 2028, contextChars: 95057 },
    { contextPath: '/c.md', contextLines: 2028, contextChars: 0 },
    { contextPath: '/c.md', contextChars: 95057 },
  ];
  for (const inp of inputs) {
    assert.doesNotMatch(sharedContextLine(inp), /undefined|NaN|Infinity|\[object/, JSON.stringify(inp));
  }
});

// --- The structural guard (issue #48 requirement 3) --------------------------

// The guard requirement #48 asks for comes in two halves, because either alone is
// evadable. The BEHAVIORAL half is primary: it is wording-independent, so it cannot be
// defeated by renaming the instruction. The SOURCE half is the backstop that catches a
// stage which builds the sentence but is not reached by the end-to-end fixture.
//
// An earlier draft of this guard counted occurrences of the literal
// "Read the shared context at " in stages.js. That was theatre: an adversarial review of
// this very change added a `crossCheckPrompt` saying "Open the shared context at <path>"
// with no plan and no truncation warning, wired it into runWith as a real dispatched
// phase, and the whole suite stayed green. A phrase count cannot enforce a structural
// property. Both guards below key on the context PATH, which any stage that names the
// file must necessarily embed, whatever verb it chooses.

test('GUARD (behavioral): every dispatched prompt that names the context file carries the read plan', async () => {
  // Wording-independent by construction: the filter is `prompt.includes(contextPath)`, so
  // a new stage saying "Open the shared context at ...", "Load the diff from ...", or
  // anything else is caught the moment it dispatches. This is the assertion that the
  // literal-phrase count only pretended to make.
  const args = validArgs({ contextLines: PROFILED_LINES, contextChars: PROFILED_CHARS });
  const ctx = makeCtx(args);
  await runWith(ctx, args);

  const contextPath = `${args.outputDir}/code-gauntlet-context-${args.headShaShort}.md`;
  const naming = ctx.calls.filter((c) => (c.prompt || '').includes(contextPath));
  assert.ok(naming.length >= 9,
    `expected summarize + 7 discovery + validate to name the context file, got ${naming.length}`);
  const offenders = naming
    .filter((c) => !/exactly 4 Read calls/.test(c.prompt) || !/NO truncation notice/.test(c.prompt))
    .map((c) => c.label);
  assert.deepEqual(offenders, [],
    'these dispatches name the shared context file without the read plan and the unannounced-truncation warning — '
    + 'they will silently review whatever one Read happens to return (issue #48)');
});

test('GUARD (behavioral): the same holds with no measurement stamped — fallback, never silence', async () => {
  const args = validArgs();
  const ctx = makeCtx(args);
  await runWith(ctx, args);
  const contextPath = `${args.outputDir}/code-gauntlet-context-${args.headShaShort}.md`;
  const naming = ctx.calls.filter((c) => (c.prompt || '').includes(contextPath));
  assert.ok(naming.length >= 9);
  const offenders = naming
    .filter((c) => !/until a Read returns no further content/.test(c.prompt) || !/NO truncation notice/.test(c.prompt))
    .map((c) => c.label);
  assert.deepEqual(offenders, [],
    'these dispatches name the context file without even the count-free read-to-end fallback');
});

test('GUARD (source): contextPath is dereferenced in exactly one function — sharedContextLine', () => {
  // The backstop. A stage function that builds its own context sentence must first GET the
  // path, and the only way to get it is to read it off its input. So: every `.contextPath`
  // dereference outside sharedContextLine is a would-be second builder. runWith's own
  // computation (`const contextPath =`) and its threading into stage inputs (the bare
  // `contextPath,` shorthand) are the allowed non-dereference forms.
  const src = readFileSync(join(SRC, 'stages.js'), 'utf8');
  const start = src.indexOf('export function sharedContextLine');
  assert.ok(start > 0, 'sharedContextLine not found — the single-owner invariant has no owner');
  // The helper's body ends at the next top-level declaration.
  const after = src.slice(start + 1);
  const end = start + 1 + after.search(/\n(?:export )?(?:async )?(?:function|const|class) /);
  const helper = src.slice(start, end);
  const rest = src.slice(0, start) + src.slice(end);

  assert.match(helper, /inp\.contextPath/, 'sharedContextLine no longer reads contextPath off its input');

  const offenders = rest.split('\n')
    .map((line, i) => ({ line: line.trim(), n: i + 1 }))
    .filter(({ line }) => /\.contextPath\b/.test(line))
    .filter(({ line }) => !line.startsWith('//') && !line.startsWith('*'))
    .map(({ n, line }) => `${n}: ${line}`);
  assert.deepEqual(offenders, [],
    'contextPath is dereferenced outside sharedContextLine — that code can build a context-read instruction '
    + 'with no read plan and no truncation warning, reopening issue #48 for its stage');
});

test('GUARD (source): every stage input threading contextPath also threads the measured size', () => {
  // contextPath without the size degrades to the count-free wording — correct for an older
  // CALLER, but a regression if runWith simply forgot to pass on a size it was handed.
  // Textual, and deliberately so: it is the third layer, behind the two behavioral guards.
  const src = readFileSync(join(SRC, 'stages.js'), 'utf8');
  const offenders = src.split('\n')
    .map((line, i) => ({ line, n: i + 1 }))
    // Both the ES6 shorthand `contextPath,` and an explicit `contextPath: <expr>,`.
    .filter(({ line }) => /(^|[\s{(])contextPath\s*(,|:\s*\w)/.test(line))
    .filter(({ line }) => !/^\s*(\/\/|\*)/.test(line))
    .filter(({ line }) => !line.includes('...contextSize'))
    .filter(({ line }) => !/const contextPath =/.test(line))
    .map(({ n, line }) => `${n}: ${line.trim()}`);
  assert.deepEqual(offenders, [],
    'a stage input threads contextPath without ...contextSize — that stage falls back to the count-free wording even though the size was measured');
});

// --- Integration: the plan reaches the agents ------------------------------

test('discoverPrompt: every dispatched discovery agent gets the enumerated plan', async () => {
  const prompts = {};
  const ctx = {
    agent: async (prompt, opts) => { prompts[opts.agentType] = prompt; return { findings: [], complete: true, total_seen: 0 }; },
    parallel: async (thunks) => Promise.all(thunks.map((t) => t())),
  };
  await discover(ctx, {
    agentFlags: {}, limits: {}, policy: {},
    contextPath: '/abs/ctx.md', contextLines: PROFILED_LINES, contextChars: PROFILED_CHARS,
  });
  const dispatched = Object.keys(prompts);
  assert.equal(dispatched.length, 7, 'all 7 discovery agents dispatch on a full-scope run');
  for (const agentType of dispatched) {
    // security-reviewer is the agent that under-read; the contract is identical for all 7.
    assert.match(prompts[agentType], /exactly 4 Read calls/, `${agentType} did not get the read plan`);
    assert.match(prompts[agentType], /Read\(offset=1924, limit=105\)/, `${agentType} is missing the tail call`);
  }
  assert.ok(prompts['code-gauntlet:security-reviewer'], 'security-reviewer dispatched');
});

test('validatePrompt: the validator gets the same plan (it reads the same file)', async () => {
  const prompts = [];
  const ctx = {
    agent: async (prompt) => { prompts.push(prompt); return { validations: [] }; },
    parallel: async (thunks) => Promise.all(thunks.map((t) => t())),
  };
  await validateStage(ctx, {
    findings: [{ id: 'F1', file: 'a.js', line_start: 1, line_end: 2, description: 'x', dimension: 'bug', severity: 'high' }],
    limits: { validateBatch: 25 }, policy: {},
    contextPath: '/abs/ctx.md', contextLines: PROFILED_LINES, contextChars: PROFILED_CHARS,
  });
  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /exactly 4 Read calls/);
});

test('runWith: threads the measured size from the args waist into summarize, discover and validate', async () => {
  const args = validArgs({ contextLines: PROFILED_LINES, contextChars: PROFILED_CHARS });
  const ctx = makeCtx(args);
  await runWith(ctx, args);

  const withContext = ctx.calls.filter((c) => (c.prompt || '').includes('Read the shared context at'));
  assert.ok(withContext.length >= 9, `expected summarize + 7 discovery + validate to read the context, got ${withContext.length}`);
  for (const call of withContext) {
    assert.match(call.prompt, /exactly 4 Read calls/, `${call.label} got the context path without the read plan`);
  }
  // The challenger is deliberately excluded — it is blind by design and never given the
  // context path — and so is the report-writer (issue #38 R1). Neither may acquire it here.
  for (const label of ['report-writer', 'artifact-writer']) {
    const calls = ctx.calls.filter((c) => (c.label || '').startsWith(label));
    for (const c of calls) assert.doesNotMatch(c.prompt, /Read the shared context/, `${label} must not read the shared context`);
  }
  assert.deepEqual(ctx.violations, []);
});

test('runWith: an args waist with NO measured size still runs and degrades to read-to-end', async () => {
  // Bench and any pre-#48 caller stamp neither field. That must remain a working,
  // unannounced-but-correct path — not a rejection and not a fabricated plan.
  const args = validArgs();
  const ctx = makeCtx(args);
  const out = await runWith(ctx, args);
  assert.notEqual(out.status, 'args-invalid');

  const withContext = ctx.calls.filter((c) => (c.prompt || '').includes('Read the shared context at'));
  assert.ok(withContext.length >= 9);
  for (const call of withContext) {
    assert.match(call.prompt, /until a Read returns no further content/, `${call.label} lost the read-to-end fallback`);
    assert.doesNotMatch(call.prompt, /Read\(offset=/, `${call.label} invented a plan with no measurement`);
    assert.doesNotMatch(call.prompt, /undefined|NaN/);
  }
});
