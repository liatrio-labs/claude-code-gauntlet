import { routeVerdict } from './verdict.js';
import { chooseGroupLead } from './groupRanking.js';
import { canonicalRange } from './location.js';
import { INT_POLICY, coerceInt } from './wire.js';
import { FIELD_RENAMES, INJECTION_STRIPPED_PROSE_FIELDS } from './registry.js';
import { findingPolicy } from './registry.js';
import { applyInjectionFilter } from './injection.js';

export function normalizeFieldNames(findings, renames = FIELD_RENAMES) {
  let normalizedCount = 0;
  for (const finding of findings) {
    let renamed = false;
    for (const [legacy, canonical] of Object.entries(renames)) {
      if (legacy in finding && !(canonical in finding)) {
        finding[canonical] = finding[legacy];
        delete finding[legacy];
        renamed = true;
      }
    }
    if (renamed) normalizedCount += 1;
  }
  return normalizedCount;
}

export function applyVerdictRoutes(findings) {
  const kept = [], eliminated = [];
  for (const finding of findings) {
    for (const key of ['consensus_boost', 'singleton_penalty', 'contested', 'contestation_drop', 'contestation_reason']) delete finding[key];
    routeVerdict(finding);
    if (finding.verdict_route === 'eliminated') {
      eliminated.push({ ...finding, eliminated_by: 'verified_refutation', elimination_reason: finding.verdict.justification });
      continue;
    }
    kept.push(finding);
  }
  return { kept, eliminated };
}

// Present null values remain data: defaulting them would merge distinct grouping keys and IDs.
function pyGet(obj, key, dflt) {
  return key in obj ? obj[key] : dflt;
}

function pyRound(x) {
  const floor = Math.floor(x);
  const diff = x - floor;
  if (diff < 0.5) return floor;
  if (diff > 0.5) return floor + 1;
  return floor % 2 === 0 ? floor : floor + 1; // exact .5 -> nearest even
}

function lineBucket(line, proximity) {
  const n = coerceInt(line, INT_POLICY.filter);
  if (n === null) return 0;
  return pyRound(n / proximity) * proximity;
}

function groupFilterFindings(findings, keyFor) {
  const groups = new Map();
  for (const finding of findings) {
    const key = canonicalRange(finding) ? keyFor(finding) : finding;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(finding);
  }
  return groups;
}

function groupByProximity(findings, lineProximity) {
  return groupFilterFindings(findings, (finding) => JSON.stringify([
    pyGet(finding, 'file', ''), lineBucket(finding.line_start, lineProximity),
  ]));
}

export function consolidateCrossAgent(findings) {
  const LINE_PROXIMITY = 5;

  // Clear consolidation stamps before regrouping so a removed primary cannot leave stale group ownership.
  for (const f of findings) {
    if (f && typeof f === 'object') {
      delete f.consolidation_key;
      delete f.consolidation_primary;
    }
  }

  const groups = groupByProximity(findings, LINE_PROXIMITY);

  let consolidatedCount = 0;

  for (const group of groups.values()) {
    const agentsInGroup = new Set(group.map((f) => pyGet(f, 'agent', '').toLowerCase()));
    if (group.length < 2 || agentsInGroup.size < 2) continue;

    const file = pyGet(group[0], 'file', '');
    const bucket = lineBucket(group[0].line_start, LINE_PROXIMITY);
    const consolidationKey = `${file}:${bucket}`;

    const members = group.filter((f) => pyGet(f, 'id', ''));
    const primary = chooseGroupLead(members);
    for (const f of members) {
      f.consolidation_key = consolidationKey;
      f.consolidation_primary = f === primary;
      consolidatedCount += 1;
    }
  }

  return { findings, consolidatedCount };
}

export function tagFindings(findings) {
  let mainCount = 0, suggestionCount = 0;
  for (const finding of findings) {
    delete finding.consolidation_key;
    delete finding.consolidation_primary;
    const tier = finding.report_tag;
    if (tier === 'main') mainCount += 1;
    else suggestionCount += 1;
  }
  return { tagged: findings, mainCount, suggestionCount };
}

// Typed consequence includes demotion; sanitization precedes reporting.
export function applyFilterPipeline(findings, generatedAt) {
  const total = findings.length;

  normalizeFieldNames(findings);


  const allEliminated = [];

  const { kept: routed, eliminated: refuted } = applyVerdictRoutes(findings);
  allEliminated.push(...refuted);
  const { kept: afterInjection, eliminated: elimInjection } = applyInjectionFilter(routed);
  allEliminated.push(...elimInjection);
  const injectionsRemoved = elimInjection.length;
  const proseFieldsRemoved = Object.fromEntries(
    INJECTION_STRIPPED_PROSE_FIELDS.map((field) => [
      `${field}s_removed`,
      afterInjection.filter((f) => f[`${field}_removed_by`] === 'injection').length,
    ]),
  );
  const suggestedFixCodesRemoved = afterInjection.filter(
    (f) => f.suggested_fix_code_removed_by === 'injection',
  ).length;

  // Every survivor keeps the route computed from the closed verdict.
  const { tagged, mainCount, suggestionCount } = tagFindings(afterInjection);

  const promotedCount = tagged.filter((finding) => {
    const { row, kind } = findingPolicy(finding);
    return kind?.dimension && kind.dimension === finding.dimension && row.dimension !== finding.dimension;
  }).length;
  const reachabilityDemoted = [...tagged, ...allEliminated]
    .filter((f) => f.verdict?.consequence === 'latent').length;

  return {
    filtered: tagged,
    eliminated: allEliminated,
    stats: {
      total,
      injections_removed: injectionsRemoved,
      // Derive prose-removal stats from the field list so extending it needs no second declaration.
      ...proseFieldsRemoved,
      suggested_fix_codes_removed: suggestedFixCodesRemoved,
      reachability_demoted: reachabilityDemoted,
      test_analyzer_promoted: promotedCount,
      tagged_main: mainCount,
      tagged_suggestion: suggestionCount,
    },
    generated_at: generatedAt,
  };
}
