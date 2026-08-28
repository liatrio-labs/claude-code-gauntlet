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

  // Every key regex is anchored to the start of a line (ignoring leading
  // whitespace) via `^` + the `m` flag. A `#` before the key -- a commented-out
  // example line, e.g. `# confidence_threshold: 70` in a scaffolding template --
  // is not in the `[ \t]*` leading-whitespace class, so it breaks the anchor and
  // the line is correctly ignored. Without this anchor, a commented example
  // silently became live config (issue #94 adversarial review F1).
  let m = /^[ \t]*confidence_threshold\s*[:=]\s*(\d+)/m.exec(blockText);
  if (m) config.confidence_threshold = parseInt(m[1], 10);

  m = /^[ \t]*security_min_confidence\s*[:=]\s*(\d+)/m.exec(blockText);
  if (m) config.security_min_confidence = parseInt(m[1], 10);

  m = /^[ \t]*severity_threshold\s*[:=]\s*(critical|high|medium|low)/im.exec(blockText);
  if (m) config.severity_threshold = m[1].toLowerCase();

  // ignore: consecutive "-"-led lines, indentation-tolerant (spaces or tabs).
  // The `ignore:` anchor itself. Same rationale: `^[ \t]*` before it, never `#`.
  const ignoreSection = /^[ \t]*ignore\s*:\s*\n((?:[ \t]*-[^\n]*\n?)+)/m.exec(blockText);
  if (ignoreSection) {
    for (const line of ignoreSection[1].split('\n')) {
      const item = line.replace(/^\s*-\s*/, '').trim();
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

// #254 (F13): the four "<word> finding" entries picked up the union
// whitespace class between the word and "finding" (previously a literal
// space) -- see the #254 record.
const INJECTION_TITLE_PATTERNS = [
  /\bTODO\b/i,
  /\bFIXME\b/i,
  /\bPlaceholder\b/i,
  /\bExample[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b/i,
  /\bSample[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b/i,
  /\btest[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b/i,
  /\bdemo[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+finding\b/i,
];

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
const INJECTION_BODY_PATTERNS = [
  /<finding(?:[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff][^>]*)?>/i,
  /<example(?:[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff][^>]*)?>/i,
  /\[[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*INSERT[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\]/i,
  /\[[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*INSERT\b[^\]]*\b(?:FINDING|TITLE|TEXT|PLACEHOLDER|HERE)\b[^\]]*\]/i,
  /lorem[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+ipsum/i,
];

const INJECTION_SHELL_PATTERNS = [
  /\brm[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+-[rf]/i,
  /\bcurl[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+https?:\/\//i,
  /\bwget[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+https?:\/\//i,
  /\bgit[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+push\b/i,
  /\bgh[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+api\b/i,
];

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
const INJECTION_URL_PATTERNS = [
  /\bvisit[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+[a-z][a-z0-9+.\-]{1,15}:\/\//i,
  /\bdownload[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+from[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+[a-z][a-z0-9+.\-]{1,15}:\/\//i,
];

// Encoded payload patterns -- base64 or hex blobs in findings are injection
// artifacts. Each shape is now two directive-gated entries: a before-branch
// requiring a decode-family verb ahead of the blob, an after-branch requiring
// decode/execute sink syntax after it. A bare encoded-looking run with no
// decode directive nearby (a commit SHA, an opaque config token, a padded
// identifier) no longer matches either branch -- both measured a false-fire
// on ordinary review/DevOps prose where a generic verb (run/curl/wget)
// happened to sit near an unrelated hash-shaped token.
const INJECTION_ENCODED_PATTERNS = [
  /\b(?:decode|base64|atob|b64decode)\b[^\x00]{0,40}[A-Za-z0-9+\/]{40,}={0,2}\b/i,
  /\b[A-Za-z0-9+\/]{40,}={0,2}\b[^\x00]{0,40}(?:\|[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*(?:sh|bash|zsh)\b|base64[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+-d\b|(?:then|and)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:run|execute|eval)\b)/i,
  /\b(?:decode|unhex|xxd|fromhex|unhexlify)\b[^\x00]{0,40}(?<!\w)(?:0x)?[0-9a-fA-F]{32,}(?!\w)/i,
  /(?<!\w)(?:0x)?[0-9a-fA-F]{32,}(?!\w)[^\x00]{0,40}(?:\|[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*(?:xxd|sh|bash)\b|(?:then|and)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:run|execute|eval)\b|-r[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+-p\b)/i,
];

// Bypass / auto-approve instruction patterns. auto-approve is now two
// grammatically-gated entries (a determiner + PR/MR/commit object, or an
// "and <verb>" continuation) instead of a bare phrase match -- the bare
// phrase false-fired on third-person policy prose ("auto-approve changes to
// lockfiles should be gated behind review") where "auto-approve" is the
// grammatical subject, not an imperative.
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

const INJECTION_INSTRUCTIONAL_PATTERNS = [
  /\byou[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+should[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+run\b/i,
  /\bexecute[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+the[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+following\b/i,
  /\brun[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+this[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+command\b/i,
  /\bplease[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+run\b/i,
  /\bpaste[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:this|the[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+following)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+into[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+(?:your[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+)?terminal/i,
  /\bcopy[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+and[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+paste[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+the[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+following\b/i,
];

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

const MIN_BODY_WORDS = 10;
const HIGH_CONFIDENCE_THRESHOLD = 85;

// Matches the union whitespace class respelled into the injection/routing
// patterns above (item 2 of the #211 decision) so a word-count boundary and
// a pattern-match boundary agree on what separates words.
export const WORD_SPLIT_RE = /[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+/;

// Port of _count_words: union-whitespace-split word count, 0 for blank/whitespace-only text.
export function countWords(text) {
  return (text || '').split(WORD_SPLIT_RE).filter(Boolean).length;
}

// Port of _first_match: the pattern SOURCE of the first regex that matches, or null.
function firstMatch(patterns, text) {
  for (const rx of patterns) {
    if (rx.test(text)) return rx.source;
  }
  return null;
}

// suggestion (and, since #213, claude_md_rule/spec_text) is rendered into
// posted PR/MR comments and reports, so payload-bearing advice must not
// reach a human -- but a benign finding must not die for its advice
// (imperative security advice like "Never disable TLS verification"
// legitimately resembles these patterns), so a match strips the field
// instead of eliminating the finding (#62).
export const SUGGESTION_SETS = [
  ['contains shell command pattern', INJECTION_SHELL_PATTERNS],
  ['contains visit-URL pattern', INJECTION_URL_PATTERNS],
  ['contains encoded payload pattern', INJECTION_ENCODED_PATTERNS],
  ['contains bypass/auto-approve instruction', INJECTION_BYPASS_PATTERNS],
  ['uses instructional tone', INJECTION_INSTRUCTIONAL_PATTERNS],
  ['recommends introducing vulnerability', INJECTION_VULN_INTRO_PATTERNS],
  ['matches injection marker', INJECTION_BODY_PATTERNS],
];

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
    for (const [phrase, patterns] of SUGGESTION_SETS) {
      const m = firstMatch(patterns, value);
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

// Port of _strip_injected_prose_fields + _strip_suggested_fix_code_if_needed
// composed as the SINGLE per-finding step applyInjectionFilter runs for every
// KEPT finding, exposed standalone for the #213 replay belt (stages.js): a
// challenge checkpoint recorded by a pipeline version that predates a scanned
// field (e.g. claude_md_rule/spec_text before #213) never had this strip
// applied when it originally ran through filterStage, and a REPLAYED
// checkpoint.challenge bypasses filterStage entirely (the persisted output is
// reused verbatim) -- so report and delivery selection must pass every
// challenge-stage finding through this before reading it. Idempotent: a
// finding a fresh run's applyInjectionFilter already stripped has nothing
// left to match, so a second pass here is a no-op.
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
    const title = finding.title || '';
    const description = finding.description || '';
    const filepath = finding.file || '';
    const confidence = 'confidence' in finding ? finding.confidence : 0;
    const combined = `${title}\n${description}`;

    const reasons = [];

    let m = firstMatch(INJECTION_SHELL_PATTERNS, combined);
    if (m) reasons.push(`contains shell command pattern: ${JSON.stringify(m)}`);

    // 2a/2b: combined title+description (#252 Finding 1 -- see doc comment above).
    m = firstMatch(INJECTION_URL_PATTERNS, combined);
    if (m) reasons.push(`contains visit-URL pattern: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_ENCODED_PATTERNS, combined);
    if (m) reasons.push(`contains encoded payload pattern: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_BYPASS_PATTERNS, combined);
    if (m) reasons.push(`contains bypass/auto-approve instruction: ${JSON.stringify(m)}`);

    if (includeH4) {
      const wordCount = countWords(description);
      if (wordCount < MIN_BODY_WORDS && confidence >= HIGH_CONFIDENCE_THRESHOLD) {
        reasons.push(`suspiciously short description (${wordCount} words) with high confidence (${confidence})`);
      }
    }

    m = firstMatch(INJECTION_INSTRUCTIONAL_PATTERNS, combined);
    if (m) reasons.push(`uses instructional tone: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_VULN_INTRO_PATTERNS, combined);
    if (m) reasons.push(`recommends introducing vulnerability: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_TITLE_PATTERNS, title);
    if (m) reasons.push(`title matches placeholder pattern: ${JSON.stringify(m)}`);

    m = firstMatch(INJECTION_BODY_PATTERNS, combined);
    if (m) reasons.push(`matches injection marker: ${JSON.stringify(m)}`);

    if (!filepath || /<[^\n]*?>|\{[^\n]*?\}/.test(filepath)) {
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
        const convText = `${pyGet(convFinding, 'description', '')} ${pyGet(convFinding, 'title', '')}`.toLowerCase();
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
  /\bcrash\b|\bdata[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+loss\b|\bsilent(?:ly)?\b|\bincorrect\b|\bwrong\b|\bfail(?:s|ure)?\b|\bruntime[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+error\b|\bexception\b|\bpanic\b|\bundefined[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+behavio(?:u)?r\b/i;

// Keywords that promote type_design findings specifically. Ported verbatim
// from _TYPE_SAFETY_BUG_KEYWORDS.
const TYPE_SAFETY_BUG_KEYWORDS =
  /\bruntime\b|\bcastexception\b|\btype[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+error\b|\bclasscastexception\b|\bnull[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+pointer\b|\bnullpointer\b|\btype[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+mismatch\b/i;

// Keyword patterns indicating a test-analyzer finding describes a functional
// correctness bug that EXISTS TODAY (vs. a coverage gap). Ported verbatim,
// in order, from _TEST_CORRECTNESS_PATTERNS -- shared by routeByDimension's
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
// A user kill-switch reaching rendered citation text is an open question,
// tracked separately, not silently declined.
export function applyExclusions(findings, exclusionPatterns) {
  if (!exclusionPatterns || !exclusionPatterns.length) return { kept: findings, eliminated: [] };

  const kept = [];
  const eliminated = [];

  for (const finding of findings) {
    const title = finding.title || '';
    const description = finding.description || '';
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
