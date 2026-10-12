import { normalizeFindingPaths } from './mergeFindings.js';
import { RETURN_CHAR_BUDGET } from './wire.js';
import { applyInjectedProseStrip, applyReplayInjectionScan, normalizeFieldNames } from './filterFindings.js';

// Arrays are unusable phase maps: indexing a phase name can otherwise replay an Array prototype method.
function unwrapCheckpointMap(cp) {
  if (!isPlainCheckpointObject(cp)) return null;
  if (cp.phases === undefined) return cp;
  return isPlainCheckpointObject(cp.phases) ? cp.phases : null;
}

// Returns a phase-keyed resume map, accepting either a bare map or a phases wrapper.
// Unusable args checkpoints fall back to ctx checkpoints, then an empty map.
export function readCheckpoints(ctx, args) {
  const A = args || {};
  return unwrapCheckpointMap(A.checkpoints) || (ctx && unwrapCheckpointMap(ctx.checkpoints)) || {};
}

// Returns zero or one checkpoint-discarded gap for rejected or wholly ignored resume input.
// Absent and legitimately empty maps stay silent because they contain no resume state to lose.
// Diagnostics use fixed vocabulary so caller-controlled keys cannot inject prose into gaps.
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
  // Presence matters here; the shape gate separately rejects malformed values under recognized keys.
  const hasRecognizedKey = resolvedKeys.some((k) => recognizedPhaseNames.includes(k));

  if (resolvedKeys.length > 0 && !hasRecognizedKey) {
    // A false flag must not send callers to truncated-return recovery.
    if (resolved.truncated) {
      return ['checkpoint-discarded: the supplied checkpoints value is a truncated failure-path return '
        + '(phase names only, no phase outputs) -- nothing in it is resumable; re-run without the '
        + 'checkpoints argument to start the review fresh.'];
    }
    if (resolvedKeys.includes('completed')) {
      // An empty refusal envelope has no persisted artifact to point the operator toward.
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

  // A non-empty phases map resumes work, so ignored sibling keys do not imply a wholly discarded resume.
  const tookPhasesBranch = topLevelCheckpoints.phases !== undefined;
  const wrapperHasRecognizedKey = Object.keys(topLevelCheckpoints).some((k) => recognizedPhaseNames.includes(k));
  if (tookPhasesBranch && resolvedKeys.length === 0 && wrapperHasRecognizedKey) {
    return ['checkpoint-discarded: checkpoints.phases is empty and a value under a recognized phase name '
      + "at the wrapper's top level was ignored -- re-supply it as the .phases map, or as a bare phase "
      + 'map, to attempt resuming from it.'];
  }

  return [];
}

// Replayed outputs bypass dispatch, so malformed containers need rejection before downstream reads.
// Tolerant finding lists require caller cleanup before replay scanning.
// Gaps and dimension lists need array containers because consumers spread, iterate, or join them.
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
const CHECKPOINT_PHASE_SHAPE_TABLE = {
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

// Missing content lists would replay a false empty review.
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

// Returns every checkpoint-shape violation from the resolved map returned by readCheckpoints.
// Optional fields may be absent so checkpoints remain usable when a producer adds fields.
// Collecting all violations lets callers repair the shape in one pass.
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

// Re-running with the same corrupt artifact repeats the failure; recovery must address the checkpoint.
const CHECKPOINT_SHAPE_RECOVERY_LINE = 'Repair or delete the corrupt checkpoint artifact, or re-run without the '
  + '`checkpoints` argument to start the review fresh.';

// Returns a checkpoint refusal envelope; violations must be non-empty and gap arguments must be arrays.
// Omitting phases prevents automatic resume from replaying the same malformed state.
// Carry prior disclosures because this early return bypasses the normal return path.
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

// Returns { phases, completed } within the return budget, otherwise { completed, truncated: true }.
// The harness serializes this channel without model transcription, so the return budget applies.
export function buildResumeCheckpoints(phaseOutputs) {
  const completed = Object.keys(phaseOutputs);
  const withPhases = { phases: phaseOutputs, completed };
  if (JSON.stringify(withPhases).length <= RETURN_CHAR_BUDGET) return withPhases;
  return { completed, truncated: true };
}

// Challenge preserves the delivered findings for resume; other finding arrays need not be duplicated.
const PERSISTED_RESUME_PHASES = ['challenge'];

function phaseFindingCount(out) {
  if (!out || typeof out !== 'object') return 0;
  if (Array.isArray(out.findings)) return out.findings.length;
  if (Array.isArray(out.filtered)) return out.filtered.length;
  return 0;
}

// Returns the persisted checkpoint with resume-consumed outputs and per-phase counts.
// Failure-path checkpoints stay full so crash recovery can skip all completed work.
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

// Duplicate detection depends on scan order, so the round trip must preserve original positions.
const REPLAY_BELT_INDEX_KEY = '__replayBeltIndex';

// Returns { list, eliminated }, marking removed findings with replay_belt.
// The caller removes non-object entries first; normalization exposes aliases to the scan.
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

export function stripEliminatedList(list) {
  return list.map(applyInjectedProseStrip);
}

// Replayed findings need the same path validation as fresh output.
export function normalizePhaseFindings(name, out, replayed, args) {
  if (!out || typeof out !== 'object') return;
  const warnings = [];
  let rejected = 0;
  let pathRewrites = 0;
  for (const [, field] of checkpointFindingListsForPhase(name)) {
    if (!Array.isArray(out[field])) continue;
    const normalized = normalizeFindingPaths(out[field], args.repoRoot);
    rejected += out[field].length - normalized.valid.length;
    pathRewrites += normalized.path_rewrites;
    out[field] = normalized.valid;
    warnings.push(...normalized.warnings);
  }
  if (pathRewrites || (replayed && rejected)) {
    const stats = isPlainCheckpointObject(out.stats) ? out.stats : {};
    out.stats = { ...stats,
      ...(pathRewrites ? { path_rewrites: (stats.path_rewrites || 0) + pathRewrites } : {}),
      ...(replayed && rejected ? { replay_path_rejected: rejected } : {}),
    }
  }
  if (name === 'merge') {
    const methodology = isPlainCheckpointObject(out.methodology) ? out.methodology : {};
    out.methodology = methodology;
    const channels = methodology.findings_per_channel;
    const surviving = out.findings.length;
    // Attribution is approximate because replayed findings carry no channel.
    const ndjsonCount = Number.isFinite(channels?.ndjson) ? channels.ndjson : 0;
    const textFallbackCount = Number.isFinite(channels?.text_fallback) ? channels.text_fallback : 0;
    const ndjson = Math.min(Math.max(0, ndjsonCount), surviving);
    const textFallback = Math.min(Math.max(0, textFallbackCount), surviving - ndjson);
    if (channels && typeof channels === 'object') {
      out.methodology = { ...methodology, findings_per_channel: {
        ...channels, ndjson, text_fallback: textFallback,
      } };
    }
    if (warnings.length) {
      out.methodology = { ...out.methodology, validation_warnings: [
        ...(Array.isArray(methodology.validation_warnings) ? methodology.validation_warnings : []),
        ...warnings,
      ] };
    }
  }
  if (!warnings.length) return;
  out.gaps = [...(Array.isArray(out.gaps) ? out.gaps : []), ...warnings];
}

// Replay bypasses filtering, so this belt must run immediately after challenge before any finding reader.
// Confidence heuristics are excluded because later adjustments can change their result.
export function applyChallengeReplayBelt(challengeOut) {
  // Fresh outputs can be malformed even though replay checkpoints are shape-gated.
  if (!challengeOut || typeof challengeOut !== 'object') return;
  // Preserve list order so replay deduplication sees the same sequence; mutate in place for reporting, delivery, and persistence.
  const findingsResult = beltPartitionList(challengeOut.findings);
  const unverifiedResult = beltPartitionList(challengeOut.unverified);
  challengeOut.findings = findingsResult.list;
  challengeOut.unverified = unverifiedResult.list;
  const newlyEliminated = [...findingsResult.eliminated, ...unverifiedResult.eliminated];

  const carriedEliminated = Array.isArray(challengeOut.eliminated) ? challengeOut.eliminated : [];
  // Strip after combining lists so eliminated-only payloads cannot survive.
  challengeOut.eliminated = stripEliminatedList([...carriedEliminated, ...newlyEliminated]);

  // Challenge outcome counts precede this belt and need not sum to final_count; array counts must reflect the scan.
  if (challengeOut.stats && typeof challengeOut.stats === 'object') {
    const k1 = findingsResult.eliminated.length;
    const k2 = unverifiedResult.eliminated.length;
    if (typeof challengeOut.stats.final_count === 'number') {
      challengeOut.stats.final_count = challengeOut.findings.length;
    }
    if (typeof challengeOut.stats.skipped === 'number' && Array.isArray(challengeOut.unverified)) {
      challengeOut.stats.skipped = challengeOut.unverified.length;
    }
    challengeOut.stats.replay_belt_eliminated = k1 + k2;
  }
}
