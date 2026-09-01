// filterFindings.js — JS twin of scripts/filter_findings.py (Phase 6 filtering).
// Part 1: normalize / thresholds / injection / exclusions / REVIEW.md parsing.
// Part 2: disagreement detection, tagging, consolidateCrossAgent, applyFilterPipeline —
// appended to this same file.

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
// DEFAULT_CONFIDENCE_THRESHOLD backs the SECURITY branch of applyThresholdFilter's
// config-absent fallback, so an unconfigured security bar stays min(70,70)=70. As
// of issue #94 F7, parseReviewMd/parse_review_md no longer pre-fill this into their
// returned config -- they only set confidence_threshold when REVIEW.md's config
// block actually sets it, so a truly config-absent REVIEW.md reaches
// applyThresholdFilter's `cfgGet(config, 'confidence_threshold', DEFAULT)` fallback
// below rather than an already-70-filled value. The NON-security runtime default is
// decoupled: when confidence_threshold is absent, non-security dimensions filter at
// 55 (rescues conf-55-68 goldens) while security is unchanged at 70.
// scripts/filter_findings.py's apply_threshold_filter carries the identical split
// (its own DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD) — both runtimes agree on the
// config-absent split; an EXPLICIT confidence_threshold (user REVIEW.md override)
// still applies to BOTH branches in both languages.
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

// Ignore entries are raw substrings matched against a finding's title+description
// (applyExclusions, further down). Written REVIEW.md examples wrap the pattern in
// quotes for readability (`- "console.log in dev mode"`) -- strip ONE matching pair
// of surrounding quotes (single or double) so the stored pattern is the bare
// substring, not a string that includes the quote characters (which would then
// never appear in an unquoted finding title/description and silently never match;
// issue #94 adversarial review F2). Only a single matching pair strips -- an entry
// that is not quote-wrapped, or whose quotes don't match, passes through untouched.
function stripMatchingQuotes(item) {
  if (item.length >= 2) {
    const first = item[0];
    const last = item[item.length - 1];
    if ((first === '"' || first === "'") && first === last) {
      return item.slice(1, -1);
    }
  }
  return item;
}

// Converged twin line splitter (issue #243): split on the universal-newline
// alternation \r\n | \r | \n (that order, \r\n first). Mirrors Python's
// _split_review_lines (re.split(r"\r\n|\r|\n", text)) byte-for-byte and
// reproduces Python open()'s universal-newline translation, so a lone \r, a
// \r\n, and a \n all break the line identically in both engines. This subsumes
// the old "strip one trailing \r" step and converges the lone-\r case the raw
// JS text used to keep inside the line -- see the Python docstring.
function splitReviewLines(text) {
  return text.split(/\r\n|\r|\n/);
}

// The config-parser pattern declarations (issue #243) are GENERATED from
// scripts/filter_patterns_registry.py -- edit the registry, then run
// scripts/generate_filter_patterns.py. `.`/`[\s\S]` became `[^\x00]`
// (cross-twin symmetric, NOT behavior-preserving against a NUL in the block
// body); the Python twin carries re.ASCII where these literals carry `/i`.
// generated-from-filter-pattern-registry:REVIEW_BLOCK_PATTERNS do not edit; run scripts/generate_filter_patterns.py
const REVIEW_BLOCK_PATTERNS = [
  /```(?:yaml|)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*#?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*code-gauntlet(?:[^\n]*)?\n([^\x00]*?)```/i,
  /<!--[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*code-gauntlet-config[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\n([^\x00]*?)-->/i,
  /```(?:yaml|)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*#?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*deep-review(?:[^\n]*)?\n([^\x00]*?)```/i,
  /<!--[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*deep-review-config[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\n([^\x00]*?)-->/i,
];
// /generated-from-filter-pattern-registry:REVIEW_BLOCK_PATTERNS
// generated-from-filter-pattern-registry:REVIEW_CONFIDENCE_RE do not edit; run scripts/generate_filter_patterns.py
const REVIEW_CONFIDENCE_RE =
  /(?:^|\n)[ \t]*confidence_threshold[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[:=][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*([0-9]{1,3})(?![0-9])/i;
// /generated-from-filter-pattern-registry:REVIEW_CONFIDENCE_RE
// generated-from-filter-pattern-registry:REVIEW_SECURITY_RE do not edit; run scripts/generate_filter_patterns.py
const REVIEW_SECURITY_RE =
  /(?:^|\n)[ \t]*security_min_confidence[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[:=][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*([0-9]{1,3})(?![0-9])/i;
// /generated-from-filter-pattern-registry:REVIEW_SECURITY_RE
// generated-from-filter-pattern-registry:REVIEW_SEVERITY_RE do not edit; run scripts/generate_filter_patterns.py
const REVIEW_SEVERITY_RE =
  /(?:^|\n)[ \t]*severity_threshold[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[:=][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*(critical|high|medium|low)/i;
// /generated-from-filter-pattern-registry:REVIEW_SEVERITY_RE
// generated-from-filter-pattern-registry:REVIEW_IGNORE_RE do not edit; run scripts/generate_filter_patterns.py
const REVIEW_IGNORE_RE =
  /(?:^|\n)[ \t]*ignore[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*:[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\n((?:[ \t]*-[^\n]*\n?)+)/i;
// /generated-from-filter-pattern-registry:REVIEW_IGNORE_RE
// generated-from-filter-pattern-registry:REVIEW_IGNORE_ITEM_RE do not edit; run scripts/generate_filter_patterns.py
const REVIEW_IGNORE_ITEM_RE =
  /^[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*-[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*/;
// /generated-from-filter-pattern-registry:REVIEW_IGNORE_ITEM_RE
// generated-from-filter-pattern-registry:REVIEW_EXCL_BLOCK_RE do not edit; run scripts/generate_filter_patterns.py
const REVIEW_EXCL_BLOCK_RE =
  /```[^\n]*\n([^\x00]*?)```/;
// /generated-from-filter-pattern-registry:REVIEW_EXCL_BLOCK_RE
// generated-from-filter-pattern-registry:REVIEW_EXCL_BULLET_RE do not edit; run scripts/generate_filter_patterns.py
const REVIEW_EXCL_BULLET_RE =
  /^[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[-*][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+([^\n]+)$/;
// /generated-from-filter-pattern-registry:REVIEW_EXCL_BULLET_RE

// Port of parse_review_md. Python reads a file by path; this twin takes the
// REVIEW.md TEXT directly (the workflow runtime has no disk access).
//
// `ignore` is always present (default: []). `confidence_threshold`,
// `security_min_confidence`, and `severity_threshold` are present ONLY when the
// corresponding key was actually found in the text (issue #94 adversarial review
// F7) -- this object is not pre-filled with DEFAULT_CONFIDENCE_THRESHOLD /
// DEFAULT_SECURITY_MIN_CONFIDENCE / DEFAULT_SEVERITY_THRESHOLD. A caller reading
// an absent key must use `cfgGet` -- applyThresholdFilter already does, and that
// is what lets its own non-security/security default split (55/70) take effect
// for a config-absent REVIEW.md.
export function parseReviewMd(text) {
  const config = { ignore: [] };

  if (text === undefined || text === null) return config;

  // Block patterns tried in order (REVIEW_BLOCK_PATTERNS): fenced ```yaml block,
  // HTML comment block, then the two legacy deep-review markers. DOTALL is now
  // `[^\x00]` (matches every char but NUL) rather than `[\s\S]`; the Python twin
  // matches these ASCII-folded (re.ASCII), which these non-unicode `/i` literals
  // already are, so `# deep-revıew` matches in neither.
  let blockText = '';
  for (const pattern of REVIEW_BLOCK_PATTERNS) {
    const m = pattern.exec(text);
    if (m) {
      blockText = m[1];
      break;
    }
  }

  // Whole-file fallback when no block found (Python logs a warning here; the
  // return value is unaffected so the JS twin has nothing to emit).
  if (!blockText) blockText = text;

  // Every key regex is anchored to a line start via `(?:^|\n)` (converged with
  // the Python twin: the old `/m` flag broke a line after \r/U+2028/U+2029, this
  // only breaks after \n or string start). A `#` before the key is not in the
  // `[ \t]*` class, so a commented example stays inert (issue #94 F1).
  //
  // confidence_threshold / security_min_confidence are bounded to a 1-3 digit
  // ASCII run and accepted only when <= 100 (review-md-spec `<0-100>`): a value
  // above 100 is ignored (defaults apply). This closes the parseInt()-vs-int()
  // divergence on out-of-range values -- `1e+21` in JS, an exact int in Python.
  let m = REVIEW_CONFIDENCE_RE.exec(blockText);
  if (m) {
    const value = parseInt(m[1], 10);
    if (value <= 100) config.confidence_threshold = value;
  }

  m = REVIEW_SECURITY_RE.exec(blockText);
  if (m) {
    const value = parseInt(m[1], 10);
    if (value <= 100) config.security_min_confidence = value;
  }

  m = REVIEW_SEVERITY_RE.exec(blockText);
  if (m) config.severity_threshold = m[1].toLowerCase();

  // ignore: consecutive "-"-led lines, indentation-tolerant (spaces or tabs).
  const ignoreSection = REVIEW_IGNORE_RE.exec(blockText);
  if (ignoreSection) {
    for (const line of splitReviewLines(ignoreSection[1])) {
      const item = line.replace(REVIEW_IGNORE_ITEM_RE, '').replace(WS_TRIM_RE, '');
      if (item) config.ignore.push(stripMatchingQuotes(item));
    }
  }

  return config;
}

// --- Filter: confidence / severity threshold (with validator contestation) -

// Port of apply_threshold_filter. Security effective threshold is literally
// Math.min(confidence_threshold, security_min_confidence) — faithful to the
// Python `min()` call even though it makes the security bar the LOWER of the
// two configured numbers (pinned by parity-map §3; not a naming bug to fix
// in a port). The CONFIG-ABSENT fallback (a `config` with no confidence_threshold
// key) splits by dimension: non-security defaults to 55
// (DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD), security stays at 70 — Python's
// apply_threshold_filter carries the identical split (issue #94), so this is no
// longer a JS-only divergence. An explicit confidence_threshold in `config`
// overrides both branches identically in both languages, so the split is
// invisible to the config_absent-agnostic parity fixtures that pass an explicit
// config; the config-absent path itself is covered by its own fixture.
export function applyThresholdFilter(findings, config) {
  const kept = [];
  const eliminated = [];
  let contestedCount = 0;

  const severityThreshold = cfgGet(config, 'severity_threshold', DEFAULT_SEVERITY_THRESHOLD);
  const sevThresholdIdx = SEVERITY_ORDER.indexOf(severityThreshold);

  for (const finding of findings) {
    const confidence = asConfidence(pyGet(finding, 'confidence', 0));
    let severity = (asText(pyGet(finding, 'severity', 'low')) || 'low').toLowerCase();
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

// #254 (F13): the four (now five) "<word> finding" entries picked up the
// union whitespace class between the word and "finding" (previously a
// literal space) -- see the #254 record.
// #260: the bare-word TODO/FIXME/Placeholder entries were dropped -- a real
// finding legitimately reports TODO/FIXME/placeholder residue about the code
// it reviews (measured: 5/727 real corpus titles, 100% false positive, 0
// true positives across 30 recorded runs). Detection now keys on the stub
// vocabulary "<word> finding" itself -- the phrase an injected scaffold
// title tends to spell and a real finding about residue essentially never
// does -- so the standalone `Placeholder` entry was replaced by a
// `Placeholder finding` entry alongside its four siblings.
// generated-from-filter-pattern-registry:INJECTION_TITLE_PATTERNS do not edit; run scripts/generate_filter_patterns.py
const INJECTION_TITLE_PATTERNS = [
  /\bExample[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b/i,
  /\bSample[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b/i,
  /\btest[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b/i,
  /\bdemo[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b/i,
  /\bPlaceholder[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b/i,
];
// /generated-from-filter-pattern-registry:INJECTION_TITLE_PATTERNS

// #254: <finding>/<example> widened to tolerate attributes (unbounded
// [^>]*, terminated by the required ">" so it stays linear and parity-safe
// across twins -- Python counts code points, JS counts UTF-16 units, so a
// bounded {0,N} window here would diverge on astral input; </finding> was
// considered and declined -- an injected block always opens, so a closing
// tag adds false-fire surface with zero catch). The bracketed placeholder
// entry gained a second, appended form gated on a placeholder noun
// (FINDING/TITLE/TEXT/PLACEHOLDER/HERE): a bare `[INSERT ...]` widened past
// ~40 interior chars collides with real SQL privilege-list findings
// (`[INSERT, UPDATE, DELETE]`), so the noun gate is the discriminator
// instead of a length bound. Appended after the original bracket entry so
// `firstMatch`'s reason for a bare `[INSERT]` payload is unchanged.
// "lorem ipsum" picked up the union whitespace class (previously a literal
// space).
// generated-from-filter-pattern-registry:INJECTION_BODY_PATTERNS do not edit; run scripts/generate_filter_patterns.py
const INJECTION_BODY_PATTERNS = [
  /<finding(?:[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff][^>]*)?>/i,
  /<example(?:[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff][^>]*)?>/i,
  /\[[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*INSERT[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\]/i,
  /\[[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*INSERT\b[^\]]*\b(?:FINDING|TITLE|TEXT|PLACEHOLDER|HERE)\b[^\]]*\]/i,
  /lorem[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+ipsum/i,
];
// /generated-from-filter-pattern-registry:INJECTION_BODY_PATTERNS

// generated-from-filter-pattern-registry:INJECTION_SHELL_PATTERNS do not edit; run scripts/generate_filter_patterns.py
const INJECTION_SHELL_PATTERNS = [
  /\brm[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+-[rf]/i,
  /\bcurl[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+https?:\/\//i,
  /\bwget[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+https?:\/\//i,
  /\bgit[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+push\b/i,
  /\bgh[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+api\b/i,
];
// /generated-from-filter-pattern-registry:INJECTION_SHELL_PATTERNS

// URL patterns -- findings should reference code locations, not external URLs
// to visit/fetch. Only "visit"/"download from" ship: they are imperatives a
// legitimate finding never states about itself. A prior pass (#252) tried
// adding two directive-gated long-bare-URL entries -- a reader-imperative
// verb immediately before the URL, and an exfiltration-verb + secret-object
// phrase ahead of it -- but round-2 review measured both false-firing on
// realistic LEGITIMATE security findings that quote the same vocabulary a
// real vulnerability description needs ("the router should navigate to
// <url>" for a routing bug, "an attacker can send the session cookie to
// <url>" for a real exfil finding): a legit finding and an injected
// instruction both read as "<verb> to/from <url>" in English, so this shape
// cannot be narrowed further to tell them apart. Reverted; see #255 review.
// #254: the scheme was widened from a bare `https?` to any scheme-shaped
// token (ftp, sftp, scp, ...) -- the imperative is the discriminator, not
// the scheme, so enumerating individual schemes is whack-a-mole and every
// scheme closes in one edit. "download from" also picked up the union
// whitespace class between "download" and "from" (previously a literal
// space) -- see F13 in the #254 record.
// generated-from-filter-pattern-registry:INJECTION_URL_PATTERNS do not edit; run scripts/generate_filter_patterns.py
const INJECTION_URL_PATTERNS = [
  /\bvisit[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+[a-z][a-z0-9+.\-]{1,15}:\/\//i,
  /\bdownload[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+from[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+[a-z][a-z0-9+.\-]{1,15}:\/\//i,
];
// /generated-from-filter-pattern-registry:INJECTION_URL_PATTERNS

// Encoded payload patterns -- base64 or hex blobs in findings are injection
// artifacts. Each shape is now two directive-gated entries: a before-branch
// requiring a decode-family verb ahead of the blob, an after-branch requiring
// decode/execute sink syntax after it. A bare encoded-looking run with no
// decode directive nearby (a commit SHA, an opaque config token, a padded
// identifier) no longer matches either branch -- both measured a false-fire
// on ordinary review/DevOps prose where a generic verb (run/curl/wget)
// happened to sit near an unrelated hash-shaped token.
// generated-from-filter-pattern-registry:INJECTION_ENCODED_PATTERNS do not edit; run scripts/generate_filter_patterns.py
const INJECTION_ENCODED_PATTERNS = [
  /\b(?:decode|base64|atob|b64decode)\b[^\x00]{0,40}[A-Za-z0-9+\/]{40,}={0,2}\b/i,
  /\b[A-Za-z0-9+\/]{40,}={0,2}\b[^\x00]{0,40}(?:\|[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*(?:sh|bash|zsh)\b|base64[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+-d\b|(?:then|and)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:run|execute|eval)\b)/i,
  /\b(?:decode|unhex|xxd|fromhex|unhexlify)\b[^\x00]{0,40}(?<!\w)(?:0x)?[0-9a-fA-F]{32,}(?!\w)/i,
  /(?<!\w)(?:0x)?[0-9a-fA-F]{32,}(?!\w)[^\x00]{0,40}(?:\|[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*(?:xxd|sh|bash)\b|(?:then|and)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:run|execute|eval)\b|-r[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+-p\b)/i,
];
// /generated-from-filter-pattern-registry:INJECTION_ENCODED_PATTERNS

// Bypass / auto-approve instruction patterns. auto-approve is now two
// grammatically-gated entries (a determiner + PR/MR/commit object, or an
// "and <verb>" continuation) instead of a bare phrase match -- the bare
// phrase false-fired on third-person policy prose ("auto-approve changes to
// lockfiles should be gated behind review") where "auto-approve" is the
// grammatical subject, not an imperative.
// generated-from-filter-pattern-registry:INJECTION_BYPASS_PATTERNS do not edit; run scripts/generate_filter_patterns.py
const INJECTION_BYPASS_PATTERNS = [
  /\bskip[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+review\b/i,
  /\bauto[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]?approve[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:this|these|the|it|my|your)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:pr|pull[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+request|mr|merge[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+request|changes?|commit)\b/i,
  /\bauto[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]?approve[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+and[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:merge|skip|bypass|push|deploy|proceed|continue)\b/i,
  /\bbypass[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:security[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?controls?\b/i,
  /\bbypass[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:the[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?(?:auth|authentication|authorization)\b/i,
  /\bdisable[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:auth|authentication|authorization)\b/i,
  /\bmark[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:this[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?(?:finding[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?as[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+safe\b/i,
  /\bapprove[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:this|the)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:PR|pull[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+request|change)\b/i,
];
// /generated-from-filter-pattern-registry:INJECTION_BYPASS_PATTERNS

// generated-from-filter-pattern-registry:INJECTION_INSTRUCTIONAL_PATTERNS do not edit; run scripts/generate_filter_patterns.py
const INJECTION_INSTRUCTIONAL_PATTERNS = [
  /\byou[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+should[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+run\b/i,
  /\bexecute[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+the[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+following\b/i,
  /\brun[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+this[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+command\b/i,
  /\bplease[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+run\b/i,
  /\bpaste[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:this|the[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+following)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+into[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:your[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?terminal/i,
  /\bcopy[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+and[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+paste[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+the[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+following\b/i,
];
// /generated-from-filter-pattern-registry:INJECTION_INSTRUCTIONAL_PATTERNS

// generated-from-filter-pattern-registry:INJECTION_VULN_INTRO_PATTERNS do not edit; run scripts/generate_filter_patterns.py
const INJECTION_VULN_INTRO_PATTERNS = [
  /\badd[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+eval[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\(/i,
  /\buse[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+eval[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\(/i,
  /\bdisable[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:CORS|CSP|content[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]security[-\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]policy)\b/i,
  /\bdisable[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:CSRF|csrf)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:protection|check|token)\b/i,
  /\ballow[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+all[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+origins\b/i,
  /\bset[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+secure[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+to[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+false\b/i,
  /\bdisable[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:TLS|SSL|HTTPS)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:verification|validation)\b/i,
  /\bskip[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:certificate|cert)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:verification|validation)\b/i,
  /\bdisable[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+security[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:check|feature|control)\b/i,
];
// /generated-from-filter-pattern-registry:INJECTION_VULN_INTRO_PATTERNS

const MIN_BODY_WORDS = 10;
const HIGH_CONFIDENCE_THRESHOLD = 85;

// Matches the union whitespace class respelled into the injection/routing
// patterns above (item 2 of the #211 decision) so a word-count boundary and
// a pattern-match boundary agree on what separates words.
// generated-from-filter-pattern-registry:WORD_SPLIT_RE do not edit; run scripts/generate_filter_patterns.py
export const WORD_SPLIT_RE = /[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+/;
// /generated-from-filter-pattern-registry:WORD_SPLIT_RE

// Port of _count_words: union-whitespace-split word count, 0 for blank/whitespace-only text.
export function countWords(text) {
  return (text || '').split(WORD_SPLIT_RE).filter(Boolean).length;
}

// Confusable-fold + invisible-strip tables (#272): a single non-ASCII codepoint folds
// to one ASCII letter, and 599 zero-width/joiner/bidi/variation-selector/combining
// codepoints are deleted, so a homoglyph- or invisible-disguised injection phrase
// reduces to the plain ASCII the heuristics scan for. GENERATED from
// scripts/filter_patterns_registry.py by scripts/generate_confusable_tables.py -- do
// not hand-edit inside the fences -- mirroring the Python twin's decode. An NFKC pre-
// pass at runtime would diverge the twins (Node ICU vs CPython UCD ship different
// Unicode versions), so the tables are hand-pinned, never derived. Precedence
// casefold > NFKC > confusables is baked into the data (U+017F LONG S folds to s, not
// the confusables f). The packed source codepoints are \u/\u{} escapes, not literal
// glyphs: U+212A KELVIN SIGN is byte-indistinguishable from ASCII 'K' in source.
// generated-from-confusable-registry:CONFUSABLE_FOLD_PACKED do not edit; run scripts/generate_confusable_tables.py
const CONFUSABLE_FOLD_PACKED =
  '\u00aaa\u00bao\u00d7x\u00fep\u0130i\u0131i\u017fs\u0184b\u018dg\u0192f\u0196l\u01a6R' +
  '\u01bds\u01bfp\u01c0l\u0251a\u0261g\u0263y\u0269i\u026ai\u026fw\u028bu\u028fy\u02b0h' +
  '\u02b2j\u02b3r\u02b7w\u02b8y\u02dbi\u02e1l\u02e2s\u02e3x\u037ai\u037fJ\u0391A\u0392B' +
  '\u0395E\u0396Z\u0397H\u0399l\u039aK\u039cM\u039dN\u039fO\u03a1P\u03a4T\u03a5Y\u03a7X' +
  '\u03b1a\u03b3y\u03b9i\u03bdv\u03bfo\u03c1p\u03c3o\u03c5u\u03d2Y\u03dcF\u03edo\u03f1p' +
  '\u03f2c\u03f3j\u03f8p\u03f9C\u03faM\u0405S\u0406l\u0408J\u0410A\u0412B\u0415E\u041aK' +
  '\u041cM\u041dH\u041eO\u0420P\u0421C\u0422T\u0423Y\u0425X\u042cb\u0430a\u0433r\u0435e' +
  '\u043eo\u0440p\u0441c\u0443y\u0445x\u0448w\u0455s\u0456i\u0458j\u0461w\u0474V\u0475v' +
  '\u04aeY\u04afy\u04bbh\u04bde\u04c0l\u04cfl\u0501d\u050cG\u051bq\u051cW\u051dw\u054dU' +
  '\u054fS\u0555O\u0561w\u0563q\u0566q\u0570h\u0578n\u057cn\u057du\u0581g\u0582i\u0584f' +
  '\u0585o\u05c0l\u05d5l\u05d8v\u05dfl\u05e1o\u0627l\u0647o\u0661l\u0665o\u0667V\u06beo' +
  '\u06c1o\u06d5o\u06f1l\u06f5o\u06f7V\u07c0O\u07cal\u0966o\u09e6o\u0a66o\u0ae6o\u0b20O' +
  '\u0b66o\u0be6o\u0c02o\u0c66o\u0c82o\u0ce6O\u0d02o\u0d1fs\u0d20o\u0d66o\u0d82o\u0e50o' +
  '\u0ed0o\u1004c\u101do\u1040o\u105ac\u10e7y\u10ffo\u1200U\u12d0O\u13a0D\u13a1R\u13a2T' +
  '\u13a5i\u13a9Y\u13aaA\u13abJ\u13acE\u13b3W\u13b7M\u13bbH\u13bdY\u13c0G\u13c2h\u13c3Z' +
  '\u13cfb\u13d2R\u13d4W\u13d5S\u13d9V\u13daS\u13deL\u13dfC\u13e2P\u13e6K\u13e7d\u13f3G' +
  '\u13f4B\u142fV\u144cU\u146dP\u146fd\u1472b\u148dJ\u14aaL\u1541x\u157cH\u157dx\u1587R' +
  '\u15afb\u15b4F\u15c5A\u15deD\u15eaD\u15f0M\u15f7B\u166dX\u166ex\u16b7X\u16c1l\u16d5K' +
  '\u16d6M\u17e0o\u1d04c\u1d0fo\u1d11o\u1d1cu\u1d20v\u1d21w\u1d22z\u1d26r\u1d2cA\u1d2eB' +
  '\u1d30D\u1d31E\u1d33G\u1d34H\u1d35I\u1d36J\u1d37K\u1d38L\u1d39M\u1d3aN\u1d3cO\u1d3eP' +
  '\u1d3fR\u1d40T\u1d41U\u1d42W\u1d43a\u1d47b\u1d48d\u1d49e\u1d4dg\u1d4fk\u1d50m\u1d52o' +
  '\u1d56p\u1d57t\u1d58u\u1d5bv\u1d62i\u1d63r\u1d64u\u1d65v\u1d83g\u1d8cy\u1d9cc\u1da0f' +
  '\u1dbbz\u1e9df\u1effy\u1fbei\u2071i\u207fn\u2090a\u2091e\u2092o\u2093x\u2095h\u2096k' +
  '\u2097l\u2098m\u2099n\u209ap\u209bs\u209ct\u2102C\u210ag\u210bH\u210cH\u210dH\u210eh' +
  '\u2110I\u2111I\u2112L\u2113l\u2115N\u2119P\u211aQ\u211bR\u211cR\u211dR\u2124Z\u2128Z' +
  '\u212ak\u212cB\u212dC\u212ee\u212fe\u2130E\u2131F\u2133M\u2134o\u2139i\u213dy\u2145D' +
  '\u2146d\u2147e\u2148i\u2149j\u2160I\u2164V\u2169X\u216cL\u216dC\u216eD\u216fM\u2170i' +
  '\u2174v\u2179x\u217cl\u217dc\u217ed\u217fm\u2223l\u2228v\u222aU\u22a4T\u22c1v\u22c3U' +
  '\u22ffE\u2373i\u2374p\u237aa\u23fdl\u24b6A\u24b7B\u24b8C\u24b9D\u24baE\u24bbF\u24bcG' +
  '\u24bdH\u24beI\u24bfJ\u24c0K\u24c1L\u24c2M\u24c3N\u24c4O\u24c5P\u24c6Q\u24c7R\u24c8S' +
  '\u24c9T\u24caU\u24cbV\u24ccW\u24cdX\u24ceY\u24cfZ\u24d0a\u24d1b\u24d2c\u24d3d\u24d4e' +
  '\u24d5f\u24d6g\u24d7h\u24d8i\u24d9j\u24dak\u24dbl\u24dcm\u24ddn\u24deo\u24dfp\u24e0q' +
  '\u24e1r\u24e2s\u24e3t\u24e4u\u24e5v\u24e6w\u24e7x\u24e8y\u24e9z\u2573X\u27d9T\u292bx' +
  '\u292cx\u2a2fx\u2c7cj\u2c7dV\u2c82B\u2c85r\u2c8eH\u2c92l\u2c93i\u2c94K\u2c98M\u2c9aN' +
  '\u2c9eO\u2c9fo\u2ca2P\u2ca3p\u2ca4C\u2ca5c\u2ca6T\u2ca8Y\u2ca9y\u2cacX\u2cbdw\u2cceP' +
  '\u2ccfp\u2cd0L\u2d38V\u2d39E\u2d4fl\u2d54O\u2d55Q\u2d5dX\u3007O\ua4d0B\ua4d1P\ua4d2d' +
  '\ua4d3D\ua4d4T\ua4d6G\ua4d7K\ua4d9J\ua4daC\ua4dcZ\ua4ddF\ua4dfM\ua4e0N\ua4e1L\ua4e2S' +
  '\ua4e3R\ua4e6V\ua4e7H\ua4eaW\ua4ebX\ua4ecY\ua4eeA\ua4f0E\ua4f2l\ua4f3O\ua4f4U\ua647i' +
  '\ua6dfV\ua731s\ua798F\ua799f\ua79fu\ua7b2J\ua7b3X\ua7b4B\ua7f2C\ua7f3F\ua7f4Q\uab32e' +
  '\uab35f\uab3do\uab47r\uab48r\uab4eu\uab52u\uab5ay\uab75i\uab81r\uab83w\uab93z\uaba9v' +
  '\uabaas\uabafc\ufba6o\ufba7o\ufba8o\ufba9o\ufbaao\ufbabo\ufbaco\ufbado\ufe8dl\ufe8el' +
  '\ufee9o\ufeeao\ufeebo\ufeeco\uff21A\uff22B\uff23C\uff24D\uff25E\uff26F\uff27G\uff28H' +
  '\uff29I\uff2aJ\uff2bK\uff2cL\uff2dM\uff2eN\uff2fO\uff30P\uff31Q\uff32R\uff33S\uff34T' +
  '\uff35U\uff36V\uff37W\uff38X\uff39Y\uff3aZ\uff41a\uff42b\uff43c\uff44d\uff45e\uff46f' +
  '\uff47g\uff48h\uff49i\uff4aj\uff4bk\uff4cl\uff4dm\uff4en\uff4fo\uff50p\uff51q\uff52r' +
  '\uff53s\uff54t\uff55u\uff56v\uff57w\uff58x\uff59y\uff5az\uffe8l\u{10282}B\u{10286}E\u{10287}F' +
  '\u{1028a}l\u{10290}X\u{10292}O\u{10295}P\u{10296}S\u{10297}T\u{102a0}A\u{102a1}B\u{102a2}C\u{102a5}F\u{102ab}O\u{102b0}M' +
  '\u{102b1}T\u{102b2}Y\u{102b4}X\u{102cf}H\u{102f5}Z\u{10301}B\u{10302}C\u{10309}l\u{10311}M\u{10315}T\u{10317}X\u{10320}l' +
  '\u{10322}X\u{10404}O\u{10415}C\u{1041b}L\u{10420}S\u{1042c}o\u{1043d}c\u{10448}s\u{104b4}R\u{104c2}O\u{104ce}U\u{104ea}o' +
  '\u{104f6}u\u{10513}N\u{10516}O\u{10518}K\u{1051c}C\u{1051d}V\u{10525}F\u{10526}L\u{10527}X\u{107a5}q\u{114d0}o\u{11706}v' +
  '\u{1170a}w\u{1170e}w\u{1170f}w\u{118a0}V\u{118a2}F\u{118a3}L\u{118a4}Y\u{118a6}E\u{118a9}Z\u{118ae}E\u{118b2}L\u{118b5}O' +
  '\u{118b8}U\u{118bc}T\u{118c0}v\u{118c1}s\u{118c2}F\u{118c3}i\u{118c4}z\u{118c8}o\u{118d7}o\u{118d8}u\u{118dc}y\u{118e0}O' +
  '\u{118e5}Z\u{118e6}W\u{118e9}C\u{118ec}X\u{118ef}W\u{118f2}C\u{11dda}l\u{11de0}O\u{11de1}l\u{16eaa}l\u{16eb6}b\u{16f08}V' +
  '\u{16f0a}T\u{16f16}L\u{16f28}l\u{16f35}R\u{16f3a}S\u{16f40}A\u{16f42}U\u{16f43}Y\u{1ccde}l\u{1ccf0}O\u{1ccf1}l\u{1d20d}V' +
  '\u{1d213}F\u{1d216}R\u{1d22a}L\u{1d400}A\u{1d401}B\u{1d402}C\u{1d403}D\u{1d404}E\u{1d405}F\u{1d406}G\u{1d407}H\u{1d408}I' +
  '\u{1d409}J\u{1d40a}K\u{1d40b}L\u{1d40c}M\u{1d40d}N\u{1d40e}O\u{1d40f}P\u{1d410}Q\u{1d411}R\u{1d412}S\u{1d413}T\u{1d414}U' +
  '\u{1d415}V\u{1d416}W\u{1d417}X\u{1d418}Y\u{1d419}Z\u{1d41a}a\u{1d41b}b\u{1d41c}c\u{1d41d}d\u{1d41e}e\u{1d41f}f\u{1d420}g' +
  '\u{1d421}h\u{1d422}i\u{1d423}j\u{1d424}k\u{1d425}l\u{1d426}m\u{1d427}n\u{1d428}o\u{1d429}p\u{1d42a}q\u{1d42b}r\u{1d42c}s' +
  '\u{1d42d}t\u{1d42e}u\u{1d42f}v\u{1d430}w\u{1d431}x\u{1d432}y\u{1d433}z\u{1d434}A\u{1d435}B\u{1d436}C\u{1d437}D\u{1d438}E' +
  '\u{1d439}F\u{1d43a}G\u{1d43b}H\u{1d43c}I\u{1d43d}J\u{1d43e}K\u{1d43f}L\u{1d440}M\u{1d441}N\u{1d442}O\u{1d443}P\u{1d444}Q' +
  '\u{1d445}R\u{1d446}S\u{1d447}T\u{1d448}U\u{1d449}V\u{1d44a}W\u{1d44b}X\u{1d44c}Y\u{1d44d}Z\u{1d44e}a\u{1d44f}b\u{1d450}c' +
  '\u{1d451}d\u{1d452}e\u{1d453}f\u{1d454}g\u{1d456}i\u{1d457}j\u{1d458}k\u{1d459}l\u{1d45a}m\u{1d45b}n\u{1d45c}o\u{1d45d}p' +
  '\u{1d45e}q\u{1d45f}r\u{1d460}s\u{1d461}t\u{1d462}u\u{1d463}v\u{1d464}w\u{1d465}x\u{1d466}y\u{1d467}z\u{1d468}A\u{1d469}B' +
  '\u{1d46a}C\u{1d46b}D\u{1d46c}E\u{1d46d}F\u{1d46e}G\u{1d46f}H\u{1d470}I\u{1d471}J\u{1d472}K\u{1d473}L\u{1d474}M\u{1d475}N' +
  '\u{1d476}O\u{1d477}P\u{1d478}Q\u{1d479}R\u{1d47a}S\u{1d47b}T\u{1d47c}U\u{1d47d}V\u{1d47e}W\u{1d47f}X\u{1d480}Y\u{1d481}Z' +
  '\u{1d482}a\u{1d483}b\u{1d484}c\u{1d485}d\u{1d486}e\u{1d487}f\u{1d488}g\u{1d489}h\u{1d48a}i\u{1d48b}j\u{1d48c}k\u{1d48d}l' +
  '\u{1d48e}m\u{1d48f}n\u{1d490}o\u{1d491}p\u{1d492}q\u{1d493}r\u{1d494}s\u{1d495}t\u{1d496}u\u{1d497}v\u{1d498}w\u{1d499}x' +
  '\u{1d49a}y\u{1d49b}z\u{1d49c}A\u{1d49e}C\u{1d49f}D\u{1d4a2}G\u{1d4a5}J\u{1d4a6}K\u{1d4a9}N\u{1d4aa}O\u{1d4ab}P\u{1d4ac}Q' +
  '\u{1d4ae}S\u{1d4af}T\u{1d4b0}U\u{1d4b1}V\u{1d4b2}W\u{1d4b3}X\u{1d4b4}Y\u{1d4b5}Z\u{1d4b6}a\u{1d4b7}b\u{1d4b8}c\u{1d4b9}d' +
  '\u{1d4bb}f\u{1d4bd}h\u{1d4be}i\u{1d4bf}j\u{1d4c0}k\u{1d4c1}l\u{1d4c2}m\u{1d4c3}n\u{1d4c5}p\u{1d4c6}q\u{1d4c7}r\u{1d4c8}s' +
  '\u{1d4c9}t\u{1d4ca}u\u{1d4cb}v\u{1d4cc}w\u{1d4cd}x\u{1d4ce}y\u{1d4cf}z\u{1d4d0}A\u{1d4d1}B\u{1d4d2}C\u{1d4d3}D\u{1d4d4}E' +
  '\u{1d4d5}F\u{1d4d6}G\u{1d4d7}H\u{1d4d8}I\u{1d4d9}J\u{1d4da}K\u{1d4db}L\u{1d4dc}M\u{1d4dd}N\u{1d4de}O\u{1d4df}P\u{1d4e0}Q' +
  '\u{1d4e1}R\u{1d4e2}S\u{1d4e3}T\u{1d4e4}U\u{1d4e5}V\u{1d4e6}W\u{1d4e7}X\u{1d4e8}Y\u{1d4e9}Z\u{1d4ea}a\u{1d4eb}b\u{1d4ec}c' +
  '\u{1d4ed}d\u{1d4ee}e\u{1d4ef}f\u{1d4f0}g\u{1d4f1}h\u{1d4f2}i\u{1d4f3}j\u{1d4f4}k\u{1d4f5}l\u{1d4f6}m\u{1d4f7}n\u{1d4f8}o' +
  '\u{1d4f9}p\u{1d4fa}q\u{1d4fb}r\u{1d4fc}s\u{1d4fd}t\u{1d4fe}u\u{1d4ff}v\u{1d500}w\u{1d501}x\u{1d502}y\u{1d503}z\u{1d504}A' +
  '\u{1d505}B\u{1d507}D\u{1d508}E\u{1d509}F\u{1d50a}G\u{1d50d}J\u{1d50e}K\u{1d50f}L\u{1d510}M\u{1d511}N\u{1d512}O\u{1d513}P' +
  '\u{1d514}Q\u{1d516}S\u{1d517}T\u{1d518}U\u{1d519}V\u{1d51a}W\u{1d51b}X\u{1d51c}Y\u{1d51e}a\u{1d51f}b\u{1d520}c\u{1d521}d' +
  '\u{1d522}e\u{1d523}f\u{1d524}g\u{1d525}h\u{1d526}i\u{1d527}j\u{1d528}k\u{1d529}l\u{1d52a}m\u{1d52b}n\u{1d52c}o\u{1d52d}p' +
  '\u{1d52e}q\u{1d52f}r\u{1d530}s\u{1d531}t\u{1d532}u\u{1d533}v\u{1d534}w\u{1d535}x\u{1d536}y\u{1d537}z\u{1d538}A\u{1d539}B' +
  '\u{1d53b}D\u{1d53c}E\u{1d53d}F\u{1d53e}G\u{1d540}I\u{1d541}J\u{1d542}K\u{1d543}L\u{1d544}M\u{1d546}O\u{1d54a}S\u{1d54b}T' +
  '\u{1d54c}U\u{1d54d}V\u{1d54e}W\u{1d54f}X\u{1d550}Y\u{1d552}a\u{1d553}b\u{1d554}c\u{1d555}d\u{1d556}e\u{1d557}f\u{1d558}g' +
  '\u{1d559}h\u{1d55a}i\u{1d55b}j\u{1d55c}k\u{1d55d}l\u{1d55e}m\u{1d55f}n\u{1d560}o\u{1d561}p\u{1d562}q\u{1d563}r\u{1d564}s' +
  '\u{1d565}t\u{1d566}u\u{1d567}v\u{1d568}w\u{1d569}x\u{1d56a}y\u{1d56b}z\u{1d56c}A\u{1d56d}B\u{1d56e}C\u{1d56f}D\u{1d570}E' +
  '\u{1d571}F\u{1d572}G\u{1d573}H\u{1d574}I\u{1d575}J\u{1d576}K\u{1d577}L\u{1d578}M\u{1d579}N\u{1d57a}O\u{1d57b}P\u{1d57c}Q' +
  '\u{1d57d}R\u{1d57e}S\u{1d57f}T\u{1d580}U\u{1d581}V\u{1d582}W\u{1d583}X\u{1d584}Y\u{1d585}Z\u{1d586}a\u{1d587}b\u{1d588}c' +
  '\u{1d589}d\u{1d58a}e\u{1d58b}f\u{1d58c}g\u{1d58d}h\u{1d58e}i\u{1d58f}j\u{1d590}k\u{1d591}l\u{1d592}m\u{1d593}n\u{1d594}o' +
  '\u{1d595}p\u{1d596}q\u{1d597}r\u{1d598}s\u{1d599}t\u{1d59a}u\u{1d59b}v\u{1d59c}w\u{1d59d}x\u{1d59e}y\u{1d59f}z\u{1d5a0}A' +
  '\u{1d5a1}B\u{1d5a2}C\u{1d5a3}D\u{1d5a4}E\u{1d5a5}F\u{1d5a6}G\u{1d5a7}H\u{1d5a8}I\u{1d5a9}J\u{1d5aa}K\u{1d5ab}L\u{1d5ac}M' +
  '\u{1d5ad}N\u{1d5ae}O\u{1d5af}P\u{1d5b0}Q\u{1d5b1}R\u{1d5b2}S\u{1d5b3}T\u{1d5b4}U\u{1d5b5}V\u{1d5b6}W\u{1d5b7}X\u{1d5b8}Y' +
  '\u{1d5b9}Z\u{1d5ba}a\u{1d5bb}b\u{1d5bc}c\u{1d5bd}d\u{1d5be}e\u{1d5bf}f\u{1d5c0}g\u{1d5c1}h\u{1d5c2}i\u{1d5c3}j\u{1d5c4}k' +
  '\u{1d5c5}l\u{1d5c6}m\u{1d5c7}n\u{1d5c8}o\u{1d5c9}p\u{1d5ca}q\u{1d5cb}r\u{1d5cc}s\u{1d5cd}t\u{1d5ce}u\u{1d5cf}v\u{1d5d0}w' +
  '\u{1d5d1}x\u{1d5d2}y\u{1d5d3}z\u{1d5d4}A\u{1d5d5}B\u{1d5d6}C\u{1d5d7}D\u{1d5d8}E\u{1d5d9}F\u{1d5da}G\u{1d5db}H\u{1d5dc}I' +
  '\u{1d5dd}J\u{1d5de}K\u{1d5df}L\u{1d5e0}M\u{1d5e1}N\u{1d5e2}O\u{1d5e3}P\u{1d5e4}Q\u{1d5e5}R\u{1d5e6}S\u{1d5e7}T\u{1d5e8}U' +
  '\u{1d5e9}V\u{1d5ea}W\u{1d5eb}X\u{1d5ec}Y\u{1d5ed}Z\u{1d5ee}a\u{1d5ef}b\u{1d5f0}c\u{1d5f1}d\u{1d5f2}e\u{1d5f3}f\u{1d5f4}g' +
  '\u{1d5f5}h\u{1d5f6}i\u{1d5f7}j\u{1d5f8}k\u{1d5f9}l\u{1d5fa}m\u{1d5fb}n\u{1d5fc}o\u{1d5fd}p\u{1d5fe}q\u{1d5ff}r\u{1d600}s' +
  '\u{1d601}t\u{1d602}u\u{1d603}v\u{1d604}w\u{1d605}x\u{1d606}y\u{1d607}z\u{1d608}A\u{1d609}B\u{1d60a}C\u{1d60b}D\u{1d60c}E' +
  '\u{1d60d}F\u{1d60e}G\u{1d60f}H\u{1d610}I\u{1d611}J\u{1d612}K\u{1d613}L\u{1d614}M\u{1d615}N\u{1d616}O\u{1d617}P\u{1d618}Q' +
  '\u{1d619}R\u{1d61a}S\u{1d61b}T\u{1d61c}U\u{1d61d}V\u{1d61e}W\u{1d61f}X\u{1d620}Y\u{1d621}Z\u{1d622}a\u{1d623}b\u{1d624}c' +
  '\u{1d625}d\u{1d626}e\u{1d627}f\u{1d628}g\u{1d629}h\u{1d62a}i\u{1d62b}j\u{1d62c}k\u{1d62d}l\u{1d62e}m\u{1d62f}n\u{1d630}o' +
  '\u{1d631}p\u{1d632}q\u{1d633}r\u{1d634}s\u{1d635}t\u{1d636}u\u{1d637}v\u{1d638}w\u{1d639}x\u{1d63a}y\u{1d63b}z\u{1d63c}A' +
  '\u{1d63d}B\u{1d63e}C\u{1d63f}D\u{1d640}E\u{1d641}F\u{1d642}G\u{1d643}H\u{1d644}I\u{1d645}J\u{1d646}K\u{1d647}L\u{1d648}M' +
  '\u{1d649}N\u{1d64a}O\u{1d64b}P\u{1d64c}Q\u{1d64d}R\u{1d64e}S\u{1d64f}T\u{1d650}U\u{1d651}V\u{1d652}W\u{1d653}X\u{1d654}Y' +
  '\u{1d655}Z\u{1d656}a\u{1d657}b\u{1d658}c\u{1d659}d\u{1d65a}e\u{1d65b}f\u{1d65c}g\u{1d65d}h\u{1d65e}i\u{1d65f}j\u{1d660}k' +
  '\u{1d661}l\u{1d662}m\u{1d663}n\u{1d664}o\u{1d665}p\u{1d666}q\u{1d667}r\u{1d668}s\u{1d669}t\u{1d66a}u\u{1d66b}v\u{1d66c}w' +
  '\u{1d66d}x\u{1d66e}y\u{1d66f}z\u{1d670}A\u{1d671}B\u{1d672}C\u{1d673}D\u{1d674}E\u{1d675}F\u{1d676}G\u{1d677}H\u{1d678}I' +
  '\u{1d679}J\u{1d67a}K\u{1d67b}L\u{1d67c}M\u{1d67d}N\u{1d67e}O\u{1d67f}P\u{1d680}Q\u{1d681}R\u{1d682}S\u{1d683}T\u{1d684}U' +
  '\u{1d685}V\u{1d686}W\u{1d687}X\u{1d688}Y\u{1d689}Z\u{1d68a}a\u{1d68b}b\u{1d68c}c\u{1d68d}d\u{1d68e}e\u{1d68f}f\u{1d690}g' +
  '\u{1d691}h\u{1d692}i\u{1d693}j\u{1d694}k\u{1d695}l\u{1d696}m\u{1d697}n\u{1d698}o\u{1d699}p\u{1d69a}q\u{1d69b}r\u{1d69c}s' +
  '\u{1d69d}t\u{1d69e}u\u{1d69f}v\u{1d6a0}w\u{1d6a1}x\u{1d6a2}y\u{1d6a3}z\u{1d6a4}i\u{1d6a8}A\u{1d6a9}B\u{1d6ac}E\u{1d6ad}Z' +
  '\u{1d6ae}H\u{1d6b0}l\u{1d6b1}K\u{1d6b3}M\u{1d6b4}N\u{1d6b6}O\u{1d6b8}P\u{1d6bb}T\u{1d6bc}Y\u{1d6be}X\u{1d6c2}a\u{1d6c4}y' +
  '\u{1d6ca}i\u{1d6ce}v\u{1d6d0}o\u{1d6d2}p\u{1d6d4}o\u{1d6d6}u\u{1d6e0}p\u{1d6e2}A\u{1d6e3}B\u{1d6e6}E\u{1d6e7}Z\u{1d6e8}H' +
  '\u{1d6ea}l\u{1d6eb}K\u{1d6ed}M\u{1d6ee}N\u{1d6f0}O\u{1d6f2}P\u{1d6f5}T\u{1d6f6}Y\u{1d6f8}X\u{1d6fc}a\u{1d6fe}y\u{1d704}i' +
  '\u{1d708}v\u{1d70a}o\u{1d70c}p\u{1d70e}o\u{1d710}u\u{1d71a}p\u{1d71c}A\u{1d71d}B\u{1d720}E\u{1d721}Z\u{1d722}H\u{1d724}l' +
  '\u{1d725}K\u{1d727}M\u{1d728}N\u{1d72a}O\u{1d72c}P\u{1d72f}T\u{1d730}Y\u{1d732}X\u{1d736}a\u{1d738}y\u{1d73e}i\u{1d742}v' +
  '\u{1d744}o\u{1d746}p\u{1d748}o\u{1d74a}u\u{1d754}p\u{1d756}A\u{1d757}B\u{1d75a}E\u{1d75b}Z\u{1d75c}H\u{1d75e}l\u{1d75f}K' +
  '\u{1d761}M\u{1d762}N\u{1d764}O\u{1d766}P\u{1d769}T\u{1d76a}Y\u{1d76c}X\u{1d770}a\u{1d772}y\u{1d778}i\u{1d77c}v\u{1d77e}o' +
  '\u{1d780}p\u{1d782}o\u{1d784}u\u{1d78e}p\u{1d790}A\u{1d791}B\u{1d794}E\u{1d795}Z\u{1d796}H\u{1d798}l\u{1d799}K\u{1d79b}M' +
  '\u{1d79c}N\u{1d79e}O\u{1d7a0}P\u{1d7a3}T\u{1d7a4}Y\u{1d7a6}X\u{1d7aa}a\u{1d7ac}y\u{1d7b2}i\u{1d7b6}v\u{1d7b8}o\u{1d7ba}p' +
  '\u{1d7bc}o\u{1d7be}u\u{1d7c8}p\u{1d7ca}F\u{1d7ce}O\u{1d7cf}l\u{1d7d8}O\u{1d7d9}l\u{1d7e2}O\u{1d7e3}l\u{1d7ec}O\u{1d7ed}l' +
  '\u{1d7f6}O\u{1d7f7}l\u{1e8c7}l\u{1ee00}l\u{1ee24}o\u{1ee64}o\u{1ee80}l\u{1ee84}o\u{1f12b}C\u{1f12c}R\u{1f130}A\u{1f131}B' +
  '\u{1f132}C\u{1f133}D\u{1f134}E\u{1f135}F\u{1f136}G\u{1f137}H\u{1f138}I\u{1f139}J\u{1f13a}K\u{1f13b}L\u{1f13c}M\u{1f13d}N' +
  '\u{1f13e}O\u{1f13f}P\u{1f140}Q\u{1f141}R\u{1f142}S\u{1f143}T\u{1f144}U\u{1f145}V\u{1f146}W\u{1f147}X\u{1f148}Y\u{1f149}Z' +
  '\u{1f74c}C\u{1f768}T\u{1fbf0}O\u{1fbf1}l';
// /generated-from-confusable-registry:CONFUSABLE_FOLD_PACKED

// generated-from-confusable-registry:INVISIBLE_STRIP_PACKED do not edit; run scripts/generate_confusable_tables.py
const INVISIBLE_STRIP_PACKED =
  '\u00ad\u0300\u0301\u0302\u0303\u0304\u0305\u0306\u0307\u0308\u0309\u030a\u030b\u030c\u030d\u030e' +
  '\u030f\u0310\u0311\u0312\u0313\u0314\u0315\u0316\u0317\u0318\u0319\u031a\u031b\u031c\u031d\u031e' +
  '\u031f\u0320\u0321\u0322\u0323\u0324\u0325\u0326\u0327\u0328\u0329\u032a\u032b\u032c\u032d\u032e' +
  '\u032f\u0330\u0331\u0332\u0333\u0334\u0335\u0336\u0337\u0338\u0339\u033a\u033b\u033c\u033d\u033e' +
  '\u033f\u0340\u0341\u0342\u0343\u0344\u0345\u0346\u0347\u0348\u0349\u034a\u034b\u034c\u034d\u034e' +
  '\u034f\u0350\u0351\u0352\u0353\u0354\u0355\u0356\u0357\u0358\u0359\u035a\u035b\u035c\u035d\u035e' +
  '\u035f\u0360\u0361\u0362\u0363\u0364\u0365\u0366\u0367\u0368\u0369\u036a\u036b\u036c\u036d\u036e' +
  '\u036f\u061c\u180e\u1ab0\u1ab1\u1ab2\u1ab3\u1ab4\u1ab5\u1ab6\u1ab7\u1ab8\u1ab9\u1aba\u1abb\u1abc' +
  '\u1abd\u1abe\u1abf\u1ac0\u1ac1\u1ac2\u1ac3\u1ac4\u1ac5\u1ac6\u1ac7\u1ac8\u1ac9\u1aca\u1acb\u1acc' +
  '\u1acd\u1ace\u1acf\u1ad0\u1ad1\u1ad2\u1ad3\u1ad4\u1ad5\u1ad6\u1ad7\u1ad8\u1ad9\u1ada\u1adb\u1adc' +
  '\u1add\u1ade\u1adf\u1ae0\u1ae1\u1ae2\u1ae3\u1ae4\u1ae5\u1ae6\u1ae7\u1ae8\u1ae9\u1aea\u1aeb\u1aec' +
  '\u1aed\u1aee\u1aef\u1af0\u1af1\u1af2\u1af3\u1af4\u1af5\u1af6\u1af7\u1af8\u1af9\u1afa\u1afb\u1afc' +
  '\u1afd\u1afe\u1aff\u1dc0\u1dc1\u1dc2\u1dc3\u1dc4\u1dc5\u1dc6\u1dc7\u1dc8\u1dc9\u1dca\u1dcb\u1dcc' +
  '\u1dcd\u1dce\u1dcf\u1dd0\u1dd1\u1dd2\u1dd3\u1dd4\u1dd5\u1dd6\u1dd7\u1dd8\u1dd9\u1dda\u1ddb\u1ddc' +
  '\u1ddd\u1dde\u1ddf\u1de0\u1de1\u1de2\u1de3\u1de4\u1de5\u1de6\u1de7\u1de8\u1de9\u1dea\u1deb\u1dec' +
  '\u1ded\u1dee\u1def\u1df0\u1df1\u1df2\u1df3\u1df4\u1df5\u1df6\u1df7\u1df8\u1df9\u1dfa\u1dfb\u1dfc' +
  '\u1dfd\u1dfe\u1dff\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2061\u2062' +
  '\u2063\u2064\u2066\u2067\u2068\u2069\u20d0\u20d1\u20d2\u20d3\u20d4\u20d5\u20d6\u20d7\u20d8\u20d9' +
  '\u20da\u20db\u20dc\u20dd\u20de\u20df\u20e0\u20e1\u20e2\u20e3\u20e4\u20e5\u20e6\u20e7\u20e8\u20e9' +
  '\u20ea\u20eb\u20ec\u20ed\u20ee\u20ef\u20f0\u20f1\u20f2\u20f3\u20f4\u20f5\u20f6\u20f7\u20f8\u20f9' +
  '\u20fa\u20fb\u20fc\u20fd\u20fe\u20ff\ufe00\ufe01\ufe02\ufe03\ufe04\ufe05\ufe06\ufe07\ufe08\ufe09' +
  '\ufe0a\ufe0b\ufe0c\ufe0d\ufe0e\ufe0f\ufe20\ufe21\ufe22\ufe23\ufe24\ufe25\ufe26\ufe27\ufe28\ufe29' +
  '\ufe2a\ufe2b\ufe2c\ufe2d\ufe2e\ufe2f\ufeff\u{e0100}\u{e0101}\u{e0102}\u{e0103}\u{e0104}\u{e0105}\u{e0106}\u{e0107}\u{e0108}' +
  '\u{e0109}\u{e010a}\u{e010b}\u{e010c}\u{e010d}\u{e010e}\u{e010f}\u{e0110}\u{e0111}\u{e0112}\u{e0113}\u{e0114}\u{e0115}\u{e0116}\u{e0117}\u{e0118}' +
  '\u{e0119}\u{e011a}\u{e011b}\u{e011c}\u{e011d}\u{e011e}\u{e011f}\u{e0120}\u{e0121}\u{e0122}\u{e0123}\u{e0124}\u{e0125}\u{e0126}\u{e0127}\u{e0128}' +
  '\u{e0129}\u{e012a}\u{e012b}\u{e012c}\u{e012d}\u{e012e}\u{e012f}\u{e0130}\u{e0131}\u{e0132}\u{e0133}\u{e0134}\u{e0135}\u{e0136}\u{e0137}\u{e0138}' +
  '\u{e0139}\u{e013a}\u{e013b}\u{e013c}\u{e013d}\u{e013e}\u{e013f}\u{e0140}\u{e0141}\u{e0142}\u{e0143}\u{e0144}\u{e0145}\u{e0146}\u{e0147}\u{e0148}' +
  '\u{e0149}\u{e014a}\u{e014b}\u{e014c}\u{e014d}\u{e014e}\u{e014f}\u{e0150}\u{e0151}\u{e0152}\u{e0153}\u{e0154}\u{e0155}\u{e0156}\u{e0157}\u{e0158}' +
  '\u{e0159}\u{e015a}\u{e015b}\u{e015c}\u{e015d}\u{e015e}\u{e015f}\u{e0160}\u{e0161}\u{e0162}\u{e0163}\u{e0164}\u{e0165}\u{e0166}\u{e0167}\u{e0168}' +
  '\u{e0169}\u{e016a}\u{e016b}\u{e016c}\u{e016d}\u{e016e}\u{e016f}\u{e0170}\u{e0171}\u{e0172}\u{e0173}\u{e0174}\u{e0175}\u{e0176}\u{e0177}\u{e0178}' +
  '\u{e0179}\u{e017a}\u{e017b}\u{e017c}\u{e017d}\u{e017e}\u{e017f}\u{e0180}\u{e0181}\u{e0182}\u{e0183}\u{e0184}\u{e0185}\u{e0186}\u{e0187}\u{e0188}' +
  '\u{e0189}\u{e018a}\u{e018b}\u{e018c}\u{e018d}\u{e018e}\u{e018f}\u{e0190}\u{e0191}\u{e0192}\u{e0193}\u{e0194}\u{e0195}\u{e0196}\u{e0197}\u{e0198}' +
  '\u{e0199}\u{e019a}\u{e019b}\u{e019c}\u{e019d}\u{e019e}\u{e019f}\u{e01a0}\u{e01a1}\u{e01a2}\u{e01a3}\u{e01a4}\u{e01a5}\u{e01a6}\u{e01a7}\u{e01a8}' +
  '\u{e01a9}\u{e01aa}\u{e01ab}\u{e01ac}\u{e01ad}\u{e01ae}\u{e01af}\u{e01b0}\u{e01b1}\u{e01b2}\u{e01b3}\u{e01b4}\u{e01b5}\u{e01b6}\u{e01b7}\u{e01b8}' +
  '\u{e01b9}\u{e01ba}\u{e01bb}\u{e01bc}\u{e01bd}\u{e01be}\u{e01bf}\u{e01c0}\u{e01c1}\u{e01c2}\u{e01c3}\u{e01c4}\u{e01c5}\u{e01c6}\u{e01c7}\u{e01c8}' +
  '\u{e01c9}\u{e01ca}\u{e01cb}\u{e01cc}\u{e01cd}\u{e01ce}\u{e01cf}\u{e01d0}\u{e01d1}\u{e01d2}\u{e01d3}\u{e01d4}\u{e01d5}\u{e01d6}\u{e01d7}\u{e01d8}' +
  '\u{e01d9}\u{e01da}\u{e01db}\u{e01dc}\u{e01dd}\u{e01de}\u{e01df}\u{e01e0}\u{e01e1}\u{e01e2}\u{e01e3}\u{e01e4}\u{e01e5}\u{e01e6}\u{e01e7}\u{e01e8}' +
  '\u{e01e9}\u{e01ea}\u{e01eb}\u{e01ec}\u{e01ed}\u{e01ee}\u{e01ef}';
// /generated-from-confusable-registry:INVISIBLE_STRIP_PACKED

// Decode the packed strings ONCE at module load into a fold Map (source codepoint ->
// ASCII letter) and a strip Set. CODE-POINT iteration (`[...str]` / `for...of`), never
// a /[...]/ char class: 919 astral fold sources + 240 astral strip codepoints would be
// corrupted to U+FFFD by a non-/u regex, and the values are byte-identical to Python's
// str.translate. Mirrors _decode_fold_table in scripts/filter_findings.py.
export const CONFUSABLE_FOLD = new Map();
{
  const cps = [...CONFUSABLE_FOLD_PACKED];
  for (let i = 0; i < cps.length; i += 2) {
    CONFUSABLE_FOLD.set(cps[i].codePointAt(0), cps[i + 1]);
  }
}
export const INVISIBLE_STRIP = new Set(
  [...INVISIBLE_STRIP_PACKED].map((c) => c.codePointAt(0)),
);

// Port of _fold_confusables: fold lookalikes to ASCII and delete zero-width/boundary
// breakers in one CODE-POINT pass. A stripped codepoint contributes nothing (the
// str.translate None); an unmapped one passes through unchanged. A `for...of` loop,
// never an inline boolean-test literal: the filter-twin unicode guard pins that
// census at 3.
export function foldConfusables(text) {
  let out = '';
  for (const ch of text) {
    const cp = ch.codePointAt(0);
    if (INVISIBLE_STRIP.has(cp)) continue;
    out += CONFUSABLE_FOLD.get(cp) ?? ch;
  }
  return out;
}
// Port of _first_match + the #242 UNION scan: the pattern SOURCE of the first
// regex matching the RAW `text`, or -- only if `folded` is a distinct
// casefold-reachable-folded copy of `text` -- the first matching the FOLDED
// text. The raw pass wins reason selection, so a finding that already matched
// at HEAD keeps its exact reason and the fold can only ADD detections. The
// scanned regexes carry no `g` flag, so `.test()` is stateless and safe to run
// twice per pattern.
function firstMatch(patterns, text, folded) {
  for (const rx of patterns) {
    if (rx.test(text)) return rx.source;
  }
  if (folded !== undefined && folded !== text) {
    for (const rx of patterns) {
      if (rx.test(folded)) return rx.source;
    }
  }
  return null;
}

// suggestion (and, since #213, claude_md_rule/spec_text) is rendered into
// posted PR/MR comments and reports, so payload-bearing advice must not
// reach a human -- but a benign finding must not die for its advice
// (imperative security advice like "Never disable TLS verification"
// legitimately resembles these patterns), so a match strips the field
// instead of eliminating the finding (#62).
// generated-from-filter-pattern-registry:SUGGESTION_SETS do not edit; run scripts/generate_filter_patterns.py
export const SUGGESTION_SETS = [
  ['contains shell command pattern', INJECTION_SHELL_PATTERNS],
  ['contains visit-URL pattern', INJECTION_URL_PATTERNS],
  ['contains encoded payload pattern', INJECTION_ENCODED_PATTERNS],
  ['contains bypass/auto-approve instruction', INJECTION_BYPASS_PATTERNS],
  ['uses instructional tone', INJECTION_INSTRUCTIONAL_PATTERNS],
  ['recommends introducing vulnerability', INJECTION_VULN_INTRO_PATTERNS],
  ['matches injection marker', INJECTION_BODY_PATTERNS],
];
// /generated-from-filter-pattern-registry:SUGGESTION_SETS

// Prose fields scanned by SUGGESTION_SETS and stripped (never eliminated) on
// a match -- #62 introduced this for `suggestion`; #213 extends it to
// `claude_md_rule`/`spec_text`, the two repo-derived citation fields the
// conventions-and-intent agent quotes verbatim into posted comments (a
// higher-risk injection source than agent-authored suggestion). One shared
// list, mirrored by scripts/filter_findings.py's
// _INJECTION_STRIPPED_PROSE_FIELDS -- a lockstep test
// (tests/test_filter_findings.py) asserts the two lists agree element-wise
// so adding a field to only one twin goes red. Order is scan/strip order:
// `suggestion` first, so its bytes are reproduced exactly when it is the
// field that matches.
export const INJECTION_STRIPPED_PROSE_FIELDS = ['suggestion', 'claude_md_rule', 'spec_text'];

// Delivery bound on suggested_fix_code content (#63/D8) -- the SAME two
// numbers bound the field at render time in scripts/post_review.py
// (`_FIX_MAX_LINES` / `_FIX_MAX_CHARS`) and in the Python filter twin
// (scripts/filter_findings.py); change all three together. tests/test_filter_findings.py's
// lockstep test (#63 round-1 F8) regex-parses all three assignments and asserts they agree.
//
// Both bound checks below measure the SAME normalized text the render-time gate does
// (post_review.py's dedicated fence normalizer, #63 round-1 F2/F5-B): strip exactly ONE
// trailing "\n" (the terminator) and nothing else, then lines = split("\n") elements,
// chars = CODE POINTS of that normalized text -- .length counts UTF-16 units, which
// disagrees with Python's len() (code points) for any astral character, so the twin uses
// [...code].length instead (#63 round-1 F6).
const FIX_MAX_LINES = 100;
const FIX_MAX_CHARS = 8000;

// Mirrors scripts/filter_findings.py's _normalize_fix_code_for_bound.
function normalizeFixCodeForBound(code) {
  return code.endsWith('\n') ? code.slice(0, -1) : code;
}

// Port of _strip_injected_prose_fields. Only called for a finding that
// already survived all ten title/description heuristics below. Scans
// INJECTION_STRIPPED_PROSE_FIELDS in list order; each PRESENT field is
// independently stripped -- never eliminates the finding -- on a non-string
// type or the first SUGGESTION_SETS pattern match (#62, extended to
// claude_md_rule/spec_text by #213). Scanning continues after a match: every
// matching field strips.
//
// Returns [keptFinding, firstPatternStrip]: firstPatternStrip is
// [field, phrase] for the FIRST field a PATTERN (not a type violation)
// stripped, or null -- this is what feeds stripSuggestedFixCodeIfNeeded's
// propagation trigger (#63/D8c, #213/D2/D7): a type-violation strip never
// propagates, and among pattern strips only the first-in-order field names
// the reason.
function stripInjectedProseFields(finding) {
  // A present field that is not a string (possible via the retained Python
  // CLI's unvalidated --input and checkpoint resume; the JS dispatch
  // boundary's JSON schema pins string-only) is inert to the scan below; a
  // dict/list/number would reach post_review's str() coercion verbatim, and a
  // null (rendered as absent downstream) is stripped too so presence +
  // non-string type is the whole trigger (#62).
  let kept = finding;
  let firstPatternStrip = null;
  for (const field of INJECTION_STRIPPED_PROSE_FIELDS) {
    if (field in kept && typeof kept[field] !== 'string') {
      kept = { ...kept };
      delete kept[field];
      kept[`${field}_removed_by`] = 'injection';
      kept[`${field}_removal_reason`] = `${field} is not a string`;
      continue;
    }
    const value = typeof kept[field] === 'string' ? kept[field] : '';
    if (!value) continue;
    const valueFolded = foldConfusables(value);
    for (const [phrase, patterns] of SUGGESTION_SETS) {
      const m = firstMatch(patterns, value, valueFolded);
      if (m) {
        kept = { ...kept };
        delete kept[field];
        kept[`${field}_removed_by`] = 'injection';
        kept[`${field}_removal_reason`] = `${field} ${phrase}: ${JSON.stringify(m)}`;
        if (firstPatternStrip === null) firstPatternStrip = [field, phrase];
        break;
      }
    }
  }
  return [kept, firstPatternStrip];
}

// Port of _strip_suggested_fix_code_if_needed (#63/D8). Mirrors
// stripInjectedProseFields's shape for suggested_fix_code: non-string strip
// first, then oversize, then propagation-on-pattern-strip. Independent of
// whether any prose field is even present -- the first two checks fire on
// their own regardless of firstPatternStrip.
//
// Deliberately NO pattern scan of the code content itself: #62 measured
// content-pattern sets killing legitimate fixes, and code trips them harder
// than prose does.
function stripSuggestedFixCodeIfNeeded(finding, firstPatternStrip) {
  if (!('suggested_fix_code' in finding)) return finding;
  const code = finding.suggested_fix_code;
  if (typeof code !== 'string') {
    const kept = { ...finding };
    delete kept.suggested_fix_code;
    kept.suggested_fix_code_removed_by = 'injection';
    kept.suggested_fix_code_removal_reason = 'suggested_fix_code is not a string';
    return kept;
  }
  const normalized = normalizeFixCodeForBound(code);
  if (normalized.split('\n').length > FIX_MAX_LINES || [...normalized].length > FIX_MAX_CHARS) {
    const kept = { ...finding };
    delete kept.suggested_fix_code;
    kept.suggested_fix_code_removed_by = 'injection';
    kept.suggested_fix_code_removal_reason = 'suggested_fix_code exceeds the delivery bound';
    return kept;
  }
  if (firstPatternStrip !== null) {
    // A patch whose accompanying prose was flagged as injection must not
    // survive as a one-click apply -- pattern-free and byte-identical to the
    // Python twin's reason (the parity test only prefix-compares
    // `${field}_removal_reason`, not this key). `field` is the FIRST scanned
    // field (list order) a pattern stripped (#213/D2/D7); "suggestion"
    // reproduces today's bytes.
    const [propField, propPhrase] = firstPatternStrip;
    const kept = { ...finding };
    delete kept.suggested_fix_code;
    kept.suggested_fix_code_removed_by = 'injection';
    kept.suggested_fix_code_removal_reason = `${propField} carried ${propPhrase}`;
    return kept;
  }
  return finding;
}

// Port of _strip_injected_prose_fields + _strip_suggested_fix_code_if_needed,
// composed as the SINGLE per-finding step applyInjectionFilter runs for every
// KEPT finding. Post-#253 role: the belt (stages.js) no longer routes
// challengeOut.findings/.unverified through this function directly -- their
// KEPT path now runs applyReplayInjectionScan (injectionScanCore), which
// calls this same strip composition INLINE (see injectionScanCore's own kept
// branch) before a survivor is returned. This export's one remaining caller
// is stages.js's stripEliminatedList, applied to the persisted
// challengeOut.eliminated bucket alone -- the scan's eliminated path never
// strips a finding's prose fields, so a belt-eliminated (or pre-#213
// replayed) entry still needs this pass before it lands in
// checkpoint-all.json. Idempotent: a finding already stripped (by either
// caller) has nothing left to match, so a second pass here is a no-op --
// safe to call again on a resume-of-a-resume.
export function applyInjectedProseStrip(finding) {
  const [stripped, firstPatternStrip] = stripInjectedProseFields(finding);
  return stripSuggestedFixCodeIfNeeded(stripped, firstPatternStrip);
}

// Port of _injection_scan_core (Python twin). All 10 heuristics (4 gated by
// includeH4, see that parameter's doc comment below), in the same order as
// the Python original so `reasons[0]` (used in the stderr-equivalent
// warning, not asserted here) lines up. Heuristic #10 (duplicate signature)
// is STATEFUL
// across the input list — the FIRST (title,file,line_start) occurrence
// survives, later ones are flagged — so caller input order is load-bearing.
// Scans only title + description; a finding that passes then has each of
// INJECTION_STRIPPED_PROSE_FIELDS (if any) scanned separately by
// stripInjectedProseFields (#62, extended #213).
//
// #256: all seven SUGGESTION_SETS content sets (shell/url/encoded/bypass/
// instructional/vuln-intro/body-marker) scan `combined` (title+description)
// uniformly -- there is no separate title-only pass. A payload split across
// fields (the directive in title, the blob/body in description) still
// fires, since the rendered PR comment concatenates them into one coherent
// instruction (#252 Finding 1, generalized to all seven sets by #256). Every
// set's reason string is bare (no "title "/"description " field-attribution
// prefix, matching shell/url/encoded's pre-existing style) since the
// scanned text is neither field alone; field attribution is a deliberately
// dropped capability (#256 record). This is a strict superset of scanning
// `title`/`description` separately: none of the seven sets' patterns anchor
// with `^`/`$`/`\A`/`\Z`/`(?m)` (guarded by
// tests/test_filter_twins_unicode_guard.py), and the union whitespace class
// joining title and description includes `\n`, so a match spanning either
// field alone still matches `combined`. The encoded set is directive-gated
// with an adjacency window (decode verb or sink syntax within up to ~40
// characters of the blob); url ships only visit/download-from (#255: the
// two directive-gated long-bare-URL entries were removed -- they
// false-fired on legitimate findings using the same vocabulary), so url's
// directive verb must sit immediately adjacent to the URL, not within a
// numeric character bound. A far-apart split (outside the adjacency window,
// where one applies) still evades by design (adjacency-gating is inherently
// local); that residual is accepted.
// #253: shared core behind applyInjectionFilter/applyReplayInjectionScan.
// `includeH4` gates heuristic 4 (short-description + high-confidence) -- the
// ONE heuristic that reads finding.confidence, a field detectDisagreement
// mutates IN PLACE after this scan first runs at filter time (the +10
// consensus boost on a corroborated finding). Heuristics 1/2/3/5-10 read
// only title/description/file/line_start/id -- static content that cannot
// change between a finding's first scan and a later re-scan -- so they are
// safe to re-run against anything that already passed them once; heuristic 4
// is not, because a finding that failed it (80 < 85) at record time can pass
// it (90 >= 85) after a later stage boosts confidence, which would make a
// re-scan eliminate a finding the pipeline just corroborated. See
// applyReplayInjectionScan below for the caller this exists for.
function injectionScanCore(findings, includeH4) {
  const kept = [];
  const eliminated = [];
  const seenSignatures = new Map();

  for (const finding of findings) {
    const title = asText(finding.title);
    const description = asText(finding.description);
    const filepath = asText(finding.file);
    const confidence = asConfidence(finding.confidence);
    const combined = `${title}\n${description}`;
    // #242 union scan: fold each scanned text ONCE per finding; the content
    // sets below scan raw-then-folded via firstMatch.
    const combinedFolded = foldConfusables(combined);
    const titleFolded = foldConfusables(title);

    const reasons = [];

    let m = firstMatch(INJECTION_SHELL_PATTERNS, combined, combinedFolded);
    if (m) reasons.push(`contains shell command pattern: ${JSON.stringify(m)}`);

    // 2a/2b: combined title+description (#252 Finding 1 -- see doc comment above).
    m = firstMatch(INJECTION_URL_PATTERNS, combined, combinedFolded);
    if (m) reasons.push(`contains visit-URL pattern: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_ENCODED_PATTERNS, combined, combinedFolded);
    if (m) reasons.push(`contains encoded payload pattern: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_BYPASS_PATTERNS, combined, combinedFolded);
    if (m) reasons.push(`contains bypass/auto-approve instruction: ${JSON.stringify(m)}`);

    if (includeH4) {
      const wordCount = countWords(description);
      if (wordCount < MIN_BODY_WORDS && confidence >= HIGH_CONFIDENCE_THRESHOLD) {
        reasons.push(`suspiciously short description (${wordCount} words) with high confidence (${confidence})`);
      }
    }

    m = firstMatch(INJECTION_INSTRUCTIONAL_PATTERNS, combined, combinedFolded);
    if (m) reasons.push(`uses instructional tone: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_VULN_INTRO_PATTERNS, combined, combinedFolded);
    if (m) reasons.push(`recommends introducing vulnerability: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_TITLE_PATTERNS, title, titleFolded);
    if (m) reasons.push(`title matches placeholder pattern: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_BODY_PATTERNS, combined, combinedFolded);
    if (m) reasons.push(`matches injection marker: ${JSON.stringify(m)}`);

    if (!filepath || /<[^\n]*?>|\{[^\n]*?\}/.test(filepath)) {
      reasons.push(`file path is empty or contains template markers: ${JSON.stringify(filepath)}`);
    }

    // Signature key: mirrors Python's (_WS_TRIM_RE.sub("", title.lower()),
    // file, line_start) tuple key via JSON.stringify of the equivalent array --
    // structural equality, immune to collisions a hand-rolled string-
    // concatenation key could hit. Deliberately built on the UNFOLDED title:
    // heuristic 7 scans folded text (#242), but this signature keeps HEAD's raw
    // title, so two fold-identical titles still hash distinct exactly as at HEAD
    // (dedup never folded). The title strip is the union whitespace class via
    // `WS_TRIM_RE.replace` (#244 (a), the shared union-trim constant, GLOBAL so
    // both leading AND trailing runs go), matching Python's
    // `str.strip()`-vs-JS-`trim()` six-codepoint skew; `line_start` stays RAW
    // here, NOT routed through `lineBucket`.
    const sig = JSON.stringify([title.toLowerCase().replace(WS_TRIM_RE, ''), filepath, finding.line_start]);
    if (seenSignatures.has(sig)) {
      reasons.push(`duplicate of finding ${JSON.stringify(seenSignatures.get(sig))}`);
    } else {
      seenSignatures.set(sig, finding.id !== undefined && finding.id !== null ? finding.id : title);
    }

    if (reasons.length) {
      const elim = { ...finding, eliminated_by: 'injection', elimination_reason: reasons.join('; ') };
      eliminated.push(elim);
    } else {
      const [strippedFinding, firstPatternStrip] = stripInjectedProseFields(finding);
      kept.push(stripSuggestedFixCodeIfNeeded(strippedFinding, firstPatternStrip));
    }
  }

  return { kept, eliminated };
}

// Port of apply_injection_filter -- the record-time entry point (filterStage,
// via applyFilterPipeline), byte-identical to its pre-#253 shape: all 10
// heuristics, including heuristic 4.
export function applyInjectionFilter(findings) {
  return injectionScanCore(findings, true);
}

// #253 replay filtering belt (stages.js): re-scans findings that already
// survived applyInjectionFilter once, at record time, against a challenge
// checkpoint the pipeline is now REPLAYING (persisted by an earlier version,
// under earlier content patterns) or a fresh challenge-stage output (a no-op
// by construction there, since filterStage already ran this same content
// scan this run). Structurally excludes heuristic 4 -- see injectionScanCore's
// doc comment -- so the belt's callable unit is confidence-free BY
// CONSTRUCTION, not by caller discipline. Heuristic 10 (duplicate signature)
// is proven unable to newly fire here: nothing between record-time
// applyInjectionFilter and a challenge checkpoint mutates a finding's
// (title, file, line_start) triple (detectDisagreement/consolidateCrossAgent/
// applyChallenges touch only confidence/severity/stamp fields), and a dedup
// re-run over a SUBSET of the originally-deduped set can only fire fewer
// times, never newly.
export function applyReplayInjectionScan(findings) {
  return injectionScanCore(findings, false);
}

// --- Exclusions loader -------------------------------------------------------

// Port of load_exclusions. Python reads a file by path; this twin takes the
// exclusions markdown TEXT directly. A fenced code block wins if present
// (returns immediately on the first one found); otherwise falls back to
// bullet-list ("- " / "* ") items scanned line by line.
export function loadExclusions(text) {
  if (text === undefined || text === null) return [];

  const patterns = [];

  const blockMatch = REVIEW_EXCL_BLOCK_RE.exec(text);
  if (blockMatch) {
    for (const rawLine of splitReviewLines(blockMatch[1])) {
      const line = rawLine.replace(WS_TRIM_RE, '');
      if (line && !line.startsWith('#')) patterns.push(line);
    }
    return patterns;
  }

  // Fallback: bullet list items. splitReviewLines splits on the universal-newline
  // alternation \r\n | \r | \n, so CRLF, a lone \r, and \n all break the line
  // identically (matching Python's universal-newline file read); the tail is
  // `([^\n]+)$` (explicit, identical
  // in both engines) rather than `(.+)$`, whose `.` excluded \r/U+2028/U+2029
  // and silently zeroed a user's exclusions on such input (issue #243).
  for (const line of splitReviewLines(text)) {
    const m = REVIEW_EXCL_BULLET_RE.exec(line);
    if (m) patterns.push(m[1].replace(WS_TRIM_RE, ''));
  }

  return patterns;
}

// re.escape equivalent: escape every regex metacharacter so the pattern is
// searched as a literal substring.
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

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

// --- Typed-field coercion (#266) --------------------------------------------
//
// A scanned finding field must contribute a value of its expected type or
// that type's default -- never a stringified null, never a crash. Applied
// wherever title/description/file/severity (string-typed) or confidence
// (numeric-typed) is read from a finding whose provenance is not schema-
// validated (a replayed checkpoint from an earlier pipeline version).
// Before this, a bare `finding.field || ''` or template-literal read let a
// non-string value (most commonly an explicit `null`) reach a regex test,
// a `.length`/`.toLowerCase()` call, or an ordering comparison (`<`), or
// land as the literal text "null" in a scanned string -- divergent from
// the Python twin's "None" spelling, and in Python's case, an outright
// TypeError. `severity` additionally keeps its historical "default to low"
// fallback: `asText(value) || 'low'`, not a bare `asText(value)`, so an
// empty or non-string severity still becomes 'low' rather than ''.
// Python's twins are `_as_text`/`_as_confidence` (scripts/filter_findings.py).
function asText(value) {
  return typeof value === 'string' ? value : '';
}

function asConfidence(value) {
  return typeof value === 'number' && !Number.isNaN(value) ? value : 0;
}

// Leading/trailing trim of the union whitespace class (#244 (a)). ONE constant
// shared by four call sites: the dedup-signature title strip AND the three
// review-line strips (loadExclusions' fenced block + bullet fallback, and
// parseReviewMd's ignore item), all of which had a per-line `trim()` whose
// Python twin `str.strip()` disagreed on the same six codepoints -- silently
// zeroing a user exclusion/ignore pattern that carried one. GLOBAL so both the
// `^...` and `...$` runs go (a non-global replace would drop only the leading
// run). Mirrors Python's module-level `_WS_TRIM_RE.sub("", ...)`;
// registry-sourced (an INLINE_SITES row).
export const WS_TRIM_RE = /^[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+|[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+$/g;

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
export function pyIntOrNull(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? Math.trunc(value) : null;
  if (typeof value === 'boolean') return value ? 1 : 0; // Python bool is an int subclass
  if (typeof value === 'string') {
    // #244 (b): the union whitespace class + ASCII [0-9], so this twin and the
    // Python `_INT_COERCE_RE` accept/reject the same strings. parseInt runs on
    // the CAPTURE m[1], never the raw value -- parseInt only skips the JS trim
    // set, so a U+001C-U+001F/U+0085 prefix on the raw value yields NaN (then a
    // 'file:NaN' consolidation_key); the digit capture sidesteps it entirely.
    const m = /^[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*([+-]?[0-9]+)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*$/.exec(value);
    return m ? parseInt(m[1], 10) : null;
  }
  return null; // null/undefined/object/array -> TypeError in Python
}

// Port of the parameterized `_bucket` helper shared by `_line_bucket`
// (detect_disagreement, proximity 10) and `group_by_proximity` (proximity 5):
// round(int(line) / proximity) * proximity. The `int()` truncation happens
// BEFORE the (banker's-rounding) division -- dropping it diverges on
// non-integer line_start values (line_start=25.7 -> int()=25 -> bucket 20,
// NOT round(25.7/10)*10=30; see the non_integer_line_start fixture).
export function lineBucket(line, proximity) {
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

  // Phase 3: consensus grouping (file + line_bucket(10) + degraded) over the
  // active set. `degraded` (origin === 'unknown') is folded into the grouping
  // key so a degraded (verify-echo-unavailable) finding only corroborates
  // other degraded findings, and a verified finding only corroborates other
  // verified findings -- never across (#73 D3a). On a UNIFORM-origin run
  // (all-verified or all-degraded), `degraded` is a constant across every
  // finding, so it changes no group membership and the boosted output is
  // byte-identical to before this extension (#73 req 2 regression pin).
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
// generated-from-filter-pattern-registry:FUNCTIONAL_VIOLATION_KEYWORDS do not edit; run scripts/generate_filter_patterns.py
const FUNCTIONAL_VIOLATION_KEYWORDS =
  /\bcrash\b|\bdata[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+loss\b|\bsilent(?:ly)?\b|\bincorrect\b|\bwrong\b|\bfail(?:s|ure)?\b|\bruntime[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+error\b|\bexception\b|\bpanic\b|\bundefined[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+behavio(?:u)?r\b/i;
// /generated-from-filter-pattern-registry:FUNCTIONAL_VIOLATION_KEYWORDS

// Keywords that promote type_design findings specifically. Ported verbatim
// from _TYPE_SAFETY_BUG_KEYWORDS.
// generated-from-filter-pattern-registry:TYPE_SAFETY_BUG_KEYWORDS do not edit; run scripts/generate_filter_patterns.py
const TYPE_SAFETY_BUG_KEYWORDS =
  /\bruntime\b|\bcastexception\b|\btype[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+error\b|\bclasscastexception\b|\bnull[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+pointer\b|\bnullpointer\b|\btype[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+mismatch\b/i;
// /generated-from-filter-pattern-registry:TYPE_SAFETY_BUG_KEYWORDS

// Keyword patterns indicating a test-analyzer finding describes a functional
// correctness bug that EXISTS TODAY (vs. a coverage gap). Ported verbatim,
// in order, from _TEST_CORRECTNESS_PATTERNS -- shared by routeByDimension's
// test_coverage branch and isTestCorrectnessFinding's promotion check.
// generated-from-filter-pattern-registry:TEST_CORRECTNESS_PATTERNS do not edit; run scripts/generate_filter_patterns.py
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
// /generated-from-filter-pattern-registry:TEST_CORRECTNESS_PATTERNS

// Port of _route_by_dimension. Returns "main", "suggestion", or null (fall
// through to agent-based routing in tagFindings).
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

// Port of _is_test_correctness_finding.
function isTestCorrectnessFinding(finding) {
  const combined = `${asText(pyGet(finding, 'title', ''))}\n${asText(pyGet(finding, 'description', ''))}`;
  return TEST_CORRECTNESS_PATTERNS.some((rx) => rx.test(combined));
}

// --- Proximity grouping + cross-agent dedup ---------------------------------

// Port of group_by_proximity. Returns a Map keyed by JSON.stringify([file,
// bucket]) -- an internal grouping key with no Python equivalent string
// form; only consolidateCrossAgent (and, later, applyChallenges per the brief)
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

// Port of consolidate_cross_agent (#22 D1 -- replaces the old dedup_cross_agent
// eliminator). NOTHING is dropped: for each proximity group (same file, same
// 5-line bucket) that has 2+ findings from 2+ DIFFERENT agents, every member
// with a truthy "id" is stamped with a shared `consolidation_key` and exactly
// one of them gets `consolidation_primary: true` (the rest get `false`).
// Same-agent-only groups and singletons get no stamps at all. A finding
// without a truthy "id" is immune -- it passes through completely unstamped,
// mirroring the old eliminator's "no id, no dedup" immunity.
//
// Primary selection reuses the OLD winner key (isCore, confidence,
// description.length), all DESCENDING via one stable composite comparator --
// this is deliberately origin-blind (#22 D3): since consolidation never drops
// anything, origin cannot cost delivery here, unlike rankKey/detectDisagreement
// which do gate on origin. Matches Python's `sorted(group, key=_winner_key,
// reverse=True)`, which for tied keys preserves original relative order
// (Python sort stability + reverse=True keeps ties in forward order, not
// reversed) -- V8's sort is equally stable, so a single multi-key comparator
// reproduces this without a second pass. The primary is the first RANKED
// member that actually has a truthy id (an id-less top-ranked member cannot
// carry the stamp, so ranking is walked until one does).
//
// EXPORTED for reuse by applyChallenges — keep this a standalone plain
// function with no closure over filterFindings-only state.
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
const CONVENTIONS_AGENT = 'conventions-and-intent';
const COMMENT_ACCURACY_DIMENSIONS = new Set(['comment-accuracy', 'documentation', 'doc-accuracy']);

// Port of tag_findings. Step 1 (cross-agent consolidation, D1 -- stamps,
// never drops) -> per-finding routeByDimension -> agent-based fallback.
// Returns { tagged, consolidatedCount, mainCount, suggestionCount }.
export function tagFindings(findings) {
  const { findings: tagged, consolidatedCount } = consolidateCrossAgent(findings);

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

  return { tagged, consolidatedCount, mainCount, suggestionCount };
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
  const exclusionsRemoved = elimExclusions.length;

  const { kept: afterInjection, eliminated: elimInjection } = applyInjectionFilter(afterExclusions);
  allEliminated.push(...elimInjection);
  const injectionsRemoved = elimInjection.length;
  // One `{field}s_removed` stat per INJECTION_STRIPPED_PROSE_FIELDS entry --
  // looping the shared list (rather than one hardcoded .filter() per field)
  // means adding a field to the list is the only edit a future extension
  // needs (#213).
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
      cross_agent_consolidated: consolidatedCount,
      test_analyzer_promoted: promotedCount,
      tagged_main: mainCount,
      tagged_suggestion: suggestionCount,
    },
    generated_at: generatedAt,
  };
}

// Port of apply_exclusions. First pattern (in list order) whose literal,
// case-insensitive substring appears in "title\ndescription\nsuggestion" wins.
// suggestion is included because it is rendered into posted PR/MR comments same
// as description -- user-authored ignore patterns are the user's kill-switch
// over everything that gets rendered (#62).
//
// claude_md_rule/spec_text are also rendered into posted comments (#213 gives
// them the same seven-set injection scan as suggestion), but are deliberately
// NOT added here: the actual discriminator is not "gets rendered" (true of
// all three) but cost. A `suggestion` exclusion match costs only that one
// agent-authored field; claude_md_rule/spec_text quote the user's own repo
// text, and a common CLAUDE.md phrasing (e.g. "MUST") would, via this
// whole-finding elimination, mass-eliminate the conventions dimension for an
// unbounded recall cost that the field-strip mechanism above does not carry.
// A user kill-switch reaching rendered citation text was declined on the
// #247 measurement (2026-08-31): the natural CLAUDE.md pattern eliminates 0
// findings today and widens 12 via model boilerplate, not user repo text.
export function applyExclusions(findings, exclusionPatterns) {
  if (!exclusionPatterns || !exclusionPatterns.length) return { kept: findings, eliminated: [] };

  const kept = [];
  const eliminated = [];

  for (const finding of findings) {
    const title = asText(finding.title);
    const description = asText(finding.description);
    const suggestion = typeof finding.suggestion === 'string' ? finding.suggestion : '';
    const combined = `${title}\n${description}\n${suggestion}`;

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
