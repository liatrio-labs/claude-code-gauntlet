// Apply challenge scores to findings and preserve the consolidation fields.

import { consolidateCrossAgent, SEVERITY_ORDER } from './filterFindings.js';
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

export function downgradeSeverity(severity) {
  const idx = typeof severity === 'string' ? SEVERITY_ORDER.indexOf(severity.toLowerCase()) : -1;
  if (idx < 0 || idx + 1 >= SEVERITY_ORDER.length) return null;
  return SEVERITY_ORDER[idx + 1];
}

export function rankKey(finding) {
  const sev = (finding.severity ?? 'low').toLowerCase();
  let sevIdx = SEVERITY_ORDER.indexOf(sev);
  if (sevIdx < 0) sevIdx = SEVERITY_ORDER.length;
  const degraded = finding.origin === 'unknown' ? 1 : 0;
  const conf = finding.confidence ?? 0;
  let tertiary;
  const rl = finding.risk_level;
  if (rl !== undefined && rl !== null && Number.isFinite(Number(rl))) tertiary = -Number(rl);
  else tertiary = -((finding.description ?? '').length);
  return [sevIdx, degraded, -conf, tertiary];
}

export function rankFindings(findings) {
  return [...findings].sort((a, b) => {
    const ka = rankKey(a);
    const kb = rankKey(b);
    return ka[0] - kb[0] || ka[1] - kb[1] || ka[2] - kb[2] || ka[3] - kb[3];
  });
}

// Match challenges by finding id, adjust scores, and return the active and eliminated findings.
export function applyChallenges(findings, challenges) {
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
      stats.unchallenged += 1;
      active.push(finding);
      continue;
    }

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

  const { findings: consolidatedActive, consolidatedCount } = consolidateCrossAgent(active);
  const ranked = rankFindings(consolidatedActive);

  return {
    findings: ranked,
    eliminated,
    stats: {
      total_input: totalInput,
      challenge_removed: stats.challenge_removed,
      challenge_downgraded: stats.challenge_downgraded,
      challenge_contested: stats.challenge_contested,
      challenge_survived: stats.challenge_survived,
      unchallenged: stats.unchallenged,
      cross_agent_consolidated: consolidatedCount,
      final_count: ranked.length,
    },
  };
}
