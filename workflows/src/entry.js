import { isPlainObject, deepFreeze } from './wire.js';

const REVIEW_TARGET_LABEL = deepFreeze({
  pr_url: 'a GitHub PR URL',
  mr_url: 'a GitLab MR URL',
  repo_ref: 'an owner/repo PR reference',
  pr_number: 'a bare PR/MR number',
  pr_shorthand: 'a PR/MR reference',
});

// The host has no per-call timeout, so review-reference patterns avoid nested quantifiers.

const REVIEW_TARGET_N = '[1-9]\\d{0,6}';

// Accept arbitrary hosts so enterprise GitHub and self-hosted GitLab references still classify.
const PR_URL_RE = new RegExp(`^(?:https?://)?[\\w.-]+/[^\\s/]+/[^\\s/]+/pull/(${REVIEW_TARGET_N})(?:/[^\\s?#]*)?(?:\\?[^\\s#]*)?(?:#\\S*)?$`);

// The lazy project path accepts GitLab subgroups without assuming their nesting depth.
const MR_URL_RE = new RegExp(`^(?:https?://)?[\\w.-]+/\\S+?/-/merge_requests/(${REVIEW_TARGET_N})(?:/[^\\s?#]*)?(?:\\?[^\\s#]*)?(?:#\\S*)?$`);

const REPO_REF_RE = new RegExp(`^[^\\s/]+/[^\\s/]+#(${REVIEW_TARGET_N})$`);

const PR_NUMBER_RE = new RegExp(`^(${REVIEW_TARGET_N})$`);

const PR_SHORTHAND_HASH_RE = new RegExp(`^[#!](${REVIEW_TARGET_N})$`);

const PR_SHORTHAND_WORD_RE = new RegExp(`^(?:pull request|merge request|PR|MR|pull)[ ]?[#!]?(${REVIEW_TARGET_N})$`, 'i');

// Preserve the caller's trimmed reference so the recovery command echoes recognizable input.
export function classifyReviewTarget(raw) {
  if (typeof raw === 'number') {
    if (Number.isSafeInteger(raw) && raw >= 1 && raw <= 9999999) {
      return { kind: 'pr_number', number: String(raw), ref: String(raw) };
    }
    return null;
  }
  if (typeof raw !== 'string') return null;
  const s = raw.trim();
  // Bound the input before regex matching to keep both classification work and refusal echoes small.
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

// Bound decoding work so callers cannot supply unlimited JSON wrapping layers.
const MAX_JSON_UNWRAP = 4;

// Finish decoding at entry so accepted depth does not depend on downstream parse hops.
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

// Bound the string echo so an oversized payload cannot dominate the refusal.
function describeShape(raw) {
  if (typeof raw === 'string') {
    const s = raw.length > 80 ? `${raw.slice(0, 80)}…` : raw;
    // JSON quoting escapes controls so the refusal stays on one physical line.
    return `a raw string: ${JSON.stringify(s)}`;
  }
  if (Array.isArray(raw)) return 'an array';
  if (typeof raw === 'boolean') return `a boolean: ${raw}`;
  if (typeof raw === 'number') return `a number: ${raw}`;
  return 'an unrecognized value';
}

// Both entry seams share the refusal wording and reuse the decoded result.
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

  // Try raw references first so the recovery command preserves the caller's spelling.
  // Then try the terminal value so encoded references also recover.
  const target = classifyReviewTarget(raw) || classifyReviewTarget(terminal);
  // Quotes and backslashes can break the copyable recovery argument.
  // Use the generic refusal rather than emitting a command the caller cannot safely paste.
  if (target && !/["\\]/.test(target.ref)) {
    return `args was ${REVIEW_TARGET_LABEL[target.kind]} (${target.ref}), not the assembled argsVersion:1 `
      + 'waist object — do not invoke this workflow directly; run the code-gauntlet skill (Phases 1-2 '
      + `build the args). Run this instead: Skill("code-gauntlet:code-gauntlet", args="${target.ref}")`;
  }

  return `args was ${describeShape(raw)}, not the assembled argsVersion:1 waist object — do not invoke `
    + 'this workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Run this '
    + 'instead: Skill("code-gauntlet:code-gauntlet", args="<your PR number or URL>")';
}

// Plain objects reach field validation for useful diagnostics, but missing fields do not explain
// where to obtain the args object. The recovery line supplies that next step.
export const SKILL_RECOVERY_LINE = 'The code-gauntlet skill assembles this object in Phases 1-2 — '
  + 'run it instead of invoking the workflow directly: '
  + 'Skill("code-gauntlet:code-gauntlet", args="<PR number or URL>")';

// Share one envelope shape so entry and field-validation failures agree for downstream callers.
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

// Return the decoded waist so callers cannot accept one shape and then validate a less-decoded value.
export function entryArgs(raw) {
  const unwrapped = unwrapWaist(raw);
  const message = refusalFrom(raw, unwrapped);
  if (message === null) return { ok: true, waist: unwrapped.waist };
  return { ok: false, envelope: makeArgsRejectEnvelope(message, [message]) };
}

// The host treats a returned refusal as completed; throwing makes direct entry failures visible.
// Keep null stripping in runWith, which can attach disclosure gaps to its envelope.
export function parseEntryArgs(raw) {
  const entry = entryArgs(raw);
  if (!entry.ok) throw new Error(entry.envelope.error);
  return entry.waist;
}
