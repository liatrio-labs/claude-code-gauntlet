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

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low'];
const DEFAULT_CONFIDENCE_THRESHOLD = 70;
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
// in a port).
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
      effectiveThreshold = cfgGet(config, 'confidence_threshold', DEFAULT_CONFIDENCE_THRESHOLD);
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
