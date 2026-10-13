// Review-target classification and pipeline entry refusal envelopes.
import { REVIEW_TARGET_LABEL } from './registry.js';
import { isPlainObject } from './wire.js';

//
// Two live incidents motivate this section, both from naked `Workflow` invocations that
// skipped the code-gauntlet skill (which is the only producer of a valid waist):
//   L1 — a raw string ("PR 310") died in JSON.parse with a native stack and no guidance.
//   The caller passed a review-target reference (a PR URL, a bare number, "PR 310")
//         WHERE THE WAIST BELONGED. The old generic message didn't say that, so the fix
//         a human reaches for is "paste the PR number", which is exactly the wrong fix —
//         the workflow doesn't consume a PR reference at all; Phases 1-2 of the skill do,
//         and build the waist FROM it.
// classifyReviewTarget recognizes that shape so the refusal can name it and echo it back
// in the exact skill invocation to run instead.

// `<n>` everywhere: PR/MR numbers start at 1; a leading zero or an 8-digit run is not a
// PR reference (spec table, rule 4). No nested quantifiers anywhere below — every pattern
// is one quantified group followed by a literal or an anchor, so a pathological input
// (verified: 200 chars of "a/") returns in microseconds, not a ReDoS hang the workflow has
// no per-call timeout to recover from.

const REVIEW_TARGET_N = '[1-9]\\d{0,6}';

// host is `[\w.-]+` (not just known hosts) so a GitHub Enterprise or self-hosted GitLab
// domain still matches; owner/repo segments are `[^\s/]+` so they stop at the next `/`
// without needing a nested quantifier. Trailing `/…`, `?…`, `#…` cover GitHub's own
// `/files`, `/commits` suffixes and query/fragment noise without being required.
const PR_URL_RE = new RegExp(`^(?:https?://)?[\\w.-]+/[^\\s/]+/[^\\s/]+/pull/(${REVIEW_TARGET_N})(?:/[^\\s?#]*)?(?:\\?[^\\s#]*)?(?:#\\S*)?$`);

// The literal `/-/merge_requests/` segment is the same discriminator
// references/phase1-preflight.md:98 already documents in prose for telling a GitLab MR
// URL apart from anything else GitLab hosts at a path. The path before it is lazy (`\S+?`)
// so a subgroup ("group/subgroup/project") matches without knowing its depth up front.
const MR_URL_RE = new RegExp(`^(?:https?://)?[\\w.-]+/\\S+?/-/merge_requests/(${REVIEW_TARGET_N})(?:/[^\\s?#]*)?(?:\\?[^\\s#]*)?(?:#\\S*)?$`);

const REPO_REF_RE = new RegExp(`^[^\\s/]+/[^\\s/]+#(${REVIEW_TARGET_N})$`);

const PR_NUMBER_RE = new RegExp(`^(${REVIEW_TARGET_N})$`);

const PR_SHORTHAND_HASH_RE = new RegExp(`^[#!](${REVIEW_TARGET_N})$`);

// Longest alternatives first (`pull request`/`merge request` before `pull`) purely to
// avoid a redundant backtrack on the common phrase — anchoring makes the order
// non-load-bearing for correctness. Case-insensitive: "pr 310" is as real a caller typo
// as "PR 310".
const PR_SHORTHAND_WORD_RE = new RegExp(`^(?:pull request|merge request|PR|MR|pull)[ ]?[#!]?(${REVIEW_TARGET_N})$`, 'i');

// classifyReviewTarget(raw) -> { kind, number, ref } | null
// Cheap, deterministic, pure — no host globals (sandbox: language builtins and JSON only).
// `ref` is always the caller's OWN text, trimmed — never a reconstruction — because it is
// echoed verbatim into the recovery line so the caller recognizes their own input.
export function classifyReviewTarget(raw) {
  if (typeof raw === 'number') {
    // A JS number input classifies exactly like its string form would (waist fields never
    // arrive this way, but a caller passing `Workflow(..., 45)` is a plausible slip).
    if (Number.isSafeInteger(raw) && raw >= 1 && raw <= 9999999) {
      return { kind: 'pr_number', number: String(raw), ref: String(raw) };
    }
    return null;
  }
  if (typeof raw !== 'string') return null;
  const s = raw.trim();
  // A review reference is short; a truncated waist JSON is long. The 200-char bound keeps
  // the classifier cheap and its echo bounded — it is checked BEFORE any regex runs.
  if (s === '' || s.length > 200) return null;
  let m;
  if ((m = PR_URL_RE.exec(s))) return { kind: 'pr_url', number: m[1], ref: s };
  if ((m = MR_URL_RE.exec(s))) return { kind: 'mr_url', number: m[1], ref: s };
  if ((m = REPO_REF_RE.exec(s))) return { kind: 'repo_ref', number: m[1], ref: s };
  if ((m = PR_NUMBER_RE.exec(s))) return { kind: 'pr_number', number: m[1], ref: s };
  if ((m = PR_SHORTHAND_HASH_RE.exec(s))) return { kind: 'pr_shorthand', number: m[1], ref: s };
  if ((m = PR_SHORTHAND_WORD_RE.exec(s))) return { kind: 'pr_shorthand', number: m[1], ref: s };
  return null;
}

// How many JSON layers to peel looking for the waist. The documented "session tool-call
// form" wraps it once; the harness quirk this file already guards for wraps it twice.
// Bounded because peeling is free work a caller could hand us without limit, and because an
// unbounded peel would accept a payload no downstream hop could have produced.
const MAX_JSON_UNWRAP = 4;

// unwrapWaist(raw) -> { waist, terminal }
//   waist    — the plain object the pipeline should run on, or null if there isn't one.
//   terminal — the fully-decoded value we stopped at (the unparseable string, the decoded
//              scalar, the array). What the refusal classifies and echoes.
//
// This exists as ONE explicit loop because the two-layer case used to work only by
// accident: parseEntryArgs parsed once and runWith's normalizeArgsReport parsed again, so
// two hops happened to peel two layers between them. Refusing at the entry removes the
// second hop, and with it that accidental recovery — a valid, double-encoded waist would
// have been hard-rejected, breaking the "no valid waist newly rejected"
// constraint. Peeling here, to completion, also decouples the accepted depth from how many
// parse hops happen to follow: parseEntryArgs returns the finished object either way.
function unwrapWaist(raw) {
  let value = raw;
  for (let depth = 0; depth <= MAX_JSON_UNWRAP; depth++) {
    if (isPlainObject(value)) return { waist: value, terminal: value };
    if (typeof value !== 'string' || depth === MAX_JSON_UNWRAP) break;
    let next;
    try { next = JSON.parse(value); } catch { break; }
    value = next;
  }
  return { waist: null, terminal: value };
}

// describeShape(raw) -> the `<desc>` half of message C (spec 1c) for a value that is
// neither an acceptable waist shape nor a classified review target. Truncated to 80 chars
// for a string — long enough to recognize, short enough that a stray multi-KB payload
// doesn't blow out the refusal message itself.
function describeShape(raw) {
  if (typeof raw === 'string') {
    const s = raw.length > 80 ? `${raw.slice(0, 80)}…` : raw;
    // JSON.stringify, not manual quoting: it is a TOTAL escape over every control
    // character, so the echo cannot break the single-physical-line invariant no matter
    // what the caller passed. Truncating alone did not — a truncated pretty-printed waist
    // carries its own newlines, and they landed raw in the message on both seams.
    return `a raw string: ${JSON.stringify(s)}`;
  }
  if (Array.isArray(raw)) return 'an array';
  if (typeof raw === 'boolean') return `a boolean: ${raw}`;
  if (typeof raw === 'number') return `a number: ${raw}`;
  return 'an unrecognized value';
}

// refusalFrom(raw, unwrapped) -> string | null. The wording and the accept/refuse decision,
// taking the ALREADY-unwrapped result so a caller that needs both the verdict and the waist
// (parseEntryArgs, runWith) peels the payload once rather than twice. Both seams reach this
// through `entryArgs` — `parseEntryArgs` throws the message, `runWith` returns it inside
// the envelope — so the two cannot say different things about the same input (pinned by
// entry_guard.test.js's "one message, two signals" test). `null` means the value may
// proceed to normalizeArgsReport/validateArgs.
function refusalFrom(raw, unwrapped) {
  if (raw === undefined || raw === null) {
    const got = raw === undefined ? 'undefined' : 'null';
    return `args never arrived: the code-gauntlet workflow was invoked with no args at all (got ${got}). `
      + 'This workflow builds nothing itself — Phases 1-2 of the code-gauntlet skill resolve and check '
      + 'out the target, write the diff and shared context, and assemble the argsVersion:1 waist object '
      + 'it consumes. Run the skill instead: Skill("code-gauntlet:code-gauntlet", args="<PR number or URL '
      + '— omit to auto-detect this branch\'s PR, else local changes>"). To resume an interrupted run, '
      + 're-invoke with the SAME args object plus resumeFromRunId.';
  }

  const { waist, terminal } = unwrapped;
  if (waist !== null) return null;

  // Classify the RAW value first, before any JSON.parse: '310' is valid JSON (the number
  // 310), so classifying only the decoded value would let a bare PR number parse clean and
  // fall through to the generic shape message with no recovery line at all (spec 1b).
  // Then the fully-decoded terminal value — the double-encoded case
  // ('"https://github.com/o/r/pull/45"', quotes and all), where no review-reference pattern
  // matches the raw quoted form but every one matches the decoded value. Echo whichever
  // value actually classified, so the recovery line carries a reference the caller can use.
  const target = classifyReviewTarget(raw) || classifyReviewTarget(terminal);
  // A classified ref containing a `"` or a `\` would corrupt the copy-paste `args="<ref>"`
  // line, and both are reachable: the repo_ref/URL owner-repo segments are `[^\s/]+` and
  // the URL patterns' optional path/query/fragment groups are `[^\s?#]*`/`\S*`, none of
  // which exclude either character. The quote closes the argument early; a ref ending in an
  // ODD number of backslashes leaves a bare `\` abutting the closing quote, escaping it in
  // every double-quoted-string grammar a consumer might re-parse this line with. Escaping
  // them here would make the line technically valid but no longer literally copy-pasteable,
  // which defeats the point — fall back to the generic message instead (spec 1c, pinned by
  // two tests). Checked as a set, not by parity: a lone `\` anywhere is already a smell.
  //
  // Both messages below lead with what WENT WRONG, not with what is required. The platform
  // reports this run as <status>completed</status> either way, so a session skimming the
  // notification has only the first clause to tell a refusal from a result — and "args must
  // be the assembled waist object…" reads as a spec statement until several words in.
  // "args was X, not Y" reads as a complaint immediately, matching the absent-class shape.
  if (target && !/["\\]/.test(target.ref)) {
    return `args was ${REVIEW_TARGET_LABEL[target.kind]} (${target.ref}), not the assembled argsVersion:1 `
      + 'waist object — do not invoke this workflow directly; run the code-gauntlet skill (Phases 1-2 '
      + `build the args). Run this instead: Skill("code-gauntlet:code-gauntlet", args="${target.ref}")`;
  }

  return `args was ${describeShape(raw)}, not the assembled argsVersion:1 waist object — do not invoke `
    + 'this workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this '
    + 'instead: Skill("code-gauntlet:code-gauntlet", args="<your PR number or URL>")';
}

// The recovery line for an args failure that has NO caller reference to echo — the
// validateArgs cascade in runWith (a plain object that is not a waist: `{}`, or a waist
// assembled by hand with fields missing). The entry's own refusals build the same call with
// the caller's reference in it; this is the referenceless form, so both paths end by naming
// the skill rather than only the defect.
//
// Why the cascade needs one at all: a plain object is accepted by the entry ON PURPOSE — a
// near-miss waist gets validateArgs's field-by-field list, which is far better diagnostics
// than "not a waist" — but that list says nothing about WHERE the fields come from, which
// is the inference this guard removes. `Workflow(scriptPath, args={})` is as
// plausible a naive call as a bare PR number and used to land here with no way out.
export const SKILL_RECOVERY_LINE = 'The code-gauntlet skill assembles this object in Phases 1-2 — '
  + 'run it instead of invoking the workflow directly: '
  + 'Skill("code-gauntlet:code-gauntlet", args="<PR number or URL>")';

// makeArgsRejectEnvelope(message, gaps) -> the ONE args-failure envelope shape.
// Used by entryArgs (entry refusal) and by runWith's validateArgs-reject arm, so a
// caller downstream sees one shape for every args failure — not two hand-built literals
// that a comment claims match. `gaps` is the full gap list for that path (entry refusal:
// `[message]`; validateArgs reject: `[...nullArgGaps, ...check.errors]`).
export function makeArgsRejectEnvelope(message, gaps) {
  return {
    ok: false,
    error: message,
    phaseReached: 'args',
    failingPhase: 'args',
    artifactPaths: {},
    stats: {},
    gaps,
  };
}

// entryArgs(raw) -> { ok:true, waist } | { ok:false, envelope }
// The one seam-agnostic entry check: unwraps ONCE, decides, and hands back either the waist
// the pipeline should run on or the refusal already wrapped in an envelope. Both callers use
// it, so neither can drift on how deeply it unwraps — runWith accepting a double-encoded
// waist and then re-normalizing from the raw string would peel one layer fewer and hand
// validateArgs a string, an accept-then-fail cascade worse than either clean outcome.
export function entryArgs(raw) {
  const unwrapped = unwrapWaist(raw);
  const message = refusalFrom(raw, unwrapped);
  if (message === null) return { ok: true, waist: unwrapped.waist };
  // This refusal has nothing else to report, so the message IS the (sole) gap.
  return { ok: false, envelope: makeArgsRejectEnvelope(message, [message]) };
}

// The bundle entry's args guard: a direct Workflow invocation
// with a raw string ("PR 310") used to die in JSON.parse with a native stack and no
// guidance. The entry cannot be unit-tested itself (its body ends in a top-level
// `return`), so the guard lives here and the entry calls it.
//
// KEEPS THROWING, on purpose (spec R3, verified — do not "fix" this to a return): a
// returned `{ok:false, ...}` is reported to the naked-call caller as
// `<status>completed</status>`, identical to a successful run (recorded bench runs in this
// repo) — so the caller least likely to parse a
// return value (it branches on status) would read a refused review as a finished one. A
// throw is the only signal this platform renders as a visible failure. runWith's own seam
// is throw-free by contract and RETURNS the identical refusal, via the shared entryArgs,
// instead (run.js) — two signals, ONE refusalFrom wording, so the wording cannot drift
// between them.
//
// Refuses three classes now, not just an unparseable string: absent (undefined/null), a
// classified review target, and any other non-object shape. `parseEntryArgs(undefined)`
// no longer returns `undefined` silently (requirement 5) — that silence used to leave the
// naked-call caller, again the one least likely to check a return value, to fall through
// to a generic downstream validateArgs rejection with no recovery line at all.
//
// PARSE ONLY — deliberately does NOT strip stamped nulls. runWith re-normalizes anyway, and
// it is the only place that can attach the disclosure gaps to the returned envelope; if the
// entry stripped first, runWith would see an already-clean waist and the substitution would
// go unreported on exactly the live path that matters.
export function parseEntryArgs(raw) {
  const entry = entryArgs(raw);
  if (!entry.ok) throw new Error(entry.envelope.error);
  // The FULLY unwrapped object, not a fresh JSON.parse: entryArgs already peeled to it, and
  // re-parsing here would duplicate that work and re-open the depth coupling unwrapWaist
  // exists to close.
  return entry.waist;
}
