import { canonicalRange, lineBucket } from './location.js';
import { WIRE_SPACE, asText, asConfidence, deepFreeze } from './wire.js';
import { FIELD_RENAMES, SEVERITY_ORDER, INJECTION_STRIPPED_PROSE_FIELDS, AGENT_SECURITY_REVIEWER } from './registry.js';
import { DIMENSIONS, dimensionRow, findingPolicy } from './registry.js';
import { cfgGet, configForFile, applyExclusions } from './reviewConfig.js';
import { applyInjectionFilter } from './injection.js';

const FUNCTIONAL_VIOLATION_KEYWORDS =
  new RegExp(String.raw`\bcrash\b|\bdata${WIRE_SPACE}+loss\b|\bsilent(?:ly)?\b|\bincorrect\b|\bwrong\b|\bfail(?:s|ure)?\b|\bruntime${WIRE_SPACE}+error\b|\bexception\b|\bpanic\b|\bundefined${WIRE_SPACE}+behavio(?:u)?r\b`, 'i');

const FILTER_DIMENSION_ROUTES = deepFreeze({ convention: { patterns: [FUNCTIONAL_VIOLATION_KEYWORDS] } });

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

// Defaults deliberately admit lower-confidence non-security findings; security keeps the stricter floor.
// Explicit confidence settings can lower either floor.
export const FILTER_DEFAULTS = deepFreeze({
  confidenceThreshold: 70,
  nonsecurityConfidenceThreshold: 55,
  securityMinConfidence: 70,
  severityThreshold: 'low',
});

const CONTESTATION_DROP_THRESHOLD = 25;

export function applyThresholdFilter(findings, config, severityOrder = SEVERITY_ORDER) {
  const kept = [];
  const eliminated = [];
  let contestedCount = 0;

  for (const finding of findings) {
    const fileConfig = configForFile(config, finding.file);
    const confidence = asConfidence(pyGet(finding, 'confidence', 0));
    let severity = (asText(finding.severity) || 'low').toLowerCase();
    const dimensions = finding.dimension ? [String(finding.dimension).toLowerCase()] : [];

    const isSecurity = dimensions.includes('security');
    let effectiveThreshold;
    if (isSecurity) {
      const minConf = cfgGet(fileConfig, 'security_min_confidence', FILTER_DEFAULTS.securityMinConfidence);
      effectiveThreshold = Math.min(cfgGet(fileConfig, 'confidence_threshold', FILTER_DEFAULTS.confidenceThreshold), minConf);
    } else {
      effectiveThreshold = cfgGet(fileConfig, 'confidence_threshold', FILTER_DEFAULTS.nonsecurityConfidenceThreshold);
    }

    // Only a drop beyond the contestation boundary bypasses thresholds; equality is not contestation.
    let isContested = false;
    const originalConfidence = 'original_confidence' in finding ? finding.original_confidence : undefined;
    if (originalConfidence !== undefined && originalConfidence !== null) {
      const drop = originalConfidence - confidence;
      if (drop > CONTESTATION_DROP_THRESHOLD) {
        isContested = true;
        contestedCount += 1;
        finding.contested = true;
        finding.contestation_drop = drop;
        finding.contestation_reason =
          `validator dropped confidence by ${drop} points (original: ${originalConfidence}, current: ${confidence})`;
      }
    }

    if (!isContested && confidence < effectiveThreshold) {
      eliminated.push({
        ...finding,
        eliminated_by: 'threshold',
        elimination_reason: `confidence ${confidence} < threshold ${effectiveThreshold}`,
      });
      continue;
    }

    if (!isContested) {
      const severityThreshold = cfgGet(fileConfig, 'severity_threshold', FILTER_DEFAULTS.severityThreshold);
      const sevThresholdIdx = severityOrder.indexOf(severityThreshold);
      if (!severityOrder.includes(severity)) severity = 'low';
      const sevIdx = severityOrder.indexOf(severity);
      if (sevIdx > sevThresholdIdx) {
        eliminated.push({
          ...finding,
          eliminated_by: 'threshold',
          elimination_reason: `severity '${severity}' is below threshold '${severityOrder[sevThresholdIdx]}'`,
        });
        continue;
      }
    }

    kept.push(finding);
  }

  return { kept, eliminated, contestedCount };
}

// Demote future-change claims before threshold filtering so the severity threshold sees the demoted value.
const REACHABILITY_DEMOTION_REASON =
  'validator found no code path that reaches this issue without a future change';

export function applyReachabilityDemotion(findings) {
  let demotedCount = 0;
  for (const finding of findings) {
    if (finding.reachability !== 'future_change_only') continue;
    if (finding.severity !== 'low') {
      if ('severity' in finding) finding.original_severity = finding.severity;
      finding.severity = 'low';
    }
    finding.demoted_by = 'reachability';
    finding.demotion_reason = REACHABILITY_DEMOTION_REASON;
    demotedCount += 1;
  }
  return { findings, demotedCount };
}

// Present null values remain data: defaulting them would merge distinct grouping keys and IDs.
function pyGet(obj, key, dflt) {
  return key in obj ? obj[key] : dflt;
}

const CONSENSUS_BOOST = 10;

const SINGLETON_PENALTY = 15;

function groupFilterFindings(findings, keyFor) {
  const groups = new Map();
  for (const finding of findings) {
    const key = canonicalRange(finding) ? keyFor(finding) : finding;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(finding);
  }
  return groups;
}

export function detectDisagreement(findings) {
  for (const f of findings) {
    for (const key of ['consensus_count', 'consensus_boost', 'corroborated_by', 'singleton_penalty', 'contradiction', 'security_escalation', 'escalation_note']) delete f[key];
  }

  const consensusGroups = groupFilterFindings(findings, (finding) => JSON.stringify([
    pyGet(finding, 'file', ''), lineBucket(finding.line_start, 10), pyGet(finding, 'origin', '') === 'unknown',
  ]));

  let boostedCount = 0;
  for (const group of consensusGroups.values()) {
    const count = group.length;
    const agentsInGroup = group.filter((f) => f.agent).map((f) => f.agent);

    if (count > 1) {
      boostedCount += count;
      for (const finding of group) {
        const thisAgent = pyGet(finding, 'agent', '');
        const otherAgents = agentsInGroup.filter((a) => a !== thisAgent);
        finding.consensus_count = count;
        finding.consensus_boost = CONSENSUS_BOOST;
        finding.corroborated_by = otherAgents;
        const originalConf = asConfidence(finding.confidence);
        finding.confidence = Math.min(originalConf + CONSENSUS_BOOST, 100);
      }
    } else {
      const finding = group[0];
      finding.consensus_count = 1;
      finding.consensus_boost = 0;
      if (!('corroborated_by' in finding)) finding.corroborated_by = [];

      if (finding.dimension && !findingPolicy(finding).core) {
        const originalConf = asConfidence(finding.confidence);
        finding.confidence = Math.max(0, originalConf - SINGLETON_PENALTY);
        finding.singleton_penalty = true;
      }
    }
  }

  // Contradiction and security escalation require exact locations; nearby bucketed findings are insufficient.
  const locationGroupsActive = groupFilterFindings(findings, (finding) => JSON.stringify([
    pyGet(finding, 'file', ''), pyGet(finding, 'line_start', 0),
  ]));

  for (const group of locationGroupsActive.values()) {
    if (group.length < 2) {
      if (!('contradiction' in group[0])) group[0].contradiction = false;
      if (!('security_escalation' in group[0])) group[0].security_escalation = false;
      continue;
    }

    const severities = new Set(group.map((f) => (asText(f.severity) || 'low').toLowerCase()));
    const agentsHere = new Set(group.map((f) => pyGet(f, 'agent', '').toLowerCase()));

    const hasContradiction = severities.has('critical') && severities.has('low');
    const hasSecurityEscalation =
      agentsHere.has(AGENT_SECURITY_REVIEWER) && agentsHere.size > 1 && severities.has('low');

    for (const finding of group) {
      finding.contradiction = hasContradiction;
      finding.security_escalation = hasSecurityEscalation;
      if (hasSecurityEscalation && pyGet(finding, 'agent', '').toLowerCase() === AGENT_SECURITY_REVIEWER) {
        finding.escalation_note =
          'Kept: security-reviewer finding retained despite conflicting low-severity ' +
          'signal from another agent (security escalation rule)';
      }
    }
  }

  return { active: findings, boostedCount };
}

export function routeByDimension(finding, routes = FILTER_DIMENSION_ROUTES) {
  const dimension = pyGet(finding, 'dimension', '').toLowerCase();
  if (!dimension) return null;
  if (!dimensionRow(finding.origin_dimension) && !dimensionRow(dimension)) return null;

  const policy = findingPolicy(finding);
  if (policy.row?.functionalRouting && Object.hasOwn(routes, dimension)) {
    return findingMatchesPatterns(finding, routes[dimension].patterns) ? 'main' : 'suggestion';
  }
  return policy.tier ?? null;
}

function findingMatchesPatterns(finding, patterns) {
  const combined = `${asText(finding.title)}\n${asText(finding.description)}`;
  return patterns.some((rx) => rx.test(combined));
}

function groupByProximity(findings, lineProximity) {
  return groupFilterFindings(findings, (finding) => JSON.stringify([
    pyGet(finding, 'file', ''), lineBucket(finding.line_start, lineProximity),
  ]));
}

export function consolidateCrossAgent(findings, prefer = () => false) {
  const LINE_PROXIMITY = 5;

  // Clear consolidation stamps before regrouping so a removed primary cannot leave stale group ownership.
  for (const f of findings) {
    if (f && typeof f === 'object') {
      delete f.consolidation_key;
      delete f.consolidation_primary;
    }
  }

  const winnerKey = (f) => {
    const isCore = findingPolicy(f).core ? 1 : 0;
    const conf = asConfidence(f.confidence);
    // Ranking uses UTF-16 length; code-point counting would change winners for astral text.
    const descLen = asText(f.description).length;
    return [prefer(f) ? 1 : 0, isCore, conf, descLen];
  };

  const compareWinnerDesc = (a, b) => {
    const ka = winnerKey(a);
    const kb = winnerKey(b);
    for (let i = 0; i < ka.length; i += 1) {
      if (ka[i] !== kb[i]) return kb[i] - ka[i];
    }
    return 0; // tie -> preserve original order (stable sort)
  };

  const groups = groupByProximity(findings, LINE_PROXIMITY);

  let consolidatedCount = 0;

  for (const group of groups.values()) {
    const agentsInGroup = new Set(group.map((f) => pyGet(f, 'agent', '').toLowerCase()));
    if (group.length < 2 || agentsInGroup.size < 2) continue;

    const file = pyGet(group[0], 'file', '');
    const bucket = lineBucket(group[0].line_start, LINE_PROXIMITY);
    const consolidationKey = `${file}:${bucket}`;

    const ranked = [...group].sort(compareWinnerDesc);
    const primary = ranked.find((f) => pyGet(f, 'id', ''));

    for (const f of group) {
      if (!pyGet(f, 'id', '')) continue; // id-less findings stay unstamped
      f.consolidation_key = consolidationKey;
      f.consolidation_primary = f === primary;
      consolidatedCount += 1;
    }
  }

  return { findings, consolidatedCount };
}

export function tagFindings(findings, routes = FILTER_DIMENSION_ROUTES) {

  let mainCount = 0;
  let suggestionCount = 0;

  for (const finding of findings) {
    delete finding.consolidation_key;
    delete finding.consolidation_primary;
    const agent = pyGet(finding, 'agent', '').toLowerCase();

    let destination;
    if (finding.demoted_by === 'reachability') {
      destination = 'suggestion';
      finding.routed_by = 'reachability';
    } else {
      const dimRoute = routeByDimension(finding, routes);
      if (dimRoute !== null) {
        destination = dimRoute;
        if (dimRoute === 'suggestion') finding.routed_by = 'dimension';
      } else {
        const rows = DIMENSIONS.filter((row) => row.agentType.split(':').pop() === agent);
        destination = (rows.find((row) => row.agentFallback) || rows[0])?.defaultTier || 'main';
      }
    }

    finding.report_destination = destination;
    finding.report_tag = destination; // Artifact readers consume both routing names.
    if (destination === 'main') mainCount += 1;
    else suggestionCount += 1;
  }

  return { tagged: findings, mainCount, suggestionCount };
}

// Reachability demotion precedes thresholds; injection sanitization precedes corroboration and routing.
export function applyFilterPipeline(findings, config, exclusionPatterns, generatedAt) {
  const total = findings.length;

  normalizeFieldNames(findings);

  applyReachabilityDemotion(findings);

  const allEliminated = [];

  const { kept: afterThreshold, eliminated: elimThreshold, contestedCount } = applyThresholdFilter(findings, config);
  allEliminated.push(...elimThreshold);
  const passedThreshold = afterThreshold.length;

  const { kept: afterExclusions, eliminated: elimExclusions } = applyExclusions(
    afterThreshold, exclusionPatterns, config,
  );
  allEliminated.push(...elimExclusions);
  const exclusionsRemoved = elimExclusions.length;

  const { kept: afterInjection, eliminated: elimInjection } = applyInjectionFilter(afterExclusions);
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

  // Only eligible findings may corroborate; disagreement adjusts ranking after thresholds.
  const { active, boostedCount: consensusBoosted } = detectDisagreement(afterInjection);
  const { tagged, mainCount, suggestionCount } = tagFindings(active);

  const promotedCount = tagged.filter((finding) => {
    const { row, kind } = findingPolicy(finding);
    return kind?.dimension && kind.dimension === finding.dimension && row.dimension !== finding.dimension;
  }).length;
  const dimensionRouted = tagged.filter((f) => f.routed_by === 'dimension').length;
  const reachabilityDemoted = [...tagged, ...allEliminated]
    .filter((f) => f.demoted_by === 'reachability').length;
  const singletonPenalized = [...tagged, ...allEliminated].filter((f) => f.singleton_penalty).length;

  return {
    filtered: tagged,
    eliminated: allEliminated,
    stats: {
      total,
      passed_threshold: passedThreshold,
      contested_count: contestedCount,
      exclusions_removed: exclusionsRemoved,
      injections_removed: injectionsRemoved,
      // Derive prose-removal stats from the field list so extending it needs no second declaration.
      ...proseFieldsRemoved,
      suggested_fix_codes_removed: suggestedFixCodesRemoved,
      consensus_boosted: consensusBoosted,
      singleton_penalized: singletonPenalized,
      dimension_routed: dimensionRouted,
      reachability_demoted: reachabilityDemoted,
      test_analyzer_promoted: promotedCount,
      tagged_main: mainCount,
      tagged_suggestion: suggestionCount,
    },
    generated_at: generatedAt,
  };
}
