import { RETURN_CHAR_BUDGET } from './wire.js';
import { applyInjectedProseStrip, applyReplayInjectionScan, normalizeFieldNames } from './filterFindings.js';

// readCheckpoints(ctx, args) -> phase-keyed resume map ({ phase: priorOutput }).
// Injected through the args waist on a rerun (a phase whose output is present is
// skipped, not re-dispatched). The platform's own resume machinery caches
// agent-level work; this is the coarser phase-level skip. Accepts EITHER a bare
// { phase: output } map OR the persisted checkpoint artifact's { phases, completed,
// phaseReached } wrapper (unwrapping .phases) so the artifact the pipeline itself
// writes can be fed straight back. Falls back to a ctx-borne map (test seam), then {}.
//
// GARBAGE IS UNIFORM (round-2 fix): a non-plain-object top-level value (null, an array,
// a primitive) is garbage, exactly like the already-pinned `checkpoints: 'garbage'`
// case -- and so is a non-plain-object `.phases`. Before this fix `typeof [] === 'object'`
// let `{checkpoints: []}` and `{checkpoints: {phases: []}}` both slip past this unwrap
// as a truthy "resolved map", which the gate then sanitized to `{}` (no violations) --
// but runWith indexes THAT ARRAY by phase name, and `[]['filter'] ===
// Array.prototype.filter`, so runPhase('filter') replayed a FUNCTION as the phase's
// output instead of refusing. Top-level-garbage-is-inert is the established decision
// (falls through to the {} default below) -- the fix is only that arrays now count
// as garbage too, at both unwrap points.
// unwrapCheckpointMap(cp) -> the resolved phase-map for a checkpoints value, or `null` if
// `cp` is not a usable shape (a non-plain-object top level, or a plain top level whose
// `.phases` is present but not itself a plain object). Module-level, NOT named `unwrap`
// (build.js flattens every src file's top-level scope into one bundle and
// detectTopLevelCollisions hard-fails on a duplicate top-level name) so readCheckpoints
// AND the discard-disclosure predicate below share one definition of "usable
// checkpoints shape" instead of readCheckpoints's old function-local copy silently
// drifting from a second one.
function unwrapCheckpointMap(cp) {
  if (!isPlainCheckpointObject(cp)) return null;
  if (cp.phases === undefined) return cp;
  return isPlainCheckpointObject(cp.phases) ? cp.phases : null;
}

export function readCheckpoints(ctx, args) {
  const A = args || {};
  return unwrapCheckpointMap(A.checkpoints) || (ctx && unwrapCheckpointMap(ctx.checkpoints)) || {};
}

// checkpointDiscardGap(topLevelCheckpoints) -> a 0-or-1-element `checkpoint-discarded:`
// gaps array. readCheckpoints's fallback chain
// silently treats an unusable `args.checkpoints` exactly like an absent one -- correct
// behavior (a malformed resume input must not abort a review that never needed to
// resume), but previously undisclosed: an operator who stamped a real (if malformed)
// checkpoints value got a fresh full-pipeline run with no signal that their resume
// attempt was discarded rather than honored.
//
// `checkpoints: undefined` (never stamped, OR a stamped `null` -- normalizeArgsReport
// already dropped that to absent on the NULLABLE_TOP_LEVEL allowlist, upstream of this
// call) is deliberately NOT a discard: nothing was tolerated, nothing was lost, exactly the
// same "previously valid and silent" reasoning F4-3 pins for null_arg gaps
// (pipeline_run.test.js). A non-plain-object top level, or a plain top level with a
// non-plain-object `.phases`, is what `unwrapCheckpointMap` REJECTS outright (`null`) --
// WAS a real value the operator supplied, reduced to a fresh run with no resume, so it
// earns the disclosure below.
//
// Names the failing POSITION (`checkpoints` at the top level, or `checkpoints.phases` for a
// malformed wrapper) and reads describeCheckpointShape AT THAT POSITION: describing the
// wrapper object itself for a `{phases: 'x'}`-class value would print the useless
// "(got object)" instead of naming what is actually wrong (the `.phases` value).
//
// unwrapCheckpointMap REJECTING isn't the only no-resume outcome an operator
// gets zero disclosure for. It can also SUCCEED to something inert:
//   Arm A (zero-recognized non-empty map) -- the classic case is a fed-back return's
//   `checkpoints` field re-supplied as the next run's `checkpoints` argument, which
//   SKILL.md documents as a real return and headless auto-resume reads
//   `return.checkpoints` directly. `{completed, truncated: true}` IS buildResumeCheckpoints'
//   own FAILURE-path return, once the in-memory phase map would exceed the return budget.
//   A bare `{completed}` (no truncated, no phases) is NOT that helper's output at all --
//   buildResumeCheckpoints only ever returns `{phases, completed}` or `{completed,
//   truncated: true}`; bare `{completed}` is the ok:true SUCCESS return's own compact form
//   (built once the writer has persisted -- see the `checkpoints: writeOut.partial ? ... :
//   { completed }` success-path assignment below), naming only the phases that ran. Either
//   way its keys ('completed', 'truncated') are not phase names, so the direct-return
//   unwrap branch hands it straight through as a "resolved map" that recognizes nothing --
//   every phase re-runs, silently. Also catches plain garbage like `{foo: 1}`.
//   Arm B (empty-.phases masking) -- `{phases: {}, challenge: {...}}`: the unwrap reads
//   ONLY `.phases` (empty), so the real `challenge` output sitting right next to it at the
//   wrapper's top level is silently ignored. Deliberately narrow to the wholly-empty
//   `.phases` case -- a NON-empty `.phases` resuming real work (`{phases: {challenge:
//   ...}, summarize: {...}}`) never reaches this arm, even though a sibling key is still
//   being ignored: partial masking is not this arm's problem to catch (see the comment at
//   the arm itself), because a legitimate producer for that shape does not exist and the
//   phase(s) that DO resume are real, not silently dropped.
// Both arms require the resolved/`.phases` map to actually be EMPTY of recognized content
// (checked via CHECKPOINT_PHASE_SHAPE_TABLE's key set -- safe to derive from the table here
// because checkpoint_shape_gate.test.js's ALL_PHASES container-test loop independently pins
// the table's completeness over all eight phases), so `{}` and `{phases: {}}` alone --
// legitimately empty, nothing was ever supplied to lose -- stay silent, and so does any map
// carrying at least one recognized phase key (that key resumes normally; the disclosure is only about
// a resume attempt that resolves to NOTHING).
//
// All message text below is FIXED pipeline-authored vocabulary: it never interpolates
// a caller-supplied key name or byte. `args.checkpoints` is PR-author-reachable, and a key
// named after a G3 bench sentinel echoed into a gap would flip that gate.
export function checkpointDiscardGap(topLevelCheckpoints) {
  if (topLevelCheckpoints === undefined) return [];

  const resolved = unwrapCheckpointMap(topLevelCheckpoints);

  if (resolved === null) {
    const atTopLevel = !isPlainCheckpointObject(topLevelCheckpoints);
    const position = atTopLevel ? 'checkpoints' : 'checkpoints.phases';
    const offending = atTopLevel ? topLevelCheckpoints : topLevelCheckpoints.phases;
    return [`checkpoint-discarded: ${position} must be a plain object, got ${describeCheckpointShape(offending)} -- `
      + 'the supplied checkpoints argument was discarded entirely; nothing was resumed, exactly as '
      + 'if no checkpoints had been supplied.'];
  }

  const recognizedPhaseNames = Object.keys(CHECKPOINT_PHASE_SHAPE_TABLE);
  const resolvedKeys = Object.keys(resolved);
  // Deliberately KEY-PRESENCE (Object.keys + includes), not a truthiness/value check on
  // `resolved[k]` -- a recognized phase key can legitimately carry any JSON-safe value
  // (including one the shape gate below will still refuse), and `undefined` cannot arrive
  // through the JSON waist at all, so there is no "recognized key present but empty" case
  // this would need to distinguish.
  const hasRecognizedKey = resolvedKeys.some((k) => recognizedPhaseNames.includes(k));

  // Arm A: the unwrap succeeded to a NON-EMPTY map recognizing none of it.
  if (resolvedKeys.length > 0 && !hasRecognizedKey) {
    // Truthiness, not `'truncated' in resolved` -- a stamped `truncated: false` sits right
    // next to `completed` in a real buildResumeCheckpoints success-path return and must NOT
    // be misread as the truncated sub-case just because the key exists.
    if (resolved.truncated) {
      return ['checkpoint-discarded: the supplied checkpoints value is a truncated failure-path return '
        + '(phase names only, no phase outputs) -- nothing in it is resumable; re-run without the '
        + 'checkpoints argument to start the review fresh.'];
    }
    if (resolvedKeys.includes('completed')) {
      // Split on whether `completed` is actually non-empty: a real compact-return producer
      // (the ok:true success path's `{ completed }`) always names at least one phase, so a
      // non-empty array is the only shape that could have come from an on-disk-backed run --
      // the on-disk pointer sentence is correct there. An empty (or non-array) `completed`
      // instead matches makeCheckpointShapeRejectEnvelope's OWN refusal envelope
      // (`{ completed: [] }`, `artifactPaths: {}` -- nothing was ever persisted for it to
      // point at), so pointing at artifactPaths.checkpoints there would send the operator to
      // a path that was never written.
      if (Array.isArray(resolved.completed) && resolved.completed.length > 0) {
        return ['checkpoint-discarded: the supplied checkpoints value looks like a compact return rather '
          + 'than a checkpoint artifact -- it carries no phase outputs to resume from; the on-disk resume '
          + 'state from that run lives at artifactPaths.checkpoints.'];
      }
      return ['checkpoint-discarded: the supplied checkpoints value looks like a return envelope rather '
        + 'than a checkpoint artifact -- it carries no phase outputs to resume from; re-run without the '
        + 'checkpoints argument to start the review fresh.'];
    }
    return ['checkpoint-discarded: the supplied checkpoints value contains none of the eight recognized '
      + 'phase names (summarize, discover, merge, verify, validate, filter, challenge, report) -- '
      + 'nothing was resumed, exactly as if no checkpoints had been supplied.'];
  }

  // Arm B: the unwrap took the `.phases` branch to an EMPTY map while the wrapper itself
  // still carries a recognized phase name at its own top level, right next to `.phases`.
  // `resolvedKeys.length === 0` is load-bearing, not redundant with `tookPhasesBranch`: a
  // NON-empty `.phases` resuming real work, sitting next to an ignored top-level sibling
  // (`{phases: {challenge: ...}, summarize: {...}}`), must NOT fire this arm. Partial
  // masking of that shape is a deliberate NON-GOAL -- there is no legitimate producer that
  // emits a real `.phases` resume plus a stray top-level phase key, and the phase(s) inside
  // `.phases` are genuinely being resumed, not silently dropped, so there is nothing to
  // disclose about them.
  const tookPhasesBranch = topLevelCheckpoints.phases !== undefined;
  const wrapperHasRecognizedKey = Object.keys(topLevelCheckpoints).some((k) => recognizedPhaseNames.includes(k));
  if (tookPhasesBranch && resolvedKeys.length === 0 && wrapperHasRecognizedKey) {
    return ['checkpoint-discarded: checkpoints.phases is empty and a value under a recognized phase name '
      + "at the wrapper's top level was ignored -- re-supply it as the .phases map, or as a bare phase "
      + 'map, to attempt resuming from it.'];
  }

  return [];
}

// --- Checkpoint shape gate -------------------------------------
//
// A REPLAYED checkpoint phase value skips dispatch entirely (runPhase reuses it as-is),
// so an unconditional downstream read against it -- rankKey's `finding.severity` on a
// null element being the crash -- sees exactly whatever shape a hand-edited
// or version-skewed checkpoint artifact carries. A non-object phase value degrades
// silently instead: `(challengeOut.findings || [])` over a primitive yields an empty,
// ok:true review with no disclosure at all, and the belt-free
// phases do the same (measured: `checkpoints: {merge: 'not-an-object'}` returned
// ok:true, merged 0). checkpointShapeErrors is the fix: a pure, exported check run
// on the RESOLVED map readCheckpoints() returns, immediately after that call and before
// any phase is attempted, so a malformed value is refused loud instead of silently
// degrading somewhere downstream.
//
// One row per phase runPhase() names, ALL EIGHT. 'strict' means every array element
// must also be a plain object; 'tolerant' means the array container is checked but its
// elements are not. `gaps` is 'tolerant' on every row: runWith spreads `...(out.gaps ||
// [])` unconditionally, so ABSENT passes but a truthy non-array (5, {},
// 'abc') either raw-TypeErrors on the spread or silently iterates per-character garbage --
// container-only is enough to close that. discover also gets `dispatched`/`degraded` as
// 'tolerant': both are unconditionally `.join`-ed or iterated (allActiveDimensionsDegraded,
// the dimensions summary table), so a string there raw-TypeErrors the same way. Element
// tolerance on finding lists is reserved for filter.eliminated and
// challenge.unverified/eliminated. Replay path normalization drops malformed entries from them
// before they reach the belt or persisted artifacts. Eliminated lists may be absent as null.
const CHECKPOINT_ARRAY_STRICT = 'strict';
const CHECKPOINT_ARRAY_TOLERANT = 'tolerant';
export const CHECKPOINT_FINDING_LISTS = [
  ['discover', 'findings', CHECKPOINT_ARRAY_STRICT, false],
  ['merge', 'findings', CHECKPOINT_ARRAY_STRICT, false],
  ['verify', 'findings', CHECKPOINT_ARRAY_STRICT, false],
  ['validate', 'findings', CHECKPOINT_ARRAY_STRICT, false],
  ['filter', 'filtered', CHECKPOINT_ARRAY_STRICT, false],
  ['filter', 'eliminated', CHECKPOINT_ARRAY_TOLERANT, true],
  ['challenge', 'findings', CHECKPOINT_ARRAY_STRICT, false],
  ['challenge', 'unverified', CHECKPOINT_ARRAY_TOLERANT, false],
  ['challenge', 'eliminated', CHECKPOINT_ARRAY_TOLERANT, true],
];
export const checkpointFindingListsForPhase = (phase) => CHECKPOINT_FINDING_LISTS.filter(([name]) => name === phase);
const findingListShape = (phase) => Object.fromEntries(checkpointFindingListsForPhase(phase)
  .map(([, field, mode]) => [field, mode]));
// Exported for checkpoint_shape_gate.test.js, which derives the strict rows from it so a
// row flipped to strict gets an element-level null/primitive test for free, with no second
// edit to keep in sync (the export costs nothing in the bundle -- the table is consumed
// below by checkpointShapeErrors either way).
export const CHECKPOINT_PHASE_SHAPE_TABLE = {
  summarize: { gaps: CHECKPOINT_ARRAY_TOLERANT },
  discover: {
    ...findingListShape('discover'),
    gaps: CHECKPOINT_ARRAY_TOLERANT,
    dispatched: CHECKPOINT_ARRAY_TOLERANT,
    degraded: CHECKPOINT_ARRAY_TOLERANT,
  },
  merge: { ...findingListShape('merge'), gaps: CHECKPOINT_ARRAY_TOLERANT },
  verify: { ...findingListShape('verify'), gaps: CHECKPOINT_ARRAY_TOLERANT },
  validate: { ...findingListShape('validate'), gaps: CHECKPOINT_ARRAY_TOLERANT },
  filter: { ...findingListShape('filter'), gaps: CHECKPOINT_ARRAY_TOLERANT },
  challenge: {
    ...findingListShape('challenge'),
    gaps: CHECKPOINT_ARRAY_TOLERANT,
  },
  report: { gaps: CHECKPOINT_ARRAY_TOLERANT },
};

// Strict content lists are always emitted; their absence would replay a false empty review.
const CHECKPOINT_REQUIRED_CONTENT_FIELD = Object.fromEntries(CHECKPOINT_FINDING_LISTS
  .filter(([, , mode]) => mode === CHECKPOINT_ARRAY_STRICT).map(([phase, field]) => [phase, field]));
const CHECKPOINT_NULLABLE_FINDING_FIELDS = new Set(CHECKPOINT_FINDING_LISTS
  .filter(([, , , nullable]) => nullable).map(([phase, field]) => `${phase}.${field}`));

export function isPlainCheckpointObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function describeCheckpointShape(v) {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  return typeof v;
}

const CHECKPOINT_FINDING_FIELDS = new Set(CHECKPOINT_FINDING_LISTS.map(([, field]) => field));
const CHECKPOINT_FINDING_STRING_FIELDS = ['severity', 'title', 'file'];
const CHECKPOINT_FINDING_INTEGER_FIELDS = ['line', 'line_start', 'line_end', 'end_line'];

function checkpointFindingShapeErrors(finding, path) {
  const violations = [];
  for (const field of CHECKPOINT_FINDING_STRING_FIELDS) {
    if (typeof finding[field] !== 'string') {
      violations.push(`checkpoint-shape: ${path}.${field} must be a string, got ${describeCheckpointShape(finding[field])}`);
    }
  }

  const hasDescription = finding.description !== undefined;
  const hasBody = finding.body !== undefined;
  if (!hasDescription && !hasBody) {
    violations.push(`checkpoint-shape: ${path} must contain a string description or body`);
  }
  for (const field of ['description', 'body']) {
    if (finding[field] !== undefined && typeof finding[field] !== 'string') {
      violations.push(`checkpoint-shape: ${path}.${field} must be a string, got ${describeCheckpointShape(finding[field])}`);
    }
  }

  for (const field of CHECKPOINT_FINDING_INTEGER_FIELDS) {
    if (finding[field] !== undefined && !Number.isInteger(finding[field])) {
      violations.push(`checkpoint-shape: ${path}.${field} must be an integer, got ${describeCheckpointShape(finding[field])}`);
    }
  }
  if (finding.confidence !== undefined && (typeof finding.confidence !== 'number' || !Number.isFinite(finding.confidence))) {
    violations.push(`checkpoint-shape: ${path}.confidence must be a number, got ${describeCheckpointShape(finding.confidence)}`);
  }

  if (finding.corroborations !== undefined) {
    if (!Array.isArray(finding.corroborations)) {
      violations.push(`checkpoint-shape: ${path}.corroborations must be an array, got ${describeCheckpointShape(finding.corroborations)}`);
    } else {
      finding.corroborations.forEach((corroboration, idx) => {
        if (!isPlainCheckpointObject(corroboration)) {
          violations.push(`checkpoint-shape: ${path}.corroborations[${idx}] must be a plain object, got ${describeCheckpointShape(corroboration)}`);
        }
      });
    }
  }
  return violations;
}

// checkpointShapeErrors(resolvedCheckpoints) -> string[] of `checkpoint-shape:`-prefixed
// violations (registered in docs/machine-parsed-strings.md). Takes the OUTPUT of
// readCheckpoints -- already `.phases`-unwrapped, already defaulted to {} for a
// non-object top-level `checkpoints` (that top-level shape stays inert BY that earlier
// default, never asserted on here). An absent OPTIONAL field always passes, so a
// version-delta replay (an older checkpoint predating a newer field) keeps working -- the
// one exception is CHECKPOINT_REQUIRED_CONTENT_FIELD, whose absence IS itself a violation
// (see that table's own comment). Unknown checkpoint keys (not one of the eight phase
// names) are still unvalidated here -- this function never inspects them -- but they are
// no longer silent when they are the ONLY keys present: checkpointDiscardGap (
// computed by runWith from the top-level `args.checkpoints` before this function ever
// runs) discloses that case as a no-op resume. Collects every violation rather than
// stopping at the first, so a caller sees the whole shape of the damage in one refusal
// instead of fixing it one field at a time.
export function checkpointShapeErrors(resolvedCheckpoints) {
  const cp = isPlainCheckpointObject(resolvedCheckpoints) ? resolvedCheckpoints : {};
  const violations = [];
  for (const phase of Object.keys(CHECKPOINT_PHASE_SHAPE_TABLE)) {
    const value = cp[phase];
    if (value === undefined) continue;
    if (!isPlainCheckpointObject(value)) {
      violations.push(`checkpoint-shape: phases.${phase} must be a plain object, got ${describeCheckpointShape(value)}`);
      continue; // container failed -- no field inside it can be checked
    }
    const requiredField = CHECKPOINT_REQUIRED_CONTENT_FIELD[phase];
    if (requiredField && value[requiredField] === undefined) {
      violations.push(`checkpoint-shape: phases.${phase} is missing required field ${requiredField}`);
    }
    const fields = CHECKPOINT_PHASE_SHAPE_TABLE[phase];
    for (const field of Object.keys(fields)) {
      const arrVal = value[field];
      if (arrVal === undefined) continue;
      if (arrVal === null && CHECKPOINT_NULLABLE_FINDING_FIELDS.has(`${phase}.${field}`)) continue;
      if (!Array.isArray(arrVal)) {
        violations.push(`checkpoint-shape: phases.${phase}.${field} must be an array, got ${describeCheckpointShape(arrVal)}`);
        continue;
      }
      arrVal.forEach((el, idx) => {
        if (!isPlainCheckpointObject(el)) {
          if (fields[field] === CHECKPOINT_ARRAY_STRICT) {
            violations.push(`checkpoint-shape: phases.${phase}.${field}[${idx}] must be a plain object, got ${describeCheckpointShape(el)}`);
          }
          return;
        }
        if (CHECKPOINT_FINDING_FIELDS.has(field)) {
          violations.push(...checkpointFindingShapeErrors(el, `phases.${phase}.${field}[${idx}]`));
        }
      });
    }
  }
  return violations;
}

// The checkpoint-shape refusal's own recovery sentence. SKILL_RECOVERY_LINE (args.js) is
// wrong for this arm -- its advice is "run the skill instead", but the skill's own
// Phase 1-2 assembly does not construct a malformed checkpoint value in the first place,
// so telling the operator to re-run the skill would just replay the same corrupt
// artifact. The actionable fix is the checkpoint artifact itself.
const CHECKPOINT_SHAPE_RECOVERY_LINE = 'Repair or delete the corrupt checkpoint artifact, or re-run without the '
  + '`checkpoints` argument to start the review fresh.';

// makeCheckpointShapeRejectEnvelope(violations, nullArgGaps, contextSizeGap, discardGap) ->
// the dedicated pre-dispatch refusal for a shape violation. A SIBLING of
// makeArgsRejectEnvelope (args.js), not a reuse of it: this failure is not an args-shape
// failure (validateArgs already accepted `A`), so failingPhase/phaseReached name the
// pseudo-phase 'checkpoints' rather than 'args'. `checkpoints: { completed: [] }` is
// present -- SKILL.md documents every ok:false envelope as ALWAYS carrying `checkpoints`
// -- but carries NO `.phases` map, so headless auto-resume (which fires only when
// `.phases` is present) cannot replay the same still-malformed checkpoint straight back in.
//
// `nullArgGaps` (the caller's already-computed nullToleranceRejectedKeys disclosures, same
// value the args-reject exit above threads into ITS gaps) rides on this exit too, exactly
// as stages.js's tolerated-null invariant promises ("rides on BOTH exits" -- runWith's own
// comment on that computation). Before this parameter existed, a run that both mis-stamped
// a nullable top-level field AND replayed a malformed checkpoint lost the null disclosure
// entirely: the checkpoint gate returns before the null-tolerant success exit even runs, so
// nullArgGaps had nowhere else to surface. `contextSizeGap` (the caller's already-computed
// context_unmeasured/context_unplannable disclosure, always an array of 0 or 1 strings)
// rides here for the identical reason -- it is computed above the gate too, and this was
// the one exit that dropped it silently on the floor.
//
// `discardGap` (also computed above the gate, alongside contextSizeGap) is the
// same shape as contextSizeGap -- a 0-or-1-element `checkpoint-discarded:` array -- and
// rides here for the identical reason. Unreachable in production today (a garbage
// `args.checkpoints` alone resolves inert with zero shape violations, so this exit is never
// taken on that path alone; reaching both a discard AND a violation needs a garbage
// `args.checkpoints` PLUS a malformed `ctx.checkpoints` fallback seam), but one line buys
// consistency with this envelope's own nullArgGaps/contextSizeGap rationale rather than a
// silent gap-channel gap should that combination ever become reachable. The arms
// specifically (checkpointDiscardGap's zero-recognized and empty-.phases-masking cases,
// as opposed to the unwrap-rejection case) can never co-occur with a shape violation at
// all: both arms require unwrapCheckpointMap to have SUCCEEDED to a resolved map whose
// keys checkpointShapeErrors -- which runs immediately after, on that same resolved map --
// never inspects (unrecognized names, or nothing at the top level of `.phases`), so a
// disclosure and a shape violation are always mutually exclusive on the SAME resolved map.
export function makeCheckpointShapeRejectEnvelope(violations, nullArgGaps, contextSizeGap, discardGap) {
  const extra = violations.length - 1;
  const error = extra > 0
    ? `${violations[0]} (and ${extra} more checkpoint-shape violation${extra === 1 ? '' : 's'}). ${CHECKPOINT_SHAPE_RECOVERY_LINE}`
    : `${violations[0]}. ${CHECKPOINT_SHAPE_RECOVERY_LINE}`;
  return {
    ok: false,
    error,
    failingPhase: 'checkpoints',
    phaseReached: 'checkpoints',
    artifactPaths: {},
    stats: {},
    gaps: [...nullArgGaps, ...contextSizeGap, ...discardGap, ...violations],
    checkpoints: { completed: [] },
  };
}

// buildResumeCheckpoints(phaseOutputs) -> resume state for a FAILURE-path return.
// Carries the full per-phase outputs map ({ phases, completed }) so the skill can resume
// from the compact return when nothing was persisted — UNLESS that map would exceed the
// RETURN char budget, in which case only the completed-phase NAMES are returned with
// truncated:true (resume then falls back to re-running from scratch).
// readCheckpoints unwraps the .phases form directly.
//
// The budget here is RETURN_CHAR_BUDGET, not the prompt-segmentation one. This is the
// only thing the map has to survive — the harness serializes the return to
// `tasks/<taskid>.output` and no model retypes it — and the prompt budget it used to
// share was sized for a WRITER'S CONTEXT, a limit that does not apply to this channel at
// all. Three recorded runs tripped the 100,000-char ceiling and threw away resume state
// that would have crossed the real one (measured byte-exact to 4 MB) untouched.
export function buildResumeCheckpoints(phaseOutputs) {
  const completed = Object.keys(phaseOutputs);
  const withPhases = { phases: phaseOutputs, completed };
  if (JSON.stringify(withPhases).length <= RETURN_CHAR_BUDGET) return withPhases;
  return { completed, truncated: true };
}

// Phases whose FULL output a resume from the PERSISTED (successful-run) checkpoint actually
// consumes. Exactly ONE qualifies: `challenge` carries the delivered high-confidence findings
// and the `unverified` bucket that selectDelivery, the report input, and writeArtifacts read
// BY VALUE — replaying it is what makes the delivered set on a resume verbatim-identical.
//
// `filter` is deliberately NOT persisted. On a resume from the persisted
// checkpoint, summarize/discover/verify/validate/filter re-run, while challenge carries the
// delivered findings and unverified bucket by value. Filter is a PURE agent-free JS function,
// so it re-runs at ZERO dispatch cost and its fresh stats feed the report input and envelope.
// Persisting it bought nothing and cost 35% of the checkpoint artifact's bytes in the profiled
// run. Every other phase contributes only a count/stat on resume, never its findings bulk.
const PERSISTED_RESUME_PHASES = ['challenge'];

// phaseFindingCount(out) -> the count summarizing one phase's output for the slim checkpoint
// (findings-bearing stages carry `findings`; the filter stage carries `filtered`).
function phaseFindingCount(out) {
  if (!out || typeof out !== 'object') return 0;
  if (Array.isArray(out.findings)) return out.findings.length;
  if (Array.isArray(out.filtered)) return out.filtered.length;
  return 0;
}

// slimPersistedCheckpoints(phaseOutputs, completed, phaseReached) -> the checkpoint artifact
// the writer persists at the end of a successful run. Only the resume-consumed phases
// (PERSISTED_RESUME_PHASES) keep their FULL output; every phase additionally records a bare
// count. This drops the by-value duplication where the OLD persisted checkpoint carried every
// phase's full findings array (discover/merge/verify/validate each ~a full findings set) inside
// the single artifact-writer prompt. readCheckpoints unwraps `.phases`, so a resume from this
// artifact skips exactly the preserved phases (reusing the delivered findings verbatim) and
// re-runs the rest. The in-memory failure-path resume (buildResumeCheckpoints) is intentionally
// NOT slimmed — a crash-recovery resume still carries every phase's full output for a fast skip.
export function slimPersistedCheckpoints(phaseOutputs, completed, phaseReached) {
  const outputs = phaseOutputs || {};
  const phases = {};
  for (const name of PERSISTED_RESUME_PHASES) {
    if (outputs[name] !== undefined) phases[name] = outputs[name];
  }
  const counts = {};
  for (const [name, out] of Object.entries(outputs)) counts[name] = phaseFindingCount(out);
  return { phases, completed, phaseReached, counts };
}

// --- replay filtering belt: helpers -------------------------------

// Marker key used ONLY to re-splice applyReplayInjectionScan's kept/eliminated
// output back into the ORIGINAL list order. Heuristic 10 (duplicate signature)
// is stateful over scan order, so object elements must be handed to the scan
// as ONE ordered list, not pre-partitioned by kind (kept vs. eliminated) --
// this key is how each element's original position survives that round trip;
// it never survives into a returned finding (stripped below on both paths).
const REPLAY_BELT_INDEX_KEY = '__replayBeltIndex';

// beltPartitionList(list) -> { list, eliminated }. The belt's per-list
// worker for challengeOut.findings / .unverified:
//   - an absent or empty list is returned untouched because it has no scan state;
//   - every object element is normalized (normalizeFieldNames, idempotent --
//     a precondition of the scan, not pre-challenge semantics: a hand-built
//     or v2-era checkpoint can carry `body`/`line`/`blame_tag` instead of the
//     canonical names the scan reads) and then scanned as ONE ordered list
//     via applyReplayInjectionScan (heuristic 4 structurally excluded -- see
//     that function's doc comment in filterFindings.js). Survivors are
//     re-spliced back into their original relative position among the other
//     surviving elements; eliminated elements are removed from `list` and
//     returned separately, stamped with a `replay_belt: true` marker on top
//     of the scan's own eliminated_by/elimination_reason. Replay normalization
//     has already removed non-objects from these lists.
export function beltPartitionList(list) {
  if (!Array.isArray(list) || list.length === 0) return { list, eliminated: [] };

  const objectElements = list.map((el, idx) => ({ ...el, [REPLAY_BELT_INDEX_KEY]: idx }));

  normalizeFieldNames(objectElements);
  const { kept, eliminated } = applyReplayInjectionScan(objectElements);

  const survivorByIdx = new Map();
  for (const f of kept) {
    const { [REPLAY_BELT_INDEX_KEY]: idx, ...clean } = f;
    survivorByIdx.set(idx, clean);
  }
  const eliminatedOut = eliminated.map((f) => {
    const { [REPLAY_BELT_INDEX_KEY]: _idx, ...clean } = f;
    return { ...clean, replay_belt: true };
  });

  const splicedList = list.flatMap((_el, idx) => survivorByIdx.has(idx) ? [survivorByIdx.get(idx)] : []);

  return { list: splicedList, eliminated: eliminatedOut };
}

// A fresh challenge can omit eliminated; replayed containers are shape-gated.
export function stripEliminatedList(list) {
  return list.map(applyInjectedProseStrip);
}
