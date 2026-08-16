// entry_guard.test.js — issue #27: the two entry-args seams get a message that names the
// caller's own review-target reference and the exact skill invocation to run instead.
//
// R3 verification (spec, do not re-litigate): a returned {ok:false,...} is reported to the
// caller as <status>completed</status> — identical to success (recorded bench runs;
// anthropics/claude-code#66745) — so the entry (pipeline_entry.js -> parseEntryArgs) keeps
// THROWING; only a throw renders as a visible platform failure. runWith's own seam is
// throw-free by contract (stages.js's runWith doc comment: "NEVER lets a throw escape") but its
// normalizeArgsReport(rawArgs) call sat OUTSIDE its try/catch, so a non-JSON-string rawArgs
// (e.g. 'PR 310') escaped as an uncaught native SyntaxError — empirically confirmed against
// this repo's source. runWith's arm therefore RETURNS the same refusalFrom wording
// wrapped in makeArgsRejectEnvelope. Two signals, one message — pinned below by
// the one-message-two-signals test, not by convention.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classifyReviewTarget, parseEntryArgs, entryArgs, makeArgsRejectEnvelope, SKILL_RECOVERY_LINE } from '../src/args.js';
import { runWith } from '../src/stages.js';
import { validArgs } from './helpers/pipelineMock.js';

// Local waist fixture, in the style of args.test.js:8-16 (duplicated deliberately — the
// same false-positive-exclusion-list rationale: a fixture failure in one file must not be
// able to hide a fixture failure in the other).
const good = {
  argsVersion: 1, mode: 'interactive', repoRoot: '/r', outputDir: '/r/.code-gauntlet',
  headShaShort: 'abc123', nonce: 'n-1', generatedAt: '2026-07-18T00:00:00Z',
  diffPath: '/r/.code-gauntlet/d.patch', changedFilesPath: '/r/.code-gauntlet/f.json',
  changedFiles: ['a.js'], changedLines: 1,
  reviewConfigPath: null, riskTable: [{ path: 'a.js', risk: 'medium' }],
  policy: { tier: 'optimized', subagentModel: null },
  limits: { summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 200 },
};

// The exact copy-paste lines the message spec (1c) pins verbatim. These strings ARE the
// deliverable of this issue — everything else in the message is prose scaffolding around
// them.
const ABSENT_RECOVERY = 'Skill("code-gauntlet:code-gauntlet", args="<PR number or URL — omit to auto-detect this branch\'s PR, else local changes>")';
const SHAPE_RECOVERY = 'Skill("code-gauntlet:code-gauntlet", args="<your PR number or URL>")';
const targetRecovery = (ref) => `Skill("code-gauntlet:code-gauntlet", args="${ref}")`;

// spec 1c labels, keyed by classifyReviewTarget's `kind`.
const KIND_LABEL = {
  pr_url: 'a GitHub PR URL',
  mr_url: 'a GitLab MR URL',
  repo_ref: 'an owner/repo PR reference',
  pr_number: 'a bare PR/MR number',
  pr_shorthand: 'a PR/MR reference',
};

// --- classifyReviewTarget: positives -----------------------------------------------------
// [description, raw, expected kind, expected ref, expected number]
const POSITIVE_CASES = [
  ['a GitHub PR URL', 'https://github.com/o/r/pull/45', 'pr_url', 'https://github.com/o/r/pull/45', '45'],
  ['an http://-prefixed GitHub PR URL', 'http://github.com/o/r/pull/45', 'pr_url', 'http://github.com/o/r/pull/45', '45'],
  ['a scheme-less GitHub PR URL', 'github.com/o/r/pull/45', 'pr_url', 'github.com/o/r/pull/45', '45'],
  ['a GitHub PR URL with a trailing slash', 'https://github.com/o/r/pull/45/', 'pr_url', 'https://github.com/o/r/pull/45/', '45'],
  ['a GitHub PR URL with a ?tab=files query', 'https://github.com/o/r/pull/45?tab=files', 'pr_url', 'https://github.com/o/r/pull/45?tab=files', '45'],
  ['a GitHub PR URL with a /files suffix', 'https://github.com/o/r/pull/45/files', 'pr_url', 'https://github.com/o/r/pull/45/files', '45'],
  ['a GitHub Enterprise PR URL', 'https://github.mycorp.internal/o/r/pull/45', 'pr_url', 'https://github.mycorp.internal/o/r/pull/45', '45'],
  ['a GitLab MR URL', 'https://gitlab.com/g/p/-/merge_requests/89', 'mr_url', 'https://gitlab.com/g/p/-/merge_requests/89', '89'],
  ['a self-hosted GitLab MR URL with a subgroup path', 'https://gitlab.mycorp.internal/group/subgroup/project/-/merge_requests/89', 'mr_url', 'https://gitlab.mycorp.internal/group/subgroup/project/-/merge_requests/89', '89'],
  ['an owner/repo#n reference', 'liatrio-forge/pet-clinic#45', 'repo_ref', 'liatrio-forge/pet-clinic#45', '45'],
  ['a bare PR-number string (the valid-JSON forcing case)', '45', 'pr_number', '45', '45'],
  ['a #-prefixed shorthand', '#310', 'pr_shorthand', '#310', '310'],
  ['a !-prefixed shorthand (GitLab MR style)', '!89', 'pr_shorthand', '!89', '89'],
  ['a "PR <n>" shorthand', 'PR 310', 'pr_shorthand', 'PR 310', '310'],
  ['a "MR#<n>" shorthand', 'MR#7', 'pr_shorthand', 'MR#7', '7'],
  // PR_SHORTHAND_WORD_RE alternatives + case-insensitivity (comment at args.js calls out
  // lowercase "pr 310" as an intended catch; pin every alternation branch).
  ['a lowercase "pr <n>" shorthand', 'pr 310', 'pr_shorthand', 'pr 310', '310'],
  ['a "pull request <n>" shorthand', 'pull request 45', 'pr_shorthand', 'pull request 45', '45'],
  ['a "merge request <n>" shorthand', 'merge request 89', 'pr_shorthand', 'merge request 89', '89'],
  ['a bare "pull <n>" shorthand', 'pull 45', 'pr_shorthand', 'pull 45', '45'],
];

for (const [desc, raw, kind, ref, number] of POSITIVE_CASES) {
  test(`classifyReviewTarget recognizes ${desc}: ${JSON.stringify(raw)}`, () => {
    const got = classifyReviewTarget(raw);
    assert.ok(got, `expected a match for ${JSON.stringify(raw)}`);
    assert.equal(got.kind, kind);
    assert.equal(got.ref, ref, 'ref must be the caller\'s own text, trimmed — never a reconstruction');
    assert.equal(got.number, number);
  });
}

test('classifyReviewTarget recognizes a JS number input as pr_number (not only its string form)', () => {
  assert.deepEqual(classifyReviewTarget(45), { kind: 'pr_number', number: '45', ref: '45' });
});

// --- classifyReviewTarget: negatives ------------------------------------------------------
const NEGATIVE_CASES = [
  ['an empty string', ''],
  ['a whitespace-only string', '   '],
  ['a 300-char string (over the 200-char classifier bound)', 'x'.repeat(300)],
  ['an issues URL (not a PR)', 'https://github.com/o/r/issues/45'],
  ['a blob/line-anchor URL', 'https://github.com/o/r/blob/main/x.js#L45'],
  ['a compare URL', 'https://github.com/o/r/compare/main...feature'],
  ['a bare repo URL (no /pull/ segment)', 'https://github.com/o/r'],
  ['a branch-compare shorthand', 'main..feature'],
  ['a source file path that looks like owner/repo', 'src/args.js'],
  ['an ISO date', '2026-07-28'],
  ['a semver string', '3.2.4'],
  ['free prose', 'review my changes'],
  ['a lone zero', '0'],
  ['a leading-zero PR number', '0045'],
  ['an 8-digit run (over the 7-digit <n> bound)', '12345678'],
  ['a plain object', {}],
  ['an array', []],
  ['null', null],
  ['NaN', NaN],
  ['a negative number', -1],
  ['a fractional number', 1.5],
];

for (const [desc, raw] of NEGATIVE_CASES) {
  test(`classifyReviewTarget does not match ${desc}`, () => {
    assert.equal(classifyReviewTarget(raw), null);
  });
}

// ReDoS guard (spec 1a rule 4): every pattern must be linear — no nested quantifiers. A
// catastrophically-backtracking regex handed any caller-controlled string would hang the
// workflow with no way to recover (there is no per-call timeout on the JS runtime side).
test('classifyReviewTarget: a pathological 200-char input returns in well under a second (ReDoS guard)', () => {
  const pathological = 'a/'.repeat(100); // exactly 200 chars — at, not over, the length bound
  const start = Date.now();
  const got = classifyReviewTarget(pathological);
  const elapsed = Date.now() - start;
  assert.ok(elapsed < 1000, `classifyReviewTarget took ${elapsed}ms on a pathological input — suspect catastrophic backtracking`);
  assert.equal(got, null);
});

// --- parseEntryArgs: absent class (requirement 5) -----------------------------------------
// Before this change, parseEntryArgs(undefined) returned `undefined` silently and the
// naked-call caller (least likely to parse a return value) fell through to a generic
// downstream validateArgs rejection with no recovery line at all.

test('parseEntryArgs(undefined) throws the absent-class message, not a silent undefined', () => {
  assert.throws(() => parseEntryArgs(undefined), (err) => {
    assert.ok(err.message.includes('code-gauntlet skill'), err.message);
    assert.ok(err.message.includes('(got undefined)'), err.message);
    assert.ok(err.message.includes('resumeFromRunId'), err.message);
    assert.ok(err.message.includes(ABSENT_RECOVERY), err.message);
    return true;
  });
});

test('parseEntryArgs(null) throws the absent-class message with (got null)', () => {
  assert.throws(() => parseEntryArgs(null), (err) => {
    assert.ok(err.message.includes('code-gauntlet skill'), err.message);
    assert.ok(err.message.includes('(got null)'), err.message);
    assert.ok(err.message.includes(ABSENT_RECOVERY), err.message);
    return true;
  });
});

// --- parseEntryArgs: target class (requirement 3 message wiring) --------------------------
// Every classified review-target input refuses with the labeled message and the caller's
// OWN ref echoed verbatim into the copy-paste Skill(...) line.

for (const [desc, raw, kind, ref] of POSITIVE_CASES) {
  test(`parseEntryArgs refuses a review-target input (${desc}) with the labeled recovery line`, () => {
    assert.throws(() => parseEntryArgs(raw), (err) => {
      assert.ok(err.message.includes('code-gauntlet skill'), err.message);
      assert.ok(err.message.includes(KIND_LABEL[kind]), err.message);
      assert.ok(err.message.includes(ref), 'must echo the caller\'s own input');
      assert.ok(err.message.includes(targetRecovery(ref)), err.message);
      return true;
    });
  });
}

test('parseEntryArgs refuses a raw JS number (45) as a pr_number target, same recovery line as its string form', () => {
  assert.throws(() => parseEntryArgs(45), (err) => {
    assert.ok(err.message.includes(KIND_LABEL.pr_number), err.message);
    assert.ok(err.message.includes(targetRecovery('45')), err.message);
    return true;
  });
});

// The case that forces raw-before-parsed classification order (spec 1b): '310' is valid
// JSON (the bare number 310), so without this ordering it would silently parse clean and
// fall through to a generic "args is not an object" with no recovery line.
test("parseEntryArgs('310') is classified as a PR number, not treated as a malformed waist", () => {
  assert.deepEqual(classifyReviewTarget('310'), { kind: 'pr_number', number: '310', ref: '310' });
  assert.throws(() => parseEntryArgs('310'), (err) => {
    assert.ok(err.message.includes(KIND_LABEL.pr_number), err.message);
    assert.ok(err.message.includes(targetRecovery('310')), err.message);
    assert.ok(!/is not an object/.test(err.message), 'must not fall through to the generic shape message');
    return true;
  });
});

// --- parseEntryArgs: shape class (unclassifiable, non-object) -----------------------------
const SHAPE_CASES = [
  ['an unparseable string', 'not valid json {{{'],
  ['a string that JSON.parses to a non-target string', JSON.stringify('review my changes')],
  ['an out-of-range number (fails classifyReviewTarget, so falls to the generic shape class)', 1e99],
  ['a boolean', true],
  ['an array', [1, 2]],
];

for (const [desc, raw] of SHAPE_CASES) {
  test(`parseEntryArgs refuses ${desc} with the generic (non-echoing) recovery line`, () => {
    assert.throws(() => parseEntryArgs(raw), (err) => {
      assert.ok(err.message.includes('code-gauntlet skill'), err.message);
      assert.ok(err.message.includes(SHAPE_RECOVERY), err.message);
      return true;
    });
  });
}

test('parseEntryArgs echoes an unparseable string input verbatim in the refusal message', () => {
  assert.throws(() => parseEntryArgs('not valid json {{{'), /not valid json \{\{\{/);
});

// The single-physical-line invariant is only as strong as the echo that feeds it. A raw
// string carrying its OWN newlines — a truncated pretty-printed waist is the realistic
// shape, e.g. a copy-paste that lost its tail — used to embed them verbatim in the message
// through both seams. The echo is escaped, not merely truncated.
test('an echoed raw string with embedded newlines/tabs stays a single physical line', async () => {
  const truncatedWaist = '{\n  "argsVersion": 1,\n\t"mode": "headless",\n  "repoRoot": "/repo';
  let thrown;
  try { parseEntryArgs(truncatedWaist); } catch (e) { thrown = e.message; }
  assert.ok(thrown, 'a truncated waist must be refused');
  assert.ok(!thrown.includes('\n'), `message must be one physical line: ${JSON.stringify(thrown)}`);
  assert.ok(!thrown.includes('\t'), `message must not embed a raw tab: ${JSON.stringify(thrown)}`);
  const out = await runWith(throwingCtx(), truncatedWaist);
  assert.ok(!out.error.includes('\n'), 'runWith error must be one physical line too');
});

// Companion to the double-quote fallback below: a classified ref ending in an ODD number of
// backslashes would put a bare `\` immediately before the closing quote of
// args="<ref>", escaping it in every double-quoted-string grammar a consumer might parse
// this line with. The URL patterns' optional path/query/fragment groups permit it, so the
// corruption guard covers the backslash as well as the quote.
test('a classified ref containing a backslash falls back to the generic recovery line', () => {
  const raw = 'https://github.com/o/r/pull/45/x\\';
  assert.ok(classifyReviewTarget(raw), 'fixture must actually classify, or this test proves nothing');
  assert.throws(() => parseEntryArgs(raw), (err) => {
    assert.ok(!err.message.includes(`args="${raw}"`), `must not emit a corrupted copy-paste line: ${err.message}`);
    assert.ok(err.message.includes(SHAPE_RECOVERY), err.message);
    return true;
  });
});

// Design decision pinned by this test (spec 1c: "a ref containing a `\"` would corrupt
// args="<ref>" — handle it — either escape it or fall back to message C. Decide, then pin
// the decision with a test."): a classified ref that itself contains a double quote falls
// back to the generic (non-echoing) message rather than emitting a corrupted or
// escaped-and-therefore-not-literally-copy-pasteable Skill(...) line. `owner`/`repo` in the
// repo_ref/URL patterns are `[^\s/]+`, which does not exclude `"`, so this is reachable.
test('a classified ref containing a double quote falls back to the generic recovery line (no corrupted copy-paste line)', () => {
  const raw = 'o"r/repo#45'; // matches repo_ref: owner=o"r, repo=repo, n=45
  assert.ok(classifyReviewTarget(raw), 'fixture must actually classify, or this test proves nothing');
  assert.throws(() => parseEntryArgs(raw), (err) => {
    assert.ok(!err.message.includes(`args="${raw}"`), `must not emit a corrupted copy-paste line: ${err.message}`);
    assert.ok(err.message.includes(SHAPE_RECOVERY), err.message);
    return true;
  });
});

// --- double-encoding: the raw value fails to classify, but its PARSED value does ----------
// A caller's args can arrive JSON-encoded a second time by the harness: the raw value is
// e.g. '"https://github.com/o/r/pull/45"' (quotes and all), which no review-reference
// pattern matches, while the parsed value is the bare string that matches every one.
const DOUBLE_ENCODED_CASES = [
  ['a GitHub PR URL', 'https://github.com/o/r/pull/45'],
  ['a PR shorthand with a space', 'PR 310'],
  ['a bare PR number', '310'],
];

for (const [desc, bare] of DOUBLE_ENCODED_CASES) {
  test(`double-encoding: ${desc} produces the identical recovery message bare and JSON-double-encoded`, () => {
    const doubled = JSON.stringify(bare);
    assert.equal(classifyReviewTarget(doubled), null, 'the RAW double-encoded string (quotes included) must not classify directly');
    let bareMsg;
    let doubledMsg;
    try { parseEntryArgs(bare); } catch (e) { bareMsg = e.message; }
    try { parseEntryArgs(doubled); } catch (e) { doubledMsg = e.message; }
    assert.ok(bareMsg, `bare form ${JSON.stringify(bare)} must be refused`);
    assert.ok(doubledMsg, `double-encoded form ${JSON.stringify(doubled)} must be refused`);
    assert.ok(bareMsg.includes(targetRecovery(bare)), bareMsg);
    assert.equal(doubledMsg, bareMsg, 'bare and double-encoded forms must produce the byte-identical message');
  });
}

// --- parseEntryArgs: pass-through (unchanged PARSE ONLY contract) -------------------------

test('parseEntryArgs passes a well-formed waist object through unchanged', () => {
  assert.deepEqual(parseEntryArgs(good), good);
});

test('parseEntryArgs parses a well-formed waist JSON string into the equivalent object', () => {
  assert.deepEqual(parseEntryArgs(JSON.stringify(good)), good);
});

test('parseEntryArgs does not strip a stamped reviewConfig:null / persist:null (parse-only contract)', () => {
  const withNulls = { ...good, reviewConfig: null, persist: null };
  assert.deepEqual(parseEntryArgs(withNulls), withNulls);
  assert.deepEqual(parseEntryArgs(JSON.stringify(withNulls)), withNulls);
});

// A DOUBLE-ENCODED valid waist must still be accepted. Before the bounded unwrap it was
// hard-rejected, which broke issue #27's own binding constraint ("no valid argsVersion:1
// waist object may be newly rejected"). It used to work only by accident: the entry parsed
// once and runWith's normalizeArgsReport parsed again, so the two hops happened to unwrap
// two levels between them. Refusing at the entry removed the second hop and with it the
// recovery. The unwrap is now explicit, in one place, and bounded — and it is the same
// harness quirk the double-encoded *scalar* cases above already anticipate, so covering
// scalars but not the (much longer) serialized waist was the gap.
test('a double-JSON-encoded valid waist is still accepted and fully unwrapped', () => {
  const doubled = JSON.stringify(JSON.stringify(good));
  assert.deepEqual(parseEntryArgs(doubled), good);
  // Triple, too — the entry returns the fully unwrapped object rather than relying on a
  // downstream second parse, so depth is no longer coupled to how many hops follow.
  assert.deepEqual(parseEntryArgs(JSON.stringify(doubled)), good);
});

// MAX_JSON_UNWRAP = 4: pin the exact accept/refuse boundary. The suite already covers 1–3
// layers accepted and a 7-layer refusal; these two pin "4 accepted, 5 refused".
function wrapJson(value, layers) {
  let raw = value;
  for (let i = 0; i < layers; i++) raw = JSON.stringify(raw);
  return raw;
}

test('a waist JSON-encoded exactly MAX_JSON_UNWRAP (4) times is accepted', () => {
  assert.deepEqual(parseEntryArgs(wrapJson(good, 4)), good);
});

test('a waist JSON-encoded one past MAX_JSON_UNWRAP (5) is refused', () => {
  assert.throws(() => parseEntryArgs(wrapJson(good, 5)), /code-gauntlet skill/);
});

// Both seams must unwrap to the SAME depth. runWith accepting a double-encoded waist and
// then re-normalizing from the raw string would peel only one layer and hand validateArgs a
// string — an accept-then-fail cascade worse than either a clean accept or a clean refusal.
test('runWith accepts a double-encoded waist and normalizes from the unwrapped object', async () => {
  const doubled = JSON.stringify(JSON.stringify({ ...good, mode: 'bogus' }));
  const out = await runWith(throwingCtx(), doubled);
  assert.equal(out.ok, false, 'the bogus mode must still be caught');
  assert.match(out.error, /invalid mode: bogus/, `must reach the real field check, not "not an object": ${out.error}`);
  assert.ok(!/is not an object/.test(out.error), out.error);
});

test('a waist encoded past the unwrap bound is refused, not silently half-unwrapped', () => {
  // Well past the bound (7 layers) — still a refusal, never a half-unwrapped accept.
  assert.throws(() => parseEntryArgs(wrapJson(good, 7)), /code-gauntlet skill/);
});

test('parseEntryArgs is inert on every waist fixture already used by the suite (good + validArgs())', () => {
  assert.doesNotThrow(() => parseEntryArgs(good));
  assert.doesNotThrow(() => parseEntryArgs(validArgs()));
  assert.doesNotThrow(() => parseEntryArgs(JSON.stringify(validArgs())));
});

// --- runWith: the second (defensive) seam --------------------------------------------------
// A ctx whose agent()/parallel() throw if called — in the idiom used elsewhere in
// workflows/test/ (pipeline_run.test.js's bare reportStage/writeArtifacts isolation ctx) —
// so any test using it proves the refusal happens before ANY dispatch is attempted.
function throwingCtx() {
  const calls = [];
  return {
    calls,
    agent: async () => { calls.push('agent'); throw new Error('ctx.agent must not be called before the entry refusal check'); },
    parallel: async () => { calls.push('parallel'); throw new Error('ctx.parallel must not be called before the entry refusal check'); },
  };
}

// Regression for the empirically-confirmed escaped SyntaxError (spec R4 verification):
// before this fix, runWith(undefined, 'PR 310') REJECTED with an uncaught
// `SyntaxError: Unexpected token 'P', "PR 310" is not valid JSON`, because
// normalizeArgsReport's JSON.parse sat OUTSIDE runWith's own try/catch. runWith's doc
// comment already promises it "NEVER lets a throw escape" — these three
// literal repro inputs from the spec's own verification section must all RESOLVE.
test('runWith never rejects on a malformed rawArgs — resolves to the refusal envelope', async () => {
  for (const rawArgs of ['PR 310', undefined, [1, 2]]) {
    await assert.doesNotReject(
      () => runWith(undefined, rawArgs),
      `runWith(undefined, ${JSON.stringify(rawArgs)}) must resolve, not reject`,
    );
  }
});

test('runWith refuses a review-target/absent/shape rawArgs BEFORE dispatching anything', async () => {
  const cases = ['PR 310', undefined, [1, 2], null, 45, 'https://github.com/o/r/pull/45'];
  for (const rawArgs of cases) {
    const ctx = throwingCtx();
    const out = await runWith(ctx, rawArgs);
    assert.equal(out.ok, false, `rawArgs=${JSON.stringify(rawArgs)} must be refused`);
    assert.deepEqual(ctx.calls, [], `no agent()/parallel() call for rawArgs=${JSON.stringify(rawArgs)}`);
  }
});

// The anti-drift test — this IS the point of the "two seams, one message" design, so it is
// pinned explicitly rather than left implicit in the two arms' individual tests.
test('one message, two signals: parseEntryArgs\'s thrown message and runWith\'s returned error are identical', async () => {
  const cases = [undefined, null, 'PR 310', '#310', 'https://github.com/o/r/pull/45', [1, 2], true, 1e99];
  for (const rawArgs of cases) {
    let thrown;
    try { parseEntryArgs(rawArgs); } catch (e) { thrown = e.message; }
    assert.ok(thrown, `parseEntryArgs must throw for ${JSON.stringify(rawArgs)}`);
    const out = await runWith(throwingCtx(), rawArgs);
    assert.equal(out.error, thrown, `runWith and parseEntryArgs must agree for rawArgs=${JSON.stringify(rawArgs)}`);
  }
});

// A plain object is accepted by the entry ON PURPOSE — a near-miss waist earns
// validateArgs's field-by-field list, better diagnostics than a flat "not a waist". But
// `Workflow(scriptPath, args={})` is as plausible a naive naked call as a bare PR number,
// and that list alone never says where the fields come from. Both halves must be present.
test('a plain non-waist object keeps the field-level cascade AND gains the skill recovery line', async () => {
  for (const rawArgs of [{}, { foo: 1 }, { ...good, argsVersion: undefined }]) {
    const ctx = throwingCtx();
    const out = await runWith(ctx, rawArgs);
    assert.equal(out.ok, false);
    assert.equal(out.failingPhase, 'args');
    assert.match(out.error, /missing required field|argsVersion/, 'the field-level cascade must survive');
    assert.ok(out.error.includes(SKILL_RECOVERY_LINE), `must name the skill: ${out.error}`);
    assert.ok(out.error.includes('Skill("code-gauntlet:code-gauntlet"'), out.error);
    assert.deepEqual(ctx.calls, [], 'nothing dispatched');
  }
});

test('runWith refusal envelope matches the validateArgs-reject envelope shape exactly (field for field)', async () => {
  // Both construction sites go through makeArgsRejectEnvelope — compare them to each
  // other (and to the factory), not to a hardcoded key list that can drift from one site.
  const entryRefuse = await runWith(throwingCtx(), undefined);
  const validateRefuse = await runWith(throwingCtx(), {});
  assert.equal(entryRefuse.ok, false);
  assert.equal(validateRefuse.ok, false);
  assert.deepEqual(
    Object.keys(entryRefuse).sort(),
    Object.keys(validateRefuse).sort(),
    'entry refusal and validateArgs-reject must share the same keys',
  );
  const factoryShape = makeArgsRejectEnvelope('x', ['x']);
  assert.deepEqual(
    Object.keys(entryRefuse).sort(),
    Object.keys(factoryShape).sort(),
    'both sites must match makeArgsRejectEnvelope',
  );
  assert.equal(typeof entryRefuse.error, 'string');
  assert.equal(entryRefuse.phaseReached, 'args');
  assert.equal(entryRefuse.failingPhase, 'args');
  assert.deepEqual(entryRefuse.artifactPaths, {});
  assert.deepEqual(entryRefuse.stats, {});
  assert.deepEqual(entryRefuse.gaps, [entryRefuse.error]);
  // entryArgs itself returns the factory envelope (not a hand-built twin).
  const viaEntryArgs = entryArgs(undefined);
  assert.equal(viaEntryArgs.ok, false);
  assert.deepEqual(viaEntryArgs.envelope, makeArgsRejectEnvelope(viaEntryArgs.envelope.error, [viaEntryArgs.envelope.error]));
});

// Every refusal message must stay a single physical line: a literal newline would corrupt
// the copy-paste Skill(...) line if a caller pastes the message verbatim into a shell or a
// chat box that treats newlines as submit.
test('every refusal message (parseEntryArgs throw and runWith error) is a single physical line', async () => {
  const cases = [
    undefined, null, 'PR 310', '#310', '!89', 'MR#7', 'https://github.com/o/r/pull/45',
    'https://gitlab.com/g/p/-/merge_requests/89', 'liatrio-forge/pet-clinic#45', '45',
    [1, 2], true, 1e99, 'not valid json {{{',
  ];
  for (const rawArgs of cases) {
    let thrown;
    try { parseEntryArgs(rawArgs); } catch (e) { thrown = e.message; }
    assert.ok(thrown, `expected a throw for ${JSON.stringify(rawArgs)}`);
    assert.ok(!thrown.includes('\n'), `parseEntryArgs message must be one physical line: ${thrown}`);
    const out = await runWith(throwingCtx(), rawArgs);
    assert.ok(!out.error.includes('\n'), `runWith error must be one physical line: ${out.error}`);
  }
});
