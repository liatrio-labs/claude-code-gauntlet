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

// The bundle entry's args guard (live-run L1): a direct Workflow invocation with a raw
// string ("PR 310") used to die in JSON.parse with a native stack and no guidance. The
// entry cannot be unit-tested itself (its body ends in a top-level `return`), so the
// guard lives here and the entry calls it.
//
// PARSE ONLY — deliberately does NOT strip stamped nulls. runWith re-normalizes anyway, and
// it is the only place that can attach the disclosure gaps to the returned envelope; if the
// entry stripped first, runWith would see an already-clean waist and the substitution would
// go unreported on exactly the live path that matters.
export function parseEntryArgs(raw) {
  try {
    return typeof raw === 'string' ? JSON.parse(raw) : raw;
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
