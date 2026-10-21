import { INT_POLICY, coerceInt, asText, asConfidence, deepFreeze } from './wire.js';
import { FIELD_RENAMES, SEVERITY_ORDER, INJECTION_STRIPPED_PROSE_FIELDS, AGENT_BUG_DETECTOR } from './registry.js';
import { AGENT_TEST_ANALYZER, AGENT_SECURITY_REVIEWER, AGENT_CONVENTIONS } from './registry.js';
import { CORE_DIMENSIONS, SUGGESTION_DIMENSIONS } from './registry.js';
import { MAIN_REPORT_AGENTS, SUGGESTION_AGENTS, COMMENT_ACCURACY_DIMENSIONS } from './registry.js';
import { cfgGet, configForFile, applyExclusions } from './reviewConfig.js';
import { applyInjectionFilter } from './injection.js';

const FUNCTIONAL_VIOLATION_KEYWORDS =
  /\bcrash\b|\bdata[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+loss\b|\bsilent(?:ly)?\b|\bincorrect\b|\bwrong\b|\bfail(?:s|ure)?\b|\bruntime[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+error\b|\bexception\b|\bpanic\b|\bundefined[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+behavio(?:u)?r\b/i;

const TYPE_SAFETY_BUG_KEYWORDS =
  /\bruntime\b|\bcastexception\b|\btype[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+error\b|\bclasscastexception\b|\bnull[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+pointer\b|\bnullpointer\b|\btype[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+mismatch\b/i;

// Promote test findings for existing correctness failures, while keeping coverage gaps as suggestions.
const TEST_CORRECTNESS_PATTERNS = deepFreeze([
  /\brace[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+condition\b/i,
  /\balways[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+pass(?:es)?\b/i,
  /\balways[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]pass(?:es)?\b/i,
  /\bnever[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+fail(?:s)?\b/i,
  /\bvacuous(?:ly)?\b/i,
  /\btautolog(?:y|ical)\b/i,
  /\bassert(?:ion)?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:is[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?never[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+reached\b/i,
  /\bdeadlock\b/i,
  /\bdata[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+race\b/i,
  /\bthread[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:safety|unsafe|race)\b/i,
  /\btest[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:never[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?(?:actually[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?(?:verif|test|check)(?:s|ies)?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+nothing\b/i,
  /\bfalse[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+positive[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:test|assertion)\b/i,
  /\bincorrect(?:ly)?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:assert|verify|test)\b/i,
  /\bwrong[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:value|result|output)\b/i,
  /\blocal[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+variable[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:is[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?never[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:used|read)\b/i,
  /\bassert(?:s|ion)?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:on[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?(?:a[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?(?:local|copy|snapshot)\b/i,
  /\bcompares?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:wrong|incorrect|different)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+object\b/i,
  /\btest[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:does[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+not|doesn'?t)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:wait|join|block)\b/i,
  /\breader[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+thread[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+not[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+waited\b/i,
  /\bflaky[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+test\b/i,
  /\bassertion[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+always[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:true|passes?|succeed)\b/i,
  /\bassert(?:s|ion)?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:is[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?always[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:true|pass(?:es?)?|succeed)\b/i,
  /\btest[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:is[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?always[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:true|pass(?:es?)?|succeed)\b/i,
  /\blogic[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+error\b/i,
  /\bincorrect[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:logic|behavior|behaviour|result)\b/i,
]);

const FILTER_DIMENSION_ROUTES = deepFreeze({
  test_coverage: { patterns: TEST_CORRECTNESS_PATTERNS },
  convention: { patterns: [FUNCTIONAL_VIOLATION_KEYWORDS] },
  type_design: { patterns: [TYPE_SAFETY_BUG_KEYWORDS] },
});

const FILTER_SUPPRESSION_RULES = deepFreeze([
  { agent: AGENT_BUG_DETECTOR, pattern: /\bintentional\b|\bby[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+design\b|\bexpected[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+behavior\b|\bdeliberate\b/,
    eliminatedBy: 'suppressed:intentional', subject: 'behaviour', conclusion: 'intentional' },
  { agent: AGENT_TEST_ANALYZER, pattern: /\bgenerated\b|\bscaffolding\b|\bauto[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]?generated\b|\bboilerplate\b/,
    eliminatedBy: 'suppressed:generated', subject: 'code', conclusion: 'generated/scaffolding' },
]);

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

const CONSENSUS_BOOST = 10;

const SINGLETON_PENALTY = 15;

function groupFilterFindings(findings, keyFor) {
  const groups = new Map();
  for (const finding of findings) {
    const key = keyFor(finding);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(finding);
  }
  return groups;
}

export function detectDisagreement(findings, rules = FILTER_SUPPRESSION_RULES) {
  const locationGroups = groupFilterFindings(findings, (finding) => JSON.stringify([
    pyGet(finding, 'file', ''), lineBucket(finding.line_start, 10),
  ]));

  const suppressedIds = new Set();
  const suppressed = [];
  const idKey = (f) => (('id' in f) ? f.id : f);

  for (const group of locationGroups.values()) {
    if (group.length < 2) continue;

    const agentMap = groupFilterFindings(group, (f) => pyGet(f, 'agent', '').toLowerCase());

    for (const row of rules) {
      if (!agentMap.has(row.agent) || !agentMap.has(AGENT_CONVENTIONS)) continue;
      for (const convFinding of agentMap.get(AGENT_CONVENTIONS)) {
        const convText = `${asText(convFinding.description)} ${asText(convFinding.title)}`.toLowerCase();
        if (!row.pattern.test(convText)) continue;
        for (const target of agentMap.get(row.agent)) {
          const fid = idKey(target);
          if (suppressedIds.has(fid)) continue;
          suppressedIds.add(fid);
          suppressed.push({
            ...target,
            eliminated_by: row.eliminatedBy,
            elimination_reason:
              `conventions-and-intent confirms ${row.subject} at ${pyGet(target, 'file', '?')}:` +
              `${pyGet(target, 'line_start', '?')} is ${row.conclusion}`,
          });
        }
        break;
      }
    }
  }

  const active = findings.filter((f) => !suppressedIds.has(idKey(f)));

  const consensusGroups = groupFilterFindings(active, (finding) => JSON.stringify([
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

      const dimension = pyGet(finding, 'dimension', '').toLowerCase();
      if (dimension && !CORE_DIMENSIONS.includes(dimension)) {
        const originalConf = asConfidence(finding.confidence);
        finding.confidence = Math.max(0, originalConf - SINGLETON_PENALTY);
        finding.singleton_penalty = true;
      }
    }
  }

  // Contradiction and security escalation require exact locations; nearby bucketed findings are insufficient.
  const locationGroupsActive = groupFilterFindings(active, (finding) => JSON.stringify([
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

  return { active, suppressed, boostedCount };
}

export function routeByDimension(finding, routes = FILTER_DIMENSION_ROUTES) {
  const dimension = pyGet(finding, 'dimension', '').toLowerCase();
  if (!dimension) return null;

  if (CORE_DIMENSIONS.includes(dimension)) return 'main';
  if (SUGGESTION_DIMENSIONS.includes(dimension)) return 'suggestion';
  if (!Object.hasOwn(routes, dimension)) return null;
  return findingMatchesPatterns(finding, routes[dimension].patterns) ? 'main' : 'suggestion';
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

export function consolidateCrossAgent(findings) {
  const LINE_PROXIMITY = 5;

  // Clear consolidation stamps before regrouping so a removed primary cannot leave stale group ownership.
  for (const f of findings) {
    if (f && typeof f === 'object') {
      delete f.consolidation_key;
      delete f.consolidation_primary;
    }
  }

  const winnerKey = (f) => {
    const dim = pyGet(f, 'dimension', '').toLowerCase();
    const isCore = CORE_DIMENSIONS.includes(dim) ? 1 : 0;
    const conf = asConfidence(f.confidence);
    // Ranking uses UTF-16 length; code-point counting would change winners for astral text.
    const descLen = asText(f.description).length;
    return [isCore, conf, descLen];
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
  const { findings: tagged, consolidatedCount } = consolidateCrossAgent(findings);

  let mainCount = 0;
  let suggestionCount = 0;

  for (const finding of tagged) {
    const agent = pyGet(finding, 'agent', '').toLowerCase();
    const dimension = finding.dimension ? String(finding.dimension).toLowerCase() : '';

    let destination;
    if (finding.demoted_by === 'reachability') {
      destination = 'suggestion';
      finding.routed_by = 'reachability';
    } else if (finding.removed_test) {
      destination = 'main';
      finding.routed_by = 'removed_test';
    } else {
      const dimRoute = routeByDimension(finding, routes);
      if (dimRoute !== null) {
        destination = dimRoute;
        if (dimRoute === 'suggestion') finding.routed_by = 'dimension';
      } else if (MAIN_REPORT_AGENTS.includes(agent)) {
        destination = 'main';
      } else if (agent === AGENT_CONVENTIONS) {
        destination = COMMENT_ACCURACY_DIMENSIONS.includes(dimension) ? 'suggestion' : 'main';
      } else if (SUGGESTION_AGENTS.includes(agent)) {
        if (agent === AGENT_TEST_ANALYZER && routes.test_coverage
          && findingMatchesPatterns(finding, routes.test_coverage.patterns)) {
          destination = 'main';
          finding.promoted_from = 'test-analyzer';
          finding.promotion_reason =
            'test-analyzer finding describes a functional correctness issue that exists today ' +
            '(not a missing-coverage gap)';
        } else {
          destination = 'suggestion';
        }
      } else {
        // Unknown agents go to main so an unrecognized producer cannot silently hide a finding.
        destination = 'main';
      }
    }

    finding.report_destination = destination;
    finding.report_tag = destination; // Artifact readers consume both routing names.
    if (destination === 'main') mainCount += 1;
    else suggestionCount += 1;
  }

  return { tagged, consolidatedCount, mainCount, suggestionCount };
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

  const { active, suppressed: elimSuppressed, boostedCount: consensusBoosted } = detectDisagreement(afterInjection);
  allEliminated.push(...elimSuppressed);

  const { tagged, consolidatedCount, mainCount, suggestionCount } = tagFindings(active);

  const promotedCount = tagged.filter((f) => f.promoted_from === 'test-analyzer').length;
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
      cross_agent_consolidated: consolidatedCount,
      test_analyzer_promoted: promotedCount,
      tagged_main: mainCount,
      tagged_suggestion: suggestionCount,
    },
    generated_at: generatedAt,
  };
}
