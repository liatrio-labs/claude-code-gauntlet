// filterFindings.js — JS twin of scripts/filter_findings.py (Phase 6 filtering).
// Part 1 (this task): normalize / thresholds / injection / exclusions / REVIEW.md
// parsing. Part 2 (Task 5): disagreement detection, tagging, dedupCrossAgent,
// applyFilterPipeline — appended to this same file.

// --- Field normalization (BF-14) --------------------------------------------

const FIELD_RENAMES = { body: 'description', line: 'line_start', blame_tag: 'origin' };

// Port of normalize_field_names. Mutates `findings` in place; when BOTH legacy
// and canonical keys are present, canonical wins and the legacy key is LEFT IN
// PLACE (asymmetric — a rename only fires when the canonical key is absent).
// Returns the count of findings that had at least one field renamed (mirrors
// the Python return value; the part-1 recorder does not surface it, but the
// mutation + count semantics both match the original).
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

// --- REVIEW.md parser ---------------------------------------------------

// Single owner of SEVERITY_ORDER for the whole bundle: applyChallenges.js imports
// this rather than re-declaring it. In the concatenated bundle build.js strips the
// `export` keyword, so two top-level `const SEVERITY_ORDER` declarations (one here,
// one there) collided as "already been declared" — a runtime SyntaxError. filterFindings.js
// is emitted before applyChallenges.js (build.js ORDER), so the export is in scope there.
export const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low'];
// DEFAULT_CONFIDENCE_THRESHOLD is the Python-parity-pinned default: parseReviewMd
// substitutes it when REVIEW.md omits confidence_threshold (the parse_review_md
// `missing_defaults` fixture pins 70), and the SECURITY branch of
// applyThresholdFilter uses it so an unconfigured security bar stays min(70,70)=70.
// The NON-security runtime default is decoupled below (hill-climb iter 5): when the
// skill's reviewConfig omits confidence_threshold, non-security dimensions filter at
// 55 (rescues conf-55-68 goldens) while security is unchanged at 70. Only the
// config-absent fallback differs; an EXPLICIT confidence_threshold (user REVIEW.md
// override) still applies to BOTH branches, so parity fixtures — all of which pass an
// explicit config — are untouched, and REVIEW.md override semantics stay intact.
const DEFAULT_CONFIDENCE_THRESHOLD = 70;
const DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD = 55;
const DEFAULT_SECURITY_MIN_CONFIDENCE = 70;
const DEFAULT_SEVERITY_THRESHOLD = 'low';
const CONTESTATION_DROP_THRESHOLD = 25;

// Approximates Python `config.get(key, default)`, which substitutes the
// default ONLY when the key is absent (a present `None` value is returned
// as-is, not replaced). This helper is deliberately broader -- it also
// substitutes on an explicit `null` -- since JS has no equivalent to Python
// silently returning `None` through arithmetic; the config fields this backs
// (confidence_threshold, severity_threshold, etc.) are never legitimately
// null in practice, so the divergence has no observable effect. It never
// substitutes on other falsy values (0, '', false), matching Python.
function cfgGet(config, key, fallback) {
  const v = config ? config[key] : undefined;
  return v === undefined || v === null ? fallback : v;
}

// Port of parse_review_md. Python reads a file by path; this twin takes the
// REVIEW.md TEXT directly (the workflow runtime has no disk access).
export function parseReviewMd(text) {
  const config = {
    confidence_threshold: DEFAULT_CONFIDENCE_THRESHOLD,
    security_min_confidence: DEFAULT_SECURITY_MIN_CONFIDENCE,
    severity_threshold: DEFAULT_SEVERITY_THRESHOLD,
    ignore: [],
  };

  if (text === undefined || text === null) return config;

  // Two block patterns tried in order: fenced ```yaml block, then HTML comment
  // block. DOTALL is `[\s\S]*?` (JS regex has no /s-independent dotall flag
  // pre-ES2018 semantics issue here — `[\s\S]` is used for portability).
  const blockPatterns = [
    /```(?:yaml|)[\s]*#?\s*code-gauntlet(?:[^\n]*)?\n([\s\S]*?)```/i,
    /<!--\s*code-gauntlet-config\s*\n([\s\S]*?)-->/i,
    // Legacy pre-rename markers -- same current-before-legacy order as the Python
    // twin's block_patterns so both pick the same block when several match.
    /```(?:yaml|)[\s]*#?\s*deep-review(?:[^\n]*)?\n([\s\S]*?)```/i,
    /<!--\s*deep-review-config\s*\n([\s\S]*?)-->/i,
  ];

  let blockText = '';
  for (const pattern of blockPatterns) {
    const m = pattern.exec(text);
    if (m) {
      blockText = m[1];
      break;
    }
  }

  // Whole-file fallback when no block found (Python logs a warning here; the
  // return value is unaffected so the JS twin has nothing to emit).
  if (!blockText) blockText = text;

  let m = /confidence_threshold\s*[:=]\s*(\d+)/.exec(blockText);
  if (m) config.confidence_threshold = parseInt(m[1], 10);

  m = /security_min_confidence\s*[:=]\s*(\d+)/.exec(blockText);
  if (m) config.security_min_confidence = parseInt(m[1], 10);

  m = /severity_threshold\s*[:=]\s*(critical|high|medium|low)/i.exec(blockText);
  if (m) config.severity_threshold = m[1].toLowerCase();

  // ignore: consecutive "-"-led lines, indentation-tolerant (spaces or tabs).
  const ignoreSection = /ignore\s*:\s*\n((?:[ \t]*-[^\n]*\n?)+)/.exec(blockText);
  if (ignoreSection) {
    for (const line of ignoreSection[1].split('\n')) {
      const item = line.replace(/^\s*-\s*/, '').trim();
      if (item) config.ignore.push(item);
    }
  }

  return config;
}

// --- Filter: confidence / severity threshold (with validator contestation) -

// Port of apply_threshold_filter. Security effective threshold is literally
// Math.min(confidence_threshold, security_min_confidence) — faithful to the
// Python `min()` call even though it makes the security bar the LOWER of the
// two configured numbers (pinned by parity-map §3; not a naming bug to fix
// in a port). The one intentional v3 divergence from Python is the CONFIG-ABSENT
// fallback: non-security dimensions default to 55 (DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD),
// security stays at 70. An explicit confidence_threshold in `config` overrides
// both branches identically, so this divergence is invisible to the parity
// fixtures (all of which pass an explicit config).
export function applyThresholdFilter(findings, config) {
  const kept = [];
  const eliminated = [];
  let contestedCount = 0;

  const severityThreshold = cfgGet(config, 'severity_threshold', DEFAULT_SEVERITY_THRESHOLD);
  const sevThresholdIdx = SEVERITY_ORDER.indexOf(severityThreshold);

  for (const finding of findings) {
    const confidence = 'confidence' in finding ? finding.confidence : 0;
    let severity = ('severity' in finding ? finding.severity : 'low').toLowerCase();
    const dimensions = finding.dimension ? [String(finding.dimension).toLowerCase()] : [];

    const isSecurity = dimensions.includes('security');
    let effectiveThreshold;
    if (isSecurity) {
      const minConf = cfgGet(config, 'security_min_confidence', DEFAULT_SECURITY_MIN_CONFIDENCE);
      effectiveThreshold = Math.min(cfgGet(config, 'confidence_threshold', DEFAULT_CONFIDENCE_THRESHOLD), minConf);
    } else {
      // Non-security config-absent fallback is 55 (iter 5), decoupled from the
      // security branch above (which keeps the 70 fallback via DEFAULT_CONFIDENCE_THRESHOLD).
      effectiveThreshold = cfgGet(config, 'confidence_threshold', DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD);
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

// --- Filter: injection artifact detection -----------------------------------

const INJECTION_TITLE_PATTERNS = [
  /\bTODO\b/i,
  /\bFIXME\b/i,
  /\bPlaceholder\b/i,
  /\bExample finding\b/i,
  /\bSample finding\b/i,
  /\btest finding\b/i,
  /\bdemo finding\b/i,
];

const INJECTION_BODY_PATTERNS = [/<finding>/i, /<example>/i, /\[\s*INSERT\s*\]/i, /lorem ipsum/i];

const INJECTION_SHELL_PATTERNS = [
  /\brm\s+-[rf]/i,
  /\bcurl\s+https?:\/\//i,
  /\bwget\s+https?:\/\//i,
  /\bgit\s+push\b/i,
  /\bgh\s+api\b/i,
];

const INJECTION_URL_PATTERNS = [
  /https?:\/\/[^\s)>"']{20,}/i,
  /\bvisit\s+https?:\/\//i,
  /\bdownload from\s+https?:\/\//i,
  /\bnavigate to\b/i,
];

const INJECTION_ENCODED_PATTERNS = [/\b[A-Za-z0-9+/]{40,}={0,2}\b/i, /(?<!\w)(?:0x)?[0-9a-fA-F]{32,}(?!\w)/i];

const INJECTION_BYPASS_PATTERNS = [
  /\bskip\s+review\b/i,
  /\bauto[-\s]?approve\b/i,
  /\bbypass\s+(?:security\s+)?controls?\b/i,
  /\bbypass\s+(?:the\s+)?(?:auth|authentication|authorization)\b/i,
  /\bdisable\s+(?:auth|authentication|authorization)\b/i,
  /\bmark\s+(?:this\s+)?(?:finding\s+)?as\s+safe\b/i,
  /\bapprove\s+(?:this|the)\s+(?:PR|pull\s+request|change)\b/i,
];

const INJECTION_INSTRUCTIONAL_PATTERNS = [
  /\byou\s+should\s+run\b/i,
  /\bexecute\s+the\s+following\b/i,
  /\brun\s+this\s+command\b/i,
  /\bplease\s+run\b/i,
  /\bpaste\s+(?:this|the\s+following)\s+into\s+(?:your\s+)?terminal/i,
  /\bcopy\s+and\s+paste\s+the\s+following\b/i,
];

const INJECTION_VULN_INTRO_PATTERNS = [
  /\badd\s+eval\s*\(/i,
  /\buse\s+eval\s*\(/i,
  /\bdisable\s+(?:CORS|CSP|content[-\s]security[-\s]policy)\b/i,
  /\bdisable\s+(?:CSRF|csrf)\s+(?:protection|check|token)\b/i,
  /\ballow\s+all\s+origins\b/i,
  /\bset\s+secure\s+to\s+false\b/i,
  /\bdisable\s+(?:TLS|SSL|HTTPS)\s+(?:verification|validation)\b/i,
  /\bskip\s+(?:certificate|cert)\s+(?:verification|validation)\b/i,
  /\bdisable\s+security\s+(?:check|feature|control)\b/i,
];

const MIN_BODY_WORDS = 10;
const HIGH_CONFIDENCE_THRESHOLD = 85;

// Port of _count_words: whitespace-split word count, 0 for blank/whitespace-only text.
function countWords(text) {
  const t = (text || '').trim();
  return t ? t.split(/\s+/).length : 0;
}

// Port of _first_match: the pattern SOURCE of the first regex that matches, or null.
function firstMatch(patterns, text) {
  for (const rx of patterns) {
    if (rx.test(text)) return rx.source;
  }
  return null;
}

// Port of apply_injection_filter. All 10 heuristics, in the same order as the
// Python original so `reasons[0]` (used in the stderr-equivalent warning, not
// asserted here) lines up. Heuristic #10 (duplicate signature) is STATEFUL
// across the input list — the FIRST (title,file,line_start) occurrence
// survives, later ones are flagged — so caller input order is load-bearing.
export function applyInjectionFilter(findings) {
  const kept = [];
  const eliminated = [];
  const seenSignatures = new Map();

  for (const finding of findings) {
    const title = finding.title || '';
    const description = finding.description || '';
    const filepath = finding.file || '';
    const confidence = 'confidence' in finding ? finding.confidence : 0;
    const combined = `${title}\n${description}`;

    const reasons = [];

    let m = firstMatch(INJECTION_SHELL_PATTERNS, combined);
    if (m) reasons.push(`contains shell command pattern: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_URL_PATTERNS, description);
    if (m) reasons.push(`description contains visit-URL pattern: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_ENCODED_PATTERNS, description);
    if (m) reasons.push(`description contains encoded payload pattern: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_BYPASS_PATTERNS, description);
    if (m) reasons.push(`description contains bypass/auto-approve instruction: ${JSON.stringify(m)}`);

    const wordCount = countWords(description);
    if (wordCount < MIN_BODY_WORDS && confidence >= HIGH_CONFIDENCE_THRESHOLD) {
      reasons.push(`suspiciously short description (${wordCount} words) with high confidence (${confidence})`);
    }

    m = firstMatch(INJECTION_INSTRUCTIONAL_PATTERNS, description);
    if (m) reasons.push(`description uses instructional tone: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_VULN_INTRO_PATTERNS, description);
    if (m) reasons.push(`description recommends introducing vulnerability: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_TITLE_PATTERNS, title);
    if (m) reasons.push(`title matches placeholder pattern: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_BODY_PATTERNS, description);
    if (m) reasons.push(`description matches injection marker: ${JSON.stringify(m)}`);

    if (!filepath || /<.*?>|\{.*?\}/.test(filepath)) {
      reasons.push(`file path is empty or contains template markers: ${JSON.stringify(filepath)}`);
    }

    // Signature key: mirrors Python's (title.lower().strip(), file, line_start)
    // tuple key via JSON.stringify of the equivalent array -- structural equality,
    // immune to collisions a hand-rolled string-concatenation key could hit.
    const sig = JSON.stringify([title.toLowerCase().trim(), filepath, finding.line_start]);
    if (seenSignatures.has(sig)) {
      reasons.push(`duplicate of finding ${JSON.stringify(seenSignatures.get(sig))}`);
    } else {
      seenSignatures.set(sig, finding.id !== undefined && finding.id !== null ? finding.id : title);
    }

    if (reasons.length) {
      eliminated.push({ ...finding, eliminated_by: 'injection', elimination_reason: reasons.join('; ') });
    } else {
      kept.push(finding);
    }
  }

  return { kept, eliminated };
}

// --- Exclusions loader -------------------------------------------------------

// Port of load_exclusions. Python reads a file by path; this twin takes the
// exclusions markdown TEXT directly. A fenced code block wins if present
// (returns immediately on the first one found); otherwise falls back to
// bullet-list ("- " / "* ") items scanned line by line.
export function loadExclusions(text) {
  if (text === undefined || text === null) return [];

  const patterns = [];

  const blockMatch = /```[^\n]*\n([\s\S]*?)```/.exec(text);
  if (blockMatch) {
    for (const rawLine of blockMatch[1].split('\n')) {
      const line = rawLine.trim();
      if (line && !line.startsWith('#')) patterns.push(line);
    }
    return patterns;
  }

  // Split on \r?\n (not plain '\n'): on CRLF input, a plain split leaves a
  // trailing \r on every line, and `.` in the regex below excludes line
  // terminators (including \r), so `(.+)$` could never bridge to the
  // (non-multiline) end-of-string anchor -- every bullet would silently fail
  // to match. Python's splitlines() (used by load_exclusions) strips \r\n as
  // a single line break, so this normalizes JS to the same behavior.
  for (const line of text.split(/\r?\n/)) {
    const m = /^\s*[-*]\s+(.+)$/.exec(line);
    if (m) patterns.push(m[1].trim());
  }

  return patterns;
}

// re.escape equivalent: escape every regex metacharacter so the pattern is
// searched as a literal substring.
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Port of apply_exclusions. First pattern (in list order) whose literal,
// case-insensitive substring appears in "title\ndescription" wins.
// --- Part 2: disagreement detection / dimension routing / dedup / tag ------
// Port of scripts/filter_findings.py:552-1236 (detect_disagreement through
// tag_findings) plus main()'s pipeline composition (1243-1397).

// Python's dict.get(key, default) semantics: substitutes `dflt` ONLY when
// `key` is absent from `obj`. A present `null`/`undefined` value passes
// through untouched -- matching Python, a subsequent `.toLowerCase()` on it
// throws the same way `finding.get(...).lower()` throws on `None`. This is
// the strict counterpart to `cfgGet` above (which also substitutes on an
// explicit `null`, a deliberately broader rule scoped to config lookups).
function pyGet(obj, key, dflt) {
  return key in obj ? obj[key] : dflt;
}

// Python round() is banker's rounding (half-to-even); JS Math.round is half-up.
// detect_disagreement buckets on round(line/10)*10, so line_start in {5,15,25,...}
// diverges unless we replicate half-to-even. (parity-map highest-risk fixture.)
export function pyRound(x) {
  const floor = Math.floor(x);
  const diff = x - floor;
  if (diff < 0.5) return floor;
  if (diff > 0.5) return floor + 1;
  return floor % 2 === 0 ? floor : floor + 1; // exact .5 -> nearest even
}

// Python int(x) truncation semantics, for the value types plausibly found on
// line_start (JSON number, numeric string, or missing/null/other -> error).
// Returns null on "would raise" so callers can fall back to 0 exactly like
// Python's `except (TypeError, ValueError): return 0`.
function pyIntOrNull(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? Math.trunc(value) : null;
  if (typeof value === 'boolean') return value ? 1 : 0; // Python bool is an int subclass
  if (typeof value === 'string') {
    const m = /^\s*[+-]?\d+\s*$/.exec(value); // Python int(str) rejects decimals
    return m ? parseInt(value, 10) : null;
  }
  return null; // null/undefined/object/array -> TypeError in Python
}

// Port of the parameterized `_bucket` helper shared by `_line_bucket`
// (detect_disagreement, proximity 10) and `group_by_proximity` (proximity 5):
// round(int(line) / proximity) * proximity. The `int()` truncation happens
// BEFORE the (banker's-rounding) division -- dropping it diverges on
// non-integer line_start values (line_start=25.7 -> int()=25 -> bucket 20,
// NOT round(25.7/10)*10=30; see the non_integer_line_start fixture).
function lineBucket(line, proximity) {
  const n = pyIntOrNull(line);
  if (n === null) return 0;
  return pyRound(n / proximity) * proximity;
}

// --- Disagreement detection --------------------------------------------

const AGENT_BUG_DETECTOR = 'bug-detector';
const AGENT_CONVENTIONS = 'conventions-and-intent';
const AGENT_TEST_ANALYZER = 'test-analyzer';
const AGENT_SECURITY_REVIEWER = 'security-reviewer';

const CONSENSUS_BOOST = 10;
const SINGLETON_PENALTY = 15;

// Core dimensions exempt from the singleton penalty (BF-15b). Textually
// identical to MAIN_DIMENSIONS below but kept as a SEPARATE constant --
// matching the source's own duplication (_CORE_DIMENSIONS vs
// _MAIN_DIMENSIONS) -- because the two lists answer different questions
// (singleton-penalty exemption vs always-route-to-main) that merely happen
// to coincide today.
const CORE_DIMENSIONS = new Set(['bug', 'security', 'cross_file_impact', 'intent']);

// Port of detect_disagreement. Returns { active, suppressed, boostedCount }
// (camelCase multi-return object, matching applyThresholdFilter's
// { kept, eliminated, contestedCount } convention elsewhere in this file).
//
// Phase-key grouping uses JSON.stringify([file, bucket]) as a Map key, NOT a
// plain object -- a plain object's keys that look like integers (e.g. "20")
// get reordered ahead of string keys by V8 regardless of insertion order,
// which would silently corrupt the location-group iteration order the
// suppression phase below depends on.
export function detectDisagreement(findings) {
  // Phase 1: group by (file, line_bucket(10)) for co-location checks.
  const locationGroups = new Map();
  for (const finding of findings) {
    const key = JSON.stringify([pyGet(finding, 'file', ''), lineBucket(pyGet(finding, 'line_start', 0), 10)]);
    if (!locationGroups.has(key)) locationGroups.set(key, []);
    locationGroups.get(key).push(finding);
  }

  // Phase 2: suppression rules on co-located findings. Identity key mirrors
  // Python's `finding.get("id", id(finding))` -- when "id" is absent, Python
  // falls back to object identity (a unique int per dict). A JS Set/Map can
  // use the finding object itself as a reference-equality key, which is the
  // exact same fallback semantics without needing to fabricate an id.
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
        const convText = `${pyGet(convFinding, 'description', '')} ${pyGet(convFinding, 'title', '')}`.toLowerCase();
        if (/\bintentional\b|\bby\s+design\b|\bexpected\s+behavior\b|\bdeliberate\b/.test(convText)) {
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
        const convText = `${pyGet(convFinding, 'description', '')} ${pyGet(convFinding, 'title', '')}`.toLowerCase();
        if (/\bgenerated\b|\bscaffolding\b|\bauto[-\s]?generated\b|\bboilerplate\b/.test(convText)) {
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

  // Phase 3: consensus grouping (file + line_bucket(10)) over the active set.
  const consensusGroups = new Map();
  for (const finding of active) {
    const key = JSON.stringify([pyGet(finding, 'file', ''), lineBucket(pyGet(finding, 'line_start', 0), 10)]);
    if (!consensusGroups.has(key)) consensusGroups.set(key, []);
    consensusGroups.get(key).push(finding);
  }

  let boostedCount = 0;
  for (const group of consensusGroups.values()) {
    const count = group.length;
    // Only findings with a truthy agent contribute to corroborated_by lists
    // (mirrors Python's `if f.get("agent")` filter on agents_in_group).
    const agentsInGroup = group.filter((f) => f.agent).map((f) => f.agent);

    if (count > 1) {
      boostedCount += count;
      for (const finding of group) {
        const thisAgent = pyGet(finding, 'agent', '');
        const otherAgents = agentsInGroup.filter((a) => a !== thisAgent);
        finding.consensus_count = count;
        finding.consensus_boost = CONSENSUS_BOOST;
        finding.corroborated_by = otherAgents;
        const originalConf = pyGet(finding, 'confidence', 0);
        finding.confidence = Math.min(originalConf + CONSENSUS_BOOST, 100);
      }
    } else {
      const finding = group[0];
      finding.consensus_count = 1;
      finding.consensus_boost = 0;
      if (!('corroborated_by' in finding)) finding.corroborated_by = [];

      const dimension = pyGet(finding, 'dimension', '').toLowerCase();
      if (dimension && !CORE_DIMENSIONS.has(dimension)) {
        const originalConf = pyGet(finding, 'confidence', 0);
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

    const severities = new Set(group.map((f) => pyGet(f, 'severity', 'low').toLowerCase()));
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

  // Safety-net pass: in normal operation every active finding already has
  // these fields from phases 3-4 above, but Python guards with setdefault()
  // and we mirror that guard verbatim for fidelity.
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

// Keywords that promote convention/type_design findings from suggestion to
// main. Ported verbatim from _FUNCTIONAL_VIOLATION_KEYWORDS.
const FUNCTIONAL_VIOLATION_KEYWORDS =
  /\bcrash\b|\bdata\s+loss\b|\bsilent(?:ly)?\b|\bincorrect\b|\bwrong\b|\bfail(?:s|ure)?\b|\bruntime\s+error\b|\bexception\b|\bpanic\b|\bundefined\s+behavio(?:u)?r\b/i;

// Keywords that promote type_design findings specifically. Ported verbatim
// from _TYPE_SAFETY_BUG_KEYWORDS.
const TYPE_SAFETY_BUG_KEYWORDS =
  /\bruntime\b|\bcastexception\b|\btype\s+error\b|\bclasscastexception\b|\bnull\s+pointer\b|\bnullpointer\b|\btype\s+mismatch\b/i;

// Keyword patterns indicating a test-analyzer finding describes a functional
// correctness bug that EXISTS TODAY (vs. a coverage gap). Ported verbatim,
// in order, from _TEST_CORRECTNESS_PATTERNS -- shared by routeByDimension's
// test_coverage branch and isTestCorrectnessFinding's promotion check.
const TEST_CORRECTNESS_PATTERNS = [
  /\brace\s+condition\b/i,
  /\balways\s+pass(?:es)?\b/i,
  /\balways[-\s]pass(?:es)?\b/i,
  /\bnever\s+fail(?:s)?\b/i,
  /\bvacuous(?:ly)?\b/i,
  /\btautolog(?:y|ical)\b/i,
  /\bassert(?:ion)?\s+(?:is\s+)?never\s+reached\b/i,
  /\bdeadlock\b/i,
  /\bdata\s+race\b/i,
  /\bthread\s+(?:safety|unsafe|race)\b/i,
  /\btest\s+(?:never\s+)?(?:actually\s+)?(?:verif|test|check)(?:s|ies)?\s+nothing\b/i,
  /\bfalse\s+positive\s+(?:test|assertion)\b/i,
  /\bincorrect(?:ly)?\s+(?:assert|verify|test)\b/i,
  /\bwrong\s+(?:value|result|output)\b/i,
  /\blocal\s+variable\s+(?:is\s+)?never\s+(?:used|read)\b/i,
  /\bassert(?:s|ion)?\s+(?:on\s+)?(?:a\s+)?(?:local|copy|snapshot)\b/i,
  /\bcompares?\s+(?:wrong|incorrect|different)\s+object\b/i,
  /\btest\s+(?:does\s+not|doesn'?t)\s+(?:wait|join|block)\b/i,
  /\breader\s+thread\s+not\s+waited\b/i,
  /\bflaky\s+test\b/i,
  /\bassertion\s+always\s+(?:true|passes?|succeed)\b/i,
  /\bassert(?:s|ion)?\s+(?:is\s+)?always\s+(?:true|pass(?:es?)?|succeed)\b/i,
  /\btest\s+(?:is\s+)?always\s+(?:true|pass(?:es?)?|succeed)\b/i,
  /\blogic\s+error\b/i,
  /\bincorrect\s+(?:logic|behavior|behaviour|result)\b/i,
];

// Port of _route_by_dimension. Returns "main", "suggestion", or null (fall
// through to agent-based routing in tagFindings).
export function routeByDimension(finding) {
  const dimension = pyGet(finding, 'dimension', '').toLowerCase();
  if (!dimension) return null;

  if (MAIN_DIMENSIONS.has(dimension)) return 'main';
  if (SUGGESTION_DIMENSIONS.has(dimension)) return 'suggestion';

  if (CONDITIONAL_SUGGESTION_DIMENSIONS.has(dimension)) {
    const combined = `${pyGet(finding, 'title', '')}\n${pyGet(finding, 'description', '')}`;

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

// Port of _is_test_correctness_finding.
function isTestCorrectnessFinding(finding) {
  const combined = `${pyGet(finding, 'title', '')}\n${pyGet(finding, 'description', '')}`;
  return TEST_CORRECTNESS_PATTERNS.some((rx) => rx.test(combined));
}

// --- Proximity grouping + cross-agent dedup ---------------------------------

// Port of group_by_proximity. Returns a Map keyed by JSON.stringify([file,
// bucket]) -- an internal grouping key with no Python equivalent string
// form; only dedupCrossAgent (and, later, applyChallenges per the brief)
// consume the grouping, never its literal key shape.
export function groupByProximity(findings, lineProximity = 5) {
  const groups = new Map();
  for (const finding of findings) {
    const key = JSON.stringify([pyGet(finding, 'file', ''), lineBucket(pyGet(finding, 'line_start', 0), lineProximity)]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(finding);
  }
  return groups;
}

// Port of dedup_cross_agent. Only dedups groups with 2+ findings from 2+
// DIFFERENT agents; same-agent siblings of the winner are always kept;
// findings without an "id" are immune (pass through unconditionally).
// Winner priority: (isCore, confidence, description.length), all DESCENDING
// via one stable composite comparator -- matches Python's
// `sorted(group, key=_winner_key, reverse=True)`, which for tied keys
// preserves original relative order (Python sort stability + reverse=True
// keeps ties in forward order, not reversed) -- V8's sort is equally stable,
// so a single multi-key comparator reproduces this without a second pass.
// EXPORTED for reuse by applyChallenges (Task 7) — keep this a standalone
// plain function with no closure over filterFindings-only state.
export function dedupCrossAgent(findings) {
  const LINE_PROXIMITY = 5;

  const safeIntLine = (f) => {
    const n = pyIntOrNull(pyGet(f, 'line_start', 0));
    return n === null ? 0 : n;
  };

  const winnerKey = (f) => {
    const dim = pyGet(f, 'dimension', '').toLowerCase();
    const isCore = CORE_DIMENSIONS.has(dim) ? 1 : 0;
    const conf = pyGet(f, 'confidence', 0);
    const descLen = pyGet(f, 'description', '').length;
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

  const keptFindingIds = new Set();
  const dropped = [];

  for (const group of groups.values()) {
    const agentsInGroup = new Set(group.map((f) => pyGet(f, 'agent', '').toLowerCase()));
    if (group.length < 2 || agentsInGroup.size < 2) {
      for (const f of group) {
        const fid = pyGet(f, 'id', '');
        if (fid) keptFindingIds.add(fid);
      }
      continue;
    }

    const ranked = [...group].sort(compareWinnerDesc);
    const winner = ranked[0];
    const winnerAgent = pyGet(winner, 'agent', '').toLowerCase();
    const winnerId = pyGet(winner, 'id', '');
    if (winnerId) keptFindingIds.add(winnerId);

    for (const loser of ranked.slice(1)) {
      const loserAgent = pyGet(loser, 'agent', '').toLowerCase();
      const loserId = pyGet(loser, 'id', '');
      if (loserAgent === winnerAgent) {
        if (loserId) keptFindingIds.add(loserId);
        continue;
      }
      const loserLine = safeIntLine(loser);
      const winnerLine = safeIntLine(winner);
      dropped.push({
        ...loser,
        eliminated_by: 'dedup:cross-agent',
        elimination_reason:
          `cross-agent dedup: finding at ${pyGet(loser, 'file', '?')}:${loserLine} ` +
          `(agent=${JSON.stringify(pyGet(loser, 'agent', '?'))}, dim=${JSON.stringify(pyGet(loser, 'dimension', '?'))}, ` +
          `conf=${pyGet(loser, 'confidence', '?')}) lost to agent=${JSON.stringify(pyGet(winner, 'agent', '?'))} ` +
          `at line ${winnerLine} within ${LINE_PROXIMITY} lines`,
      });
    }
  }

  // Findings without a truthy "id" pass through unconditionally (mirrors
  // Python's `f.get("id", "") in kept_finding_ids or not f.get("id")`).
  const kept = findings.filter((f) => keptFindingIds.has(pyGet(f, 'id', '')) || !pyGet(f, 'id', undefined));
  return { kept, dropped };
}

// --- Tagging -----------------------------------------------------------

const MAIN_REPORT_AGENTS = new Set(['bug-detector', 'security-reviewer', 'cross-file-impact', 'type-design-analyzer']);
const SUGGESTION_AGENTS = new Set(['test-analyzer', 'code-simplifier']);
const CONVENTIONS_AGENT = 'conventions-and-intent';
const COMMENT_ACCURACY_DIMENSIONS = new Set(['comment-accuracy', 'documentation', 'doc-accuracy']);

// Port of tag_findings. Step 1 (dedup) -> per-finding routeByDimension ->
// agent-based fallback. Returns { tagged, dedupDropped, mainCount,
// suggestionCount }.
export function tagFindings(findings) {
  const { kept: tagged, dropped: dedupDropped } = dedupCrossAgent(findings);

  let mainCount = 0;
  let suggestionCount = 0;

  for (const finding of tagged) {
    const agent = pyGet(finding, 'agent', '').toLowerCase();
    // Truthy check (not `'dimension' in finding`) on purpose -- mirrors the
    // established `finding.dimension ? [...] : []` idiom already used by
    // applyThresholdFilter above, which in turn mirrors Python's
    // `if finding.get("dimension")` (truthy, not presence) guard.
    const dimensions = finding.dimension ? new Set([String(finding.dimension).toLowerCase()]) : new Set();

    const dimRoute = routeByDimension(finding);
    let destination;
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

    finding.report_destination = destination;
    finding.report_tag = destination; // backward-compat alias
    if (destination === 'main') mainCount += 1;
    else suggestionCount += 1;
  }

  return { tagged, dedupDropped, mainCount, suggestionCount };
}

// --- Pipeline composition ----------------------------------------------

// Port of main()'s filter pipeline composition (filter_findings.py:1296-1376),
// minus argparse/file I/O -- config and exclusionPatterns are passed in
// directly (already parsed by parseReviewMd/loadExclusions upstream), and
// generatedAt is injected (never `new Date()`/`Date.now()` -- workflow JS
// has no wall clock; see the Global Constraints "No wall-clock" rule).
export function applyFilterPipeline(findings, config, exclusionPatterns, generatedAt) {
  const total = findings.length;

  normalizeFieldNames(findings);

  // Python: `exclusion_patterns = config.get("ignore", []) + load_exclusions(...)`.
  const allExclusions = [...(config.ignore || []), ...(exclusionPatterns || [])];

  const allEliminated = [];

  const { kept: afterThreshold, eliminated: elimThreshold, contestedCount } = applyThresholdFilter(findings, config);
  allEliminated.push(...elimThreshold);
  const passedThreshold = afterThreshold.length;

  const { kept: afterExclusions, eliminated: elimExclusions } = applyExclusions(afterThreshold, allExclusions);
  allEliminated.push(...elimExclusions);

  const { kept: afterInjection, eliminated: elimInjection } = applyInjectionFilter(afterExclusions);
  allEliminated.push(...elimInjection);
  const injectionsRemoved = elimInjection.length;

  const { active, suppressed: elimSuppressed, boostedCount: consensusBoosted } = detectDisagreement(afterInjection);
  allEliminated.push(...elimSuppressed);

  const { tagged, dedupDropped, mainCount, suggestionCount } = tagFindings(active);
  allEliminated.push(...dedupDropped);

  const promotedCount = tagged.filter((f) => f.promoted_from === 'test-analyzer').length;
  const dimensionRouted = tagged.filter((f) => f.routed_by === 'dimension').length;
  const singletonPenalized = [...tagged, ...allEliminated].filter((f) => f.singleton_penalty).length;

  return {
    filtered: tagged,
    eliminated: allEliminated,
    stats: {
      total,
      passed_threshold: passedThreshold,
      contested_count: contestedCount,
      injections_removed: injectionsRemoved,
      consensus_boosted: consensusBoosted,
      singleton_penalized: singletonPenalized,
      dimension_routed: dimensionRouted,
      cross_agent_deduped: dedupDropped.length,
      test_analyzer_deduped: dedupDropped.length, // backward-compat alias
      test_analyzer_promoted: promotedCount,
      tagged_main: mainCount,
      tagged_suggestion: suggestionCount,
    },
    generated_at: generatedAt,
  };
}

export function applyExclusions(findings, exclusionPatterns) {
  if (!exclusionPatterns || !exclusionPatterns.length) return { kept: findings, eliminated: [] };

  const kept = [];
  const eliminated = [];

  for (const finding of findings) {
    const title = finding.title || '';
    const description = finding.description || '';
    const combined = `${title}\n${description}`;

    let matchedPattern = null;
    for (const pattern of exclusionPatterns) {
      const rx = new RegExp(escapeRegExp(pattern), 'i');
      if (rx.test(combined)) {
        matchedPattern = pattern;
        break;
      }
    }

    if (matchedPattern) {
      eliminated.push({
        ...finding,
        eliminated_by: 'exclusion',
        elimination_reason: `matched exclusion pattern: ${JSON.stringify(matchedPattern)}`,
      });
    } else {
      kept.push(finding);
    }
  }

  return { kept, eliminated };
}
