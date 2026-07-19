// applyChallenges.js — JS twin of scripts/apply_challenges.py:94-398 (Phase
// 7->8 bridge). Applies blind-challenge scores to Phase 6 findings, re-runs
// cross-agent dedup, ranks the final set. File I/O (load_filtered/
// load_challenges) and CLI argparse/stdout wiring stay in the SKILL/stage
// layer -- not ported here, matching filterFindings.js's applyFilterPipeline
// (config/exclusions passed in already-parsed, no disk access).

import { dedupCrossAgent, SEVERITY_ORDER } from './filterFindings.js';
import { pyIntStrict } from './applyValidations.js';

// SEVERITY_ORDER is imported from filterFindings.js (its single owner) — see the
// note there. A second top-level `const SEVERITY_ORDER` here collided in the
// concatenated bundle after build.js strips the `export` keyword.

// Deep clone via JSON round-trip. The workflow runtime sandbox does NOT provide
// structuredClone (a node/browser global, absent here — it crashed the live smoke
// run at the call site below). Findings are JSON-safe by construction (strings,
// numbers, booleans, null, plain arrays/objects — no Date/Map/Set/undefined/
// functions), so a JSON round-trip is a faithful deep copy. See CLAUDE.md
// (Workflow runtime section — only JSON-safe globals are guaranteed here).
export function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

// Port of _downgrade_severity: "critical" -> "high" -> "medium" -> "low" ->
// null. Non-string input (Python's severity.lower() raising AttributeError
// on a non-str) and unknown severities both fall through to null, exactly
// like Python's `except (ValueError, AttributeError): return None`.
export function downgradeSeverity(severity) {
  const idx = typeof severity === 'string' ? SEVERITY_ORDER.indexOf(severity.toLowerCase()) : -1;
  if (idx < 0 || idx + 1 >= SEVERITY_ORDER.length) return null;
  return SEVERITY_ORDER[idx + 1];
}

// Port of _rank_key. Single 3-tuple (sevIdx, -confidence, tertiary):
// tertiary is -risk_level when risk_level is present, non-null, and
// numeric (Number.isFinite(Number(rl))) -- the explicit `rl !== null` guard
// matters because Number(null) === 0 is finite, which would otherwise
// silently treat an explicit `risk_level: null` as risk_level 0 instead of
// falling back to -description.length (see the
// rank_risk_level_absent_falls_back_to_description_length fixture, which
// pins this exact null-vs-zero distinction against the authoritative
// Python: `if risk_level is not None: ... else: tertiary = -len(description)`).
export function rankKey(finding) {
  const sev = (finding.severity ?? 'low').toLowerCase();
  let sevIdx = SEVERITY_ORDER.indexOf(sev);
  if (sevIdx < 0) sevIdx = SEVERITY_ORDER.length;
  const conf = finding.confidence ?? 0;
  let tertiary;
  const rl = finding.risk_level;
  if (rl !== undefined && rl !== null && Number.isFinite(Number(rl))) tertiary = -Number(rl);
  else tertiary = -((finding.description ?? '').length);
  return [sevIdx, -conf, tertiary];
}

// Port of rank_findings. ONE composite comparator built from rankKey's
// 3-tuple -- not chained `.sort()` calls -- so severity, confidence, and
// risk_level/description-length are decided together in a single stable
// pass (see the rank_by_severity_then_confidence_then_risk_level fixture).
export function rankFindings(findings) {
  return [...findings].sort((a, b) => {
    const ka = rankKey(a);
    const kb = rankKey(b);
    return ka[0] - kb[0] || ka[1] - kb[1] || ka[2] - kb[2];
  });
}

// Port of apply_challenges (scripts/apply_challenges.py:219-391) plus the
// dedup-rerun + rank composition from main() (:449-480). Bridges Phase 6
// output through blind-challenge thresholds:
//   score < 25   non-security -> remove; security -> downgrade (already
//                "low" -> remove)
//   score 25-49  downgrade one step + re-route to suggestion; already
//                "low" -> remove with eliminated_by="challenge:downgraded"
//   score 50-74  contest (kept); origin="surfaced" -> re-route to suggestion
//   score >= 75  survive unchanged
// Findings with no matching challenge entry pass through untouched (by
// reference -- not cloned, mirroring Python's aliasing of unmatched dict
// objects). Findings WITH a matching entry are deep-cloned (deepClone, a
// JSON round-trip) before any mutation, so the caller's input array/objects
// are never mutated (see the deep_copy_no_mutation_of_input fixture).
export function applyChallenges(findings, challenges) {
  // Build id -> challenge entry map (O(n) lookup). An entry is registered
  // only when it has both a truthy id and an int-coercible score -- matches
  // Python's challenge_by_id build loop, which `continue`s (silently, past
  // a stderr warning not reproduced here) on either failure.
  const challengeById = new Map();
  for (const entry of challenges) {
    const cid = 'id' in entry ? entry.id : undefined;
    if (cid === undefined || cid === null) continue;
    const rawScore = 'score' in entry ? entry.score : undefined;
    if (rawScore === undefined || rawScore === null) continue;
    if (pyIntStrict(rawScore) === null) continue;
    challengeById.set(cid, entry);
  }

  const active = [];
  const eliminated = [];
  const stats = {
    challenge_removed: 0,
    challenge_downgraded: 0,
    challenge_contested: 0,
    challenge_survived: 0,
    unchallenged: 0,
  };

  for (let finding of findings) {
    const fid = 'id' in finding ? finding.id : undefined;
    const entry = challengeById.get(fid);

    if (entry === undefined) {
      // No challenge result -- pass through unchanged (no clone: matches
      // Python's aliasing of the original dict when no entry matches).
      stats.unchallenged += 1;
      active.push(finding);
      continue;
    }

    // Re-derive score from the entry (mirrors Python's second `int()` call
    // at apply time; guaranteed to succeed since the map-build loop above
    // already validated int-coercibility for this same entry).
    const rawScore = 'score' in entry ? entry.score : 0;
    const score = pyIntStrict(rawScore);
    const justification = 'justification' in entry ? entry.justification : undefined;

    // Deep-clone before mutation -- no aliasing of the caller's finding.
    finding = deepClone(finding);
    finding.challenge_score = score;
    if (justification) finding.challenge_justification = justification;

    const isSurfaced = ('origin' in finding ? finding.origin : '').toLowerCase() === 'surfaced';
    const isSecurity = ('dimension' in finding ? finding.dimension : '').toLowerCase() === 'security';

    if (score < 25) {
      if (isSecurity) {
        const currentSev = ('severity' in finding ? finding.severity : 'low').toLowerCase();
        const newSev = downgradeSeverity(currentSev);
        finding.challenge_contested = false;
        if (newSev === null) {
          eliminated.push({
            ...finding,
            eliminated_by: 'challenge:removed',
            elimination_reason:
              `challenge score ${score} < 25; security finding severity already ` +
              `'${currentSev}' (lowest) — finding removed`,
          });
          stats.challenge_removed += 1;
        } else {
          finding.severity = newSev;
          finding.severity_downgraded = true;
          finding.original_severity = currentSev;
          finding.report_destination = 'suggestion';
          finding.report_tag = 'suggestion';
          active.push(finding);
          stats.challenge_downgraded += 1;
        }
      } else {
        // Hard remove for non-security findings. NOTE: challenge_contested
        // is intentionally never set on this branch's finding -- Python
        // never sets it here either (only the security sub-branch above and
        // the 25-49/50-74/>=75 branches below set it).
        eliminated.push({
          ...finding,
          eliminated_by: 'challenge:removed',
          elimination_reason: `challenge score ${score} < 25; finding does not survive blind challenge`,
        });
        stats.challenge_removed += 1;
      }
    } else if (score < 50) {
      const currentSev = ('severity' in finding ? finding.severity : 'low').toLowerCase();
      const newSev = downgradeSeverity(currentSev);
      finding.challenge_contested = false;
      if (newSev === null) {
        eliminated.push({
          ...finding,
          eliminated_by: 'challenge:downgraded',
          elimination_reason:
            `challenge score ${score} in 25-49 range; severity already ` +
            `'${currentSev}' (lowest) — finding removed`,
        });
        stats.challenge_downgraded += 1;
      } else {
        finding.severity = newSev;
        finding.severity_downgraded = true;
        finding.original_severity = currentSev;
        finding.report_destination = 'suggestion';
        finding.report_tag = 'suggestion';
        active.push(finding);
        stats.challenge_downgraded += 1;
      }
    } else if (score < 75) {
      finding.challenge_contested = true;
      if (isSurfaced) {
        finding.report_destination = 'suggestion';
        finding.report_tag = 'suggestion';
      }
      active.push(finding);
      stats.challenge_contested += 1;
    } else {
      finding.challenge_contested = false;
      active.push(finding);
      stats.challenge_survived += 1;
    }
  }

  const totalInput = findings.length;

  // Cross-agent dedup re-run (Task 5's dedupCrossAgent, reused not
  // reimplemented) + rank -- mirrors main()'s post-challenge composition.
  const { kept: dedupedActive, dropped: dedupDropped } = dedupCrossAgent(active);
  const ranked = rankFindings(dedupedActive);

  const allEliminated = [...eliminated, ...dedupDropped];

  return {
    findings: ranked,
    eliminated: allEliminated,
    stats: {
      total_input: totalInput,
      challenge_removed: stats.challenge_removed,
      challenge_downgraded: stats.challenge_downgraded,
      challenge_contested: stats.challenge_contested,
      challenge_survived: stats.challenge_survived,
      unchallenged: stats.unchallenged,
      dedup_dropped: dedupDropped.length,
      final_count: ranked.length,
    },
  };
}
