// args.js — the pipeline args waist: ARGS_VERSION, normalizeArgs, validateArgs.
// Single producer of the waist shape that bench and the pipeline entry both consume.
//
// policy shape: { tier, subagentModel } — tier records the resolved model_tier knob
// (its only valid value today is "optimized"; alternate modes are roadmap #17 V3.2).
//   - policy.subagentModel is passed to registry.js's resolvePolicy() as opts.subagentModelEnv.
//     This is a RENAME, not a passthrough — dispatch sites must map the field name.
//   - policy.tier is carried through the waist but is not read by resolvePolicy today.
export const ARGS_VERSION = 1;
// REQUIRED mirrors consumption: changedFiles/changedLines feed summarize bucketing and
// the agent-count guard; changedFilesPath is on-disk provenance the workflow never opens.
const REQUIRED = ['mode', 'repoRoot', 'outputDir', 'headShaShort', 'nonce', 'generatedAt', 'diffPath', 'changedFiles', 'changedLines', 'agentFlags', 'policy', 'limits'];

// The nonce is interpolated into the verify executor command argv (the verify stage
// derives one per slice as `${nonce}.${i}`), so it must be a single AST-safe,
// non-splitting token: word chars plus `.` `_` `-` only — no whitespace or shell
// metacharacters that could split argv or break AST-safe emission.
const NONCE_RE = /^[A-Za-z0-9._-]+$/;

// The optional Phase 8 delivery selector: { tier }. Absent is fine (the workflow defaults
// the tier to 'all' — post every challenge-survivor). A present tier must be a known value.
const DELIVERY_TIERS = ['all', 'main_only'];

export function normalizeArgs(raw) {
  return typeof raw === 'string' ? JSON.parse(raw) : raw;
}

// The bundle entry's args guard (live-run L1): a direct Workflow invocation with a raw
// string ("PR 310") used to die in JSON.parse with a native stack and no guidance. The
// entry cannot be unit-tested itself (its body ends in a top-level `return`), so the
// guard lives here and the entry calls it.
export function parseEntryArgs(raw) {
  try {
    return normalizeArgs(raw);
  } catch (e) {
    throw new Error(`args must be the assembled argsVersion:1 waist object — do not invoke this workflow directly; run the code-gauntlet skill (Phases 1-2 build the args). Got: ${String(raw).slice(0, 80)}`);
  }
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
  return { ok: errors.length === 0, errors };
}
