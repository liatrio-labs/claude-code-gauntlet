// REVIEW.md parsing, scoped configuration, and exclusion matching.
import { WS_TRIM_RE, escapeRegExp, asText, deepFreeze } from './wire.js';
import { REVIEW_SETTING_KEYS } from './registry.js';

export function cfgGet(config, key, fallback) {
  const v = config ? config[key] : undefined;
  return v === undefined || v === null ? fallback : v;
}

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

// Split all three newline spellings, treating CRLF as one break.
export function splitReviewLines(text) {
  return text.split(/\r\n|\r|\n/);
}

// Review block patterns accept every character except NUL in the block body.
const REVIEW_BLOCK_PATTERNS = deepFreeze([
  /```(?:yaml|)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*#?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*code-gauntlet(?:[^\n]*)?\n([^\x00]*?)```/i,
  /<!--[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*code-gauntlet-config[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\n([^\x00]*?)-->/i,
  /```(?:yaml|)[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*#?[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*deep-review(?:[^\n]*)?\n([^\x00]*?)```/i,
  /<!--[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*deep-review-config[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\n([^\x00]*?)-->/i,
]);

const REVIEW_CONFIDENCE_RE =
  /(?:^|\n)[ \t]*confidence_threshold[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[:=][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*([0-9]{1,3})(?![0-9])/i;

const REVIEW_SECURITY_RE =
  /(?:^|\n)[ \t]*security_min_confidence[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[:=][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*([0-9]{1,3})(?![0-9])/i;

const REVIEW_SEVERITY_RE =
  /(?:^|\n)[ \t]*severity_threshold[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[:=][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*(critical|high|medium|low)/i;

const REVIEW_IGNORE_RE =
  /(?:^|\n)[ \t]*ignore[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*:[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\n((?:[ \t]*-[^\n]*\n?)+)/i;

const REVIEW_IGNORE_ITEM_RE =
  /^[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*-[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*/;

const REVIEW_EXCL_BLOCK_RE =
  /```[^\n]*\n([^\x00]*?)```/;

const REVIEW_EXCL_BULLET_RE =
  /^[\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[-*][\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+([^\n]+)$/;

export const REVIEW_CONFIG_RULES = deepFreeze([
  ...REVIEW_BLOCK_PATTERNS.map((pattern) => ({ kind: 'block', pattern })),
  { kind: 'numeric', key: REVIEW_SETTING_KEYS[0], pattern: REVIEW_CONFIDENCE_RE },
  { kind: 'numeric', key: REVIEW_SETTING_KEYS[1], pattern: REVIEW_SECURITY_RE },
  { kind: 'severity', key: REVIEW_SETTING_KEYS[2], pattern: REVIEW_SEVERITY_RE },
  { kind: 'ignore', pattern: REVIEW_IGNORE_RE, itemPattern: REVIEW_IGNORE_ITEM_RE },
]);

// Parse REVIEW.md text; only keys present in the selected config block enter the result.
export function parseReviewMd(text, rules = REVIEW_CONFIG_RULES) {
  const config = { ignore: [] };

  if (text === undefined || text === null) return config;

  // Prefer code-gauntlet fenced and comment blocks, then legacy deep-review blocks.
  let blockText = '';
  for (const row of rules) {
    if (row.kind !== 'block') continue;
    const m = row.pattern.exec(text);
    if (m) {
      blockText = m[1];
      break;
    }
  }

  // Fall back to the whole file when there is no config block.
  if (!blockText) blockText = text;

  // Anchor each key to a real line start so commented examples stay inert.
  for (const row of rules) {
    if (row.kind === 'block') continue;
    const m = row.pattern.exec(blockText);
    if (!m) continue;
    if (row.kind === 'numeric') {
      const value = parseInt(m[1], 10);
      if (value <= 100) config[row.key] = value;
    } else if (row.kind === 'severity') {
      config[row.key] = m[1].toLowerCase();
    } else if (row.kind === 'ignore') {
      // ignore: consecutive "-"-led lines, indentation-tolerant (spaces or tabs).
      for (const line of splitReviewLines(m[1])) {
        const item = line.replace(row.itemPattern, '').replace(WS_TRIM_RE, '');
        if (item) config.ignore.push(stripMatchingQuotes(item));
      }
    }
  }

  return config;
}

// A REVIEW.md entry owns the directory containing its final REVIEW.md segment.
// Keep layer construction separate from file lookup so the resolver preserves the
// raw discovery shape while the filter can derive a fresh effective view per finding.
function reviewScopeDir(path) {
  const slash = path.lastIndexOf('/');
  return slash < 0 ? '' : path.slice(0, slash);
}

function reviewScopeDepth(dir) {
  return dir.split('/').length;
}

function mergeReviewLayer(layer, parsed, rules) {
  for (const row of rules) {
    if (row.key !== undefined && parsed[row.key] !== undefined) layer[row.key] = parsed[row.key];
  }
  layer.ignore.push(...parsed.ignore);
}

export function buildReviewConfig(entries, rules = REVIEW_CONFIG_RULES) {
  const root = { ignore: [] };
  const scopes = [];
  for (let index = 0; index < entries.length; index++) {
    const entry = entries[index];
    const dir = reviewScopeDir(entry.path);
    const parsed = parseReviewMd(entry.text, rules);
    if (dir === '') {
      mergeReviewLayer(root, parsed, rules);
      continue;
    }
    let scope = scopes.find((candidate) => candidate.dir === dir);
    if (!scope) {
      scope = { dir, ignore: [], _index: index };
      scopes.push(scope);
    }
    mergeReviewLayer(scope, parsed, rules);
  }
  scopes.sort((a, b) =>
    (reviewScopeDepth(a.dir) - reviewScopeDepth(b.dir)) || (a._index - b._index));
  for (const scope of scopes) delete scope._index;
  if (scopes.length > 0) root.scopes = scopes;
  return root;
}

export function scopeMatchesFile(scope, file) {
  return typeof file === 'string'
    && scope && typeof scope.dir === 'string'
    && scope.dir !== ''
    && file.startsWith(`${scope.dir}/`);
}

export function configForFile(config, file, keys = REVIEW_SETTING_KEYS) {
  // Build a flat, non-mutating view: root settings and ignores are followed by
  // matching subtree layers in structural depth order, so array order cannot
  // let a shallower layer override a deeper one.
  const source = config || {};
  // The args waist guarantees the ignore shape is an array at this boundary.
  const view = { ignore: [...(source.ignore === undefined ? [] : source.ignore)] };
  for (const key of keys) {
    if (key in source) view[key] = source[key];
  }
  if (!Array.isArray(source.scopes) || typeof file !== 'string') return view;

  const matching = source.scopes
    .map((scope, index) => ({ scope, index }))
    .filter(({ scope }) => scopeMatchesFile(scope, file))
    .sort(({ scope: a, index: ai }, { scope: b, index: bi }) =>
      (reviewScopeDepth(a.dir) - reviewScopeDepth(b.dir)) || (ai - bi));
  for (const { scope } of matching) {
    for (const key of keys) {
      if (key in scope) view[key] = scope[key];
    }
    view.ignore.push(...(scope.ignore === undefined ? [] : scope.ignore));
  }
  return view;
}

// --- Exclusions loader -------------------------------------------------------

// Parse exclusion text with the same newline handling as REVIEW.md.
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

  for (const line of splitReviewLines(text)) {
    const m = REVIEW_EXCL_BULLET_RE.exec(line);
    if (m) patterns.push(m[1].replace(WS_TRIM_RE, ''));
  }

  return patterns;
}

export function applyExclusions(findings, exclusionPatterns, config = null, keys = REVIEW_SETTING_KEYS) {
  const hasConfig = config !== null && config !== undefined;
  if (!hasConfig && (!exclusionPatterns || !exclusionPatterns.length)) {
    return { kept: findings, eliminated: [] };
  }

  const kept = [];
  const eliminated = [];

  for (const finding of findings) {
    const patterns = hasConfig
      ? [...configForFile(config, finding.file, keys).ignore, ...(exclusionPatterns || [])]
      : (exclusionPatterns || []);
    const title = asText(finding.title);
    const description = asText(finding.description);
    const suggestion = typeof finding.suggestion === 'string' ? finding.suggestion : '';
    const combined = `${title}\n${description}\n${suggestion}`;

    let matchedPattern = null;
    for (const pattern of patterns) {
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
