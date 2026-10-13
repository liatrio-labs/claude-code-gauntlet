// Normalize, filter, consolidate, and tag findings for the workflow.
import { INT_POLICY, coerceInt, asText, asConfidence } from './wire.js';
import { FIELD_RENAMES, SEVERITY_ORDER, INJECTION_STRIPPED_PROSE_FIELDS, AGENT_BUG_DETECTOR, AGENT_CONVENTIONS, AGENT_TEST_ANALYZER, AGENT_SECURITY_REVIEWER, CONVENTIONS_AGENT } from './registry.js';
import { cfgGet, configForFile, applyExclusions } from './reviewConfig.js';
import { applyInjectionFilter } from './injection.js';

export function normalizeFieldNames(findings) {
  let normalizedCount = 0;
  for (const finding of findings) {
    let renamed = false;
    for (const [legacy, canonical] of Object.entries(FIELD_RENAMES)) {
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

// An absent confidence setting uses 55 for non-security findings and 70 for security findings; explicit confidence_threshold applies to both, while security_min_confidence applies only to security.
const DEFAULT_CONFIDENCE_THRESHOLD = 70;

const DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD = 55;

const DEFAULT_SECURITY_MIN_CONFIDENCE = 70;

const DEFAULT_SEVERITY_THRESHOLD = 'low';

const CONTESTATION_DROP_THRESHOLD = 25;

// --- Filter: confidence / severity threshold (with validator contestation) -

export function applyThresholdFilter(findings, config) {
  const kept = [];
  const eliminated = [];
  let contestedCount = 0;

  for (const finding of findings) {
    const fileConfig = configForFile(config, finding.file);
    const confidence = asConfidence(pyGet(finding, 'confidence', 0));
    let severity = (asText(pyGet(finding, 'severity', 'low')) || 'low').toLowerCase();
    const dimensions = finding.dimension ? [String(finding.dimension).toLowerCase()] : [];

    const isSecurity = dimensions.includes('security');
    let effectiveThreshold;
    if (isSecurity) {
      const minConf = cfgGet(fileConfig, 'security_min_confidence', DEFAULT_SECURITY_MIN_CONFIDENCE);
      effectiveThreshold = Math.min(cfgGet(fileConfig, 'confidence_threshold', DEFAULT_CONFIDENCE_THRESHOLD), minConf);
    } else {
      // Non-security config-absent fallback is 55 (iter 5), decoupled from the
      // security branch above (which keeps the 70 fallback via DEFAULT_CONFIDENCE_THRESHOLD).
      effectiveThreshold = cfgGet(fileConfig, 'confidence_threshold', DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD);
    }

    // Validator contestation check (V5-09C): strict `> 25`, not `>=` — an
    // exact 25-point drop does NOT contest.
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
      const severityThreshold = cfgGet(fileConfig, 'severity_threshold', DEFAULT_SEVERITY_THRESHOLD);
      const sevThresholdIdx = SEVERITY_ORDER.indexOf(severityThreshold);
      if (!SEVERITY_ORDER.includes(severity)) severity = 'low';
      const sevIdx = SEVERITY_ORDER.indexOf(severity);
      if (sevIdx > sevThresholdIdx) {
        eliminated.push({
          ...finding,
          eliminated_by: 'threshold',
          elimination_reason: `severity '${severity}' is below threshold '${SEVERITY_ORDER[sevThresholdIdx]}'`,
        });
        continue;
      }
    }

    kept.push(finding);
  }

  return { kept, eliminated, contestedCount };
}

// Demote validator findings whose claim is true only after a future change. The
// step stamps every matching finding, including one already at low severity, but
// never removes anything; applyThresholdFilter below applies the user's severity
// threshold to the demoted result.
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

// Disagreement detection, dimension routing, consolidation, and tagging.

function pyGet(obj, key, dflt) {
  return key in obj ? obj[key] : dflt;
}

export function pyRound(x) {
  const floor = Math.floor(x);
  const diff = x - floor;
  if (diff < 0.5) return floor;
  if (diff > 0.5) return floor + 1;
  return floor % 2 === 0 ? floor : floor + 1; // exact .5 -> nearest even
}

export function lineBucket(line, proximity) {
  const n = coerceInt(line, INT_POLICY.filter);
  if (n === null) return 0;
  return pyRound(n / proximity) * proximity;
}

const CONSENSUS_BOOST = 10;

const SINGLETON_PENALTY = 15;

// Core dimensions exempt from the singleton penalty (BF-15b). Textually
// identical to MAIN_DIMENSIONS below but kept as a SEPARATE constant --
// matching the source's own duplication (_CORE_DIMENSIONS vs
// _MAIN_DIMENSIONS) -- because the two lists answer different questions
// (singleton-penalty exemption vs always-route-to-main) that merely happen
// to coincide today.
const CORE_DIMENSIONS = new Set(['bug', 'security', 'cross_file_impact', 'intent']);

export function detectDisagreement(findings) {
  // Phase 1: group by (file, line_bucket(10)) for co-location checks.
  const locationGroups = new Map();
  for (const finding of findings) {
    const key = JSON.stringify([pyGet(finding, 'file', ''), lineBucket(pyGet(finding, 'line_start', 0), 10)]);
    if (!locationGroups.has(key)) locationGroups.set(key, []);
    locationGroups.get(key).push(finding);
  }

  const suppressedIds = new Set();
  const suppressed = [];
  const idKey = (f) => (('id' in f) ? f.id : f);

  for (const group of locationGroups.values()) {
    if (group.length < 2) continue;

    const agentMap = new Map();
    for (const f of group) {
      const agent = pyGet(f, 'agent', '').toLowerCase();
      if (!agentMap.has(agent)) agentMap.set(agent, []);
      agentMap.get(agent).push(f);
    }

    // Suppression rule 1: bug-detector + conventions-and-intent -> intentional.
    if (agentMap.has(AGENT_BUG_DETECTOR) && agentMap.has(AGENT_CONVENTIONS)) {
      for (const convFinding of agentMap.get(AGENT_CONVENTIONS)) {
        const convText = `${asText(pyGet(convFinding, 'description', ''))} ${asText(pyGet(convFinding, 'title', ''))}`.toLowerCase();
        if (/\bintentional\b|\bby[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+design\b|\bexpected[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+behavior\b|\bdeliberate\b/.test(convText)) {
          for (const bugFinding of agentMap.get(AGENT_BUG_DETECTOR)) {
            const fid = idKey(bugFinding);
            if (!suppressedIds.has(fid)) {
              suppressedIds.add(fid);
              suppressed.push({
                ...bugFinding,
                eliminated_by: 'suppressed:intentional',
                elimination_reason:
                  `conventions-and-intent confirms behaviour at ${pyGet(bugFinding, 'file', '?')}:` +
                  `${pyGet(bugFinding, 'line_start', '?')} is intentional`,
              });
            }
          }
          break;
        }
      }
    }

    // Suppression rule 2: test-analyzer + conventions-and-intent -> generated/scaffolding.
    if (agentMap.has(AGENT_TEST_ANALYZER) && agentMap.has(AGENT_CONVENTIONS)) {
      for (const convFinding of agentMap.get(AGENT_CONVENTIONS)) {
        const convText = `${asText(pyGet(convFinding, 'description', ''))} ${asText(pyGet(convFinding, 'title', ''))}`.toLowerCase();
        if (/\bgenerated\b|\bscaffolding\b|\bauto[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]?generated\b|\bboilerplate\b/.test(convText)) {
          for (const testFinding of agentMap.get(AGENT_TEST_ANALYZER)) {
            const fid = idKey(testFinding);
            if (!suppressedIds.has(fid)) {
              suppressedIds.add(fid);
              suppressed.push({
                ...testFinding,
                eliminated_by: 'suppressed:generated',
                elimination_reason:
                  `conventions-and-intent confirms code at ${pyGet(testFinding, 'file', '?')}:` +
                  `${pyGet(testFinding, 'line_start', '?')} is generated/scaffolding`,
              });
            }
          }
          break;
        }
      }
    }
  }

  const active = findings.filter((f) => !suppressedIds.has(idKey(f)));

  const consensusGroups = new Map();
  for (const finding of active) {
    const degraded = pyGet(finding, 'origin', '') === 'unknown';
    const key = JSON.stringify([pyGet(finding, 'file', ''), lineBucket(pyGet(finding, 'line_start', 0), 10), degraded]);
    if (!consensusGroups.has(key)) consensusGroups.set(key, []);
    consensusGroups.get(key).push(finding);
  }

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
        const originalConf = asConfidence(pyGet(finding, 'confidence', 0));
        finding.confidence = Math.min(originalConf + CONSENSUS_BOOST, 100);
      }
    } else {
      const finding = group[0];
      finding.consensus_count = 1;
      finding.consensus_boost = 0;
      if (!('corroborated_by' in finding)) finding.corroborated_by = [];

      const dimension = pyGet(finding, 'dimension', '').toLowerCase();
      if (dimension && !CORE_DIMENSIONS.has(dimension)) {
        const originalConf = asConfidence(pyGet(finding, 'confidence', 0));
        finding.confidence = Math.max(0, originalConf - SINGLETON_PENALTY);
        finding.singleton_penalty = true;
      }
    }
  }

  // Phase 4: contradiction and security escalation, keyed on the EXACT
  // (file, line_start) pair -- NOT bucketed, unlike phases 1 and 3 above.
  const locationGroupsActive = new Map();
  for (const finding of active) {
    const key = JSON.stringify([pyGet(finding, 'file', ''), pyGet(finding, 'line_start', 0)]);
    if (!locationGroupsActive.has(key)) locationGroupsActive.set(key, []);
    locationGroupsActive.get(key).push(finding);
  }

  for (const group of locationGroupsActive.values()) {
    if (group.length < 2) {
      if (!('contradiction' in group[0])) group[0].contradiction = false;
      if (!('security_escalation' in group[0])) group[0].security_escalation = false;
      continue;
    }

    const severities = new Set(group.map((f) => (asText(pyGet(f, 'severity', 'low')) || 'low').toLowerCase()));
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

  for (const finding of active) {
    if (!('consensus_count' in finding)) finding.consensus_count = 1;
    if (!('consensus_boost' in finding)) finding.consensus_boost = 0;
    if (!('corroborated_by' in finding)) finding.corroborated_by = [];
    if (!('contradiction' in finding)) finding.contradiction = false;
    if (!('security_escalation' in finding)) finding.security_escalation = false;
  }

  return { active, suppressed, boostedCount };
}

// --- Dimension-based routing (BF-15a) ---------------------------------------

const SUGGESTION_DIMENSIONS = new Set(['comment_accuracy', 'comment-accuracy']);

const MAIN_DIMENSIONS = new Set(['bug', 'security', 'cross_file_impact', 'intent']);

const CONDITIONAL_SUGGESTION_DIMENSIONS = new Set(['test_coverage', 'convention', 'type_design']);

// Keywords that promote convention/type_design findings from suggestion to main.
const FUNCTIONAL_VIOLATION_KEYWORDS =
  /\bcrash\b|\bdata[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+loss\b|\bsilent(?:ly)?\b|\bincorrect\b|\bwrong\b|\bfail(?:s|ure)?\b|\bruntime[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+error\b|\bexception\b|\bpanic\b|\bundefined[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+behavio(?:u)?r\b/i;

// Keywords that promote type_design findings specifically.
const TYPE_SAFETY_BUG_KEYWORDS =
  /\bruntime\b|\bcastexception\b|\btype[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+error\b|\bclasscastexception\b|\bnull[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+pointer\b|\bnullpointer\b|\btype[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+mismatch\b/i;

// Keyword patterns indicating a test-analyzer finding describes a functional
// correctness bug that EXISTS TODAY (vs. a coverage gap). Shared by routeByDimension's
// test_coverage branch and isTestCorrectnessFinding's promotion check.
const TEST_CORRECTNESS_PATTERNS = [
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
];

export function routeByDimension(finding) {
  const dimension = pyGet(finding, 'dimension', '').toLowerCase();
  if (!dimension) return null;

  if (MAIN_DIMENSIONS.has(dimension)) return 'main';
  if (SUGGESTION_DIMENSIONS.has(dimension)) return 'suggestion';

  if (CONDITIONAL_SUGGESTION_DIMENSIONS.has(dimension)) {
    const combined = `${asText(pyGet(finding, 'title', ''))}\n${asText(pyGet(finding, 'description', ''))}`;

    if (dimension === 'test_coverage') {
      return TEST_CORRECTNESS_PATTERNS.some((rx) => rx.test(combined)) ? 'main' : 'suggestion';
    }
    if (dimension === 'convention') {
      return FUNCTIONAL_VIOLATION_KEYWORDS.test(combined) ? 'main' : 'suggestion';
    }
    if (dimension === 'type_design') {
      return TYPE_SAFETY_BUG_KEYWORDS.test(combined) ? 'main' : 'suggestion';
    }
  }

  return null;
}

function isTestCorrectnessFinding(finding) {
  const combined = `${asText(pyGet(finding, 'title', ''))}\n${asText(pyGet(finding, 'description', ''))}`;
  return TEST_CORRECTNESS_PATTERNS.some((rx) => rx.test(combined));
}

// --- Proximity grouping + cross-agent dedup ---------------------------------

export function groupByProximity(findings, lineProximity = 5) {
  const groups = new Map();
  for (const finding of findings) {
    const key = JSON.stringify([pyGet(finding, 'file', ''), lineBucket(pyGet(finding, 'line_start', 0), lineProximity)]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(finding);
  }
  return groups;
}

export function consolidateCrossAgent(findings) {
  const LINE_PROXIMITY = 5;

  // Clear any stamps from a prior pass first: a re-run (applyChallenges, after
  // a group's primary is eliminated) must not leave survivors carrying a
  // consolidation_key/consolidation_primary from a group that no longer
  // qualifies.
  for (const f of findings) {
    if (f && typeof f === 'object') {
      delete f.consolidation_key;
      delete f.consolidation_primary;
    }
  }

  const winnerKey = (f) => {
    const dim = pyGet(f, 'dimension', '').toLowerCase();
    const isCore = CORE_DIMENSIONS.has(dim) ? 1 : 0;
    const conf = asConfidence(pyGet(f, 'confidence', 0));
    const descLen = asText(pyGet(f, 'description', '')).length;
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
    if (group.length < 2 || agentsInGroup.size < 2) continue; // no stamps

    const file = pyGet(group[0], 'file', '');
    const bucket = lineBucket(pyGet(group[0], 'line_start', 0), LINE_PROXIMITY);
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

// --- Tagging -----------------------------------------------------------

const MAIN_REPORT_AGENTS = new Set(['bug-detector', 'security-reviewer', 'cross-file-impact', 'type-design-analyzer']);

const SUGGESTION_AGENTS = new Set(['test-analyzer', 'code-simplifier']);

const COMMENT_ACCURACY_DIMENSIONS = new Set(['comment-accuracy', 'documentation', 'doc-accuracy']);

export function tagFindings(findings) {
  const { findings: tagged, consolidatedCount } = consolidateCrossAgent(findings);

  let mainCount = 0;
  let suggestionCount = 0;

  for (const finding of tagged) {
    const agent = pyGet(finding, 'agent', '').toLowerCase();
    const dimensions = finding.dimension ? new Set([String(finding.dimension).toLowerCase()]) : new Set();

    let destination;
    if (finding.demoted_by === 'reachability') {
      destination = 'suggestion';
      finding.routed_by = 'reachability';
    } else {
      const dimRoute = routeByDimension(finding);
      if (dimRoute !== null) {
        destination = dimRoute;
        if (dimRoute === 'suggestion') finding.routed_by = 'dimension';
      } else if (MAIN_REPORT_AGENTS.has(agent)) {
        destination = 'main';
      } else if (agent === CONVENTIONS_AGENT) {
        destination = [...dimensions].some((d) => COMMENT_ACCURACY_DIMENSIONS.has(d)) ? 'suggestion' : 'main';
      } else if (SUGGESTION_AGENTS.has(agent)) {
        if (agent === AGENT_TEST_ANALYZER && isTestCorrectnessFinding(finding)) {
          destination = 'main';
          finding.promoted_from = 'test-analyzer';
          finding.promotion_reason =
            'test-analyzer finding describes a functional correctness issue that exists today ' +
            '(not a missing-coverage gap)';
        } else {
          destination = 'suggestion';
        }
      } else {
        // Unknown agent -- conservative fallback: route to main.
        destination = 'main';
      }
    }

    finding.report_destination = destination;
    finding.report_tag = destination; // backward-compat alias
    if (destination === 'main') mainCount += 1;
    else suggestionCount += 1;
  }

  return { tagged, consolidatedCount, mainCount, suggestionCount };
}

// --- Pipeline composition ----------------------------------------------

// Compose the filter passes and return each outcome in pipeline order.
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
      // Spliced, not hand-listed: proseFieldsRemoved's keys/order are exactly
      // INJECTION_STRIPPED_PROSE_FIELDS's (Object.fromEntries over the list, in
      // order), so adding a field to that list is the only edit a future stat needs
      // -- no second key to add here.
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
