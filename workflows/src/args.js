// args.js — the pipeline args waist: ARGS_VERSION, normalizeArgs, validateArgs.
// Single producer of the waist shape that bench and the pipeline entry both consume.
//
// policy shape: { tier, subagentModel } — tier records the resolved model_tier knob
// (its only valid value today is "optimized"; alternate modes are roadmap #17 V3.2).
//   - policy.subagentModel is passed to registry.js's resolvePolicy() as opts.subagentModelEnv.
//     This is a RENAME, not a passthrough — dispatch sites must map the field name.
//   - policy.tier is carried through the waist but is not read by resolvePolicy today.
export const ARGS_VERSION = 1;
// changedFiles/changedLines feed summarize bucketing and the agent-count guard, so they're
// REQUIRED because they're consumed. `mode` and `repoRoot` are NOT read anywhere in
// workflows/src (mode is only ever re-checked against its own enum below; repoRoot is unread
// entirely) — they're required as provenance/telemetry the skill always stamps, not because
// any stage consumes them. changedFilesPath is on-disk provenance the workflow never opens.
const REQUIRED = ['mode', 'repoRoot', 'outputDir', 'headShaShort', 'nonce', 'generatedAt', 'diffPath', 'changedFiles', 'changedLines', 'agentFlags', 'policy', 'limits'];

// The nonce is interpolated into the verify executor command argv (the verify stage
// derives one per slice as `${nonce}.${i}`), so it must be a single AST-safe,
// non-splitting token: word chars plus `.` `_` `-` only — no whitespace or shell
// metacharacters that could split argv or break AST-safe emission.
const NONCE_RE = /^[A-Za-z0-9._-]+$/;

// The optional Phase 8 delivery selector: { tier }. Absent is fine (the workflow defaults
// the tier to 'all' — post every challenge-survivor). A present tier must be a known value.
const DELIVERY_TIERS = ['all', 'main_only'];

// Issue #38 A1 (measured): a dispatch was rejected solely because reviewConfig arrived as a
// stamped `null` rather than absent — a wasted model round trip. These five top-level
// optional fields have no meaning as `null` (only "absent" or "a well-formed object/array"),
// so a literal null is equivalent to omission and gets dropped before validateArgs ever sees
// it. This allowlist is intentionally narrow: do NOT extend the null-tolerance treatment to
// any other field. In particular, limits.deliveryCap: null means "uncapped" and
// policy.subagentModel: null means "no override" — both are load-bearing DATA, not omitted-
// field stand-ins, and must survive this step untouched.
//
// `persist` is on the list for the same reason its siblings are (its null means "take the
// legacy by-value writer path", exactly what absence means); leaving it off was a hole that
// hard-rejected a whole run over a stamped null — the very cost this allowlist exists to
// remove.
//
// TOLERATED IS NOT SILENT. Dropping a stamped null removes a fail-loud guard: a mis-stamped
// `reviewConfig: null` would otherwise review under the Filter stage's config-absent
// defaults instead of the operator's REVIEW.md thresholds — a silent change to the DELIVERED
// findings, which issue #38 forbids. So every drop that ACTUALLY removed a guard is REPORTED
// (stripNullOptionalsReport -> nullToleranceRejectedKeys -> runWith -> an envelope gap via
// nullToleranceGap): degraded-but-disclosed, the contract this pipeline uses everywhere else.
// A drop that validateArgs would have accepted anyway (`checkpoints`) tolerated nothing and
// is deliberately NOT disclosed — see nullToleranceRejectedKeys.
const NULLABLE_TOP_LEVEL = ['reviewConfig', 'exclusionPatterns', 'delivery', 'checkpoints', 'persist'];

// What the operator actually loses when a stamped null is treated as absent, per key. Used
// only to word the disclosure gap — no control flow reads it.
const NULL_TOLERANCE_CONSEQUENCE = {
  reviewConfig: 'the review runs on the Filter stage built-in thresholds (non-security 55, security 70) with no ignore list, NOT your REVIEW.md configuration, so the delivered findings can differ',
  exclusionPatterns: 'no exclusion patterns are applied, so the delivered findings can differ',
  delivery: 'delivery falls back to tier "all" with no PR identity',
  'delivery.tier': 'delivery falls back to tier "all", so a narrowing intent is lost',
  'delivery.prIdentity': 'the post-review artifact is persisted as a bare findings array instead of the post_review-ready wrapper',
  checkpoints: 'no resume state is replayed — every phase re-runs from scratch',
  persist: 'the artifact-writer takes the legacy full by-value persist path',
};

// nullToleranceGap(key) -> the operator-actionable gap line for one dropped null. Names the
// field, states it was treated as ABSENT, states the consequence, and says to omit it.
// Pure string building — no host globals (the workflow sandbox has no console/process).
export function nullToleranceGap(key) {
  const why = NULL_TOLERANCE_CONSEQUENCE[key] || 'the run proceeds as if the field had not been supplied';
  return `null_arg: args.${key} arrived as a literal null and was treated as ABSENT — ${why}. Omit ${key} entirely (or stamp a well-formed value); do not stamp null.`;
}

// nullToleranceRejectedKeys(cleanArgs, dropped) -> the subset of `dropped` whose stamped null
// validateArgs would ACTUALLY have rejected — i.e. the keys where the tolerance changed the
// outcome and there is therefore something to disclose.
//
// `checkpoints` is why this exists (issue #38 F4-3): validateArgs has never carried a
// checkpoints shape check, so a stamped `checkpoints: null` was ALWAYS equivalent to absence.
// Nothing was tolerated and nothing was lost, so a gap there announced a degradation that
// never happened — and noise in the gap channel is corrosive precisely because gaps are how
// this pipeline stays honest about the degradations that DID happen.
//
// The subset is COMPUTED, not listed: one differential validateArgs call per dropped key (the
// stripped waist, vs the same waist with that null put back). A second hand-maintained list
// would drift the moment someone adds or removes a shape check. Pure — validateArgs is pure,
// and the probe objects are shallow copies, so the caller's waist is untouched.
export function nullToleranceRejectedKeys(cleanArgs, dropped) {
  if (!Array.isArray(dropped) || dropped.length === 0) return [];
  const baseline = validateArgs(cleanArgs).errors.length;
  return dropped.filter((key) => validateArgs(withNullAt(cleanArgs, key)).errors.length > baseline);
}

// The stripped waist with ONE dropped null put back, addressed by the same key spelling
// stripNullOptionalsReport reports (`delivery.tier` / `delivery.prIdentity` are the only
// nested forms today).
function withNullAt(args, key) {
  const base = (args && typeof args === 'object' && !Array.isArray(args)) ? args : {};
  const dot = key.indexOf('.');
  if (dot === -1) return { ...base, [key]: null };
  const parent = key.slice(0, dot);
  const child = key.slice(dot + 1);
  const parentValue = base[parent];
  const parentObject = (parentValue && typeof parentValue === 'object' && !Array.isArray(parentValue)) ? parentValue : {};
  return { ...base, [parent]: { ...parentObject, [child]: null } };
}

// stripNullOptionalsReport(args) -> { args, dropped }
// Drops a literal top-level null for the narrow NULLABLE_TOP_LEVEL allowlist (and, inside a
// present `delivery` object, a literal null for its `prIdentity`/`tier` sub-fields), and
// names every key it dropped so the caller can DISCLOSE the substitution.
// Non-mutating: the returned waist is a fresh shallow copy and a touched `delivery` is
// itself copied, so a caller holding the original object sees no change (its stamped nulls
// are still there). Non-object input (undefined, null, a string) passes through as-is with
// an empty `dropped`; there is nothing to strip.
export function stripNullOptionalsReport(args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) return { args, dropped: [] };
  const dropped = [];
  const out = { ...args };
  for (const k of NULLABLE_TOP_LEVEL) {
    if (out[k] === null) { delete out[k]; dropped.push(k); }
  }
  if (out.delivery && typeof out.delivery === 'object' && !Array.isArray(out.delivery)) {
    const delivery = { ...out.delivery };
    if (delivery.prIdentity === null) { delete delivery.prIdentity; dropped.push('delivery.prIdentity'); }
    if (delivery.tier === null) { delete delivery.tier; dropped.push('delivery.tier'); }
    out.delivery = delivery;
  }
  return { args: out, dropped };
}

export function normalizeArgsReport(raw) {
  const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw;
  return stripNullOptionalsReport(parsed);
}

export function normalizeArgs(raw) {
  return normalizeArgsReport(raw).args;
}

// --- Issue #27: classified entry refusals ------------------------------------------------
//
// Two live incidents motivate this section, both from naked `Workflow` invocations that
// skipped the code-gauntlet skill (which is the only producer of a valid waist):
//   L1 — a raw string ("PR 310") died in JSON.parse with a native stack and no guidance.
//   #27 — the caller passed a review-target reference (a PR URL, a bare number, "PR 310")
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

const REVIEW_TARGET_LABEL = {
  pr_url: 'a GitHub PR URL',
  mr_url: 'a GitLab MR URL',
  repo_ref: 'an owner/repo PR reference',
  pr_number: 'a bare PR/MR number',
  pr_shorthand: 'a PR/MR reference',
};

const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

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
// have been hard-rejected, breaking issue #27's own "no valid waist newly rejected"
// constraint. Peeling here, to completion, also decouples the accepted depth from how many
// parse hops happen to follow: parseEntryArgs returns the finished object either way.
function unwrapWaist(raw) {
  let value = raw;
  for (let depth = 0; depth <= MAX_JSON_UNWRAP; depth++) {
    if (isPlainObject(value)) return { waist: value, terminal: value };
    if (typeof value !== 'string' || depth === MAX_JSON_UNWRAP) break;
    let next;
    try { next = JSON.parse(value); } catch (e) { break; }
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
// is exactly the inference issue #27 exists to remove. `Workflow(scriptPath, args={})` is as
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

// The bundle entry's args guard (live-run L1 / issue #27): a direct Workflow invocation
// with a raw string ("PR 310") used to die in JSON.parse with a native stack and no
// guidance. The entry cannot be unit-tested itself (its body ends in a top-level
// `return`), so the guard lives here and the entry calls it.
//
// KEEPS THROWING, on purpose (spec R3, verified — do not "fix" this to a return): a
// returned `{ok:false, ...}` is reported to the naked-call caller as
// `<status>completed</status>`, identical to a successful run (recorded bench runs in this
// repo; anthropics/claude-code#66745, still open) — so the caller least likely to parse a
// return value (it branches on status) would read a refused review as a finished one. A
// throw is the only signal this platform renders as a visible failure. runWith's own seam
// is throw-free by contract and RETURNS the identical refusal, via the shared entryArgs,
// instead (stages.js) — two signals, ONE refusalFrom wording, so the wording cannot drift
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

export function validateArgs(args) {
  const errors = [];
  if (!args || typeof args !== 'object') return { ok: false, errors: ['args is not an object'] };
  if (args.argsVersion !== ARGS_VERSION) errors.push(`unsupported argsVersion ${args.argsVersion} (expected ${ARGS_VERSION})`);
  for (const k of REQUIRED) if (args[k] === undefined) errors.push(`missing required field: ${k}`);
  if (args.mode && !['interactive', 'headless'].includes(args.mode)) errors.push(`invalid mode: ${args.mode}`);
  // Only charset-check a present nonce (absence is already a REQUIRED error above).
  if (args.nonce !== undefined && (typeof args.nonce !== 'string' || !NONCE_RE.test(args.nonce))) {
    errors.push(`invalid nonce: must match ${NONCE_RE} (AST-safe, non-splitting — interpolated into the verify command argv per slice)`);
  }
  // Path-bearing waist fields (requirement 6, issue #27). repoRoot/outputDir/headShaShort/
  // diffPath interpolate into the shared-context path
  // (`${outputDir}/code-gauntlet-context-${headShaShort}.md`, stages.js:2398), which reaches
  // every discovery prompt, and headShaShort/diffPath also reach the verify executor's argv
  // (--head-sha, --diff-file) — the same argv-splitting hazard NONCE_RE already guards
  // against above. A present-but-garbage value would otherwise render a junk path into
  // every paid dispatch instead of failing here, at the waist. Absence is already a
  // REQUIRED-field error above; these fire only when the field is PRESENT. Nothing valid is
  // newly rejected: the skill stamps `git rev-parse --show-toplevel`, an absolute
  // {output_dir}, `git rev-parse --short=8 HEAD`, and a `{output_dir}/….patch` path — all
  // non-empty plain strings — and every existing fixture already uses those.
  const PATH_CONTROL_RE = /[\u0000-\u001F\u007F]/;
  for (const field of ['repoRoot', 'outputDir', 'headShaShort', 'diffPath']) {
    const v = args[field];
    if (v === undefined) continue;
    if (typeof v !== 'string' || v.trim() === '') {
      errors.push(`${field} must be a non-empty string when present`);
      continue;
    }
    if (PATH_CONTROL_RE.test(v)) {
      errors.push(`${field} must not contain a control character`);
      continue;
    }
    // headShaShort is interpolated bare into the verify executor's --head-sha argv
    // (verifyCommand joins tokens with spaces into a shell-run string). Whitespace alone
    // is not enough — `;`, `$`, backticks and friends would still reach the shell. A real
    // short SHA never needs those characters (unlike path fields / issue #75), so apply
    // the same AST-safe charset NONCE_RE already enforces above.
    if (field === 'headShaShort' && !NONCE_RE.test(v)) {
      errors.push(`headShaShort must match ${NONCE_RE} (AST-safe, non-splitting — interpolated into the verify command argv)`);
    }
  }
  // agentFlags is the scope-gating map consumed by agentActive (stages.js): OPT-OUT, so an
  // empty/absent-keyed map leaves every dimension on and only an explicit `false` disables a
  // gated dimension (e.g. light scope stamps { deep: false }). It is a REQUIRED waist field
  // (the skill always stamps it, {} for full scope), but shape-guard it so a malformed map
  // cannot silently gate dimensions: it must be a plain object and every value a boolean —
  // a non-boolean (a truthy "0"/"no" string, say) would slip past the strict `!== false`
  // check and read as ON, hiding an operator's intent to disable.
  if (args.agentFlags !== undefined) {
    if (args.agentFlags === null || typeof args.agentFlags !== 'object' || Array.isArray(args.agentFlags)) {
      errors.push('agentFlags must be an object of the form { <flag>: boolean } when present');
    } else {
      for (const [k, v] of Object.entries(args.agentFlags)) {
        if (typeof v !== 'boolean') errors.push(`invalid agentFlags.${k}: must be a boolean (got ${typeof v})`);
      }
    }
  }
  // Type-check the consumed by-value fields (absence is already a REQUIRED error above).
  if (args.changedFiles !== undefined && !Array.isArray(args.changedFiles))
    errors.push('changedFiles must be an array of repo-relative paths');
  if (args.changedLines !== undefined && typeof args.changedLines !== 'number')
    errors.push('changedLines must be a number');
  // Optional shared-context file size, measured by the skill immediately after it writes
  // {output_dir}/code-gauntlet-context-{head_sha_short}.md. The workflow has no disk, so
  // this is the only path for it. contextReadPlan (stages.js) turns the pair into the
  // exact Read calls the discovery/validate/summarize prompts enumerate — issue #48,
  // where an unmeasurable file left the agent to guess whether it had read all of it.
  //
  // OPTIONAL and independently so. Not for old callers: SKILL.md is the only producer of
  // this waist (bench invokes the skill rather than assembling args) and it ships with the
  // bundle, so there is no real skew window. It is optional because Phase 2 is
  // MODEL-EXECUTED and can skip the measurement step in a live run, and hard-failing there
  // would trade a partial read for a dead review. runWith emits a context_unmeasured gap so
  // the degradation is disclosed rather than silent. Absent contextChars just means the
  // line cap binds the chunk size alone. Both are shape-checked when present, because a
  // zero/negative/fractional value would produce a plan that either misses the file's
  // tail or names a nonsense offset — a silent under-read is exactly what this exists to
  // prevent, so a malformed value fails loud at the waist instead.
  // The MAX bounds are memory safety, not taste. contextReadPlan allocates one entry per
  // chunk; handed Number.MAX_SAFE_INTEGER it OOM-kills the node process with a V8 fatal
  // error — uncatchable, so runWith's top-level catch never runs, no gap is recorded and
  // nothing is dispatched. contextReadPlan carries its own chunk ceiling as the last line
  // of defence; this is the fail-loud one, at the waist, where a nonsense measurement is
  // still attributable to the producer that stamped it.
  const CONTEXT_SIZE_MAX = { contextLines: 5000000, contextChars: 500000000 };
  for (const k of ['contextLines', 'contextChars']) {
    if (args[k] === undefined) continue;
    if (!Number.isSafeInteger(args[k]) || args[k] <= 0) {
      errors.push(`${k} must be a positive safe integer (the measured size of the shared context file) when present`);
    } else if (args[k] > CONTEXT_SIZE_MAX[k]) {
      errors.push(`${k} is ${args[k]}, above the ${CONTEXT_SIZE_MAX[k]} ceiling — that is not a review context, it is a mis-measurement`);
    }
  }
  if (args.contextChars !== undefined && args.contextLines === undefined) {
    errors.push('contextChars requires contextLines — chars alone cannot size a line-offset read plan');
  }
  // Optional reviewConfig (the parsed REVIEW.md shape, see parseReviewMd in
  // filterFindings.js). Its `ignore` list feeds escapeRegExp in the Filter stage, which
  // assumes flat strings — a session that assembles entries as {pattern, reason} objects
  // crashes there AFTER five paid stages (observed live, PR-310 run). Same
  // present-then-shape-checked pattern as `delivery`: absent is fine, malformed fails loud
  // at the waist before anything is dispatched.
  if (args.reviewConfig !== undefined) {
    if (args.reviewConfig === null || typeof args.reviewConfig !== 'object' || Array.isArray(args.reviewConfig)) {
      errors.push('reviewConfig must be an object (the parseReviewMd output shape) when present');
    } else if (args.reviewConfig.ignore !== undefined) {
      if (!Array.isArray(args.reviewConfig.ignore)) {
        errors.push('reviewConfig.ignore must be an array of flat pattern strings');
      } else {
        for (let i = 0; i < args.reviewConfig.ignore.length; i++) {
          if (typeof args.reviewConfig.ignore[i] !== 'string') {
            errors.push(`reviewConfig.ignore[${i}] must be a flat pattern string (got ${typeof args.reviewConfig.ignore[i]}) — parseReviewMd emits strings, never objects`);
          }
        }
      }
    }
  }
  // Optional exclusionPatterns (the parsed exclusion-pattern list threaded alongside
  // reviewConfig). It feeds the same escapeRegExp path as reviewConfig.ignore — both are
  // concatenated in applyFilterPipeline (filterFindings.js) before the Filter stage builds
  // its regexes — so it is exposed to the same crash class (same live-run L2) and gets the
  // identical present-then-shape-checked treatment: absent is fine, malformed fails loud.
  if (args.exclusionPatterns !== undefined) {
    if (!Array.isArray(args.exclusionPatterns)) {
      errors.push('exclusionPatterns must be an array of flat pattern strings');
    } else {
      for (let i = 0; i < args.exclusionPatterns.length; i++) {
        if (typeof args.exclusionPatterns[i] !== 'string') {
          errors.push(`exclusionPatterns[${i}] must be a flat pattern string (got ${typeof args.exclusionPatterns[i]})`);
        }
      }
    }
  }
  // Optional delivery selector. Absence is fine; when present it must be an object, and a
  // present tier must be a known value — an unknown tier would otherwise fall through to the
  // 'all' default in selectDelivery, silently ignoring an operator's narrowing intent.
  if (args.delivery !== undefined) {
    if (args.delivery === null || typeof args.delivery !== 'object' || Array.isArray(args.delivery)) {
      errors.push('delivery must be an object of the form { tier } when present');
    } else {
      if (args.delivery.tier !== undefined && !DELIVERY_TIERS.includes(args.delivery.tier)) {
        errors.push(`invalid delivery.tier: ${args.delivery.tier} (expected one of ${DELIVERY_TIERS.join(', ')})`);
      }
      // Optional PR identity (live-run L3): when present, the artifact-writer persists the
      // post_review-ready wrapper { owner, repo, pr_number, sha, review_body, findings }
      // instead of the bare findings array — Phase 8 consumes it without hand-assembly.
      // ABSENT for local-diff reviews (the waist stays target-agnostic).
      const id = args.delivery.prIdentity;
      if (id !== undefined) {
        if (id === null || typeof id !== 'object' || Array.isArray(id)) {
          errors.push('delivery.prIdentity must be an object { owner, repo, pr_number, sha_full } when present');
        } else {
          if (typeof id.owner !== 'string' || !id.owner) errors.push('delivery.prIdentity.owner must be a non-empty string');
          if (typeof id.repo !== 'string' || !id.repo) errors.push('delivery.prIdentity.repo must be a non-empty string');
          if (typeof id.pr_number !== 'number') errors.push('delivery.prIdentity.pr_number must be a number');
          if (typeof id.sha_full !== 'string' || !id.sha_full) errors.push('delivery.prIdentity.sha_full must be a non-empty string');
        }
      }
    }
  }
  // Optional persist waist (issue #38, D3.4): { assembleScriptPath }. When present, the
  // artifact-writer persists only the UNIQUE content (findings.json, report.md, the
  // persist plan) and the executor runs that script to DERIVE the post-review and
  // checkpoint artifacts on disk. When ABSENT, writeArtifacts takes the legacy full
  // by-value path and records no gap — a clean, documented degradation for older callers
  // (bench included). Same present-then-shape-checked pattern as `delivery`: a malformed
  // object fails loud at the waist rather than mid-persist, after every paid stage.
  // (A literal `persist: null` never reaches here — it is on NULLABLE_TOP_LEVEL and is
  // dropped, and disclosed, by stripNullOptionalsReport. The `=== null` arm below is the
  // guard for a caller that skipped normalization entirely.)
  if (args.persist !== undefined) {
    if (args.persist === null || typeof args.persist !== 'object' || Array.isArray(args.persist)) {
      errors.push('persist must be an object of the form { assembleScriptPath } when present');
    } else if (args.persist.assembleScriptPath !== undefined
      && (typeof args.persist.assembleScriptPath !== 'string' || !args.persist.assembleScriptPath)) {
      errors.push('persist.assembleScriptPath must be a non-empty string path to scripts/assemble_artifacts.py');
    }
  }
  return { ok: errors.length === 0, errors };
}
